# -*- coding: utf-8 -*-
"""Claude Code CLI backend (primary).

One ``QProcess`` per turn: ``claude -p --output-format stream-json --verbose
--include-partial-messages``
reading the prompt from stdin. The first turn captures ``session_id`` from the
init event; later turns pass ``--resume <session_id>`` so skills, CLAUDE.md, and
project grounding persist without a fragile long-lived stdin pipe.

Tool policy: only our MCP tools + Task (subagents) + read-only builtins are
allowed; Bash/Write/Edit/web tools are disallowed so the agent stays inside the
QGIS bridge. The *real* destructive-op gate lives in the socket server.
"""
from qgis.PyQt.QtCore import QProcess, QProcessEnvironment

from .backend_base import AgentBackend
from .stream_parser import StreamJsonParser
from .. import config

_ALLOWED_TOOLS = ",".join([
    "mcp__qgis__execute_pyqgis",
    "mcp__qgis__get_project_context",
    "mcp__qgis__run_processing",
    "mcp__qgis__get_layer_features",
    "mcp__qgis__render_map_snapshot",
    "Task", "TodoWrite", "Read", "Glob", "Grep",
])
_DISALLOWED_TOOLS = ",".join([
    "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
])


class ClaudeCodeBackend(AgentBackend):
    def __init__(self, cli_path, runtime_dir, mcp_config_path, env, parent=None):
        super().__init__(runtime_dir, mcp_config_path, env, parent)
        self.cli_path = cli_path
        self.proc = None
        self.parser = StreamJsonParser()
        self._final = None
        self._saw_text_delta = False
        # tool_use id -> subagent name, so we can emit "finished" on its result
        self._pending_subagents = {}

    # -- interface ----------------------------------------------------------
    def is_busy(self):
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def send(self, user_message, context_block):
        if self.is_busy():
            self.error.emit("A turn is already running.")
            return
        self.parser.reset()
        self._final = None
        self._saw_text_delta = False
        self._pending_subagents.clear()

        args = ["-p", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages",
                "--model", config.get(config.K_MODEL_SUPERVISOR),
                "--mcp-config", self.mcp_config_path,
                "--allowedTools", _ALLOWED_TOOLS,
                "--disallowedTools", _DISALLOWED_TOOLS]
        if self.session_id:
            args += ["--resume", self.session_id]

        self.proc = QProcess(self)
        self.proc.setWorkingDirectory(self.runtime_dir)
        self.proc.setProcessEnvironment(self._qenv())
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_proc_error)

        self.busy_changed.emit(True)
        self.proc.start(self.cli_path, args)
        if not self.proc.waitForStarted(5000):
            self.busy_changed.emit(False)
            self.error.emit(f"Could not start Claude CLI at {self.cli_path!r}.")
            self.proc = None
            return

        # Context is prefixed into the user turn (fresh grounding each message,
        # and avoids Windows command-line length limits on --append-system-prompt).
        prompt = user_message
        if context_block:
            prompt = context_block + "\n\n---\n\n" + user_message
        self.proc.write((prompt).encode("utf-8"))
        self.proc.closeWriteChannel()

    def cancel(self):
        if self.is_busy():
            self.proc.kill()

    # -- process plumbing ---------------------------------------------------
    def _qenv(self):
        qenv = QProcessEnvironment.systemEnvironment()
        for key, val in (self.env or {}).items():
            qenv.insert(key, str(val))
        return qenv

    def _on_stdout(self):
        text = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        for evt in self.parser.feed(text):
            self._dispatch(evt)

    def _dispatch(self, evt):
        etype = evt.get("type")
        if etype == "system" and evt.get("session_id"):
            if not self.session_id:
                self.session_id = evt["session_id"]
                self.session_started.emit(self.session_id)
        elif etype == "stream_event":
            inner = evt.get("event") or {}
            delta = inner.get("delta") or {}
            if (inner.get("type") == "content_block_delta"
                    and delta.get("type") == "text_delta"):
                text = delta.get("text", "")
                if text:
                    self._saw_text_delta = True
                    self.token.emit(text)
        elif etype == "assistant":
            for block in evt.get("message", {}).get("content", []):
                if block.get("type") == "text" and self._saw_text_delta:
                    continue
                self._handle_content_block(block)
            self._saw_text_delta = False
        elif etype == "user":
            # tool_result blocks coming back into the main agent
            for block in evt.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self._handle_tool_result(block)
        elif etype == "result":
            self._final = evt

    def _handle_content_block(self, block):
        btype = block.get("type")
        if btype == "text":
            self.token.emit(block.get("text", ""))
        elif btype == "tool_use":
            name = block.get("name", "")
            tool_input = block.get("input", {})
            if name in ("Task", "Agent"):
                sub = tool_input.get("subagent_type") or "subagent"
                self._pending_subagents[block.get("id")] = sub
                self.subagent_event.emit(sub, "started")
            else:
                self.tool_call.emit(name, tool_input)

    def _handle_tool_result(self, block):
        tuid = block.get("tool_use_id")
        if tuid in self._pending_subagents:
            sub = self._pending_subagents.pop(tuid)
            self.subagent_event.emit(sub, "finished")
            return
        content = block.get("content")
        text = _flatten_result_content(content)
        if text:
            self.tool_result.emit(text)

    def _on_finished(self, exit_code, _status):
        self.busy_changed.emit(False)
        stderr = ""
        if self.proc is not None:
            stderr = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        if self._final is not None:
            if self._final.get("is_error"):
                self.error.emit(self._final.get("result") or "Agent reported an error.")
            else:
                self.done.emit(self._final)
        elif exit_code != 0:
            self.error.emit(stderr.strip() or f"Claude CLI exited with code {exit_code}.")
        else:
            self.error.emit("Turn ended without a result event.")
        self.proc = None

    def _on_proc_error(self, err):
        if err == QProcess.FailedToStart:
            self.busy_changed.emit(False)
            self.error.emit(f"Claude CLI failed to start at {self.cli_path!r}.")


def _flatten_result_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts)
    return ""
