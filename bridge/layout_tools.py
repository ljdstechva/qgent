# -*- coding: utf-8 -*-
"""Print-layout tools: inspect, build from a spec, preview, and template I/O.

Backs two MCP tools so an agent can work the Layout Manager on its own:

* ``layout_info`` (read-only): ``list`` project layouts and every template
  folder QGIS reads, ``describe`` a layout or .qpt with a design lint
  (clipped text, overflowing legends, off-page or colliding items, unfilled
  ``[placeholders]``), and ``render`` a page to PNG so the agent can *see*
  its design before calling it done.
* ``manage_layouts``: ``build`` a layout from a JSON spec or preset,
  ``save_template`` into the user template folder that Layout Manager lists,
  ``create_from_template`` with placeholder fill, ``export``, ``open`` the
  designer, and ``delete``.

A declarative spec beats generated PyQGIS here: page geometry, item IDs and
fonts become data the agent can patch item-by-item, and the fiddly API
(legend model ownership, scale-bar sizing, table frames) is solved once.

Main thread only — called from :class:`MainThreadExecutor`. Overwrites,
replacements and deletes are approved by the user at the bridge *before* a
call arrives here (see ``safety.layout_reasons``); this module still refuses
to overwrite unless the caller passed the flag that triggered that approval.
"""
import json
import math
import os
import re
import tempfile

from qgis.PyQt.QtCore import QFile, QIODevice, QPointF, QRectF, QSize, Qt
from qgis.PyQt.QtGui import QColor, QFont, QPolygonF
from qgis.PyQt.QtXml import QDomDocument
from qgis.core import (
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsExpressionContextUtils, QgsFillSymbol, QgsLayoutExporter,
    QgsLayoutFrame, QgsLayoutItemAttributeTable, QgsLayoutItemLabel,
    QgsLayoutItemLegend, QgsLayoutItemManualTable, QgsLayoutItemMap,
    QgsLayoutItemMapGrid, QgsLayoutItemPage, QgsLayoutItemPicture,
    QgsLayoutItemPolyline, QgsLayoutItemScaleBar, QgsLayoutItemShape,
    QgsLayoutMeasurement, QgsLayoutPoint, QgsLayoutSize, QgsLayoutUtils,
    QgsLegendRenderer, QgsLegendStyle, QgsLineSymbol,
    QgsMapLayerLegendUtils, QgsPrintLayout, QgsRasterLayer,
    QgsProject, QgsProperty, QgsReadWriteContext, QgsRectangle,
    QgsScaleBarSettings,
    QgsTableCell, QgsTextFormat, QgsUnitTypes,
)

# "[Map Title]" style placeholders — the convention of the Map Template
# Builder plugin. "[% expression %]" is QGIS syntax and never a placeholder.
PLACEHOLDER_RE = re.compile(r"\[(?!%)([^\[\]\r\n%]{1,60})\]")
DEFAULT_NORTH_ARROW = ":/images/north_arrows/layout_default_north_arrow.svg"
_MM = QgsUnitTypes.LayoutMillimeters
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAX_PREVIEW_EDGE_PX = 1400
_LINT_TOLERANCE_MM = 0.6

PRESETS = ("side_panel", "title_strip", "report_figure")


class LayoutToolError(Exception):
    """A user-facing failure; its message goes back to the agent verbatim."""


class LayoutTools:
    def __init__(self, iface=None):
        self.iface = iface

    # ======================================================================
    # Entry points (one per MCP tool)
    # ======================================================================
    def info(self, args):
        args = _args(args)
        action = str(args.get("action") or "list")
        if action == "list":
            return self._list()
        if action == "describe":
            if args.get("template"):
                return self._describe_template(args["template"])
            return describe_layout(self._layout(args.get("layout")))
        if action == "render":
            return self._render(args)
        raise LayoutToolError(
            f"unknown layout_info action {action!r}; use list, describe "
            "or render")

    def manage(self, args):
        args = _args(args)
        action = str(args.get("action") or "")
        handler = {
            "build": self._build,
            "save_template": self._save_template,
            "create_from_template": self._create_from_template,
            "export": self._export,
            "open": self._open,
            "delete": self._delete,
        }.get(action)
        if handler is None:
            raise LayoutToolError(
                f"unknown manage_layouts action {action!r}; use build, "
                "save_template, create_from_template, export, open or delete")
        return handler(args)

    # ======================================================================
    # layout_info
    # ======================================================================
    def _list(self):
        manager = QgsProject.instance().layoutManager()
        layouts = []
        for layout in manager.printLayouts():
            layouts.append({
                "name": layout.name(),
                "pages": [_page_summary(page) for page in
                          layout.pageCollection().pages()],
                "items": sum(1 for item in layout.items()
                             if _is_content_item(item)),
                "atlas": bool(layout.atlas().enabled()),
            })
        reports = [layout.name() for layout in manager.layouts()
                   if not isinstance(layout, QgsPrintLayout)]
        templates = []
        for source, folder in template_dirs():
            if not os.path.isdir(folder):
                continue
            for entry in sorted(os.listdir(folder)):
                if entry.lower().endswith(".qpt"):
                    path = os.path.join(folder, entry)
                    templates.append({
                        "name": os.path.splitext(entry)[0],
                        "source": source,
                        "path": os.path.normpath(path),
                        "size_bytes": os.path.getsize(path),
                    })
        return {
            "layouts": layouts,
            "reports": reports,
            "templates": templates,
            "user_template_dir": os.path.normpath(user_template_dir()),
            "presets": list(PRESETS),
        }

    def _describe_template(self, reference):
        path = find_template(reference)
        layout = _load_template_layout(path)
        summary = describe_layout(layout)
        summary["template_path"] = os.path.normpath(path)
        summary.pop("layout", None)
        return summary

    def _render(self, args):
        layout = self._layout(args.get("layout"))
        page_count = layout.pageCollection().pageCount()
        page = int(args.get("page", 1)) - 1
        if not 0 <= page < page_count:
            raise LayoutToolError(
                f"page must be 1..{page_count} for layout {layout.name()!r}")
        size = layout.pageCollection().page(page).pageSize()
        long_edge_in = max(size.width(), size.height()) / 25.4
        dpi = args.get("dpi")
        if dpi is None:
            dpi = _MAX_PREVIEW_EDGE_PX / max(long_edge_in, 1.0)
        dpi = max(20.0, min(float(dpi), 300.0))
        layout.refresh()
        image = QgsLayoutExporter(layout).renderPageToImage(page, QSize(), dpi)
        if image.isNull():
            raise LayoutToolError("QGIS returned an empty preview image")
        path = os.path.join(
            tempfile.gettempdir(),
            f"qgent_layout_{_slug(layout.name())}_p{page + 1}.png")
        if not image.save(path, "PNG"):
            raise LayoutToolError(f"could not write preview PNG {path}")
        return {
            "snapshot_path": path,
            "layout": layout.name(),
            "page": page + 1,
            "dpi": round(dpi, 1),
            "pixels": [image.width(), image.height()],
            "note": ("PNG preview of the print layout page — read it to "
                     "check the design."),
        }

    # ======================================================================
    # manage_layouts
    # ======================================================================
    def _build(self, args):
        spec = args.get("spec")
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except json.JSONDecodeError as exc:
                raise LayoutToolError(f"spec is not valid JSON: {exc}")
        if not isinstance(spec, dict):
            raise LayoutToolError("build needs a 'spec' object")
        name = str(spec.get("name") or "").strip()
        if not name:
            raise LayoutToolError("spec.name is required")
        mode = str(spec.get("mode") or "create")
        project = QgsProject.instance()
        manager = project.layoutManager()
        existing = manager.layoutByName(name)
        replace = bool(spec.get("replace") or args.get("replace"))

        if mode == "update":
            if existing is None:
                raise LayoutToolError(
                    f"no layout named {name!r} to update; build it with "
                    "mode 'create' first")
            layout = existing
            if spec.get("page"):
                _set_page(layout, spec["page"])
            item_specs = list(spec.get("items") or [])
        elif mode == "create":
            if existing is not None and not replace:
                raise LayoutToolError(
                    f"a layout named {name!r} already exists. Pick another "
                    "name, use mode 'update' to edit it, or pass "
                    "replace=true (the user is asked to approve).")
            if existing is not None:
                manager.removeLayout(existing)
            layout = QgsPrintLayout(project)
            layout.initializeDefaults()
            layout.setName(name)
            _set_page(layout, spec.get("page") or {
                "size": "A4", "orientation": "landscape"})
            if not manager.addLayout(layout):
                raise LayoutToolError(f"QGIS refused to add layout {name!r}")
            item_specs = _merge_items(
                preset_items(spec.get("preset"), layout, spec.get("style")),
                spec.get("items") or [])
        else:
            raise LayoutToolError("spec.mode must be 'create' or 'update'")

        errors = []
        for item_spec in item_specs:
            try:
                apply_item(layout, item_spec, self.iface)
            except LayoutToolError as exc:
                errors.append(f"{item_spec.get('id') or '(no id)'}: {exc}")
            except Exception as exc:  # noqa: BLE001 - report, keep building
                errors.append(f"{item_spec.get('id') or '(no id)'}: "
                              f"{type(exc).__name__}: {exc}")
        _apply_variables(layout, spec.get("variables"))
        _apply_images(layout, spec.get("images"))
        filled = fill_placeholders(layout, spec.get("fill"))
        layout.refresh()
        result = describe_layout(layout)
        result["mode"] = mode
        if filled:
            result["filled"] = filled
        if errors:
            result["item_errors"] = errors
        return result

    def _save_template(self, args):
        layout = self._layout(args.get("layout"))
        folder = str(args.get("folder") or user_template_dir())
        name = str(args.get("name") or layout.name()).strip()
        if not name:
            raise LayoutToolError("template name is empty")
        path = os.path.join(folder, _safe_filename(name) + ".qpt")
        if os.path.exists(path) and not args.get("overwrite"):
            raise LayoutToolError(
                f"template already exists: {os.path.normpath(path)}. Use "
                "another name, or pass overwrite=true (the user is asked to "
                "approve).")
        os.makedirs(folder, exist_ok=True)
        portable = args.get("portable", True)
        target, unpinned = ((_portable_copy(layout)) if portable
                            else (layout, []))
        if not target.saveAsTemplate(path, QgsReadWriteContext()):
            raise LayoutToolError(f"QGIS could not write {path}")
        on_user_list = (os.path.normcase(os.path.abspath(folder))
                        == os.path.normcase(os.path.abspath(
                            user_template_dir())))
        result = {
            "template_path": os.path.normpath(path),
            "size_bytes": os.path.getsize(path),
            "layout": layout.name(),
            "pages": [_page_summary(page) for page in
                      layout.pageCollection().pages()],
            "placeholders": layout_placeholders(layout),
            "listed_in_layout_manager": on_user_list,
            "note": ("Appears under Project > Layout Manager > New from "
                     "template > User templates (reopen the Layout Manager "
                     "if it is already open)." if on_user_list else
                     "Saved outside the user template folder: QGIS lists it "
                     "only if this folder is in Settings > Options > Layouts "
                     "> Layout Paths."),
        }
        if unpinned:
            result["made_portable"] = unpinned
        warnings = _portability_warnings(target)
        if warnings:
            result["portability_warnings"] = warnings
        return result

    def _create_from_template(self, args):
        project = QgsProject.instance()
        manager = project.layoutManager()
        source_layout = args.get("source_layout")
        if source_layout:
            source = self._layout(source_layout)
            default_name = source.name() + " copy"
        else:
            template = args.get("template")
            if not template:
                raise LayoutToolError(
                    "create_from_template needs 'template' (name or .qpt "
                    "path) or 'source_layout' (a project layout to copy)")
            path = find_template(template)
            default_name = os.path.splitext(os.path.basename(path))[0]
        name = str(args.get("layout_name") or default_name).strip()
        existing = manager.layoutByName(name)
        if existing is not None:
            if not args.get("replace"):
                raise LayoutToolError(
                    f"a layout named {name!r} already exists. Pass another "
                    "layout_name, or replace=true (the user is asked to "
                    "approve).")
            manager.removeLayout(existing)

        if source_layout:
            layout = manager.duplicateLayout(source, name)
            if layout is None:
                raise LayoutToolError(f"QGIS could not copy {source.name()!r}")
        else:
            layout = _load_template_layout(path)
            layout.setName(name)
            if not manager.addLayout(layout):
                raise LayoutToolError(f"QGIS refused to add layout {name!r}")

        errors = []
        map_spec = args.get("map")
        if map_spec:
            try:
                target = _target_map(layout, map_spec.get("id"))
                _configure_map(target, dict(map_spec), layout, self.iface)
            except LayoutToolError as exc:
                errors.append(f"map: {exc}")
        _apply_variables(layout, args.get("variables"))
        _apply_images(layout, args.get("images"))
        filled = fill_placeholders(layout, args.get("fill"))
        layout.refresh()
        result = describe_layout(layout)
        result["created_from"] = (source.name() if source_layout
                                  else os.path.normpath(path))
        result["filled"] = filled
        if errors:
            result["item_errors"] = errors
        return result

    def _export(self, args):
        layout = self._layout(args.get("layout"))
        path = str(args.get("path") or "").strip()
        if not path:
            raise LayoutToolError("export needs an absolute output 'path'")
        if not os.path.isabs(path):
            raise LayoutToolError(f"export path must be absolute: {path}")
        if os.path.exists(path) and not args.get("overwrite"):
            raise LayoutToolError(
                f"{path} already exists. Pick another path, or pass "
                "overwrite=true (the user is asked to approve).")
        folder = os.path.dirname(path)
        if folder and not os.path.isdir(folder):
            raise LayoutToolError(f"output folder does not exist: {folder}")
        dpi = float(args.get("dpi") or 300)
        exporter = QgsLayoutExporter(layout)
        extension = os.path.splitext(path)[1].lower()
        if extension == ".pdf":
            settings = QgsLayoutExporter.PdfExportSettings()
            settings.dpi = dpi
            code = exporter.exportToPdf(path, settings)
        elif extension in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
            settings = QgsLayoutExporter.ImageExportSettings()
            settings.dpi = dpi
            code = exporter.exportToImage(path, settings)
        elif extension == ".svg":
            settings = QgsLayoutExporter.SvgExportSettings()
            settings.dpi = dpi
            code = exporter.exportToSvg(path, settings)
        else:
            raise LayoutToolError(
                "export path must end in .pdf, .png, .jpg, .tif, .bmp or .svg")
        if code != QgsLayoutExporter.Success:
            raise LayoutToolError(
                f"export failed with QgsLayoutExporter code {code}: "
                f"{exporter.errorMessage() if hasattr(exporter, 'errorMessage') else ''}")
        written = [path] if os.path.exists(path) else []
        if not written and layout.pageCollection().pageCount() > 1:
            stem, ext = os.path.splitext(path)
            written = [f"{stem}_{n}{ext}" for n in range(
                1, layout.pageCollection().pageCount() + 1)
                if os.path.exists(f"{stem}_{n}{ext}")]
        return {
            "layout": layout.name(),
            "files": [{"path": os.path.normpath(item),
                       "size_bytes": os.path.getsize(item)}
                      for item in written],
            "dpi": dpi,
        }

    def _open(self, args):
        layout = self._layout(args.get("layout"))
        if self.iface is None:
            raise LayoutToolError("no QGIS window to open the designer in")
        self.iface.openLayoutDesigner(layout)
        return {"opened": layout.name()}

    def _delete(self, args):
        layout = self._layout(args.get("layout"))
        name = layout.name()
        if not QgsProject.instance().layoutManager().removeLayout(layout):
            raise LayoutToolError(f"QGIS could not delete {name!r}")
        return {"deleted": name}

    # -- lookup -------------------------------------------------------------
    def _layout(self, name):
        manager = QgsProject.instance().layoutManager()
        name = str(name or "").strip()
        if not name:
            raise LayoutToolError("'layout' (the layout name) is required")
        layout = manager.layoutByName(name)
        if layout is None or not isinstance(layout, QgsPrintLayout):
            names = [item.name() for item in manager.printLayouts()]
            raise LayoutToolError(
                f"no print layout named {name!r}. Layouts: {names or 'none'}")
        return layout


# ==========================================================================
# Templates on disk
# ==========================================================================
def user_template_dir():
    """The folder Layout Manager lists as "User templates"."""
    return os.path.join(QgsApplication.qgisSettingsDirPath(),
                        "composer_templates")


def template_dirs():
    """Every folder QGIS (and QGent) reads layout templates from, in order."""
    dirs = [("user", user_template_dir())]
    dirs += [("search_path", path)
             for path in QgsApplication.layoutTemplatePaths()]
    dirs.append(("qgis_default", os.path.join(
        QgsApplication.pkgDataPath(), "composer_templates")))
    dirs.append(("qgent", os.path.join(_PLUGIN_DIR, "claude_runtime",
                                       "assets")))
    seen = set()
    unique = []
    for source, path in dirs:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            unique.append((source, path))
    return unique


def find_template(reference):
    """Resolve a .qpt path or a template name (case-insensitive stem)."""
    reference = str(reference or "").strip()
    if not reference:
        raise LayoutToolError("template reference is empty")
    if os.path.isfile(reference):
        return reference
    stem = os.path.splitext(os.path.basename(reference))[0].casefold()
    for _source, folder in template_dirs():
        if not os.path.isdir(folder):
            continue
        for entry in os.listdir(folder):
            if (entry.lower().endswith(".qpt")
                    and os.path.splitext(entry)[0].casefold() == stem):
                return os.path.join(folder, entry)
    raise LayoutToolError(
        f"template {reference!r} not found (call layout_info list to see "
        "the available templates)")


def _portable_copy(layout):
    """A clone whose maps and legends follow whatever project loads it.

    Locked map layers, map themes and fixed legend lists are stored as this
    project's layer IDs and render empty anywhere else. The live layout is
    left exactly as it is; only the saved template is unpinned.
    """
    clone = layout.clone()
    unpinned = []
    for item in clone.items():
        if isinstance(item, QgsLayoutItemMap):
            if item.followVisibilityPreset():
                item.setFollowVisibilityPreset(False)
                unpinned.append(f"map '{item.id()}' no longer follows a map "
                                "theme")
            if item.keepLayerSet():
                item.setKeepLayerSet(False)
                item.setLayers([])
                unpinned.append(f"map '{item.id()}' now shows the project's "
                                "visible layers")
        elif (isinstance(item, QgsLayoutItemLegend)
              and not item.autoUpdateModel()):
            item.setAutoUpdateModel(True)
            unpinned.append(f"legend '{item.id()}' now lists the project's "
                            "layers")
    return clone, unpinned


def _portability_warnings(layout):
    """Items that pin this project's layers and so go blank elsewhere."""
    warnings = []
    for item in layout.items():
        if isinstance(item, QgsLayoutItemMap) and item.keepLayerSet():
            warnings.append(
                f"map '{item.id()}' locks this project's layers; in another "
                "project it renders empty until its layers are set "
                "(manage_layouts map.layers or create_from_template map)")
        elif (isinstance(item, QgsLayoutItemLegend)
              and not item.autoUpdateModel()):
            warnings.append(
                f"legend '{item.id()}' keeps a fixed layer list from this "
                "project; in another project it shows nothing until "
                "auto_update is switched back on")
    return warnings


def _load_template_layout(path):
    document = QDomDocument()
    handle = QFile(path)
    if not handle.open(QIODevice.ReadOnly):
        raise LayoutToolError(f"cannot open template {path}")
    try:
        parsed = document.setContent(handle)
    finally:
        handle.close()
    if not (parsed[0] if isinstance(parsed, tuple) else parsed):
        raise LayoutToolError(f"template is not valid XML: {path}")
    layout = QgsPrintLayout(QgsProject.instance())
    layout.initializeDefaults()
    loaded = layout.loadFromTemplate(document, QgsReadWriteContext(), True)
    ok = loaded[1] if isinstance(loaded, tuple) else bool(loaded)
    if not ok:
        raise LayoutToolError(f"QGIS could not load template {path}")
    return layout


# ==========================================================================
# Describe + lint
# ==========================================================================
def describe_layout(layout):
    """Compact, JSON-able account of a layout plus design issues."""
    pages = layout.pageCollection()
    items = []
    for item in sorted((item for item in layout.items()
                        if _is_content_item(item)),
                       key=lambda item: item.zValue()):
        items.append(_describe_item(layout, item))
    variables = {}
    scope = QgsExpressionContextUtils.layoutScope(layout)
    for name in scope.variableNames():
        if name.startswith("layout_") or name in ("project_title",):
            continue
        if scope.isReadOnly(name):
            continue
        variables[name] = _jsonable(scope.variable(name))
    return {
        "layout": layout.name(),
        "pages": [_page_summary(page) for page in pages.pages()],
        "items": items,
        "placeholders": layout_placeholders(layout),
        "variables": variables,
        "issues": lint_layout(layout),
    }


def _describe_item(layout, item):
    page = layout.pageCollection().pageNumberForPoint(item.pos())
    position = layout.pageCollection().positionOnPage(item.pos())
    size = item.sizeWithUnits()
    entry = {
        "id": item.id() or "",
        "type": _item_type(item),
        "page": page + 1,
        "rect_mm": [round(position.x(), 1), round(position.y(), 1),
                    round(layout.convertToLayoutUnits(size).width(), 1),
                    round(layout.convertToLayoutUnits(size).height(), 1)],
    }
    if not item.isVisible():
        entry["visible"] = False
    if isinstance(item, QgsLayoutItemLabel):
        entry["text"] = _clip(item.text(), 160)
    elif isinstance(item, QgsLayoutItemMap):
        entry["scale"] = round(item.scale())
        entry["crs"] = item.crs().authid()
        entry["layers"] = ([layer.name() for layer in item.layers()]
                           if item.keepLayerSet() else "follows visible layers")
        grids = item.grids().asList()
        if grids:
            entry["grids"] = len(grids)
        overview = item.overview()
        if overview is not None and overview.linkedMap() is not None:
            entry["overview_of"] = overview.linkedMap().id()
    elif isinstance(item, QgsLayoutItemLegend):
        entry["title"] = item.title()
        linked = item.linkedMap()
        entry["map"] = linked.id() if linked else None
        entry["auto_update"] = item.autoUpdateModel()
    elif isinstance(item, QgsLayoutItemScaleBar):
        entry["style"] = item.style()
        linked = item.linkedMap()
        entry["map"] = linked.id() if linked else None
    elif isinstance(item, QgsLayoutItemPicture):
        entry["path"] = item.picturePath()
        linked = item.linkedMap()
        if linked is not None:
            entry["map"] = linked.id()
    elif isinstance(item, QgsLayoutFrame) and item.multiFrame() is not None:
        frame_owner = item.multiFrame()
        if isinstance(frame_owner, QgsLayoutItemManualTable):
            entry["rows"] = [[_clip(_cell_text(cell), 40) for cell in row]
                             for row in frame_owner.tableContents()]
        elif isinstance(frame_owner, QgsLayoutItemAttributeTable):
            layer = frame_owner.vectorLayer()
            entry["layer"] = layer.name() if layer else None
    return entry


def lint_layout(layout):
    """Design problems an agent should fix before calling a layout done."""
    issues = []
    pages = layout.pageCollection()
    # Item frames, not scene bounds: a page's bounds include its drop shadow
    # and a map's include a generous allowance for grid annotations.
    page_rects = [page.mapRectToScene(page.rect()) for page in pages.pages()]
    boxes = []
    annotation_bands = []
    tolerance = _LINT_TOLERANCE_MM
    map_rects = [item.mapRectToScene(item.rect()) for item in layout.items()
                 if isinstance(item, QgsLayoutItemMap) and item.isVisible()]
    for item in layout.items():
        if not _is_content_item(item) or not item.isVisible():
            continue
        label = item.id() or _item_type(item)
        rect = item.mapRectToScene(item.rect())
        inside = any(_within(rect, page, tolerance) for page in page_rects)
        if not inside:
            issues.append(f"'{label}' extends beyond the page edge")
        if isinstance(item, QgsLayoutItemMap):
            for side, band in _annotation_bands(item, rect):
                annotation_bands.append((label, side, band))
                if not any(_within(band, page, tolerance)
                           for page in page_rects):
                    issues.append(
                        f"map '{label}' {side} grid labels need ~3.5 mm of "
                        "paper beyond the frame — they will be cut off")
        if (isinstance(item, QgsLayoutItemLabel)
                and item.mode() == QgsLayoutItemLabel.ModeFont):
            needed = _label_required_size(layout, item)
            have = layout.convertToLayoutUnits(item.sizeWithUnits())
            if needed is not None and (
                    needed[0] > have.width() + tolerance
                    or needed[1] > have.height() + tolerance):
                issues.append(
                    f"label '{label}' needs {needed[0]:.1f} x "
                    f"{needed[1]:.1f} mm but its box is "
                    f"{have.width():.1f} x {have.height():.1f} mm — text is "
                    "clipped; enlarge the box, shrink the font, or shorten "
                    "the text")
            elif needed is not None and _boxed(item, rect, map_rects):
                issue = _mostly_empty(label, "label", needed[1], have.height())
                if issue:
                    issues.append(issue)
        elif isinstance(item, QgsLayoutItemLegend) and not item.resizeToContents():
            needed = _legend_size(layout, item)
            have = layout.convertToLayoutUnits(item.sizeWithUnits())
            if needed is not None and (
                    needed.height() > have.height() + tolerance
                    or needed.width() > have.width() + tolerance):
                issues.append(
                    f"legend '{label}' needs {needed.width():.1f} x "
                    f"{needed.height():.1f} mm but its box is "
                    f"{have.width():.1f} x {have.height():.1f} mm — entries "
                    "are cut off; enlarge it, add columns, shrink fonts, or "
                    "exclude layers")
            elif needed is not None and _boxed(item, rect, map_rects):
                issue = _mostly_empty(label, "legend", needed.height(),
                                      have.height())
                if issue:
                    issues.append(issue)
        elif isinstance(item, QgsLayoutItemMap):
            if not item.crs().isValid():
                issues.append(f"map '{label}' has no valid CRS")
            if item.extent().isEmpty():
                issues.append(f"map '{label}' has an empty extent")
        elif isinstance(item, QgsLayoutItemPicture):
            if not item.picturePath() and label not in ("logo",) \
                    and not label.endswith("logo"):
                issues.append(f"picture '{label}' has no image path")
        if _is_overlay_item(item):
            boxes.append((label, rect))
    live_owners = list(layout.multiFrames())
    for item in layout.items():
        if (isinstance(item, QgsLayoutFrame)
                and not any(item.multiFrame() is owner
                            for owner in live_owners)):
            issues.append(
                f"frame '{item.id() or 'frame'}' belongs to no table (left "
                "over from an earlier edit) — remove it with {id, remove: "
                "true}")
    for frame_owner in layout.multiFrames():
        frames = [frame for frame in frame_owner.frames() if frame.isVisible()]
        if not frames or not isinstance(frame_owner, (
                QgsLayoutItemManualTable, QgsLayoutItemAttributeTable)):
            continue
        # totalSize() equals the frames' height in the default
        # use-existing-frames mode, so measure the rows instead.
        needed = _table_content_height(layout, frame_owner)
        if needed is None:
            continue
        have = sum(layout.convertToLayoutUnits(frame.sizeWithUnits()).height()
                   for frame in frames)
        if needed > have + tolerance:
            name = frames[0].id() or _item_type(frames[0])
            issues.append(
                f"table '{name}' needs {needed:.1f} mm of height but its "
                f"frame is {have:.1f} mm — rows are cut off; enlarge the "
                "frame, reduce cell_margin, or shrink the font")
    for label, rect in boxes:
        for map_label, side, band in annotation_bands:
            overlap = band.intersected(rect)
            if overlap.width() > 0.3 and overlap.height() > 0.3:
                issues.append(
                    f"'{label}' covers the {side} grid labels of map "
                    f"'{map_label}' — move it ~4 mm clear of the frame, or "
                    f"drop '{side}' from grid.annotation_sides")
                break
    for index, (label, rect) in enumerate(boxes):
        for other_label, other in boxes[index + 1:]:
            overlap = rect.intersected(other)
            if (overlap.width() > 1.0 and overlap.height() > 1.0
                    and not rect.contains(other) and not other.contains(rect)):
                issues.append(f"'{label}' and '{other_label}' overlap")
    issues.extend(_missing_essentials(layout, page_rects))
    placeholders = layout_placeholders(layout)
    if placeholders:
        issues.append("unfilled placeholders: " + ", ".join(placeholders))
    return issues


def _missing_essentials(layout, page_rects):
    """A map sheet without legend, scale or north arrow is not finished.

    Only for a *main* map (at least a quarter of its page): small figures
    and decorative insets are left alone. Agents are told to fix every
    issue, so these name the exception that justifies leaving one out.
    """
    visible = [item for item in layout.items()
               if _is_content_item(item) and item.isVisible()]
    page_area = max((page.width() * page.height() for page in page_rects),
                    default=0.0)
    main_maps = [item for item in visible
                 if isinstance(item, QgsLayoutItemMap) and page_area
                 and item.rect().width() * item.rect().height()
                 >= 0.25 * page_area]
    if not main_maps:
        return []
    issues = []
    if not any(isinstance(item, QgsLayoutItemLegend) for item in visible):
        issues.append(
            "no legend — add one unless the user asked for none or the map "
            "shows a single self-explanatory layer")
    has_scale = any(isinstance(item, QgsLayoutItemScaleBar)
                    for item in visible) or any(
        isinstance(item, QgsLayoutItemLabel) and "map_scale" in item.text()
        for item in visible)
    if not has_scale:
        issues.append("no scale bar or scale text — add one unless the user "
                      "asked for none")
    if not any(isinstance(item, QgsLayoutItemPicture)
               and _item_type(item) == "north_arrow" for item in visible):
        issues.append("no north arrow — add one unless the user asked for "
                      "none or the map has a graticule and is north-up")
    return issues


def _boxed(item, rect, map_rects):
    """Whether unused space inside an item is visible on the page.

    A frame always shows it. A fill shows it unless it is white on paper:
    legends get a white background by default, invisible on a white panel
    but a blank plate once it sits over a map.
    """
    if item.frameEnabled():
        return True
    if not item.hasBackground() or item.backgroundColor().alpha() == 0:
        return False
    if item.backgroundColor().lightness() < 250:
        return True
    return any(rect.intersects(map_rect) for map_rect in map_rects)


def _mostly_empty(label, kind, needed, have):
    """An outlined box at least twice as tall as what it holds looks unfinished.

    Only framed or filled items are judged: an open text block or legend can
    leave room for later content without anyone seeing it.
    """
    if have - needed < 12.0 or needed >= 0.5 * have:
        return ""
    hint = ("set resize_to_contents" if kind == "legend"
            else "shrink the box or drop its frame/background")
    return (f"{kind} '{label}' is a framed {have:.0f} mm tall box holding "
            f"{needed:.0f} mm of content — {hint}")


def _within(rect, page, tolerance):
    """Edge test that, unlike QRectF.contains, accepts zero-height lines."""
    return (rect.left() >= page.left() - tolerance
            and rect.right() <= page.right() + tolerance
            and rect.top() >= page.top() - tolerance
            and rect.bottom() <= page.bottom() + tolerance)


def _label_required_size(layout, label):
    """(width, height) in mm the label needs once QGIS wraps it to its box.

    Mirrors ``QgsLayoutItemLabel::sizeForText`` but wraps first, the way the
    label paints — ``sizeForText`` alone measures one unwrapped line and so
    flags every multi-word label that wraps fine.
    """
    try:
        from qgis.core import Qgis, QgsTextRenderer
        context = QgsLayoutUtils.createRenderContextForLayout(layout, None)
        flag = getattr(getattr(Qgis, "RenderContextFlag", None),
                       "ApplyScalingWorkaroundForTextRendering", None)
        if flag is not None:
            context.setFlag(flag)
        per_mm = context.convertToPainterUnits(1, QgsUnitTypes.RenderMillimeters)
        pen = (label.pen().widthF() / 2.0) if label.frameEnabled() else 0.0
        box = layout.convertToLayoutUnits(label.sizeWithUnits())
        available = max(1.0, box.width() - 2 * label.marginX() - 2 * pen)
        text_format = label.textFormat()
        lines = []
        for line in label.currentText().split("\n"):
            if hasattr(QgsTextRenderer, "wrappedText") and line.strip():
                lines.extend(QgsTextRenderer.wrappedText(
                    context, line, available * per_mm, text_format) or [line])
            else:
                lines.append(line)
        widest = max((QgsTextRenderer.textWidth(context, text_format, [line])
                      for line in lines), default=0.0) / per_mm
        height = QgsTextRenderer.textHeight(
            context, text_format, lines) / per_mm
        return (math.ceil(widest) + 2 * label.marginX() + 2 * pen,
                math.ceil(height) + 2 * label.marginY() + 2 * pen)
    except Exception:  # noqa: BLE001 - lint is best-effort
        return None


def _table_content_height(layout, table):
    """Height in mm a table's rows need, measured like QgsLayoutTable does.

    Each row is its tallest cell's text plus the cell margin above and below,
    and a shown grid adds one stroke per row boundary.
    """
    try:
        from qgis.core import Qgis, QgsTextRenderer
        context = QgsLayoutUtils.createRenderContextForLayout(layout, None)
        per_mm = context.convertToPainterUnits(
            1, QgsUnitTypes.RenderMillimeters)
        mode = getattr(getattr(Qgis, "TextLayoutMode", None),
                       "RectangleAscentBased", None)

        def text_height(text_format, text):
            lines = str(text).split("\n") or [""]
            args = (context, text_format, lines)
            if mode is not None:
                args += (mode,)
            return QgsTextRenderer.textHeight(*args) / per_mm

        margin = table.cellMargin()
        stroke = table.gridStrokeWidth() if table.showGrid() else 0.0
        if isinstance(table, QgsLayoutItemManualTable):
            rows = [[_cell_sample(cell) for cell in row]
                    for row in table.tableContents()]
        else:
            rows = [[str(value) for value in row] for row in table.contents()]
        total = 0.0
        for row_index, row in enumerate(rows):
            tallest = 0.0
            for column, text in enumerate(row):
                text_format = (table.textFormatForCell(row_index, column)
                               if hasattr(table, "textFormatForCell")
                               else table.contentTextFormat())
                tallest = max(tallest, text_height(text_format, text or "Ag"))
            total += tallest + 2 * margin
        if (not isinstance(table, QgsLayoutItemManualTable)
                or table.includeTableHeader()):
            header_format = table.headerTextFormat()
            total += text_height(header_format, "Ag") + 2 * margin
            rows = rows + [[]]
        return total + (len(rows) + 1) * stroke
    except Exception:  # noqa: BLE001 - layout still builds without it
        return None


def _cell_sample(cell):
    content = cell.content()
    return content if isinstance(content, str) else "Ag"


def _legend_size(layout, legend):
    try:
        renderer = QgsLegendRenderer(legend.model(), legend.legendSettings())
        context = QgsLayoutUtils.createRenderContextForLayout(layout, None)
        size = renderer.minimumSize(context)
        return size if size.isValid() else None
    except Exception:  # noqa: BLE001 - lint is best-effort
        return None


# ==========================================================================
# Placeholders, variables, images
# ==========================================================================
def layout_placeholders(layout):
    found = []
    for text in _texts(layout):
        for match in PLACEHOLDER_RE.finditer(text):
            token = match.group(0)
            if token not in found:
                found.append(token)
    return found


def fill_placeholders(layout, values):
    """Replace ``[Key]`` tokens in labels and manual-table cells.

    Keys match case-, space- and underscore-insensitively, with or without
    brackets: ``project_name`` fills ``[Project Name]``. Returns the tokens
    that were filled.
    """
    if not values:
        return []
    if not isinstance(values, dict):
        raise LayoutToolError("'fill' must be an object of placeholder: text")
    lookup = {_norm_key(key): str(value) for key, value in values.items()}
    filled = []

    def substitute(text, html=False):
        def replace(match):
            key = _norm_key(match.group(1))
            if key not in lookup:
                return match.group(0)
            if match.group(0) not in filled:
                filled.append(match.group(0))
            value = lookup[key]
            if html:
                value = (value.replace("&", "&amp;").replace("<", "&lt;")
                         .replace(">", "&gt;").replace("\n", "<br>"))
            return value
        return PLACEHOLDER_RE.sub(replace, text)

    for item in layout.items():
        if isinstance(item, QgsLayoutItemLabel):
            html = item.mode() == QgsLayoutItemLabel.ModeHtml
            text = item.text()
            new_text = substitute(text, html)
            if new_text != text:
                item.setText(new_text)
    for frame_owner in layout.multiFrames():
        if isinstance(frame_owner, QgsLayoutItemManualTable):
            contents = frame_owner.tableContents()
            changed = False
            for row in contents:
                for cell in row:
                    if not isinstance(cell.content(), str):
                        continue
                    text = cell.content()
                    new_text = substitute(text)
                    if new_text != text:
                        cell.setContent(new_text)
                        changed = True
            if changed:
                frame_owner.setTableContents(contents)
    return filled


def _texts(layout):
    for item in layout.items():
        if isinstance(item, QgsLayoutItemLabel):
            yield item.text()
    for frame_owner in layout.multiFrames():
        if isinstance(frame_owner, QgsLayoutItemManualTable):
            for row in frame_owner.tableContents():
                for cell in row:
                    if isinstance(cell.content(), str):
                        yield cell.content()


def _apply_variables(layout, variables):
    if not variables:
        return
    if not isinstance(variables, dict):
        raise LayoutToolError("'variables' must be an object")
    for name, value in variables.items():
        QgsExpressionContextUtils.setLayoutVariable(layout, str(name), value)


def _apply_images(layout, images):
    """``{"logo": "C:/.../logo.png"}`` → set picture items by ID."""
    if not images:
        return
    if not isinstance(images, dict):
        raise LayoutToolError("'images' must be an object of item_id: path")
    for item_id, path in images.items():
        item = layout.itemById(str(item_id))
        if not isinstance(item, QgsLayoutItemPicture):
            raise LayoutToolError(f"no picture item with id {item_id!r}")
        if not os.path.isfile(str(path)):
            raise LayoutToolError(f"image not found: {path}")
        item.setPicturePath(str(path))


# ==========================================================================
# Building items from specs
# ==========================================================================
_TYPE_CLASSES = {
    "map": QgsLayoutItemMap,
    "label": QgsLayoutItemLabel,
    "legend": QgsLayoutItemLegend,
    "scalebar": QgsLayoutItemScaleBar,
    "north_arrow": QgsLayoutItemPicture,
    "picture": QgsLayoutItemPicture,
    "rectangle": QgsLayoutItemShape,
    "ellipse": QgsLayoutItemShape,
    "line": QgsLayoutItemPolyline,
}
_TABLE_TYPES = ("table", "attribute_table")


def apply_item(layout, spec, iface=None):
    """Create or update one item from its spec, matched by ``id``."""
    if not isinstance(spec, dict):
        raise LayoutToolError("each item spec must be an object")
    item_id = str(spec.get("id") or "").strip()
    existing = _find_item(layout, item_id) if item_id else None
    if spec.get("remove"):
        if existing is None:
            raise LayoutToolError("nothing to remove")
        _remove_item(layout, existing)
        return None
    item_type = str(spec.get("type") or (
        _item_type(existing) if existing is not None else "")).strip()
    if item_type in _TABLE_TYPES:
        if existing is not None:
            if isinstance(existing, QgsLayoutFrame):
                # A table is rebuilt to change it; keep whatever the update
                # does not mention, so moving or widening it keeps its rows.
                spec = dict(_existing_table_spec(layout, existing), **spec)
            _remove_item(layout, existing)
        return _build_table(layout, spec, item_type)
    cls = _TYPE_CLASSES.get(item_type)
    if cls is None:
        raise LayoutToolError(
            f"unknown item type {item_type!r}; use one of "
            f"{sorted(list(_TYPE_CLASSES) + list(_TABLE_TYPES))}")
    if existing is not None and not isinstance(existing, cls):
        _remove_item(layout, existing)
        existing = None
    creating = existing is None
    if creating:
        if item_type == "line":
            item = QgsLayoutItemPolyline(QPolygonF(), layout)
        else:
            item = cls(layout)
        if item_type in ("label", "picture", "north_arrow", "rectangle",
                         "ellipse"):
            item.setFrameEnabled(False)
        layout.addLayoutItem(item)
        if item_id:
            item.setId(item_id)
    else:
        item = existing

    if item_type == "line":
        _configure_line(layout, item, spec)
    else:
        _place(layout, item, spec, creating)
    builder = {
        "map": lambda: _configure_map(item, spec, layout, iface),
        "label": lambda: _configure_label(item, spec, creating),
        "legend": lambda: _configure_legend(item, spec, layout, creating),
        "scalebar": lambda: _configure_scalebar(item, spec, layout, creating),
        "north_arrow": lambda: _configure_picture(item, spec, layout, True),
        "picture": lambda: _configure_picture(item, spec, layout, False),
        "rectangle": lambda: _configure_shape(item, spec, "rectangle"),
        "ellipse": lambda: _configure_shape(item, spec, "ellipse"),
        "line": lambda: None,
    }[item_type]
    builder()
    _apply_common(item, spec)
    return item


def _place(layout, item, spec, creating):
    rect = spec.get("rect")
    page = int(spec.get("page", 1)) - 1
    if rect is None:
        if creating and not isinstance(item, (QgsLayoutItemLabel,
                                              QgsLayoutItemScaleBar,
                                              QgsLayoutItemLegend)):
            raise LayoutToolError("'rect' [x, y, width, height] in mm is "
                                  "required for a new item")
        return
    x, y, width, height = _rect(rect)
    if not 0 <= page < layout.pageCollection().pageCount():
        raise LayoutToolError(f"page {page + 1} does not exist")
    item.attemptResize(QgsLayoutSize(width, height, _MM))
    item.attemptMove(QgsLayoutPoint(x, y, _MM), True, False, page)


def _apply_common(item, spec):
    frame = spec.get("frame")
    if frame is not None:
        item.setFrameEnabled(bool(frame))
        if isinstance(frame, dict):
            if frame.get("color"):
                item.setFrameStrokeColor(_color(frame["color"]))
            if frame.get("width") is not None:
                item.setFrameStrokeWidth(
                    QgsLayoutMeasurement(float(frame["width"]), _MM))
    if "background" in spec:
        color = _color(spec.get("background"))
        item.setBackgroundEnabled(color is not None)
        if color is not None:
            item.setBackgroundColor(color)
    if "visible" in spec:
        item.setVisibility(bool(spec["visible"]))
    if "locked" in spec:
        item.setLocked(bool(spec["locked"]))
    if spec.get("rotation") is not None and not isinstance(
            item, QgsLayoutItemMap):
        item.setItemRotation(float(spec["rotation"]))
    if spec.get("opacity") is not None:
        item.setItemOpacity(float(spec["opacity"]))
    if spec.get("z") is not None:
        item.setZValue(float(spec["z"]))


# -- map ---------------------------------------------------------------------
def _configure_map(item, spec, layout, iface=None):
    project = QgsProject.instance()
    if spec.get("crs"):
        crs = QgsCoordinateReferenceSystem(str(spec["crs"]))
        if not crs.isValid():
            raise LayoutToolError(f"invalid CRS {spec['crs']!r}")
        item.setCrs(crs)
    elif not item.crs().isValid():
        item.setCrs(project.crs())
    if "layers" in spec:
        layers = spec.get("layers")
        if layers in (None, "visible", "all_visible"):
            item.setKeepLayerSet(False)
            item.setLayers([])
        else:
            resolved = [_project_layer(name) for name in layers]
            item.setLayers(resolved)
            item.setKeepLayerSet(True)
    if "lock_layers" in spec and spec["lock_layers"] and not item.keepLayerSet():
        visible = [layer for layer in
                   project.layerTreeRoot().checkedLayers()]
        item.setLayers(visible)
        item.setKeepLayerSet(True)
    extent = _resolve_extent(spec, item, iface)
    if extent is not None:
        item.zoomToExtent(extent)
        if not spec.get("scale") and spec.get("nice_scale", True):
            item.setScale(_nice_scale(item.scale()))
    elif item.extent().isEmpty() or item.extent().width() == 0:
        fallback = _full_extent(item)
        if fallback is not None:
            item.zoomToExtent(fallback)
    if spec.get("scale"):
        item.setScale(float(spec["scale"]))
    if spec.get("map_rotation") is not None:
        item.setMapRotation(float(spec["map_rotation"]))
    if "grid" in spec:
        _configure_grid(item, spec.get("grid"))
    if "overview" in spec:
        _configure_overview(item, spec.get("overview"), layout)
    if spec.get("frame") is None and spec.get("type") == "map" \
            and not item.frameEnabled():
        item.setFrameEnabled(True)
        item.setFrameStrokeWidth(QgsLayoutMeasurement(0.3, _MM))


def _resolve_extent(spec, item, iface):
    """Return a QgsRectangle in the map item's CRS, or None."""
    target_crs = item.crs()
    project = QgsProject.instance()
    center = spec.get("center")
    if center is not None:
        if not spec.get("scale"):
            raise LayoutToolError("'center' needs a 'scale' as well")
        x, y = float(center[0]), float(center[1])
        source = QgsCoordinateReferenceSystem(
            str(spec.get("center_crs") or "EPSG:4326"))
        point = QgsCoordinateTransform(source, target_crs, project) \
            .transform(x, y)
        return QgsRectangle(point.x() - 1, point.y() - 1,
                            point.x() + 1, point.y() + 1)
    extent = spec.get("extent")
    if extent is None:
        return None
    if extent == "canvas":
        if iface is None:
            raise LayoutToolError("no map canvas available for 'canvas'")
        canvas = iface.mapCanvas()
        return _transform_rect(canvas.extent(),
                               canvas.mapSettings().destinationCrs(),
                               target_crs)
    if isinstance(extent, str) and extent.startswith("layer:"):
        layers = [_project_layer(name.strip())
                  for name in extent[len("layer:"):].split("|")]
        combined = None
        for layer in layers:
            rect = _transform_rect(layer.extent(), layer.crs(), target_crs)
            if combined is None:
                combined = QgsRectangle(rect)
            else:
                combined.combineExtentWith(rect)
        return _pad(combined, float(spec.get("padding", 0.08)))
    if isinstance(extent, (list, tuple)) and len(extent) == 4:
        rect = QgsRectangle(*[float(value) for value in extent])
        source = spec.get("extent_crs")
        if source:
            rect = _transform_rect(
                rect, QgsCoordinateReferenceSystem(str(source)), target_crs)
        return rect
    raise LayoutToolError(
        "extent must be 'canvas', 'layer:<name>[|<name>…]' or "
        "[xmin, ymin, xmax, ymax] (optionally with extent_crs)")


def _full_extent(item):
    project = QgsProject.instance()
    combined = None
    for layer in project.layerTreeRoot().checkedLayers():
        try:
            rect = _transform_rect(layer.extent(), layer.crs(), item.crs())
        except Exception:  # noqa: BLE001 - skip untransformable layers
            continue
        if rect.isEmpty():
            continue
        if combined is None:
            combined = QgsRectangle(rect)
        else:
            combined.combineExtentWith(rect)
    return _pad(combined, 0.05) if combined is not None else None


def _configure_grid(item, grid_spec):
    for grid in list(item.grids().asList()):
        item.grids().removeGrid(grid.id())
    if not grid_spec:
        return
    if grid_spec is True:
        grid_spec = {}
    grid = QgsLayoutItemMapGrid("Grid", item)
    item.grids().addGrid(grid)
    grid.setEnabled(True)
    if grid_spec.get("crs"):
        grid.setCrs(QgsCoordinateReferenceSystem(str(grid_spec["crs"])))
    interval = grid_spec.get("interval")
    if interval is None and hasattr(QgsLayoutItemMapGrid,
                                    "DynamicPageSizeBased"):
        # Spacing measured on paper, so a template keeps a readable grid at
        # whatever scale it is later filled with.
        grid.setUnits(QgsLayoutItemMapGrid.DynamicPageSizeBased)
        grid.setMinimumIntervalWidth(float(grid_spec.get("min_spacing", 35)))
        grid.setMaximumIntervalWidth(float(grid_spec.get("max_spacing", 75)))
    else:
        if interval is None:
            extent = item.extent()
            interval = _nice_interval(
                max(extent.width(), extent.height()) / 5.0)
            if grid_spec.get("crs") and QgsCoordinateReferenceSystem(
                    str(grid_spec["crs"])).isGeographic():
                interval = _nice_degrees(interval, item)
        if isinstance(interval, (list, tuple)):
            grid.setIntervalX(float(interval[0]))
            grid.setIntervalY(float(interval[1]))
        else:
            grid.setIntervalX(float(interval))
            grid.setIntervalY(float(interval))
    style = {
        "solid": QgsLayoutItemMapGrid.Solid,
        "cross": QgsLayoutItemMapGrid.Cross,
        "markers": QgsLayoutItemMapGrid.Markers,
        "frame_only": QgsLayoutItemMapGrid.FrameAnnotationsOnly,
    }.get(str(grid_spec.get("style") or "cross"))
    if style is None:
        raise LayoutToolError("grid.style must be solid, cross, markers or "
                              "frame_only")
    grid.setStyle(style)
    if style == QgsLayoutItemMapGrid.Cross:
        grid.setCrossLength(float(grid_spec.get("cross_length", 2.0)))
    frame = {
        "zebra": QgsLayoutItemMapGrid.Zebra,
        "line": QgsLayoutItemMapGrid.LineBorder,
        "ticks": QgsLayoutItemMapGrid.InteriorTicks,
        "none": QgsLayoutItemMapGrid.NoFrame,
    }.get(str(grid_spec.get("frame") or "none"))
    if frame is None:
        raise LayoutToolError("grid.frame must be zebra, line, ticks or none")
    grid.setFrameStyle(frame)
    if frame != QgsLayoutItemMapGrid.NoFrame:
        grid.setFrameWidth(float(grid_spec.get("frame_width", 1.5)))
    color = _color(grid_spec.get("color") or "#6e6e6e")
    grid.setGridLineColor(color)
    grid.setGridLineWidth(float(grid_spec.get("line_width", 0.15)))
    if grid_spec.get("annotations", True):
        grid.setAnnotationEnabled(True)
        geographic = grid.crs().isGeographic() if grid.crs().isValid() \
            else item.crs().isGeographic()
        annotation_format = {
            "dms": QgsLayoutItemMapGrid.DegreeMinuteSecond,
            "dm": QgsLayoutItemMapGrid.DegreeMinute,
            "decimal": QgsLayoutItemMapGrid.Decimal,
            "decimal_suffix": QgsLayoutItemMapGrid.DecimalWithSuffix,
        }.get(str(grid_spec.get("format") or (
            "dms" if geographic else "decimal")))
        if annotation_format is None:
            raise LayoutToolError(
                "grid.format must be dms, dm, decimal or decimal_suffix")
        grid.setAnnotationFormat(annotation_format)
        grid.setAnnotationPrecision(int(grid_spec.get("precision", 0)))
        grid.setAnnotationTextFormat(_text_format(
            {"size": grid_spec.get("font_size", 6),
             "family": grid_spec.get("font")}, "#333333"))
        for side in (QgsLayoutItemMapGrid.Left, QgsLayoutItemMapGrid.Right):
            grid.setAnnotationDirection(QgsLayoutItemMapGrid.Vertical, side)
        sides = [str(side) for side in grid_spec.get(
            "annotation_sides", list(_GRID_SIDES))]
        unknown = set(sides) - set(_GRID_SIDES)
        if unknown:
            raise LayoutToolError(
                f"grid.annotation_sides takes {list(_GRID_SIDES)}, "
                f"not {sorted(unknown)}")
        for name, side in _GRID_SIDES.items():
            grid.setAnnotationPosition(
                QgsLayoutItemMapGrid.InsideMapFrame
                if grid_spec.get("annotations_inside")
                else QgsLayoutItemMapGrid.OutsideMapFrame, side)
            grid.setAnnotationDisplay(
                QgsLayoutItemMapGrid.ShowAll if name in sides
                else QgsLayoutItemMapGrid.HideAll, side)
        grid.setAnnotationFrameDistance(float(grid_spec.get("distance", 1.0)))
    else:
        grid.setAnnotationEnabled(False)


_GRID_SIDES = {
    "left": QgsLayoutItemMapGrid.Left,
    "right": QgsLayoutItemMapGrid.Right,
    "top": QgsLayoutItemMapGrid.Top,
    "bottom": QgsLayoutItemMapGrid.Bottom,
}


def _annotation_bands(item, rect):
    """Paper strips outside a map frame that its grid labels occupy."""
    depth = 3.5
    bands = []
    for grid in item.grids().asList():
        if not (grid.enabled() and grid.annotationEnabled()):
            continue
        for name, side in _GRID_SIDES.items():
            if (grid.annotationDisplay(side) == QgsLayoutItemMapGrid.HideAll
                    or grid.annotationPosition(side)
                    != QgsLayoutItemMapGrid.OutsideMapFrame):
                continue
            if name == "left":
                band = QRectF(rect.left() - depth, rect.top(), depth,
                              rect.height())
            elif name == "right":
                band = QRectF(rect.right(), rect.top(), depth, rect.height())
            elif name == "top":
                band = QRectF(rect.left(), rect.top() - depth, rect.width(),
                              depth)
            else:
                band = QRectF(rect.left(), rect.bottom(), rect.width(), depth)
            bands.append((name, band))
    return bands


def _configure_overview(item, overview_spec, layout):
    overview = item.overview()
    if not overview_spec:
        if overview is not None:
            overview.setLinkedMap(None)
        return
    if isinstance(overview_spec, str):
        overview_spec = {"of": overview_spec}
    target = _find_item(layout, str(overview_spec.get("of") or ""))
    if not isinstance(target, QgsLayoutItemMap):
        raise LayoutToolError("overview.of must name another map item")
    overview.setLinkedMap(target)
    fill = _color(overview_spec.get("fill") or "#e31a1c40")
    stroke = _color(overview_spec.get("stroke") or "#e31a1c")
    symbol = QgsFillSymbol.createSimple({
        "color": _symbol_color(fill),
        "outline_color": _symbol_color(stroke),
        "outline_width": str(overview_spec.get("stroke_width", 0.5)),
        "outline_width_unit": "MM",
    })
    overview.setFrameSymbol(symbol)


# -- label -------------------------------------------------------------------
def _configure_label(item, spec, creating):
    if "text" in spec:
        item.setText(str(spec["text"]))
    if "html" in spec:
        item.setMode(QgsLayoutItemLabel.ModeHtml if spec["html"]
                     else QgsLayoutItemLabel.ModeFont)
    if creating or "font" in spec or "color" in spec:
        item.setTextFormat(_text_format(
            spec.get("font"), spec.get("color"),
            base=None if creating else item.textFormat()))
    if "align" in spec or creating:
        item.setHAlign(_halign(spec.get("align", "left")))
    if "valign" in spec or creating:
        item.setVAlign(_valign(spec.get("valign", "top")))
    if "margin" in spec or creating:
        margin = float(spec.get("margin", 0.8))
        item.setMarginX(margin)
        item.setMarginY(margin)
    if creating and spec.get("rect") is None:
        item.adjustSizeToText()


# -- legend ------------------------------------------------------------------
def _configure_legend(item, spec, layout, creating):
    if "map" in spec or creating:
        target = _target_map(layout, spec.get("map"), required=False)
        if target is not None:
            item.setLinkedMap(target)
    if "title" in spec or creating:
        item.setTitle(str(spec.get("title", "Legend")))
    if "columns" in spec:
        item.setColumnCount(max(1, int(spec["columns"])))
    if "filter_by_map" in spec:
        item.setLegendFilterByMapEnabled(bool(spec["filter_by_map"]))
    exclude = spec.get("exclude") or []
    include = spec.get("layers")
    hide_groups = bool(spec.get("hide_groups"))
    hide_bands = bool(spec.get("hide_band_labels"))
    if include is not None or exclude or hide_groups or hide_bands:
        # Turning auto-update off gives the legend its own copy of the layer
        # tree, so the edits below can never touch the project's tree.
        item.setAutoUpdateModel(False)
        root = item.model().rootGroup()
        wanted = None
        if include is not None:
            wanted = {_project_layer(name).id() for name in include}
        unwanted = {_project_layer(name).id() for name in exclude}
        for node in list(root.findLayers()):
            layer_id = node.layerId()
            if (wanted is not None and layer_id not in wanted) \
                    or layer_id in unwanted:
                parent = node.parent()
                if parent is not None:
                    parent.removeChildNode(node)
        if hide_groups:
            for group in _legend_groups(root):
                QgsLegendRenderer.setNodeLegendStyle(group,
                                                     QgsLegendStyle.Hidden)
        if hide_bands:
            _drop_band_rows(item.model(), root)
    elif spec.get("auto_update") is not None:
        item.setAutoUpdateModel(bool(spec["auto_update"]))
    sizes = spec.get("font_sizes") or {}
    font = spec.get("font") or {}
    color = spec.get("color")
    if creating or sizes or font or color:
        defaults = {"title": 10, "group": 8.5, "subgroup": 8, "item": 7.5}
        for key, style_id in (("title", QgsLegendStyle.Title),
                              ("group", QgsLegendStyle.Group),
                              ("subgroup", QgsLegendStyle.Subgroup),
                              ("item", QgsLegendStyle.SymbolLabel)):
            style = item.style(style_id)
            text_spec = dict(font)
            text_spec["size"] = sizes.get(key, font.get("size") and (
                font["size"] + (2 if key == "title" else 0))
                or defaults[key])
            if key in ("title", "group"):
                text_spec.setdefault("bold", True)
            text_format = _text_format(text_spec, color)
            if hasattr(style, "setTextFormat"):
                style.setTextFormat(text_format)
            else:  # QGIS < 3.30
                style.setFont(text_format.toQFont())
            item.setStyle(style_id, style)
    if spec.get("symbol_size"):
        width, height = spec["symbol_size"]
        item.setSymbolWidth(float(width))
        item.setSymbolHeight(float(height))
    elif creating:
        item.setSymbolWidth(6.0)
        item.setSymbolHeight(3.5)
    if "resize_to_contents" in spec:
        item.setResizeToContents(bool(spec["resize_to_contents"]))
    elif creating:
        item.setResizeToContents(spec.get("rect") is None)
    item.updateLegend()
    if item.resizeToContents():
        item.adjustBoxSize()


_BAND_ROW = re.compile(r"^Band \d+\b")


def _legend_groups(group):
    """Every group below ``group`` (portable to QGIS without findGroups(True))."""
    found = []
    for child in group.children():
        if child.nodeType() == child.NodeGroup:
            found.append(child)
            found.extend(_legend_groups(child))
    return found


def _drop_band_rows(model, root):
    """Hide raster rows like 'Band 1 (Gray)', keeping ramps and classes.

    A gray raster then shows its name over its colour ramp; an RGB image
    shows just its name.
    """
    for node in root.findLayers():
        if not isinstance(node.layer(), QgsRasterLayer):
            continue
        if QgsMapLayerLegendUtils.hasLegendNodeOrder(node):
            continue
        labels = [str(legend_node.data(Qt.DisplayRole) or "")
                  for legend_node in model.layerLegendNodes(node)]
        keep = [index for index, label in enumerate(labels)
                if not _BAND_ROW.match(label)]
        if len(keep) != len(labels):
            QgsMapLayerLegendUtils.setLegendNodeOrder(node, keep)
            model.refreshLayerLegend(node)


# -- scale bar ---------------------------------------------------------------
_SCALEBAR_UNITS = {
    "m": (QgsUnitTypes.DistanceMeters, "m"),
    "km": (QgsUnitTypes.DistanceKilometers, "km"),
    "ft": (QgsUnitTypes.DistanceFeet, "ft"),
    "mi": (QgsUnitTypes.DistanceMiles, "mi"),
    "nmi": (QgsUnitTypes.DistanceNauticalMiles, "nmi"),
}


def _configure_scalebar(item, spec, layout, creating):
    if "map" in spec or creating:
        target = _target_map(layout, spec.get("map"))
        item.setLinkedMap(target)
    if creating:
        item.applyDefaultSettings()
    if "style" in spec or creating:
        item.setStyle(str(spec.get("style") or "Single Box"))
    if spec.get("units"):
        units = _SCALEBAR_UNITS.get(str(spec["units"]).lower())
        if units is None:
            raise LayoutToolError(
                f"scalebar.units must be one of {sorted(_SCALEBAR_UNITS)}")
        item.setUnits(units[0])
        item.setUnitLabel(units[1])
    elif creating:
        item.applyDefaultSize(item.guessUnits())
    if spec.get("segments") is not None:
        item.setNumberOfSegments(int(spec["segments"]))
    if spec.get("segments_left") is not None:
        item.setNumberOfSegmentsLeft(int(spec["segments_left"]))
    rect = spec.get("rect")
    if spec.get("segment_size"):
        item.setSegmentSizeMode(QgsScaleBarSettings.SegmentSizeFixed)
        item.setUnitsPerSegment(float(spec["segment_size"]))
    elif rect is not None:
        # Fit the bar to the box the spec reserved for it.
        width = _rect(rect)[2]
        item.setSegmentSizeMode(QgsScaleBarSettings.SegmentSizeFitWidth)
        item.setMinimumBarWidth(max(5.0, width * 0.55))
        item.setMaximumBarWidth(max(10.0, width * 0.85))
    if spec.get("bar_height") is not None or creating:
        item.setHeight(float(spec.get("bar_height", 2.0)))
    if creating or "font" in spec or "color" in spec:
        item.setTextFormat(_text_format(
            spec.get("font") or {"size": 7}, spec.get("color")))
    if spec.get("align"):
        item.setAlignment({
            "left": QgsScaleBarSettings.AlignLeft,
            "center": QgsScaleBarSettings.AlignMiddle,
            "right": QgsScaleBarSettings.AlignRight,
        }.get(str(spec["align"]), QgsScaleBarSettings.AlignLeft))
    item.update()
    item.refresh()


# -- pictures ----------------------------------------------------------------
def _configure_picture(item, spec, layout, north_arrow):
    path = spec.get("path")
    if north_arrow and not path and not item.picturePath():
        path = DEFAULT_NORTH_ARROW
    if path:
        resolved = _resolve_svg(str(path))
        item.setPicturePath(resolved)
    if north_arrow:
        if "map" in spec or not item.linkedMap():
            target = _target_map(layout, spec.get("map"), required=False)
            if target is not None:
                item.setLinkedMap(target)
        mode = str(spec.get("north", "grid"))
        item.setNorthMode(QgsLayoutItemPicture.TrueNorth if mode == "true"
                          else QgsLayoutItemPicture.GridNorth)
    if spec.get("fill"):
        item.setSvgFillColor(_color(spec["fill"]))
    if spec.get("stroke"):
        item.setSvgStrokeColor(_color(spec["stroke"]))
    item.setResizeMode(QgsLayoutItemPicture.Zoom)


def _resolve_svg(path):
    """'arrows/NorthArrow_02.svg' → a real file from QGIS's SVG paths."""
    if path.startswith(":/") or os.path.isabs(path) or os.path.isfile(path):
        return path
    for root in QgsApplication.svgPaths():
        candidate = os.path.join(root, path)
        if os.path.isfile(candidate):
            return candidate
    raise LayoutToolError(f"image not found: {path}")


# -- shapes and lines --------------------------------------------------------
def _configure_shape(item, spec, kind):
    item.setShapeType(QgsLayoutItemShape.Ellipse if kind == "ellipse"
                      else QgsLayoutItemShape.Rectangle)
    fill = _color(spec.get("fill")) if "fill" in spec else None
    stroke = _color(spec.get("stroke", "#000000"))
    properties = {
        "color": _symbol_color(fill) if fill is not None else "0,0,0,0",
        "style": "solid" if fill is not None else "no",
        "outline_color": _symbol_color(stroke) if stroke is not None
        else "0,0,0,0",
        "outline_style": "solid" if stroke is not None else "no",
        "outline_width": str(spec.get("stroke_width", 0.3)),
        "outline_width_unit": "MM",
    }
    item.setSymbol(QgsFillSymbol.createSimple(properties))
    if spec.get("radius"):
        item.setCornerRadius(QgsLayoutMeasurement(float(spec["radius"]), _MM))


def _configure_line(layout, item, spec):
    points = spec.get("points")
    if points is not None:
        if len(points) < 2:
            raise LayoutToolError("a line needs at least two points")
        page = int(spec.get("page", 1)) - 1
        origin = layout.pageCollection().page(page).pos()
        polygon = QPolygonF([QPointF(origin.x() + float(x),
                                     origin.y() + float(y))
                             for x, y in points])
        item.setNodes(polygon)
    elif item.nodes().isEmpty():
        raise LayoutToolError("'points' [[x, y], …] in mm are required")
    if "stroke" in spec or "stroke_width" in spec or points is not None:
        stroke = _color(spec.get("stroke", "#000000"))
        item.setSymbol(QgsLineSymbol.createSimple({
            "line_color": _symbol_color(stroke),
            "line_width": str(spec.get("stroke_width", 0.3)),
            "line_width_unit": "MM",
        }))


# -- tables ------------------------------------------------------------------
def _build_table(layout, spec, kind):
    rect = spec.get("rect")
    if rect is None:
        raise LayoutToolError("'rect' is required for a table")
    x, y, width, height = _rect(rect)
    font = spec.get("font") or {"size": 7}
    if kind == "table":
        table = QgsLayoutItemManualTable.create(layout)
        layout.addMultiFrame(table)
        rows = spec.get("rows") or []
        if not rows or not all(isinstance(row, list) for row in rows):
            raise LayoutToolError("a table needs 'rows' (lists of cell text)")
        bold_format = _text_format(dict(font, bold=True), spec.get("color"))
        contents = []
        for row in rows:
            cells = []
            for value in row:
                text = str(value if value is not None else "")
                bold = _is_bold(text)
                if bold:
                    text = text[2:-2]
                # Cells do not evaluate "[% … %]" the way labels do; they need
                # a real expression property.
                cell = QgsTableCell(
                    QgsProperty.fromExpression(_template_expression(text))
                    if "[%" in text else text)
                if bold:
                    cell.setTextFormat(bold_format)
                cells.append(cell)
            contents.append(cells)
        table.setTableContents(contents)
        table.setIncludeTableHeader(False)
        columns = max(len(row) for row in rows)
    else:
        layer = _project_layer(spec.get("layer"))
        table = QgsLayoutItemAttributeTable.create(layout)
        layout.addMultiFrame(table)
        table.setVectorLayer(layer)
        if spec.get("columns"):
            table.setDisplayedFields([str(name) for name in spec["columns"]])
        table.setMaximumNumberOfFeatures(int(spec.get("max_rows", 30)))
        if spec.get("filter"):
            table.setFilterFeatures(True)
            table.setFeatureFilter(str(spec["filter"]))
        table.setHeaderTextFormat(_text_format(dict(font, bold=True),
                                               spec.get("color")))
        columns = len(table.columns()) if hasattr(table, "columns") else 0
    table.setContentTextFormat(_text_format(font, spec.get("color")))
    grid = bool(spec.get("grid", True))
    grid_width = float(spec.get("grid_width", 0.2))
    table.setShowGrid(grid)
    table.setGridStrokeWidth(grid_width)
    table.setGridColor(_color(spec.get("grid_color") or "#333333"))
    margin = float(spec.get("cell_margin", 1.0))
    table.setCellMargin(margin)
    frame = QgsLayoutFrame(layout, table)
    frame.attemptResize(QgsLayoutSize(width, height, _MM))
    frame.attemptMove(QgsLayoutPoint(x, y, _MM), True, False,
                      int(spec.get("page", 1)) - 1)
    if spec.get("id"):
        frame.setId(str(spec["id"]))
    table.addFrame(frame)
    table.refreshAttributes()
    if kind == "table":
        # Spread spare height over the rows through the cell padding — an
        # explicit QGIS row height doubles in the rendered table — so the
        # rows fill the box a preset reserved for them.
        natural = _table_content_height(layout, table)
        if (spec.get("fill_height", True) and natural is not None
                and 0 < natural < height):
            margin += (height - natural) / len(rows) / 2.0
            table.setCellMargin(margin)
        # A table frame takes the width of its columns, so the columns must
        # add up to the box or the frame shrinks to fit the text.
        _set_column_widths(table, spec, columns, width, margin,
                           grid_width if grid else 0.0)
    table.refresh()
    _apply_common(frame, spec)
    return frame


def _existing_table_spec(layout, frame):
    """The spec a rebuilt table needs to look like ``frame``'s table."""
    owner = frame.multiFrame()
    pages = layout.pageCollection()
    position = pages.positionOnPage(frame.pos())
    size = layout.convertToLayoutUnits(frame.sizeWithUnits())
    spec = {
        "rect": [round(position.x(), 2), round(position.y(), 2),
                 round(size.width(), 2), round(size.height(), 2)],
        "page": pages.pageNumberForPoint(frame.pos()) + 1,
    }
    if owner is None:
        return spec
    text_format = owner.contentTextFormat()
    spec["font"] = {"family": text_format.font().family(),
                    "size": round(text_format.size(), 2)}
    spec["color"] = text_format.color().name()
    spec["grid"] = owner.showGrid()
    spec["grid_width"] = owner.gridStrokeWidth()
    spec["grid_color"] = owner.gridColor().name()
    if isinstance(owner, QgsLayoutItemManualTable):
        spec["rows"] = [[_cell_spec(cell) for cell in row]
                        for row in owner.tableContents()]
        widths = list(owner.columnWidths())
        if widths and all(width > 0 for width in widths):
            spec["col_fractions"] = widths
    elif isinstance(owner, QgsLayoutItemAttributeTable):
        layer = owner.vectorLayer()
        if layer is not None:
            spec["layer"] = layer.id()
        spec["columns"] = [column.attribute() for column in owner.columns()]
        spec["max_rows"] = owner.maximumNumberOfFeatures()
        if owner.filterFeatures() and owner.featureFilter():
            spec["filter"] = owner.featureFilter()
    return spec


def _cell_spec(cell):
    """A table cell back as spec text: '**bold**', '[% expr %]', or plain."""
    content = cell.content()
    if isinstance(content, QgsProperty):
        text = "[% " + content.expressionString() + " %]"
    else:
        text = str(content if content is not None else "")
    text_format = cell.textFormat()
    bold = text_format.isValid() and (
        text_format.font().bold()
        or (hasattr(text_format, "forcedBold") and text_format.forcedBold()))
    return f"**{text}**" if bold and text else text


def _set_column_widths(table, spec, columns, width, margin, grid_width):
    if spec.get("col_widths"):
        table.setColumnWidths([float(value) for value in spec["col_widths"]])
        return
    if columns <= 0:
        return
    fractions = spec.get("col_fractions") or [1.0 / columns] * columns
    if len(fractions) != columns:
        raise LayoutToolError(
            f"col_fractions needs {columns} values, one per column")
    total = float(sum(fractions)) or 1.0
    usable = width - columns * 2 * margin - (columns + 1) * grid_width
    if usable <= columns:
        raise LayoutToolError("table rect is too narrow for its columns")
    table.setColumnWidths([round(usable * value / total, 2)
                           for value in fractions])


# ==========================================================================
# Presets — page-adaptive starting designs, merged with spec.items by id
# ==========================================================================
def preset_items(name, layout, style=None):
    if not name:
        return []
    if name not in PRESETS:
        raise LayoutToolError(f"unknown preset {name!r}; use one of {PRESETS}")
    size = layout.pageCollection().page(0).pageSize()
    width, height = size.width(), size.height()
    s = _Style(width, height, style or {})
    return {"side_panel": _preset_side_panel,
            "title_strip": _preset_title_strip,
            "report_figure": _preset_report_figure}[name](width, height, s)


class _Style:
    def __init__(self, width, height, overrides):
        # Fonts and margins grow gently with paper size (A4 = 1.0, A3 ≈ 1.25,
        # A1 ≈ 1.75) so one preset reads well from A4 to A0.
        area_ratio = math.sqrt((width * height) / (297.0 * 210.0))
        self.k = max(0.85, min(2.6, 1.0 + (area_ratio - 1.0) * 0.6))
        self.font = overrides.get("font", "Arial")
        self.accent = overrides.get("accent", "#1F3864")
        self.text = overrides.get("text_color", "#1A1A1A")
        self.line = overrides.get("line_color", "#333333")
        self.margin = round(overrides.get("margin", 6.0 + 2.0 * self.k), 1)

    def f(self, size, bold=False, color=None):
        return {"family": self.font, "size": round(size * self.k, 1),
                "bold": bold, "color": color or self.text}

    def mm(self, value):
        return round(value * self.k, 1)


_SCALE_EXPR = ("Scale 1:[% format_number(map_get(item_variables('map_main'),"
               " 'map_scale'), 0) %]")
_CRS_EXPR = ("[% map_get(item_variables('map_main'), 'map_crs') %] - "
             "[% map_get(item_variables('map_main'), 'map_crs_description') %]")


def _preset_side_panel(width, height, s):
    m = s.margin
    panel_w = round(min(max((width - 2 * m) * 0.26, 62.0), 125.0), 1)
    map_w = round(width - 2 * m - panel_w, 1)
    inner_h = height - 2 * m
    px = round(m + map_w, 1)
    pad = s.mm(3.0)
    cx = round(px + pad, 1)
    cw = round(panel_w - 2 * pad, 1)
    items = [
        {"id": "map_main", "type": "map", "rect": [m, m, map_w, inner_h],
         "extent": None, "frame": {"width": 0.4, "color": s.line},
         "grid": {"style": "cross", "frame": "ticks", "font_size":
                  round(5.5 * s.k, 1),
                  "annotation_sides": ["left", "top", "bottom"]}},
        {"id": "panel_frame", "type": "rectangle",
         "rect": [px, m, panel_w, inner_h], "fill": "#FFFFFF",
         "stroke": s.line, "stroke_width": 0.4},
    ]
    y = m + pad
    title_h = s.mm(11)
    items += [
        {"id": "map_title", "type": "label", "text": "[Map Title]",
         "rect": [cx, y, cw, title_h], "font": s.f(15, True, s.accent),
         "align": "center", "valign": "middle"},
        {"id": "project_name", "type": "label", "text": "[Project Name]",
         "rect": [cx, y + title_h, cw, s.mm(9)], "font": s.f(9),
         "align": "center", "valign": "top"},
    ]
    y += title_h + s.mm(9) + s.mm(2)
    items.append(_hline("rule_1", px, y, panel_w, s))
    y += s.mm(3)
    arrow = s.mm(15)
    items += [
        {"id": "north_arrow", "type": "north_arrow", "map": "map_main",
         "rect": [round(cx + (cw - arrow) / 2, 1), y, arrow, arrow]},
    ]
    y += arrow + s.mm(2)
    items += [
        {"id": "scale_bar", "type": "scalebar", "map": "map_main",
         "rect": [cx, y, cw, s.mm(9)], "style": "Single Box",
         "font": s.f(6.5), "align": "center"},
        {"id": "scale_text", "type": "label", "text": _SCALE_EXPR,
         "rect": [cx, y + s.mm(10), cw, s.mm(5)], "font": s.f(7),
         "align": "center", "valign": "middle"},
    ]
    y += s.mm(16)
    items.append(_hline("rule_2", px, y, panel_w, s))
    y += s.mm(2)

    # Bottom-up: information table, location inset, projection note.
    rows = [["**Proponent**", "[Proponent]"],
            ["**Location**", "[Location]"],
            ["**Prepared by**", "[Prepared By]"],
            ["**Date**", "[Date]"],
            ["**Map No.**", "[Map No.]"]]
    row_h = s.mm(5.5)
    table_h = round(row_h * len(rows), 1)
    table_y = round(m + inner_h - pad - table_h, 1)
    items.append({"id": "info_table", "type": "table",
                  "rect": [cx, table_y, cw, table_h], "rows": rows,
                  "col_fractions": [0.36, 0.64], "font": s.f(6.5),
                  "grid_color": s.line})
    note_h = s.mm(9)
    note_y = round(table_y - note_h - s.mm(1.5), 1)
    items.append({"id": "crs_note", "type": "label",
                  "text": "Projection: " + _CRS_EXPR + "\nSource: [Source]",
                  "rect": [cx, note_y, cw, note_h], "font": s.f(6),
                  "valign": "top"})
    inset_h = round(min(cw * 0.75, inner_h * 0.24), 1)
    inset_y = round(note_y - inset_h - s.mm(6), 1)
    items += [
        {"id": "inset_title", "type": "label", "text": "LOCATION MAP",
         "rect": [cx, inset_y - s.mm(5), cw, s.mm(4.5)],
         "font": s.f(7, True, s.accent), "align": "center",
         "valign": "middle"},
        {"id": "map_inset", "type": "map",
         "rect": [cx, inset_y, cw, inset_h], "frame": {"width": 0.3},
         "overview": {"of": "map_main"}},
    ]
    legend_top = y
    legend_h = round(inset_y - s.mm(7) - legend_top, 1)
    items.append({"id": "legend", "type": "legend", "map": "map_main",
                  "title": "LEGEND", "rect": [cx, legend_top, cw, legend_h],
                  "resize_to_contents": False, "filter_by_map": True,
                  "font_sizes": {"title": round(9 * s.k, 1),
                                 "group": round(7.5 * s.k, 1),
                                 "subgroup": round(7 * s.k, 1),
                                 "item": round(6.8 * s.k, 1)}})
    return items


def _preset_title_strip(width, height, s):
    m = s.margin
    strip_h = round(min(max(height * 0.15, 30.0), 62.0), 1)
    inner_w = round(width - 2 * m, 1)
    map_h = round(height - 2 * m - strip_h, 1)
    sy = round(m + map_h, 1)
    logo_w = round(strip_h, 1)
    info_w = round(min(max(inner_w * 0.30, 70.0), 150.0), 1)
    title_w = round(inner_w - logo_w - info_w, 1)
    pad = s.mm(2.5)
    box = s.mm(4)
    legend_w = round(min(max(width * 0.2, 42.0), 95.0), 1)
    items = [
        {"id": "map_main", "type": "map", "rect": [m, m, inner_w, map_h],
         "frame": {"width": 0.4, "color": s.line},
         "grid": {"style": "cross", "frame": "ticks",
                  "font_size": round(5.5 * s.k, 1),
                  "annotation_sides": ["left", "right", "top"]}},
        {"id": "strip_frame", "type": "rectangle",
         "rect": [m, sy, inner_w, strip_h], "fill": "#FFFFFF",
         "stroke": s.line, "stroke_width": 0.4},
        {"id": "strip_div_1", "type": "line",
         "points": [[m + logo_w, sy], [m + logo_w, sy + strip_h]],
         "stroke": s.line, "stroke_width": 0.3},
        {"id": "strip_div_2", "type": "line",
         "points": [[m + logo_w + title_w, sy],
                    [m + logo_w + title_w, sy + strip_h]],
         "stroke": s.line, "stroke_width": 0.3},
        {"id": "logo", "type": "picture",
         "rect": [m + pad, sy + pad, logo_w - 2 * pad, strip_h - 2 * pad]},
    ]
    tx = round(m + logo_w + pad, 1)
    tw = round(title_w - 2 * pad, 1)
    line_h = round((strip_h - 2 * pad) / 4.2, 1)
    items += [
        {"id": "map_title", "type": "label", "text": "[Map Title]",
         "rect": [tx, sy + pad, tw, round(line_h * 1.5, 1)],
         "font": s.f(14, True, s.accent), "align": "center",
         "valign": "middle"},
        {"id": "project_name", "type": "label", "text": "[Project Name]",
         "rect": [tx, round(sy + pad + line_h * 1.5, 1), tw, line_h],
         "font": s.f(9, True), "align": "center", "valign": "middle"},
        {"id": "proponent", "type": "label", "text": "[Proponent]",
         "rect": [tx, round(sy + pad + line_h * 2.5, 1), tw, line_h * 0.85],
         "font": s.f(7.5), "align": "center", "valign": "middle"},
        {"id": "location", "type": "label", "text": "[Location]",
         "rect": [tx, round(sy + pad + line_h * 3.35, 1), tw, line_h * 0.85],
         "font": s.f(7.5), "align": "center", "valign": "middle"},
    ]
    ix = round(m + logo_w + title_w + pad, 1)
    iw = round(info_w - 2 * pad, 1)
    rows = [["**Prepared by**", "[Prepared By]"],
            ["**Date**", "[Date]"],
            ["**Scale**", _SCALE_EXPR.replace("Scale ", "")],
            ["**Map No.**", "[Map No.]"]]
    row_h = round((strip_h - 2 * pad) / len(rows), 1)
    items.append({"id": "info_table", "type": "table",
                  "rect": [ix, sy + pad, iw, row_h * len(rows)],
                  "rows": rows,
                  "col_fractions": [0.38, 0.62], "font": s.f(6.5),
                  "grid_color": s.line})
    # On-map overlays: legend top-left, north arrow top-right, scale
    # bottom-left, each on a white plate so it reads over any basemap.
    arrow = s.mm(14)
    items += [
        {"id": "legend", "type": "legend", "map": "map_main",
         "title": "LEGEND", "rect": [m + box, m + box, legend_w,
                                     round(map_h * 0.3, 1)],
         "resize_to_contents": True, "background": "#FFFFFFE6",
         "frame": {"width": 0.25, "color": s.line}, "filter_by_map": True,
         "font_sizes": {"title": round(8.5 * s.k, 1),
                        "group": round(7.2 * s.k, 1),
                        "subgroup": round(6.8 * s.k, 1),
                        "item": round(6.5 * s.k, 1)}},
        {"id": "north_arrow", "type": "north_arrow", "map": "map_main",
         "rect": [round(m + inner_w - box - arrow, 1), m + box, arrow,
                  arrow], "background": "#FFFFFFE6"},
        {"id": "scale_bar", "type": "scalebar", "map": "map_main",
         "rect": [m + box, round(m + map_h - box - s.mm(11), 1),
                  round(min(inner_w * 0.32, 110.0), 1), s.mm(11)],
         "style": "Single Box", "background": "#FFFFFFE6",
         "font": s.f(6.5)},
    ]
    return items


def _preset_report_figure(width, height, s):
    m = s.margin
    inner_w = round(width - 2 * m, 1)
    title_h = s.mm(9)
    caption_h = s.mm(12)
    gap = 4.5  # paper kept clear for the map's outside grid annotations
    below_h = round(min(max((height - 2 * m) * 0.15, 26.0), 60.0), 1)
    map_h = round(height - 2 * m - title_h - below_h - caption_h - 2 * gap, 1)
    my = round(m + title_h + gap, 1)
    by = round(my + map_h + gap, 1)
    side_w = round(min(inner_w * 0.32, 70.0), 1)
    items = [
        {"id": "map_title", "type": "label", "text": "[Map Title]",
         "rect": [m, m, inner_w, title_h], "font": s.f(13, True, s.accent),
         "align": "left", "valign": "middle"},
        {"id": "map_main", "type": "map", "rect": [m, my, inner_w, map_h],
         "frame": {"width": 0.3, "color": s.line},
         "grid": {"style": "cross", "frame": "ticks",
                  "font_size": round(5.5 * s.k, 1)}},
        {"id": "legend", "type": "legend", "map": "map_main",
         "title": "Legend",
         "rect": [m, by, round(inner_w - side_w - s.mm(3), 1),
                  round(below_h - s.mm(2), 1)],
         "resize_to_contents": False, "columns": 2, "filter_by_map": True,
         "font_sizes": {"title": round(8.5 * s.k, 1),
                        "group": round(7.2 * s.k, 1),
                        "subgroup": round(6.8 * s.k, 1),
                        "item": round(6.6 * s.k, 1)}},
    ]
    sx = round(m + inner_w - side_w, 1)
    arrow = s.mm(12)
    items += [
        {"id": "north_arrow", "type": "north_arrow", "map": "map_main",
         "rect": [round(sx + (side_w - arrow) / 2, 1), by, arrow, arrow]},
        {"id": "scale_bar", "type": "scalebar", "map": "map_main",
         "rect": [sx, round(by + arrow + s.mm(1), 1), side_w, s.mm(9)],
         "style": "Single Box", "font": s.f(6.3), "align": "center"},
        {"id": "caption", "type": "label",
         "text": "Figure [Figure No.]. [Caption]",
         "rect": [m, round(by + below_h, 1), inner_w,
                  s.mm(6)], "font": s.f(8, True), "valign": "middle"},
        {"id": "source_note", "type": "label",
         "text": "Source: [Source]. Projection: " + _CRS_EXPR,
         "rect": [m, round(by + below_h + s.mm(6), 1),
                  inner_w, s.mm(6)], "font": s.f(6.3), "valign": "top"},
    ]
    return items


def _hline(item_id, x, y, length, s):
    return {"id": item_id, "type": "line",
            "points": [[x, round(y, 1)], [round(x + length, 1), round(y, 1)]],
            "stroke": s.line, "stroke_width": 0.3}


def _merge_items(base, overrides):
    """Overlay ``overrides`` onto preset items by id; unknown ids append."""
    if not isinstance(overrides, list):
        raise LayoutToolError("spec.items must be a list")
    merged = [dict(item) for item in base]
    index = {item.get("id"): position for position, item in enumerate(merged)
             if item.get("id")}
    for override in overrides:
        if not isinstance(override, dict):
            raise LayoutToolError("each item spec must be an object")
        item_id = override.get("id")
        if item_id in index:
            position = index[item_id]
            if override.get("remove"):
                merged[position] = None
                continue
            combined = dict(merged[position])
            for key, value in override.items():
                if isinstance(value, dict) and isinstance(
                        combined.get(key), dict):
                    combined[key] = dict(combined[key], **value)
                else:
                    combined[key] = value
            merged[position] = combined
        else:
            merged.append(dict(override))
    return [item for item in merged if item is not None
            and not (item.get("remove") and item.get("id") not in index)]


# ==========================================================================
# Small helpers
# ==========================================================================
def _args(args):
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    return args if isinstance(args, dict) else {}


def _set_page(layout, page_spec):
    if not isinstance(page_spec, dict):
        raise LayoutToolError(
            "page must be {'size': 'A3', 'orientation': 'landscape'} or "
            "{'width': mm, 'height': mm}")
    collection = layout.pageCollection()
    for page in collection.pages():
        if page_spec.get("width") and page_spec.get("height"):
            page.setPageSize(QgsLayoutSize(float(page_spec["width"]),
                                           float(page_spec["height"]), _MM))
            continue
        orientation = (QgsLayoutItemPage.Portrait
                       if str(page_spec.get("orientation", "landscape"))
                       .lower().startswith("p")
                       else QgsLayoutItemPage.Landscape)
        if not page.setPageSize(str(page_spec.get("size") or "A4"),
                                orientation):
            raise LayoutToolError(
                f"unknown page size {page_spec.get('size')!r} (A0-A6, B0-B6,"
                " Letter, Legal, ANSI A-E, Arch A-E3, or width/height)")
    collection.reflow()


def _page_summary(page):
    size = page.pageSize()
    name = QgsApplication.pageSizeRegistry().find(size)
    return {
        "size": name or "custom",
        "width_mm": round(size.width(), 1),
        "height_mm": round(size.height(), 1),
        "orientation": ("landscape" if size.width() > size.height()
                        else "portrait"),
    }


def _is_content_item(item):
    return (hasattr(item, "id") and hasattr(item, "sizeWithUnits")
            and not isinstance(item, QgsLayoutItemPage)
            and type(item).__name__ != "QgsLayoutItemGroup"
            and item.layout() is not None)


def _is_overlay_item(item):
    return isinstance(item, (QgsLayoutItemLabel, QgsLayoutItemLegend,
                             QgsLayoutItemScaleBar, QgsLayoutItemPicture,
                             QgsLayoutFrame))


def _item_type(item):
    if isinstance(item, QgsLayoutItemMap):
        return "map"
    if isinstance(item, QgsLayoutItemLabel):
        return "label"
    if isinstance(item, QgsLayoutItemLegend):
        return "legend"
    if isinstance(item, QgsLayoutItemScaleBar):
        return "scalebar"
    if isinstance(item, QgsLayoutItemPicture):
        return "north_arrow" if item.linkedMap() is not None or \
            "north" in item.picturePath().lower() else "picture"
    if isinstance(item, QgsLayoutItemShape):
        return ("ellipse" if item.shapeType() == QgsLayoutItemShape.Ellipse
                else "rectangle")
    if isinstance(item, QgsLayoutItemPolyline):
        return "line"
    if isinstance(item, QgsLayoutFrame):
        owner = item.multiFrame()
        if isinstance(owner, QgsLayoutItemManualTable):
            return "table"
        if isinstance(owner, QgsLayoutItemAttributeTable):
            return "attribute_table"
        return "frame"
    return type(item).__name__


def _find_item(layout, item_id):
    if not item_id:
        return None
    for item in layout.items():
        if _is_content_item(item) and item.id() == item_id:
            return item
    return None


def _remove_item(layout, item):
    """Remove an item; for a table, every frame it is drawn in.

    ``removeMultiFrame`` only detaches the table's content — its frames stay
    on the page as orphans — so the frames are removed as items, and the
    table with them.
    """
    owner = item.multiFrame() if isinstance(item, QgsLayoutFrame) else None
    if owner is None:
        layout.removeLayoutItem(item)
        return
    for frame in list(owner.frames()) or [item]:
        layout.removeLayoutItem(frame)
    if any(other is owner for other in layout.multiFrames()):
        layout.removeMultiFrame(owner)


def _target_map(layout, map_id=None, required=True):
    if map_id:
        item = _find_item(layout, str(map_id))
        if not isinstance(item, QgsLayoutItemMap):
            raise LayoutToolError(f"no map item with id {map_id!r}")
        return item
    maps = [item for item in layout.items()
            if isinstance(item, QgsLayoutItemMap)]
    main = [item for item in maps if item.id() == "map_main"]
    if main:
        return main[0]
    if maps:
        return max(maps, key=lambda item: item.rect().width()
                   * item.rect().height())
    if required:
        raise LayoutToolError("the layout has no map item to link to")
    return None


def _project_layer(name):
    project = QgsProject.instance()
    name = str(name or "").strip()
    layer = project.mapLayer(name)
    if layer is None:
        matches = project.mapLayersByName(name)
        layer = matches[0] if matches else None
    if layer is None:
        raise LayoutToolError(f"layer not found in project: {name!r}")
    return layer


def _transform_rect(rect, source, target):
    if not source.isValid() or not target.isValid() or source == target:
        return QgsRectangle(rect)
    transform = QgsCoordinateTransform(source, target, QgsProject.instance())
    return transform.transformBoundingBox(rect)


def _pad(rect, fraction):
    if rect is None or rect.isEmpty():
        if rect is not None and rect.width() == 0 and rect.height() == 0:
            # A single point: give it a sensible window.
            grow = 500.0 if abs(rect.xMinimum()) > 360 else 0.01
            return QgsRectangle(rect.xMinimum() - grow, rect.yMinimum() - grow,
                                rect.xMaximum() + grow, rect.yMaximum() + grow)
        return rect
    dx = rect.width() * fraction
    dy = rect.height() * fraction
    return QgsRectangle(rect.xMinimum() - dx, rect.yMinimum() - dy,
                        rect.xMaximum() + dx, rect.yMaximum() + dy)


_NICE_SCALES = (
    100, 200, 250, 500, 1000, 1250, 1500, 2000, 2500, 3000, 4000, 5000, 6000,
    7500, 10000, 12500, 15000, 20000, 25000, 30000, 40000, 50000, 60000,
    75000, 100000, 125000, 150000, 200000, 250000, 300000, 400000, 500000,
    750000, 1000000, 1500000, 2000000, 2500000, 3000000, 4000000, 5000000,
    7500000, 10000000, 15000000, 20000000, 25000000, 50000000)


def _nice_scale(scale):
    """Round up to a conventional map scale so the extent still fits."""
    for nice in _NICE_SCALES:
        if scale <= nice * 1.0001:
            return float(nice)
    return float(round(scale, -6))


def _nice_interval(raw):
    if raw <= 0:
        return 1.0
    exponent = math.floor(math.log10(raw))
    base = raw / 10 ** exponent
    step = 1 if base < 1.5 else 2 if base < 3.5 else 5 if base < 7.5 else 10
    return step * 10 ** exponent


def _nice_degrees(_interval, item):
    """A degree interval for a geographic grid over a projected map."""
    extent = _transform_rect(item.extent(), item.crs(),
                             QgsCoordinateReferenceSystem("EPSG:4326"))
    raw = max(extent.width(), extent.height()) / 5.0
    for step in (1 / 120, 1 / 60, 1 / 30, 1 / 12, 1 / 6, 1 / 4, 1 / 2, 1, 2,
                 5, 10):
        if raw <= step:
            return step
    return 15


def _rect(rect):
    if not isinstance(rect, (list, tuple)) or len(rect) != 4:
        raise LayoutToolError("rect must be [x, y, width, height] in mm")
    x, y, width, height = (float(value) for value in rect)
    if width <= 0 or height <= 0:
        raise LayoutToolError("rect width and height must be positive")
    return x, y, width, height


def _color(value):
    if value is None:
        return None
    if isinstance(value, QColor):
        return value
    text = str(value).strip()
    if not text or text.lower() in ("none", "transparent", "no"):
        return None
    if re.fullmatch(r"#[0-9a-fA-F]{8}", text):
        # CSS order #RRGGBBAA; QColor reads 8 hex digits as #AARRGGBB.
        color = QColor(text[:7])
        color.setAlpha(int(text[7:9], 16))
        return color
    if re.fullmatch(r"\d{1,3}(,\s*\d{1,3}){2,3}", text):
        parts = [int(part) for part in text.split(",")]
        return QColor(*parts)
    color = QColor(text)
    if not color.isValid():
        raise LayoutToolError(f"invalid color {value!r}")
    return color


def _symbol_color(color):
    return "{},{},{},{}".format(color.red(), color.green(), color.blue(),
                                color.alpha())


def _text_format(font_spec, color=None, base=None):
    font_spec = font_spec or {}
    if isinstance(font_spec, (int, float)):
        font_spec = {"size": font_spec}
    fmt = QgsTextFormat(base) if base is not None else QgsTextFormat()
    font = QFont(fmt.font()) if base is not None else QFont()
    if font_spec.get("family"):
        font.setFamily(str(font_spec["family"]))
    elif base is None:
        font.setFamily("Arial")
    if "bold" in font_spec:
        font.setBold(bool(font_spec["bold"]))
    if "italic" in font_spec:
        font.setItalic(bool(font_spec["italic"]))
    fmt.setFont(font)
    if "bold" in font_spec and hasattr(fmt, "setForcedBold"):
        fmt.setForcedBold(bool(font_spec["bold"]))
    if "italic" in font_spec and hasattr(fmt, "setForcedItalic"):
        fmt.setForcedItalic(bool(font_spec["italic"]))
    if font_spec.get("size") or base is None:
        fmt.setSize(float(font_spec.get("size") or 8))
        fmt.setSizeUnit(QgsUnitTypes.RenderPoints)
    chosen = color or font_spec.get("color")
    if chosen or base is None:
        fmt.setColor(_color(chosen or "#1A1A1A"))
    return fmt


def _halign(value):
    return {"left": Qt.AlignLeft, "center": Qt.AlignHCenter,
            "right": Qt.AlignRight, "justify": Qt.AlignJustify}.get(
        str(value), Qt.AlignLeft)


def _valign(value):
    return {"top": Qt.AlignTop, "middle": Qt.AlignVCenter,
            "center": Qt.AlignVCenter, "bottom": Qt.AlignBottom}.get(
        str(value), Qt.AlignTop)


def _template_expression(text):
    """'1:[% expr %]' → "concat('1:', (expr))" for a table-cell property."""
    parts = re.split(r"\[%(.*?)%\]", text)
    if len(parts) == 3 and not parts[0] and not parts[2]:
        # A cell that is one expression stays that expression, so a table
        # rebuilt from its own cells does not nest concat() each time.
        return parts[1].strip()
    pieces = []
    for index, part in enumerate(parts):
        if index % 2:
            pieces.append("(" + part.strip() + ")")
        elif part:
            pieces.append("'" + part.replace("'", "''") + "'")
    return "concat(" + ", ".join(pieces) + ")" if pieces else "''"


def _cell_text(cell):
    content = cell.content()
    if isinstance(content, QgsProperty):
        return "[expression] " + content.expressionString()
    return str(content if content is not None else "")


def _is_bold(text):
    return len(text) > 4 and text.startswith("**") and text.endswith("**")


def _norm_key(key):
    key = str(key or "").strip()
    if key.startswith("[") and key.endswith("]"):
        key = key[1:-1]
    return re.sub(r"[\s_\-]+", " ", key).strip().casefold()


def _safe_filename(name):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(" .")
    return cleaned or "layout"


def _slug(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", str(name)).strip("_")[:40] or "layout"


def _clip(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
