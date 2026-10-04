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


class UpdateManager(QObject):
    changed = pyqtSignal()
    notice = pyqtSignal(object)
    models_changed = pyqtSignal()

    def __init__(self, profile_dir, plugin_dir, parent=None):
        super().__init__(parent)
        self.profile_dir = str(profile_dir)
        self.plugin_dir = str(plugin_dir)
        self.worker = None
        self.report = None
        self.error = ""
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
        if self._closed or self.worker is not None:
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
        self.worker = None
        if not self._closed:
            self.changed.emit()

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
            self.worker = None


class UpdatesPanel(QWidget):
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
        busy = manager is not None and manager.worker is not None
        self.check_button.setEnabled(manager is not None and not busy)
        self.dismiss_button.setEnabled(bool(manager and not busy and manager.report and manager.report.get("notices")))
        if manager is None:
            self.status.setText("Open Settings from the QGent panel to check updates.")
        elif busy:
            self.status.setText("Checking releases and models… You can keep working or close Settings.")
        else:
            self.status.setText(manager.error or ("Check complete — some sources need attention." if manager.report and manager.report.get("errors") else "Ready."))
        self.details.setPlainText(report_text(manager.report if manager else None))

    def shutdown(self):
        if self.manager is not None:
            self.manager.changed.disconnect(self.refresh)
            self.manager = None
