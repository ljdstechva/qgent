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
import re

from qgis.PyQt.QtCore import QProcess, QProcessEnvironment

from .backend_base import AgentBackend, normalized_usage
from .stream_parser import StreamJsonParser
from .. import config

_ALLOWED_TOOLS = ",".join([
    "mcp__qgis__execute_pyqgis",
    "mcp__qgis__get_project_context",
    "mcp__qgis__run_processing",
    "mcp__qgis__get_layer_features",
    "mcp__qgis__render_map_snapshot",
    "mcp__qgis__stat_path",
    "Task", "TodoWrite", "Read", "Glob", "Grep",
])
_DISALLOWED_TOOLS = ",".join([
    "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
])

_FABLE_FALLBACK_NOTE = (
    "Fable is unavailable on this Claude plan; retrying this turn with Opus."
)
_FABLE_PLAN_REJECTION_PATTERNS = (
    r"\bmodel\b.{0,120}\b(?:not available|unavailable|not supported|"
    r"unsupported|invalid|unknown|not found)\b",
    r"\b(?:not available|unavailable|not supported|unsupported|invalid|"
    r"unknown|not found)\b.{0,80}\bmodel\b",
    r"\b(?:do not|don't|does not|doesn't|cannot|can't|not authorized to)\s+"
    r"(?:have\s+)?access\s+to\s+(?:this|the|that|requested)?\s*model\b",
    r"\bmodel\s+access\s+(?:is\s+)?(?:denied|restricted|unavailable)\b",
    r"\baccess\s+(?:is\s+)?denied\b.{0,80}\b(?:for|to)\s+"
    r"(?:this|the|that|requested)?\s*model\b",
    r"\b(?:not authorized|unauthorized)\b.{0,80}\b(?:use|access)\b"
    r".{0,60}\b(?:this|the|that|requested)?\s*model\b",
    r"\b(?:plan|subscription|account|organization|workspace)\b.{0,120}"
    r"\b(?:does not|doesn't|do not|don't|cannot|can't|has no|lacks?)\b"
    r".{0,100}\b(?:support|include|allow|provide|access)\b.{0,100}\bmodel\b",
)


def is_fable_plan_rejection(message):
    """Recognize model/plan-specific rejection, never generic auth failure."""
    text = str(message or "").lower()
    return any(re.search(pattern, text, re.DOTALL)
               for pattern in _FABLE_PLAN_REJECTION_PATTERNS)


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
        self.last_model_id = ""
        self.last_model_was_custom = False
        self._turn_prompt = ""
        self._turn_session_id = None
        self._attempt_model_id = ""
        self._tentative_session_id = None
        self._fable_fallback_attempted = False
        self._cancel_requested = False

    # -- interface ----------------------------------------------------------
    def is_busy(self):
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def send(self, user_message, context_block):
        if self.is_busy():
            self.error.emit("A turn is already running.")
            return
        model_choice = config.validate_model_choice("claude", "supervisor")
        self.last_model_id = model_choice["model_id"]
        self.last_model_was_custom = model_choice["custom"]
        if model_choice["note"]:
            self.status_note.emit(model_choice["note"])

        # Context is prefixed into the user turn (fresh grounding each message,
        # and avoids Windows command-line length limits on --append-system-prompt).
        prompt = user_message
        if context_block:
            prompt = context_block + "\n\n---\n\n" + user_message
        self._turn_prompt = prompt
        self._turn_session_id = self.session_id
        self._fable_fallback_attempted = False
        self._cancel_requested = False
        self._start_attempt(
            self.last_model_id, self._turn_session_id, announce_busy=True)

    def cancel(self):
        if self.is_busy():
            self._cancel_requested = True
            self.proc.kill()

    # -- process plumbing ---------------------------------------------------
    def _qenv(self):
        qenv = QProcessEnvironment.systemEnvironment()
        for key, val in (self.env or {}).items():
            qenv.insert(key, str(val))
        return qenv

    def _reset_attempt_state(self):
        self.parser.reset()
        self._final = None
        self._saw_text_delta = False
        self._pending_subagents.clear()
        self._tentative_session_id = None
        self.last_stderr = ""

    def _start_attempt(self, model_id, resume_session, announce_busy=False):
        self._reset_attempt_state()
        self._attempt_model_id = str(model_id or "")
        self.last_model_id = self._attempt_model_id
        args = ["-p", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages",
                "--model", self._attempt_model_id,
                "--mcp-config", self.mcp_config_path,
                "--strict-mcp-config",
                "--setting-sources", "project",
                "--settings", '{"env":{"ENABLE_TOOL_SEARCH":"false"}}',
                "--allowedTools", _ALLOWED_TOOLS,
                "--disallowedTools", _DISALLOWED_TOOLS]
        if resume_session:
            args += ["--resume", resume_session]

        proc = QProcess(self)
        proc.setWorkingDirectory(self.runtime_dir)
        proc.setProcessEnvironment(self._qenv())
        proc.readyReadStandardOutput.connect(
            lambda current=proc: self._on_stdout(current))
        proc.finished.connect(
            lambda exit_code, status, current=proc:
            self._on_finished(exit_code, status, current))
        self.proc = proc

        if announce_busy:
            self.busy_changed.emit(True)
        proc.start(self.cli_path, args)
        if not proc.waitForStarted(5000):
            self._retire_process(proc)
            self.busy_changed.emit(False)
            self.last_stderr = f"Could not start Claude CLI at {self.cli_path!r}."
            self.error.emit(self.last_stderr)
            self._clear_turn_state()
            return False
        proc.errorOccurred.connect(
            lambda err, current=proc: self._on_proc_error(err, current))
        proc.write(self._turn_prompt.encode("utf-8"))
        proc.closeWriteChannel()
        return True

    def _retire_process(self, proc):
        if proc is None:
            return
        for signal in (
                proc.readyReadStandardOutput, proc.finished,
                proc.errorOccurred):
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass
        if self.proc is proc:
            self.proc = None
        proc.deleteLater()

    def _clear_turn_state(self):
        self._turn_prompt = ""
        self._turn_session_id = None
        self._attempt_model_id = ""
        self._tentative_session_id = None
        self._fable_fallback_attempted = False
        self._cancel_requested = False

    def _on_stdout(self, proc=None):
        proc = proc or self.proc
        if proc is None or proc is not self.proc:
            return
        text = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
        for evt in self.parser.feed(text):
            self._dispatch(evt)

    def _dispatch(self, evt):
        etype = evt.get("type")
        if etype == "system" and evt.get("session_id"):
            if not self._turn_session_id and not self._tentative_session_id:
                self._tentative_session_id = evt["session_id"]
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
            terminal = dict(evt)
            terminal["qgent_usage"] = normalized_usage("claude", evt)
            self._final = terminal

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

    def _on_finished(self, exit_code, _status, proc=None):
        proc = proc or self.proc
        if proc is None or proc is not self.proc:
            return
        self._on_stdout(proc)
        stderr = ""
        stderr = bytes(proc.readAllStandardError()).decode("utf-8", "replace")
        self.last_stderr = stderr
        final = self._final
        failed = ((final is not None and bool(final.get("is_error")))
                  or (final is None and exit_code != 0))
        failure_text = "\n".join(filter(None, (
            str((final or {}).get("result") or ""), stderr.strip())))
        retry_fable = (
            failed
            and not self._cancel_requested
            and not self._fable_fallback_attempted
            and self._attempt_model_id == "fable"
            and is_fable_plan_rejection(failure_text)
        )
        self._retire_process(proc)

        if retry_fable:
            self._fable_fallback_attempted = True
            self.status_note.emit(_FABLE_FALLBACK_NOTE)
            self._start_attempt(
                "opus", self._turn_session_id, announce_busy=False)
            return

        self.busy_changed.emit(False)
        if final is not None:
            if final.get("is_error"):
                self.error.emit(final.get("result") or "Agent reported an error.")
            else:
                self._commit_tentative_session()
                self.done.emit(final)
        elif exit_code != 0:
            self.error.emit(stderr.strip() or f"Claude CLI exited with code {exit_code}.")
        else:
            self.error.emit("Turn ended without a result event.")
        self._clear_turn_state()

    def _commit_tentative_session(self):
        if not self.session_id and self._tentative_session_id:
            self.session_id = self._tentative_session_id
            self.session_started.emit(self.session_id)

    def _on_proc_error(self, err, proc=None):
        proc = proc or self.proc
        if proc is None or proc is not self.proc:
            return
        if err == QProcess.FailedToStart:
            self._retire_process(proc)
            self.busy_changed.emit(False)
            self.last_stderr = f"Claude CLI failed to start at {self.cli_path!r}."
            self.error.emit(self.last_stderr)
            self._clear_turn_state()


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
