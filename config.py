# -*- coding: utf-8 -*-
"""Persistent settings and CLI autodetection for QGent.

All settings are stored under the ``QgisCopilot/`` group via ``QSettings`` so
they survive QGIS restarts and are shared across projects. CLI paths are
autodetected once and cached, but always overridable in the settings dialog.
"""
import glob
import os
import shutil
import sys

from qgis.PyQt.QtCore import QSettings

from .model_catalog import (
    CUSTOM_MODEL_SENTINEL, LIGHT_ROLE_TOOLTIP, MODEL_CATALOG, MODEL_DEFAULTS,
    MODEL_IDS_BY_BACKEND, MODEL_PRESET_CUSTOM, MODEL_PRESET_LONG_CONTEXT,
    MODEL_PRESET_MAX_QUALITY,
    MODEL_PRESET_SPEED, MODEL_ROLES, accepted_model_ids,
    classify_model_preset, default_model, model_ids, model_options,
    model_preset_options, model_preset_values, normalize_model_id,
    repair_model_specs,
)

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
K_MODEL_SUPERVISOR_CLAUDE = "model_supervisor_claude"
K_MODEL_WORKER_CLAUDE = "model_worker_claude"
K_MODEL_LIGHT_CLAUDE = "model_light_claude"
K_MODEL_SUPERVISOR_CODEX = "model_supervisor_codex"
K_MODEL_WORKER_CODEX = "model_worker_codex"
K_MODEL_LIGHT_CODEX = "model_light_codex"
K_MODEL_SUPERVISOR_CLAUDE_CUSTOM = "model_supervisor_claude_custom"
K_MODEL_WORKER_CLAUDE_CUSTOM = "model_worker_claude_custom"
K_MODEL_LIGHT_CLAUDE_CUSTOM = "model_light_claude_custom"
K_MODEL_SUPERVISOR_CODEX_CUSTOM = "model_supervisor_codex_custom"
K_MODEL_WORKER_CODEX_CUSTOM = "model_worker_codex_custom"
K_MODEL_LIGHT_CODEX_CUSTOM = "model_light_codex_custom"
K_MODEL_SETTINGS_VERSION = "model_settings_version"
K_EXEC_TIMEOUT = "exec_timeout_s"
K_REDUCE_MOTION = "reduce_motion"
K_SHOW_MAP_SNAPSHOTS = "show_map_snapshots"
K_FAST_MODE = "fast_mode"

MODEL_SETTINGS_VERSION = 2

_MODEL_KEYS = {
    ("claude", "supervisor"): K_MODEL_SUPERVISOR_CLAUDE,
    ("claude", "worker"): K_MODEL_WORKER_CLAUDE,
    ("claude", "light"): K_MODEL_LIGHT_CLAUDE,
    ("codex", "supervisor"): K_MODEL_SUPERVISOR_CODEX,
    ("codex", "worker"): K_MODEL_WORKER_CODEX,
    ("codex", "light"): K_MODEL_LIGHT_CODEX,
}
_MODEL_CUSTOM_KEYS = {
    ("claude", "supervisor"): K_MODEL_SUPERVISOR_CLAUDE_CUSTOM,
    ("claude", "worker"): K_MODEL_WORKER_CLAUDE_CUSTOM,
    ("claude", "light"): K_MODEL_LIGHT_CLAUDE_CUSTOM,
    ("codex", "supervisor"): K_MODEL_SUPERVISOR_CODEX_CUSTOM,
    ("codex", "worker"): K_MODEL_WORKER_CODEX_CUSTOM,
    ("codex", "light"): K_MODEL_LIGHT_CODEX_CUSTOM,
}
_LEGACY_MODEL_KEYS = {
    "supervisor": K_MODEL_SUPERVISOR,
    "worker": K_MODEL_WORKER,
    "light": K_MODEL_LIGHT,
}

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
    K_MODEL_SUPERVISOR_CLAUDE: "sonnet",
    K_MODEL_WORKER_CLAUDE: "sonnet",
    K_MODEL_LIGHT_CLAUDE: "haiku",
    K_MODEL_SUPERVISOR_CODEX: "gpt-5.6-sol",
    K_MODEL_WORKER_CODEX: "gpt-5.6-sol",
    K_MODEL_LIGHT_CODEX: "gpt-5.6-sol",
    K_MODEL_SUPERVISOR_CLAUDE_CUSTOM: False,
    K_MODEL_WORKER_CLAUDE_CUSTOM: False,
    K_MODEL_LIGHT_CLAUDE_CUSTOM: False,
    K_MODEL_SUPERVISOR_CODEX_CUSTOM: False,
    K_MODEL_WORKER_CODEX_CUSTOM: False,
    K_MODEL_LIGHT_CODEX_CUSTOM: False,
    K_MODEL_SETTINGS_VERSION: 0,
    K_EXEC_TIMEOUT: 60,
    K_REDUCE_MOTION: False,
    K_SHOW_MAP_SNAPSHOTS: True,
    K_FAST_MODE: False,
}

_BOOL_KEYS = {
    K_VERIFIER, K_REDUCE_MOTION, K_SHOW_MAP_SNAPSHOTS, K_FAST_MODE,
    *_MODEL_CUSTOM_KEYS.values(),
}
_INT_KEYS = {K_MAX_RESULT, K_EXEC_TIMEOUT, K_MODEL_SETTINGS_VERSION}


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


def _model_setting_key(backend, role, custom=False):
    backend = str(backend or "").lower()
    role = str(role or "").lower()
    table = _MODEL_CUSTOM_KEYS if custom else _MODEL_KEYS
    try:
        return table[(backend, role)]
    except KeyError as exc:
        raise ValueError(f"Unsupported model setting: {backend}/{role}") from exc


def _as_bool(value):
    return value in (True, "true", "True", 1, "1")


def migrate_model_settings():
    """Migrate Goal 4's shared values to Claude and seed valid Codex defaults."""
    settings = _settings()
    try:
        version = int(settings.value(K_MODEL_SETTINGS_VERSION, 0) or 0)
    except (TypeError, ValueError):
        version = 0
    if version >= MODEL_SETTINGS_VERSION:
        settings.endGroup()
        return False

    for role in MODEL_ROLES:
        claude_key = _model_setting_key("claude", role)
        claude_custom_key = _model_setting_key("claude", role, custom=True)
        legacy = str(settings.value(
            _LEGACY_MODEL_KEYS[role], MODEL_DEFAULTS["claude"][role]) or "").strip()
        normalized = normalize_model_id("claude", legacy)
        if not settings.contains(claude_key):
            settings.setValue(claude_key, normalized or legacy or default_model(
                "claude", role))
        if not settings.contains(claude_custom_key):
            settings.setValue(claude_custom_key, bool(legacy and not normalized))

        codex_key = _model_setting_key("codex", role)
        codex_custom_key = _model_setting_key("codex", role, custom=True)
        if not settings.contains(codex_key):
            settings.setValue(codex_key, default_model("codex", role))
        if not settings.contains(codex_custom_key):
            settings.setValue(codex_custom_key, False)

    settings.setValue(K_MODEL_SETTINGS_VERSION, MODEL_SETTINGS_VERSION)
    settings.sync()
    settings.endGroup()
    return True


def get_model_choice(backend, role="supervisor"):
    """Return the stored per-backend choice and explicit-Custom provenance."""
    backend = str(backend or "").lower()
    role = str(role or "supervisor").lower()
    migrate_model_settings()
    settings = _settings()
    value = str(settings.value(
        _model_setting_key(backend, role), default_model(backend, role))
        or "").strip()
    custom = _as_bool(settings.value(
        _model_setting_key(backend, role, custom=True), False))
    settings.endGroup()
    return {"model_id": value, "custom": bool(custom)}


def set_model_choice(backend, role, model_id, custom=False):
    """Persist a choice without making legacy shared keys authoritative."""
    backend = str(backend or "").lower()
    role = str(role or "").lower()
    value = str(model_id or "").strip()
    if not custom:
        value = normalize_model_id(backend, value) or default_model(backend, role)
    elif not value:
        custom = False
        value = default_model(backend, role)
    settings = _settings()
    settings.setValue(_model_setting_key(backend, role), value)
    settings.setValue(_model_setting_key(backend, role, custom=True), bool(custom))
    settings.sync()
    settings.endGroup()


def validate_model_choice(backend, role="supervisor", persist=True):
    """Resolve a safe CLI id and reset unknown non-Custom values to default."""
    backend = str(backend or "").lower()
    role = str(role or "supervisor").lower()
    choice = get_model_choice(backend, role)
    value = choice["model_id"]
    if choice["custom"] and value:
        return {
            "model_id": value, "custom": True, "reset": False, "note": "",
        }
    normalized = normalize_model_id(backend, value)
    if normalized:
        if persist and normalized != value:
            set_model_choice(backend, role, normalized, custom=False)
        return {
            "model_id": normalized, "custom": False,
            "reset": False, "note": "",
        }

    fallback = default_model(backend, role)
    if persist:
        set_model_choice(backend, role, fallback, custom=False)
    backend_name = "Codex" if backend == "codex" else "Claude Code"
    return {
        "model_id": fallback,
        "custom": False,
        "reset": True,
        "note": (
            f"model reset to {fallback} — previous selection was not valid "
            f"for {backend_name}"
        ),
    }


# --- CLI autodetection -----------------------------------------------------
def _which(*names):
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def _first_existing(*paths):
    for path in paths:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return ""


def detect_claude():
    """Best-effort path to the Claude Code CLI.

    On Windows the npm global install exposes ``claude.cmd``; ``shutil.which``
    resolves it when the PATHEXT includes ``.CMD`` (it does by default).
    """
    cached = get(K_CLAUDE_PATH)
    if cached and os.path.exists(cached):
        return cached
    user_profile = os.environ.get("USERPROFILE", "")
    app_data = os.environ.get("APPDATA", "")
    found = _which("claude", "claude.cmd", "claude.exe") or _first_existing(
        os.path.join(user_profile, ".local", "bin", "claude.exe"),
        os.path.join(app_data, "npm", "claude.cmd"),
        os.path.join(app_data, "npm", "claude.exe"),
    )
    if found:
        set(K_CLAUDE_PATH, found)
    return found


def detect_codex():
    cached = get(K_CODEX_PATH)
    if cached and os.path.exists(cached):
        native = _codex_native_from_npm_shim(cached)
        if native:
            set(K_CODEX_PATH, native)
            return native
        return cached
    app_data = os.environ.get("APPDATA", "")
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    found = _which("codex", "codex.cmd", "codex.exe") or _first_existing(
        os.path.join(app_data, "npm", "codex.cmd"),
        os.path.join(app_data, "npm", "codex.exe"),
        os.path.join(local_app_data, "Programs", "codex", "codex.exe"),
    )
    found = _codex_native_from_npm_shim(found) or found
    if found:
        set(K_CODEX_PATH, found)
    return found


def _codex_native_from_npm_shim(path):
    """Resolve npm's Node-dependent Windows shim to its bundled native CLI.

    QGIS's OSGeo4W launcher intentionally supplies a reduced ``PATH`` which
    commonly excludes Node.js.  Current Codex npm packages bundle the native
    executable below the shim, so prefer it when available.
    """
    text = os.path.abspath(str(path or "")) if path else ""
    if os.name != "nt" or os.path.splitext(text)[1].lower() not in (
            ".cmd", ".bat"):
        return ""
    pattern = os.path.join(
        os.path.dirname(text), "node_modules", "@openai", "codex",
        "node_modules", "@openai", "codex-win32-*", "vendor", "*",
        "bin", "codex.exe")
    return _first_existing(*sorted(glob.glob(pattern)))


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
