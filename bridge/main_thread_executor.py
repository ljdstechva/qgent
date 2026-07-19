# -*- coding: utf-8 -*-
"""Runs PyQGIS/Processing work on the QGIS GUI thread.

Every method that touches the QGIS API must execute on the main thread. The
socket server lives in a worker thread and hands work here via a queued Qt
signal carrying a small ``payload`` dict; this object fills ``payload['result']``
and sets ``payload['event']`` so the worker can wake and reply to the CLI.

Nothing here blocks on user input — the destructive-code approval gate runs in
the dock *before* work reaches this executor.
"""
import io
import json
import os
import contextlib
import tempfile
import traceback

from qgis.PyQt.QtCore import QObject, pyqtSlot

from .. import config


class MainThreadExecutor(QObject):
    """Slot target that runs one bridge tool call per invocation."""

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface

    # The single entry point. ``payload`` = {tool, args, result, error, event}.
    @pyqtSlot(object)
    def handle(self, payload):
        tool = payload.get("tool")
        args = payload.get("args") or {}
        try:
            handler = getattr(self, f"_tool_{tool}", None)
            if handler is None:
                payload["error"] = f"unknown tool: {tool}"
            else:
                payload["result"] = handler(args)
        except Exception:
            payload["error"] = "ERROR:\n" + traceback.format_exc()[-2000:]
        finally:
            event = payload.get("event")
            if event is not None:
                event.set()

    # -- helpers ------------------------------------------------------------
    def _cap(self):
        return config.get(config.K_MAX_RESULT)

    def _truncate(self, text):
        cap = self._cap()
        if len(text) > cap:
            return text[:cap] + f"\n…[truncated, {len(text) - cap} more chars]"
        return text

    def _namespace(self):
        import processing
        from qgis.utils import iface
        from qgis import core as qgis_core
        from qgis.core import QgsProject

        ns = {
            "iface": iface,
            "QgsProject": QgsProject,
            "processing": processing,
            "qgis": __import__("qgis"),
        }
        # Expose the whole qgis.core namespace for convenience.
        for name in dir(qgis_core):
            if name.startswith("Qgs"):
                ns[name] = getattr(qgis_core, name)
        return ns

    # -- tools --------------------------------------------------------------
    def _tool_execute_pyqgis(self, args):
        code = args.get("code", "")
        ns = self._namespace()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                exec(compile(code, "<execute_pyqgis>", "exec"), ns)  # noqa: S102
        except Exception:
            captured = out.getvalue()
            tb = traceback.format_exc()[-2000:]
            return self._truncate((captured + "\n" if captured else "") + "ERROR:\n" + tb)
        self.iface.mapCanvas().refresh()
        text = out.getvalue()
        return self._truncate(text) if text.strip() else "OK (no stdout)"

    def _tool_get_project_context(self, args):
        from qgis.core import QgsProject, QgsWkbTypes

        project = QgsProject.instance()
        canvas = self.iface.mapCanvas()
        active = self.iface.activeLayer()
        layers = []
        for lyr in project.mapLayers().values():
            entry = {
                "name": lyr.name(),
                "id": lyr.id(),
                "type": type(lyr).__name__,
                "crs": lyr.crs().authid() if lyr.crs().isValid() else "",
            }
            # Vector-specific detail.
            if hasattr(lyr, "featureCount"):
                try:
                    entry["feature_count"] = lyr.featureCount()
                except Exception:
                    pass
            if hasattr(lyr, "geometryType") and hasattr(lyr, "wkbType"):
                try:
                    entry["geometry"] = QgsWkbTypes.displayString(lyr.wkbType())
                except Exception:
                    pass
            layers.append(entry)

        extent = canvas.extent()
        ctx = {
            "project_crs": project.crs().authid(),
            "project_title": project.title() or os.path.basename(project.fileName() or ""),
            "layer_count": len(layers),
            "layers": layers,
            "active_layer": active.name() if active else None,
            "canvas_extent": {
                "xmin": extent.xMinimum(), "ymin": extent.yMinimum(),
                "xmax": extent.xMaximum(), "ymax": extent.yMaximum(),
            },
            "canvas_crs": canvas.mapSettings().destinationCrs().authid(),
        }
        return self._truncate(json.dumps(ctx, indent=2))

    def _tool_run_processing(self, args):
        import processing

        alg_id = args.get("alg_id")
        params = args.get("params") or {}
        if not alg_id:
            return "ERROR: missing 'alg_id'"
        if "OUTPUT" not in params:
            params["OUTPUT"] = "TEMPORARY_OUTPUT"
        try:
            result = processing.run(alg_id, params)
        except Exception:
            return "ERROR running %s:\n%s" % (alg_id, traceback.format_exc()[-1500:])
        # Load a produced layer into the project if it is a file/memory layer.
        summary = {"alg_id": alg_id, "outputs": {}}
        for key, val in result.items():
            summary["outputs"][key] = str(val)
        return self._truncate(json.dumps(summary, indent=2))

    def _tool_get_layer_features(self, args):
        from qgis.core import QgsProject, QgsFeatureRequest, QgsExpression

        name = args.get("layer")
        limit = int(args.get("limit", 20))
        limit = max(1, min(limit, 200))  # hard cap
        filter_expr = args.get("filter_expr")

        layer = self._find_layer(name)
        if layer is None:
            return f"ERROR: layer not found: {name!r}"
        if not hasattr(layer, "getFeatures"):
            return f"ERROR: {name!r} is not a vector layer"

        req = QgsFeatureRequest()
        if filter_expr:
            expr = QgsExpression(filter_expr)
            if expr.hasParserError():
                return f"ERROR: bad filter_expr: {expr.parserErrorString()}"
            req.setFilterExpression(filter_expr)
        req.setLimit(limit)

        fields = [f.name() for f in layer.fields()]
        rows = []
        for feat in layer.getFeatures(req):
            rows.append({fn: _jsonable(feat[fn]) for fn in fields})
        payload = {"layer": layer.name(), "fields": fields,
                   "returned": len(rows), "features": rows}
        return self._truncate(json.dumps(payload, indent=2, default=str))

    def _tool_render_map_snapshot(self, args):
        width = int(args.get("width", 1000))
        height = int(args.get("height", 700))
        canvas = self.iface.mapCanvas()
        path = os.path.join(tempfile.gettempdir(),
                            f"qgis_copilot_snap_{os.getpid()}.png")
        # saveAsImage renders the current canvas at its own size; for a fixed
        # size we grab the canvas pixmap and scale it.
        try:
            from qgis.PyQt.QtCore import QSize
            img = canvas.grab().toImage().scaled(QSize(width, height))
            img.save(path, "PNG")
        except Exception:
            canvas.saveAsImage(path)
        return json.dumps({"snapshot_path": path,
                           "note": "PNG of the current QGIS canvas"})

    # -- lookup -------------------------------------------------------------
    def _find_layer(self, name_or_id):
        from qgis.core import QgsProject

        project = QgsProject.instance()
        if not name_or_id:
            return self.iface.activeLayer()
        by_id = project.mapLayer(name_or_id)
        if by_id is not None:
            return by_id
        matches = project.mapLayersByName(name_or_id)
        return matches[0] if matches else None


def _jsonable(value):
    """Coerce QVariant/None/geometry-ish values into JSON-friendly forms."""
    if value is None:
        return None
    try:
        # QVariant NULL comes through as a special object in some builds.
        if repr(value) == "NULL":
            return None
    except Exception:
        pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
