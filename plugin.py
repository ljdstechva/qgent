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

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QPushButton
from qgis.core import Qgis, QgsApplication

from .ui.chat_dock import ChatDock
from .ui.updates import UpdateManager

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
        self.updates = None
        self.update_action = None
        self._update_notice = None

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
        self.updates = UpdateManager(
            QgsApplication.qgisSettingsDirPath(), PLUGIN_DIR,
            self.iface.mainWindow())
        self.updates.notice.connect(self._on_update_notice)
        self.updates.changed.connect(self._sync_update_notice)
        self.update_action = QAction("Check for updates…", self.iface.mainWindow())
        self.update_action.triggered.connect(self._open_updates)
        self.iface.addPluginToMenu(self.menu, self.update_action)
        self.updates.start()

    def unload(self):
        if self.updates is not None:
            self.updates.close()
            self.updates.deleteLater()
            self.updates = None
        if self._update_notice is not None:
            self.iface.messageBar().popWidget(self._update_notice)
            self._update_notice = None
        if self.update_action is not None:
            self.iface.removePluginMenu(self.menu, self.update_action)
            self.update_action.deleteLater()
            self.update_action = None
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

    # -- updates ------------------------------------------------------------
    def _on_update_notice(self, report):
        if self.action is None:
            return
        bar = self.iface.messageBar()
        if self._update_notice is not None:
            bar.popWidget(self._update_notice)
        releases = sum(row.get("update", False) for row in report["releases"].values())
        models = sum(len(row.get("ids", [])) for row in report["models"].values())
        item = bar.createMessage(
            "QGent updates",
            f"{releases} software update(s), {models} unlisted model candidate(s).")
        settings = QPushButton("Review updates")
        settings.clicked.connect(self._open_updates)
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(self._dismiss_update_notice)
        item.layout().addWidget(settings)
        item.layout().addWidget(dismiss)
        self._update_notice = item
        item.destroyed.connect(lambda: self._notice_destroyed(item))
        bar.pushWidget(item, Qgis.Info, 0)

    def _notice_destroyed(self, item):
        if self._update_notice is item:
            self._update_notice = None

    def _sync_update_notice(self):
        if (self._update_notice is not None and self.updates is not None
                and self.updates.report and not self.updates.report.get("notices")):
            self.iface.messageBar().popWidget(self._update_notice)
            self._update_notice = None

    def _open_updates(self, _checked=False):
        if self.action is not None:
            self.action.setChecked(True)
        self.toggle_panel(True)
        if self.updates is not None:
            self.updates.check(force=True)
        if self.dock is not None:
            self.dock.open_update_settings()

    def _dismiss_update_notice(self):
        if self.updates is not None and self.updates.dismiss() and self._update_notice is not None:
            self.iface.messageBar().popWidget(self._update_notice)
            self._update_notice = None

    # -- behaviour ----------------------------------------------------------
    def toggle_panel(self, checked):
        if self.dock is None:
            self.dock = ChatDock(
                self.iface, PLUGIN_DIR,
                diagnostic_logs=self.diagnostic_logs)
            self.dock.update_manager = self.updates
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
