# -*- coding: utf-8 -*-
"""In-QGIS localhost socket server that the MCP stdio bridge talks to.

Design notes
------------
* Binds ``127.0.0.1:0`` (ephemeral port) so nothing external can reach it, and
  authenticates every request with a per-session token passed to the bridge via
  an environment variable. Both defend against other local processes driving
  your QGIS.
* Serving happens in plain daemon threads (``ThreadingTCPServer``). The
  ``BridgeServer`` QObject itself lives on the GUI thread, so emitting its
  signals from a handler thread is auto-promoted to a queued connection —
  delivering work to the main-thread executor without any ``moveToThread``.
* The approval gate for destructive code runs *here*, before execution, so it
  covers every subagent regardless of prompt content.

Wire protocol: newline-delimited JSON, one request / one response per line.
  request : {"token": str, "tool": str, "args": {...}}
  response: {"ok": bool, "result": str}  |  {"ok": false, "error": str}
"""
import json
import socketserver
import threading

from qgis.PyQt.QtCore import QObject, pyqtSignal

from .. import config
from . import safety

_APPROVAL_TIMEOUT_S = 300


class BridgeServer(QObject):
    execute_requested = pyqtSignal(object)   # -> MainThreadExecutor.handle
    approval_requested = pyqtSignal(object)  # -> ChatDock.on_approval_requested
    approval_finished = pyqtSignal(object)   # closes timed-out/cancelled cards
    auto_approved = pyqtSignal(object)       # destructive batch operation metadata
    activity = pyqtSignal(str)               # tool name, for the status line

    def __init__(self, token, parent=None):
        super().__init__(parent)
        self._token = token
        self.host = "127.0.0.1"
        self.port = None
        self._server = None
        self._thread = None
        self._gate_lock = threading.RLock()
        self._batch_permission_mode = None
        self._pending_approvals = {}
        self._approval_sequence = 0

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        self._server = _TokenTCPServer((self.host, 0), _Handler, bridge=self)
        self.host, self.port = self._server.server_address[:2]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="qgis-copilot-bridge",
            daemon=True)
        self._thread.start()
        return self.port

    def stop(self):
        self.cancel_pending_approvals("QGent bridge stopped")
        self.clear_batch_permission_mode()
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None

    # -- batch-scoped approval policy --------------------------------------
    def set_batch_permission_mode(self, mode):
        """Install a non-persistent permission override for one queue run."""
        if mode not in ("auto", "ask_destructive", "ask_always"):
            raise ValueError(f"Unsupported batch permission mode: {mode!r}")
        with self._gate_lock:
            self._batch_permission_mode = mode

    def clear_batch_permission_mode(self):
        with self._gate_lock:
            previous = self._batch_permission_mode
            self._batch_permission_mode = None
        return previous

    def effective_permission_mode(self):
        with self._gate_lock:
            override = self._batch_permission_mode
        return override or config.get(config.K_PERMISSION_MODE)

    def batch_permission_mode(self):
        with self._gate_lock:
            return self._batch_permission_mode

    def cancel_pending_approvals(self, reason="operation cancelled"):
        """Release every waiting bridge thread and make its card inert."""
        with self._gate_lock:
            pending = list(self._pending_approvals.values())
            for approval in pending:
                approval["cancelled"] = True
                approval["cancel_reason"] = str(reason)
                approval["approved"] = False
                approval["event"].set()
        return len(pending)

    # -- request handling (runs in a handler thread) ------------------------
    def check_token(self, token):
        return bool(self._token) and token == self._token

    def process(self, tool, args):
        """Return (ok: bool, payload_str: str). Blocks the handler thread only."""
        self.activity.emit(tool)

        if tool == "execute_pyqgis":
            denied = self._maybe_gate(args.get("code", ""))
            if denied is not None:
                return False, denied

        payload = {"tool": tool, "args": args, "result": None,
                   "error": None, "event": threading.Event()}
        self.execute_requested.emit(payload)
        timeout = config.get(config.K_EXEC_TIMEOUT)
        if not payload["event"].wait(timeout=timeout + 5):
            return False, (f"TIMEOUT: '{tool}' did not finish within "
                           f"{timeout}s. It may still be running in QGIS; "
                           f"consider chunking the work or using a QgsTask.")
        if payload["error"]:
            return False, payload["error"]
        return True, payload["result"] if payload["result"] is not None else ""

    def _maybe_gate(self, code):
        """Return a denial string if the user rejects, else None to proceed."""
        reasons = safety.scan(code)
        with self._gate_lock:
            override = self._batch_permission_mode
        mode = override or config.get(config.K_PERMISSION_MODE)
        need = mode == "ask_always" or (mode == "ask_destructive" and reasons)
        if not need:
            if mode == "auto" and reasons:
                self.auto_approved.emit({
                    "code": code,
                    "reasons": list(reasons),
                    "batch_scoped": override == "auto",
                })
            return None
        with self._gate_lock:
            self._approval_sequence += 1
            approval_id = f"bridge-{self._approval_sequence}"
            ap = {
                "approval_id": approval_id,
                "code": code,
                "reasons": reasons,
                "event": threading.Event(),
                "approved": None,
                "cancelled": False,
                "cancel_reason": "",
            }
            self._pending_approvals[approval_id] = ap
        self.approval_requested.emit(ap)
        try:
            if not ap["event"].wait(timeout=_APPROVAL_TIMEOUT_S):
                ap["cancelled"] = True
                ap["cancel_reason"] = "approval timed out"
                ap["approved"] = False
                ap["event"].set()
                return "DENIED: approval timed out (no response from the user)."
            if ap.get("cancelled"):
                return "CANCELLED: " + (
                    ap.get("cancel_reason") or "approval was cancelled") + "."
            if not ap["approved"]:
                return "DENIED: the user declined to run this code."
            return None
        finally:
            with self._gate_lock:
                self._pending_approvals.pop(approval_id, None)
            self.approval_finished.emit({
                "approval_id": approval_id,
                "cancelled": bool(ap.get("cancelled")),
                "reason": str(ap.get("cancel_reason") or ""),
            })


class _TokenTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, handler, bridge):
        super().__init__(addr, handler)
        self.bridge = bridge


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        bridge = self.server.bridge
        for raw in self.rfile:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as exc:
                self._reply({"ok": False, "error": f"bad JSON: {exc}"})
                continue
            if not bridge.check_token(req.get("token")):
                self._reply({"ok": False, "error": "auth failed"})
                continue
            tool = req.get("tool", "")
            args = req.get("args") or {}
            try:
                ok, payload = bridge.process(tool, args)
            except Exception as exc:  # never kill the handler loop
                self._reply({"ok": False, "error": f"bridge error: {exc}"})
                continue
            self._reply({"ok": ok, "result": payload} if ok
                        else {"ok": False, "error": payload})

    def _reply(self, obj):
        data = (json.dumps(obj) + "\n").encode("utf-8")
        self.wfile.write(data)
        self.wfile.flush()
