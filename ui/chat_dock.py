# -*- coding: utf-8 -*-
"""The QGent chat dock.

Owns, for the panel's lifetime:
  * the execution bridge (socket server + main-thread executor),
  * the generated MCP config the CLI reads,
  * the active agent backend and its per-turn signals,
  * message rendering (bubbles, tool chips, subagent chips, approval cards).

UI notes: streamed tokens are coalesced on a 40 ms timer before rendering
(markdown re-layout per delta is O(n^2) otherwise), and all entrance/state
animations honour the "reduce motion" setting.

The per-turn benchmark instrumentation (``_perf`` / ``_finish_perf``) is
load-bearing for benchmark comparability — do not restructure it.
"""
import csv
import datetime
import json
import os
import secrets
import time

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel,
    QToolButton, QFrame,
)

from .. import config
from . import theme
from .animations import fade_in, smooth_scroll_to_bottom, staggered, ThinkingDots
from .widgets import (
    MessageBubble, ToolChip, SubagentChip, ApprovalCard, ChatInput,
    SendStopButton, SuggestionChip,
)
from .settings_dialog import SettingsDialog
from ..bridge.qgis_socket_server import BridgeServer
from ..bridge.main_thread_executor import MainThreadExecutor
from ..context.project_snapshot import build_context_block

_SUGGESTIONS = [
    "What CRS is this project?",
    "Buffer the active layer by 100 m and add the result",
    "Make an A3 vicinity map of the current canvas extent",
]


class ChatDock(QDockWidget):
    def __init__(self, iface, plugin_dir):
        super().__init__("QGent", iface.mainWindow())
        self.iface = iface
        self.plugin_dir = plugin_dir
        self.runtime_dir = os.path.join(plugin_dir, "claude_runtime")
        self.token = secrets.token_hex(16)
        self.t = theme.Tokens()

        self.executor = None
        self.bridge = None
        self.backend = None
        self._current_bubble = None
        self._last_tool_chip = None
        self._subagent_chips = {}
        self._perf = None
        self.hero = None

        # token coalescing
        self._stream_buf = ""
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(40)
        self._flush_timer.timeout.connect(self._flush_tokens)

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
        root.setObjectName("QgentRoot")
        root.setStyleSheet(theme.build_qss(self.t))
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # -- header --------------------------------------------------------
        top = QHBoxLayout()
        top.setSpacing(8)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        wordmark = QLabel("QGent")
        wordmark.setObjectName("QgentWordmark")
        subtitle = QLabel("A QGIS AI Agent")
        subtitle.setObjectName("QgentSubtitle")
        brand.addWidget(wordmark)
        brand.addWidget(subtitle)
        top.addLayout(brand)
        top.addStretch(1)
        self.session_label = QLabel("new session")
        self.session_label.setObjectName("QgentSessionPill")
        top.addWidget(self.session_label)
        self.new_btn = QToolButton()
        self.new_btn.setObjectName("QgentIconBtn")
        self.new_btn.setText("✚")
        self.new_btn.setToolTip("New session")
        self.new_btn.setCursor(Qt.PointingHandCursor)
        self.new_btn.clicked.connect(self.new_session)
        top.addWidget(self.new_btn)
        self.settings_btn = QToolButton()
        self.settings_btn.setObjectName("QgentIconBtn")
        self.settings_btn.setText("⚙")
        self.settings_btn.setToolTip("Settings")
        self.settings_btn.setCursor(Qt.PointingHandCursor)
        self.settings_btn.clicked.connect(self.open_settings)
        top.addWidget(self.settings_btn)
        outer.addLayout(top)

        # -- message list --------------------------------------------------
        self.scroll = QScrollArea()
        self.scroll.setObjectName("QgentScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.msg_container = QWidget()
        self.msg_container.setObjectName("QgentMsgContainer")
        self.msg_layout = QVBoxLayout(self.msg_container)
        self.msg_layout.setContentsMargins(2, 2, 2, 2)
        self.msg_layout.setSpacing(8)
        self.msg_layout.addStretch(1)
        self.scroll.setWidget(self.msg_container)
        # let the themed root background show through the scroll area
        self.scroll.viewport().setAutoFillBackground(False)
        self.msg_container.setAutoFillBackground(False)
        outer.addWidget(self.scroll, 1)

        # -- activity strip ------------------------------------------------
        activity = QWidget()
        activity.setObjectName("QgentActivity")
        arow = QHBoxLayout(activity)
        arow.setContentsMargins(4, 0, 4, 0)
        arow.setSpacing(6)
        self.dots = ThinkingDots(self.t.accent)
        self.dots.hide()
        arow.addWidget(self.dots)
        self.status = QLabel("")
        arow.addWidget(self.status)
        arow.addStretch(1)
        outer.addWidget(activity)

        # -- composer ------------------------------------------------------
        composer = QFrame()
        composer.setObjectName("QgentComposer")
        crow = QHBoxLayout(composer)
        crow.setContentsMargins(12, 4, 6, 4)
        crow.setSpacing(6)
        self.input = ChatInput()
        self.input.send_requested.connect(self.on_send)
        self.input.focus_changed.connect(
            lambda on: self._set_composer_focus(composer, on))
        crow.addWidget(self.input, 1)
        self.action_btn = SendStopButton(self.t)
        self.action_btn.clicked.connect(self._on_action_clicked)
        crow.addWidget(self.action_btn, 0, Qt.AlignBottom)
        outer.addWidget(composer)

        self.setWidget(root)

    def _set_composer_focus(self, composer, on):
        composer.setProperty("focused", "true" if on else "false")
        theme.repolish(composer)

    def focus_input(self):
        self.input.setFocus()

    # -- activity helpers ---------------------------------------------------
    def _set_activity(self, text):
        self.status.setText(text)

    def _show_thinking(self):
        self.dots.start()
        self.status.setText("Thinking…")

    def _hide_thinking(self):
        self.dots.halt()

    # ======================================================================
    # Bridge + backend  (unchanged plumbing)
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
    def _on_action_clicked(self):
        if self.action_btn.is_busy_state():
            self.on_stop()
        else:
            self.on_send()

    def on_send(self):
        text = self.input.toPlainText().strip()
        if not text:
            return
        if self.backend is None:
            return
        if self.backend.is_busy():
            self._set_activity("Still working — press Stop to interrupt.")
            return
        if not self.backend.cli_path:
            self._add_error(
                "No CLI found. Install the Claude Code CLI "
                "(`npm i -g @anthropic-ai/claude-code`) and log in, then set its "
                "path in ⚙ Settings.")
            return

        marker = text.split(maxsplit=1)[0].upper()
        task_id = marker[1:-1] if marker in (
            "[T1]", "[T2]", "[T3]", "[T4]", "[T5]", "[T6]", "[SMOKE]") else "adhoc"
        backend = config.get(config.K_BACKEND)
        self._perf = {"start": time.monotonic(), "task_id": task_id,
                      "ttft_ms": None, "tool_calls": 0, "delegations": 0,
                      "backend": backend,
                      "model": config.get(config.K_MODEL_SUPERVISOR) if backend == "claude" else ""}
        self.input.clear()
        self._add_user_message(text)
        self._current_bubble = None
        self._last_tool_chip = None
        self._show_thinking()

        try:
            context_block = build_context_block(self.iface)
        except Exception:
            context_block = ""
        self.backend.send(text, context_block)

    def on_stop(self):
        if self.backend is not None:
            self.backend.cancel()
            self._end_stream()
            self._hide_thinking()
            self._set_activity("Stopped.")
            self._finish_perf()

    def new_session(self):
        if self.backend is not None and self.backend.is_busy():
            self.backend.cancel()
            self._finish_perf()
        # Fresh session id; keep the same bridge (port/token still valid).
        self._build_backend()
        self._clear_messages()
        self.session_label.setText("new session")
        self._post_welcome()

    def open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec_():
            # Backend or model choice may have changed — rebuild + rewrite config.
            self._write_mcp_config()
            self._build_backend()
            self._set_activity("Settings applied.")

    # ======================================================================
    # Streaming (coalesced)
    # ======================================================================
    def _on_token(self, text):
        # TTFT is recorded at signal arrival, before coalescing, so the
        # benchmark metric is unaffected by the 40 ms flush timer.
        if self._perf is not None and self._perf["ttft_ms"] is None:
            self._perf["ttft_ms"] = round((time.monotonic() - self._perf["start"]) * 1000)
        self._hide_thinking()
        if self._current_bubble is None:
            self._current_bubble = MessageBubble("assistant", self.t)
            self._add_widget(self._current_bubble)
            self._current_bubble.set_streaming(True)
        self._stream_buf += text
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_tokens(self):
        if self._stream_buf and self._current_bubble is not None:
            self._current_bubble.append_text(self._stream_buf)
            self._stream_buf = ""
            smooth_scroll_to_bottom(self.scroll)

    def _end_stream(self):
        """Flush pending text and finish the streaming bubble."""
        self._flush_timer.stop()
        self._flush_tokens()
        if self._current_bubble is not None:
            self._current_bubble.set_streaming(False)
        self._current_bubble = None

    # ======================================================================
    # Backend signal handlers
    # ======================================================================
    def _on_tool_call(self, name, args):
        if self._perf is not None:
            self._perf["tool_calls"] += 1
        self._end_stream()
        self._hide_thinking()
        chip = ToolChip(name, args, self.t)
        self._last_tool_chip = chip
        self._add_widget(chip)
        self._set_activity(f"Running {name.replace('mcp__qgis__', '')}…")

    def _on_tool_result(self, text):
        if self._last_tool_chip is not None:
            self._last_tool_chip.set_result(text)

    def _on_subagent_event(self, name, status):
        self._end_stream()
        if status == "started":
            if self._perf is not None:
                self._perf["delegations"] += 1
            self._hide_thinking()
            chip = SubagentChip(name, self.t)
            self._subagent_chips[name] = chip
            self._add_widget(chip)
            self._set_activity(f"{name} working…")
        elif status == "finished":
            chip = self._subagent_chips.get(name)
            if chip is not None:
                chip.set_finished()

    def _on_session_started(self, session_id):
        short = session_id[:8]
        backend = config.get(config.K_BACKEND)
        self.session_label.setText(f"● {backend} · {short}")

    def _on_done(self, _result):
        self._end_stream()
        self._hide_thinking()
        if self._last_tool_chip is not None:
            self._last_tool_chip.mark_done_without_result()
        for chip in self._subagent_chips.values():
            if chip._shimmer_pos is not None:  # never got its "finished" event
                chip.set_finished()
        self._set_activity("Ready.")
        self._finish_perf()

    def _on_error(self, message):
        self._end_stream()
        self._hide_thinking()
        self._add_error(message)
        self._set_activity("Error.")
        self._finish_perf()

    def _finish_perf(self):
        turn = self._perf
        if turn is None:
            return
        self._perf = None
        path = os.path.join(os.environ["TEMP"], "qgis_copilot_perf.csv")
        add_header = not os.path.exists(path) or os.path.getsize(path) == 0
        row = [datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"),
               turn["task_id"], turn["ttft_ms"] if turn["ttft_ms"] is not None else "",
               round((time.monotonic() - turn["start"]) * 1000), turn["tool_calls"],
               turn["delegations"], turn["backend"], turn["model"]]
        with open(path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if add_header:
                writer.writerow(["timestamp", "task_id", "ttft_ms", "total_ms", "tool_calls",
                                 "delegations", "backend", "model"])
            writer.writerow(row)

    def _on_busy_changed(self, busy):
        self.action_btn.set_busy(busy)
        if busy:
            self._show_thinking()
        else:
            self._hide_thinking()

    def _on_bridge_activity(self, tool):
        # Fires from the socket thread (queued); low-noise status hint.
        self._set_activity(f"QGIS: {tool.replace('mcp__qgis__', '')}")

    # ======================================================================
    # Approval gate
    # ======================================================================
    def on_approval_requested(self, ap):
        self._end_stream()
        card = ApprovalCard(ap.get("code", ""), ap.get("reasons", []), self.t)

        def decide(approved):
            ap["approved"] = approved
            ap["event"].set()

        card.decided.connect(decide)
        self._add_widget(card)
        card.attention()

    # ======================================================================
    # Message list helpers
    # ======================================================================
    def _add_user_message(self, text):
        bubble = MessageBubble("user", self.t)
        self._add_widget(bubble)
        bubble.set_text(text)

    def _add_error(self, text):
        bubble = MessageBubble("assistant", self.t)
        bubble.set_error()
        self._add_widget(bubble)
        bubble.set_text("**⚠ " + text + "**")

    def _add_widget(self, widget):
        if self.hero is not None and self.hero.isVisible():
            self.hero.hide()
        # Insert before the trailing stretch.
        self.msg_layout.insertWidget(self.msg_layout.count() - 1, widget)
        fade_in(widget)
        smooth_scroll_to_bottom(self.scroll)

    def _clear_messages(self):
        while self.msg_layout.count() > 1:
            item = self.msg_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.hero = None
        self._current_bubble = None
        self._last_tool_chip = None
        self._subagent_chips.clear()
        self._stream_buf = ""

    # ======================================================================
    # Empty state
    # ======================================================================
    def _post_welcome(self):
        self._show_hero()
        if not config.active_cli_path():
            self._add_error(
                "No agent CLI found. Install the Claude Code CLI "
                "(`npm i -g @anthropic-ai/claude-code`) and log in with your "
                "Claude subscription, then open ⚙ Settings to set its path.")

    def _show_hero(self):
        hero = QWidget()
        hero.setObjectName("QgentHero")
        lay = QVBoxLayout(hero)
        lay.setContentsMargins(10, 28, 10, 10)
        lay.setSpacing(10)

        greet = QLabel("Hi, I'm QGent 🌏")
        greet.setObjectName("QgentGreeting")
        greet.setAlignment(Qt.AlignCenter)
        lay.addWidget(greet)
        sub = QLabel("Describe a GIS task in plain language. I run PyQGIS\n"
                     "directly in this project — and always ask before\n"
                     "anything destructive.")
        sub.setObjectName("QgentGreetingSub")
        sub.setAlignment(Qt.AlignCenter)
        lay.addWidget(sub)
        lay.addSpacing(8)

        chips = []
        for text in _SUGGESTIONS:
            chip = SuggestionChip(text)
            chip.clicked.connect(lambda _=False, s=text: self._use_suggestion(s))
            lay.addWidget(chip)
            chips.append(chip)

        self.hero = hero
        self.msg_layout.insertWidget(0, hero)
        staggered(chips)

    def _use_suggestion(self, text):
        self.input.setPlainText(text)
        self.input.setFocus()
        cursor = self.input.textCursor()
        cursor.movePosition(cursor.End)
        self.input.setTextCursor(cursor)

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
