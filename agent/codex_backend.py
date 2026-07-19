# -*- coding: utf-8 -*-
"""Codex CLI backend (fallback, single-agent mode).

Codex has no ``.claude/agents/`` equivalent, so there are no subagents here: the
Goal Contract + mandatory self-verification checklist are injected into the
prompt instead (see context/project_snapshot.py and CLAUDE.md notes). MCP
servers are registered in ``~/.codex/config.toml`` (the dock writes the entry),
not via a CLI flag.

One ``QProcess`` per turn: ``codex exec --json`` (first) /
``codex exec resume <thread_id> --json`` (subsequent), prompt on stdin. The
JSON event schema is experimental, so parsing is deliberately tolerant.
"""
from qgis.PyQt.QtCore import QProcess, QProcessEnvironment

from .backend_base import AgentBackend
from .stream_parser import StreamJsonParser


class CodexBackend(AgentBackend):
    def __init__(self, cli_path, runtime_dir, mcp_config_path, env, parent=None):
        super().__init__(runtime_dir, mcp_config_path, env, parent)
        self.cli_path = cli_path
        self.proc = None
        self.parser = StreamJsonParser()
        self._got_result = False

    def is_busy(self):
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def send(self, user_message, context_block):
        if self.is_busy():
            self.error.emit("A turn is already running.")
            return
        self.parser.reset()
        self._got_result = False
        self.last_stderr = ""

        if self.session_id:
            args = ["exec", "resume", self.session_id, "--json"]
        else:
            args = ["exec", "--json"]

        self.proc = QProcess(self)
        self.proc.setWorkingDirectory(self.runtime_dir)
        self.proc.setProcessEnvironment(self._qenv())
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.finished.connect(self._on_finished)

        self.busy_changed.emit(True)
        self.proc.start(self.cli_path, args)
        if not self.proc.waitForStarted(5000):
            self.busy_changed.emit(False)
            self.last_stderr = f"Could not start Codex CLI at {self.cli_path!r}."
            self.error.emit(self.last_stderr)
            self.proc = None
            return

        prompt = user_message
        if context_block:
            prompt = context_block + "\n\n---\n\n" + user_message
        self.proc.write(prompt.encode("utf-8"))
        self.proc.closeWriteChannel()

    def cancel(self):
        if self.is_busy():
            self.proc.kill()

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
        etype = evt.get("type", "")
        # Thread/session id.
        if etype == "thread.started" and evt.get("thread_id"):
            if not self.session_id:
                self.session_id = evt["thread_id"]
                self.session_started.emit(self.session_id)
            return
        if etype in ("item.completed", "item.updated"):
            item = evt.get("item", {})
            itype = item.get("type") or item.get("item_type")
            if itype in ("assistant_message", "agent_message"):
                text = item.get("text") or item.get("content") or ""
                if text:
                    self.token.emit(text if text.endswith("\n") else text + "\n")
            elif itype in ("mcp_tool_call", "tool_call", "command_execution"):
                name = item.get("tool") or item.get("name") or itype
                self.tool_call.emit(name, item.get("arguments") or item.get("input") or {})
                out = item.get("output") or item.get("result")
                if out:
                    self.tool_result.emit(str(out))
            return
        if etype in ("turn.completed", "thread.completed"):
            self._got_result = True
            self.done.emit(evt)

    def _on_finished(self, exit_code, _status):
        self.busy_changed.emit(False)
        stderr = ""
        if self.proc is not None:
            stderr = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        self.last_stderr = stderr
        if not self._got_result:
            if exit_code != 0:
                self.error.emit(stderr.strip() or f"Codex CLI exited with code {exit_code}.")
            else:
                self.done.emit({"result": ""})
        self.proc = None
