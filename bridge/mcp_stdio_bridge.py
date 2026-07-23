#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone MCP server (stdio) launched by the Claude Code / Codex CLI.

STDLIB ONLY. This process must never import qgis/Qt — it runs outside QGIS and
is spawned by the CLI as an MCP server. It speaks MCP (JSON-RPC 2.0 over
newline-delimited stdio) to the CLI, and forwards each tool call to the in-QGIS
socket server over 127.0.0.1.

Connection details come from the environment (written by the plugin at session
start):
    QGIS_COPILOT_HOST, QGIS_COPILOT_PORT, QGIS_COPILOT_TOKEN
"""

import json
import os
import socket
import sys


def _configure_utf8_stdio():
    """Decode MCP pipe bytes as UTF-8 regardless of the Windows ANSI locale.

    Codex emits raw Unicode in JSON-RPC tool arguments.  A pipe-backed Python
    process on Windows can otherwise decode those UTF-8 bytes as cp1252 before
    ``json.loads`` sees them (for example U+00B0 becomes U+00C2 U+00B0).
    Reconfigure before the first read so the model payload stays Unicode.
    """
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


_configure_utf8_stdio()

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "qgis"

HOST = os.environ.get("QGIS_COPILOT_HOST", "127.0.0.1")
PORT = int(os.environ.get("QGIS_COPILOT_PORT", "0"))
TOKEN = os.environ.get("QGIS_COPILOT_TOKEN", "")
_QUESTION_SOCKET_TIMEOUT_S = 15 * 60 + 30

# --- tool catalogue (executor tools plus bridge-owned ask_user) ------------
TOOLS = [
    {
        "name": "execute_pyqgis",
        "description": (
            "Run arbitrary PyQGIS on the QGIS GUI thread and return captured "
            "stdout (truncated). Pre-injected names: iface, QgsProject, "
            "processing, and all qgis.core.Qgs* classes. This is the workhorse "
            "— prefer ONE script that does a whole workflow over many small "
            "calls. Destructive code triggers a user approval prompt."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string",
                                    "description": "PyQGIS source to exec()."}},
            "required": ["code"],
        },
    },
    {
        "name": "get_project_context",
        "description": (
            "Return the live project state as JSON: project CRS, every layer "
            "(name, id, type, CRS, geometry, feature count), the active layer, "
            "and the canvas extent/CRS. Cheap; use to ground layer/field names."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_processing",
        "description": (
            "Thin, validated wrapper over processing.run(alg_id, params). "
            "OUTPUT defaults to TEMPORARY_OUTPUT. Returns the output map as "
            "JSON. You may also just call execute_pyqgis for full control."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "alg_id": {"type": "string",
                           "description": "e.g. 'native:buffer'."},
                "params": {"type": "object",
                           "description": "Algorithm parameter dict."},
            },
            "required": ["alg_id", "params"],
        },
    },
    {
        "name": "get_layer_features",
        "description": (
            "Sample attribute rows from a vector layer safely (row-capped at "
            "200). Returns JSON. Use to inspect data, never to dump a full "
            "table into context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "layer": {"type": "string",
                          "description": "Layer name or id (blank = active)."},
                "limit": {"type": "integer", "description": "Max rows (<=200)."},
                "filter_expr": {"type": "string",
                                "description": "Optional QGIS expression filter."},
            },
        },
    },
    {
        "name": "render_map_snapshot",
        "description": (
            "Render the current QGIS canvas to a PNG and return its file path, "
            "so you can read the image to see your work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "width": {"type": "integer"},
                "height": {"type": "integer"},
            },
        },
    },
    {
        "name": "stat_path",
        "description": (
            "Return strictly read-only metadata for one filesystem path as "
            "JSON: existence, file/directory type, byte size, and ISO mtime. "
            "Does not read file contents, list directories, or write anything."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Exact file or directory path to inspect.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "ask_user",
        "description": (
            "Ask one structured clarifying question only when unresolved "
            "ambiguity would materially change the GIS outcome or safety. "
            "Do not use this for minor preferences covered by stated defaults; "
            "offer concrete choices instead of an open-ended question. For "
            "a choice between project layers, always set allow_other=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "description": "A concise, outcome-changing question.",
                },
                "options": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 5,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 80,
                    },
                    "description": "Two to five concrete choices.",
                },
                "allow_other": {
                    "type": "boolean",
                    "default": True,
                    "description": "Allow a short custom answer.",
                },
            },
            "required": ["question", "options"],
        },
    },
]


# --- socket forwarding -----------------------------------------------------
def call_qgis(tool, args):
    """Send one request to the in-QGIS socket server; return (ok, text)."""
    req = json.dumps({"token": TOKEN, "tool": tool, "args": args}) + "\n"
    timeout = _QUESTION_SOCKET_TIMEOUT_S if tool == "ask_user" else 180
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout) as sock:
            sock.sendall(req.encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
            line = buf.split(b"\n", 1)[0].decode("utf-8", "replace")
    except OSError as exc:
        return False, (f"Could not reach QGIS bridge at {HOST}:{PORT} ({exc}). "
                       "Is the QGent panel still open?")
    if not line:
        return False, "Empty response from QGIS bridge."
    try:
        resp = json.loads(line)
    except json.JSONDecodeError as exc:
        return False, f"Bad response from QGIS bridge: {exc}"
    if resp.get("ok"):
        return True, resp.get("result", "")
    return False, resp.get("error", "unknown bridge error")


# --- MCP JSON-RPC plumbing -------------------------------------------------
def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def result(msg_id, payload):
    send({"jsonrpc": "2.0", "id": msg_id, "result": payload})


def error(msg_id, code, message):
    send({"jsonrpc": "2.0", "id": msg_id,
          "error": {"code": code, "message": message}})


def handle(msg):
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        result(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
        })
    elif method == "notifications/initialized":
        pass  # notification, no reply
    elif method == "ping":
        result(msg_id, {})
    elif method == "tools/list":
        result(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in {t["name"] for t in TOOLS}:
            error(msg_id, -32602, f"unknown tool: {name}")
            return
        ok, text = call_qgis(name, args)
        result(msg_id, {
            "content": [{"type": "text", "text": text}],
            "isError": not ok,
        })
    elif msg_id is not None:
        error(msg_id, -32601, f"method not found: {method}")
    # else: unknown notification — ignore.


def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        try:
            handle(msg)
        except Exception as exc:  # keep the server alive
            if msg.get("id") is not None:
                error(msg.get("id"), -32603, f"internal error: {exc}")


if __name__ == "__main__":
    main()
