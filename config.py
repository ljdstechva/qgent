# -*- coding: utf-8 -*-
"""Persistent settings and CLI autodetection for QGIS Copilot.

All settings are stored under the ``QgisCopilot/`` group via ``QSettings`` so
they survive QGIS restarts and are shared across projects. CLI paths are
autodetected once and cached, but always overridable in the settings dialog.
"""
import os
import shutil
import sys

from qgis.PyQt.QtCore import QSettings

GROUP = "QgisCopilot"

# --- keys ------------------------------------------------------------------
K_BACKEND = "backend"                 # "claude" | "codex"
K_CLAUDE_PATH = "claude_path"
K_CODEX_PATH = "codex_path"
K_PERMISSION_MODE = "permission_mode"  # "auto" | "ask_always" | "ask_destructive"
K_VERIFIER = "verifier_enabled"
K_MAX_RESULT = "max_result_bytes"
K_MODEL_SUPERVISOR = "model_supervisor"
K_MODEL_WORKER = "model_worker"
K_MODEL_LIGHT = "model_light"          # data-scout / qa-verifier
K_EXEC_TIMEOUT = "exec_timeout_s"

DEFAULTS = {
    K_BACKEND: "claude",
    K_CLAUDE_PATH: "",
    K_CODEX_PATH: "",
    K_PERMISSION_MODE: "ask_destructive",
    K_VERIFIER: True,
    K_MAX_RESULT: 4000,
    K_MODEL_SUPERVISOR: "sonnet",
    K_MODEL_WORKER: "sonnet",
    K_MODEL_LIGHT: "haiku",
    K_EXEC_TIMEOUT: 60,
}

_BOOL_KEYS = {K_VERIFIER}
_INT_KEYS = {K_MAX_RESULT, K_EXEC_TIMEOUT}


def _settings():
    s = QSettings()
    s.beginGroup(GROUP)
    return s


def get(key):
    s = _settings()
    raw = s.value(key, DEFAULTS.get(key))
    s.endGroup()
    if key in _BOOL_KEYS:
        # QSettings returns strings on some platforms.
        return raw in (True, "true", "True", 1, "1")
    if key in _INT_KEYS:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return DEFAULTS[key]
    return raw


def set(key, value):  # noqa: A001 (shadowing builtin is fine for a config module)
    s = _settings()
    s.setValue(key, value)
    s.endGroup()


# --- CLI autodetection -----------------------------------------------------
def _which(*names):
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def detect_claude():
    """Best-effort path to the Claude Code CLI.

    On Windows the npm global install exposes ``claude.cmd``; ``shutil.which``
    resolves it when the PATHEXT includes ``.CMD`` (it does by default).
    """
    cached = get(K_CLAUDE_PATH)
    if cached and os.path.exists(cached):
        return cached
    found = _which("claude", "claude.cmd", "claude.exe")
    if found:
        set(K_CLAUDE_PATH, found)
    return found


def detect_codex():
    cached = get(K_CODEX_PATH)
    if cached and os.path.exists(cached):
        return cached
    found = _which("codex", "codex.cmd", "codex.exe")
    if found:
        set(K_CODEX_PATH, found)
    return found


def active_cli_path():
    return detect_claude() if get(K_BACKEND) == "claude" else detect_codex()


def python_executable():
    """A real Python interpreter for running the stdlib-only MCP bridge.

    ``sys.executable`` inside QGIS on Windows often points at ``qgis-bin.exe``,
    which would relaunch QGIS rather than run a script. Prefer the interpreter
    that lives under ``sys.prefix`` (the QGIS Python home), then fall back.
    """
    candidates = [
        os.path.join(sys.prefix, "python.exe"),          # Windows QGIS Python
        os.path.join(sys.prefix, "bin", "python3"),      # *nix
        os.path.join(sys.prefix, "bin", "python"),
    ]
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    found = _which("python3", "python")
    if found:
        return found
    # Last resort — may be qgis-bin.exe; documented as a known limitation.
    return sys.executable
