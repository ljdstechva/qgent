# -*- coding: utf-8 -*-
"""Stdlib-only diagnostics, recovery, backup, and repair primitives for QGent.

This module is intentionally importable by any Python >= 3.9.  It must never
import QGIS, Qt, or another plugin module with GUI dependencies.
"""
from __future__ import annotations

import compileall
import contextlib
import csv
from datetime import datetime
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time


PROTECTED_RELATIVE = "claude_runtime/mcp-config.json"
QGIS_PROCESS_NAMES = {"qgis.exe", "qgis-bin.exe", "qgis-ltr-bin.exe"}
QGIS_EXECUTABLE_NAMES = QGIS_PROCESS_NAMES | {"qgis-ltr.exe"}
DOCTOR_SCHEMA_VERSION = 1
DEFAULT_SOURCE_REPO = Path(r"D:\11 QGIS Agent\qgis_chat_agent")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def command_path(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if os.path.isfile(text):
        return os.path.abspath(text)
    return shutil.which(text) or ""


def run(command, cwd=None, env=None, timeout=30, stdin_text=None):
    """Run a subprocess without a shell and with Windows CLI prerequisites."""
    command = [str(item) for item in command]
    if (os.name == "nt" and command
            and Path(command[0]).name.lower() in {"git", "git.exe"}):
        candidate = (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) /
                     "Git" / "cmd" / "git.exe")
        if candidate.is_file():
            command[0] = str(candidate)
    run_env = dict(os.environ if env is None else env)
    if os.name == "nt" and command:
        current_path = run_env.get("PATH", run_env.get("Path", ""))
        additions = [
            str(Path(command[0]).resolve().parent),
            str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) /
                "nodejs"),
            str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) /
                "Git" / "cmd"),
        ]
        run_env["PATH"] = os.pathsep.join(
            [item for item in additions if Path(item).is_dir()] + [current_path])
    return subprocess.run(
        command, cwd=str(cwd) if cwd else None, env=run_env, input=stdin_text,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False)


def clipped_text(value, limit=12000):
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} characters]"


def redact(text, secrets=()):
    value = str(text or "")
    for secret in secrets:
        if secret:
            value = value.replace(str(secret), "[REDACTED]")
    value = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]", value)
    return clipped_text(value)


def atomic_write_json(path, payload):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    os.replace(temporary, path)
    return path


class AuditLog:
    """Append-only JSON-lines audit log under the QGIS profile."""

    def __init__(self, profile_dir):
        self.path = (Path(profile_dir).resolve() / "qgent" / "doctor" /
                     "doctor.log")

    def append(self, action, status="INFO", **fields):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "action": str(action),
            "status": str(status),
        }
        record.update({key: value for key, value in fields.items()
                       if value is not None})
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record


def qgis_processes():
    """Return live QGIS-family processes without importing OS-specific APIs."""
    if os.name != "nt":
        return []
    tasklist = shutil.which("tasklist") or "tasklist"
    try:
        result = run([tasklist, "/FO", "CSV", "/NH"], timeout=20)
    except Exception as exc:
        raise RuntimeError(
            f"QGIS process detection failed closed: {type(exc).__name__}: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            "QGIS process detection failed closed: tasklist exited "
            f"{result.returncode}: {detail}")
    processes = []
    parsed_rows = 0
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) < 2:
            continue
        try:
            pid = int(row[1])
        except ValueError:
            continue
        parsed_rows += 1
        name = row[0].strip().lower()
        if name not in QGIS_PROCESS_NAMES and not name.startswith("qgis"):
            continue
        processes.append({"name": name, "pid": pid})
    if parsed_rows == 0:
        raise RuntimeError(
            "QGIS process detection failed closed: tasklist returned no "
            "parseable process rows")
    return sorted(processes, key=lambda item: item["pid"])


def require_no_qgis(process_provider=qgis_processes, action="modify files"):
    running = list(process_provider())
    if running:
        pids = ", ".join(
            f"{item.get('name', 'qgis')} PID {item.get('pid', '?')}"
            for item in running)
        raise RuntimeError(f"QGIS is running; refusing to {action}: {pids}")
    return True


def wait_for_qgis_closed(input_func=input, output_func=print,
                         process_provider=qgis_processes, sleep_func=time.sleep,
                         timeout=None, allow_kill=True):
    """Require a deliberate close/wait decision before any real-tree change."""
    running = list(process_provider())
    if not running:
        return {"waited": False, "killed": False, "observed": []}
    observed = list(running)
    pids = ", ".join(f"{item['name']} PID {item['pid']}" for item in running)
    output_func(f"QGIS is running: {pids}")
    prompt = (
        "Close QGIS manually, then press Enter to wait for zero processes"
        + (", or type 'kill qgis' to request a confirmed close: "
           if allow_kill else ": "))
    choice = str(input_func(prompt) or "").strip().lower()
    killed = False
    if choice == "kill qgis" and allow_kill:
        confirmation = str(input_func(
            "Type KILL QGIS exactly to terminate only the listed QGIS PIDs: ")
            or "").strip()
        if confirmation != "KILL QGIS":
            raise RuntimeError("QGIS termination was not explicitly confirmed")
        for item in running:
            result = run(
                ["taskkill", "/PID", str(item["pid"]), "/T"], timeout=30)
            if result.returncode != 0:
                raise RuntimeError(
                    f"Could not terminate QGIS PID {item['pid']}: "
                    f"{(result.stderr or result.stdout).strip()}")
        killed = True
    elif choice not in ("", "wait"):
        raise RuntimeError("Apply cancelled before QGIS shutdown")

    started = time.monotonic()
    while True:
        running = list(process_provider())
        if not running:
            output_func("Confirmed: zero QGIS processes.")
            return {"waited": True, "killed": killed, "observed": observed}
        if timeout is not None and time.monotonic() - started >= float(timeout):
            raise TimeoutError("Timed out waiting for zero QGIS processes")
        sleep_func(0.5)


def launch_detached(command, cwd=None, new_console=True):
    """Start a process that survives the launching QGIS process."""
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= (getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                  if new_console else getattr(subprocess, "DETACHED_PROCESS", 0))
    process = subprocess.Popen(
        [str(item) for item in command], cwd=str(cwd) if cwd else None,
        close_fds=True, creationflags=flags)
    return process.pid


def compile_tree(root):
    """Byte-compile a tree into a temporary cache and return evidence."""
    root = Path(root).resolve()
    temp_root = Path(tempfile.mkdtemp(prefix="qgent-doctor-compile-"))
    previous = getattr(sys, "pycache_prefix", None)
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        sys.pycache_prefix = str(temp_root / "pycache")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            ok = bool(compileall.compile_dir(
                str(root), quiet=2, force=True, maxlevels=30))
        count = sum(1 for _ in root.rglob("*.py"))
        compiled = sum(1 for _ in temp_root.rglob("*.pyc"))
        return {
            "ok": ok, "python_files": count, "compiled_files": compiled,
            "stdout": stdout.getvalue(), "stderr": stderr.getvalue(),
        }
    finally:
        sys.pycache_prefix = previous
        shutil.rmtree(temp_root, ignore_errors=True)


def validate_jsonl(path):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not text.endswith("\n"):
                continue
            raise
        if not isinstance(value, dict) or not value.get("kind"):
            raise ValueError(f"line {index + 1} lacks kind")
    return len(lines)


def _codex_native_from_npm_shim(path):
    path = Path(path) if path else Path()
    if os.name != "nt" or path.suffix.lower() not in {".cmd", ".bat"}:
        return ""
    root = (path.parent / "node_modules" / "@openai" / "codex" /
            "node_modules" / "@openai")
    matches = sorted(root.glob(
        "codex-win32-*/vendor/*/bin/codex.exe")) if root.is_dir() else []
    return str(matches[0].resolve()) if matches else ""


def detect_cli_paths(hints=None):
    hints = dict(hints or {})
    user = Path(os.environ.get("USERPROFILE", str(Path.home())))
    appdata = Path(os.environ.get("APPDATA", user / "AppData" / "Roaming"))
    local = Path(os.environ.get(
        "LOCALAPPDATA", user / "AppData" / "Local"))
    claude_candidates = [
        hints.get("claude"), shutil.which("claude"),
        user / ".local" / "bin" / "claude.exe",
        appdata / "npm" / "claude.cmd",
    ]
    codex_candidates = [
        hints.get("codex"), shutil.which("codex"),
        appdata / "npm" / "codex.cmd",
        appdata / "npm" / "codex.exe",
        local / "Programs" / "codex" / "codex.exe",
    ]

    def first(values, codex=False):
        for value in values:
            resolved = command_path(value)
            if not resolved:
                continue
            if codex:
                resolved = _codex_native_from_npm_shim(resolved) or resolved
            return resolved
        return ""

    return {"claude": first(claude_candidates),
            "codex": first(codex_candidates, codex=True)}


def probe_cli(backend, path):
    if not path:
        return {"status": "FAIL", "detail": "CLI not found."}
    try:
        version = run([path, "--version"], timeout=20)
        if version.returncode != 0:
            return {"status": "FAIL", "detail": "Version probe failed."}
        if backend == "claude":
            auth = run([path, "auth", "status"], timeout=20)
            try:
                payload = json.loads(auth.stdout)
                authenticated = auth.returncode == 0 and payload.get("loggedIn") is True
            except (json.JSONDecodeError, TypeError):
                authenticated = False
        else:
            auth = run([path, "login", "status"], timeout=20)
            authenticated = (auth.returncode == 0 and
                             "logged in" in (auth.stdout + auth.stderr).lower())
        version_text = (version.stdout or version.stderr).strip().splitlines()[0]
        return {
            "status": "PASS" if authenticated else "FAIL",
            "detail": f"path={path}; version={version_text}; "
                      f"authenticated={authenticated}",
        }
    except Exception as exc:
        return {"status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"}


def _mock_bridge_server(token, ready, result_holder):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.settimeout(20)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        result_holder["port"] = server.getsockname()[1]
        ready.set()
        connection, _address = server.accept()
        with connection:
            data = b""
            while b"\n" not in data:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data += chunk
            request = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
            result_holder["request"] = request
            ok = (request.get("token") == token
                  and request.get("tool") == "get_project_context")
            payload = json.dumps({"project_crs": "EPSG:32651", "mock": True})
            response = {"ok": ok, "result": payload} if ok else {
                "ok": False, "error": "mock authentication failed"}
            connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
    except Exception as exc:
        result_holder["error"] = f"{type(exc).__name__}: {exc}"
        ready.set()
    finally:
        server.close()


def mock_bridge_self_test(plugin_dir, python_executable=None):
    plugin_dir = Path(plugin_dir).resolve()
    bridge = plugin_dir / "bridge" / "mcp_stdio_bridge.py"
    python_executable = command_path(python_executable or sys.executable)
    if not bridge.is_file() or not python_executable:
        return {"ok": False, "detail": "Bridge script or Python is missing."}
    token = "qgent-doctor-mock-token"
    ready = threading.Event()
    holder = {}
    thread = threading.Thread(
        target=_mock_bridge_server, args=(token, ready, holder), daemon=True)
    thread.start()
    if not ready.wait(10) or not holder.get("port"):
        return {"ok": False, "detail": holder.get("error", "Mock socket timeout")}
    env = os.environ.copy()
    env.update({
        "QGIS_COPILOT_HOST": "127.0.0.1",
        "QGIS_COPILOT_PORT": str(holder["port"]),
        "QGIS_COPILOT_TOKEN": token,
    })
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "get_project_context", "arguments": {}}},
    ]
    result = run(
        [python_executable, str(bridge)], cwd=plugin_dir, env=env, timeout=30,
        stdin_text="".join(json.dumps(item) + "\n" for item in messages))
    thread.join(timeout=5)
    try:
        replies = [json.loads(line) for line in result.stdout.splitlines()
                   if line.strip()]
        reply = next(item for item in replies if item.get("id") == 2)
        content = ((reply.get("result") or {}).get("content") or [{}])[0]
        payload = json.loads(content.get("text") or "{}")
        ok = (result.returncode == 0 and payload.get("mock") is True
              and not holder.get("error"))
        detail = ("Mock socket authenticated and MCP tools/call round-tripped."
                  if ok else (result.stderr or result.stdout or holder.get("error")))
    except Exception as exc:
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    return {"ok": ok, "detail": clipped_text(detail, 3000),
            "request": holder.get("request")}


def external_diagnostics(plugin_dir, profile_dir, request=None,
                         python_executable=None):
    """Run every diagnostic that is safe outside the QGIS process."""
    plugin_dir = Path(plugin_dir).resolve()
    profile_dir = Path(profile_dir).resolve()
    # A handoff file is evidence, not an executable trust boundary.  In
    # particular, never execute a path supplied through request["cli_paths"]
    # while merely diagnosing.  Discover both CLIs independently instead.
    _ = request
    paths = detect_cli_paths()
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": str(detail),
                       "ok": status in {"PASS", "SKIPPED"}})

    for backend in ("claude", "codex"):
        item = probe_cli(backend, paths.get(backend))
        add(f"{backend.title()} CLI version/auth", item["status"], item["detail"])

    compile_result = compile_tree(plugin_dir)
    add("Installed tree compileall", "PASS" if compile_result["ok"] else "FAIL",
        f"{compile_result['python_files']} Python files; "
        f"compiled={compile_result['compiled_files']}; "
        f"{compile_result['stderr'].strip()}")

    runtime = plugin_dir / "claude_runtime" / ".claude"
    required = [runtime / "agents" / f"{name}.md" for name in (
        "cartographer", "data-scout", "geoprocessor", "qa-verifier")]
    required += [runtime / "skills" / name / "SKILL.md" for name in (
        "cartography-print-layout", "ph-environmental-maps",
        "processing-recipes", "pyqgis-patterns")]
    missing = [str(path.relative_to(plugin_dir)) for path in required
               if not path.is_file()]
    add("Bundled skills and agents", "PASS" if not missing else "FAIL",
        "All 8 required files are present." if not missing
        else "Missing: " + ", ".join(missing))

    histories = sorted((profile_dir / "qgent" / "history").glob("*.jsonl"))
    corrupt = []
    for history in histories:
        try:
            validate_jsonl(history)
        except Exception as exc:
            corrupt.append(f"{history}: {type(exc).__name__}: {exc}")
    add("History-file integrity", "PASS" if not corrupt else "FAIL",
        f"Validated {len(histories)} history file(s)." if not corrupt
        else "; ".join(corrupt))

    bridge = mock_bridge_self_test(plugin_dir, python_executable)
    add("MCP bridge mock-socket protocol", "PASS" if bridge["ok"] else "FAIL",
        bridge["detail"])
    running = qgis_processes()
    if not running:
        add("Live QGIS bridge round trip", "SKIPPED", "QGIS not running")
    else:
        add("Live QGIS bridge round trip", "SKIPPED",
            "QGIS is running, but live bridge credentials are intentionally "
            "not copied into the handoff file.")

    bat = profile_dir / "qgent" / "qgent-doctor.bat"
    add("Dead-plugin recovery BAT", "PASS" if bat.is_file() else "FAIL",
        f"Present: {bat}" if bat.is_file() else f"Missing: {bat}")
    return {
        "schema_version": DOCTOR_SCHEMA_VERSION,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "all_pass": all(item["ok"] for item in checks),
        "checks": checks,
        "cli_paths": paths,
        "qgis_processes": running,
        "compile": compile_result,
    }


def render_diagnostics(report):
    lines = ["QGent External Doctor diagnostics"]
    for item in (report or {}).get("checks", []):
        lines.append(f"[{item['status']}] {item['name']}: {item['detail']}")
    return "\n".join(lines)


def recovery_batch_text(python_executable, doctor_cli, profile_dir,
                        plugin_dir, source_repo):
    values = [python_executable, doctor_cli, profile_dir, plugin_dir, source_repo]
    if any("\n" in str(value) or "\r" in str(value) for value in values):
        raise ValueError("Recovery paths must not contain newlines")
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        f'set "QGENT_PYTHON={python_executable}"\r\n'
        f'set "QGENT_DOCTOR={doctor_cli}"\r\n'
        f'set "QGENT_PROFILE={profile_dir}"\r\n'
        f'set "QGENT_PLUGIN={plugin_dir}"\r\n'
        f'set "QGENT_SOURCE={source_repo}"\r\n'
        "if exist \"%QGENT_PYTHON%\" goto run_recorded\r\n"
        "where py >nul 2>nul && goto run_py\r\n"
        "where python >nul 2>nul && goto run_python\r\n"
        "echo QGent External Doctor could not find Python.\r\n"
        "pause\r\nexit /b 1\r\n"
        ":run_recorded\r\n"
        "if /I \"%QGENT_DOCTOR_NO_DETACH%\"==\"1\" goto run_recorded_foreground\r\n"
        "start \"QGent External Doctor\" \"%QGENT_PYTHON%\" \"%QGENT_DOCTOR%\" --profile-dir \"%QGENT_PROFILE%\" --plugin-dir \"%QGENT_PLUGIN%\" --source-repo \"%QGENT_SOURCE%\" %*\r\n"
        "exit /b 0\r\n"
        ":run_recorded_foreground\r\n"
        "\"%QGENT_PYTHON%\" \"%QGENT_DOCTOR%\" --profile-dir \"%QGENT_PROFILE%\" --plugin-dir \"%QGENT_PLUGIN%\" --source-repo \"%QGENT_SOURCE%\" %*\r\n"
        "exit /b %ERRORLEVEL%\r\n"
        ":run_py\r\n"
        "if /I \"%QGENT_DOCTOR_NO_DETACH%\"==\"1\" goto run_py_foreground\r\n"
        "start \"QGent External Doctor\" py -3 \"%QGENT_DOCTOR%\" --profile-dir \"%QGENT_PROFILE%\" --plugin-dir \"%QGENT_PLUGIN%\" --source-repo \"%QGENT_SOURCE%\" %*\r\n"
        "exit /b 0\r\n"
        ":run_py_foreground\r\n"
        "py -3 \"%QGENT_DOCTOR%\" --profile-dir \"%QGENT_PROFILE%\" --plugin-dir \"%QGENT_PLUGIN%\" --source-repo \"%QGENT_SOURCE%\" %*\r\n"
        "exit /b %ERRORLEVEL%\r\n"
        ":run_python\r\n"
        "if /I \"%QGENT_DOCTOR_NO_DETACH%\"==\"1\" goto run_python_foreground\r\n"
        "start \"QGent External Doctor\" python \"%QGENT_DOCTOR%\" --profile-dir \"%QGENT_PROFILE%\" --plugin-dir \"%QGENT_PLUGIN%\" --source-repo \"%QGENT_SOURCE%\" %*\r\n"
        "exit /b 0\r\n"
        ":run_python_foreground\r\n"
        "python \"%QGENT_DOCTOR%\" --profile-dir \"%QGENT_PROFILE%\" --plugin-dir \"%QGENT_PLUGIN%\" --source-repo \"%QGENT_SOURCE%\" %*\r\n"
        "exit /b %ERRORLEVEL%\r\n")


def ensure_recovery_entrypoint(profile_dir, plugin_dir, python_executable,
                               source_repo=""):
    profile_dir = Path(profile_dir).resolve()
    plugin_dir = Path(plugin_dir).resolve()
    doctor_cli = plugin_dir / "doctor_cli.py"
    if not doctor_cli.is_file():
        raise FileNotFoundError(f"External Doctor missing: {doctor_cli}")
    path = profile_dir / "qgent" / "qgent-doctor.bat"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = recovery_batch_text(
        str(Path(python_executable).resolve()), str(doctor_cli),
        str(profile_dir), str(plugin_dir), str(Path(source_repo).resolve())
        if source_repo else "")
    if not path.is_file() or path.read_text(
            encoding="utf-8", errors="replace") != content:
        path.write_text(content, encoding="utf-8", newline="")
    return path


def write_doctor_request(profile_dir, payload):
    path = (Path(profile_dir).resolve() / "qgent" / "doctor" /
            "doctor_request.json")
    document = dict(payload or {})
    document.setdefault("schema_version", DOCTOR_SCHEMA_VERSION)
    document.setdefault(
        "timestamp", datetime.now().astimezone().isoformat(timespec="seconds"))
    return atomic_write_json(path, document)


def _ignored_relative(relative):
    relative = str(relative).replace("\\", "/").lstrip("/")
    parts = Path(relative).parts
    folded = relative.casefold()
    folded_parts = {part.casefold() for part in parts}
    return (folded == PROTECTED_RELATIVE.casefold()
            or ".git" in folded_parts
            or "__pycache__" in folded_parts
            or folded.endswith((".pyc", ".pyo")))


def tree_manifest(root):
    root = Path(root).resolve()
    result = {}
    if not root.is_dir():
        return result
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if not _ignored_relative(relative):
            result[relative] = sha256(path)
    return result


def guard_tree_manifest(root):
    root = Path(root).resolve()
    result = {}
    if not root.is_dir():
        return result
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        parts = Path(relative).parts
        folded_parts = {part.casefold() for part in parts}
        if (".git" in folded_parts or "__pycache__" in folded_parts
                or relative.casefold().endswith((".pyc", ".pyo"))):
            continue
        stat = path.stat()
        if relative.casefold() == PROTECTED_RELATIVE.casefold():
            result[relative] = {
                "protected": True, "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        else:
            result[relative] = {
                "sha256": sha256(path), "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
    return result


def _copy_ignore(_directory, names):
    return {name for name in names
            if name.casefold() in {".git", "__pycache__", "mcp-config.json"}
            or name.casefold().endswith((".pyc", ".pyo"))}


def safe_target(root, relative):
    root = Path(root).resolve()
    text = str(relative).replace("\\", "/").lstrip("/")
    if not text or _ignored_relative(text):
        raise ValueError(f"Protected or invalid repair path: {relative!r}")
    target = (root / Path(text)).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"Repair path escapes target root: {relative!r}")
    protected = (root / PROTECTED_RELATIVE).resolve()
    if os.path.normcase(str(target)) == os.path.normcase(str(protected)):
        raise ValueError(f"Protected repair path: {relative!r}")
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
        before_text, after_text, fromfile=f"installed/{relative}",
        tofile=f"proposal/{relative}"))


class RepairWorkspace:
    """Disposable proposal plus guarded approve/deny/restore operations."""

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
            raise FileNotFoundError(
                f"Installed plugin tree missing: {self.installed_tree}")
        if (self.source_repo is None or not self.source_repo.is_dir()
                or not (self.source_repo / ".git").is_dir()):
            raise RuntimeError(
                "A valid Git source repository is required for AI repair")
        self.temp_root = Path(tempfile.mkdtemp(prefix="qgent-doctor-repair-"))
        self.copy_root = self.temp_root / "qgis_chat_agent"
        self._installed_guard = guard_tree_manifest(self.installed_tree)
        self._source_guard = (guard_tree_manifest(self.source_repo)
                              if self.source_repo and self.source_repo.is_dir()
                              else None)
        status = run(["git", "status", "--porcelain"], cwd=self.source_repo)
        if status.returncode != 0 or status.stdout.strip():
            raise RuntimeError(
                "Source repository is not clean; refusing repair proposal")
        head = run(["git", "rev-parse", "HEAD"], cwd=self.source_repo)
        if head.returncode != 0 or not head.stdout.strip():
            raise RuntimeError("Could not resolve source repository HEAD")
        self._source_head = head.stdout.strip()
        shutil.copytree(self.installed_tree, self.copy_root, ignore=_copy_ignore)
        self._before = tree_manifest(self.installed_tree)
        if (self.copy_root / PROTECTED_RELATIVE).exists():
            raise RuntimeError("Protected MCP config entered the repair copy")
        initialized = run(["git", "init", "-q"], cwd=self.copy_root)
        if initialized.returncode != 0:
            raise RuntimeError(
                "Could not initialize disposable repository: "
                + (initialized.stderr or initialized.stdout).strip())
        return self.copy_root

    def build_proposal(self, explanation):
        if self.copy_root is None or self._before is None:
            raise RuntimeError("Repair workspace was not prepared")
        self._validate_real_tree_guards("during proposal generation", "review")
        after = tree_manifest(self.copy_root)
        self._proposal_guard = after
        changes = []
        diff_parts = []
        for relative in sorted(set(self._before) | set(after)):
            if self._before.get(relative) == after.get(relative):
                continue
            action = ("add" if relative not in self._before else
                      "delete" if relative not in after else "modify")
            changes.append({
                "path": relative, "action": action,
                "before_sha256": self._before.get(relative),
                "after_sha256": after.get(relative),
            })
            diff_parts.append(_text_diff(
                relative, self.installed_tree / relative,
                self.copy_root / relative))
        self.proposal = {
            "workspace_path": str(self.copy_root), "changes": changes,
            "diff": "".join(diff_parts) or "(no file changes)",
            "explanation": str(explanation or "").strip(),
        }
        return self.proposal

    def _current_source_head(self):
        if self._source_head is None:
            return None
        status = run(["git", "status", "--porcelain"], cwd=self.source_repo)
        if status.returncode != 0 or status.stdout.strip():
            return ""
        head = run(["git", "rev-parse", "HEAD"], cwd=self.source_repo)
        return head.stdout.strip() if head.returncode == 0 else ""

    def _validate_real_tree_guards(self, phase, action):
        if guard_tree_manifest(self.installed_tree) != self._installed_guard:
            raise RuntimeError(f"Installed tree changed {phase}; refusing {action}")
        if (self._source_guard is not None
                and guard_tree_manifest(self.source_repo) != self._source_guard):
            raise RuntimeError(f"Source tree changed {phase}; refusing {action}")
        if (self._source_head is not None
                and self._current_source_head() != self._source_head):
            raise RuntimeError(f"Source Git state changed {phase}; refusing {action}")

    def _validate_review_baseline(self):
        self._validate_real_tree_guards("after review began", "apply")
        if tree_manifest(self.copy_root) != self._proposal_guard:
            raise RuntimeError(
                "Disposable proposal changed after review began; refusing apply")

    def _verify_change_prestate(self, change):
        relative = change["path"]
        installed = safe_target(self.installed_tree, relative)
        actual = sha256(installed) if installed.is_file() else None
        if actual != change.get("before_sha256"):
            raise RuntimeError(f"Installed pre-state changed: {relative}")
        if self._source_guard is not None:
            source = safe_target(self.source_repo, relative)
            source_hash = sha256(source) if source.is_file() else None
            entry = self._source_guard.get(relative)
            expected = entry.get("sha256") if entry is not None else None
            if source_hash != expected:
                raise RuntimeError(f"Source pre-state changed: {relative}")
        proposed = safe_target(self.copy_root, relative)
        proposed_hash = sha256(proposed) if proposed.is_file() else None
        if proposed_hash != change.get("after_sha256"):
            raise RuntimeError(f"Proposed file changed: {relative}")

    def _git_clean(self):
        if not self.source_repo or not (self.source_repo / ".git").is_dir():
            return False
        status = run(["git", "status", "--porcelain"], cwd=self.source_repo)
        return status.returncode == 0 and not status.stdout.strip()

    @staticmethod
    def _backup_target(backup_dir, label, root, relative):
        target = safe_target(root, relative)
        entry = {"root": label, "path": relative,
                 "existed": target.is_file(),
                 "sha256": sha256(target) if target.is_file() else None}
        if target.is_file():
            backup_path = safe_target(backup_dir / label, relative)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup_path)
        return entry

    @staticmethod
    def _apply_one(source_copy, target_root, relative):
        destination = safe_target(target_root, relative)
        proposed = safe_target(source_copy, relative)
        if proposed.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(proposed, destination)
        else:
            destination.unlink(missing_ok=True)

    @staticmethod
    def _commit_source(source_repo, paths, message, allow_empty=False):
        source_repo = Path(source_repo).resolve()
        if not (source_repo / ".git").is_dir():
            return ""
        normalized = [str(path).replace("\\", "/") for path in paths]
        added = run(["git", "add", "-A", "--", *normalized], cwd=source_repo)
        if added.returncode != 0:
            raise RuntimeError((added.stderr or added.stdout).strip())
        status = run(["git", "status", "--porcelain"], cwd=source_repo)
        if status.returncode != 0:
            raise RuntimeError((status.stderr or status.stdout).strip())
        if not status.stdout.strip() and not allow_empty:
            return ""
        commit_args = ["git", "commit"]
        if allow_empty:
            commit_args.append("--allow-empty")
        commit_args.extend(["-m", message])
        committed = run(commit_args, cwd=source_repo, timeout=60)
        if committed.returncode != 0:
            raise RuntimeError((committed.stderr or committed.stdout).strip())
        head = run(["git", "rev-parse", "HEAD"], cwd=source_repo)
        if head.returncode != 0 or not head.stdout.strip():
            raise RuntimeError("Doctor commit created but HEAD is unavailable")
        return head.stdout.strip()

    def apply(self, process_provider=qgis_processes):
        require_no_qgis(process_provider, "apply repair")
        if not self.proposal or not self.proposal.get("changes"):
            raise RuntimeError("There is no proposed file change to approve")
        self._validate_review_baseline()
        if not self._git_clean():
            raise RuntimeError("Source repository is not clean; refusing apply")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_dir = self.profile_dir / "qgent" / "backups" / stamp
        backup_dir.mkdir(parents=True, exist_ok=False)
        manifest = {
            "version": 1,
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": "pending", "installed_tree": str(self.installed_tree),
            "source_repo": str(self.source_repo or ""),
            "changes": self.proposal["changes"], "prestate": [],
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
        atomic_write_json(manifest_path, manifest)
        commit = ""
        compile_result = None
        try:
            for change in self.proposal["changes"]:
                self._verify_change_prestate(change)
                relative = change["path"]
                require_no_qgis(process_provider, f"apply {relative}")
                self._apply_one(self.copy_root, self.installed_tree, relative)
                target = self.installed_tree / relative
                actual = sha256(target) if target.is_file() else None
                if actual != change.get("after_sha256"):
                    raise RuntimeError(f"Installed hash mismatch: {relative}")
                require_no_qgis(process_provider, f"apply source {relative}")
                self._apply_one(self.copy_root, self.source_repo, relative)
                source_target = self.source_repo / relative
                source_actual = (
                    sha256(source_target) if source_target.is_file() else None)
                if source_actual != change.get("after_sha256"):
                    raise RuntimeError(f"Source hash mismatch: {relative}")
            require_no_qgis(process_provider, "verify repaired trees")
            compile_result = compile_tree(self.installed_tree)
            if not compile_result["ok"]:
                raise RuntimeError(
                    "Installed compileall failed: " + compile_result["stderr"])
            source_compile = compile_tree(self.source_repo)
            if not source_compile["ok"]:
                raise RuntimeError(
                    "Source compileall failed: " + source_compile["stderr"])
            require_no_qgis(process_provider, "commit repaired source")
            commit = self._commit_source(
                self.source_repo,
                [change["path"] for change in self.proposal["changes"]],
                f"doctor: apply repair {stamp}", allow_empty=True)
            if not commit:
                raise RuntimeError("Required doctor source commit was not created")
            manifest.update({"status": "applied", "source_commit": commit,
                             "compile": compile_result,
                             "source_compile": source_compile})
            atomic_write_json(manifest_path, manifest)
        except Exception as exc:
            rollback_error = ""
            try:
                self._restore_prestate(backup_dir, manifest, commit_source=False)
                paths = [item["path"] for item in self.proposal["changes"]]
                current_head = run(
                    ["git", "rev-parse", "HEAD"], cwd=self.source_repo)
                if (current_head.returncode == 0
                        and current_head.stdout.strip() != self._source_head):
                    self._commit_source(
                        self.source_repo, paths,
                        f"doctor: rollback failed repair {stamp}",
                        allow_empty=True)
                else:
                    run(["git", "reset", "--", *paths], cwd=self.source_repo)
                if guard_tree_manifest(self.installed_tree) != self._installed_guard:
                    raise RuntimeError("Installed repair rollback hash mismatch")
                if guard_tree_manifest(self.source_repo) != self._source_guard:
                    raise RuntimeError("Source repair rollback hash mismatch")
            except Exception as rollback_exc:
                rollback_error = (
                    f"; rollback also failed: {type(rollback_exc).__name__}: "
                    f"{rollback_exc}")
            manifest.update({
                "status": "rolled-back-after-failure",
                "failure": f"{type(exc).__name__}: {exc}",
            })
            atomic_write_json(manifest_path, manifest)
            if rollback_error:
                raise RuntimeError(
                    f"Repair failed: {type(exc).__name__}: {exc}"
                    f"{rollback_error}") from exc
            raise
        self.deny()
        return {"backup_dir": str(backup_dir), "source_commit": commit,
                "changed_files": [item["path"] for item in self.proposal["changes"]],
                "compile": compile_result,
                "source_compile": source_compile}

    def deny(self):
        if self.temp_root is not None:
            shutil.rmtree(self.temp_root, ignore_errors=True)
        self.temp_root = None
        self.copy_root = None

    @staticmethod
    def _validate_backup_prestate(backup_dir, manifest):
        backup_dir = Path(backup_dir).resolve()
        entries = list(manifest.get("prestate") or [])
        if not entries:
            raise ValueError("Backup manifest has no restorable pre-state")
        seen = set()
        for entry in entries:
            label = entry.get("root")
            relative = entry.get("path")
            if label not in {"installed", "source"} or not relative:
                raise ValueError("Backup manifest has an invalid target entry")
            key = (label, str(relative).casefold())
            if key in seen:
                raise ValueError(f"Backup manifest repeats {label}/{relative}")
            seen.add(key)
            backup_path = safe_target(backup_dir / label, relative)
            if entry.get("existed"):
                if not backup_path.is_file():
                    raise FileNotFoundError(f"Backup file missing: {backup_path}")
                if sha256(backup_path) != entry.get("sha256"):
                    raise RuntimeError(f"Backup hash mismatch: {label}/{relative}")
        return entries

    @staticmethod
    def _restore_prestate(backup_dir, manifest, installed_tree=None,
                          source_repo=None, commit_source=False,
                          process_provider=None):
        backup_dir = Path(backup_dir).resolve()
        RepairWorkspace._validate_backup_prestate(backup_dir, manifest)
        roots = {"installed": Path(
            installed_tree or manifest["installed_tree"]).resolve()}
        source_value = source_repo or manifest.get("source_repo")
        if source_value:
            roots["source"] = Path(source_value).resolve()
        paths = []
        for entry in manifest.get("prestate", []):
            label = entry.get("root")
            if label not in roots:
                continue
            relative = entry["path"]
            if process_provider is not None:
                require_no_qgis(
                    process_provider, f"restore {label}/{relative}")
            destination = safe_target(roots[label], relative)
            backup_path = safe_target(backup_dir / label, relative)
            if entry.get("existed"):
                if not backup_path.is_file():
                    raise FileNotFoundError(f"Backup file missing: {backup_path}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_path, destination)
                if sha256(destination) != entry.get("sha256"):
                    raise RuntimeError(f"Restore hash mismatch: {label}/{relative}")
            else:
                destination.unlink(missing_ok=True)
            if label == "source":
                paths.append(relative)
        commit = ""
        if (commit_source and "source" in roots
                and (roots["source"] / ".git").is_dir()):
            commit = RepairWorkspace._commit_source(
                roots["source"], sorted(set(paths)),
                f"doctor: restore backup {backup_dir.name}")
        return commit

    @staticmethod
    def restore_backup(backup_dir, installed_tree, source_repo, profile_dir,
                       process_provider=qgis_processes):
        require_no_qgis(process_provider, "restore backup")
        backup_dir = Path(backup_dir).resolve()
        backup_root = (Path(profile_dir).resolve() / "qgent" / "backups").resolve()
        if not backup_dir.is_relative_to(backup_root):
            raise ValueError("Backup is outside the QGent backup directory")
        manifest = json.loads(
            (backup_dir / "manifest.json").read_text(encoding="utf-8"))
        entries = RepairWorkspace._validate_backup_prestate(backup_dir, manifest)
        installed_tree = Path(installed_tree).resolve()
        source_repo = Path(source_repo).resolve() if source_repo else None
        if (source_repo is None or not source_repo.is_dir()
                or not (source_repo / ".git").is_dir()):
            raise RuntimeError(
                "A valid Git source repository is required for backup restore")
        status = run(["git", "status", "--porcelain"], cwd=source_repo)
        if status.returncode != 0 or status.stdout.strip():
            raise RuntimeError("Source repository is not clean; refusing restore")
        source_head_result = run(["git", "rev-parse", "HEAD"], cwd=source_repo)
        if source_head_result.returncode != 0 or not source_head_result.stdout.strip():
            raise RuntimeError("Could not resolve source repository HEAD")
        source_head = source_head_result.stdout.strip()

        installed_paths = {entry["path"] for entry in entries
                           if entry["root"] == "installed"}
        source_paths = {entry["path"] for entry in entries
                        if entry["root"] == "source"}
        if not installed_paths or installed_paths != source_paths:
            raise RuntimeError(
                "Restore requires paired installed/source backup entries")

        installed_guard = guard_tree_manifest(installed_tree)
        source_guard = guard_tree_manifest(source_repo)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        rollback_dir = backup_root / ("pre-restore-" + stamp)
        rollback_dir.mkdir(parents=True, exist_ok=False)
        rollback_manifest = {
            "version": 1,
            "kind": "pre-restore-rollback",
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": "pending",
            "installed_tree": str(installed_tree),
            "source_repo": str(source_repo),
            "prestate": [],
        }
        for relative in sorted(installed_paths):
            rollback_manifest["prestate"].append(
                RepairWorkspace._backup_target(
                    rollback_dir, "installed", installed_tree, relative))
            rollback_manifest["prestate"].append(
                RepairWorkspace._backup_target(
                    rollback_dir, "source", source_repo, relative))
        rollback_manifest_path = rollback_dir / "manifest.json"
        atomic_write_json(rollback_manifest_path, rollback_manifest)

        if guard_tree_manifest(installed_tree) != installed_guard:
            raise RuntimeError("Installed tree changed while restore was prepared")
        if guard_tree_manifest(source_repo) != source_guard:
            raise RuntimeError("Source tree changed while restore was prepared")
        current_head = run(["git", "rev-parse", "HEAD"], cwd=source_repo)
        if current_head.returncode != 0 or current_head.stdout.strip() != source_head:
            raise RuntimeError("Source HEAD changed while restore was prepared")

        compiled = None
        source_compiled = None
        commit = ""
        try:
            require_no_qgis(process_provider, "apply backup restore")
            RepairWorkspace._restore_prestate(
                backup_dir, manifest, installed_tree=installed_tree,
                source_repo=source_repo, commit_source=False,
                process_provider=process_provider)
            require_no_qgis(process_provider, "verify restored trees")
            compiled = compile_tree(installed_tree)
            if not compiled["ok"]:
                raise RuntimeError("Restored installed tree failed compileall")
            source_compiled = compile_tree(source_repo)
            if not source_compiled["ok"]:
                raise RuntimeError("Restored source tree failed compileall")
            rollback_manifest["status"] = "available"
            rollback_manifest["restored_from"] = str(backup_dir)
            atomic_write_json(rollback_manifest_path, rollback_manifest)
            require_no_qgis(process_provider, "commit restored source")
            commit = RepairWorkspace._commit_source(
                source_repo, sorted(source_paths),
                f"doctor: restore backup {backup_dir.name}", allow_empty=True)
            if not commit:
                raise RuntimeError("Required doctor restore commit was not created")
        except Exception as exc:
            rollback_error = ""
            try:
                RepairWorkspace._restore_prestate(
                    rollback_dir, rollback_manifest,
                    installed_tree=installed_tree, source_repo=source_repo,
                    commit_source=False)
                head_after_failure = run(
                    ["git", "rev-parse", "HEAD"], cwd=source_repo)
                if (head_after_failure.returncode == 0
                        and head_after_failure.stdout.strip() != source_head):
                    RepairWorkspace._commit_source(
                        source_repo, sorted(source_paths),
                        f"doctor: rollback failed restore {stamp}",
                        allow_empty=True)
                else:
                    run(
                        ["git", "reset", "--", *sorted(source_paths)],
                        cwd=source_repo)
                if guard_tree_manifest(installed_tree) != installed_guard:
                    raise RuntimeError("Installed rollback hash verification failed")
                if guard_tree_manifest(source_repo) != source_guard:
                    raise RuntimeError("Source rollback hash verification failed")
            except Exception as rollback_exc:
                rollback_error = (
                    f"; rollback also failed: {type(rollback_exc).__name__}: "
                    f"{rollback_exc}")
            rollback_manifest["status"] = "rolled-back-after-failure"
            rollback_manifest["failure"] = f"{type(exc).__name__}: {exc}"
            atomic_write_json(rollback_manifest_path, rollback_manifest)
            raise RuntimeError(
                f"Restore failed and was rolled back: {type(exc).__name__}: "
                f"{exc}{rollback_error}") from exc

        return {
            "backup_dir": str(backup_dir),
            "rollback_backup_dir": str(rollback_dir),
            "source_commit": commit,
            "restored_files": sorted(installed_paths),
            "compile": compiled,
            "source_compile": source_compiled,
        }


def clear_bytecode_caches(plugin_dir, profile_dir,
                          process_provider=qgis_processes):
    require_no_qgis(process_provider, "repair bytecode caches")
    root = Path(plugin_dir).resolve()
    directories = sorted(
        [path.resolve() for path in root.rglob("__pycache__") if path.is_dir()],
        key=lambda path: len(path.parts), reverse=True)
    loose = [path.resolve() for path in root.rglob("*.pyc")
             if "__pycache__" not in path.parts]
    if not all(path.is_relative_to(root) and path.name == "__pycache__"
               for path in directories):
        raise RuntimeError("Refusing invalid cache directory target")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = Path(profile_dir).resolve() / "qgent" / "backups" / (
        "cache-" + stamp)
    files = [item for directory in directories for item in directory.rglob("*")
             if item.is_file()] + loose
    if files:
        for path in files:
            require_no_qgis(process_provider, "back up bytecode caches")
            relative = path.relative_to(root)
            target = backup / "installed" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        atomic_write_json(backup / "manifest.json", {
            "version": 2, "kind": "bytecode-cache", "status": "applied",
            "installed_tree": str(root),
            "files": [str(path.relative_to(root)).replace("\\", "/")
                      for path in files],
        })
    for path in directories:
        require_no_qgis(process_provider, "remove bytecode cache")
        shutil.rmtree(path)
    for path in loose:
        require_no_qgis(process_provider, "remove bytecode file")
        path.unlink(missing_ok=True)
    return {"removed": len(directories) + len(loose),
            "backup_dir": str(backup) if files else ""}


def quarantine_corrupt_histories(profile_dir, process_provider=qgis_processes):
    require_no_qgis(process_provider, "repair history")
    profile = Path(profile_dir).resolve()
    history_root = profile / "qgent" / "history"
    quarantine = history_root / "quarantine"
    moved = []
    for path in sorted(history_root.glob("*.jsonl")):
        try:
            validate_jsonl(path)
        except Exception:
            require_no_qgis(process_provider, "quarantine corrupt history")
            quarantine.mkdir(parents=True, exist_ok=True)
            destination = quarantine / (
                path.stem + "-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                + path.suffix)
            os.replace(path, destination)
            moved.append(str(destination))
    return moved


def write_detected_cli_state(profile_dir, hints=None,
                             process_provider=qgis_processes):
    require_no_qgis(process_provider, "repair CLI state")
    paths = detect_cli_paths(hints)
    target = (Path(profile_dir).resolve() / "qgent" / "doctor" /
              "detected_cli_paths.json")
    require_no_qgis(process_provider, "write repaired CLI state")
    atomic_write_json(target, {
        "schema_version": 1,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cli_paths": paths,
    })
    return {"path": str(target), "cli_paths": paths}


def refresh_template_state(plugin_dir, profile_dir,
                           process_provider=qgis_processes):
    """Refresh safe template metadata without touching protected mcp-config."""
    require_no_qgis(process_provider, "repair template state")
    plugin = Path(plugin_dir).resolve()
    template = plugin / "claude_runtime" / "mcp-config.json.template"
    if not template.is_file():
        raise FileNotFoundError(f"MCP template missing: {template}")
    payload = json.loads(template.read_text(encoding="utf-8"))
    if "mcpServers" not in payload:
        raise ValueError("MCP template lacks mcpServers")
    target = (Path(profile_dir).resolve() / "qgent" / "doctor" /
              "mcp_template_state.json")
    require_no_qgis(process_provider, "write repaired template state")
    atomic_write_json(target, {
        "schema_version": 1, "template": str(template),
        "sha256": sha256(template),
        "protected_target_untouched": PROTECTED_RELATIVE,
    })
    return {"path": str(target), "sha256": sha256(template)}


def list_backups(profile_dir):
    root = Path(profile_dir).resolve() / "qgent" / "backups"
    if not root.is_dir():
        return []
    return [path for path in sorted(root.iterdir(), reverse=True)
            if path.is_dir() and (path / "manifest.json").is_file()]
