# -*- coding: utf-8 -*-
"""Codex CLI backend (fallback, single-agent mode).

Codex has no ``.claude/agents/`` equivalent, so there are no subagents here.
The CLI loads the single-agent Goal Contract and self-verification rules from
``claude_runtime/AGENTS.md``.  Each invocation ignores the user's global Codex
configuration, disables plugin/app MCP injection, and reconstructs only the
live ``qgis`` server from the generated runtime MCP JSON.  ``CODEX_HOME`` is
not relocated, so the user's existing subscription authentication is retained.

One ``QProcess`` per turn: ``codex exec --json`` (first) /
``codex exec resume <thread_id> --json`` (subsequent), prompt on stdin. The
JSON event schema is experimental, so parsing is deliberately tolerant.
"""
import json
import os

from qgis.PyQt.QtCore import QProcess, QProcessEnvironment

from .backend_base import AgentBackend, normalized_usage
from .stream_parser import StreamJsonParser
from .. import config


_QGIS_TOOLS = (
    "get_project_context",
    "execute_pyqgis",
    "run_processing",
    "get_layer_features",
    "render_map_snapshot",
    "stat_path",
    "ask_user",
)


def _toml_literal(value):
    """Encode the small JSON-compatible subset used by CLI ``-c`` values."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[{}]".format(",".join(_toml_literal(item) for item in value))
    if isinstance(value, dict):
        parts = [
            "{} = {}".format(_toml_literal(key), _toml_literal(item))
            for key, item in value.items()
        ]
        return "{{ {} }}".format(", ".join(parts))
    raise TypeError("Unsupported Codex MCP configuration value.")


class CodexBackend(AgentBackend):
    def __init__(self, cli_path, runtime_dir, mcp_config_path, env, parent=None):
        super().__init__(runtime_dir, mcp_config_path, env, parent)
        self.cli_path = cli_path
        self.proc = None
        self.parser = StreamJsonParser()
        self._got_result = False
        self.last_model_id = ""
        self.last_model_was_custom = False

    def is_busy(self):
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def send(self, user_message, context_block, fast_mode=False):
        if self.is_busy():
            self.error.emit("A turn is already running.")
            return
        self.parser.reset()
        self._got_result = False
        self.last_stderr = ""

        model_choice = config.validate_model_choice("codex", "supervisor")
        self.last_model_id = model_choice["model_id"]
        self.last_model_was_custom = model_choice["custom"]
        if model_choice["note"]:
            self.status_note.emit(model_choice["note"])

        try:
            isolation_args, mcp_environment = self._isolation_config()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.last_stderr = (
                "Could not load the live QGIS MCP configuration: {}".format(exc)
            )
            self.error.emit(self.last_stderr)
            return

        effort_args = (
            ["-c", 'model_reasoning_effort="low"'] if fast_mode else []
        )
        if self.session_id:
            args = [
                "exec", "resume", *isolation_args, *effort_args,
                self.session_id,
                "--model", self.last_model_id, "--json",
            ]
        else:
            args = [
                "exec", *isolation_args, *effort_args,
                "--model", self.last_model_id, "--json",
            ]

        self.proc = QProcess(self)
        self.proc.setWorkingDirectory(self.runtime_dir)
        self.proc.setProcessEnvironment(self._qenv(mcp_environment))
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

    def _isolation_config(self):
        """Return isolated CLI args plus secret-safe child environment values."""
        with open(self.mcp_config_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("MCP configuration root must be an object")
        server = (payload.get("mcpServers") or {}).get("qgis")
        if not isinstance(server, dict):
            raise ValueError("missing mcpServers.qgis entry")

        command = server.get("command")
        arguments = server.get("args", [])
        environment = server.get("env", {})
        if not isinstance(command, str) or not command:
            raise ValueError("mcpServers.qgis.command must be a non-empty string")
        if not isinstance(arguments, list) or not all(
                isinstance(item, str) for item in arguments):
            raise ValueError("mcpServers.qgis.args must be a string array")
        if not isinstance(environment, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in environment.items()):
            raise ValueError("mcpServers.qgis.env must contain string values")

        mcp_environment = {
            key: os.environ[key]
            for key in ("PYTHONHOME", "PYTHONPATH")
            if os.environ.get(key)
        }
        mcp_environment.update(environment)

        isolation_args = [
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--disable", "plugins",
            "--disable", "remote_plugin",
            "--disable", "apps",
            "--disable", "enable_mcp_apps",
            "-c", 'approval_policy="never"',
            "-c", "mcp_servers.qgis.command={}".format(
                _toml_literal(command)),
            "-c", "mcp_servers.qgis.args={}".format(
                _toml_literal(arguments)),
            "-c", "mcp_servers.qgis.env_vars={}".format(
                _toml_literal(list(mcp_environment))),
            "-c", "shell_environment_policy.exclude={}".format(
                _toml_literal(list(environment))),
            "-c", "mcp_servers.qgis.enabled=true",
            "-c", "mcp_servers.qgis.required=true",
            "-c", 'mcp_servers.qgis.default_tools_approval_mode="approve"',
            "-c", "mcp_servers.qgis.enabled_tools={}".format(
                _toml_literal(list(_QGIS_TOOLS))),
        ]
        return isolation_args, mcp_environment

    def cancel(self):
        if self.is_busy():
            self.proc.kill()

    def _qenv(self, mcp_environment=None):
        qenv = QProcessEnvironment.systemEnvironment()
        for key, val in (self.env or {}).items():
            qenv.insert(key, str(val))
        for key, val in (mcp_environment or {}).items():
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
            terminal = dict(evt)
            terminal["qgent_usage"] = normalized_usage("codex", evt)
            self.done.emit(terminal)

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
