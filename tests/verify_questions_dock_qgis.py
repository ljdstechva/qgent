"""Real-widget check of question detection and layout previews in the dock.

Run with the QGIS Python (not collected by pytest):

    "%OSGEO4W_ROOT%\\bin\\python-qgis-ltr.bat" tests\\verify_questions_dock_qgis.py EVIDENCE_DIR

Builds the real ChatDock on an isolated QGIS profile, a copy of the plugin,
and a scratch TEMP (so the benchmark perf CSV is never touched). A fake
backend stands in for the CLI; turns are driven through the same signal
handlers the real backends call. No model, network, or desktop QGIS is used.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
EVIDENCE = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp())
EVIDENCE.mkdir(parents=True, exist_ok=True)
SCRATCH = Path(tempfile.mkdtemp(prefix="qgent-questions-"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(
    os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = str(SCRATCH / "profile")
os.environ["TEMP"] = os.environ["TMP"] = str(SCRATCH / "temp")
(SCRATCH / "temp").mkdir()

PLUGIN = SCRATCH / "plugins" / "qgis_chat_agent"
shutil.copytree(SOURCE, PLUGIN, ignore=shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "mcp-config.json"))
sys.path.insert(0, str(PLUGIN.parent))

from qgis.PyQt.QtCore import QSettings, Qt  # noqa: E402
from qgis.PyQt.QtGui import QColor, QImage  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QMainWindow  # noqa: E402
from qgis.core import QgsApplication  # noqa: E402
from qgis.gui import QgsMapCanvas, QgsMessageBar  # noqa: E402

app = QgsApplication([], True)
app.initQgis()
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                  str(SCRATCH / "settings"))

import importlib  # noqa: E402

chat_dock = importlib.import_module("qgis_chat_agent.ui.chat_dock")
widgets = importlib.import_module("qgis_chat_agent.ui.widgets")
export = importlib.import_module("qgis_chat_agent.export")


class Iface:
    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas(self.window)
        self.bar = QgsMessageBar(self.window)

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def layerTreeView(self):
        return None

    def activeLayer(self):
        return None


class FakeBackend:
    """Accepts turns and lets the test emit the CLI's signals itself."""

    cli_path = "fake-cli"
    session_id = "session-1"

    def __init__(self):
        self.sent = []

    def is_busy(self):
        return False

    def send(self, text, context_block, fast_mode=False):
        self.sent.append(text)

    def cancel(self):
        pass


results = {}


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"{name} FAILED {detail}")
    results[name] = detail or True
    print("PASS", name, detail)


def new_dock():
    iface = Iface()
    dock = chat_dock.ChatDock(iface, str(PLUGIN))
    # As plugin.py does: docked into a visible main window.
    iface.window.addDockWidget(Qt.RightDockWidgetArea, dock)
    iface.window.show()
    dock.backend = FakeBackend()
    return dock


def run_turn(dock, user_text, reply_parts, tool=None):
    assert dock._start_turn(user_text)
    for part in reply_parts:
        if isinstance(part, tuple):
            dock._on_tool_call(part[0], part[1])
            dock._on_tool_result(part[2])
        else:
            dock._on_token(part)
    dock._on_done({"result": ""})
    app.processEvents()


def cards(dock):
    return [widget for widget in dock.findChildren(widgets.QuestionCard)
            if widget.detected]


dock = new_dock()

# 1. A trailing prose question becomes an answerable card with choices.
run_turn(dock, "Buffer the site by 100 m", [
    "Created site_buffer (1 feature, EPSG:32651).\n\n",
    "Should I export it as PDF or PNG?"])
open_ids = list(dock._detected_questions)
check("detected_card_shown", len(open_ids) == 1, open_ids)
card = dock._detected_questions[open_ids[0]]["card"]
check("detected_card_options", card.options == ("PDF", "PNG"), card.options)
check("detected_card_header",
      card.head.text() == "QGent is waiting for your reply", card.head.text())

# 2. Clicking an option sends it as the next message and freezes the card.
sent_before = len(dock.backend.sent)
card.answered.emit("PNG", "option")
app.processEvents()
check("click_sends_reply", dock.backend.sent[sent_before:] == ["PNG"],
      dock.backend.sent[sent_before:])
check("click_freezes_card", card.outcome() == "answered"
      and card.answer() == "PNG" and not dock._detected_questions)
dock._on_done({"result": ""})

# 3. Text before a tool call is not the final word; no false card.
run_turn(dock, "Style it", [
    "Which colour should I use? Let me check the layer first.",
    ("mcp__qgis__execute_pyqgis", {"code": "print(1)"}, "1"),
    "Styled site_buffer in red."])
check("question_before_tool_ignored", not dock._detected_questions)

# 4. Typing in the composer answers an open question.
run_turn(dock, "Make a layout", [
    "Which page size do you want?\n\n- A4 landscape\n- A3 landscape"])
check("list_options_detected",
      next(iter(dock._detected_questions.values()))["options"]
      == ("A4 landscape", "A3 landscape"))
typed_card = next(iter(dock._detected_questions.values()))["card"]
dock.input.setPlainText("A3 landscape, please")
dock.on_send()
app.processEvents()
check("typing_freezes_card", typed_card.outcome() == "answered"
      and typed_card.head.text() == "Replied in chat")
check("typed_reply_sent", dock.backend.sent[-1] == "A3 landscape, please")
dock._on_done({"result": ""})

# 5. A layout_info render result shows an inline preview card.
preview = SCRATCH / "temp" / "qgent_layout_Test_p1.png"
image = QImage(400, 280, QImage.Format_ARGB32)
image.fill(QColor("#336699"))
image.save(str(preview), "PNG")
run_turn(dock, "Preview the layout", [
    ("mcp__qgis__layout_info", {"action": "render", "layout": "Test"},
     json.dumps({"snapshot_path": str(preview), "layout": "Test",
                 "page": 1})),
    "Here is the preview."])
snapshot_cards = dock.findChildren(widgets.SnapshotCard)
captions = [card.findChild(widgets.QLabel).text() if card.findChild(
    widgets.QLabel) else "" for card in snapshot_cards]
history_text = Path(dock.history_store.path).read_text(encoding="utf-8")
check("layout_preview_card", any(
    "Layout preview — Test" in line for line in history_text.splitlines()),
    [c for c in captions if c])

# 6. Attention. QGIS in front: the dock is raised, nothing else nags.
dock.hide()
QApplication.setActiveWindow(dock.iface.window)
app.processEvents()
foreground = dock._notify_question_attention("Which layer?")
check("attention_shows_dock", dock.isVisible())
check("attention_quiet_in_foreground", foreground == [], foreground)
# QGIS behind another application: the question reaches the user anyway.
other_app = QMainWindow()
other_app.show()
QApplication.setActiveWindow(other_app)
app.processEvents()
background = dock._notify_question_attention("Which layer?")
check("attention_channels_in_background", "message_bar" in background,
      background)
QApplication.setActiveWindow(dock.iface.window)
other_app.close()

# 7. Structured ask_user outside a queue also raises attention.
dock.hide()
payload = {"question_id": "question-x-1", "question": "Which layer?",
           "options": ("roads", "rivers"), "allow_other": True,
           "state": "pending"}
dock.bridge.cancel_pending_questions("test")  # nothing pending; harmless
dock.on_question_requested(payload)
check("structured_question_raises_dock", dock.isVisible())
dock._finalize_question("question-x-1", "cancelled", reason="test over")

# 8. History: a conversation that ends on a question restores it live.
run_turn(dock, "Export it", ["Done. Would you like a PNG copy as well?"])
history_path = Path(dock.history_store.path)
records = [json.loads(line) for line in
           history_path.read_text(encoding="utf-8").splitlines() if line]
detected_records = [record for record in records
                    if record.get("kind") == "question"
                    and record.get("source") == "detected"]
check("history_records_detected",
      [record["event"] for record in detected_records]
      == ["requested", "answered", "requested", "answered", "requested"],
      [record["event"] for record in detected_records])
dock.shutdown()
dock.deleteLater()
app.processEvents()

restored = new_dock()
restored_cards = cards(restored)
check("restore_rebuilds_cards", len(restored_cards) == 3,
      len(restored_cards))
check("restore_keeps_last_live",
      len(restored._detected_questions) == 1
      and not next(iter(restored._detected_questions.values()))[
          "card"]._terminal)
check("restore_freezes_answered", all(
    card._terminal for card in restored_cards
    if card not in [item["card"] for item in
                    restored._detected_questions.values()]))
live = next(iter(restored._detected_questions.values()))["card"]
live.answered.emit("Yes", "option")
app.processEvents()
check("restored_card_answers", restored.backend.sent == ["Yes"],
      restored.backend.sent)

# 9. Export renders detected questions as QGent's questions.
markdown = export.render_markdown(
    export.read_history_jsonl(str(history_path))["records"],
    {"project": "Test"})
check("export_labels_detected",
      "Question from QGent - Replied" in markdown
      and "Reply: PNG" in markdown)

restored.shutdown()
(EVIDENCE / "questions_dock_evidence.json").write_text(
    json.dumps(results, indent=1, default=str), encoding="utf-8")
print("ALL DOCK QUESTION CHECKS PASSED ->", EVIDENCE)
app.exitQgis()
