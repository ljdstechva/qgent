---
name: layout-templates
description: >
  Design, build, preview, fix, and save QGIS print layouts and reusable layout
  templates (.qpt) with the layout_info and manage_layouts tools — page-adaptive
  presets, a JSON item spec, [placeholder] title blocks, a design lint, PNG
  previews, saving into the Layout Manager's user template list, and
  instantiating templates with filled values. Read before any request to
  create, edit, copy, save, or reuse a print layout or layout template.
---

# Layout Manager work and layout templates

Use the two layout tools instead of hand-written `QgsPrintLayout` code. They
solve page geometry, legend ownership, scale-bar sizing, and table frames
once, and they report design problems you would otherwise miss.

| Tool | Use it to |
|---|---|
| `layout_info` (read-only) | `list` layouts, presets, and every template QGIS can see; `describe` a layout or `.qpt` (items, placeholders, **issues**); `render` a page to PNG |
| `manage_layouts` | `build` (create/update from a spec), `save_template`, `create_from_template`, `export`, `open` (show it in the Layout Designer), `delete` |

For an A4 landscape **Philippine vicinity map**, use `vicinity-map-template`
and its bundled v2 QPT instead; this skill is for every other layout and for
designing new templates.

## The loop — never skip a step

1. **Ground.** `layout_info {action: "list"}` → existing layout names, the
   user template folder, and templates already available. Reuse a matching
   template before designing a new one. Never invent a layout or template name.
2. **Clarify only what changes the design.** Page size/orientation and the
   template's purpose (vicinity, site plan, hazard map, report figure) are
   material; ask once with `ask_user` if absent. Defaults when the user says
   "you decide": maps → A3 landscape `side_panel`; report figures → A4
   portrait `report_figure`; engineering sheets → `title_strip`.
3. **Build.** One `manage_layouts {action: "build"}` with a preset plus item
   overrides. The response is the layout description with `issues`.
4. **Fix every issue** with `build` in `mode: "update"`, patching only the
   items named. In a *template*, the only acceptable issue is
   `unfilled placeholders` — those are the template's fields.
5. **Look at it.** `layout_info {action: "render", layout}` and **read the PNG**.
   Check hierarchy, alignment, empty or crowded panels, legend content, and
   that nothing covers the map's grid labels. The lint cannot judge taste;
   you can. Fix and re-render until it looks like professional cartography.
6. **Save** (templates): `manage_layouts {action: "save_template", layout,
   name}`. It writes `<name>.qpt` to the user template folder, so it appears
   in **Project ▸ Layout Manager ▸ User templates**. Confirm the file with
   `stat_path` and report `template_path` + `size_bytes`.
7. **Show it**: `manage_layouts {action: "open", layout}` opens the Layout
   Designer so the user sees the result.

## Spec reference

All lengths are **millimetres**; `rect` is `[x, y, width, height]` from the
**top-left corner of the page** (`page` is 1-based, default 1).

```json
{"name": "EIA A3 Landscape", "mode": "create",
 "preset": "side_panel",
 "page": {"size": "A3", "orientation": "landscape"},
 "style": {"font": "Arial", "accent": "#1F3864", "text_color": "#1A1A1A",
           "line_color": "#333333", "margin": 8.5},
 "items": [
   {"id": "map_main", "extent": "layer:Project Site Boundary"},
   {"id": "legend", "exclude": ["OpenStreetMap"]},
   {"id": "logo_text", "type": "label", "text": "[Company]",
    "rect": [330, 20, 60, 8], "font": {"size": 9, "bold": true}}
 ],
 "fill": {}, "variables": {}, "images": {}}
```

Page: `{"size": "A0"…"A6" | "B0"…"B6" | "Letter" | "Legal" | "ANSI A"…"E" |
"Arch A"…"E3", "orientation"}` or `{"width": mm, "height": mm}`.
`mode: "update"` patches an existing layout: items are matched by `id`;
missing ids are created; `{"id": …, "remove": true}` deletes one.
`replace: true` with `mode: "create"` replaces a same-named layout (the user
approves it).

Common item keys: `id`, `type`, `rect`, `page`, `frame` (`true` or
`{"width", "color"}`), `background` (`"#FFFFFFE6"` = 90 % white,
`null` = none), `visible`, `locked`, `opacity`, `rotation`, `z`.

| type | keys |
|---|---|
| `map` | `extent`: `"canvas"`, `"layer:Name"` (`"layer:A|B"` combines), `[xmin, ymin, xmax, ymax]` + `extent_crs`; or `center: [lon, lat]` + `scale`; `scale` (else rounded up to a nice scale), `crs`, `layers`: `"visible"` (follow the project — use this in templates) or `[names]`; `grid`: `{style: cross\|solid\|markers\|frame_only, frame: ticks\|zebra\|line\|none, interval?, crs?: "EPSG:4326", format?: dms\|dm\|decimal, annotation_sides: [left, right, top, bottom], font_size}` (no `interval` = adapts to any scale); `overview: {of: "map_main"}` |
| `label` | `text` (literal, `[Placeholder]`, `[% expression %]`), `font: {family, size, bold, italic, color}`, `align` left\|center\|right\|justify, `valign` top\|middle\|bottom, `margin`, `html` |
| `legend` | `map`, `title`, `columns`, `exclude: [layers]`, `layers: [only these]`, `filter_by_map`, `resize_to_contents`, `font_sizes: {title, group, subgroup, item}`, `symbol_size: [w, h]` |
| `scalebar` | `map`, `style` (`Single Box`, `Double Box`, `Line Ticks Middle`, `Line Ticks Down`, `Line Ticks Up`, `stepped`, `hollow`, `Numeric`), `units` m\|km\|ft\|mi\|nmi, `segments`, `segments_left`, `segment_size` (else fitted to `rect` width), `bar_height`, `font`, `align` |
| `north_arrow` | `map` (rotates with it), `path` (default QGIS arrow; or `arrows/NorthArrow_02.svg`), `north` grid\|true, `fill`, `stroke` |
| `picture` | `path` (PNG/SVG/JPG; logos, seals) |
| `rectangle` / `ellipse` | `fill`, `stroke`, `stroke_width`, `radius` |
| `line` | `points: [[x, y], …]` in page mm, `stroke`, `stroke_width` |
| `table` | `rows: [[cell, …], …]` (`"**bold**"`, `[Placeholder]`, `[% expression %]`), `col_fractions: [0.35, 0.65]`, `font`, `cell_margin`, `grid`, `grid_color`; rows spread to fill `rect` |
| `attribute_table` | `layer`, `columns`, `max_rows`, `filter`, `font` |

## Presets (page-adaptive, A4 → A0)

Fonts and margins scale with paper size. Override any item by id.

- **`side_panel`** — map left (~74 %), right panel: `map_title`,
  `project_name`, `north_arrow`, `scale_bar`, `scale_text` (live 1:N),
  `legend`, `inset_title` + `map_inset` (location map with the main extent
  outlined), `crs_note` (live CRS + `[Source]`), `info_table` (Proponent,
  Location, Prepared by, Date, Map No.). Grid labels left/top/bottom.
- **`title_strip`** — full-width map; bottom strip: `logo`, `map_title`,
  `project_name`, `proponent`, `location`, `info_table` (Prepared by, Date,
  live Scale, Map No.). Overlays on white plates: `legend` (top-left),
  `north_arrow` (top-right), `scale_bar` (bottom-left).
- **`report_figure`** — `map_title` above the map, `legend` (2 columns) and
  `north_arrow` + `scale_bar` below, `caption` "Figure [Figure No.].
  [Caption]", `source_note`.

## Placeholders and live fields

- `[Map Title]`-style tokens are the template's fields. They are also the
  convention of the user's **Map Template Builder** plugin, so QGent
  templates work there too. Use Title Case words; no `%` inside.
- `fill` keys match case-, space- and underscore-insensitively:
  `{"project_name": "…"}` fills `[Project Name]`.
- Values that must stay correct when the map changes are expressions, not
  placeholders:
  - scale `1:[% format_number(map_get(item_variables('map_main'), 'map_scale'), 0) %]`
  - CRS `[% map_get(item_variables('map_main'), 'map_crs_description') %]`
  - date `[% format_date(now(), 'MMMM d, yyyy') %]` — only when the user
    wants the print date; compliance maps usually need the fixed `[Date]`.
- Never fill a credential, preparer, license, seal, or TIN the user did not
  supply. Leave the placeholder and say so.

## Design rules

- One font family; hierarchy title 14–20 pt (accent colour), subtitles
  9–11 pt, body 7–9 pt, notes 6–7 pt (A4/A3 values; presets scale them).
- Margins ≥ 7 mm; align panel contents to one left edge and one width.
- Keep ~4 mm of paper clear outside a map frame on every side that shows
  grid labels; drop that side from `annotation_sides` when a panel is attached.
- Legend: exclude basemaps and helper layers; title "LEGEND" or "Legend".
- Group the north arrow and scale bar; give the scale as a live 1:N label.
- Philippine compliance sheets (DENR-EMB, ECC/EIA, WDP): title block with
  project, proponent, location (Brgy., Municipality, Province), prepared by
  (as supplied), date, map number; projection note (e.g. WGS 84 / UTM zone
  51N, EPSG:32651 or PRS92) and data source (NAMRIA, PhilSA, MGB, field
  survey) as supplied.

## Templates that travel

A template is reused in other projects, where this project's layer IDs do
not exist. `save_template` therefore saves a **portable copy** by default:
locked maps follow the visible layers, map themes are dropped, and fixed
legend lists become auto-updating; `made_portable` lists what changed. The
live layout is untouched. Pass `portable: false` only when the user wants a
template tied to this project.

- Every fill-in field is a `[Field Name]` placeholder — never blank lines
  or underscores — or `create_from_template` cannot fill it.
- Prefer `filter_by_map` over `exclude` lists for legends meant to travel.

## Reusing a template

```json
{"action": "create_from_template", "template": "EIA A3 Landscape",
 "layout_name": "Site Development Plan",
 "fill": {"Map Title": "SITE DEVELOPMENT PLAN", "Project Name": "…",
          "Proponent": "…", "Location": "…", "Prepared By": "…",
          "Date": "…", "Map No.": "…", "Source": "…"},
 "map": {"extent": "layer:Project Site Boundary", "scale": 10000}}
```

`source_layout` instead of `template` copies an existing project layout.
The response lists any placeholders still unfilled and new issues (a long
title may now be clipped — shrink its font in an `update`).

## Export

`manage_layouts {action: "export", layout, path: "C:/…/map.pdf", dpi: 300}`
(PDF, PNG, JPG, TIF, SVG). Then `stat_path` the file and cite its size.

## Safety

`replace`, `overwrite` and `delete` raise the user's approval card; a
denial means stop and report. Never overwrite a user's template or layout
without being asked. Raw `removeLayout` / `saveAsTemplate` in
`execute_pyqgis` is also approval-gated — prefer the tools.

## When to drop to execute_pyqgis

Atlas generation (`QgsLayoutAtlas`), HTML or elevation-profile items,
3D map items, multi-frame tables flowing across pages, and data-defined
item properties are outside the tools. Build the base with
`manage_layouts`, then adjust those parts with one `execute_pyqgis`
script that looks items up with `layout.itemById("…")`.

## Report back

Layout name, page size, template path + `stat_path` size, the issues left
(should be none, or only template placeholders), the preview you read, and
anything you could not fill.
