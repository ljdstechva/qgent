# -*- coding: utf-8 -*-
"""QGent plugin — QGIS integration layer.

Responsibilities kept deliberately thin:
  * register a toolbar/menu action,
  * create and dock the chat panel,
  * own the lifetime of the execution bridge (socket server + main-thread
    executor) so it starts/stops with the plugin, not with each chat turn.
"""
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .ui.chat_dock import ChatDock

PLUGIN_DIR = os.path.dirname(__file__)


class QgisCopilotPlugin:
    """Life-cycle object QGIS keeps for the loaded plugin."""

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None
        self.menu = "&QGent"

    # -- QGIS hooks ---------------------------------------------------------
    def initGui(self):  # noqa: N802
        icon_path = os.path.join(PLUGIN_DIR, "resources", "icon.svg")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, "QGent", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle_panel)

        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(self.menu, self.action)

    def unload(self):
        if self.dock is not None:
            self.dock.shutdown()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu(self.menu, self.action)
            self.action = None

    # -- behaviour ----------------------------------------------------------
    def toggle_panel(self, checked):
        if self.dock is None:
            self.dock = ChatDock(self.iface, PLUGIN_DIR)
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
