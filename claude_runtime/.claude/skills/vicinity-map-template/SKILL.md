---
name: vicinity-map-template
description: Use for A4 Philippine vicinity maps that must load the bundled QGIS template, preserve its house style, safely handle preparer credentials, and export a verified PDF.
---

# Philippine vicinity-map template

Use this skill together with `cartography-print-layout` and
`ph-environmental-maps`. The bundled assets are:

- `claude_runtime/assets/vicinity_a4_landscape.qpt`
- `claude_runtime/assets/ph_outline.geojson`

The QPT is the source of truth for A4 landscape layouts. Load it; do not rebuild
an A4 title block or layout from scratch.

## Required parameters and safety gate

Ground these values in the request, attached files, or live project before
executing: site coordinates or site layer, project title, client, address,
map title, scale, date, revision, basemap, title-block style, and output PDF.
Optional values are logo path, lot area, title number, legend position, inset
layout, and an LGU boundary layer.

Normalize the enumerated controls before dispatch: `basemap` is
`osm_standard` or `google_road`; `titleblock_style` is `corporate` or `strip`;
`legend_pos` is `top-left` or `bottom-left`; and `inset_layout` is
`right-column` or `left-stack`. Use `top-left` and `right-column` when the two
position controls are omitted. Do not silently coerce an unsupported value.

`prepared_by` is always user-supplied. Never infer or invent a person, title,
license number, seal, or TIN. If it is absent, call `ask_user` exactly once:

```text
question: Who should appear in the Prepared by block?
options:
  - Provide preparer details now
  - Leave the block blank
allow_other: true
```

If the answer is `Leave the block blank`, set both `corp_prepared_by` and
`strip_prepared_by` to the empty string. If the user chooses to provide details
but supplies none, stop before export and report that the required supplied
text is still missing.

## Dispatch and verification

For Claude Supervisor tasks, the Supervisor must `Read` this skill itself, then
embed the complete grounded parameter set and the applicable low-freedom recipe
below in the `cartographer` Task contract. The cartographer's granted tool set
may be QGIS-only: it must not spend an `execute_pyqgis` call on `open()` or any
other local-file read. The Supervisor's Read event plus the cartographer's
`loadFromTemplate` code is the transcript proof that this skill was followed.
The cartographer must make exactly one tool call total: one complete
`execute_pyqgis` call. The contract already supplies the resolved package-based
asset recipe, so the cartographer must not probe guessed paths with `stat_path`,
`list_dir`, `open`, `Read`, `Bash`, `get_project_context`, or any other tool
before execution. Put all layout assertions and the export in the single call;
after it returns, do not call any tool for diagnostics, verification, repair,
or inspection.
Hand all post-execution checks to the Supervisor and `qa-verifier` through the
read-only project-context, snapshot, feature, and path-stat tools.
After the cartographer finishes, dispatch `qa-verifier` and require a terminal
`VERDICT: PASS`. For Codex single-agent tasks, follow the same recipe directly
and perform the equivalent self-verification. In either case, call `stat_path`
on the exported PDF and quote its absolute path and `size_bytes`.

## Load the template

Resolve assets from the installed package, never from QGIS's working directory:

```python
from pathlib import Path
import qgis_chat_agent
from qgis.PyQt.QtCore import QFile
from qgis.PyQt.QtXml import QDomDocument
from qgis.core import QgsPrintLayout, QgsProject, QgsReadWriteContext

package_root = Path(qgis_chat_agent.__file__).resolve().parent
assets = package_root / "claude_runtime" / "assets"
template_path = assets / "vicinity_a4_landscape.qpt"
ph_path = assets / "ph_outline.geojson"

doc = QDomDocument()
source = QFile(str(template_path))
if not source.open(QFile.ReadOnly):
    raise RuntimeError(f"Cannot open bundled template: {template_path}")
try:
    if not doc.setContent(source):
        raise RuntimeError(f"Invalid QPT XML: {template_path}")
finally:
    source.close()

project = QgsProject.instance()
layout = QgsPrintLayout(project)  # empty target required by loadFromTemplate
layout.initializeDefaults()
loaded_items, ok = layout.loadFromTemplate(
    doc, QgsReadWriteContext(), clearExisting=True)
if not ok:
    raise RuntimeError(f"QGIS could not load template: {template_path}")
layout.setName("Vicinity Map - " + project_title)
old = project.layoutManager().layoutByName(layout.name())
if old:
    project.layoutManager().removeLayout(old)
project.layoutManager().addLayout(layout)
```

Do not create another `QgsPrintLayout`; do not create replacement title-block
items. Retrieve all existing items with `layout.itemById(...)` and fail if an
expected ID is missing. In QGIS 3.44, `layout.items()` can also return Qt scene
helpers such as `QGraphicsRectItem` that have no `id()` method; if iteration is
needed, filter with `hasattr(item, "id")` before reading its ID. Prefer direct
`itemById` lookups for this fixed template.

## Configure layers and maps

1. Use a projected main-map CRS. Default to the valid projected project CRS;
   otherwise use `EPSG:32651`. If the supplied site is outside UTM zone 51N,
   choose the grounded Philippine UTM zone deliberately and state it. Set the
   CRS on `map_main`; do not change the live project's CRS as a side effect.
   In an empty project QGIS may auto-adopt the first added layer's CRS through a
   queued signal which fires after synchronous PyQGIS code appears finished.
   Capture the grounded project CRS before adding layers. Add the projected site
   layer first in its own `project.addMapLayer(site_layer)` call, drain queued Qt
   events, and restore the grounded CRS before adding the basemap or locator.
   After all layer additions, and again after PDF export, use the stabilization
   gate below so the live project cannot settle on the basemap or locator CRS.
2. Validate the supplied site coordinate in `EPSG:4326`, then transform it with
   `QgsCoordinateTransform` into the grounded projected map CRS. In an empty
   project, create the new site memory layer in that projected CRS and insert
   the transformed point; do not add a WGS84 site layer first, because QGIS can
   apply a delayed first-layer CRS adoption after the script returns. Style the
   site layer with the exact display name `Project Site` and a `name` field
   whose feature value is also exactly `Project Site`. Style it as a 5.5 mm
   `star` in `#E31A1C`, with a white outline. Label the `name` field in bold red
   with a 1 mm white text buffer. In QGIS 3.44 set label placement with
   `pal.placement = Qgis.LabelPlacement.OverPoint`, importing `Qgis` first with
   `from qgis.core import Qgis`; do not use the legacy
   `QgsPalLayerSettings.OverPoint`, which resolves to the incompatible
   `LabelPredefinedPointPosition` enum. Preserve the star instead of covering
   it with the label: set `pal.quadOffset = Qgis.LabelQuadrantPosition.AboveRight`,
   `pal.xOffset = 3.0`, `pal.yOffset = 3.0`, and
   `pal.offsetUnits = Qgis.RenderUnit.Millimeters`. For a new memory layer,
   insert the
   fixture feature directly
   with `site_layer.dataProvider().addFeature(feature)`; do not enter edit mode
   or call `commitChanges()` merely to populate that new layer. In the target
   QGIS 3.44 runtime this one-feature overload returns one `bool`, so write
   `ok_add = site_layer.dataProvider().addFeature(feature)` and never unpack it
   as `(ok, features)`.
3. Resolve the requested `basemap` without substitution:
   - `osm_standard`: create a real XYZ layer with URL
     `https://tile.openstreetmap.org/{z}/{x}/{y}.png`, provider `wms`, and
     credit `OpenStreetMap contributors`. Never label a local fixture as OSM.
   - `google_road`: use a valid, user-authorized Google Road XYZ layer already
     present in the project, backed by Google's official Map Tiles API. That
     API requires a billing-enabled API key and a 2D-tiles session token; never
     invent, print, persist, or place either secret in the layout. If no such
     layer is grounded, stop and request it rather than using an unofficial
     `mt*.google.com` endpoint or silently falling back to OSM. Credit it
     exactly as required by the configured layer/Google terms. See
     `https://developers.google.com/maps/documentation/tile/roadmap`.
4. Load `ph_outline.geojson` with provider `ogr`, verify one non-empty feature
   in `EPSG:4326`, and style it gray `#808080` with a black outline. This
   country outline belongs only in `map_ph_locator`; never add it to
   `map_main`, where its fill would obscure the basemap.
5. Set `map_main` to the projected CRS, requested scale, and locked layer set
   (site above thematic layers above basemap). Preserve the template map item
   at `x=9, y=9, w=279, h=150 mm`: in QGIS 3.44, `setExtent()` with a
   mismatched rectangle aspect ratio can resize the layout item. Center a
   scale-derived rectangle on the projected site with `zoomToExtent()`, then
   call `setScale()` and assert the item remains 279 x 150 mm. For a scale
   denominator `scale`, the rectangle half-width/half-height in metres are
   `279 * scale / 2000` and `150 * scale / 2000`. Never call `setExtent()` on
   `map_main`, not even as an intermediate square extent before `setScale()`;
   that changes the final aspect/degree span and invalidates the grid contract.
   Set `map_ph_locator` to
   `EPSG:4326`, the Philippines outline plus site, and an extent covering the
   country. If an LGU boundary was supplied, configure and show
   `map_lgu_locator`, `lgu_inset_panel`, and `lgu_inset_title`; otherwise hide
   all three.

The main map's first grid must remain independent of the map CRS. In QGIS 3.44,
use the map item's direct `grid()` accessor exactly as shown. Never call
`map_main.grids().at(0)` (or any `.at(0)` variant):
`QgsLayoutItemMapGridStack` does not provide that API and the map export will
fail before the grid can be configured.

```python
map_main = layout.itemById("map_main")
grid = map_main.grid()
grid.setEnabled(True)
grid.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
grid.setAnnotationEnabled(True)
grid.setAnnotationFormat(QgsLayoutItemMapGrid.DegreeMinuteSecond)
grid.setAnnotationPrecision(3)
for side in (QgsLayoutItemMapGrid.Left, QgsLayoutItemMapGrid.Right,
             QgsLayoutItemMapGrid.Top, QgsLayoutItemMapGrid.Bottom):
    grid.setAnnotationPosition(QgsLayoutItemMapGrid.OutsideMapFrame, side)
for side in (QgsLayoutItemMapGrid.Top, QgsLayoutItemMapGrid.Bottom):
    grid.setAnnotationDirection(QgsLayoutItemMapGrid.Horizontal, side)
for side in (QgsLayoutItemMapGrid.Left, QgsLayoutItemMapGrid.Right):
    grid.setAnnotationDirection(QgsLayoutItemMapGrid.Vertical, side)
```

Do not call `setFrameSideFlag()` or `setFrameSideFlags()`. The bundled template
already supplies the grid frame, and in QGIS 3.44 those methods use a different
flags enum than the `Left`/`Right`/`Top`/`Bottom` annotation-side values above.
All-side annotation is established solely by the four `setAnnotationPosition`
calls and the matching direction calls shown here.

Transform the final main-map extent to `EPSG:4326`. From the fixed candidates
`0.005, 0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25, 0.5, 1.0`, choose each axis
interval closest to four divisions while retaining 3-5 divisions. Use this
exact fail-closed selector; do not minimize interval distance to `span / 4`
without first filtering the feasible candidates:

```python
GRID_INTERVALS = (0.005, 0.01, 0.02, 0.025, 0.05,
                  0.1, 0.2, 0.25, 0.5, 1.0)

def choose_grid_interval(span):
    feasible = [value for value in GRID_INTERVALS
                if 3.0 <= span / value <= 5.0]
    if not feasible:
        raise RuntimeError(f"No 3-5-division grid interval for span {span}")
    selected = min(feasible, key=lambda value: abs(span / value - 4.0))
    if not 3.0 <= span / selected <= 5.0:
        raise RuntimeError("Grid interval postcondition failed")
    return selected

lon_interval = choose_grid_interval(main_extent_4326.width())
lat_interval = choose_grid_interval(main_extent_4326.height())
grid.setIntervalX(lon_interval)
grid.setIntervalY(lat_interval)
```

For the fixed 6.0894 N, 125.1717 E / 1:25,000 / 279 x 150 mm acceptance
fixture, the required results are `intervalX=0.02` and `intervalY=0.01`;
assert those exact values in addition to the general 3-5-division
postcondition. Reject a Philippine result outside 114-127 E or 4-21 N. DMS
annotations must use E and N suffixes and increase monotonically with the true
transformed extent.

## Fill existing items

Use `setText` on the matching IDs. Populate both corporate and strip variants
from the same grounded common values before applying visibility; the hidden
alternative must not retain template braces. Set a missing optional text value
to the empty string. In particular, set both prepared-by IDs to the supplied
block, or both to empty after `Leave the block blank`. No brace placeholder may
remain in any layout label.

| Parameter | Corporate IDs | Strip IDs |
|---|---|---|
| project title | `corp_project_name` | `strip_project_title` |
| map title | `corp_map_title` | `strip_map_title` |
| client | `corp_client_name` | `strip_client_name` |
| address | `corp_address` | `strip_address` |
| prepared by | `corp_prepared_by` | `strip_prepared_by` |
| date | `corp_date` | `strip_date` |
| revision | `corp_rev` | `strip_rev` |
| lot area | `corp_lot_area` | none |
| title number | `corp_title_number` | none |

Set corporate date/revision to `DATE: <date>` and `REV: <revision>`; set strip
date/revision to `<date>` and `REV <revision>` because the strip already has a
`DATE / REV` header. Set `scale_ratio` to
`Scale 1:<rounded denominator>`, `scale_crs` to
`CRS: <main map CRS authid> - <main map CRS description>`, and `scale_source`
to `Source: <truthful basemap credit>`.
Link `scale_bar` to `map_main`; retain style `Single Box`, one left segment,
two right segments, and metres. Set the QGIS-bundled solid black north-arrow
SVG on `north_arrow` by searching `QgsApplication.svgPaths()` and preferring
the grounded `arrows/NorthArrow_04.svg`; only if that exact bundled file is
absent may the first sorted `NorthArrow_*.svg` be used. Retain the separate
`north_label` text `N`. Assert the selected SVG path exists.

Clear the legend model and add only the site and supplied thematic layers; do
not list the basemap or Philippines outline. `corp_logo` and `strip_logo` are
`QgsLayoutItemPicture` objects, not labels. Never include either ID in a text
map and never call `setText()` on them. If no logo was supplied, call
`setPicturePath("")` on both picture items; keep their visibility governed by
the selected title-block variant so the existing frame contract is preserved.
A supplied logo path must exist before it is assigned.

For `titleblock_style=corporate`, show every `corp_` item and
`titleblock_corporate_frame`, and hide every `strip_` item and
`titleblock_strip_frame`. Reverse those groups for `strip`. Do not merely cover
one variant with another. Use `setVisibility(...)` to set item visibility and
`isVisible()` to read or assert it. QGIS layout items do not provide a
`visibility()` getter. Do not implement this by calling `.id()` on every value
from `layout.items()`; that collection includes `QGraphicsRectItem` scene
helpers without an `id()` method in QGIS 3.44. Use the fixed template IDs:

```python
corporate_ids = (
    "titleblock_corporate_frame", "corp_logo_frame", "corp_logo",
    "corp_center_cell", "corp_map_title", "corp_client_name", "corp_address",
    "corp_details_cell", "corp_project_name_label", "corp_project_name",
    "corp_lot_area_label", "corp_lot_area", "corp_title_number_label",
    "corp_title_number", "corp_prepared_by_label", "corp_prepared_by",
    "corp_date", "corp_rev", "corp_seal_frame")
strip_ids = (
    "titleblock_strip_frame", "strip_project_cell", "strip_project_header",
    "strip_project_title", "strip_client_name", "strip_address",
    "strip_map_cell", "strip_map_header", "strip_map_title",
    "strip_prepared_cell", "strip_prepared_header", "strip_prepared_by",
    "strip_date_cell", "strip_date_header", "strip_date", "strip_rev",
    "strip_spare_frame", "strip_logo")
show_corporate = titleblock_style == "corporate"
for item_id in corporate_ids:
    layout.itemById(item_id).setVisibility(show_corporate)
for item_id in strip_ids:
    layout.itemById(item_id).setVisibility(not show_corporate)
```

The template contract requires every listed ID, so fail before export if any
direct lookup is missing; do not silently skip a missing title-block cell.

Apply the two position controls by moving the existing items in millimetres;
never create replacements. `legend_pos=top-left` places `legend_panel` at
`(13,18)` and `legend` at `(15,20)`. `bottom-left` places them at `(13,110)`
and `(15,112)`. `inset_layout=right-column` places the PH panel/title/map at
`(224,16)`, `(225,17)`, `(227,23)` and the optional LGU panel/title/map at
`(224,70)`, `(225,71)`, `(227,77)`. `left-stack` moves those same triplets to
`(72,16)`, `(73,17)`, `(75,23)` and `(72,70)`, `(73,71)`, `(75,77)`.

Keep the 91 x 32 mm scale panel at y=110 and resolve collisions deterministically:
use x=134 for `left-stack`, x=70 for `right-column` plus a bottom-left legend,
and x=13 otherwise. The child x coordinates are **panel-relative**; never move
them to the raw offsets `3`, `54`, `3`, `3`. Apply this exact formula:

```python
scale_x = (134 if inset_layout == "left-stack" else
           70 if inset_layout == "right-column" and legend_pos == "bottom-left"
           else 13)
move("scale_panel", scale_x, 110)
move("scale_bar", scale_x + 3, 113)
move("scale_ratio", scale_x + 54, 121)
move("scale_crs", scale_x + 3, 127)
move("scale_source", scale_x + 3, 133)
```

Assert the five resulting positions equal those values, every moved item stays
within the main-map rectangle, and no two white panels overlap. Use
`QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters)` with `attemptMove()`.

## Export gate

Refresh the layout and export searchable text explicitly. The two calls to
`stabilize_project_crs()` are mandatory in an initially empty project: export
can process the delayed first-layer signal even when an earlier synchronous
assertion passed.

```python
from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import Qgis, QgsLayoutExporter

def stabilize_project_crs():
    # Let any queued empty-project auto-adoption run, then overwrite it. Drain
    # once more and set the grounded CRS last so no queued layer CRS can win.
    for _ in range(3):
        QCoreApplication.processEvents()
    project.setCrs(grounded_project_crs)
    for _ in range(3):
        QCoreApplication.processEvents()
    project.setCrs(grounded_project_crs)
    if project.crs().authid() != grounded_project_crs.authid():
        raise RuntimeError(
            f"Project CRS changed unexpectedly: {project.crs().authid()}")

stabilize_project_crs()
exporter = QgsLayoutExporter(layout)
settings = QgsLayoutExporter.PdfExportSettings()
settings.textRenderFormat = Qgis.TextRenderFormat.AlwaysText
result = exporter.exportToPdf(str(output_pdf), settings)
if result != QgsLayoutExporter.Success:
    raise RuntimeError(f"PDF export failed with code {result}")
stabilize_project_crs()
```

Approve a write only when its resolved path is the exact user-requested PDF.
Keeping the layout and layers in the live project means keeping them in memory;
never call `QgsProject.write()`, `project.write()`, or otherwise save/overwrite
the project as part of this workflow. Do not write any second artifact.
Then call `stat_path`, verify a nonzero byte size, reopen the PDF, and confirm
its supplied labels are searchable text. The final verifier must check:

- A4 landscape (297 x 210 mm), white page, black frames;
- main map above the title block with a real basemap;
- all-side EPSG:4326 DMS labels matching true Philippine coordinates;
- red star and `Project Site` label;
- filtered legend, scale bar/ratio/CRS/source, and north arrow;
- Philippines locator with the site in the correct relative position;
- the requested title-block variant and every supplied text value;
- no `{{...}}`, fabricated credentials, backend error, or unverified export.

For A3 landscape or a custom large sheet such as 900 x 500 mm, rebuild from
the same proportions and ID contract, then save a separate template. Never
stretch the bundled A4 template blindly.

## Bundled outline provenance

`ph_outline.geojson` is derived from Natural Earth 1:10m Admin-0 Countries
version 5.1.1, public domain, downloaded 2026-07-21 from
`https://naturalearth.s3.amazonaws.com/5.1.1/10m_cultural/ne_10m_admin_0_countries.zip`.
It selects the grounded field/value `ADM0_A3=PHL`, retains only `name`, repairs
geometry if necessary, simplifies at 0.005 degrees, and writes EPSG:4326 with
five-decimal coordinate precision. The installed GeoJSON therefore does not
retain an `ADM0_A3` field; do not treat its absence as a validation failure or
make a follow-up PyQGIS diagnostic call for it. Do not treat the outline as a
cadastral or official NAMRIA boundary.
