# -*- coding: utf-8 -*-
"""Background update checks shared by the plugin menu and Settings."""
from qgis.PyQt.QtCore import QObject, QThread, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)
from qgis.core import Qgis, QgsApplication

from .. import config
from ..update_check import LINKS, LABELS, check_updates, dismiss, report_text
from ..update_install import prepare_update, discard_update, install_update, rollback_update


class UpdateWorker(QThread):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, profile_dir, plugin_dir, paths, force):
        super().__init__()
        self.arguments = (profile_dir, plugin_dir, paths, force)

    def run(self):
        try:
            report = check_updates(*self.arguments, cancelled=self.isInterruptionRequested)
            if report is not None and not self.isInterruptionRequested():
                self.completed.emit(report)
        except Exception as exc:
            self.failed.emit(f"Update check failed: {type(exc).__name__}. Try again or inspect the installation.")


class InstallWorker(QThread):
    failed = pyqtSignal(str)

    def __init__(self, profile_dir, plugin_dir, version):
        super().__init__()
        self.arguments = (profile_dir, plugin_dir, version, Qgis.QGIS_VERSION_INT)
        self.prepared = None

    def run(self):
        try:
            self.prepared = prepare_update(*self.arguments, cancelled=self.isInterruptionRequested)
        except Exception as exc:
            self.failed.emit(f"QGent update failed: {type(exc).__name__}: {exc}")


def _start_plugin(package):
    """Keep the instance long enough to clean up even a partially failed initGui."""
    import sys
    from qgis import utils

    if not utils.loadPlugin(package):
        return False
    module = sys.modules[package]
    factory = module.classFactory
    created = []
    def capture(iface):
        instance = factory(iface)
        created.append(instance)
        return instance
    module.classFactory = capture
    started = False
    try:
        started = utils.startPlugin(package)
        return started
    finally:
        module.classFactory = factory
        # QGIS drops its reference on initGui failure without calling unload.
        if not started and created:
            created[0].unload()


def restart_with_update(iface, plugin_dir, prepared):
    """Run on the GUI thread after Settings and the download thread have exited."""
    from pathlib import Path
    from qgis import utils

    package = Path(plugin_dir).name
    replaced = False
    unloaded = False
    try:
        if not utils.unloadPlugin(package):
            raise RuntimeError("Could not stop QGent; no files were replaced")
        unloaded = True
        install_update(prepared, plugin_dir)
        replaced = True
        utils.updateAvailablePlugins()
        if not _start_plugin(package):
            raise RuntimeError("The new QGent could not start")
        utils.plugins[package].toggle_panel(True)
    except Exception as exc:
        message = f"QGent update failed: {type(exc).__name__}: {exc}."
        try:
            if unloaded:
                if utils.isPluginLoaded(package) and not utils.unloadPlugin(package):
                    raise RuntimeError("Could not unload the failed update")
                if replaced:
                    rollback_update(prepared, plugin_dir)
                utils.updateAvailablePlugins()
                if not _start_plugin(package):
                    raise RuntimeError("Could not restart the previous QGent")
                utils.plugins[package].toggle_panel(True)
                message += " Previous QGent restored."
            else:
                previous = utils.plugins.get(package)
                if previous is not None:
                    previous.updates.installing = False
                    previous.updates._failed(message)
        except Exception as recovery_error:
            message += f" Recovery failed: {recovery_error}. Close QGIS and restore the backup."
        message += f" Update files: {prepared['backup'].parent}"
        QgsApplication.messageLog().logMessage(message, "QGent updates", Qgis.Critical)
        iface.messageBar().pushMessage("QGent update", message, level=Qgis.Critical, duration=0)
        return False
    iface.messageBar().pushMessage(
        "QGent updated", f"Version {prepared['version']} installed and QGent restarted. "
        f"Backup: {prepared['backup']}", level=Qgis.Success, duration=15)
    return True


class UpdateManager(QObject):
    changed = pyqtSignal()
    notice = pyqtSignal(object)
    models_changed = pyqtSignal()
    install_requested = pyqtSignal(str)
    install_ready = pyqtSignal(object)

    def __init__(self, profile_dir, plugin_dir, parent=None):
        super().__init__(parent)
        self.profile_dir = str(profile_dir)
        self.plugin_dir = str(plugin_dir)
        self.worker = None
        self.report = None
        self.error = ""
        self.installing = False
        self._closed = False
        self._shown = set()
        self.timer = QTimer(self)
        self.timer.setInterval(60 * 60 * 1000)
        self.timer.timeout.connect(self.check_automatic)
        self.startup = QTimer(self)
        self.startup.setSingleShot(True)
        self.startup.timeout.connect(self.check_automatic)

    def start(self):
        self.timer.start()
        self.startup.start(5000)

    def check_automatic(self):
        if config.get(config.K_CHECK_UPDATES):
            self.check()

    def check(self, force=False):
        if self._closed or self.worker is not None or self.installing:
            return
        self.error = ""
        paths = {"claude": config.detect_claude(), "codex": config.detect_codex()}
        worker = UpdateWorker(self.profile_dir, self.plugin_dir, paths, force)
        worker.completed.connect(self._complete)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._finished)
        worker.finished.connect(worker.deleteLater)
        self.worker = worker
        self.changed.emit()
        worker.start()

    def install(self, version):
        if self._closed or self.worker is not None or self.installing:
            return
        self.error = ""
        self.installing = True
        worker = InstallWorker(self.profile_dir, self.plugin_dir, version)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._finished)
        worker.finished.connect(worker.deleteLater)
        self.worker = worker
        self.changed.emit()
        worker.start()

    def _complete(self, report):
        if self._closed:
            return
        self.report = report
        for backend, row in report.get("models", {}).items():
            if "entries" in row:
                try:
                    config.set_discovered_models(backend, row["entries"])
                except (OSError, ValueError) as exc:
                    report["errors"].append(f"Could not save {backend} model choices: {type(exc).__name__}.")
        self.models_changed.emit()
        for error in report.get("errors", []):
            QgsApplication.messageLog().logMessage(error, "QGent updates", Qgis.Warning)
        fresh = set(report.get("notices", [])) - self._shown
        if fresh:
            self._shown.update(fresh)
            self.notice.emit(report)
        self.changed.emit()

    def _failed(self, message):
        if self._closed:
            return
        self.error = message
        QgsApplication.messageLog().logMessage(message, "QGent updates", Qgis.Warning)
        self.changed.emit()

    def _finished(self):
        prepared = getattr(self.worker, "prepared", None)
        self.worker = None
        if not self._closed:
            if self.installing and prepared is None:
                self.installing = False
            self.changed.emit()
            if prepared is not None:
                self.install_ready.emit(prepared)

    def dismiss(self):
        if not self.report:
            return True
        try:
            dismiss(self.profile_dir, self.report.get("notices", []))
        except OSError as exc:
            self._failed(f"Could not save update dismissal: {type(exc).__name__}.")
            return False
        self.report["notices"] = []
        self.changed.emit()
        return True

    def close(self):
        self._closed = True
        self.timer.stop()
        self.startup.stop()
        if self.worker is not None:
            self.worker.requestInterruption()
            # Keep the QThread alive until its current bounded read finishes.
            # No subsequent CLI/network operation runs after cancellation.
            self.worker.wait()
            prepared = getattr(self.worker, "prepared", None)
            if prepared is not None:
                discard_update(prepared)
            self.worker = None


class UpdatesPanel(QWidget):
    install_requested = pyqtSignal()

    def __init__(self, manager=None, parent=None):
        super().__init__(parent)
        self.manager = manager
        layout = QVBoxLayout(self)
        self.automatic = QCheckBox("Check for QGent, Claude Code and Codex updates automatically")
        self.automatic.setChecked(config.get(config.K_CHECK_UPDATES))
        layout.addWidget(self.automatic)
        explanation = QLabel(
            "Checks run in the background, at most daily (hourly retries after errors). "
            "Public release checks contact GitHub; model lists refresh from your installed Claude and Codex CLIs. "
            "No paid model request is made. Save this preference with OK.")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        row = QHBoxLayout()
        self.check_button = QPushButton("Check for updates")
        self.check_button.clicked.connect(self._check)
        row.addWidget(self.check_button)
        self.dismiss_button = QPushButton("Dismiss these notifications")
        self.dismiss_button.clicked.connect(self._dismiss)
        row.addWidget(self.dismiss_button)
        layout.addLayout(row)
        self.install_button = QPushButton("Install update and restart QGent")
        self.install_button.clicked.connect(self.install_requested)
        layout.addWidget(self.install_button)
        install_note = QLabel(
            "Saves Settings, downloads QGent from GitHub, and restarts its panel automatically. "
            "Your QGIS project stays open. A backup is kept in your profile's qgent/updates folder.")
        install_note.setWordWrap(True)
        layout.addWidget(install_note)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 1)
        links = QHBoxLayout()
        for name in LINKS:
            button = QPushButton(LABELS[name] + (" installation" if name == "qgent" else " releases"))
            button.clicked.connect(lambda _checked=False, key=name: self._open_link(key))
            links.addWidget(button)
        layout.addLayout(links)
        if manager is not None:
            manager.changed.connect(self.refresh)
        self.refresh()

    def _open_link(self, name):
        if not QDesktopServices.openUrl(QUrl(LINKS[name])):
            self.status.setText("Could not open your browser. Visit: " + LINKS[name])

    def _check(self):
        if self.manager is not None:
            self.manager.check(force=True)

    def _dismiss(self):
        if self.manager is not None:
            self.manager.dismiss()

    def refresh(self):
        manager = self.manager
        busy = manager is not None and (manager.worker is not None or manager.installing)
        release = manager.report.get("releases", {}).get("qgent", {}) if manager and manager.report else {}
        self.install_button.setEnabled(bool(manager and not busy and release.get("update") and not release.get("error")))
        self.check_button.setEnabled(manager is not None and not busy)
        self.dismiss_button.setEnabled(bool(manager and not busy and manager.report and manager.report.get("notices")))
        if manager is None:
            self.status.setText("Open Settings from the QGent panel to check updates.")
        elif manager.installing:
            self.status.setText("Installing QGent… It will restart automatically when the download is verified.")
        elif busy:
            self.status.setText("Checking releases and models… You can keep working or close Settings.")
        else:
            self.status.setText(manager.error or ("Check complete — some sources need attention." if manager.report and manager.report.get("errors") else "Ready."))
        self.details.setPlainText(report_text(manager.report if manager else None))

    def shutdown(self):
        if self.manager is not None:
            self.manager.changed.disconnect(self.refresh)
            self.manager = None
