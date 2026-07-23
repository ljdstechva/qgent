from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading


BRIDGE = Path(__file__).resolve().parents[1] / "bridge" / "mcp_stdio_bridge.py"
DEGREE_SIGN = chr(0x00B0)
PRIME = chr(0x2032)
DOUBLE_PRIME = chr(0x2033)


def test_raw_model_payload_survives_windows_ansi_bridge_stdio() -> None:
    """Reproduce and guard the original MCP pipe boundary at codepoint level."""

    coordinate = (
        f"5{DEGREE_SIGN}52{PRIME}23.4{DOUBLE_PRIME}N, "
        f"125{DEGREE_SIGN}04{PRIME}48.9{DOUBLE_PRIME}E"
    )
    code = f"layout.itemById('corp_site_coordinates').setText({coordinate!r})"
    captured: dict[str, object] = {}
    ready = threading.Event()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def qgis_stub() -> None:
        ready.set()
        connection, _ = server.accept()
        with connection:
            raw = b""
            while b"\n" not in raw:
                raw += connection.recv(65536)
            request = json.loads(raw.split(b"\n", 1)[0].decode("utf-8"))
            captured.update(request)
            response = json.dumps(
                {"ok": True, "result": "UNICODE_BRIDGE_OK"}, ensure_ascii=False
            )
            connection.sendall((response + "\n").encode("utf-8"))
        server.close()

    worker = threading.Thread(target=qgis_stub, daemon=True)
    worker.start()
    assert ready.wait(2)

    model_tool_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "execute_pyqgis",
            "arguments": {"code": code},
        },
    }
    raw_model_bytes = (
        json.dumps(model_tool_payload, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONIOENCODING": "cp1252",
            "QGIS_COPILOT_HOST": "127.0.0.1",
            "QGIS_COPILOT_PORT": str(port),
            "QGIS_COPILOT_TOKEN": "fixture-token",
        }
    )
    process = subprocess.run(
        [sys.executable, str(BRIDGE)],
        input=raw_model_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        timeout=10,
        check=False,
    )
    worker.join(timeout=2)

    assert process.returncode == 0, process.stderr.decode("utf-8", "replace")
    response = json.loads(process.stdout.decode("utf-8"))
    assert response["result"]["isError"] is False
    assert response["result"]["content"][0]["text"] == "UNICODE_BRIDGE_OK"
    captured_code = captured["args"]["code"]  # type: ignore[index]
    assert captured_code == code
    assert coordinate in captured_code
    assert chr(0x00C2) not in captured_code
    assert [ord(char) for char in coordinate if ord(char) > 127] == [
        0x00B0,
        0x2032,
        0x2033,
        0x00B0,
        0x2032,
        0x2033,
    ]
