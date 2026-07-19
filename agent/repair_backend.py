# -*- coding: utf-8 -*-
"""Asynchronous CLI runner for sandboxed Doctor repair proposals."""
from __future__ import annotations

import json
import os
import subprocess
import time

from qgis.PyQt.QtCore import (
    QObject, QProcess, QProcessEnvironment, QThread, pyqtSignal)

from .stream_parser import StreamJsonParser
from ..doctor import RepairWorkspace


def _windows_process_snapshot():
    """Return {pid: parent_pid} without invoking a shell or CIM provider."""
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateToolhelp32Snapshot
    create.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create.restype = wintypes.HANDLE
    first = kernel32.Process32FirstW
    first.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    first.restype = wintypes.BOOL
    next_entry = kernel32.Process32NextW
    next_entry.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    next_entry.restype = wintypes.BOOL
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(0x00000002, 0)  # TH32CS_SNAPPROCESS
    invalid = ctypes.c_void_p(-1).value
    if not handle or int(handle) == invalid:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    snapshot = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = first(handle, ctypes.byref(entry))
        while ok:
            snapshot[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = next_entry(handle, ctypes.byref(entry))
    finally:
        close(handle)
    return snapshot


def _descendant_pids(root_pid, snapshot):
    found = {int(root_pid)}
    changed = True
    while changed:
        changed = False
        for pid, parent in snapshot.items():
            if parent in found and pid not in found:
                found.add(pid)
                changed = True
    return found


class _CancelProcessTreeWorker(QThread):
    completed = pyqtSignal(object)

    def __init__(self, root_pid, parent=None):
        super().__init__(parent)
        self.root_pid = int(root_pid)

    def run(self):
        result = {
            "root_pid": self.root_pid,
            "observed_pids": [],
            "remaining_pids": [],
            "taskkill_exit_code": None,
            "taskkill_stdout": "",
            "taskkill_stderr": "",
            "all_gone": False,
        }
        try:
            before = _windows_process_snapshot()
            observed = _descendant_pids(self.root_pid, before)
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            killed = subprocess.run(
                ["taskkill", "/PID", str(self.root_pid), "/T", "/F"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=False, timeout=15,
                creationflags=creationflags)
            result["taskkill_exit_code"] = killed.returncode
            result["taskkill_stdout"] = killed.stdout[-2000:]
            result["taskkill_stderr"] = killed.stderr[-2000:]
            deadline = time.monotonic() + 8.0
            remaining = set(observed)
            while time.monotonic() < deadline:
                after = _windows_process_snapshot()
                observed.update(_descendant_pids(self.root_pid, after))
                remaining = observed.intersection(after)
                if not remaining:
                    break
                time.sleep(0.05)
            result["observed_pids"] = sorted(observed)
            result["remaining_pids"] = sorted(remaining)
            result["all_gone"] = not remaining
        except Exception as exc:
            result["taskkill_stderr"] = f"{type(exc).__name__}: {exc}"
        self.completed.emit(result)


class RepairBackend(QObject):
    progress = pyqtSignal(str)
    proposal_ready = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()
    busy_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = None
        self.workspace = None
        self.current_proposal = None
        self.last_stderr = ""
        self._backend = ""
        self._stdout = ""
        self._stderr = ""
        self._explanation = ""
        self._parser = StreamJsonParser()
        self._cancel_requested = False
        self._completed = False
        self._final_error = ""
        self._saw_claude_delta = False
        self._cancel_worker = None
        self._cancel_result = None
        self._cancel_process_finished = False
        self.last_cancel_result = None

    def is_busy(self):
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def start(self, spec, diagnostics_bundle, user_description,
              installed_tree, source_repo, profile_dir):
        if self.is_busy():
            self.failed.emit("A Doctor repair is already running.")
            return
        self.deny_current()
        self.workspace = RepairWorkspace(
            installed_tree, source_repo, profile_dir)
        try:
            workdir = self.workspace.prepare()
        except Exception as exc:
            self.workspace.deny()
            self.workspace = None
            self.failed.emit(f"Could not prepare repair copy: {type(exc).__name__}: {exc}")
            return

        self._backend = str(spec.get("backend") or "")
        self._stdout = ""
        self._stderr = ""
        self._explanation = ""
        self._parser.reset()
        self._cancel_requested = False
        self._completed = False
        self._final_error = ""
        self._saw_claude_delta = False
        self._cancel_result = None
        self._cancel_process_finished = False
        self.last_cancel_result = None
        safe_bundle = str(diagnostics_bundle or "")
        for real_path, replacement in (
                (installed_tree, "[INSTALLED_PLUGIN_TREE]"),
                (source_repo, "[SOURCE_REPOSITORY]"),
                (profile_dir, "[QGIS_PROFILE]")):
            value = str(real_path or "")
            if value:
                safe_bundle = safe_bundle.replace(value, replacement)
                safe_bundle = safe_bundle.replace(
                    value.replace("\\", "/"), replacement)
        prompt = self._repair_prompt(safe_bundle, user_description)
        try:
            program, args = self._command(spec, workdir)
        except Exception as exc:
            self.workspace.deny()
            self.workspace = None
            self.failed.emit(f"Invalid repair model: {type(exc).__name__}: {exc}")
            return

        self.proc = QProcess(self)
        self.proc.setWorkingDirectory(str(workdir))
        self.proc.setProcessEnvironment(self._process_environment(program))
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.readyReadStandardError.connect(self._on_stderr)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_process_error)
        self.busy_changed.emit(True)
        self.progress.emit(
            f"Prepared disposable repair copy: {workdir}\n"
            f"Starting {spec.get('label') or spec.get('model')}…\n")
        self.proc.start(program, args)
        if not self.proc.waitForStarted(5000):
            self.busy_changed.emit(False)
            self.workspace.deny()
            self.workspace = None
            self.proc = None
            self.failed.emit(f"Could not start repair CLI at {program!r}.")
            return
        self.proc.write(prompt.encode("utf-8"))
        self.proc.closeWriteChannel()

    @staticmethod
    def _process_environment(program):
        qenv = QProcessEnvironment.systemEnvironment()
        if os.name == "nt":
            additions = [os.path.dirname(os.path.abspath(program))]
            node_dir = os.path.join(
                os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs")
            if os.path.isdir(node_dir):
                additions.append(node_dir)
            git_dir = os.path.join(
                os.environ.get("ProgramFiles", r"C:\Program Files"),
                "Git", "cmd")
            if os.path.isdir(git_dir):
                additions.append(git_dir)
            current = qenv.value("PATH") or qenv.value("Path")
            qenv.insert("PATH", os.pathsep.join(additions + [current]))
        return qenv

    @staticmethod
    def _repair_prompt(diagnostics_bundle, user_description):
        description = str(user_description or "").strip()
        return (
            "You are repairing the QGent QGIS plugin in a disposable copy.\n"
            "Work ONLY inside the current working directory. Do not access or "
            "modify the installed plugin, source repository, user profile, or "
            "any path outside this copy. Do not create commits.\n"
            "Inspect the evidence, make the smallest correct code/configuration "
            "change in this copy, and run focused checks when useful. Never "
            "create or modify claude_runtime/mcp-config.json.\n"
            "In your final response, summarize the diagnosis, changed files, "
            "and verification; the host will compute and review the actual diff.\n\n"
            "DIAGNOSTICS BUNDLE\n"
            "==================\n"
            f"{diagnostics_bundle or '(diagnostics unavailable)'}\n\n"
            "USER DESCRIPTION\n"
            "================\n"
            f"{description or '(none; diagnose from the bundle)'}\n")

    @staticmethod
    def _command(spec, workdir=None):
        backend = spec.get("backend")
        model = str(spec.get("model") or "")
        cli_path = str(spec.get("cli_path") or "")
        if not cli_path or not model:
            raise ValueError("CLI path and model are required")
        if backend == "codex":
            effort = str(spec.get("effort") or "xhigh")
            if not workdir:
                raise ValueError("Codex repair requires a disposable working root")
            args = [
                "-a", "never", "exec", "-s", "workspace-write",
                "-C", str(workdir),
                "-c", 'windows.sandbox="elevated"',
                "--disable", "unified_exec",
                "--model", model,
                "-c", f'model_reasoning_effort="{effort}"',
                "--skip-git-repo-check", "--ephemeral",
                "--ignore-user-config", "--ignore-rules", "--json", "-",
            ]
        elif backend == "claude":
            effort = str(spec.get("effort") or "max")
            args = [
                "-p", "--safe-mode", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages",
                "--no-session-persistence", "--model", model,
                "--effort", effort, "--permission-mode", "acceptEdits",
                "--allowedTools", "Read,Glob,Grep,Edit,Write",
                "--disallowedTools", "Bash,WebFetch,WebSearch",
            ]
        else:
            raise ValueError(f"Unsupported repair backend: {backend!r}")
        return cli_path, args

    def _on_stdout(self):
        if self.proc is None:
            return
        text = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        self._stdout += text
        for event in self._parser.feed(text):
            if self._backend == "claude":
                self._dispatch_claude(event)
            else:
                self._dispatch_codex(event)

    def _on_stderr(self):
        if self.proc is None:
            return
        text = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        self._stderr += text
        if text:
            self.progress.emit(text)

    def _dispatch_claude(self, event):
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            self.progress.emit(
                f"Claude session started with {event.get('model') or 'unknown model'}.\n")
        elif etype == "stream_event":
            inner = event.get("event") or {}
            delta = inner.get("delta") or {}
            if (inner.get("type") == "content_block_delta"
                    and delta.get("type") == "text_delta"):
                text = str(delta.get("text") or "")
                if text:
                    self._saw_claude_delta = True
                    self._explanation += text
                    self.progress.emit(text)
        elif etype == "assistant":
            if self._saw_claude_delta:
                return
            for block in (event.get("message") or {}).get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    text = str(block["text"])
                    self._explanation += text
                    self.progress.emit(text)
        elif etype == "result":
            self._completed = not bool(event.get("is_error"))
            if event.get("is_error"):
                self._final_error = str(
                    event.get("result") or "Claude repair reported an error.")
            elif not self._explanation:
                self._explanation = str(event.get("result") or "")

    def _dispatch_codex(self, event):
        etype = event.get("type")
        if etype == "thread.started":
            self.progress.emit("Codex repair session started.\n")
        elif etype in ("item.completed", "item.updated"):
            item = event.get("item") or {}
            item_type = item.get("type") or item.get("item_type")
            if item_type in ("agent_message", "assistant_message"):
                text = str(item.get("text") or item.get("content") or "")
                if text and etype == "item.completed":
                    self._explanation += text
                    self.progress.emit(text)
            elif item_type in ("command_execution", "file_change"):
                description = item.get("command") or item.get("path") or item_type
                self.progress.emit(f"\n{description}\n")
        elif etype in ("turn.completed", "thread.completed"):
            self._completed = True
        elif etype in ("turn.failed", "error"):
            self._final_error = json.dumps(event, ensure_ascii=False)

    def _on_finished(self, exit_code, _status):
        if self.proc is not None:
            tail = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
            if tail:
                self._stderr += tail
                self.progress.emit(tail)
        self.last_stderr = self._stderr
        self.proc = None
        if self._cancel_requested:
            self._cancel_process_finished = True
            self._finish_cancel_if_ready()
            return
        self.busy_changed.emit(False)
        if exit_code != 0 or not self._completed or self._final_error:
            message = self._final_error or self._stderr.strip() or self._stdout.strip()
            if not message:
                message = f"Repair CLI exited with code {exit_code}."
            if self.workspace is not None:
                self.workspace.deny()
            self.workspace = None
            self.failed.emit(message)
            return
        try:
            proposal = self.workspace.build_proposal(self._explanation)
        except Exception as exc:
            self.workspace.deny()
            self.workspace = None
            self.failed.emit(f"Could not build repair diff: {type(exc).__name__}: {exc}")
            return
        if not proposal.get("changes"):
            self.workspace.deny()
            self.workspace = None
            self.failed.emit(
                "The repair agent completed but proposed no file changes.\n"
                + (self._explanation or ""))
            return
        self.current_proposal = proposal
        self.proposal_ready.emit(proposal)

    def _on_process_error(self, error):
        if error == QProcess.FailedToStart and not self._cancel_requested:
            self.busy_changed.emit(False)
            self.failed.emit("Repair CLI failed to start.")

    def cancel(self):
        if not self.is_busy():
            return
        self._cancel_requested = True
        pid = int(self.proc.processId())
        if os.name == "nt" and pid:
            worker = _CancelProcessTreeWorker(pid, self)
            self._cancel_worker = worker
            worker.completed.connect(self._on_cancel_tree_done)
            worker.finished.connect(self._on_cancel_worker_finished)
            worker.start()
        elif self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self.proc.kill()

    def _on_cancel_tree_done(self, result):
        self._cancel_result = dict(result or {})
        self.last_cancel_result = dict(self._cancel_result)
        if (not self._cancel_result.get("all_gone") and self.proc is not None
                and self.proc.state() != QProcess.NotRunning):
            self.proc.kill()
        self._finish_cancel_if_ready()

    def _on_cancel_worker_finished(self):
        worker = self._cancel_worker
        self._cancel_worker = None
        if worker is not None:
            worker.deleteLater()

    def _finish_cancel_if_ready(self):
        if not self._cancel_process_finished:
            return
        if os.name == "nt" and self._cancel_result is None:
            return
        if self.workspace is not None:
            self.workspace.deny()
        self.workspace = None
        self.current_proposal = None
        self.busy_changed.emit(False)
        result = self._cancel_result or {"all_gone": True}
        if result.get("all_gone"):
            self.cancelled.emit()
        else:
            remaining = result.get("remaining_pids") or []
            self.failed.emit(
                "Cancel could not terminate the complete repair process tree; "
                f"remaining PIDs: {remaining}")

    def approve_current(self):
        workspace = self.take_current_for_approval()
        try:
            return workspace.apply()
        finally:
            workspace.deny()

    def take_current_for_approval(self):
        if self.is_busy() or not self.current_proposal or self.workspace is None:
            raise RuntimeError("No completed proposal is ready for approval")
        workspace = self.workspace
        self.workspace = None
        self.current_proposal = None
        return workspace

    def deny_current(self):
        if self.workspace is not None and not self.is_busy():
            self.workspace.deny()
            self.workspace = None
        self.current_proposal = None

    def shutdown(self):
        if self.is_busy():
            self.cancel()
        elif self.workspace is not None:
            self.workspace.deny()
            self.workspace = None
        self.current_proposal = None
