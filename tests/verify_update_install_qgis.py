"""Windows/QGIS offscreen install + real unload/load checks in a disposable profile.

Run with python-qgis-ltr.bat; only download transport is replaced by fixture ZIPs.
No desktop session, CLI inference, user settings or installed files are touched.
"""
import argparse
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qgent-install-test-") as temporary:
        root = Path(temporary)
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["QGIS_CUSTOM_CONFIG_PATH"] = str(root / "profile")
        os.environ["TEMP"] = os.environ["TMP"] = str(root)
        from qgis.PyQt.QtCore import QCoreApplication, QObject, QSettings, QTimer
        from qgis.PyQt.QtGui import QFont, QFontDatabase
        from qgis.PyQt.QtTest import QTest
        from qgis.PyQt.QtWidgets import QMainWindow
        from qgis.core import Qgis, QgsApplication, QgsProject, QgsVectorLayer
        from qgis.gui import QgsMapCanvas, QgsMessageBar
        from qgis import utils

        app = QgsApplication([], True)
        app.initQgis()
        profile = Path(QgsApplication.qgisSettingsDirPath()).resolve()
        assert profile.is_relative_to(root)
        QCoreApplication.setOrganizationName("QGentInstallerTest")
        QCoreApplication.setApplicationName("IsolatedInstaller")
        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(root))
        assert Path(QSettings().fileName()).is_relative_to(root)
        for font in (Path(os.environ["WINDIR"]) / "Fonts").glob("segoeui*.ttf"):
            QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Segoe UI", 9))
        package = "qgent_install_test"
        plugin_dir = profile / "python" / "plugins" / package
        shutil.copytree(args.plugin_root, plugin_dir, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "mcp-config.json"))
        metadata = (plugin_dir / "metadata.txt").read_text(encoding="utf-8")
        (plugin_dir / "metadata.txt").write_text(metadata.replace("version=0.5.0", "version=0.4.1"), encoding="utf-8")
        sys.path.insert(0, str(plugin_dir.parent))
        utils.plugin_paths = [str(plugin_dir.parent)]
        utils.updateAvailablePlugins()
        assert utils.loadPlugin(package)
        config = importlib.import_module(package + ".config")
        config.set(config.K_BACKEND, "claude")
        config.set(config.K_CLAUDE_PATH, sys.executable)
        config.set(config.K_CHECK_UPDATES, False)
        config.set(config.K_REDUCE_MOTION, True)

        class Iface:
            def __init__(self):
                self.window = QMainWindow()
                self.canvas = QgsMapCanvas(self.window)
                self.bar = QgsMessageBar(self.window)
                self.window.setCentralWidget(self.canvas)
                self.actions = []
            def mainWindow(self): return self.window
            def mapCanvas(self): return self.canvas
            def messageBar(self): return self.bar
            def layerTreeView(self): return None
            def activeLayer(self): return None
            def addToolBarIcon(self, action): self.actions.append(action)
            def removeToolBarIcon(self, action): self.actions.remove(action)
            def addPluginToMenu(self, _menu, action): self.actions.append(action)
            def removePluginMenu(self, _menu, action): self.actions.remove(action)
            def addDockWidget(self, area, dock): self.window.addDockWidget(area, dock)
            def removeDockWidget(self, dock): self.window.removeDockWidget(dock)

        iface = Iface()
        utils.iface = iface
        assert utils.startPlugin(package)
        original = utils.plugins[package]
        original.toggle_panel(True)
        iface.window.resize(1100, 820)
        iface.window.show()
        project = QgsProject.instance()
        layer = QgsVectorLayer("Point?crs=EPSG:4326", "Unsaved project layer", "memory")
        project.addMapLayer(layer)
        project.setDirty(True)
        original.dock._history_append("user", text="Retain this conversation across updates")
        history = original.dock.history_store.path
        history_before = history.read_bytes()
        assertions = {}

        def verify(name, value):
            assert value, name
            assertions[name] = True
            print("PASS", name, flush=True)

        def until(predicate, seconds=12):
            deadline = time.monotonic() + seconds
            while not predicate() and time.monotonic() < deadline:
                QTest.qWait(20)
            assert predicate(), "Timed out waiting for update transition"

        # No running work, drafts or queued tasks may be discarded by a restart.
        dock = original.dock
        for attribute, value in (("_active_turn", {}), ("_queue_running", True),
                                 ("_queue_tasks", [{"status": "queued"}]),
                                 ("_attached_files", [{"path": "fixture"}])):
            before = getattr(dock, attribute)
            setattr(dock, attribute, value)
            original._install_update("0.5.0")
            verify("blocks_" + attribute, original.updates.worker is None and bool(original.updates.error))
            setattr(dock, attribute, before)
        dock.input.setPlainText("unsent draft")
        verify("draft_is_protected", bool(original._update_blocker()))
        dock.input.clear()

        sha = "b" * 40
        def archive(version, broken=False):
            stream = io.BytesIO()
            with ZipFile(stream, "w") as output:
                for path in args.plugin_root.rglob("*"):
                    relative = path.relative_to(args.plugin_root)
                    if not path.is_file() or set(relative.parts) & {".git", "__pycache__", ".pytest_cache", ".ruff_cache"}:
                        continue
                    if relative.as_posix() == "claude_runtime/mcp-config.json":
                        continue
                    data = path.read_bytes()
                    if relative.as_posix() == "metadata.txt":
                        data = metadata.replace("version=0.5.0", "version=" + version).encode()
                    if relative.as_posix() == "plugin.py":
                        data += b"\nUPDATE_TEST_COMMIT = 'new module loaded'\n"
                        if broken:
                            # Fail AFTER registration/timers, not before any side effects.
                            data = data.replace(b"        self.updates.start()", b"        self.updates.start()\n        raise RuntimeError('fixture startup failure')")
                    output.writestr("qgent-" + sha + "/" + relative.as_posix(), data)
            return stream.getvalue()

        def report(version):
            return {"checked_at": time.time(), "cached": False,
                    "releases": {"qgent": {"installed": "0.4.1", "latest": version, "update": True, "error": ""}},
                    "models": {}, "errors": [], "notices": []}

        payload = archive("0.5.0")
        installer = importlib.import_module(package + ".update_install")
        ui = importlib.import_module(package + ".ui.updates")
        prepare = installer.prepare_update
        download_threads = []
        def fetched(url, _limit, cancelled):
            download_threads.append(threading.get_ident())
            if url == installer.COMMIT_URL:
                return json.dumps({"sha": sha}).encode()
            assert url == installer.ARCHIVE_URL + sha
            assert not cancelled()
            return payload
        def fixture_prepare(*args, **kwargs):
            return prepare(*args, fetcher=fetched, **kwargs)

        original.updates.report = report("0.5.0")
        settings = importlib.import_module(package + ".ui.settings_dialog")
        modal_checks = []
        def click_install():
            dialog = next(child for child in dock.findChildren(settings.SettingsDialog) if child.isVisible())
            dialog.tabs.setCurrentWidget(dialog.updates)
            verify("install_button_available", dialog.updates.install_button.isEnabled())
            dialog._chat_busy = True
            dialog.updates.install_button.click()
            verify("settings_blocks_busy_task", dialog.isVisible() and "Finish" in dialog.updates.status.text())
            dialog._chat_busy = False
            with patch.object(dialog, "_long_operation_running", return_value=True):
                dialog.updates.install_button.click()
                verify("settings_blocks_diagnostics", dialog.isVisible() and "diagnostics" in dialog.updates.status.text())
            dialog.updates.automatic.setChecked(False)
            dialog.grab().save(str(args.evidence / "install-button.png"))
            dialog.updates.install_button.click()
            modal_checks.append(not dialog.isVisible())
        with patch.object(settings.SettingsDialog, "_init_doctor", lambda self: None), patch.object(ui, "prepare_update", fixture_prepare):
            QTimer.singleShot(100, click_install)
            dock.open_update_settings()
            verify("settings_closed_before_install", modal_checks == [True])
            until(lambda: utils.plugins.get(package) is not original)
            until(lambda: utils.isPluginLoaded(package) and utils.plugins[package].dock is not None)
        current = utils.plugins[package]
        verify("real_qgis_plugin_restart", current is not original and current.dock is not dock)
        verify("download_off_main_thread", all(t != threading.get_ident() for t in download_threads))
        verify("new_module_loaded", importlib.import_module(package + ".plugin").UPDATE_TEST_COMMIT == "new module loaded")
        verify("plugin_manager_version_refreshed", utils.pluginMetadata(package, "version") == "0.5.0")
        verify("project_and_unsaved_layer_preserved", QgsProject.instance() is project and project.mapLayer(layer.id()) is layer and project.isDirty())
        verify("chat_history_preserved", history.read_bytes() == history_before and bool(current.dock._history_state["records"]))
        config = importlib.import_module(package + ".config")
        verify("saved_settings_preserved", not config.get(config.K_CHECK_UPDATES) and config.get(config.K_REDUCE_MOTION))
        backups = list((profile / "qgent" / "updates").glob("*/previous/metadata.txt"))
        verify("backup_retained", len(backups) == 1 and "version=0.4.1" in backups[0].read_text(encoding="utf-8"))
        verify("single_set_of_plugin_actions", len(iface.actions) == 3)
        iface.window.grab().save(str(args.evidence / "restarted.png"))

        # A valid ZIP with a startup failure must restore and restart the previous code.
        ui = importlib.import_module(package + ".ui.updates")
        installer = importlib.import_module(package + ".update_install")
        payload = archive("0.5.1", broken=True)
        prepare = installer.prepare_update
        errors = []
        with patch.object(ui, "prepare_update", fixture_prepare), patch.object(utils, "showException", lambda *a, **k: errors.append(True)):
            current._install_update("0.5.1")
            until(lambda: utils.plugins.get(package) is not current)
            until(lambda: utils.isPluginLoaded(package) and utils.plugins[package].dock is not None)
        restored = utils.plugins[package]
        verify("startup_failure_rollback", bool(errors) and utils.pluginMetadata(package, "version") == "0.5.0")
        verify("rollback_restarts_panel", restored.dock.isVisible() and len(iface.actions) == 3)
        verify("failed_startup_leaves_no_orphan_timers", sum(
            obj.timer.isActive() for obj in iface.window.findChildren(QObject)
            if obj.metaObject().className() == "UpdateManager") == 1)
        verify("rollback_preserves_project_and_history", project.mapLayer(layer.id()) is layer and history.read_bytes() == history_before)
        iface.window.grab().save(str(args.evidence / "rollback.png"))

        # A download error keeps the current plugin loaded and usable.
        ui = importlib.import_module(package + ".ui.updates")
        with patch.object(ui, "prepare_update", side_effect=TimeoutError("fixture offline")):
            restored._install_update("0.5.1")
            until(lambda: restored.updates.worker is None)
        verify("download_failure_visible_and_reenabled", "offline" in restored.updates.error and restored.dock.isEnabled() and not restored.updates.installing)
        verify("download_failure_keeps_original_plugin", utils.plugins[package] is restored)

        assert utils.unloadPlugin(package)
        app.processEvents()
        project.clear()
        iface.window.close()
        app.exitQgis()
        summary = {"verdict": "PASS", "qgis": Qgis.QGIS_VERSION, "assertions": assertions,
                   "scope": "Isolated offscreen Windows QGIS with real plugin unload/load and fixture downloads; desktop confirmation separate."}
        (args.evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
