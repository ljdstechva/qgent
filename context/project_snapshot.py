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


def snapshot_layer(layer):
    """Return immutable, prompt-safe metadata for one live map layer.

    QGIS may invalidate the underlying C++ object while a layer-tree selection
    is changing. Every access is therefore guarded and a dead reference is
    represented by ``None`` so callers can skip it without breaking a send.
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
    if hasattr(layer, "featureCount"):
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


def build_selected_layers_section(selected_layers):
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
            count = item.get("feature_count")
            if count is not None:
                details.append(f"{int(count)} features")
            lines.append(f"  - {name} [{', '.join(details)}]")
        except (KeyError, TypeError, ValueError):
            continue
    return "\n".join(lines) if len(lines) > 1 else ""


def build_context_block(iface, selected_layers=None):
    project = QgsProject.instance()
    canvas = iface.mapCanvas()
    active = iface.activeLayer()

    lines = ["## LIVE QGIS PROJECT CONTEXT (auto-injected — do not re-fetch "
             "unless you need detail beyond this)"]
    lines.append(f"Project CRS: {project.crs().authid() or 'unset'}")
    title = project.title() or (project.fileName() or "unsaved project")
    lines.append(f"Project: {title}")

    layers = list(project.mapLayers().values())
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
        if hasattr(lyr, "featureCount"):
            try:
                count = f" {lyr.featureCount()} feats"
            except Exception:
                count = ""
        star = " *ACTIVE*" if active is not None and lyr.id() == active.id() else ""
        lines.append(f"  - {lyr.name()} [{detail}{geom}, {crs}{count}]{star}")

    ext = canvas.extent()
    lines.append(
        "Canvas extent (%s): %.4f, %.4f, %.4f, %.4f"
        % (canvas.mapSettings().destinationCrs().authid(),
           ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()))
    selected_section = build_selected_layers_section(selected_layers)
    if selected_section:
        lines.extend(["", selected_section])
    return "\n".join(lines)
