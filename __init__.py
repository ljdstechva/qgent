# -*- coding: utf-8 -*-
"""QGent — plugin entry point.

QGIS calls ``classFactory`` to instantiate the plugin. Keep this file free of
heavy imports so that a broken dependency never prevents QGIS from loading the
plugin far enough to show an error.
"""


def _prepare_external_doctor():
    """Refresh dead-plugin recovery before importing the heavier UI layer."""
    import json
    import os
    from pathlib import Path

    from qgis.core import QgsApplication, QgsMessageLog, Qgis

    from . import config
    from .doctor_core import DEFAULT_SOURCE_REPO, ensure_recovery_entrypoint

    profile_dir = QgsApplication.qgisSettingsDirPath()
    plugin_dir = os.path.dirname(__file__)
    source_repo = str(DEFAULT_SOURCE_REPO) if DEFAULT_SOURCE_REPO.is_dir() else ""
    ensure_recovery_entrypoint(
        profile_dir, plugin_dir, config.python_executable(), source_repo)

    # The stdlib-only Doctor cannot write QSettings directly. Consume its
    # verified CLI detection state on the next successful plugin load.
    detected = os.path.join(
        profile_dir, "qgent", "doctor", "detected_cli_paths.json")
    if os.path.isfile(detected):
        try:
            payload = json.loads(Path(detected).read_text(encoding="utf-8"))
            paths = payload.get("cli_paths") or {}
            if paths.get("claude") and os.path.isfile(paths["claude"]):
                config.set(config.K_CLAUDE_PATH, paths["claude"])
            if paths.get("codex") and os.path.isfile(paths["codex"]):
                config.set(config.K_CODEX_PATH, paths["codex"])
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            QgsMessageLog.logMessage(
                f"External Doctor CLI-state import failed: {type(exc).__name__}: {exc}",
                "QGent", Qgis.Warning)


def classFactory(iface):  # noqa: N802 (QGIS-mandated name)
    try:
        _prepare_external_doctor()
    except Exception as exc:
        from qgis.core import QgsMessageLog, Qgis
        QgsMessageLog.logMessage(
            f"External Doctor recovery setup failed: {type(exc).__name__}: {exc}",
            "QGent", Qgis.Warning)
    from .plugin import QgisCopilotPlugin

    return QgisCopilotPlugin(iface)
