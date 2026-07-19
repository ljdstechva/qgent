# -*- coding: utf-8 -*-
"""The QGIS Copilot chat dock.

Owns, for the panel's lifetime:
  * the execution bridge (socket server + main-thread executor),
  * the generated MCP config the CLI reads,
  * the active agent backend and its per-turn signals,
  * message rendering (bubbles, tool chips, subagent chips, approval cards).
"""
import json
import os
import secrets

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel,
    QPushButton, QToolButton, QMessageBox,
)

from .. import config
from .widgets import (
    MessageBubble, ToolChip, SubagentChip, ApprovalCard, ChatInput,
)
from .settings_dialog import SettingsDialog
from ..bridge.qgis_socket_server import BridgeServer
from ..bridge.main_thread_executor import MainThreadExecutor
from ..context.project_snapshot import build_context_block


class ChatDock(QDockWidget):
    def __init__(self, iface, plugin_dir):
        super().__init__("QGIS Copilot", iface.mainWindow())
        self.iface = iface
        self.plugin_dir = plugin_dir
        self.runtime_dir = os.path.join(plugin_dir, "claude_runtime")
        self.token = secrets.token_hex(16)

        self.executor = None
        self.bridge = None
        self.backend = None
        self._current_bubble = None
        self._last_tool_chip = None
        self._subagent_chips = {}

        self._build_ui()
        self._start_bridge()
        self._write_mcp_config()
        self._build_backend()
        self._post_welcome()

    # ======================================================================
    # UI
    # ======================================================================
    def _build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(6, 6, 6, 6)

        # top bar
        top = QHBoxLayout()
        self.session_label = QLabel("New session")
        self.session_label.setStyleSheet("color: palette(mid);")
        top.addWidget(self.session_label)
        top.addStretch(1)
        self.new_btn = QToolButton()
        self.new_btn.setText("＋ New")
        self.new_btn.clicked.connect(self.new_session)
        top.addWidget(self.new_btn)
        self.settings_btn = QToolButton()
        self.settings_btn.setText("⚙")
        self.settings_btn.setToolTip("Settings")
        self.settings_btn.clicked.connect(self.open_settings)
        top.addWidget(self.settings_btn)
        outer.addLayout(top)

        # message list
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.msg_container = QWidget()
        self.msg_layout = QVBoxLayout(self.msg_container)
        self.msg_layout.setContentsMargins(2, 2, 2, 2)
        self.msg_layout.setSpacing(6)
        self.msg_layout.addStretch(1)
        self.scroll.setWidget(self.msg_container)
        outer.addWidget(self.scroll, 1)

        # status line
        self.status = QLabel("")
        self.status.setStyleSheet("color: palette(mid); font-size: 11px;")
        outer.addWidget(self.status)

        # input row
        self.input = ChatInput()
        self.input.send_requested.connect(self.on_send)
        outer.addWidget(self.input)

        row = QHBoxLayout()
        row.addStretch(1)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.on_stop)
        row.addWidget(self.stop_btn)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.on_send)
        row.addWidget(self.send_btn)
        outer.addLayout(row)

        self.setWidget(root)

    def focus_input(self):
        self.input.setFocus()

    # ======================================================================
    # Bridge + backend
    # ======================================================================
    def _start_bridge(self):
        self.executor = MainThreadExecutor(self.iface, self)
        self.bridge = BridgeServer(self.token, self)
        # Cross-thread → auto-queued to the GUI thread where these live.
        self.bridge.execute_requested.connect(self.executor.handle)
        self.bridge.approval_requested.connect(self.on_approval_requested)
        self.bridge.activity.connect(self._on_bridge_activity)
        port = self.bridge.start()
        self._bridge_port = port

    def _bridge_env(self):
        return {
            "QGIS_COPILOT_HOST": self.bridge.host,
            "QGIS_COPILOT_PORT": str(self._bridge_port),
            "QGIS_COPILOT_TOKEN": self.token,
        }

    def _mcp_config_path(self):
        return os.path.join(self.runtime_dir, "mcp-config.json")

    def _write_mcp_config(self):
        py = config.python_executable()
        bridge_script = os.path.join(self.plugin_dir, "bridge", "mcp_stdio_bridge.py")
        conf = {
            "mcpServers": {
                "qgis": {
                    "command": py,
                    "args": [bridge_script],
                    "env": self._bridge_env(),
                }
            }
        }
        os.makedirs(self.runtime_dir, exist_ok=True)
        with open(self._mcp_config_path(), "w", encoding="utf-8") as fh:
            json.dump(conf, fh, indent=2)
        # Codex reads MCP servers from ~/.codex/config.toml.
        if config.get(config.K_BACKEND) == "codex":
            self._write_codex_toml(py, bridge_script)

    def _write_codex_toml(self, py, bridge_script):
        path = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        env = self._bridge_env()
        env_inline = ", ".join(f'{k} = "{v}"' for k, v in env.items())
        block = (
            "\n[mcp_servers.qgis]\n"
            f'command = "{py.replace(chr(92), chr(92) * 2)}"\n'
            f'args = ["{bridge_script.replace(chr(92), chr(92) * 2)}"]\n'
            f"env = {{ {env_inline} }}\n"
        )
        existing = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                existing = fh.read()
            existing = _strip_toml_block(existing, "[mcp_servers.qgis]")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(existing.rstrip() + "\n" + block)

    def _build_backend(self):
        if self.backend is not None:
            try:
                self.backend.cancel()
            except Exception:
                pass
            self.backend.deleteLater()
            self.backend = None

        backend_kind = config.get(config.K_BACKEND)
        cli_path = config.active_cli_path()
        env = {}  # CLI inherits system env; bridge env goes via mcp-config
        if backend_kind == "codex":
            from ..agent.codex_backend import CodexBackend
            self.backend = CodexBackend(cli_path, self.runtime_dir,
                                        self._mcp_config_path(), env, self)
        else:
            from ..agent.claude_code_backend import ClaudeCodeBackend
            self.backend = ClaudeCodeBackend(cli_path, self.runtime_dir,
                                             self._mcp_config_path(), env, self)

        self.backend.token.connect(self._on_token)
        self.backend.tool_call.connect(self._on_tool_call)
        self.backend.tool_result.connect(self._on_tool_result)
        self.backend.subagent_event.connect(self._on_subagent_event)
        self.backend.session_started.connect(self._on_session_started)
        self.backend.done.connect(self._on_done)
        self.backend.error.connect(self._on_error)
        self.backend.busy_changed.connect(self._on_busy_changed)

    # ======================================================================
    # Sending
    # ======================================================================
    def on_send(self):
        text = self.input.toPlainText().strip()
        if not text:
            return
        if self.backend is None:
            return
        if self.backend.is_busy():
            self.status.setText("Still working — press Stop to interrupt.")
            return
        if not self.backend.cli_path:
            self._add_error(
                "No CLI found. Install the Claude Code CLI "
                "(`npm i -g @anthropic-ai/claude-code`) and log in, then set its "
                "path in ⚙ Settings.")
            return

        self.input.clear()
        self._add_user_message(text)
        self._current_bubble = None
        self._last_tool_chip = None

        try:
            context_block = build_context_block(self.iface)
        except Exception:
            context_block = ""
        self.backend.send(text, context_block)

    def on_stop(self):
        if self.backend is not None:
            self.backend.cancel()
            self.status.setText("Stopped.")

    def new_session(self):
        if self.backend is not None and self.backend.is_busy():
            self.backend.cancel()
        # Fresh session id; keep the same bridge (port/token still valid).
        self._build_backend()
        self._clear_messages()
        self.session_label.setText("New session")
        self._post_welcome()

    def open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec_():
            # Backend or model choice may have changed — rebuild + rewrite config.
            self._write_mcp_config()
            self._build_backend()
            self.status.setText("Settings applied.")

    # ======================================================================
    # Backend signal handlers
    # ======================================================================
    def _on_token(self, text):
        if self._current_bubble is None:
            self._current_bubble = MessageBubble("assistant")
            self._add_widget(self._current_bubble)
        self._current_bubble.append_text(text)
        self._scroll_to_bottom()

    def _on_tool_call(self, name, args):
        self._current_bubble = None
        chip = ToolChip(name, args)
        self._last_tool_chip = chip
        self._add_widget(chip)
        self.status.setText(f"Running {name.replace('mcp__qgis__', '')}…")

    def _on_tool_result(self, text):
        if self._last_tool_chip is not None:
            self._last_tool_chip.set_result(text)

    def _on_subagent_event(self, name, status):
        self._current_bubble = None
        if status == "started":
            chip = SubagentChip(name)
            self._subagent_chips[name] = chip
            self._add_widget(chip)
            self.status.setText(f"🤖 {name} working…")
        elif status == "finished":
            chip = self._subagent_chips.get(name)
            if chip is not None:
                chip.set_finished()

    def _on_session_started(self, session_id):
        short = session_id[:8]
        backend = config.get(config.K_BACKEND).capitalize()
        self.session_label.setText(f"{backend} · session {short}…")

    def _on_done(self, _result):
        self._current_bubble = None
        self.status.setText("Ready.")

    def _on_error(self, message):
        self._current_bubble = None
        self._add_error(message)
        self.status.setText("Error.")

    def _on_busy_changed(self, busy):
        self.send_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        if busy:
            self.status.setText("Thinking…")

    def _on_bridge_activity(self, tool):
        # Fires from the socket thread (queued); low-noise status hint.
        self.status.setText(f"QGIS: {tool.replace('mcp__qgis__', '')}")

    # ======================================================================
    # Approval gate
    # ======================================================================
    def on_approval_requested(self, ap):
        self._current_bubble = None
        card = ApprovalCard(ap.get("code", ""), ap.get("reasons", []))

        def decide(approved):
            ap["approved"] = approved
            ap["event"].set()

        card.decided.connect(decide)
        self._add_widget(card)
        self._scroll_to_bottom()

    # ======================================================================
    # Message list helpers
    # ======================================================================
    def _add_user_message(self, text):
        bubble = MessageBubble("user")
        self._add_widget(bubble)
        bubble.set_text(text)

    def _add_error(self, text):
        bubble = MessageBubble("assistant")
        self._add_widget(bubble)
        bubble.set_text("**⚠ " + text + "**")

    def _add_widget(self, widget):
        # Insert before the trailing stretch.
        self.msg_layout.insertWidget(self.msg_layout.count() - 1, widget)
        self._scroll_to_bottom()

    def _clear_messages(self):
        while self.msg_layout.count() > 1:
            item = self.msg_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._current_bubble = None
        self._last_tool_chip = None
        self._subagent_chips.clear()

    def _scroll_to_bottom(self):
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()))

    def _post_welcome(self):
        cli = config.active_cli_path()
        backend = config.get(config.K_BACKEND).capitalize()
        if cli:
            msg = (f"**QGIS Copilot ready** ({backend}).\n\nDescribe a GIS task — "
                   "e.g. *“buffer the active layer by 100 m and add the result”* "
                   "or *“make an A3 vicinity map in EPSG:32651”*. I execute PyQGIS "
                   "directly in this project and ask before anything destructive.")
        else:
            msg = ("**No agent CLI found.** Install the Claude Code CLI "
                   "(`npm i -g @anthropic-ai/claude-code`) and log in with your "
                   "Claude subscription, then open ⚙ Settings to set its path.")
        bubble = MessageBubble("assistant")
        self._add_widget(bubble)
        bubble.set_text(msg)

    # ======================================================================
    # Teardown
    # ======================================================================
    def shutdown(self):
        if self.backend is not None:
            try:
                self.backend.cancel()
            except Exception:
                pass
        if self.bridge is not None:
            self.bridge.stop()


def _strip_toml_block(text, header):
    """Remove a top-level TOML table block starting at ``header``."""
    lines = text.splitlines()
    out = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped == header:
            skipping = True
            continue
        if skipping and stripped.startswith("[") and stripped.endswith("]"):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out)
