"""Headless end-to-end check of the layout tools against real PyQGIS.

Not collected by pytest (it needs QGIS). Run with the QGIS Python, e.g.:

    set QT_QPA_PLATFORM=offscreen
    "%OSGEO4W_ROOT%\\bin\\python-qgis-ltr.bat" tests\\verify_layout_tools_qgis.py OUT_DIR

It builds every preset, previews, lints, saves a .qpt into a scratch "user
template" folder, re-creates a layout from it with placeholder fill, and
exports a PDF. Evidence (sizes, issues, preview paths) is printed as JSON and
written to OUT_DIR/layout_tools_evidence.json.
"""
import json
import os
import shutil
import sys
import tempfile

PLUGIN_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, PLUGIN_PARENT)

from qgis.core import (  # noqa: E402
    QgsApplication, QgsFeature, QgsGeometry, QgsPointXY, QgsProject,
    QgsVectorLayer,
)

app = QgsApplication([], False)
app.initQgis()

package = os.path.basename(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
layout_tools = __import__(package + ".bridge.layout_tools",
                          fromlist=["layout_tools"])

OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                      else tempfile.mkdtemp(prefix="qgent_layout_"))
os.makedirs(OUT, exist_ok=True)
TEMPLATE_DIR = os.path.join(OUT, "composer_templates")
shutil.rmtree(TEMPLATE_DIR, ignore_errors=True)
# Point the "user template folder" at scratch space: the real profile's
# composer_templates must never be touched by a test.
layout_tools.user_template_dir = lambda: TEMPLATE_DIR

project = QgsProject.instance()
project.setCrs(project.crs().fromEpsgId(32651))
site = QgsVectorLayer("Polygon?crs=EPSG:32651&field=name:string",
                      "Project Site Boundary", "memory")
feature = QgsFeature(site.fields())
feature.setAttribute("name", "Site")
feature.setGeometry(QgsGeometry.fromPolygonXY([[
    QgsPointXY(290000, 1620000), QgsPointXY(292500, 1620000),
    QgsPointXY(292500, 1622000), QgsPointXY(290000, 1622000)]]))
site.dataProvider().addFeatures([feature])
site.updateExtents()
points = QgsVectorLayer("Point?crs=EPSG:32651&field=station:string",
                        "Sampling Stations", "memory")
for index, (x, y) in enumerate(((290800, 1620600), (291900, 1621400))):
    point = QgsFeature(points.fields())
    point.setAttribute("station", f"SW-{index + 1}")
    point.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
    points.dataProvider().addFeatures([point])
points.updateExtents()
ph = QgsVectorLayer(os.path.join(PLUGIN_PARENT, package, "claude_runtime",
                                 "assets", "ph_outline.geojson"),
                    "Philippines", "ogr")
for layer in (ph, site, points):
    assert layer.isValid(), layer.name()
    project.addMapLayer(layer)

tools = layout_tools.LayoutTools(iface=None)
evidence = {"out_dir": OUT, "steps": []}


def step(name, payload):
    evidence["steps"].append({"step": name, "result": payload})
    print(f"--- {name}")
    print(json.dumps(payload, indent=1, default=str)[:3000])
    return payload


def expect_error(name, call):
    try:
        call()
    except layout_tools.LayoutToolError as exc:
        step(name, {"refused": str(exc)})
        return
    raise AssertionError(f"{name}: expected a LayoutToolError")


# 1. Every preset on several page sizes builds, renders, and lints clean of
#    clipped text / overflow (placeholders are expected to be unfilled).
for preset, page in (("side_panel", {"size": "A3", "orientation": "landscape"}),
                     ("title_strip", {"size": "A4", "orientation": "portrait"}),
                     ("title_strip", {"size": "A1", "orientation": "landscape"}),
                     ("report_figure", {"size": "A4", "orientation": "portrait"})):
    name = f"{preset} {page['size']} {page['orientation']}"
    built = tools.manage({"action": "build", "spec": {
        "name": name, "preset": preset, "page": page,
        "items": [{"id": "map_main", "extent": "layer:Project Site Boundary"},
                  {"id": "legend", "exclude": ["Philippines"]}]
        + ([{"id": "map_inset", "layers": ["Philippines",
                                           "Project Site Boundary"],
             "extent": "layer:Philippines"}] if preset == "side_panel" else []),
    }})
    assert not built.get("item_errors"), built.get("item_errors")
    design_issues = [issue for issue in built["issues"]
                     if not issue.startswith("unfilled placeholders")]
    assert not design_issues, design_issues
    preview = tools.info({"action": "render", "layout": name})
    assert os.path.getsize(preview["snapshot_path"]) > 10000
    kept = os.path.join(OUT, os.path.basename(preview["snapshot_path"]))
    shutil.copyfile(preview["snapshot_path"], kept)
    step(f"build+render {name}", {
        "items": len(built["items"]), "pages": built["pages"],
        "placeholders": built["placeholders"], "issues": built["issues"],
        "preview": kept, "preview_bytes": os.path.getsize(kept),
        "pixels": preview["pixels"]})

# 2. The project's layer tree is untouched by the legend's 'exclude'.
assert project.layerTreeRoot().findLayer(ph.id()) is not None

# 3. Save as a template into the user folder; refuse a silent overwrite.
saved = step("save_template", tools.manage({
    "action": "save_template", "layout": "side_panel A3 landscape",
    "name": "QGent EIA A3 Landscape"}))
assert saved["listed_in_layout_manager"] is True
assert saved["size_bytes"] > 1000
# The inset was locked to two layers and the legend excluded one: the saved
# copy is unpinned, the live layout is not.
assert len(saved.get("made_portable", [])) == 2, saved.get("made_portable")
assert "portability_warnings" not in saved, saved["portability_warnings"]
live = project.layoutManager().layoutByName("side_panel A3 landscape")
assert live.itemById("map_inset").keepLayerSet()
assert not live.itemById("legend").autoUpdateModel()
reloaded = layout_tools._load_template_layout(saved["template_path"])
assert not reloaded.itemById("map_inset").keepLayerSet()
assert reloaded.itemById("legend").autoUpdateModel()
expect_error("save_template refuses overwrite", lambda: tools.manage({
    "action": "save_template", "layout": "side_panel A3 landscape",
    "name": "QGent EIA A3 Landscape"}))
step("save_template overwrite=true", tools.manage({
    "action": "save_template", "layout": "side_panel A3 landscape",
    "name": "QGent EIA A3 Landscape", "overwrite": True}))

# 4. layout_info list shows the new template; describe works on the file.
listing = tools.info({"action": "list"})
names = [entry["name"] for entry in listing["templates"]
         if entry["source"] == "user"]
assert "QGent EIA A3 Landscape" in names, names
step("list", {"layouts": [entry["name"] for entry in listing["layouts"]],
              "user_templates": names, "presets": listing["presets"]})
template_summary = tools.info({"action": "describe",
                               "template": "qgent eia a3 landscape"})
step("describe template", {"pages": template_summary["pages"],
                           "placeholders": template_summary["placeholders"]})

# 5. Create from the template with fill; fill is key-insensitive.
created = step("create_from_template", tools.manage({
    "action": "create_from_template", "template": "QGent EIA A3 Landscape",
    "layout_name": "Site Development Plan",
    "fill": {"map_title": "SITE DEVELOPMENT PLAN",
             "Project Name": "Proposed 5 MW Solar Farm",
             "proponent": "Verde Laya Energy Corp.",
             "[Location]": "Brgy. Example, Alabel, Sarangani",
             "prepared by": "J. Dela Cruz, PCO",
             "date": "October 4, 2026", "Map No.": "EIA-03",
             "source": "NAMRIA; field survey"},
    "map": {"extent": "layer:Project Site Boundary", "scale": 10000}}))
assert created["placeholders"] == [], created["placeholders"]
assert not [issue for issue in created["issues"]
            if "clipped" in issue or "cut off" in issue], created["issues"]
filled_preview = tools.info({"action": "render",
                             "layout": "Site Development Plan"})
kept = os.path.join(OUT, "filled_" + os.path.basename(
    filled_preview["snapshot_path"]))
shutil.copyfile(filled_preview["snapshot_path"], kept)
step("render filled layout", {"preview": kept,
                              "preview_bytes": os.path.getsize(kept)})
expect_error("create_from_template refuses duplicate name", lambda: tools.manage({
    "action": "create_from_template", "template": "QGent EIA A3 Landscape",
    "layout_name": "Site Development Plan"}))

# 6. Iterative update: move/resize an item, edit text, remove one, add one.
updated = step("build update", tools.manage({"action": "build", "spec": {
    "name": "Site Development Plan", "mode": "update",
    "items": [{"id": "map_title", "font": {"size": 18}},
              {"id": "crs_note", "remove": True},
              {"id": "draft_stamp", "type": "label", "text": "DRAFT",
               "rect": [30, 30, 40, 12], "font": {"size": 20, "bold": True,
                                                  "color": "#C00000"}}]}}))
ids = [item["id"] for item in updated["items"]]
assert "crs_note" not in ids and "draft_stamp" in ids, ids

# 7. Lint catches clipped text.
clipped = tools.manage({"action": "build", "spec": {
    "name": "Site Development Plan", "mode": "update",
    "items": [{"id": "draft_stamp",
               "text": "A VERY LONG LABEL THAT CANNOT FIT IN FORTY MM"}]}})
assert any("draft_stamp" in issue and "clipped" in issue
           for issue in clipped["issues"]), clipped["issues"]
step("lint clipped label", [issue for issue in clipped["issues"]
                            if "draft_stamp" in issue])
crowded = tools.manage({"action": "build", "spec": {
    "name": "Site Development Plan", "mode": "update",
    "items": [{"id": "crowded_table", "type": "table",
               "rect": [30, 50, 60, 10],
               "rows": [[f"row {n}", "value"] for n in range(8)]}]}})
assert any("crowded_table" in issue and "cut off" in issue
           for issue in crowded["issues"]), crowded["issues"]
step("lint overflowing table", [issue for issue in crowded["issues"]
                                if "crowded_table" in issue])
tools.manage({"action": "build", "spec": {
    "name": "Site Development Plan", "mode": "update",
    "items": [{"id": "crowded_table", "remove": True},
              {"id": "draft_stamp", "text": "DRAFT"}]}})

# 7b. A main map without legend, scale and north arrow is flagged.
bare = tools.manage({"action": "build", "spec": {
    "name": "Bare map", "page": {"size": "A4", "orientation": "landscape"},
    "items": [{"id": "map_main", "type": "map", "rect": [10, 10, 277, 190],
               "extent": "layer:Project Site Boundary"}]}})
for needle in ("no legend", "no scale bar", "no north arrow"):
    assert any(issue.startswith(needle) for issue in bare["issues"]), (
        needle, bare["issues"])
step("lint missing essentials", [issue for issue in bare["issues"]
                                 if issue.startswith("no ")])
roomy = tools.manage({"action": "build", "spec": {
    "name": "Bare map", "mode": "update",
    "items": [{"id": "boxed_caption", "type": "label", "text": "Caption",
               "rect": [10, 150, 100, 40], "frame": True}]}})
assert any("boxed_caption" in issue and "framed" in issue
           for issue in roomy["issues"]), roomy["issues"]
step("lint mostly empty box", [issue for issue in roomy["issues"]
                               if "boxed_caption" in issue])
tools.manage({"action": "delete", "layout": "Bare map"})

# 8. Export a PDF and refuse to overwrite it silently.
pdf = os.path.join(OUT, "site_development_plan.pdf")
if os.path.exists(pdf):
    os.remove(pdf)
exported = step("export pdf", tools.manage({
    "action": "export", "layout": "Site Development Plan", "path": pdf,
    "dpi": 150}))
assert exported["files"][0]["size_bytes"] > 20000
expect_error("export refuses overwrite", lambda: tools.manage({
    "action": "export", "layout": "Site Development Plan", "path": pdf}))

# 9. Copy a project layout, then delete the copy.
step("duplicate", {"layout": tools.manage({
    "action": "create_from_template",
    "source_layout": "Site Development Plan",
    "layout_name": "Site Development Plan (B)"})["layout"]})
step("delete", tools.manage({"action": "delete",
                             "layout": "Site Development Plan (B)"}))
expect_error("unknown layout", lambda: tools.info(
    {"action": "describe", "layout": "nope"}))

# 10. The bundled vicinity QPT is discoverable as a QGent template.
assert any(entry["source"] == "qgent" for entry in listing["templates"])

with open(os.path.join(OUT, "layout_tools_evidence.json"), "w",
          encoding="utf-8") as handle:
    json.dump(evidence, handle, indent=1, default=str)
print("ALL LAYOUT TOOL CHECKS PASSED ->", OUT)
app.exitQgis()
