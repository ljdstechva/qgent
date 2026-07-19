# -*- coding: utf-8 -*-
"""Abstract agent backend + the Qt signal contract the dock listens to.

Both ``ClaudeCodeBackend`` and ``CodexBackend`` emit the same signals so the UI
is backend-agnostic. Session continuity is handled per backend (Claude uses
``--resume <session_id>``; Codex uses ``resume``), spawning a fresh CLI process
per turn rather than babysitting a long-lived stdin pipe.
"""
from qgis.PyQt.QtCore import QObject, pyqtSignal


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
    # coarse busy/idle state for the UI
    busy_changed = pyqtSignal(bool)

    def __init__(self, runtime_dir, mcp_config_path, env, parent=None):
        super().__init__(parent)
        self.runtime_dir = runtime_dir
        self.mcp_config_path = mcp_config_path
        self.env = env
        self.session_id = None

    # -- interface ----------------------------------------------------------
    def send(self, user_message, context_block):
        """Start a turn. Must be non-blocking; results arrive via signals."""
        raise NotImplementedError

    def cancel(self):
        """Interrupt the in-flight turn, if any."""
        raise NotImplementedError

    def is_busy(self):
        raise NotImplementedError
