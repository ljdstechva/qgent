---
name: vicinity-map-template
description: Use for professional A4-landscape Philippine vicinity maps that must load the bundled QGent v2 template, ask for missing required title data, preserve supplied credentials, and export a verified PDF.
---

# Philippine vicinity-map template

**Version 2.0 (Goal 18).** Use this skill with `cartography-print-layout` and
`ph-environmental-maps`. The bundled assets are:

- `claude_runtime/assets/vicinity_a4_landscape.qpt`
- `claude_runtime/assets/ph_outline.geojson`

The QPT is the source of truth for A4 landscape. Load it and fill its stable
IDs. Never rebuild this layout, stretch it to another page size, create a
second north arrow, or draw replacement title-block cells.

## Grounded inputs and required questions

Ground site coordinates or a site layer, project title, map title, client,
address, prepared-by text, date, revision, scale, basemap, title-block style,
optional logo/lot/title/seal values, and the exact output PDF. Accepted scales
are `10000`, `12500`, `25000`, and `50000`; accepted title-block styles are
`corporate` and `strip`; basemap is `osm_standard` or `google_road`. V2 fixes
the legend at top-left and the Philippines inset at top-right. Do not silently
coerce unsupported values or invent missing values.

`project_title` is required. `Project Site`, `Untitled Project`, a filename,
and other placeholders are prohibited. If it is absent, call `ask_user`
exactly once so QGent renders the Goal 16 QuestionCard:

```text
question: A project title is required in the vicinity-map title block. How should I proceed?
options:
  - Enter project title now
  - Cancel this export
allow_other: true
```

Use a grounded free-form answer as the title. If no title is supplied, stop;
do not export. The minimal fixture must therefore show exactly one QuestionCard.

Never infer a preparer identity, title, license, seal, or TIN. If
`prepared_by` is absent, ask once using the existing credential-safety choice:

```text
question: Who should appear in the Prepared by block?
options:
  - Provide preparer details now
  - Leave the block blank
allow_other: true
```

An explicit blank choice hides the complete prepared-by group so it cannot
leave an empty framed cell. A supplied identity must be reproduced exactly.
When both title and preparer are initially missing, resolve the project title
first; a subsequent preparer question is allowed only because the first answer
exposes that still-material credential choice. Agent-run fixtures must supply
the preparer so the missing-title test still asks exactly one QuestionCard.

Map title defaults to `VICINITY MAP`; revision defaults to `Rev. 0`. Date is
the grounded request date or current task date. All other optional fields are
omitted when absent.

## Execution contract

For Claude Supervisor tasks, the Supervisor reads this skill and gives the
cartographer one complete, low-freedom `execute_pyqgis` contract. The
cartographer makes exactly one QGIS tool call; post-execution inspection goes
to the Supervisor and `qa-verifier`. For Codex, execute the same recipe
directly and self-verify. In both modes:

1. use only fixture data for agent-run tests;
2. never save or overwrite the live QGIS project;
3. approve only the exact requested PDF path;
4. call `stat_path` after export and report its absolute path and `size_bytes`;
5. reopen/render the PDF and require the full v2 checklist below.

## Load v2 and require all 56 stable IDs

```python
from pathlib import Path
import qgis_chat_agent
from qgis_chat_agent.vicinity_text import (
    assert_layout_unicode, format_coordinate_pair)
from qgis.PyQt.QtCore import QFile, QCoreApplication
from qgis.PyQt.QtXml import QDomDocument
from qgis.core import (
    Qgis, QgsApplication, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsFillSymbol, QgsLayoutExporter,
    QgsLayoutItemLabel, QgsLayoutItemLegend, QgsLayoutItemMapGrid,
    QgsLayoutItemPicture, QgsLayoutItemShape, QgsLayoutPoint, QgsLayoutSize,
    QgsLineSymbol, QgsPrintLayout, QgsProject, QgsReadWriteContext,
    QgsRectangle, QgsUnitTypes, QgsVectorLayer)

package_root = Path(qgis_chat_agent.__file__).resolve().parent
assets = package_root / "claude_runtime" / "assets"
template_path = assets / "vicinity_a4_landscape.qpt"
ph_path = assets / "ph_outline.geojson"

doc = QDomDocument()
source = QFile(str(template_path))
if not source.open(QFile.ReadOnly):
    raise RuntimeError(f"Cannot open bundled template: {template_path}")
try:
    parsed = doc.setContent(source)
    parsed_ok = parsed[0] if isinstance(parsed, tuple) else parsed
    if not parsed_ok:
        raise RuntimeError(f"Invalid QPT XML: {template_path}")
finally:
    source.close()

project = QgsProject.instance()
layout = QgsPrintLayout(project)
layout.initializeDefaults()
loaded_items, ok = layout.loadFromTemplate(
    doc, QgsReadWriteContext(), clearExisting=True)
if not ok:
    raise RuntimeError(f"QGIS could not load template: {template_path}")
layout.setName("VICINITY MAP")
layout_manager = project.layoutManager()
if not layout_manager.addLayout(layout):
    raise RuntimeError("Could not retain the vicinity layout in the project")
if layout not in layout_manager.layouts():
    raise RuntimeError("Vicinity layout is not owned by the project manager")

EXPECTED_IDS = {
    "map_main", "map_ph_locator", "map_lgu_locator", "ph_inset_panel",
    "ph_inset_title", "lgu_inset_panel", "lgu_inset_title", "legend_panel",
    "legend", "scale_panel", "scale_bar", "scale_ratio", "scale_crs",
    "scale_source", "north_arrow", "north_label",
    "titleblock_corporate_frame", "corp_logo_frame", "corp_logo",
    "corp_center_cell", "corp_map_title", "corp_site_coordinates",
    "corp_client_name", "corp_address", "corp_details_cell",
    "corp_project_name_label", "corp_project_name", "corp_lot_area_label",
    "corp_lot_area", "corp_title_number_label", "corp_title_number",
    "corp_prepared_by_label", "corp_prepared_by", "corp_date", "corp_rev",
    "corp_seal_frame", "corp_seal_label", "titleblock_strip_frame",
    "strip_project_cell", "strip_project_header", "strip_project_title",
    "strip_client_name", "strip_address", "strip_map_cell",
    "strip_map_header", "strip_map_title", "strip_site_coordinates",
    "strip_prepared_cell", "strip_prepared_header", "strip_prepared_by",
    "strip_date_cell", "strip_date_header", "strip_date", "strip_rev",
    "strip_spare_frame", "strip_logo"}
items = {item.id(): item for item in layout.items()
         if hasattr(item, "id") and item.id()}
missing = sorted(EXPECTED_IDS - set(items))
if missing or len(EXPECTED_IDS) != 56:
    raise RuntimeError(f"V2 template ID contract failed: missing={missing}")
```

Do not call `.id()` without the `hasattr` filter; QGIS scene helpers have no
ID. Do not create another `QgsPrintLayout` or replacement layout item. Add the
loaded layout to `project.layoutManager()` exactly once and never remove it;
`qa-verifier` must be able to inspect that same live layout after export.

## Layers, site, and balanced main extent

Use a projected Philippine UTM CRS. Prefer the grounded valid projected
project CRS; otherwise deliberately choose the correct zone (EPSG:32651 for
the three v2 fixtures). Never change the live project CRS as a side effect.

Create the main `Project Site` layer in the projected map CRS, with one point,
a 5.5 mm `star`, fill `#E31A1C`, white outline, and an optional bold red
`Project Site` label with a 1 mm white buffer. Offset the label toward open
map area and verify it does not touch a panel. In QGIS 3.44 use
`Qgis.LabelPlacement.OverPoint` and `Qgis.LabelQuadrantPosition`; do not use
legacy `QgsPalLayerSettings.OverPoint`. A new memory provider's
`addFeature(feature)` returns one boolean, not a tuple.

Create a separate WGS84 locator-only point layer with a 2.6 mm red star. It is
used only by `map_ph_locator` and excluded from the legend. Load the bundled
Philippines outline with provider `ogr`; require one non-empty EPSG:4326
feature. Give it gray fill and black outline and use it only in the locator.

For `osm_standard`, use the real XYZ URL
`https://tile.openstreetmap.org/{z}/{x}/{y}.png` with provider `wms` and credit
`OpenStreetMap contributors`. Never label a fixture as OSM. For
`google_road`, use only a grounded user-authorized official Map Tiles layer;
never invent or expose a key/token and never fall back silently.

Set `map_main` to `(7, 7, 283, 156) mm`, the projected CRS, requested nice
scale, and locked layers: site, supplied thematic layers, basemap. Preserve
that item size. Center a scale-derived rectangle using half-width
`283 * scale / 2000` and half-height `156 * scale / 2000`; use
`zoomToExtent()` then `setScale()`, never `setExtent()` on `map_main`.

Measure `land_fraction = area(country intersection extent) / area(extent)`
after transforming the bundled Philippines geometry to the main CRS. Also
render the unlabelled map body to memory at 96 dpi and split its interior into
10 x 10 tiles. A grayscale 0-255 tile is featureless only when pixel standard
deviation is below 6 **and** mean Sobel edge-gradient magnitude is below 4;
set `featureless_fraction = featureless_tiles / 100`.

Balance when `land_fraction < 0.50` or `featureless_fraction > 0.50`. The water
target is the centroid of the country intersection with a two-times-expanded
base extent. The content target is the weighted centroid of non-featureless
tile centers with `standard_deviation + mean_edge_gradient` as the positive
weight. Use the water target for a water-only trigger, the content target for
a featureless-only trigger, or their arithmetic midpoint when both trigger and
contentful tiles exist. With no contentful tile, use the water target when
available; otherwise retain the site-centered extent and record that fallback.
Clamp x/y independently to 25% of base width/height and require the site to
remain within 30% of each dimension from the new center (the central-60%
postcondition). Record both fractions, targets, selected target, proposed and
clamped shifts, fallback, and postcondition.

Choose the site-label quadrant from AboveRight, AboveLeft, BelowRight, and
BelowLeft at 3 mm x/y offsets. Estimate a 30 x 5 mm label rectangle; reject a
candidate crossing the frame or legend/scale/inset/north rectangles. Render
the unlabelled map to memory, measure edge density under each remaining
rectangle, and choose the least-cluttered result, tie-breaking in the order
listed. Record all scores and the selected quadrant, then visually verify the
final label against the marker, roads, and panels.

## Grid: exterior ticks plus subtle crosses

The first grid remains EPSG:4326 even when the map is projected. QGIS 3.44
uses `map_main.grid()`; never use `map_main.grids().at(0)`.

```python
NICE_GRID_STEPS = (15/3600, 30/3600, 1/60, 2/60, 5/60)

def nice_grid_step(span):
    feasible = [step for step in NICE_GRID_STEPS
                if 3.0 <= span / step <= 5.0]
    if not feasible:
        raise RuntimeError(f"No 3-5-division nice grid step for span {span}")
    return min(feasible, key=lambda step: abs(span / step - 4.0))

grid = layout.itemById("map_main").grid()
grid.setEnabled(True)
grid.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
grid.setStyle(QgsLayoutItemMapGrid.Cross)
grid.setCrossLength(4.0)
cross = QgsLineSymbol.createSimple(
    {"line_color": "#A6A6A6", "line_width": "0.18"})
grid.setLineSymbol(cross)
grid.setFrameStyle(QgsLayoutItemMapGrid.ExteriorTicks)
grid.setFrameWidth(2.5)
grid.setFramePenSize(0.35)
grid.setAnnotationEnabled(True)
grid.setAnnotationFormat(QgsLayoutItemMapGrid.DegreeMinuteSecondPadded)
grid.setAnnotationPrecision(0)
grid.setRotatedAnnotationsMarginToCorner(8.0)
grid.setRotatedTicksMarginToCorner(8.0)
grid.setRotatedAnnotationsEnabled(True)
grid.setRotatedTicksEnabled(True)
for side in (QgsLayoutItemMapGrid.Top, QgsLayoutItemMapGrid.Bottom):
    grid.setAnnotationDisplay(QgsLayoutItemMapGrid.LongitudeOnly, side)
    grid.setAnnotationPosition(QgsLayoutItemMapGrid.OutsideMapFrame, side)
    grid.setAnnotationDirection(QgsLayoutItemMapGrid.Horizontal, side)
for side in (QgsLayoutItemMapGrid.Left, QgsLayoutItemMapGrid.Right):
    grid.setAnnotationDisplay(QgsLayoutItemMapGrid.LatitudeOnly, side)
    grid.setAnnotationPosition(QgsLayoutItemMapGrid.OutsideMapFrame, side)
    grid.setAnnotationDirection(QgsLayoutItemMapGrid.Vertical, side)
```

Transform the final extent to WGS84, set each interval with
`nice_grid_step()`, and require 3–5 divisions. Annotations must be monotonic,
within 114–127 E and 4–21 N, and show whole seconds. The approved v2 style is
exterior ticks; use Zebra only if a preserved rendered comparison proves the
rotated exterior-tick result illegible.

## Compact fixed panels and exactly one arrow

- Keep the legacy `legend_panel` hidden. Put `legend` at `(15,15)`, enable
  resize-to-contents, **1.5 mm** box space, 95% white background, 0.3 mm frame,
  exact title `LEGEND` in 10 pt bold and 8 pt item text. Clear its model and add
  only Project Site and supplied thematic layers. Apply the content styling
  before `adjustBoxSize()`; never force-clip an auto-sized legend with
  `attemptResize()`. Use this QGIS 3.44 recipe (with `QFont`,
  `QgsLegendStyle`, and `QgsLayoutMeasurement` imported):

  ```python
  title_font = QFont()
  title_font.setPointSizeF(10.0)
  title_font.setBold(True)
  item_font = QFont()
  item_font.setPointSizeF(8.0)
  legend.setStyleFont(QgsLegendStyle.Title, title_font)
  legend.setStyleFont(QgsLegendStyle.SymbolLabel, item_font)
  legend.setSymbolHeight(3.0)
  legend.setSymbolWidth(4.0)
  legend.setBoxSpace(1.5)
  legend.setBackgroundEnabled(True)
  legend.setBackgroundColor(QColor(255, 255, 255, 242))
  legend.setFrameEnabled(True)
  legend.setFrameStrokeWidth(QgsLayoutMeasurement(0.3, MM))
  for component in (
          QgsLegendStyle.Title,
          QgsLegendStyle.Symbol,
          QgsLegendStyle.SymbolLabel):
      legend.rstyle(component).setMargin(QgsLegendStyle.Top, 0.8)
  legend.setResizeToContents(True)
  legend.adjustBoxSize()
  legend_size = legend.sizeWithUnits()
  if legend_size.width() > 34 or legend_size.height() > 18:
      raise RuntimeError(
          "Site-only legend exceeds 34 x 18 mm after content styling: "
          f"{legend_size.width():.3f} x {legend_size.height():.3f}")
  ```

  This post-`adjustBoxSize()` exception must run before export. For a site-only
  legend, both measured dimensions are hard gates. `setFrameStrokeWidth()`
  requires `QgsLayoutMeasurement`; do not pass a bare float.
- Keep `scale_panel` at `(15,129,64,26)`. Link `scale_bar` to `map_main`, style
  `Single Box`, one left/two right segments, 2.2 mm bar height, 8 pt numerals.
  Use metres per segment `{10000:100, 12500:100, 25000:250, 50000:500}`;
  this keeps the three-segment bar near 24--30 mm and leaves room for endpoint
  numerals inside the panel. Fail if the bar or its `m` label crosses the
  64 x 26 mm panel border.
  Set `scale_ratio` to `Scale 1 : 25 000` style (7.5 pt regular) below the
  bar. Set the CRS line in this exact order to
  `CRS: EPSG:<code> — <description>` (construct the U+2014 with
  `chr(0x2014)`) and set the OSM credit exactly to
  `Source: OpenStreetMap contributors`; both are 6.5 pt `#444444`.
- Keep the single outer `ph_inset_panel` at `(233,15,49,54)`. Disable the
  frame on `map_ph_locator`, hide `ph_inset_title`, and pad the outline extent
  by at least 8% on each axis before aspect correction. Require the 2.6 mm
  locator star to remain fully inside.
- The template already contains the north arrow: **NEVER add another**. Use
  only `north_arrow` at `(269,79,11,11)` with the QGIS-bundled solid
  `arrows/Arrow_02.svg` and the one `north_label` at `(272,75,5,4)`. Give each
  existing item its own 60%-opaque white background; together those two halos
  protect the 11 x 15 mm group without another item or stable ID. The group
  starts 6 mm below the inset and remains at least 8 mm inside the map frame.

  ```python
  for arrow_item_id in ("north_arrow", "north_label"):
      arrow_item = items[arrow_item_id]
      arrow_item.setBackgroundEnabled(True)
      arrow_item.setBackgroundColor(QColor(255, 255, 255, 153))
  ```

  Apply that background directly to the two existing items. Never construct a
  `QgsLayoutItemShape`, picture, label, panel, or other halo item for this
  purpose, and never call `layout.addLayoutItem()`.

Before export, count visible picture items whose stable ID or SVG basename
identifies an arrow. Require exactly one and require its ID to be
`north_arrow`; this catches an agent-created duplicate.

## Unicode fill and adaptive title block

Never embed a model-authored DMS literal. Generate it from numeric inputs:

```python
site_coordinates = format_coordinate_pair(latitude, longitude, precision=1)
```

This helper emits U+00B0 DEGREE SIGN, U+2032 PRIME, and U+2033 DOUBLE PRIME
from explicit code points. Goal 18 also fixes the demonstrated upstream cause:
the MCP stdio bridge now selects strict UTF-8 before reading a Windows pipe,
instead of allowing UTF-8 byte C2 to become U+00C2 under cp1252. Fill both
coordinate IDs with the returned string. Fill both corporate and strip
variants before selecting visibility; no hidden label may retain braces.

Populate common IDs from grounded values. Corporate date is `DATE: <date>`;
strip date is `<date>`; revision is normalized to `Rev. 0` and shown as
`REV: 0` / `REV 0`. Scale CRS uses `chr(0x2014)` for the em dash. After all
labels are filled, call `assert_layout_unicode(layout)`. It rejects U+00C2,
U+FFFD, unresolved braces, missing DMS glyphs, and mismatched coordinate IDs.

Choose title-block height by populated optional groups: 26 mm with none,
28 mm with one, 30 mm with two or three, and 32 mm with four or more. Place
its bottom at y=203 mm (`y = 203 - height`). Resize/move existing stable IDs;
do not create items.

Exact corporate rectangles (`cx/cw` mean the selected center cell):

- outer `(7,y,283,h)` and details `(142,y,148,h)`;
- logo supplied: logo cell `(7,y,32,h)`, picture
  `(10.5,y+3.5,25,min(25,h-7))`, center `(39,y,103,h)`;
- logo absent: hide logo cell/picture and use center `(7,y,135,h)`;
- seal supplied: frame `(265,y+(h-23)/2,23,23)` plus non-empty `SEAL` label,
  and details text ends at x=263; absent: hide both and end text at x=288;
- no client/address: map title `(cx+2,y+4,cw-4,8)` and coordinates
  `(cx+2,y+13,cw-4,5)`; otherwise allocate supplied lines below coordinates;
- project begins at y+1; a lot/title row exists only when supplied; prepared
  follows it (or project), and date/revision occupy y+h-7 through y+h-1;
- always show map title, Unicode coordinate line, project title, date, and a
  grounded prepared-by group;
- show client/address and their text only when supplied;
- show each lot-area/title-number label/value pair only when supplied;
- show `corp_seal_frame` and non-empty `corp_seal_label` only when
  `include_seal=true`; otherwise hide both and expand adjacent text;
- no text rectangle may cross its exact cell.

Exact strip rectangles:

- outer/project/map `(7,y,283,h)`, `(7,y,108,h)`, `(115,y,65,h)`;
- logo supplied: prepared `(180,y,57,h)`, date `(237,y,25,h)`, logo cell
  `(262,y,28,h)`, picture `(265,y+3.5,22,min(25,h-7))`;
- logo absent: hide logo cell/picture, prepared `(180,y,70,h)`, date
  `(250,y,40,h)`;
- explicit blank preparer: hide its whole group and expand map to
  `(115,y,135,h)` without logo or `(115,y,122,h)` with logo;
- project header is y+1..y+5 and project title expands through omitted
  client/address rows; map header is y+1..y+5, title starts y+5, coordinates
  start y+16; other rows stay within their resized cells;
- always show project title, map title, Unicode coordinate line, date/revision,
  and grounded preparer;
- hide absent client/address labels and expand the project-title area;
- never show a spare or logo frame without an image.

Treat each optional pair/group atomically: logo, lot, title number, seal,
client/address, and prepared group. Show only the selected style's IDs. For
every visible cell shape, assert at
least one contained, visible label has non-whitespace text or one contained,
visible picture has an existing path. Outer frames are satisfied by their
visible child cells. No drawn cell may be empty; no placeholder may survive.
Assert exactly 56 IDs, no missing/extras/duplicates, and no visible empty
labeled field after every fixture fill.

## Project-CRS stability and export gate

In an initially empty project, QGIS can auto-adopt the first layer CRS through
a queued signal. Capture the grounded project CRS before adding layers; add
the projected site first, drain Qt events, restore the grounded CRS, and repeat
this stabilization immediately before and after PDF export:

```python
def stabilize_project_crs():
    for _ in range(3):
        QCoreApplication.processEvents()
    project.setCrs(grounded_project_crs)
    for _ in range(3):
        QCoreApplication.processEvents()
    project.setCrs(grounded_project_crs)
    if project.crs().authid() != grounded_project_crs.authid():
        raise RuntimeError("Project CRS changed unexpectedly")

layout.refresh()
assert_layout_unicode(layout)
stabilize_project_crs()
settings = QgsLayoutExporter.PdfExportSettings()
settings.textRenderFormat = Qgis.TextRenderFormat.AlwaysText
result = QgsLayoutExporter(layout).exportToPdf(str(output_pdf), settings)
if result != QgsLayoutExporter.Success:
    raise RuntimeError(f"PDF export failed with code {result}")
stabilize_project_crs()
```

Never call `project.write()` or `QgsProject.write()`. Then `stat_path` the
exact PDF, reopen it, verify searchable supplied labels, render the whole page,
and inspect at original resolution.

## V2 verification checklist

Record PASS/FAIL for all 16 items on all three runs unless scoped otherwise:

1. no full interior lines; exterior ticks or the evidenced Zebra choice;
2. subtle 4 mm, 0.18 mm, 35%-gray crosses at intersections;
3. whole-second DMS annotations, 3–5 per axis, vertical sides, clear corners;
4. annotations match true monotonic Philippine extent coordinates;
5. exactly one 11 mm arrow with required position and clearance;
6. no U+00C2; correct degree/prime glyph crop preserved;
7. legend tightly auto-sized (site-only cap 34 x 18 mm);
8. compact scale panel, text below bar, size at most 64 x 26 mm;
9. inset single-framed, padded, marker fully inside and correctly located;
10. no overlaps; floating panels follow 8 mm offsets/3 mm gutters;
11. site marker/label legible and site in central 60%;
12. title block has required data, no empty cells, optional groups correctly
    present/absent (presence in run 1; omission in run 2);
13. run 2 only: exactly one project-title QuestionCard and no placeholder;
14. map dominates with no trapped dead-space block; give one-sentence visual
    judgment;
15. `qa-verifier` PASS plus `stat_path` evidence for every PDF;
16. every PDF opens and has a preserved whole-page render.

The required live fixtures are: rejected coastal corporate/full at
5°52′23″N, 125°04′48.9″E and 1:25 000; coastal corporate/minimal with one
project-title QuestionCard; and inland strip at 14.65 N, 121.05 E and
1:10 000. Preserve the v1/v2 coastal A/B PNG. Agent-run evidence must be
honestly labelled and must never use a client file.

## Outline provenance

`ph_outline.geojson` is the unchanged Natural Earth 1:10m Admin-0 Countries
5.1.1 public-domain Philippines selection (`ADM0_A3=PHL`), simplified to
0.005 degrees and written in EPSG:4326. It retains only `name`; do not demand
an `ADM0_A3` field or represent it as an official NAMRIA boundary.
