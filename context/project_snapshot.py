# -*- coding: utf-8 -*-
"""Build the compact live-project context block injected into each turn.

Prepending this to every user message (a) removes 1-2 discovery round trips and
(b) gives the Supervisor grounded nouns for the Goal Contract, so it never has
to invent a layer/CRS name. Kept small on purpose — full detail is one
``get_project_context`` call away.

Must be called on the GUI thread (it reads the QGIS API).
"""
from qgis.core import QgsProject, QgsWkbTypes


_GEOMETRY_LABELS = {
    QgsWkbTypes.PointGeometry: "vector point",
    QgsWkbTypes.LineGeometry: "vector line",
    QgsWkbTypes.PolygonGeometry: "vector polygon",
    QgsWkbTypes.NullGeometry: "vector table",
    QgsWkbTypes.UnknownGeometry: "vector unknown geometry",
}

_FAST_LAYER_LIMIT = 30
_FAST_MODE_DIRECTIVE = """## FAST MODE (user-enabled for this turn)
Work with the minimum number of model round trips:
- Do the work yourself in ONE consolidated execute_pyqgis script where
  feasible. Do NOT delegate to subagents and do NOT dispatch the
  qa-verifier (Claude backend); skip the separate self-verification
  pass (Codex backend).
- Still report evidence inline: print created layer names, feature
  counts, CRS, and use stat_path for any exported file. Evidence stays;
  the extra verification TURN goes.
- Skip render_map_snapshot unless the user explicitly asked to see the
  result.
- Keep prose terse: answer, evidence, done. No restated plans for
  simple tasks.
- UNCHANGED even in fast mode: destructive-code approval, asking via
  ask_user when ambiguity materially changes the outcome (a wrong
  guess costs more than the question), and honest failure reporting."""


def build_fast_mode_directive():
    """Return the exact per-turn directive injected while Fast mode is on."""
    return _FAST_MODE_DIRECTIVE


def snapshot_layer(layer, include_feature_count=True):
    """Return immutable, prompt-safe metadata for one live map layer.

    QGIS may invalidate the underlying C++ object while a layer-tree selection
    is changing. Every access is therefore guarded and a dead reference is
    represented by ``None`` so callers can skip it without breaking a send.
    ``include_feature_count=False`` avoids a potentially expensive provider
    query for Fast mode.
    """
    if layer is None:
        return None
    try:
        layer_id = str(layer.id())
        name = str(layer.name()).strip()
    except (AttributeError, RuntimeError):
        return None
    if not layer_id or not name:
        return None

    crs = "?"
    try:
        layer_crs = layer.crs()
        if layer_crs is not None and layer_crs.isValid():
            crs = layer_crs.authid() or "?"
    except (AttributeError, RuntimeError):
        pass

    kind = type(layer).__name__
    if hasattr(layer, "wkbType"):
        try:
            kind = _GEOMETRY_LABELS.get(
                QgsWkbTypes.geometryType(layer.wkbType()), "vector layer")
        except (AttributeError, RuntimeError, TypeError):
            kind = "vector layer"
    elif "raster" in kind.lower():
        kind = "raster"

    feature_count = None
    if include_feature_count and hasattr(layer, "featureCount"):
        try:
            feature_count = int(layer.featureCount())
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass

    return {
        "id": layer_id,
        "name": name,
        "kind": kind,
        "crs": crs,
        "feature_count": feature_count,
    }


def build_selected_layers_section(
        selected_layers, include_feature_count=True):
    """Format a frozen selection snapshot for per-turn grounding."""
    selected_layers = list(selected_layers or [])
    if not selected_layers:
        return ""
    lines = [
        "## USER-SELECTED LAYERS (the user has these selected in the Layers "
        "panel — treat them as the layers this request refers to unless the "
        "message says otherwise)"
    ]
    for item in selected_layers:
        try:
            name = str(item["name"])
            details = [str(item.get("kind") or "map layer"),
                       str(item.get("crs") or "?")]
            count = item.get("feature_count") if include_feature_count else None
            if count is not None:
                details.append(f"{int(count)} features")
            lines.append(f"  - {name} [{', '.join(details)}]")
        except (KeyError, TypeError, ValueError):
            continue
    return "\n".join(lines) if len(lines) > 1 else ""


def build_attached_files_section(attached_files):
    """Format the files frozen for this request without touching them."""
    attached_files = list(attached_files or [])
    if not attached_files:
        return ""
    header = [
        "## ATTACHED FILES (the user dropped or pasted these for this "
        "request):",
        "  Read any image listed below — it is usually a screenshot of what "
        "the user is asking about.",
    ]
    lines = []
    for item in attached_files:
        try:
            path = str(item["path"]).replace("\r", " ").replace("\n", " ")
            if not path:
                continue
            size = int(item["size"])
            extension = str(item.get("extension") or "unknown")
            kind = str(
                item.get("file_kind") or item.get("context_kind")
                or item.get("kind") or "file")
            lines.append(
                f"  - {path} [{kind}; {size} bytes; {extension}]")
            warning = str(item.get("warning") or "").strip()
            if warning:
                warning = warning.replace("\r", " ").replace("\n", " ")
                lines.append(f"    Warning: {warning}")
        except (KeyError, TypeError, ValueError):
            continue
    # Every entry can still be skipped above, and a header with no files under
    # it would be a lie to the model.
    return "\n".join(header + lines) if lines else ""


def build_context_block(
        iface, selected_layers=None, attached_files=None, fast_mode=False):
    project = QgsProject.instance()
    canvas = iface.mapCanvas()
    active = iface.activeLayer()

    lines = ["## LIVE QGIS PROJECT CONTEXT (auto-injected — do not re-fetch "
             "unless you need detail beyond this)"]
    lines.append(f"Project CRS: {project.crs().authid() or 'unset'}")
    title = project.title() or (project.fileName() or "unsaved project")
    lines.append(f"Project: {title}")

    layers = list(project.mapLayers().values())
    if fast_mode:
        layer_total = len(layers)
        layers = layers[:_FAST_LAYER_LIMIT]
        lines.append(
            f"Layers ({layer_total}; showing up to {_FAST_LAYER_LIMIT}; "
            "counts omitted — fast mode):")
    else:
        lines.append(f"Layers ({len(layers)}):")
    if not layers:
        lines.append("  (none loaded)")
    for lyr in layers:
        crs = lyr.crs().authid() if lyr.crs().isValid() else "?"
        detail = type(lyr).__name__
        geom = ""
        count = ""
        if hasattr(lyr, "wkbType"):
            try:
                geom = f" {QgsWkbTypes.displayString(lyr.wkbType())}"
            except Exception:
                geom = ""
        if not fast_mode and hasattr(lyr, "featureCount"):
            try:
                count = f" {lyr.featureCount()} feats"
            except Exception:
                count = ""
        star = " *ACTIVE*" if active is not None and lyr.id() == active.id() else ""
        lines.append(f"  - {lyr.name()} [{detail}{geom}, {crs}{count}]{star}")

    ext = canvas.extent()
    if fast_mode:
        lines.append(
            "Canvas extent (%s): %.0f, %.0f, %.0f, %.0f"
            % (canvas.mapSettings().destinationCrs().authid(),
               ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()))
    else:
        lines.append(
            "Canvas extent (%s): %.4f, %.4f, %.4f, %.4f"
            % (canvas.mapSettings().destinationCrs().authid(),
               ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()))
    selected_section = build_selected_layers_section(
        selected_layers, include_feature_count=not fast_mode)
    if selected_section:
        lines.extend(["", selected_section])
    attached_section = build_attached_files_section(attached_files)
    if attached_section:
        lines.extend(["", attached_section])
    if fast_mode:
        lines.extend(["", build_fast_mode_directive()])
    return "\n".join(lines)
