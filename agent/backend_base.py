# -*- coding: utf-8 -*-
"""Abstract agent backend + the Qt signal contract the dock listens to.

Both ``ClaudeCodeBackend`` and ``CodexBackend`` emit the same signals so the UI
is backend-agnostic. Session continuity is handled per backend (Claude uses
``--resume <session_id>``; Codex uses ``resume``), spawning a fresh CLI process
per turn rather than babysitting a long-lived stdin pipe.
"""
import math

from qgis.PyQt.QtCore import QObject, pyqtSignal


def _usage_number(value, integer=True):
    """Return a non-negative CLI-reported number, never a guessed value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not math.isfinite(value) or value < 0:
        return None
    if integer:
        if value.is_integer():
            return int(value)
        return None
    return value


def normalized_usage(backend, payload):
    """Normalize usage already present in a terminal CLI event.

    Missing fields remain missing.  In particular, cached/reasoning counters are
    retained as breakdown fields but are not double-counted in Codex totals.
    """
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    kind = str(backend or "").strip().lower()

    if kind == "claude":
        token_keys = (
            "input_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens", "output_tokens",
        )
        result = {"backend": "claude"}
        for key in token_keys:
            value = _usage_number(usage.get(key))
            if value is not None:
                result[key] = value
        total = _usage_number(usage.get("total_tokens"))
        if total is None:
            total = _usage_number(payload.get("total_tokens"))
        if total is None and any(key in result for key in token_keys):
            total = sum(result.get(key, 0) for key in token_keys)
        if total is not None:
            result["total_tokens"] = total
        cost = _usage_number(payload.get("total_cost_usd"), integer=False)
        if cost is None:
            cost = _usage_number(usage.get("cost_usd"), integer=False)
        if cost is not None:
            result["cost_usd"] = cost
        return result if len(result) > 1 else None

    if kind == "codex":
        token_keys = (
            "input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_output_tokens",
        )
        result = {"backend": "codex"}
        for key in token_keys:
            value = _usage_number(usage.get(key))
            if value is not None:
                result[key] = value
        total = _usage_number(usage.get("total_tokens"))
        if total is None:
            total = _usage_number(payload.get("total_tokens"))
        if total is None and any(
                key in result for key in ("input_tokens", "output_tokens")):
            total = result.get("input_tokens", 0) + result.get(
                "output_tokens", 0)
        if total is not None:
            result["total_tokens"] = total
        return result if len(result) > 1 else None

    return None


class AgentBackend(QObject):
    # streamed assistant text delta
    token = pyqtSignal(str)
    # a tool the agent invoked: (name, args_dict)
    tool_call = pyqtSignal(str, object)
    # textual result returned for the most recent tool call
    tool_result = pyqtSignal(str)
    # subagent lifecycle: (subagent_name, status)  status in {started, finished}
    subagent_event = pyqtSignal(str, str)
    # the CLI reported a session id we can resume
    session_started = pyqtSignal(str)
    # turn finished cleanly; payload is the final result dict
    done = pyqtSignal(object)
    # a fatal error for this turn
    error = pyqtSignal(str)
    # a non-fatal configuration/status note for the transcript
    status_note = pyqtSignal(str)
    # coarse busy/idle state for the UI
    busy_changed = pyqtSignal(bool)

    def __init__(self, runtime_dir, mcp_config_path, env, parent=None):
        super().__init__(parent)
        self.runtime_dir = runtime_dir
        self.mcp_config_path = mcp_config_path
        self.env = env
        self.session_id = None
        self.last_stderr = ""

    # -- interface ----------------------------------------------------------
    def send(self, user_message, context_block):
        """Start a turn. Must be non-blocking; results arrive via signals."""
        raise NotImplementedError

    def cancel(self):
        """Interrupt the in-flight turn, if any."""
        raise NotImplementedError

    def is_busy(self):
        raise NotImplementedError
