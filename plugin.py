# -*- coding: utf-8 -*-
"""QGent plugin — QGIS integration layer.

Responsibilities kept deliberately thin:
  * register a toolbar/menu action,
  * create and dock the chat panel,
  * own the lifetime of the execution bridge (socket server + main-thread
    executor) so it starts/stops with the plugin, not with each chat turn.
"""
from collections import deque
from datetime import datetime
import os

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QPushButton
from qgis.core import Qgis, QgsApplication

from .doctor import ModelWatchWorker
from .model_watch import acknowledge
from .ui.chat_dock import ChatDock

PLUGIN_DIR = os.path.dirname(__file__)


class QgisCopilotPlugin:
    """Life-cycle object QGIS keeps for the loaded plugin."""

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None
        self.menu = "&QGent"
        self.diagnostic_logs = deque(maxlen=200)
        self._message_log = None
        self._model_watch = None

    # -- QGIS hooks ---------------------------------------------------------
    def initGui(self):  # noqa: N802
        self._message_log = QgsApplication.messageLog()
        self._message_log.messageReceived.connect(self._capture_plugin_log)
        icon_path = os.path.join(PLUGIN_DIR, "resources", "icon.svg")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, "QGent", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle_panel)

        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(self.menu, self.action)
        # Let QGIS finish starting before touching the CLIs.
        QTimer.singleShot(5000, self._start_model_watch)

    def unload(self):
        if self._model_watch is not None:
            self._model_watch.wait(5000)
            self._model_watch = None
        if self._message_log is not None:
            try:
                self._message_log.messageReceived.disconnect(
                    self._capture_plugin_log)
            except (RuntimeError, TypeError):
                pass
            self._message_log = None
        if self.dock is not None:
            self.dock.shutdown()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu(self.menu, self.action)
            self.action = None

    # -- model watch --------------------------------------------------------
    def _start_model_watch(self):
        """Check once per QGIS session whether the CLIs ship unlisted models.

        Cached per CLI build, so a session that follows no CLI update reads a
        small JSON file and does no scanning at all.
        """
        if self.action is None or self._model_watch is not None:
            return
        try:
            worker = ModelWatchWorker(
                QgsApplication.qgisSettingsDirPath(),
                self.iface.mainWindow())
            worker.completed.connect(self._on_model_watch)
            worker.failed.connect(self._on_model_watch_failed)
            worker.finished.connect(self._clear_model_watch)
            worker.finished.connect(worker.deleteLater)
            self._model_watch = worker
            worker.start()
        except Exception as exc:
            self._on_model_watch_failed(f"{type(exc).__name__}: {exc}")

    def _clear_model_watch(self):
        self._model_watch = None

    def _on_model_watch_failed(self, message):
        QgsApplication.messageLog().logMessage(
            f"QGent model watch failed: {message}", "QGent", Qgis.Warning)

    def _on_model_watch(self, report):
        model_ids = list((report or {}).get("new") or [])
        if not model_ids or self.action is None:
            return
        bar = self.iface.messageBar()
        item = bar.createMessage(
            "QGent",
            "New AI model(s) available in your CLI: " + ", ".join(model_ids))
        settings = QPushButton("Open QGent settings")
        settings.clicked.connect(lambda: self._open_settings_from_notice(item))
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(
            lambda: self._dismiss_model_notice(item, model_ids))
        item.layout().addWidget(settings)
        item.layout().addWidget(dismiss)
        bar.pushWidget(item, Qgis.Info)

    def _open_settings_from_notice(self, item):
        self.iface.messageBar().popWidget(item)
        if self.action is not None:
            self.action.setChecked(True)
        self.toggle_panel(True)
        if self.dock is not None:
            self.dock.open_settings()

    def _dismiss_model_notice(self, item, model_ids):
        self.iface.messageBar().popWidget(item)
        try:
            acknowledge(QgsApplication.qgisSettingsDirPath(), model_ids)
        except OSError as exc:
            self._on_model_watch_failed(f"{type(exc).__name__}: {exc}")

    # -- behaviour ----------------------------------------------------------
    def toggle_panel(self, checked):
        if self.dock is None:
            self.dock = ChatDock(
                self.iface, PLUGIN_DIR,
                diagnostic_logs=self.diagnostic_logs)
            self.dock.visibilityChanged.connect(self._sync_action_state)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.dock.setVisible(checked)
        if checked:
            self.dock.raise_()
            self.dock.focus_input()

    def _sync_action_state(self, visible):
        if self.action is not None:
            self.action.blockSignals(True)
            self.action.setChecked(visible)
            self.action.blockSignals(False)

    def _capture_plugin_log(self, message, tag, level):
        haystack = f"{tag} {message}".lower()
        if "qgent" not in haystack and "qgis copilot" not in haystack:
            return
        try:
            numeric_level = int(level)
        except (TypeError, ValueError):
            numeric_level = str(level)
        self.diagnostic_logs.append({
            "t": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tag": str(tag),
            "level": numeric_level,
            "message": str(message),
        })
