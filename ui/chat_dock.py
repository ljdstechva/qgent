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

from qgis.PyQt.QtCore import Qt, QTimer, QCoreApplication
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel,
    QToolButton, QFrame,
)
from qgis.core import (
    QgsApplication, QgsLayerTreeGroup, QgsLayerTreeLayer, QgsMessageLog,
    QgsProject,
)

from .. import config
from . import theme
from .animations import fade_in, smooth_scroll_to_bottom, staggered, ThinkingDots
from .widgets import (
    MessageBubble, ToolChip, SubagentChip, ApprovalCard, ChatInput,
    SendStopButton, SuggestionChip, ContextStrip, StatusNote,
)
from .settings_dialog import SettingsDialog
from ..bridge.qgis_socket_server import BridgeServer
from ..bridge.main_thread_executor import MainThreadExecutor
from ..context.project_snapshot import build_context_block, snapshot_layer
from ..history import HistoryStore, bounded_json_value, clipped_text

_SUGGESTIONS = [
    "What CRS is this project?",
    "Buffer the active layer by 100 m and add the result",
    "Make an A3 vicinity map of the current canvas extent",
]


class ChatDock(QDockWidget):
    def __init__(self, iface, plugin_dir, diagnostic_logs=None):
        super().__init__("QGent", iface.mainWindow())
        self.iface = iface
        self.plugin_dir = plugin_dir
        self.runtime_dir = os.path.join(plugin_dir, "claude_runtime")
        self.diagnostic_logs = (diagnostic_logs if diagnostic_logs is not None
                                else [])
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
        self._layer_tree_view = None
        self._layer_selection_model = None
        self.history_store = None
        self._history_state = None
        self._history_replaying = False
        self._backend_kind = None
        self._last_tool_id = None
        self._last_tool_name = ""
        self._last_tool_args = {}
        self._last_tool_finished = True
        self._subagent_history = {}
        self._approval_history = {}
        self._active_turn = None

        # token coalescing
        self._stream_buf = ""
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(40)
        self._flush_timer.timeout.connect(self._flush_tokens)

        self._build_ui()
        self._connect_layer_selection()
        self._start_bridge()
        self._write_mcp_config()
        self._init_history()
        self._build_backend()
        restored = self._restore_history(self._history_state)
        if not restored:
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

        # -- selected-layer context ---------------------------------------
        self.context_strip = ContextStrip()
        outer.addWidget(self.context_strip)

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

    # -- selected-layer context --------------------------------------------
    def _connect_layer_selection(self):
        """Keep the live strip synchronized with the Layers panel."""
        try:
            view = self.iface.layerTreeView()
            selection_model = view.selectionModel()
            if selection_model is None:
                return
            selection_model.selectionChanged.connect(
                self._on_layer_selection_changed)
            self._layer_tree_view = view
            self._layer_selection_model = selection_model
            self._update_layer_context_strip()
        except (AttributeError, RuntimeError, TypeError):
            self.context_strip.set_items([])

    def _on_layer_selection_changed(self, *_args):
        self._update_layer_context_strip()

    def _selection_chip_items(self):
        view = self._layer_tree_view
        if view is None:
            return []
        try:
            nodes = list(view.selectedNodes(False))
        except (AttributeError, RuntimeError, TypeError):
            return []

        items = []
        for node in nodes:
            try:
                if isinstance(node, QgsLayerTreeGroup):
                    count = 0
                    for child in node.findLayers():
                        try:
                            if child is not None and child.layer() is not None:
                                count += 1
                        except (AttributeError, RuntimeError, TypeError):
                            continue
                    items.append({
                        "kind": "group",
                        "name": str(node.name()),
                        "layer_count": count,
                    })
                elif isinstance(node, QgsLayerTreeLayer):
                    layer = node.layer()
                    if layer is None:
                        continue
                    items.append({"kind": "layer", "name": str(layer.name())})
            except (AttributeError, RuntimeError, TypeError):
                continue
        return items

    def _selected_layer_snapshots(self):
        view = self._layer_tree_view
        if view is None:
            return []
        try:
            layers = list(view.selectedLayersRecursive())
        except (AttributeError, RuntimeError, TypeError):
            try:
                layers = list(view.selectedLayers())
            except (AttributeError, RuntimeError, TypeError):
                return []

        snapshots = []
        seen = set()
        for layer in layers:
            item = snapshot_layer(layer)
            if item is None or item["id"] in seen:
                continue
            seen.add(item["id"])
            snapshots.append(item)
        return snapshots

    def _capture_layer_selection(self):
        """Freeze one send-time snapshot shared by tags and grounding."""
        chips = self._selection_chip_items()
        tags = []
        for item in chips:
            if item.get("kind") == "group":
                tags.append(
                    f"{item['name']} ({int(item.get('layer_count') or 0)} layers)")
            else:
                tags.append(item["name"])
        return {
            "chips": tuple(dict(item) for item in chips),
            "tags": tuple(tags),
            "layers": tuple(dict(item) for item in self._selected_layer_snapshots()),
        }

    def _update_layer_context_strip(self):
        self.context_strip.set_items(self._selection_chip_items())

    # -- activity helpers ---------------------------------------------------
    def _set_activity(self, text):
        self.status.setText(text)

    def _show_thinking(self):
        self.dots.start()
        self.status.setText("Thinking…")

    def _hide_thinking(self):
        self.dots.halt()

    # ======================================================================
    # Persistent project history
    # ======================================================================
    def _init_history(self):
        """Open the current project's profile-local history without failing UI."""
        try:
            self.history_store = HistoryStore(
                QgsApplication.qgisSettingsDirPath(),
                QgsProject.instance().fileName(),
            )
            self._history_state = self.history_store.load()
            warning = self._history_state.get("warning") or ""
            if warning:
                self._log_history_warning(warning)
        except Exception as exc:
            self.history_store = None
            self._history_state = {"session": {}, "records": []}
            self._log_history_warning(
                f"History startup failed: {type(exc).__name__}: {exc}")

    def _history_append(self, kind, **fields):
        if self._history_replaying or self.history_store is None:
            return None
        try:
            return self.history_store.append(kind, **fields)
        except Exception as exc:
            self._log_history_warning(
                f"History append failed: {type(exc).__name__}: {exc}")
            return None

    def _history_update_session(self):
        if self.history_store is None or self.backend is None:
            return
        try:
            self.history_store.update_session(
                self._backend_kind or config.get(config.K_BACKEND),
                self._current_model(),
                self.backend.session_id,
            )
        except Exception as exc:
            self._log_history_warning(
                f"History session update failed: {type(exc).__name__}: {exc}")

    def _current_model(self):
        backend = self._backend_kind or config.get(config.K_BACKEND)
        return config.get_model_choice(backend, "supervisor")["model_id"]

    @staticmethod
    def _display_timestamp(value):
        text = str(value or "")
        return text[11:16] if len(text) >= 16 and text[10:11] == "T" else text

    @staticmethod
    def _record_text(record, key, default=""):
        value = record.get(key, default)
        if isinstance(value, dict) and value.get("truncated"):
            return str(value.get("preview") or default)
        return str(value if value is not None else default)

    def _restore_history(self, state):
        """Replay capped records into final/static widgets without re-appending."""
        state = state or {}
        records = list(state.get("records") or [])
        session = dict(state.get("session") or {})
        if self.backend is not None:
            session_id = session.get("session_id")
            if session_id and session.get("backend") == self._backend_kind:
                self.backend.session_id = session_id
                self.session_label.setText(
                    f"● {self._backend_kind} · {str(session_id)[:8]}")
            else:
                self.backend.session_id = None
            self._history_update_session()
        if not records:
            return False

        tool_widgets = {}
        subagent_widgets = {}
        approval_widgets = {}
        self._history_replaying = True
        try:
            for index, record in enumerate(records):
                kind = record.get("kind")
                stamp = self._display_timestamp(record.get("t"))
                if kind == "user":
                    self._add_user_message(
                        self._record_text(record, "text"),
                        record.get("tags") or [], persist=False, timestamp=stamp)
                elif kind == "assistant":
                    text = self._record_text(record, "text")
                    if record.get("style") == "status":
                        self._add_status_note(text, persist=False)
                    else:
                        bubble = MessageBubble("assistant", self.t)
                        bubble.set_timestamp(stamp)
                        bubble.set_text(text)
                        self._add_widget(bubble, animate=False)
                elif kind == "error":
                    self._add_error(
                        self._record_text(record, "text"), persist=False,
                        timestamp=stamp)
                elif kind == "tool":
                    tool_id = str(record.get("tool_id") or f"legacy-{index}")
                    event = record.get("event") or "finished"
                    chip = tool_widgets.get(tool_id)
                    if chip is None:
                        chip = ToolChip(
                            self._record_text(record, "name", "tool"),
                            record.get("args") or {}, self.t)
                        tool_widgets[tool_id] = chip
                        self._add_widget(chip, animate=False)
                    if event == "finished":
                        chip.set_static_result(
                            self._record_text(
                                record, "result", "(no result captured)"))
                elif kind == "subagent":
                    sub_id = str(record.get("subagent_id") or f"legacy-{index}")
                    chip = subagent_widgets.get(sub_id)
                    if chip is None:
                        chip = SubagentChip(
                            self._record_text(record, "name", "subagent"), self.t)
                        subagent_widgets[sub_id] = chip
                        self._add_widget(chip, animate=False)
                    if record.get("event") == "finished":
                        chip.set_static("finished", record.get("elapsed_s"))
                elif kind == "approval":
                    approval_id = str(
                        record.get("approval_id") or f"legacy-{index}")
                    card = approval_widgets.get(approval_id)
                    if card is None:
                        card = ApprovalCard(
                            self._record_text(record, "code"),
                            record.get("reasons") or [], self.t)
                        card.set_static_outcome(None)
                        approval_widgets[approval_id] = card
                        self._add_widget(card, animate=False)
                    if record.get("event") == "decided":
                        card.set_static_outcome(bool(record.get("approved")))
                if index and index % 25 == 0:
                    QCoreApplication.processEvents()

            for chip in tool_widgets.values():
                if not chip.is_static_state():
                    chip.set_static_result("(turn interrupted before result)")
            for chip in subagent_widgets.values():
                if not chip.is_static_state():
                    chip.set_static("interrupted")
        finally:
            self._history_replaying = False
        QTimer.singleShot(0, lambda: smooth_scroll_to_bottom(self.scroll))
        return True

    def _log_history_warning(self, message):
        try:
            QgsMessageLog.logMessage(str(message), "QGent")
        except Exception:
            pass

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

    def _write_mcp_config(self, include_codex=None):
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
        if (include_codex is True
                or (include_codex is None
                    and config.get(config.K_BACKEND) == "codex")):
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

    def _regenerate_codex_config_for_doctor(self):
        """Repair only Codex state; the protected Claude MCP file is untouched."""
        py = config.python_executable()
        bridge_script = os.path.join(
            self.plugin_dir, "bridge", "mcp_stdio_bridge.py")
        self._write_codex_toml(py, bridge_script)

    def _repair_history_for_doctor(self):
        if self.history_store is None:
            return
        self._history_state = self.history_store.load()

    def _clear_session_for_doctor(self):
        if self.backend is not None:
            self.backend.session_id = None
        self._history_update_session()
        self.session_label.setText("new session")

    def _doctor_context(self):
        from ..doctor import DEFAULT_SOURCE_REPO

        return {
            "plugin_dir": self.plugin_dir,
            "profile_dir": QgsApplication.qgisSettingsDirPath(),
            "mcp_config_path": self._mcp_config_path(),
            "codex_config_path": os.path.join(
                os.path.expanduser("~"), ".codex", "config.toml"),
            "python_executable": config.python_executable,
            "cli_paths": lambda: {
                "claude": config.detect_claude(),
                "codex": config.detect_codex(),
            },
            "qgis_executable_path": QCoreApplication.applicationFilePath,
            "bridge_env": self._bridge_env,
            "log_entries": lambda: list(self.diagnostic_logs),
            "last_cli_stderr": lambda: getattr(
                self.backend, "last_stderr", "") if self.backend else "",
            "session_id": lambda: getattr(
                self.backend, "session_id", None) if self.backend else None,
            "history_path": lambda: str(self.history_store.path)
            if self.history_store is not None else "",
            "project_filename": lambda: QgsProject.instance().fileName(),
            "source_repo": str(DEFAULT_SOURCE_REPO),
            "regenerate_codex_config": self._regenerate_codex_config_for_doctor,
            "repair_history": self._repair_history_for_doctor,
            "clear_session": self._clear_session_for_doctor,
            "chat_busy": lambda: bool(
                self.backend is not None and self.backend.is_busy()),
        }

    def _build_backend(self, preserve_session=True):
        previous_kind = self._backend_kind
        previous_session = None
        if self.backend is not None:
            previous_session = self.backend.session_id
            try:
                self.backend.blockSignals(True)
            except (AttributeError, RuntimeError):
                pass
            try:
                self.backend.cancel()
            except Exception:
                pass
            self.backend.deleteLater()
            self.backend = None

        backend_kind = config.get(config.K_BACKEND)
        self._backend_kind = backend_kind
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
        self.backend.status_note.connect(self._on_backend_status_note)
        self.backend.busy_changed.connect(self._on_busy_changed)
        if (preserve_session and previous_session
                and previous_kind == backend_kind):
            self.backend.session_id = previous_session

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

        selection = self._capture_layer_selection()
        marker = text.split(maxsplit=1)[0].upper()
        task_id = marker[1:-1] if marker in (
            "[T1]", "[T2]", "[T3]", "[T4]", "[T5]", "[T6]", "[SMOKE]") else "adhoc"
        backend = config.get(config.K_BACKEND)
        self._perf = {"start": time.monotonic(), "task_id": task_id,
                      "ttft_ms": None, "tool_calls": 0, "delegations": 0,
                      "backend": backend,
                      "model": config.get(config.K_MODEL_SUPERVISOR) if backend == "claude" else ""}
        self._history_update_session()
        self.input.clear()
        self._add_user_message(text, selection["tags"])
        self._current_bubble = None
        self._last_tool_chip = None
        self._last_tool_id = None
        self._last_tool_name = ""
        self._last_tool_args = {}
        self._last_tool_finished = True
        self._subagent_chips = {}
        self._subagent_history = {}
        self._show_thinking()

        try:
            context_block = build_context_block(
                self.iface, selected_layers=selection["layers"])
        except Exception:
            context_block = ""
        self._active_turn = {
            "text": text,
            "context_block": context_block,
            "resumed": bool(self.backend.session_id),
            "fallback_attempted": False,
        }
        self.backend.send(text, context_block)

    def on_stop(self):
        if self.backend is not None:
            self.backend.cancel()
            self._end_stream()
            self._hide_thinking()
            self._set_activity("Stopped.")
            self._add_status_note("Turn stopped.")
            self._active_turn = None
            self._finish_perf()

    def new_session(self):
        if self.history_store is not None:
            try:
                self.history_store.delete()
            except Exception as exc:
                self._log_history_warning(
                    f"Could not clear history: {type(exc).__name__}: {exc}")
                self._set_activity("Could not clear chat history.")
                return
        if self.backend is not None and self.backend.is_busy():
            try:
                self.backend.blockSignals(True)
            except (AttributeError, RuntimeError):
                pass
            self.backend.cancel()
            self._finish_perf()
        # Fresh session id; keep the same bridge (port/token still valid).
        self._active_turn = None
        self._build_backend(preserve_session=False)
        self._history_update_session()
        self._clear_messages()
        self.session_label.setText("new session")
        self._post_welcome()

    def open_settings(self):
        dlg = SettingsDialog(self, doctor_context=self._doctor_context())
        busy_signal = self.backend.busy_changed if self.backend is not None else None
        if busy_signal is not None:
            busy_signal.connect(dlg.set_chat_busy)
        dlg.set_chat_busy(bool(self.backend is not None and self.backend.is_busy()))
        try:
            accepted = dlg.exec_()
        finally:
            if busy_signal is not None:
                try:
                    busy_signal.disconnect(dlg.set_chat_busy)
                except (RuntimeError, TypeError):
                    pass
            dlg.shutdown()
        if accepted:
            # Backend or model choice may have changed — rebuild + rewrite config.
            self._write_mcp_config()
            self._build_backend()
            self._history_update_session()
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

    def _end_stream(self, persist=True):
        """Flush pending text and finish the streaming bubble."""
        self._flush_timer.stop()
        self._flush_tokens()
        bubble = self._current_bubble
        if bubble is not None:
            bubble.set_streaming(False)
            if persist and bubble.text():
                self._history_append("assistant", text=bubble.text())
        self._current_bubble = None

    # ======================================================================
    # Backend signal handlers
    # ======================================================================
    def _on_tool_call(self, name, args):
        if self._perf is not None:
            self._perf["tool_calls"] += 1
        self._end_stream()
        self._hide_thinking()
        if self._last_tool_chip is not None and not self._last_tool_finished:
            self._last_tool_chip.mark_done_without_result()
            self._history_append(
                "tool", event="finished", tool_id=self._last_tool_id,
                name=self._last_tool_name, args=self._last_tool_args,
                result="(no result captured)")
            self._last_tool_finished = True
        chip = ToolChip(name, args, self.t)
        self._last_tool_chip = chip
        self._last_tool_id = secrets.token_hex(8)
        self._last_tool_name = str(name)
        self._last_tool_args = bounded_json_value(args)
        self._last_tool_finished = False
        self._add_widget(chip)
        self._history_append(
            "tool", event="started", tool_id=self._last_tool_id,
            name=self._last_tool_name, args=self._last_tool_args)
        self._set_activity(f"Running {name.replace('mcp__qgis__', '')}…")

    def _on_tool_result(self, text):
        if self._last_tool_chip is not None:
            self._last_tool_chip.set_result(text)
            self._history_append(
                "tool", event="finished", tool_id=self._last_tool_id,
                name=self._last_tool_name, args=self._last_tool_args,
                result=clipped_text(text))
            self._last_tool_finished = True

    def _on_subagent_event(self, name, status):
        self._end_stream()
        if status == "started":
            if self._perf is not None:
                self._perf["delegations"] += 1
            self._hide_thinking()
            chip = SubagentChip(name, self.t)
            self._subagent_chips[name] = chip
            subagent_id = secrets.token_hex(8)
            self._subagent_history[name] = {
                "id": subagent_id,
                "started": time.monotonic(),
                "finished": False,
            }
            self._add_widget(chip)
            self._history_append(
                "subagent", event="started", subagent_id=subagent_id,
                name=str(name))
            self._set_activity(f"{name} working…")
        elif status == "finished":
            chip = self._subagent_chips.get(name)
            if chip is not None:
                chip.set_finished()
            info = self._subagent_history.get(name) or {}
            if not info.get("finished"):
                elapsed = max(0.0, time.monotonic() - info.get(
                    "started", time.monotonic()))
                self._history_append(
                    "subagent", event="finished",
                    subagent_id=info.get("id") or secrets.token_hex(8),
                    name=str(name), elapsed_s=round(elapsed, 3))
                info["finished"] = True

    def _on_session_started(self, session_id):
        short = session_id[:8]
        backend = config.get(config.K_BACKEND)
        self.session_label.setText(f"● {backend} · {short}")
        self._history_update_session()

    def _on_done(self, _result):
        self._end_stream()
        self._hide_thinking()
        if self._last_tool_chip is not None:
            self._last_tool_chip.mark_done_without_result()
            if not self._last_tool_finished:
                self._history_append(
                    "tool", event="finished", tool_id=self._last_tool_id,
                    name=self._last_tool_name, args=self._last_tool_args,
                    result="(no result captured)")
                self._last_tool_finished = True
        for name, chip in self._subagent_chips.items():
            if chip._shimmer_pos is not None:  # never got its "finished" event
                chip.set_finished()
                info = self._subagent_history.get(name) or {}
                if not info.get("finished"):
                    elapsed = max(0.0, time.monotonic() - info.get(
                        "started", time.monotonic()))
                    self._history_append(
                        "subagent", event="finished",
                        subagent_id=info.get("id") or secrets.token_hex(8),
                        name=str(name), elapsed_s=round(elapsed, 3))
                    info["finished"] = True
        self._set_activity("Ready.")
        self._active_turn = None
        self._finish_perf()

    def _on_error(self, message):
        self._end_stream()
        self._hide_thinking()
        if self._should_retry_missing_session(message):
            self._active_turn["fallback_attempted"] = True
            self._active_turn["resumed"] = False
            self.backend.session_id = None
            self._history_update_session()
            self.session_label.setText("new session")
            self._add_status_note("Started a fresh agent session.")
            self._set_activity("Starting a fresh agent session…")
            QTimer.singleShot(0, self._retry_after_missing_session)
            return
        self._add_error(message)
        self._set_activity("Error.")
        self._active_turn = None
        self._finish_perf()

    def _should_retry_missing_session(self, message):
        turn = self._active_turn
        if (not turn or not turn.get("resumed")
                or turn.get("fallback_attempted")):
            return False
        text = str(message or "").lower()
        patterns = (
            "session not found", "session id not found", "no session found",
            "no conversation found", "conversation not found",
            "thread not found", "unknown session", "invalid session id",
            "failed to resume", "could not resume", "session does not exist",
            "thread does not exist",
        )
        if any(pattern in text for pattern in patterns):
            return True
        subject = any(word in text for word in (
            "session", "conversation", "thread"))
        missing = any(phrase in text for phrase in (
            "not found", "does not exist", "unknown", "invalid"))
        return subject and missing

    def _retry_after_missing_session(self):
        turn = self._active_turn
        if turn is None or self.backend is None or self.backend.is_busy():
            return
        self.backend.send(turn["text"], turn["context_block"])

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

    def _on_backend_status_note(self, message):
        self._add_status_note(str(message))
        # A model-reset guard updates the per-backend key before emitting.
        self._history_update_session()

    def _on_bridge_activity(self, tool):
        # Fires from the socket thread (queued); low-noise status hint.
        self._set_activity(f"QGIS: {tool.replace('mcp__qgis__', '')}")

    # ======================================================================
    # Approval gate
    # ======================================================================
    def on_approval_requested(self, ap):
        self._end_stream()
        approval_id = secrets.token_hex(8)
        code = clipped_text(ap.get("code", ""))
        reasons = [clipped_text(reason, 1000)
                   for reason in (ap.get("reasons") or [])]
        card = ApprovalCard(code, reasons, self.t)
        self._approval_history[approval_id] = card
        self._history_append(
            "approval", event="requested", approval_id=approval_id,
            code=code, reasons=reasons)

        def decide(approved):
            ap["approved"] = approved
            ap["event"].set()
            self._history_append(
                "approval", event="decided", approval_id=approval_id,
                code=code, reasons=reasons, approved=bool(approved))

        card.decided.connect(decide)
        self._add_widget(card)
        card.attention()

    # ======================================================================
    # Message list helpers
    # ======================================================================
    def _add_user_message(self, text, tags=None, persist=True, timestamp=None):
        bubble = MessageBubble("user", self.t)
        bubble.set_tags(tags)
        self._add_widget(bubble)
        if timestamp is not None:
            bubble.set_timestamp(timestamp)
        bubble.set_text(text)
        if persist:
            self._history_append("user", text=str(text), tags=list(tags or []))

    def _add_error(self, text, persist=True, timestamp=None):
        bubble = MessageBubble("assistant", self.t)
        bubble.set_error()
        self._add_widget(bubble)
        if timestamp is not None:
            bubble.set_timestamp(timestamp)
        bubble.set_text("**⚠ " + text + "**")
        if persist:
            self._history_append("error", text=str(text))

    def _add_status_note(self, text, persist=True):
        note = StatusNote(text, self.t)
        self._add_widget(note)
        if persist:
            self._history_append("assistant", style="status", text=str(text))

    def _add_widget(self, widget, animate=True):
        if self.hero is not None and self.hero.isVisible():
            self.hero.hide()
        # Insert before the trailing stretch.
        self.msg_layout.insertWidget(self.msg_layout.count() - 1, widget)
        if animate and not self._history_replaying:
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
        self._last_tool_id = None
        self._last_tool_name = ""
        self._last_tool_args = {}
        self._last_tool_finished = True
        self._subagent_chips.clear()
        self._subagent_history.clear()
        self._approval_history.clear()
        self._stream_buf = ""

    # ======================================================================
    # Empty state
    # ======================================================================
    def _post_welcome(self):
        self._show_hero()
        if not config.active_cli_path():
            self._set_activity(
                "No agent CLI found — open ⚙ Settings to configure one.")

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
        if self._layer_selection_model is not None:
            try:
                self._layer_selection_model.selectionChanged.disconnect(
                    self._on_layer_selection_changed)
            except (RuntimeError, TypeError):
                pass
            self._layer_selection_model = None
            self._layer_tree_view = None
        if self.backend is not None:
            try:
                self.backend.blockSignals(True)
            except (AttributeError, RuntimeError):
                pass
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
