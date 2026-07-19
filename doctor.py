# -*- coding: utf-8 -*-
"""Diagnostics, deterministic repairs, and review-gated repair workspaces."""
from __future__ import annotations

import compileall
from datetime import datetime
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from qgis.PyQt.QtCore import QThread, pyqtSignal

from . import config
from .agent.stream_parser import StreamJsonParser
from .history import HistoryStore, clipped_text


DEFAULT_SOURCE_REPO = Path(r"D:\11 QGIS Agent\qgis_chat_agent")
PROTECTED_RELATIVE = "claude_runtime/mcp-config.json"
REPAIR_MODEL_SPECS = (
    {
        "backend": "codex",
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "label": "Codex · gpt-5.6-sol · xhigh",
    },
    {
        "backend": "claude",
        "model": "claude-opus-4-8",
        "effort": "max",
        "label": "Claude · opus-4.8 (claude-opus-4-8)",
    },
    {
        "backend": "claude",
        "model": "claude-fable-5",
        "effort": "max",
        "label": "Claude · fable-5 (claude-fable-5)",
    },
)
STALE_SESSION_PATTERNS = (
    "session not found", "session id not found", "no session found",
    "no conversation found", "conversation not found", "thread not found",
    "unknown session", "invalid session id", "failed to resume",
    "could not resume", "session does not exist", "thread does not exist",
)


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _command_path(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if os.path.isfile(text):
        return os.path.abspath(text)
    return shutil.which(text) or ""


def _run(command, cwd=None, env=None, timeout=30, stdin_text=None):
    command = list(command)
    if (os.name == "nt" and command
            and Path(command[0]).name.lower() in {"git", "git.exe"}):
        git_executable = (Path(
            os.environ.get("ProgramFiles", r"C:\Program Files")) /
            "Git" / "cmd" / "git.exe")
        if git_executable.is_file():
            command[0] = str(git_executable)
    run_env = dict(os.environ if env is None else env)
    if os.name == "nt" and command:
        cli_dir = str(Path(command[0]).resolve().parent)
        node_dir = str(Path(
            os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs")
        git_dir = str(Path(
            os.environ.get("ProgramFiles", r"C:\Program Files")) /
            "Git" / "cmd")
        current_path = run_env.get("PATH", run_env.get("Path", ""))
        additions = [path for path in (cli_dir, node_dir, git_dir)
                     if Path(path).is_dir()]
        run_env["PATH"] = os.pathsep.join(additions + [current_path])
    return subprocess.run(
        command, cwd=str(cwd) if cwd else None, env=run_env,
        input=stdin_text, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, check=False)


def _redact(text, secrets=()):
    value = str(text or "")
    for secret in secrets:
        if secret:
            value = value.replace(str(secret), "[REDACTED]")
    value = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]", value)
    return clipped_text(value, 12000)


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
        if spec["model"] not in config.model_ids(backend):
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
                f"Missing: {path}", True)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            server = payload["mcpServers"]["qgis"]
            ok = bool(server.get("command") and server.get("args")
                      and server.get("env"))
            detail = f"Valid qgis MCP entry at {path}."
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            ok = False
            detail = f"Invalid {path}: {type(exc).__name__}: {exc}"
        return _check_record(
            "mcp_config", "Claude MCP config", ok, detail, True)

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
                True)
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
            "bridge", "MCP bridge round trip", ok, detail, True)

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
        temp_root = Path(tempfile.mkdtemp(prefix="qgent-doctor-compile-"))
        previous = getattr(sys, "pycache_prefix", None)
        try:
            sys.pycache_prefix = str(temp_root)
            ok = bool(compileall.compile_dir(
                str(root), quiet=2, force=True, maxlevels=20))
            count = sum(1 for _ in root.rglob("*.py"))
            detail = f"In-process compileall passed {count} Python files."
        except Exception as exc:
            ok = False
            detail = f"Compile failed: {type(exc).__name__}: {exc}"
        finally:
            sys.pycache_prefix = previous
            shutil.rmtree(temp_root, ignore_errors=True)
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
                  if not item.get("ok")}
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

        restarted = False
        if "bridge" in failed:
            callback = self._callback("restart_bridge")
            if callback:
                callback()
                restarted = True
                actions.append("Restarted the bridge socket server.")
        if ({"mcp_config", "codex_config"} & failed) or restarted:
            callback = self._callback("regenerate_configs")
            if callback:
                callback()
                actions.append("Regenerated Claude and Codex MCP configuration.")
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

    def backups(self):
        root = self._profile_dir() / "qgent" / "backups"
        if not root.is_dir():
            return []
        return [path for path in sorted(root.iterdir(), reverse=True)
                if path.is_dir() and (path / "manifest.json").is_file()]

    def restore_backup(self, backup_dir):
        return RepairWorkspace.restore_backup(
            backup_dir,
            self._plugin_dir(),
            Path(self._value("source_repo", DEFAULT_SOURCE_REPO)).resolve(),
            self._profile_dir(),
        )


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


class DoctorActionWorker(QThread):
    """Run an approved apply/restore action without blocking the dialog."""
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, action, parent=None):
        super().__init__(parent)
        self.action = action

    def run(self):
        try:
            self.completed.emit(self.action())
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def _ignored_relative(relative):
    relative = str(relative).replace("\\", "/").lstrip("/")
    parts = Path(relative).parts
    return (relative == PROTECTED_RELATIVE
            or ".git" in parts
            or "__pycache__" in parts
            or relative.endswith((".pyc", ".pyo")))


def _tree_manifest(root):
    root = Path(root).resolve()
    result = {}
    if not root.is_dir():
        return result
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if _ignored_relative(relative):
            continue
        result[relative] = _sha256(path)
    return result


def _guard_tree_manifest(root):
    """Guard real-tree bytes and metadata, including protected MCP config."""
    root = Path(root).resolve()
    result = {}
    if not root.is_dir():
        return result
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        parts = Path(relative).parts
        if (".git" in parts or "__pycache__" in parts
                or relative.endswith((".pyc", ".pyo"))):
            continue
        stat = path.stat()
        result[relative] = {
            "sha256": _sha256(path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    return result


def _copy_ignore(_directory, names):
    return {name for name in names
            if name in {".git", "__pycache__", "mcp-config.json"}
            or name.endswith((".pyc", ".pyo"))}


def _safe_target(root, relative):
    root = Path(root).resolve()
    text = str(relative).replace("\\", "/").lstrip("/")
    if not text or _ignored_relative(text):
        raise ValueError(f"Protected or invalid repair path: {relative!r}")
    target = (root / Path(text)).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"Repair path escapes target root: {relative!r}")
    return target


def _text_diff(relative, before_path, after_path):
    before = before_path.read_bytes() if before_path.is_file() else b""
    after = after_path.read_bytes() if after_path.is_file() else b""
    try:
        before_text = before.decode("utf-8").splitlines(keepends=True)
        after_text = after.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return f"Binary file changed: {relative}\n"
    return "".join(difflib.unified_diff(
        before_text, after_text,
        fromfile=f"installed/{relative}", tofile=f"proposal/{relative}"))


class RepairWorkspace:
    """Disposable proposal tree plus explicit approve/deny/restore operations."""

    def __init__(self, installed_tree, source_repo, profile_dir):
        self.installed_tree = Path(installed_tree).resolve()
        self.source_repo = Path(source_repo).resolve() if source_repo else None
        self.profile_dir = Path(profile_dir).resolve()
        self.temp_root = None
        self.copy_root = None
        self._before = None
        self._installed_guard = None
        self._source_guard = None
        self._source_head = None
        self._proposal_guard = None
        self.proposal = None

    def prepare(self):
        if not self.installed_tree.is_dir():
            raise FileNotFoundError(f"Installed plugin tree missing: {self.installed_tree}")
        self.temp_root = Path(tempfile.mkdtemp(prefix="qgent-doctor-repair-"))
        self.copy_root = self.temp_root / "qgis_chat_agent"
        self._installed_guard = _guard_tree_manifest(self.installed_tree)
        self._source_guard = (
            _guard_tree_manifest(self.source_repo)
            if self.source_repo and self.source_repo.is_dir() else None)
        if (self.source_repo and self.source_repo.is_dir()
                and (self.source_repo / ".git").is_dir()):
            status = _run(
                ["git", "status", "--porcelain"], cwd=self.source_repo)
            if status.returncode != 0:
                raise RuntimeError(
                    (status.stderr or status.stdout).strip()
                    or "Could not inspect source repository state")
            if status.stdout.strip():
                raise RuntimeError(
                    "Source repository is not clean; refusing repair proposal")
            head = _run(["git", "rev-parse", "HEAD"], cwd=self.source_repo)
            if head.returncode != 0 or not head.stdout.strip():
                raise RuntimeError(
                    (head.stderr or head.stdout).strip()
                    or "Could not resolve source repository HEAD")
            self._source_head = head.stdout.strip()
        shutil.copytree(self.installed_tree, self.copy_root, ignore=_copy_ignore)
        self._before = _tree_manifest(self.installed_tree)
        if (self.copy_root / PROTECTED_RELATIVE).exists():
            raise RuntimeError("Protected MCP config entered the repair copy")
        # Codex's Windows workspace-write policy derives its writable root
        # from a repository.  The installed plugin is not itself a checkout,
        # so give the disposable copy an uncommitted, disposable repository.
        # The .git directory is excluded from proposals and is never copied
        # back to either real tree.
        initialized = _run(["git", "init", "-q"], cwd=self.copy_root)
        if initialized.returncode != 0:
            raise RuntimeError(
                "Could not initialize the disposable repair repository: "
                + (initialized.stderr or initialized.stdout).strip())
        return self.copy_root

    def build_proposal(self, explanation):
        if self.copy_root is None or self._before is None:
            raise RuntimeError("Repair workspace was not prepared")
        self._validate_real_tree_guards("during proposal generation", "review")
        after = _tree_manifest(self.copy_root)
        self._proposal_guard = after
        changes = []
        diff_parts = []
        for relative in sorted(set(self._before) | set(after)):
            if self._before.get(relative) == after.get(relative):
                continue
            action = ("add" if relative not in self._before else
                      "delete" if relative not in after else "modify")
            changes.append({
                "path": relative,
                "action": action,
                "before_sha256": self._before.get(relative),
                "after_sha256": after.get(relative),
            })
            diff_parts.append(_text_diff(
                relative, self.installed_tree / relative,
                self.copy_root / relative))
        self.proposal = {
            "workspace": self,
            "workspace_path": str(self.copy_root),
            "changes": changes,
            "diff": "".join(diff_parts) or "(no file changes)",
            "explanation": str(explanation or "").strip(),
        }
        return self.proposal

    def _current_source_head(self):
        if self._source_head is None:
            return None
        status = _run(["git", "status", "--porcelain"], cwd=self.source_repo)
        if status.returncode != 0 or status.stdout.strip():
            return ""
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.source_repo)
        return head.stdout.strip() if head.returncode == 0 else ""

    def _validate_real_tree_guards(self, phase, action):
        if _guard_tree_manifest(self.installed_tree) != self._installed_guard:
            raise RuntimeError(
                f"Installed tree changed {phase}; refusing {action}")
        if (self._source_guard is not None
                and _guard_tree_manifest(self.source_repo) != self._source_guard):
            raise RuntimeError(f"Source tree changed {phase}; refusing {action}")
        if (self._source_head is not None
                and self._current_source_head() != self._source_head):
            raise RuntimeError(
                f"Source Git state changed {phase}; refusing {action}")

    def _validate_review_baseline(self):
        self._validate_real_tree_guards("after review began", "apply")
        if _tree_manifest(self.copy_root) != self._proposal_guard:
            raise RuntimeError(
                "Disposable proposal changed after review began; refusing apply")

    def _verify_change_prestate(self, change):
        relative = change["path"]
        installed = _safe_target(self.installed_tree, relative)
        installed_hash = _sha256(installed) if installed.is_file() else None
        if installed_hash != change.get("before_sha256"):
            raise RuntimeError(
                f"Installed pre-state changed after review: {relative}")
        if self._source_guard is not None:
            source = _safe_target(self.source_repo, relative)
            source_hash = _sha256(source) if source.is_file() else None
            source_entry = self._source_guard.get(relative)
            expected_source = (
                source_entry.get("sha256") if source_entry is not None else None)
            if source_hash != expected_source:
                raise RuntimeError(
                    f"Source pre-state changed after review: {relative}")
        proposed = _safe_target(self.copy_root, relative)
        proposed_hash = _sha256(proposed) if proposed.is_file() else None
        if proposed_hash != change.get("after_sha256"):
            raise RuntimeError(
                f"Proposed file changed after review: {relative}")

    def _git_clean(self):
        if not self.source_repo or not (self.source_repo / ".git").exists():
            return True
        result = _run(["git", "status", "--porcelain"], cwd=self.source_repo)
        return result.returncode == 0 and not result.stdout.strip()

    def _backup_target(self, backup_dir, label, root, relative):
        target = _safe_target(root, relative)
        entry = {
            "root": label,
            "path": relative,
            "existed": target.is_file(),
            "sha256": _sha256(target) if target.is_file() else None,
        }
        if target.is_file():
            backup_path = _safe_target(backup_dir / label, relative)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup_path)
        return entry

    @staticmethod
    def _apply_one(source_copy, target_root, relative):
        destination = _safe_target(target_root, relative)
        proposed = _safe_target(source_copy, relative)
        if proposed.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(proposed, destination)
        else:
            destination.unlink(missing_ok=True)

    @staticmethod
    def _commit_source(source_repo, paths, message):
        source_repo = Path(source_repo).resolve()
        if not (source_repo / ".git").is_dir():
            return ""
        normalized = [str(path).replace("\\", "/") for path in paths]
        add = _run(["git", "add", "-A", "--", *normalized], cwd=source_repo)
        if add.returncode != 0:
            raise RuntimeError(
                (add.stderr or add.stdout).strip()
                or f"Git add failed with exit code {add.returncode}")
        status = _run(["git", "status", "--porcelain"], cwd=source_repo)
        if status.returncode != 0:
            raise RuntimeError(
                (status.stderr or status.stdout).strip()
                or f"Git status failed with exit code {status.returncode}")
        if not status.stdout.strip():
            return ""
        commit = _run(["git", "commit", "-m", message], cwd=source_repo, timeout=60)
        if commit.returncode != 0:
            raise RuntimeError(
                (commit.stderr or commit.stdout).strip()
                or f"Git commit failed with exit code {commit.returncode}")
        revision = _run(["git", "rev-parse", "HEAD"], cwd=source_repo)
        if revision.returncode != 0 or not revision.stdout.strip():
            raise RuntimeError(
                (revision.stderr or revision.stdout).strip()
                or "Git commit succeeded but HEAD could not be resolved")
        return revision.stdout.strip()

    def apply(self):
        if not self.proposal or not self.proposal.get("changes"):
            raise RuntimeError("There is no proposed file change to approve")
        self._validate_review_baseline()
        if self.source_repo and self.source_repo.is_dir() and not self._git_clean():
            raise RuntimeError("Source repository is not clean; refusing to mix changes")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_dir = self.profile_dir / "qgent" / "backups" / stamp
        backup_dir.mkdir(parents=True, exist_ok=False)
        manifest = {
            "version": 1,
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": "pending",
            "installed_tree": str(self.installed_tree),
            "source_repo": str(self.source_repo or ""),
            "changes": self.proposal["changes"],
            "prestate": [],
        }
        for change in self.proposal["changes"]:
            self._verify_change_prestate(change)
            relative = change["path"]
            manifest["prestate"].append(self._backup_target(
                backup_dir, "installed", self.installed_tree, relative))
            if self.source_repo and self.source_repo.is_dir():
                manifest["prestate"].append(self._backup_target(
                    backup_dir, "source", self.source_repo, relative))
        manifest_path = backup_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        commit = ""
        try:
            for change in self.proposal["changes"]:
                self._verify_change_prestate(change)
                relative = change["path"]
                self._apply_one(self.copy_root, self.installed_tree, relative)
                if self.source_repo and self.source_repo.is_dir():
                    self._apply_one(self.copy_root, self.source_repo, relative)
                installed = self.installed_tree / relative
                expected = change.get("after_sha256")
                actual = _sha256(installed) if installed.is_file() else None
                if actual != expected:
                    raise RuntimeError(f"Installed hash mismatch after apply: {relative}")
            if self.source_repo and self.source_repo.is_dir():
                commit = self._commit_source(
                    self.source_repo,
                    [change["path"] for change in self.proposal["changes"]],
                    f"doctor: apply repair {stamp}",
                )
            manifest["status"] = "applied"
            manifest["source_commit"] = commit
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        except Exception:
            self._restore_prestate(backup_dir, manifest, commit_source=False)
            if (self.source_repo and self.source_repo.is_dir()
                    and (self.source_repo / ".git").is_dir()):
                paths = [item["path"] for item in self.proposal["changes"]]
                if commit:
                    self._commit_source(
                        self.source_repo, paths,
                        f"doctor: rollback failed repair {stamp}")
                else:
                    _run(
                        ["git", "reset", "--", *paths],
                        cwd=self.source_repo)
            raise
        self.deny()
        return {
            "backup_dir": str(backup_dir),
            "source_commit": commit,
            "changed_files": [item["path"] for item in self.proposal["changes"]],
        }

    def deny(self):
        if self.temp_root is not None:
            shutil.rmtree(self.temp_root, ignore_errors=True)
        self.temp_root = None
        self.copy_root = None

    @staticmethod
    def _restore_prestate(backup_dir, manifest, installed_tree=None,
                          source_repo=None, commit_source=False):
        backup_dir = Path(backup_dir).resolve()
        roots = {
            "installed": Path(installed_tree or manifest["installed_tree"]).resolve(),
        }
        source_value = source_repo or manifest.get("source_repo")
        if source_value:
            roots["source"] = Path(source_value).resolve()
        paths = []
        for entry in manifest.get("prestate", []):
            label = entry.get("root")
            if label not in roots:
                continue
            relative = entry["path"]
            destination = _safe_target(roots[label], relative)
            backup_path = _safe_target(backup_dir / label, relative)
            if entry.get("existed"):
                if not backup_path.is_file():
                    raise FileNotFoundError(f"Backup file missing: {backup_path}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_path, destination)
                if _sha256(destination) != entry.get("sha256"):
                    raise RuntimeError(f"Restore hash mismatch: {label}/{relative}")
            else:
                destination.unlink(missing_ok=True)
                if destination.exists():
                    raise RuntimeError(f"Could not remove restored addition: {destination}")
            if label == "source":
                paths.append(relative)
        commit = ""
        if commit_source and "source" in roots and (roots["source"] / ".git").is_dir():
            commit = RepairWorkspace._commit_source(
                roots["source"], sorted(set(paths)),
                f"doctor: restore backup {backup_dir.name}")
        return commit

    @staticmethod
    def restore_backup(backup_dir, installed_tree, source_repo, profile_dir):
        backup_dir = Path(backup_dir).resolve()
        backup_root = (Path(profile_dir).resolve() / "qgent" / "backups").resolve()
        if not backup_dir.is_relative_to(backup_root):
            raise ValueError("Backup is outside the QGent backup directory")
        manifest_path = backup_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_repo = Path(source_repo).resolve() if source_repo else None
        if source_repo and source_repo.is_dir() and (source_repo / ".git").is_dir():
            status = _run(["git", "status", "--porcelain"], cwd=source_repo)
            if status.returncode != 0 or status.stdout.strip():
                raise RuntimeError("Source repository is not clean; refusing restore")
        commit = RepairWorkspace._restore_prestate(
            backup_dir, manifest, installed_tree=installed_tree,
            source_repo=source_repo, commit_source=True)
        return {
            "backup_dir": str(backup_dir),
            "source_commit": commit,
            "restored_files": sorted({entry["path"]
                                      for entry in manifest.get("prestate", [])}),
        }
