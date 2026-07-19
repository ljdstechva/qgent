# -*- coding: utf-8 -*-
"""QGIS Copilot — plugin entry point.

QGIS calls ``classFactory`` to instantiate the plugin. Keep this file free of
heavy imports so that a broken dependency never prevents QGIS from loading the
plugin far enough to show an error.
"""


def classFactory(iface):  # noqa: N802 (QGIS-mandated name)
    from .plugin import QgisCopilotPlugin

    return QgisCopilotPlugin(iface)
