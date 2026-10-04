"""Live QGIS/Qt regression check using real backends and local CLI fixtures.

Linux: python3 tests/verify_work_status_qgis.py EVIDENCE_DIR
Also callable as main(iface, evidence) from QGIS's --code launcher. Uses an
isolated profile, plugin copy and subprocesses; no model requests or API keys.
"""
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from unittest.mock import patch


def main(iface=None, evidence=None):
    source = Path(__file__).resolve().parents[1]
    evidence = Path(evidence or tempfile.mkdtemp(prefix="qgent-status-evidence-"))
    evidence.mkdir(parents=True, exist_ok=True)
    scratch = tempfile.TemporaryDirectory(prefix="qgent-status-")
    root = Path(scratch.name)
    os.environ["TEMP"] = os.environ["TMP"] = str(root)
    standalone = iface is None
    if standalone:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ["QGIS_CUSTOM_CONFIG_PATH"] = str(root / "profile")

    from qgis.PyQt.QtCore import QCoreApplication, QSettings, Qt
    from qgis.PyQt.QtGui import QColor, QPalette
    from qgis.PyQt.QtTest import QTest
    from qgis.PyQt.QtWidgets import QMainWindow
    from qgis.core import Qgis, QgsApplication
    from qgis.gui import QgsMapCanvas, QgsMessageBar

    app = QgsApplication([], True) if standalone else QgsApplication.instance()
    if standalone:
        app.initQgis()
    else:
        assert "qgent-status-" in QgsApplication.qgisSettingsDirPath(), (
            "Run desktop checks with a disposable --profiles-path qgent-status-* folder")
    QCoreApplication.setOrganizationName("QGentStatusTest")
    QCoreApplication.setApplicationName("IsolatedStatus")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(root))
    assert str(QSettings().fileName()).startswith(str(root))

    plugin = root / "plugins" / "qgent_status_test"
    shutil.copytree(source, plugin, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", ".pytest_cache", "mcp-config.json"))
    sys.path.insert(0, str(plugin.parent))
    config = importlib.import_module("qgent_status_test.config")
    chat = importlib.import_module("qgent_status_test.ui.chat_dock")
    config.set(config.K_BACKEND, "claude")  # no global Codex config writes
    config.set(config.K_REDUCE_MOTION, False)
    config.set(config.K_CHECK_UPDATES, False)
    config.set(config.K_SHOW_MAP_SNAPSHOTS, False)

    fixture = root / "fixture-cli"
    fixture.write_text("#!" + shutil.which("python3") + "\n" + '''
import json, os, sys, time
sys.stdin.read()
mode = os.environ.get("QGENT_TEST_MODE", "finish")
claude = "--output-format" in sys.argv
def emit(event):
    print(json.dumps(event), flush=True)
emit({"type": "system", "session_id": "fixture-session"} if claude else
     {"type": "thread.started", "thread_id": "fixture-session"})
if mode == "empty":
    sys.exit(0)
if mode in ("failure", "stream_error"):
    emit({"type": "result", "is_error": True, "result": "Fixture failure"}
         if claude else {"type": "turn.failed", "error": {"message": "Fixture failure"}}
         if mode == "failure" else {"type": "error", "message": "Fixture failure"})
    sys.exit(0)
if mode == "recovered" and not claude:
    emit({"type": "error", "message": "Reconnecting..."})
text = ("Should I export it as PDF or PNG?" if mode == "question" else
        "The project is ready. I am checking the selected layers.")
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
     if claude else {"type": "item.completed", "item": {"type": "agent_message", "text": text}})
time.sleep(30 if mode == "wait" else 0.3)
emit({"type": "result", "is_error": False, "result": text} if claude else
     {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 8}})
time.sleep(0.3)
sys.exit(1 if mode == "exit_error" else 0)
''', encoding="utf-8")
    fixture.chmod(0o700)
    config.set(config.K_CLAUDE_PATH, str(fixture))
    config.set(config.K_CODEX_PATH, str(fixture))

    if standalone:
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

        iface = Iface()

    results = {"qgis": Qgis.QGIS_VERSION, "desktop": not standalone, "checks": []}

    def check(name, condition):
        assert condition, name
        results["checks"].append(name)
        print("PASS", name, flush=True)

    def until(predicate, timeout=5000):
        deadline = time.monotonic() + timeout / 1000
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(20)
        assert predicate(), "Timed out waiting for live Qt/process transition"

    dock = chat.ChatDock(iface, str(plugin))
    iface.mainWindow().addDockWidget(Qt.RightDockWidgetArea, dock)
    iface.mainWindow().resize(1200 if not standalone else 520, 850)
    iface.mainWindow().show()
    dock.show()

    def capture(name):
        QTest.qWait(220)
        assert dock.grab().save(str(evidence / (name + ".png")))

    def state(expected):
        check("state_" + expected, dock.work_icon.state == expected)
        check("label_" + expected,
              bool(dock.work_label.text()) and dock.work_label.isVisible())
        check("animation_" + expected,
              (dock.work_icon._anim.state() == dock.work_icon._anim.Running)
              == (expected == "working" and not config.get(config.K_REDUCE_MOTION)))

    def start(mode="wait"):
        dock.backend.env["QGENT_TEST_MODE"] = mode
        dock.input.setPlainText("Check the project and selected layers.")
        QTest.mouseClick(dock.action_btn, Qt.LeftButton)

    def stop():
        QTest.mouseClick(dock.stop_turn_btn, Qt.LeftButton)
        until(lambda: not dock.backend.is_busy())
        QTest.qWait(30)
        state("stopped")

    try:
        QTest.qWait(100)
        state("idle")
        for backend in ("claude", "codex"):
            config.set(config.K_BACKEND, backend)
            # Rebuild only: generated JSON is backend-neutral and this avoids
            # the separate legacy global Codex TOML writer entirely.
            dock._build_backend(preserve_session=False)
            start()
            until(lambda: dock._current_bubble is not None)
            state("working")
            check(backend + "_stop_visible", dock.stop_turn_btn.isVisible())
            angle = dock.work_icon._angle
            QTest.qWait(130)
            check(backend + "_spinner_rotates", dock.work_icon._angle != angle)
            dock._on_tool_call("mcp__qgis__get_project_context", {})
            dock._on_subagent_event("data-scout", "started")
            state("working")
            capture(backend + "-working")
            stop()
            check(backend + "_tool_stops", dock._last_tool_chip.spinner.isHidden())
            check(backend + "_subagent_stops",
                  dock._subagent_chips["data-scout"]._shimmer_pos is None)
            before = dock.msg_layout.count()
            dock._on_token("late token")
            dock._on_tool_call("late_tool", {})
            dock._on_tool_result("late result")
            dock._on_subagent_event("late-agent", "started")
            dock._on_bridge_activity("late_activity")
            dock._on_done({})
            dock._on_busy_changed(False)
            state("stopped")
            check(backend + "_late_events_ignored", dock.msg_layout.count() == before)
            capture(backend + "-stopped")

            start("finish")
            until(lambda: dock._active_turn is None)
            state("done")
            check(backend + "_stop_hidden", dock.stop_turn_btn.isHidden())
            capture(backend + "-done")

            for mode in ("failure", "empty", "stream_error", "exit_error"):
                start(mode)
                until(lambda: dock._active_turn is None)
                state("error")
                check(backend + "_" + mode, not dock.backend.is_busy())
            capture(backend + "-error")
            if backend == "codex":
                start("recovered")
                until(lambda: dock._active_turn is None)
                state("done")
                check("codex_recovered_error_is_done", not dock.backend.is_busy())

            # A real bridge handler blocks on a question until the user clicks.
            start()
            until(lambda: dock._current_bubble is not None)
            answers = []
            worker = threading.Thread(target=lambda: answers.append(
                dock.bridge._ask_user("Which format?", ("PDF", "PNG"), True)))
            worker.start()
            until(lambda: bool(dock._live_questions))
            state("question")
            check(backend + "_question_no_stale_activity", not dock.status.text())
            capture(backend + "-question")
            card = next(iter(dock._live_questions.values()))["card"]
            check(backend + "_question_choices_visible",
                  not card._option_buttons["PDF"].visibleRegion().isEmpty())
            QTest.mouseClick(card._option_buttons["PDF"], Qt.LeftButton)
            until(lambda: bool(answers))
            worker.join(timeout=1)
            state("working")
            check(backend + "_answer_resumes", answers == ["USER ANSWERED: PDF"])

            approval = {"approval_id": "test-approval", "code": "layer.commitChanges()",
                        "reasons": ["Save layer edits"], "event": threading.Event()}
            dock.on_approval_requested(approval)
            state("approval")
            dock._live_approvals["test-approval"]["card"].decided.emit(False)
            state("working")
            check(backend + "_approval_resolved", approval["event"].is_set())
            stop()

            # Stopping while a question blocks must also release the handler.
            start()
            answers.clear()
            worker = threading.Thread(target=lambda: answers.append(
                dock.bridge._ask_user("Which layer?", ("roads", "rivers"), True)))
            worker.start()
            until(lambda: bool(dock._live_questions))
            stop()
            until(lambda: bool(answers))
            worker.join(timeout=1)
            check(backend + "_stop_cancels_question",
                  answers == ["CANCELLED"] and not dock._live_questions)

            start("question")
            until(lambda: dock._active_turn is None)
            state("question")
            stop()
            check(backend + "_stop_cancels_detected", not dock._detected_questions)

        start()
        until(lambda: dock._current_bubble is not None)
        dock._build_backend()
        check("backend_replacement_cleans_turn", dock._active_turn is None)
        state("stopped")
        check("backend_replacement_restores_send",
              dock.action_btn.text() == "Send" and not dock.action_btn.is_busy_state())

        # Reduced motion, hidden dock and new-session cleanup.
        config.set(config.K_REDUCE_MOTION, True)
        start()
        state("working")
        stop()
        config.set(config.K_REDUCE_MOTION, False)
        start()
        dock.hide()
        check("hidden_spinner_stops", dock.work_icon._anim.state() == 0)
        dock.show()
        state("working")
        stop()

        dock.backend.cli_path = ""
        start()
        state("error")
        dock.backend.cli_path = str(fixture)
        with patch.object(dock.backend, "send", side_effect=RuntimeError("send failed")):
            start()
            state("error")
            check("send_exception_cleans_turn", dock._active_turn is None)

        # Keep new_session from invoking the unrelated legacy TOML writer.
        config.set(config.K_BACKEND, "claude")
        dock.new_session()
        state("idle")
        check("new_session_hides_stop", dock.stop_turn_btn.isHidden())

        # Existing queue controls, including a failure followed by success.
        dock._choose_queue_policy = lambda *_: ("pause", False)
        dock.backend.env["QGENT_TEST_MODE"] = "finish"
        dock._enqueue_text("First queued task")
        dock._enqueue_text("Second queued task")
        dock.run_queue()
        until(lambda: dock._active_turn is not None)
        dock._set_queue_pause(True)
        until(lambda: dock._active_turn is None)
        QTest.qWait(50)
        state("paused")
        dock._set_queue_pause(False)
        until(lambda: not dock._queue_running)
        state("done")
        dock.backend.env["QGENT_TEST_MODE"] = "failure"
        dock._enqueue_text("Failed queued task")
        dock.run_queue()
        until(lambda: not dock._queue_running)
        state("error")
        dock.backend.env["QGENT_TEST_MODE"] = "wait"
        dock._enqueue_text("Cancelled queued task")
        dock.run_queue()
        until(lambda: dock._active_turn is not None)
        QTest.mouseClick(dock.queue_panel.stop_all_btn, Qt.LeftButton)
        until(lambda: not dock._queue_running)
        state("stopped")

        # Visual review in a dark palette at the narrow dock width.
        palette = QPalette(app.palette())
        palette.setColor(QPalette.Window, QColor("#1B1D21"))
        palette.setColor(QPalette.WindowText, QColor("#E8EAED"))
        app.setPalette(palette)
        dark = chat.ChatDock(iface, str(plugin))
        dark._clear_messages()
        dark._add_assistant_message("Project checks are complete.", persist=False)
        dark.setFloating(True)
        dark.resize(440, 740)
        dark.show()
        for value in ("working", "done", "question", "stopped", "error"):
            dark._set_work_state(value)
            QTest.qWait(100)
            assert dark.grab().save(str(evidence / ("dark-" + value + ".png")))
            check("dark_layout_" + value,
                  dark.work_label.width() >= dark.work_label.sizeHint().width()
                  and dark.work_icon.width() == 20)
            if value == "working":
                check("stop_does_not_crowd_composer",
                      dark.input.width() >= 160 and dark.width() <= 450)
        dark.shutdown()
        dark.close()
        dark.deleteLater()
        if not standalone:
            check("desktop_screenshot", iface.mainWindow().grab().save(
                str(evidence / "qgis-desktop.png")))
        check("no_active_backend", not dock.backend.is_busy())
        (evidence / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print("ALL WORK STATUS CHECKS PASSED:", len(results["checks"]), evidence, flush=True)
    finally:
        dock.shutdown()
        dock.close()
        dock.deleteLater()
        QTest.qWait(50)
        if standalone:
            from qgis.PyQt import sip
            # QgsMapCanvas must be destroyed before its application/providers.
            iface.mainWindow().close()
            sip.delete(iface.mainWindow())
            app.exitQgis()
        scratch.cleanup()


if __name__ == "__main__":
    main(evidence=sys.argv[1] if len(sys.argv) > 1 else None)
