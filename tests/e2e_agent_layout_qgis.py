"""Live end-to-end: a real CLI turn designs and saves a layout template.

COSTS ONE MODEL TURN on the user's subscription — run deliberately, never in
CI. Not collected by pytest. Usage (QGIS closed or open, either is fine):

    set QT_QPA_FONTDIR=C:\\Windows\\Fonts
    "%OSGEO4W_ROOT%\\bin\\python-qgis-ltr.bat" tests\\e2e_agent_layout_qgis.py EVIDENCE_DIR [codex|claude] [design|reuse]

Scenarios: ``design`` builds and saves an A3 template; ``reuse`` builds an
A4 report-figure template and then a filled layout from it.

Headless QGIS on an isolated profile hosts the real bridge + executor; the
real backend class spawns the real CLI with QGent's isolation flags. Approval
cards are auto-approved and questions auto-answered with their first option
(both are logged), because nobody is at the keyboard. The template lands in
the isolated profile's composer_templates, never the user's.
"""
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
EVIDENCE = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp())
BACKEND = (sys.argv[2] if len(sys.argv) > 2 else "codex").lower()
SCENARIO = (sys.argv[3] if len(sys.argv) > 3 else "design").lower()
EVIDENCE.mkdir(parents=True, exist_ok=True)
SCRATCH = Path(tempfile.mkdtemp(prefix="qgent-e2e-"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = str(SCRATCH / "profile")
PLUGIN = SCRATCH / "plugins" / "qgis_chat_agent"
shutil.copytree(SOURCE, PLUGIN, ignore=shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "mcp-config.json"))
sys.path.insert(0, str(PLUGIN.parent))

from qgis.PyQt.QtCore import QEventLoop, QSettings, QTimer  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry,
    QgsPointXY, QgsProject, QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas, QgsMessageBar  # noqa: E402

app = QgsApplication([], True)
app.initQgis()
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                  str(SCRATCH / "settings"))

import importlib  # noqa: E402

config = importlib.import_module("qgis_chat_agent.config")
bridge_mod = importlib.import_module("qgis_chat_agent.bridge.qgis_socket_server")
executor_mod = importlib.import_module(
    "qgis_chat_agent.bridge.main_thread_executor")
layout_tools = importlib.import_module("qgis_chat_agent.bridge.layout_tools")
snapshot = importlib.import_module("qgis_chat_agent.context.project_snapshot")
backend_mod = importlib.import_module(
    "qgis_chat_agent.agent." + ("codex_backend" if BACKEND == "codex"
                                else "claude_code_backend"))
detector = importlib.import_module("qgis_chat_agent.agent.question_detect")

config.set(config.K_BACKEND, BACKEND)
config.set(config.K_PERMISSION_MODE, "ask_destructive")

# -- a small, realistic project ---------------------------------------------
project = QgsProject.instance()
project.setCrs(QgsCoordinateReferenceSystem("EPSG:32651"))
site = QgsVectorLayer("Polygon?crs=EPSG:32651&field=name:string",
                      "Project Site Boundary", "memory")
feature = QgsFeature(site.fields())
feature.setAttribute("name", "Site")
feature.setGeometry(QgsGeometry.fromPolygonXY([[
    QgsPointXY(290000, 1620000), QgsPointXY(292500, 1620000),
    QgsPointXY(292500, 1622000), QgsPointXY(290000, 1622000)]]))
site.dataProvider().addFeatures([feature])
site.updateExtents()
hazard = QgsVectorLayer("Polygon?crs=EPSG:32651&field=level:string",
                        "Flood Hazard", "memory")
for level, (x0, y0) in (("High", (289500, 1619500)),
                        ("Moderate", (291000, 1620800))):
    zone = QgsFeature(hazard.fields())
    zone.setAttribute("level", level)
    zone.setGeometry(QgsGeometry.fromPolygonXY([[
        QgsPointXY(x0, y0), QgsPointXY(x0 + 2200, y0),
        QgsPointXY(x0 + 2200, y0 + 1600), QgsPointXY(x0, y0 + 1600)]]))
    hazard.dataProvider().addFeatures([zone])
hazard.updateExtents()
project.addMapLayers([hazard, site])


class Iface:
    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas(self.window)
        self.canvas.setDestinationCrs(project.crs())
        self.canvas.setLayers([site, hazard])
        self.canvas.setExtent(hazard.extent())
        self.bar = QgsMessageBar(self.window)
        self.opened = []

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def activeLayer(self):
        return site

    def layerTreeView(self):
        return None

    def openLayoutDesigner(self, layout):
        self.opened.append(layout.name())


iface = Iface()
log = {"backend": BACKEND, "tool_calls": [], "approvals": [],
       "questions": [], "notes": [], "text": ""}

# -- the real bridge and executor --------------------------------------------
token = secrets.token_hex(16)
executor = executor_mod.MainThreadExecutor(iface)
bridge = bridge_mod.BridgeServer(token)
bridge.execute_requested.connect(executor.handle)


def approve(payload):
    log["approvals"].append({"reasons": payload.get("reasons"),
                             "code": str(payload.get("code"))[:600]})
    payload["approved"] = True
    payload["event"].set()


def answer(payload):
    log["questions"].append({"question": payload.get("question"),
                             "options": list(payload.get("options") or [])})
    choice = (payload.get("options") or ["Use your judgement"])[0]
    QTimer.singleShot(0, lambda: bridge.resolve_question(
        payload["question_id"], "answered", answer=choice))


bridge.approval_requested.connect(approve)
bridge.question_requested.connect(answer)
port = bridge.start()
runtime = PLUGIN / "claude_runtime"
mcp_path = runtime / "mcp-config.json"
mcp_path.write_text(json.dumps({"mcpServers": {"qgis": {
    "command": config.python_executable(),
    "args": [str(PLUGIN / "bridge" / "mcp_stdio_bridge.py")],
    "env": {"QGIS_COPILOT_HOST": bridge.host,
            "QGIS_COPILOT_PORT": str(port),
            "QGIS_COPILOT_TOKEN": token}}}}, indent=1), encoding="utf-8")

cli = config.detect_codex() if BACKEND == "codex" else config.detect_claude()
backend_cls = (backend_mod.CodexBackend if BACKEND == "codex"
               else backend_mod.ClaudeCodeBackend)
backend = backend_cls(cli, str(runtime), str(mcp_path), {})
loop = QEventLoop()
outcome = {}
backend.token.connect(lambda text: log.__setitem__("text", log["text"] + text))
backend.tool_call.connect(lambda name, args: log["tool_calls"].append(
    {"name": name, "args": json.dumps(args, default=str)[:800]}))
backend.status_note.connect(lambda note: log["notes"].append(note))
backend.done.connect(lambda result: (outcome.update(done=True), loop.quit()))
backend.error.connect(lambda message: (outcome.update(error=message),
                                       loop.quit()))

if SCENARIO == "reuse":
    TEMPLATE = "QGent Report Figure A4"
    FILLED = "Figure 3 - Flood Hazard"
    prompt = (
        f"Create a reusable A4 portrait report-figure template named "
        f"'{TEMPLATE}' for my environmental reports, with fill-in fields "
        f"for the map title, figure number, caption and data source. Save "
        f"it to my Layout Manager. Then make a layout from it called "
        f"'{FILLED}' showing the flood hazard layer, filled with: map title "
        f"'Flood Hazard Map', figure number '3', caption 'Flood "
        f"susceptibility at the project site', source 'MGB; field survey'.")
else:
    TEMPLATE = "QGent Flood Hazard A3"
    FILLED = None
    prompt = (
        f"Design a reusable A3 landscape print-layout template for flood "
        f"hazard maps of project sites and save it to my Layout Manager as "
        f"'{TEMPLATE}'. It needs a title block with project, proponent, "
        f"location, prepared by, date and map number fields, a legend, north "
        f"arrow, scale bar and a location inset. Check the preview before "
        f"you finish, then open it in the designer.")
started = time.monotonic()
backend.send(prompt, snapshot.build_context_block(iface))
timeout = QTimer()
timeout.setSingleShot(True)
timeout.timeout.connect(lambda: (outcome.update(error="timeout"),
                                 backend.cancel(), loop.quit()))
timeout.start(20 * 60 * 1000)
loop.exec_()
log["elapsed_s"] = round(time.monotonic() - started, 1)
log["outcome"] = outcome
log["stderr_tail"] = (backend.last_stderr or "")[-1500:]

# -- evidence -----------------------------------------------------------------
template_dir = Path(layout_tools.user_template_dir())
templates = sorted(template_dir.glob("*.qpt")) if template_dir.is_dir() else []
log["templates"] = [{"path": str(path), "size_bytes": path.stat().st_size}
                    for path in templates]
log["layouts"] = [layout.name() for layout in
                  project.layoutManager().printLayouts()]
log["opened_in_designer"] = iface.opened
log["detected_trailing_question"] = detector.detect_question(
    log["text"].strip().split("\n\n")[-1] if log["text"] else "")
tools = layout_tools.LayoutTools(iface)
for name in log["layouts"]:
    summary = tools.info({"action": "describe", "layout": name})
    log.setdefault("described", {})[name] = {
        "pages": summary["pages"], "issues": summary["issues"],
        "placeholders": summary["placeholders"],
        "items": [item["id"] for item in summary["items"]]}
    preview = tools.info({"action": "render", "layout": name})
    kept = EVIDENCE / ("e2e_" + Path(preview["snapshot_path"]).name)
    shutil.copyfile(preview["snapshot_path"], kept)
    log["described"][name]["preview"] = str(kept)
saved = [path for path in templates if path.stem == TEMPLATE]
if saved:
    described = tools.info({"action": "describe", "template": str(saved[0])})
    log["template_placeholders"] = described["placeholders"]
(EVIDENCE / f"e2e_{BACKEND}_{SCENARIO}_layout.json").write_text(
    json.dumps(log, indent=1, default=str), encoding="utf-8")
print(json.dumps({key: log[key] for key in (
    "outcome", "elapsed_s", "templates", "layouts", "opened_in_designer",
    "approvals", "questions", "notes")}, indent=1, default=str))
print("TOOLS:", [call["name"] for call in log["tool_calls"]])
print("FINAL TEXT:", log["text"][-1500:])
bridge.stop()
app.exitQgis()
ok = bool(outcome.get("done") and saved
          and len(log.get("template_placeholders") or []) >= 3)
if FILLED:
    filled = log.get("described", {}).get(FILLED)
    ok = ok and filled is not None and not filled["placeholders"]
    print("FILLED LAYOUT:", json.dumps(filled, default=str))
print("TEMPLATE PLACEHOLDERS:", log.get("template_placeholders"))
print("E2E", "PASSED" if ok else "FAILED")
sys.exit(0 if ok else 1)
