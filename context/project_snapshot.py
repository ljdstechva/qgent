# -*- coding: utf-8 -*-
"""Build the compact live-project context block injected into each turn.

Prepending this to every user message (a) removes 1-2 discovery round trips and
(b) gives the Supervisor grounded nouns for the Goal Contract, so it never has
to invent a layer/CRS name. Kept small on purpose — full detail is one
``get_project_context`` call away.

Must be called on the GUI thread (it reads the QGIS API).
"""
from qgis.core import QgsProject, QgsWkbTypes


def build_context_block(iface):
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
    return "\n".join(lines)
