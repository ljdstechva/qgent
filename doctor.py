# -*- coding: utf-8 -*-
"""Live QGIS diagnostics and deterministic, live-safe self-heal actions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

from qgis.PyQt.QtCore import QThread, pyqtSignal

from . import config
from .agent.stream_parser import StreamJsonParser
from .doctor_core import (
    DEFAULT_SOURCE_REPO, command_path as _command_path, compile_tree,
    redact as _redact, run as _run,
)
from .history import HistoryStore
from .model_watch import check_models, summary_text


# Compatibility name retained for Goal 5 callers; values are derived from the
# one Goal 6 catalog in config.py so chat and repair lists cannot diverge.
REPAIR_MODEL_SPECS = config.repair_model_specs()
STALE_SESSION_PATTERNS = (
    "session not found", "session id not found", "no session found",
    "no conversation found", "conversation not found", "thread not found",
    "unknown session", "invalid session id", "failed to resume",
    "could not resume", "session does not exist", "thread does not exist",
)

def _check_record(check_id, name, ok, detail, repairable=False):
    return {
        "id": check_id,
        "name": name,
        "ok": bool(ok),
        "status": "PASS" if ok else "FAIL",
        "detail": str(detail),
        "repairable": bool(repairable),
    }


def repair_model_options(cli_paths=None):
    """Return repair models whose owning CLI is currently available."""
    cli_paths = dict(cli_paths or {})
    resolved = {
        "claude": _command_path(
            cli_paths.get("claude") or config.detect_claude()),
        "codex": _command_path(
            cli_paths.get("codex") or config.detect_codex()),
    }
    options = []
    for spec in REPAIR_MODEL_SPECS:
        backend = spec["backend"]
        if not resolved.get(backend):
            continue
        if spec["model"] not in config.accepted_model_ids(backend):
            continue
        item = dict(spec)
        item["cli_path"] = resolved[backend]
        options.append(item)
    return options


class DoctorService:
    """Run checks and only the enumerated deterministic remedies."""

    def __init__(self, context):
        self.context = dict(context or {})

    def _value(self, key, default=None):
        value = self.context.get(key, default)
        return value() if callable(value) else value

    def _callback(self, key):
        value = self.context.get(key)
        return value if callable(value) else None

    def _plugin_dir(self):
        return Path(self._value("plugin_dir", "")).resolve()

    def _profile_dir(self):
        return Path(self._value("profile_dir", "")).resolve()

    def _history_path(self):
        explicit = self._value("history_path", "")
        if explicit:
            return Path(explicit).resolve()
        project_filename = self._value("project_filename", "")
        return HistoryStore(self._profile_dir(), project_filename).path

    def _codex_config_path(self):
        explicit = self._value("codex_config_path", "")
        if explicit:
            return Path(explicit).resolve()
        return Path(os.path.expanduser("~/.codex/config.toml")).resolve()

    def _configured_cli(self, backend):
        key = config.K_CLAUDE_PATH if backend == "claude" else config.K_CODEX_PATH
        stored = str(config.get(key) or "").strip()
        names = (("claude", "claude.cmd", "claude.exe") if backend == "claude"
                 else ("codex", "codex.cmd", "codex.exe"))
        detected = next((shutil.which(name) for name in names
                         if shutil.which(name)), "")
        return stored, _command_path(stored), detected

    def _check_cli(self, backend):
        stored, configured, detected = self._configured_cli(backend)
        name = "Claude CLI" if backend == "claude" else "Codex CLI"
        if stored and not configured:
            return _check_record(
                f"cli_{backend}", name, False,
                f"Configured path is invalid: {stored!r}. "
                f"Autodetection candidate: {detected or 'none'}.", True)
        path = configured or detected
        if not path:
            return _check_record(
                f"cli_{backend}", name, False, "CLI not found on PATH.", True)
        version = _run([path, "--version"], timeout=20)
        if version.returncode != 0:
            return _check_record(
                f"cli_{backend}", name, False,
                f"Version probe failed: {(version.stderr or version.stdout).strip()}",
                True)
        if backend == "claude":
            auth = _run([path, "auth", "status"], timeout=20)
            auth_ok = False
            auth_detail = ""
            try:
                payload = json.loads(auth.stdout)
                auth_ok = auth.returncode == 0 and payload.get("loggedIn") is True
                auth_detail = (
                    f"loggedIn={bool(payload.get('loggedIn'))}, "
                    f"method={payload.get('authMethod') or 'unknown'}, "
                    f"subscription={payload.get('subscriptionType') or 'unknown'}")
            except (json.JSONDecodeError, TypeError):
                auth_detail = (auth.stderr or auth.stdout).strip()
        else:
            auth = _run([path, "login", "status"], timeout=20)
            auth_text = (auth.stdout + "\n" + auth.stderr).strip()
            auth_ok = auth.returncode == 0 and "logged in" in auth_text.lower()
            auth_detail = auth_text.splitlines()[0] if auth_text else "no output"
        ok = version.returncode == 0 and auth_ok
        detail = (
            f"path={path}; version={(version.stdout or version.stderr).strip()}; "
            f"auth={auth_detail}")
        return _check_record(f"cli_{backend}", name, ok, detail, True)

    def _check_models(self):
        """Report models the installed CLIs know about but QGent does not list.

        Informational: a newer model shipping is news, not a broken install, so
        this never fails the run.
        """
        try:
            report = check_models(self._profile_dir(), {
                "claude": self._configured_cli("claude")[1] or config.detect_claude(),
                "codex": self._configured_cli("codex")[1] or config.detect_codex(),
            })
            detail = summary_text(report)
        except Exception as exc:
            detail = f"Model check failed: {type(exc).__name__}: {exc}"
        return _check_record("models", "Model catalogue freshness", True, detail)

    def _check_python(self):
        path = _command_path(self._value("python_executable", "")
                             or config.python_executable())
        if not path:
            return _check_record(
                "python", "Python executable", False, "No executable found.")
        probe = _run([path, "--version"], timeout=20)
        detail = f"path={path}; {(probe.stdout or probe.stderr).strip()}"
        return _check_record(
            "python", "Python executable", probe.returncode == 0, detail)

    def _check_mcp_config(self):
        path = Path(self._value(
            "mcp_config_path", self._plugin_dir() / "claude_runtime" /
            "mcp-config.json")).resolve()
        if not path.is_file():
            return _check_record(
                "mcp_config", "Claude MCP config", False,
                f"Missing: {path}", False)
        try:
            stat = path.stat()
            ok = stat.st_size > 0
            detail = (
                f"Protected MCP config is present ({stat.st_size} bytes); "
                "Doctor did not read its contents.")
        except OSError as exc:
            ok = False
            detail = f"Could not stat protected MCP config: {type(exc).__name__}: {exc}"
        return _check_record(
            "mcp_config", "Claude MCP config", ok, detail, False)

    def _check_codex_config(self):
        path = self._codex_config_path()
        try:
            text = path.read_text(encoding="utf-8")
            ok = ("[mcp_servers.qgis]" in text
                  and "QGIS_COPILOT_PORT" in text
                  and "QGIS_COPILOT_TOKEN" in text)
            detail = (f"qgis MCP entry present in {path}." if ok else
                      f"qgis MCP entry missing or incomplete in {path}.")
        except (OSError, UnicodeError) as exc:
            ok = False
            detail = f"Unreadable {path}: {type(exc).__name__}: {exc}"
        return _check_record(
            "codex_config", "Codex MCP config", ok, detail, True)

    def _bridge_environment(self):
        value = self.context.get("bridge_env", {})
        return dict(value() if callable(value) else value or {})

    def _check_bridge(self):
        py = _command_path(self._value("python_executable", "")
                           or config.python_executable())
        script = self._plugin_dir() / "bridge" / "mcp_stdio_bridge.py"
        env_values = self._bridge_environment()
        if not py or not script.is_file() or not env_values:
            return _check_record(
                "bridge", "MCP bridge round trip", False,
                "Python, bridge script, or live socket environment is missing.",
                False)
        env = os.environ.copy()
        env.update({str(key): str(value) for key, value in env_values.items()})
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "get_project_context", "arguments": {}}},
        ]
        stdin_text = "".join(json.dumps(item) + "\n" for item in requests)
        try:
            result = _run(
                [py, str(script)], cwd=self._plugin_dir(), env=env, timeout=30,
                stdin_text=stdin_text)
            messages = [json.loads(line) for line in result.stdout.splitlines()
                        if line.strip()]
            reply = next((item for item in messages if item.get("id") == 2), {})
            content = ((reply.get("result") or {}).get("content") or [{}])[0]
            payload = json.loads(content.get("text") or "{}")
            ok = (result.returncode == 0
                  and not (reply.get("result") or {}).get("isError")
                  and bool(payload.get("project_crs")))
            detail = (
                f"get_project_context returned project_crs="
                f"{payload.get('project_crs') or 'missing'}."
                if ok else
                f"Bridge failed: {(result.stderr or result.stdout).strip()}")
        except Exception as exc:
            ok = False
            detail = f"Bridge probe failed: {type(exc).__name__}: {exc}"
        return _check_record(
            "bridge", "MCP bridge round trip", ok, detail, False)

    def _check_stream(self):
        stored, configured, detected = self._configured_cli("claude")
        path = configured or (detected if not stored else "")
        if not path:
            return _check_record(
                "stream", "Claude stream seam", False,
                "Claude CLI unavailable for stream probe.")
        command = [
            path, "-p", "--safe-mode", "--tools", "",
            "--no-session-persistence", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages", "--model", "haiku",
        ]
        try:
            result = _run(
                command, cwd=self._plugin_dir(), timeout=90,
                stdin_text="Reply exactly OK and nothing else.")
            parser = StreamJsonParser()
            events = parser.feed(result.stdout + (
                "" if result.stdout.endswith("\n") else "\n"))
            final = next((event for event in reversed(events)
                          if event.get("type") == "result"), {})
            ok = (result.returncode == 0
                  and final.get("subtype") == "success"
                  and final.get("is_error") is False
                  and final.get("result") == "OK")
            detail = (
                f"Parsed {len(events)} JSONL events; result=OK."
                if ok else
                f"Stream probe failed: {(result.stderr or result.stdout).strip()}")
        except Exception as exc:
            ok = False
            detail = f"Stream probe failed: {type(exc).__name__}: {exc}"
        return _check_record("stream", "Claude stream seam", ok, detail)

    def _check_bundled_files(self):
        root = self._plugin_dir() / "claude_runtime" / ".claude"
        required = [
            root / "agents" / f"{name}.md" for name in (
                "cartographer", "data-scout", "geoprocessor", "qa-verifier")]
        required += [
            root / "skills" / name / "SKILL.md" for name in (
                "cartography-print-layout", "ph-environmental-maps",
                "processing-recipes", "pyqgis-patterns")]
        missing = [str(path.relative_to(self._plugin_dir()))
                   for path in required if not path.is_file()]
        return _check_record(
            "bundled_files", "Bundled skills and agents", not missing,
            "All 8 required files are present." if not missing else
            "Missing: " + ", ".join(missing))

    def _check_history_writable(self):
        directory = self._history_path().parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="doctor-write-", dir=str(directory))
            os.close(fd)
            os.unlink(name)
            ok = True
            detail = f"Writable: {directory}"
        except OSError as exc:
            ok = False
            detail = f"Not writable: {type(exc).__name__}: {exc}"
        return _check_record(
            "history_writable", "History directory writable", ok, detail)

    def _check_history_integrity(self):
        path = self._history_path()
        if not path.exists():
            return _check_record(
                "history_integrity", "Current history integrity", True,
                f"No active history file at {path}.", True)
        try:
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            for index, line in enumerate(lines):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    if index == len(lines) - 1 and not text.endswith("\n"):
                        break
                    raise
                if not isinstance(value, dict) or not value.get("kind"):
                    raise ValueError(f"line {index + 1} lacks kind")
            ok = True
            detail = f"Valid JSONL: {path}"
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            ok = False
            detail = f"Corrupt {path}: {type(exc).__name__}: {exc}"
        return _check_record(
            "history_integrity", "Current history integrity", ok, detail, True)

    def _cache_items(self):
        root = self._plugin_dir()
        items = [path for path in root.rglob("__pycache__") if path.is_dir()]
        items += [path for path in root.rglob("*.pyc")
                  if "__pycache__" not in path.parts]
        return items

    def _check_cache_clean(self):
        items = self._cache_items()
        return _check_record(
            "cache_clean", "Installed bytecode cache cleanliness", not items,
            "No plugin-local caches." if not items else
            f"Found {len(items)} cache item(s).", True)

    def _check_compile(self):
        root = self._plugin_dir()
        try:
            result = compile_tree(root)
            ok = result["ok"]
            detail = (
                f"In-process compileall passed {result['python_files']} "
                f"Python files." if ok else
                f"Compile failed: {result['stderr'] or result['stdout']}")
        except Exception as exc:
            ok = False
            detail = f"Compile failed: {type(exc).__name__}: {exc}"
        return _check_record("compile", "Installed tree byte-compiles", ok, detail)

    def _check_logs(self):
        entries = list(self._value("log_entries", []) or [])[-200:]
        return _check_record(
            "logs", "Recent plugin-related QGIS log entries", True,
            f"Captured {len(entries)} matching entr{'y' if len(entries) == 1 else 'ies'}."), entries

    def _check_session(self):
        session_id = str(self._value("session_id", "") or "")
        stderr = str(self._value("last_cli_stderr", "") or "")
        lower = stderr.lower()
        stale = bool(session_id and any(pattern in lower
                                        for pattern in STALE_SESSION_PATTERNS))
        detail = (
            f"Stored session {session_id[:8]} is paired with a resume failure."
            if stale else
            (f"Stored session {session_id[:8]} has no stale marker."
             if session_id else "No stored session id."))
        return _check_record(
            "session", "Stored CLI session", not stale, detail, True)

    def run_diagnostics(self):
        checks = []
        logs = []
        for method in (
                lambda: self._check_cli("claude"),
                lambda: self._check_cli("codex"),
                self._check_models,
                self._check_python,
                self._check_mcp_config,
                self._check_codex_config,
                self._check_bridge,
                self._check_stream,
                self._check_bundled_files,
                self._check_history_writable,
                self._check_history_integrity,
                self._check_cache_clean,
                self._check_compile,
                self._check_session):
            try:
                checks.append(method())
            except Exception as exc:
                checks.append(_check_record(
                    "internal", "Internal diagnostics error", False,
                    f"{type(exc).__name__}: {exc}"))
        try:
            log_check, logs = self._check_logs()
            checks.append(log_check)
        except Exception as exc:
            checks.append(_check_record(
                "logs", "Recent plugin-related QGIS log entries", False,
                f"{type(exc).__name__}: {exc}"))

        bridge_token = self._bridge_environment().get("QGIS_COPILOT_TOKEN", "")
        lines = ["QGent Doctor diagnostics"]
        for item in checks:
            lines.append(
                f"[{item['status']}] {item['name']}: {item['detail']}")
        lines.append("\nRecent plugin-related QGIS log entries:")
        if logs:
            for entry in logs[-200:]:
                if isinstance(entry, dict):
                    lines.append(
                        f"- {entry.get('t', '')} [{entry.get('tag', '')}] "
                        f"{entry.get('message', '')}")
                else:
                    lines.append(f"- {entry}")
        else:
            lines.append("- none captured")
        lines.append("\nLast CLI stderr:")
        lines.append(str(self._value("last_cli_stderr", "") or "(empty)"))
        bundle = _redact("\n".join(lines), secrets=(bridge_token,))
        return {
            "all_pass": all(item["ok"] for item in checks),
            "checks": checks,
            "bundle": bundle,
        }

    def clear_bytecode_caches(self):
        root = self._plugin_dir()
        directories = sorted(
            [path.resolve() for path in root.rglob("__pycache__")
             if path.is_dir()], key=lambda path: len(path.parts), reverse=True)
        loose = [path.resolve() for path in root.rglob("*.pyc")
                 if "__pycache__" not in path.parts]
        if not all(path.is_relative_to(root) and path.name == "__pycache__"
                   for path in directories):
            raise RuntimeError("Refusing invalid cache directory target")
        if not all(path.is_relative_to(root) for path in loose):
            raise RuntimeError("Refusing invalid bytecode target")
        for path in directories:
            shutil.rmtree(path)
        for path in loose:
            path.unlink(missing_ok=True)
        return len(directories) + len(loose)

    def auto_repair(self, report):
        """Apply only contract-enumerated remedies for matching failed checks."""
        failed = {item.get("id") for item in (report or {}).get("checks", [])
                  if not item.get("ok") and item.get("repairable")}
        actions = []
        for backend, check_id, key, detector in (
                ("Claude", "cli_claude", config.K_CLAUDE_PATH,
                 config.detect_claude),
                ("Codex", "cli_codex", config.K_CODEX_PATH,
                 config.detect_codex)):
            if check_id in failed:
                config.set(key, "")
                detected = detector()
                actions.append(
                    f"Re-ran {backend} CLI path autodetection: "
                    f"{detected or 'not found'}")

        if "codex_config" in failed:
            callback = self._callback("regenerate_codex_config")
            if callback:
                callback()
                actions.append("Regenerated the Codex MCP configuration.")
        if "cache_clean" in failed:
            removed = self.clear_bytecode_caches()
            actions.append(f"Cleared {removed} plugin bytecode cache item(s).")
        if "history_integrity" in failed:
            callback = self._callback("repair_history")
            if callback:
                callback()
                actions.append("Quarantined the corrupt history file.")
        if "session" in failed:
            callback = self._callback("clear_session")
            if callback:
                callback()
                actions.append("Cleared the stale stored session id.")
        if not actions:
            actions.append("No matching deterministic repair was available.")
        return actions


class DoctorWorker(QThread):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service

    def run(self):
        try:
            self.completed.emit(self.service.run_diagnostics())
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ModelWatchWorker(QThread):
    """Scan the CLIs for unknown models off the UI thread (~2 s per binary)."""

    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, profile_dir, parent=None):
        super().__init__(parent)
        self.profile_dir = str(profile_dir)

    def run(self):
        try:
            self.completed.emit(check_models(self.profile_dir, {
                "claude": config.detect_claude(),
                "codex": config.detect_codex(),
            }))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
