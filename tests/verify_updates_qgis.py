"""Isolated Qt/QGIS integration check; run with python-qgis-ltr.bat.

Uses real widgets, an isolated settings profile, and fixture update sources.
Does not launch the QGIS desktop, real CLIs, or a chat/backend session.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qgent-updates-ui-") as temporary:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["QGIS_CUSTOM_CONFIG_PATH"] = temporary
        from qgis.PyQt.QtCore import QCoreApplication, QEventLoop, QSettings, QTimer
        from qgis.PyQt.QtWidgets import QMainWindow
        from qgis.PyQt.QtGui import QFont, QFontDatabase
        from qgis.core import Qgis, QgsApplication
        from qgis.gui import QgsMessageBar

        app = QgsApplication([], True)
        app.initQgis()
        for font_path in (Path(os.environ["WINDIR"]) / "Fonts").glob("segoeui*.ttf"):
            assert QFontDatabase.addApplicationFont(str(font_path)) >= 0
        app.setFont(QFont("Segoe UI", 9))
        QCoreApplication.setOrganizationName("QGentUpdateTest")
        QCoreApplication.setApplicationName("IsolatedUpdates")
        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, temporary)
        assert str(QSettings().fileName()).startswith(temporary.replace("\\", "/"))
        sys.path.insert(0, str(args.plugin_root.resolve().parent))
        import importlib
        package = args.plugin_root.name
        config = importlib.import_module(package + ".config")
        core = importlib.import_module(package + ".update_check")
        ui = importlib.import_module(package + ".ui.updates")
        plugin_module = importlib.import_module(package + ".plugin")
        settings = importlib.import_module(package + ".ui.settings_dialog")
        assertions = {}

        def verify(name, value):
            assert value, name
            assertions[name] = True

        def drain(manager):
            loop = QEventLoop()
            expired = []
            timeout = QTimer()
            timeout.setSingleShot(True)
            timeout.timeout.connect(lambda: (expired.append(True), loop.quit()))
            def changed():
                if manager.worker is None:
                    loop.quit()
            manager.changed.connect(changed)
            timeout.start(8000)
            if manager.worker is not None:
                loop.exec_()
            timeout.stop()
            manager.changed.disconnect(changed)
            assert not expired, "Worker did not finish"
            app.processEvents()

        class Iface:
            def __init__(self):
                self.window = QMainWindow()
                self.bar = QgsMessageBar(self.window)
                self.actions = []
            def mainWindow(self):
                return self.window
            def messageBar(self):
                return self.bar
            def addToolBarIcon(self, action):
                self.actions.append(action)
            def addPluginToMenu(self, _menu, action):
                self.actions.append(action)
            def removeToolBarIcon(self, action):
                self.actions.remove(action)
            def removePluginMenu(self, _menu, action):
                self.actions.remove(action)

        iface = Iface()
        plugin = plugin_module.QgisCopilotPlugin(iface)
        plugin.initGui()
        manager = plugin.updates
        manager.profile_dir = temporary
        manager.startup.stop()
        report = {
            "checked_at": 1791108000, "cached": False,
            "releases": {
                "qgent": {"installed": "0.3.0", "latest": "0.4.0", "update": True, "error": ""},
                "claude": {"installed": "2.1.287", "latest": "v2.1.289", "update": True, "error": ""},
                "codex": {"installed": "0.160.0", "latest": "rust-v0.160.0", "update": False, "error": ""}},
            "models": {
                "claude": {"ids": [], "source": "installed CLI strings (unconfirmed)"},
                "codex": {"ids": ["gpt-6-astra", "gpt-6.1-sol"], "source": "Codex model catalog"}},
            "errors": [],
        }
        report["notices"] = core.notice_ids(report)
        before_models = {name: {role: config.get_model_choice(name, role) for role in config.MODEL_ROLES}
                         for name in ("claude", "codex")}
        with patch.object(settings.SettingsDialog, "_init_doctor", lambda self: None):
            dialog = settings.SettingsDialog(doctor_context={"update_manager": manager})
        dialog.tabs.setCurrentWidget(dialog.updates)
        dialog.resize(900, 720)
        dialog.show()
        app.processEvents()
        verify("updates_tab_present", dialog.tabs.tabText(dialog.tabs.currentIndex()) == "Updates")
        verify("automatic_enabled_by_default", dialog.updates.automatic.isChecked())
        verify("manual_menu_registered", plugin.update_action in iface.actions)
        verify("empty_state", "No update check yet" in dialog.updates.details.toPlainText())

        gate = threading.Event()
        worker_threads = []
        def fixture_check(*_args, **_kwargs):
            worker_threads.append(threading.get_ident())
            assert gate.wait(4)
            return json.loads(json.dumps(report))

        ticks = []
        heartbeat = QTimer()
        heartbeat.timeout.connect(lambda: ticks.append(True))
        heartbeat.start(10)
        with patch.object(ui, "check_updates", fixture_check), patch.object(config, "detect_claude", return_value=""), patch.object(config, "detect_codex", return_value=""):
            dialog.updates.check_button.click()
            running = manager.worker
            manager.check(force=True)
            verify("concurrent_checks_coalesced", manager.worker is running)
            verify("loading_state", not dialog.updates.check_button.isEnabled())
            QTimer.singleShot(150, gate.set)
            drain(manager)
        heartbeat.stop()
        verify("background_worker_not_ui_thread", worker_threads[0] != threading.get_ident())
        verify("ui_remains_responsive", len(ticks) >= 2)
        verify("completion_enables_manual_check", dialog.updates.check_button.isEnabled())
        verify("new_models_rendered", "gpt-6.1-sol" in dialog.updates.details.toPlainText())
        verify("notification_visible", plugin._update_notice is not None)
        verify("model_choices_unchanged", before_models == {
            name: {role: config.get_model_choice(name, role) for role in config.MODEL_ROLES}
            for name in ("claude", "codex")})
        dialog.grab().save(str(args.evidence / "updates-success.png"))

        # A replacement notification must not be cleared by the old widget's destruction.
        previous = plugin._update_notice
        report["models"]["codex"]["ids"].append("gpt-7-sol")
        report["notices"] = core.notice_ids(report)
        manager._complete(json.loads(json.dumps(report)))
        app.processEvents()
        verify("replacement_notice_retained", plugin._update_notice is not None and plugin._update_notice is not previous)
        dialog.updates.dismiss_button.click()
        verify("dismissal_saved", bool(core.load_state(temporary).get("dismissed")))
        verify("dismissal_removes_banner", plugin._update_notice is None)
        verify("dismissal_keeps_details", "gpt-6.1-sol" in dialog.updates.details.toPlainText())

        incomplete = json.loads(json.dumps(report))
        incomplete["errors"] = ["Codex CLI: Upstream check failed: TimeoutError. Check your connection or try later."]
        incomplete["releases"]["codex"]["error"] = incomplete["errors"][0]
        manager._complete(incomplete)
        manager._failed("Update check failed: TimeoutError. Try again.")
        verify("errors_visible", "TimeoutError" in dialog.updates.status.text() and "Check incomplete" in dialog.updates.details.toPlainText())
        dialog.grab().save(str(args.evidence / "updates-error.png"))
        dialog.updates.automatic.setChecked(False)
        dialog._save_and_accept()
        verify("automatic_preference_persists", not config.get(config.K_CHECK_UPDATES))
        with patch.object(manager, "check") as check:
            manager.check_automatic()
            verify("disabled_prevents_automatic_check", not check.called)
        with patch.object(settings.SettingsDialog, "_init_doctor", lambda self: None):
            reopened = settings.SettingsDialog(doctor_context={"update_manager": manager})
        verify("reopened_preference_preserved", not reopened.updates.automatic.isChecked())
        reopened.updates.automatic.setChecked(True)
        reopened.reject()
        verify("cancel_does_not_save_preference", not config.get(config.K_CHECK_UPDATES))
        reopened.shutdown()
        reopened.deleteLater()
        dialog.shutdown()
        dialog.deleteLater()

        # Exercise the menu route without constructing a real chat/backend.
        class Dock:
            def __init__(self):
                self.opened_updates = False
            def setVisible(self, _visible):
                pass
            def raise_(self):
                pass
            def focus_input(self):
                pass
            def open_update_settings(self):
                self.opened_updates = True
        dock = Dock()
        plugin.dock = dock
        with patch.object(manager, "check") as check:
            plugin.update_action.trigger()
            verify("menu_routes_to_updates_and_forces_check", dock.opened_updates and check.call_args.kwargs == {"force": True})
        plugin.dock = None

        # Unload while work is pending: cancellation must not destroy a live QThread.
        def cancellable(*_args, cancelled, **_kwargs):
            while not cancelled():
                threading.Event().wait(0.01)
            return None
        with patch.object(ui, "check_updates", cancellable), patch.object(config, "detect_claude", return_value=""), patch.object(config, "detect_codex", return_value=""):
            manager.check(force=True)
            plugin.unload()
        app.processEvents()
        verify("unload_stops_worker_and_timers", manager.worker is None and not manager.timer.isActive() and not manager.startup.isActive())
        verify("unload_removes_actions", iface.actions == [])
        payload = {"verdict": "PASS", "qgis": Qgis.QGIS_VERSION, "plugin_root": str(args.plugin_root),
                   "assertions": assertions, "scope": "Isolated offscreen widgets; QGIS desktop confirmation is separate."}
        (args.evidence / "summary.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2))
        iface.window.close()
        app.processEvents()
        app.exitQgis()


if __name__ == "__main__":
    main()
