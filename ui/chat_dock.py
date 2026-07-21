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
import hashlib
import json
import os
import re
import secrets
import shutil
import time

from qgis.PyQt.QtCore import (
    Qt, QTimer, QCoreApplication, QSettings, QStandardPaths,
)
from qgis.PyQt.QtGui import QIcon, QImage
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel,
    QToolButton, QFrame, QMenu, QFileDialog, QMessageBox, QApplication,
    QPushButton, QSystemTrayIcon,
)
from qgis.core import (
    Qgis, QgsApplication, QgsLayerTreeGroup, QgsLayerTreeLayer, QgsMessageLog,
    QgsProject,
)

from .. import config
from . import theme
from .animations import fade_in, smooth_scroll_to_bottom, staggered, ThinkingDots
from .widgets import (
    MessageBubble, ToolChip, SubagentChip, ApprovalCard, ChatInput,
    SendStopButton, SuggestionChip, ContextStrip, StatusNote, QueuePanel,
    SnapshotCard,
)
from .settings_dialog import SettingsDialog
from ..bridge.qgis_socket_server import BridgeServer
from ..bridge.main_thread_executor import MainThreadExecutor
from ..context.project_snapshot import (
    build_attached_files_section, build_context_block, snapshot_layer,
)
from ..history import (
    HistoryStore, bounded_json_value, clipped_text, create_batch_backup,
)
from ..export import (
    default_export_name, plugin_version, read_history_jsonl, render_markdown,
    write_markdown, write_pdf,
)
from ..turn_report import (
    batch_token_totals, build_turn_report, render_batch_summary,
)

_SUGGESTIONS = [
    "What CRS is this project?",
    "Buffer the active layer by 100 m and add the result",
    "Make an A3 vicinity map of the current canvas extent",
]

_ATTACHMENT_KINDS = {
    ".shp": "ESRI Shapefile",
    ".gpkg": "GeoPackage",
    ".geojson": "GeoJSON",
    ".json": "JSON",
    ".kml": "KML",
    ".kmz": "KMZ archive",
    ".tif": "GeoTIFF raster",
    ".tiff": "GeoTIFF raster",
    ".csv": "CSV table",
    ".qml": "QGIS layer style",
    ".qpt": "QGIS print layout template",
}

_VISUAL_CHANGE_TOOLS = {"execute_pyqgis", "run_processing"}
_SNAPSHOT_TOOL = "render_map_snapshot"
_SNAPSHOT_MAX_EDGE = 640


def _canonical_qgis_tool_name(name):
    """Return a known QGIS tool's terminal name across backend spellings."""
    text = str(name or "").strip()
    for known in (*sorted(_VISUAL_CHANGE_TOOLS), _SNAPSHOT_TOOL):
        if text == known or any(text.endswith(separator + known) for separator in (
                "__", ".", "/", ":")):
            return known
    return ""


def _snapshot_path_from_result(value):
    """Extract the bridge's local PNG path from a textual tool result."""
    if isinstance(value, dict):
        candidate = value.get("snapshot_path")
        return str(candidate or "").strip() if isinstance(candidate, str) else ""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = None
    if isinstance(decoded, dict):
        candidate = decoded.get("snapshot_path")
        if isinstance(candidate, str):
            return candidate.strip()
    match = re.search(
        r'"snapshot_path"\s*:\s*("(?:\\.|[^"\\])*")', text)
    if match:
        try:
            candidate = json.loads(match.group(1))
        except (TypeError, ValueError, json.JSONDecodeError):
            candidate = ""
        return str(candidate or "").strip()
    return ""


def _snapshot_file_fingerprint(path):
    """Identify one rendered temp-file instance without trusting tool IDs."""
    full_path = os.path.abspath(os.path.realpath(os.path.normpath(str(path))))
    stat_result = os.stat(full_path)
    digest = hashlib.sha256()
    with open(full_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return (
        os.path.normcase(full_path), int(stat_result.st_size),
        int(stat_result.st_mtime_ns), digest.hexdigest().upper(),
    )


def _attachment_descriptor(path):
    """Return immutable metadata for one dropped local path, never opening it."""
    raw = str(path or "").strip()
    if not raw:
        return None, "Dropped item has no local file path."
    full_path = os.path.abspath(os.path.normpath(raw))
    name = os.path.basename(full_path) or full_path
    extension = os.path.splitext(full_path)[1].casefold()
    if extension not in _ATTACHMENT_KINDS:
        return None, f"Unsupported file: {name}"
    if not os.path.isfile(full_path):
        return None, f"File is unavailable: {name}"
    try:
        size = int(os.path.getsize(full_path))
    except OSError as exc:
        return None, f"Could not attach {name}: {exc}"

    warnings = []
    if extension == ".shp":
        stem = os.path.splitext(full_path)[0]
        required = [stem + suffix for suffix in (".shx", ".dbf")]
        missing_required = [os.path.basename(item) for item in required
                            if not os.path.isfile(item)]
        if missing_required:
            warnings.append(
                "Missing required Shapefile sidecar(s): "
                + ", ".join(missing_required) + ".")
        projection = stem + ".prj"
        if not os.path.isfile(projection):
            warnings.append(
                "Missing optional projection sidecar: "
                + os.path.basename(projection) + ".")

    return {
        "kind": "attachment",
        "path": full_path,
        "key": os.path.normcase(full_path),
        "name": name,
        "size": size,
        "extension": extension,
        "file_kind": _ATTACHMENT_KINDS[extension],
        "warnings": warnings,
        "warning": " ".join(warnings),
    }, ""


class ChatDock(QDockWidget):
    def __init__(self, iface, plugin_dir, diagnostic_logs=None):
        super().__init__("QGent", iface.mainWindow())
        self.setAcceptDrops(True)
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
        self._live_approvals = {}
        self._queue_tasks = []
        self._queue_running = False
        self._queue_pause_after_current = False
        self._queue_stop_requested = False
        self._queue_started_at = None
        self._queue_policy = None
        self._queue_backup = None
        self._queue_warning = ""
        self._queue_auto_approvals = []
        self._batch_task_ids = []
        self._ignore_next_terminal = False
        self._queue_stop_reason = ""
        self._queue_stop_error = ""
        self._tray_icon = None
        self._notification_events = []
        self._attached_files = []

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
        self.export_btn = QToolButton()
        self.export_btn.setObjectName("QgentIconBtn")
        self.export_btn.setText("⇩")
        self.export_btn.setToolTip("Export conversation")
        self.export_btn.setCursor(Qt.PointingHandCursor)
        self.export_btn.setPopupMode(QToolButton.InstantPopup)
        export_menu = QMenu(self.export_btn)
        export_menu.addAction(
            "Export as Markdown…", lambda: self._export_chat("md"))
        export_menu.addAction(
            "Export as PDF…", lambda: self._export_chat("pdf"))
        self.export_btn.setMenu(export_menu)
        self.export_btn.setEnabled(False)
        top.addWidget(self.export_btn)
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

        # -- sequential task queue ----------------------------------------
        self.queue_panel = QueuePanel(self.t)
        self.queue_panel.run_requested.connect(self.run_queue)
        self.queue_panel.pause_requested.connect(self._set_queue_pause)
        self.queue_panel.stop_all_requested.connect(self.stop_all_queue)
        self.queue_panel.move_requested.connect(self._move_queue_task)
        self.queue_panel.remove_requested.connect(self._remove_queue_task)
        self.queue_panel.stop_requested.connect(self._stop_queue_task)
        outer.addWidget(self.queue_panel)

        # -- selected-layer context ---------------------------------------
        self.context_strip = ContextStrip()
        self.context_strip.remove_requested.connect(
            self._remove_attached_file)
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
        self.queue_add_btn = QPushButton("+ Queue")
        self.queue_add_btn.setObjectName("QgentQueueAdd")
        self.queue_add_btn.setCursor(Qt.PointingHandCursor)
        self.queue_add_btn.setToolTip("Add this request without starting it")
        self.queue_add_btn.clicked.connect(self.add_composer_to_queue)
        crow.addWidget(self.queue_add_btn, 0, Qt.AlignBottom)
        self.stop_turn_btn = QPushButton("Stop")
        self.stop_turn_btn.setObjectName("QgentStopCurrent")
        self.stop_turn_btn.setCursor(Qt.PointingHandCursor)
        self.stop_turn_btn.clicked.connect(self.on_stop)
        self.stop_turn_btn.hide()
        crow.addWidget(self.stop_turn_btn, 0, Qt.AlignBottom)
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

    # -- file drops --------------------------------------------------------
    def dragEnterEvent(self, event):
        try:
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
                return
        except (AttributeError, RuntimeError, TypeError):
            pass
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        try:
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
                return
        except (AttributeError, RuntimeError, TypeError):
            pass
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        try:
            mime = event.mimeData()
            urls = list(mime.urls()) if mime.hasUrls() else []
        except (AttributeError, RuntimeError, TypeError):
            urls = []
        if not urls:
            super().dropEvent(event)
            return

        paths = []
        for url in urls:
            try:
                path = str(url.toLocalFile() or "") if url.isLocalFile() else ""
                if path:
                    paths.append(path)
                else:
                    label = str(url.fileName() or url.toString() or "item")
                    self._push_drop_warning(
                        f"Unsupported non-local file: {label}")
            except (AttributeError, RuntimeError, TypeError):
                self._push_drop_warning("Unsupported dropped item.")
        self._attach_dropped_paths(paths)
        event.acceptProposedAction()

    def _push_drop_warning(self, message):
        message = str(message)
        try:
            self.iface.messageBar().pushMessage(
                "QGent", message, level=Qgis.Warning, duration=5)
        except Exception:
            # Drop rejection is advisory and must never escape the Qt event.
            pass
        self._set_activity(message)

    def _attach_dropped_paths(self, paths):
        accepted = []
        rejected = []
        existing = {str(item.get("key") or "") for item in self._attached_files}
        for path in paths or ():
            descriptor, error = _attachment_descriptor(path)
            if descriptor is None:
                rejected.append({"path": str(path or ""), "error": error})
                self._push_drop_warning(error)
                continue
            if descriptor["key"] in existing:
                self._set_activity(
                    f"Already attached: {descriptor['name']}")
                continue
            existing.add(descriptor["key"])
            self._attached_files.append(descriptor)
            accepted.append(dict(descriptor))
        if accepted:
            self._update_layer_context_strip()
            suffix = "file" if len(accepted) == 1 else "files"
            self._set_activity(
                f"Attached {len(accepted)} {suffix} to the next request.")
        return accepted, rejected

    def _attachment_snapshot(self):
        return tuple(dict(item) for item in self._attached_files)

    @staticmethod
    def _attachment_tags(attachments):
        return tuple(
            f"📄 {str(item.get('name') or os.path.basename(str(item.get('path') or '')))}"
            for item in (attachments or ())
            if str(item.get("name") or item.get("path") or "").strip()
        )

    def _consume_attached_files(self, attachments):
        keys = {str(item.get("key") or os.path.normcase(
            str(item.get("path") or ""))) for item in (attachments or ())}
        if not keys:
            return
        self._attached_files = [
            item for item in self._attached_files
            if str(item.get("key") or "") not in keys
        ]
        self._update_layer_context_strip()

    def _remove_attached_file(self, path):
        key = os.path.normcase(os.path.abspath(os.path.normpath(str(path or ""))))
        before = len(self._attached_files)
        self._attached_files = [
            item for item in self._attached_files
            if str(item.get("key") or "") != key
        ]
        if len(self._attached_files) != before:
            self._update_layer_context_strip()
            self._set_activity("Removed file from the next request.")

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
        items = list(self._selection_chip_items())
        items.extend(self._attachment_snapshot())
        self.context_strip.set_items(items)

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
            self._update_export_enabled(
                bool(self._history_state.get("records")))
        except Exception as exc:
            self.history_store = None
            self._history_state = {"session": {}, "records": []}
            self._log_history_warning(
                f"History startup failed: {type(exc).__name__}: {exc}")
            self._update_export_enabled(False)

    def _history_append(self, kind, **fields):
        if self._history_replaying or self.history_store is None:
            return None
        try:
            record = self.history_store.append(kind, **fields)
            self._update_export_enabled(True)
            return record
        except Exception as exc:
            self._log_history_warning(
                f"History append failed: {type(exc).__name__}: {exc}")
            return None

    def _update_export_enabled(self, has_records=None):
        button = getattr(self, "export_btn", None)
        if button is None:
            return
        if has_records is None:
            has_records = bool(
                self.history_store is not None
                and self.history_store.path.is_file()
                and self.history_store.path.stat().st_size > 0
            )
        button.setEnabled(bool(has_records))

    def _export_chat(self, extension):
        """Export the persisted current conversation; never scrape widgets."""
        if self.history_store is None:
            return
        try:
            history = read_history_jsonl(self.history_store.path)
        except Exception as exc:
            self._export_failed(exc)
            return
        records = list(history.get("records") or [])
        if not records:
            self._update_export_enabled(False)
            return

        project = QgsProject.instance()
        project_path = str(project.fileName() or "")
        project_name = str(project.title() or "").strip()
        if not project_name and project_path:
            project_name = os.path.splitext(os.path.basename(project_path))[0]
        project_name = project_name or "unsaved"
        filename = default_export_name(
            project_path, project_name, extension,
            today=datetime.date.today(),
        )
        settings = QSettings()
        last_dir = str(settings.value(
            "QgisCopilot/export_last_directory", "") or "")
        if not os.path.isdir(last_dir):
            last_dir = (
                os.path.dirname(project_path) if project_path else
                QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
            )
        default_path = os.path.join(last_dir, filename)
        file_filter = (
            "Markdown files (*.md)" if extension == "md"
            else "PDF files (*.pdf)"
        )
        target, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export QGent conversation", default_path, file_filter)
        if not target:
            return
        suffix = "." + extension
        if not target.lower().endswith(suffix):
            target += suffix

        session = dict(history.get("session") or {})
        metadata = {
            "project_name": project_name,
            "project_path": project_path or "(unsaved)",
            "backend": session.get("backend") or self._backend_kind
                       or config.get(config.K_BACKEND),
            "model": session.get("model") or self._current_model(),
            "version": plugin_version(self.plugin_dir),
        }
        in_progress = bool(
            self._active_turn is not None
            or (self.backend is not None and self.backend.is_busy()))
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            QApplication.processEvents()
            markdown = render_markdown(
                records, metadata, in_progress=in_progress)
            QApplication.processEvents()
            if extension == "md":
                write_markdown(target, markdown)
            else:
                write_pdf(target, markdown, title=(
                    "QGent Chat — " + project_name))
            settings.setValue(
                "QgisCopilot/export_last_directory",
                os.path.dirname(os.path.abspath(target)),
            )
            settings.sync()
            self._set_activity("Exported chat to {}.".format(
                os.path.basename(target)))
        except Exception as exc:
            self._export_failed(exc)
        finally:
            QApplication.restoreOverrideCursor()
            QApplication.processEvents()

    def _export_failed(self, exc):
        self._set_activity("Chat export failed.")
        QMessageBox.critical(
            self, "QGent export failed",
            "Could not export this conversation:\n{}: {}".format(
                type(exc).__name__, exc),
        )

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

    @staticmethod
    def _snapshots_enabled():
        try:
            return bool(config.get(config.K_SHOW_MAP_SNAPSHOTS))
        except Exception:
            return True

    def _snapshot_directory(self, create=False):
        if self.history_store is None:
            return ""
        path = os.path.join(str(self.history_store.history_dir), "snaps")
        if create:
            os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def _snapshot_caption():
        return "Map after this step — " + datetime.datetime.now().strftime("%H:%M")

    def _add_snapshot_card(self, path, caption, persist=True, animate=True):
        card = SnapshotCard(path, caption, self.t)
        self._add_widget(card, animate=animate)
        card.show()
        if persist:
            self._history_append(
                "snapshot", path=os.path.abspath(str(path)),
                caption=str(caption))
        return card

    def _record_explicit_snapshot(self, result):
        """Copy one first-seen render_map_snapshot result into history."""
        turn = self._active_turn
        if turn is None or not self._snapshots_enabled():
            return None
        source = _snapshot_path_from_result(result)
        if not source:
            return None
        temporary = ""
        try:
            fingerprint = _snapshot_file_fingerprint(source)
            seen = turn.setdefault("explicit_snapshot_fingerprints", set())
            if fingerprint in seen:
                return None
            source_path = fingerprint[0]
            image = QImage(source_path)
            if image.isNull():
                raise ValueError("render_map_snapshot returned an unreadable PNG")
            directory = self._snapshot_directory(create=True)
            if not directory:
                return None
            sequence = int(turn.get("explicit_snapshot_sequence") or 0) + 1
            target = os.path.join(
                directory, f"{turn['id']}-render-{sequence}.png")
            temporary = target + ".tmp-" + secrets.token_hex(6)
            shutil.copyfile(source_path, temporary)
            copied = _snapshot_file_fingerprint(temporary)
            if copied[1] != fingerprint[1] or copied[3] != fingerprint[3]:
                raise OSError("snapshot copy verification failed")
            os.replace(temporary, target)
            temporary = ""
            seen.add(fingerprint)
            turn["explicit_snapshot_sequence"] = sequence
            return self._add_snapshot_card(
                target, self._snapshot_caption())
        except Exception as exc:
            self._log_history_warning(
                "Map snapshot copy failed: {}: {}".format(
                    type(exc).__name__, exc))
            return None
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def _capture_terminal_snapshot(self, turn):
        """Capture at most once at a true terminal boundary for one turn."""
        if turn is None or turn.get("terminal_snapshot_attempted"):
            return None
        turn["terminal_snapshot_attempted"] = True
        if (not turn.get("visual_change_tool_ran")
                or not self._snapshots_enabled()):
            return None
        temporary = ""
        try:
            directory = self._snapshot_directory(create=True)
            if not directory:
                return None
            canvas = self.iface.mapCanvas()
            image = canvas.grab().toImage()
            if image.isNull():
                raise ValueError("QGIS canvas grab returned an empty image")
            if max(image.width(), image.height()) > _SNAPSHOT_MAX_EDGE:
                image = image.scaled(
                    _SNAPSHOT_MAX_EDGE, _SNAPSHOT_MAX_EDGE,
                    Qt.KeepAspectRatio, Qt.SmoothTransformation)
            target = os.path.join(directory, f"{turn['id']}.png")
            temporary = target + ".tmp-" + secrets.token_hex(6)
            if not image.save(temporary, "PNG"):
                raise OSError("Qt could not encode the canvas as PNG")
            os.replace(temporary, target)
            temporary = ""
            return self._add_snapshot_card(
                target, self._snapshot_caption())
        except Exception as exc:
            self._log_history_warning(
                "Map snapshot capture failed: {}: {}".format(
                    type(exc).__name__, exc))
            return None
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

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
                    elif record.get("event") == "cancelled":
                        card.set_cancelled(
                            self._record_text(record, "reason", "Approval cancelled"))
                elif kind == "queue":
                    event = self._record_text(record, "event", "event")
                    task_text = " ".join(
                        self._record_text(record, "text").split())[:60]
                    note = "Queue: " + event.replace("_", " ")
                    if task_text:
                        note += " - " + task_text
                    if record.get("error"):
                        note += " - " + self._record_text(record, "error")
                    self._add_status_note(note, persist=False)
                elif kind == "queue_summary":
                    self._add_assistant_message(
                        self._record_text(record, "text"), persist=False,
                        timestamp=stamp)
                elif kind == "snapshot":
                    self._add_snapshot_card(
                        self._record_text(record, "path"),
                        self._record_text(
                            record, "caption",
                            "Map after this step — " + stamp),
                        persist=False, animate=False)
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
        self.bridge.approval_finished.connect(self._on_approval_finished)
        self.bridge.auto_approved.connect(self._on_auto_approved)
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
            self._cancel_pending_approvals("Backend replaced")
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
        self.on_send()

    def on_send(self):
        text = self.input.toPlainText().strip()
        if not text:
            return
        if (self._queue_running
                or (self.backend is not None and self.backend.is_busy())):
            self._enqueue_text(text)
            self.input.clear()
            return
        self._start_turn(text)

    def add_composer_to_queue(self):
        text = self.input.toPlainText().strip()
        if not text:
            return
        self._enqueue_text(text)
        self.input.clear()

    def _enqueue_text(self, text):
        attachments = self._attachment_snapshot()
        task = {
            "id": secrets.token_hex(8),
            "text": str(text),
            "status": "queued",
            "created_at": datetime.datetime.now().astimezone().isoformat(
                timespec="milliseconds"),
            "started_at": None,
            "elapsed_s": None,
            "error": "",
            "usage": None,
            "tokens": None,
            "verdict": "",
            "layers_created": [],
            "files_exported": [],
            "attachments": [dict(item) for item in attachments],
        }
        self._queue_tasks.append(task)
        if self._queue_running:
            self._batch_task_ids.append(task["id"])
        self._history_append(
            "queue", event="enqueued", task_id=task["id"], text=task["text"],
            attachments=bounded_json_value(list(attachments)))
        # A queued request now owns this frozen snapshot; later drops belong
        # to later requests and cannot leak into the queued task.
        self._consume_attached_files(attachments)
        self._sync_queue_panel()
        self._set_activity("Added request to the queue.")
        return task

    def _sync_queue_panel(self):
        self.queue_panel.set_tasks(self._queue_tasks)
        self.queue_panel.set_batch_running(self._queue_running)
        self.queue_panel.set_paused(self._queue_pause_after_current)
        busy = bool(self.backend is not None and self.backend.is_busy())
        self.action_btn.set_busy(self._queue_running or busy)

    def _queue_task(self, task_id):
        return next((task for task in self._queue_tasks
                     if task.get("id") == task_id), None)

    def _move_queue_task(self, task_id, direction):
        task = self._queue_task(task_id)
        if task is None or task.get("status") != "queued":
            return
        current = self._queue_tasks.index(task)
        candidates = [index for index, item in enumerate(self._queue_tasks)
                      if item.get("status") == "queued"]
        position = candidates.index(current)
        target_position = position + (-1 if int(direction) < 0 else 1)
        if target_position < 0 or target_position >= len(candidates):
            return
        target = candidates[target_position]
        self._queue_tasks[current], self._queue_tasks[target] = (
            self._queue_tasks[target], self._queue_tasks[current])
        self._history_append(
            "queue", event="reordered", task_id=task_id,
            direction=-1 if int(direction) < 0 else 1)
        self._sync_queue_panel()

    def _remove_queue_task(self, task_id):
        task = self._queue_task(task_id)
        if task is None or task.get("status") != "queued":
            return
        self._queue_tasks.remove(task)
        if task_id in self._batch_task_ids:
            self._batch_task_ids.remove(task_id)
        self._history_append(
            "queue", event="removed", task_id=task_id,
            text=task.get("text", ""))
        self._sync_queue_panel()
        if self._queue_running:
            QTimer.singleShot(0, self._queue_idle_checkpoint)

    def _set_queue_pause(self, paused):
        if not self._queue_running:
            return
        self._queue_pause_after_current = bool(paused)
        self._history_append(
            "queue", event="pause_changed", paused=self._queue_pause_after_current)
        self._sync_queue_panel()
        if paused:
            self._set_activity("Queue will pause after the current task.")
        else:
            self._set_activity("Queue resumed.")
            QTimer.singleShot(0, self._queue_idle_checkpoint)

    def run_queue(self):
        queued = [task for task in self._queue_tasks
                  if task.get("status") == "queued"]
        if not queued or self._queue_running:
            return
        if self.backend is None or self.backend.is_busy():
            self._set_activity("Wait for the current turn before running the queue.")
            return

        project = QgsProject.instance()
        project_path = str(project.fileName() or "")
        saved = bool(project_path)
        policy = self._choose_queue_policy(saved, bool(project.isDirty()))
        if policy is None:
            return
        if policy == "auto" and not saved:
            self._history_append(
                "queue", event="auto_rejected_unsaved",
                warning="Project is unsaved; auto-approve is unavailable.")
            self._set_activity(
                "Auto-approve is unavailable until the project is saved.")
            return
        try:
            backup = create_batch_backup(
                QgsApplication.qgisSettingsDirPath(), project_path,
                self._project_layer_manifest())
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self._history_append(
                "queue", event="backup_failed", error=message)
            QMessageBox.critical(
                self, "QGent queue backup failed",
                "No queued tasks were started because the pre-run backup "
                f"failed.\n\n{message}")
            self._set_activity("Queue not started: backup failed.")
            return

        if policy == "auto":
            self.bridge.set_batch_permission_mode("auto")
        else:
            normal = config.get(config.K_PERMISSION_MODE)
            pause_mode = (
                "ask_always" if normal == "ask_always" else "ask_destructive")
            self.bridge.set_batch_permission_mode(pause_mode)

        self._queue_running = True
        self._queue_stop_requested = False
        self._queue_pause_after_current = False
        self._queue_started_at = time.monotonic()
        self._queue_policy = policy
        self._queue_backup = backup
        self._queue_warning = (
            "Project is unsaved - no file backup was possible." if not saved else "")
        self._queue_auto_approvals = []
        self._batch_task_ids = [task["id"] for task in queued]
        self._queue_stop_reason = ""
        self._queue_stop_error = ""
        self._history_append(
            "queue", event="batch_started", policy=policy,
            backup_path=backup["path"], warning=self._queue_warning,
            task_ids=list(self._batch_task_ids))
        note = f"Queue started ({policy}); backup: {backup['path']}"
        if self._queue_warning:
            note += ". " + self._queue_warning
        self._add_status_note(note)
        self._sync_queue_panel()
        QTimer.singleShot(0, self._queue_idle_checkpoint)

    def _choose_queue_policy(self, project_saved, project_dirty=False):
        box = QMessageBox(self)
        box.setWindowTitle("Run QGent queue")
        box.setIcon(QMessageBox.Warning)
        text = (
            "QGent will create a project restore point, then run queued "
            "requests one at a time in this chat session.\n\n"
            "Choose how this batch handles destructive-operation approvals.")
        if not project_saved:
            text += ("\n\nProject is unsaved - no file backup is possible. "
                     "Auto-approve is disabled.")
        elif project_dirty:
            text += ("\n\nThe project has unsaved changes. The backup will contain "
                     "the last saved project file plus the current layer manifest.")
        box.setText(text)
        pause_button = box.addButton(
            "Pause on approvals", QMessageBox.AcceptRole)
        pause_button.setObjectName("QgentBatchPauseApprovals")
        auto_button = box.addButton(
            "Auto-approve destructive steps for this batch",
            QMessageBox.AcceptRole)
        auto_button.setObjectName("QgentBatchAutoApprove")
        auto_button.setEnabled(bool(project_saved))
        box.addButton(QMessageBox.Cancel)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is pause_button:
            return "pause"
        if clicked is auto_button and project_saved:
            return "auto"
        return None

    @staticmethod
    def _project_layer_manifest():
        records = []
        for layer in QgsProject.instance().mapLayers().values():
            records.append({
                "name": layer.name(),
                "id": layer.id(),
                "source": layer.source(),
                "provider": layer.providerType(),
            })
        return records

    def _start_turn(self, text, queue_task=None):
        text = str(text or "").strip()
        if not text:
            return False
        if self.backend is None:
            if queue_task is not None:
                self._stop_queue_after_error(
                    queue_task, "Backend is unavailable.")
            return False
        if self.backend.is_busy():
            return False
        if not self.backend.cli_path:
            message = (
                "No CLI found. Install the selected agent CLI and log in, "
                "then set its path in Settings.")
            self._add_error(message)
            if queue_task is not None:
                self._stop_queue_after_error(queue_task, message)
            return False

        selection = self._capture_layer_selection()
        if queue_task is None:
            attachments = self._attachment_snapshot()
        else:
            attachments = tuple(
                dict(item) for item in (queue_task.get("attachments") or []))
        message_tags = tuple(selection["tags"]) + self._attachment_tags(
            attachments)
        marker = text.split(maxsplit=1)[0].upper()
        task_id = marker[1:-1] if marker in (
            "[T1]", "[T2]", "[T3]", "[T4]", "[T5]", "[T6]", "[SMOKE]") else "adhoc"
        backend = config.get(config.K_BACKEND)
        self._perf = {"start": time.monotonic(), "task_id": task_id,
                      "ttft_ms": None, "tool_calls": 0, "delegations": 0,
                      "backend": backend,
                      "model": config.get(config.K_MODEL_SUPERVISOR) if backend == "claude" else ""}
        self._history_update_session()
        if queue_task is None:
            self.input.clear()
        self._add_user_message(
            text, message_tags, attachments=attachments)
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
                self.iface, selected_layers=selection["layers"],
                attached_files=attachments)
        except Exception:
            context_block = build_attached_files_section(attachments)
        self._active_turn = {
            "id": secrets.token_hex(12),
            "text": text,
            "context_block": context_block,
            "resumed": bool(self.backend.session_id),
            "fallback_attempted": False,
            "queue_task_id": queue_task.get("id") if queue_task else None,
            "assistant_parts": [],
            "tool_reports": [],
            "subagent_events": [],
            "attachments": [dict(item) for item in attachments],
            "visual_change_tool_ran": False,
            "terminal_snapshot_attempted": False,
            "explicit_snapshot_fingerprints": set(),
            "explicit_snapshot_sequence": 0,
        }
        if queue_task is not None:
            queue_task["status"] = "running"
            queue_task["started_at"] = time.monotonic()
            queue_task["elapsed_s"] = None
            queue_task["error"] = ""
            queue_task["usage"] = None
            queue_task["tokens"] = None
            queue_task["verdict"] = ""
            queue_task["layers_created"] = []
            queue_task["files_exported"] = []
            self._history_append(
                "queue", event="task_started", task_id=queue_task["id"],
                text=queue_task["text"],
                attachments=bounded_json_value(list(attachments)))
            self._sync_queue_panel()
        try:
            self.backend.send(text, context_block)
            if queue_task is None:
                self._consume_attached_files(attachments)
            return True
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self._active_turn = None
            self._finish_perf()
            self._add_error(message)
            if queue_task is not None:
                self._stop_queue_after_error(queue_task, message)
            return False

    def _mark_queue_task(self, task, status, error=""):
        if task is None:
            return
        task["status"] = str(status)
        task["error"] = str(error or "")
        if status in ("done", "failed", "skipped"):
            started = task.get("started_at")
            if started is not None:
                task["elapsed_s"] = round(
                    max(0.0, time.monotonic() - float(started)), 3)
            task["ended_at"] = datetime.datetime.now().astimezone().isoformat(
                timespec="milliseconds")
        self._history_append(
            "queue", event=f"task_{status}", task_id=task.get("id"),
            text=task.get("text", ""), elapsed_s=task.get("elapsed_s"),
            error=task.get("error", ""), usage=task.get("usage"),
            tokens=task.get("tokens"), verdict=task.get("verdict", ""),
            layers_created=list(task.get("layers_created") or []),
            files_exported=list(task.get("files_exported") or []))
        self._sync_queue_panel()

    @staticmethod
    def _apply_turn_report(task, turn, terminal_payload=None):
        if task is None:
            return
        task.update(build_turn_report(turn, terminal_payload))

    def _stop_queue_after_error(self, task, message):
        """Fail one terminally errored task and stop this batch at idle."""
        error = str(message or "Agent backend error.")
        self._mark_queue_task(task, "failed", error)
        if not self._queue_running:
            return
        self._queue_stop_requested = True
        self._queue_stop_reason = "error"
        self._queue_stop_error = error
        batch_ids = set(self._batch_task_ids)
        for queued in self._queue_tasks:
            if (queued.get("id") in batch_ids
                    and queued.get("status") == "queued"):
                self._mark_queue_task(
                    queued, "skipped", "Skipped after a previous task failed.")
        if self.bridge is not None:
            self.bridge.clear_batch_permission_mode()
        self._cancel_pending_approvals("Queue stopped after an agent error")
        self._history_append(
            "queue", event="batch_error_stop_requested",
            task_id=(task or {}).get("id"), error=error)
        self._sync_queue_panel()
        QTimer.singleShot(0, self._queue_idle_checkpoint)

    def _queue_idle_checkpoint(self):
        backend_busy = bool(self.backend is not None and self.backend.is_busy())
        if self._ignore_next_terminal and self._active_turn is None and not backend_busy:
            self._ignore_next_terminal = False
        if not self._queue_running:
            return
        if backend_busy or self._active_turn is not None:
            return
        if self._queue_stop_requested:
            self._finish_queue(stopped=True)
            return
        if self._queue_pause_after_current:
            self._set_activity("Queue paused after the current task.")
            return
        batch_ids = set(self._batch_task_ids)
        task = next((item for item in self._queue_tasks
                     if item.get("id") in batch_ids
                     and item.get("status") == "queued"), None)
        if task is None:
            self._finish_queue(stopped=False)
            return
        self._start_turn(task.get("text", ""), queue_task=task)

    def _finish_queue(self, stopped=False):
        if not self._queue_running:
            return
        if self.backend is not None and self.backend.is_busy():
            return
        if self.bridge is not None:
            self.bridge.clear_batch_permission_mode()
        wall_time = max(0.0, time.monotonic() - (
            self._queue_started_at or time.monotonic()))
        batch_ids = set(self._batch_task_ids)
        tasks = [task for task in self._queue_tasks
                 if task.get("id") in batch_ids]
        rows = [{
            "task_id": task.get("id"),
            "text": task.get("text", ""),
            "status": task.get("status", "queued"),
            "elapsed_s": task.get("elapsed_s"),
            "error": task.get("error", ""),
            "usage": task.get("usage"),
            "tokens": task.get("tokens"),
            "verdict": task.get("verdict", ""),
            "layers_created": list(task.get("layers_created") or []),
            "files_exported": list(task.get("files_exported") or []),
        } for task in tasks]
        passed = sum(1 for task in tasks if task.get("status") == "done")
        failures = sum(1 for task in tasks if task.get("status") == "failed")
        skipped = sum(1 for task in tasks if task.get("status") == "skipped")
        total_tokens, unavailable_tokens = batch_token_totals(tasks)
        policy = self._queue_policy or "pause"
        backup_path = (self._queue_backup or {}).get("path", "")
        stop_reason = self._queue_stop_reason
        stop_error = self._queue_stop_error
        summary = self._queue_summary_text(
            rows, policy, backup_path, self._queue_warning, wall_time,
            failures, skipped, self._queue_auto_approvals, stopped,
            stop_reason=stop_reason)
        self._history_append(
            "queue_summary", text=summary, policy=policy,
            backup_path=backup_path, warning=self._queue_warning,
            tasks=rows, failure_count=failures, skip_count=skipped,
            pass_count=passed, total_tokens=total_tokens,
            token_usage_unavailable_count=unavailable_tokens,
            wall_time_s=round(wall_time, 3), stopped=bool(stopped),
            stop_reason=stop_reason, stop_error=stop_error,
            auto_approvals=list(self._queue_auto_approvals))
        self._add_assistant_message(summary, persist=False)

        self._queue_running = False
        self._queue_pause_after_current = False
        self._queue_stop_requested = False
        self._queue_started_at = None
        self._queue_policy = None
        self._queue_backup = None
        self._queue_warning = ""
        self._queue_auto_approvals = []
        self._batch_task_ids = []
        self._queue_stop_reason = ""
        self._queue_stop_error = ""
        self._sync_queue_panel()
        self._set_activity("Queue stopped." if stopped else "Queue complete.")
        if stopped and stop_reason == "error":
            detail = (": " + stop_error) if stop_error else "."
            self._notify_batch_attention(
                "error", "QGent queue stopped on error" + detail)
        elif stopped:
            self._notify_batch_attention("stopped", "QGent queue stopped.")
        else:
            self._notify_batch_attention(
                "complete", "QGent queue complete: {} passed, {} failed.".format(
                    passed, failures))

    @staticmethod
    def _queue_summary_text(rows, policy, backup_path, warning, wall_time,
                            failures, skipped, auto_approvals, stopped,
                            stop_reason=""):
        del failures, skipped  # Counts are derived from the immutable rows.
        return render_batch_summary(
            rows, policy, backup_path, warning, wall_time, auto_approvals,
            stopped=stopped, stop_reason=stop_reason)

    def _stop_queue_task(self, task_id):
        turn = self._active_turn or {}
        if turn.get("queue_task_id") != task_id:
            return
        task = self._queue_task(task_id)
        self._mark_queue_task(task, "failed", "Stopped by user.")
        self._cancel_pending_approvals("Current queue task stopped")
        busy = bool(self.backend is not None and self.backend.is_busy())
        if busy:
            self._ignore_next_terminal = True
            self.backend.cancel()
        self._end_stream()
        self._hide_thinking()
        self._active_turn = None
        self._finish_perf()
        self._set_activity("Current queue task stopped.")
        QTimer.singleShot(0, self._queue_idle_checkpoint)

    def stop_all_queue(self):
        if not self._queue_running:
            return
        self._queue_stop_requested = True
        self._queue_stop_reason = "user"
        self._queue_stop_error = ""
        self._history_append("queue", event="stop_all_requested")
        if self.bridge is not None:
            self.bridge.clear_batch_permission_mode()
        self._cancel_pending_approvals("Queue stopped")
        active_id = (self._active_turn or {}).get("queue_task_id")
        for task in self._queue_tasks:
            if task.get("status") == "queued" and task.get("id") in self._batch_task_ids:
                self._mark_queue_task(task, "skipped", "Queue stopped.")
        if active_id:
            self._mark_queue_task(
                self._queue_task(active_id), "failed", "Queue stopped.")
        busy = bool(self.backend is not None and self.backend.is_busy())
        if busy:
            self._ignore_next_terminal = True
            self.backend.cancel()
        self._end_stream()
        self._hide_thinking()
        self._active_turn = None
        self._finish_perf()
        self._sync_queue_panel()
        QTimer.singleShot(0, self._queue_idle_checkpoint)

    def on_stop(self):
        turn = self._active_turn or {}
        task_id = turn.get("queue_task_id")
        if task_id:
            self._stop_queue_task(task_id)
            return
        if self.backend is not None and self.backend.is_busy():
            self._cancel_pending_approvals("Turn stopped")
            self._ignore_next_terminal = True
            self.backend.cancel()
        self._end_stream()
        self._hide_thinking()
        self._set_activity("Stopped.")
        self._add_status_note("Turn stopped.")
        self._active_turn = None
        self._finish_perf()

    def new_session(self):
        self._cancel_pending_approvals("New session started")
        if self.bridge is not None:
            self.bridge.clear_batch_permission_mode()
        self._queue_running = False
        self._queue_pause_after_current = False
        self._queue_stop_requested = False
        self._queue_started_at = None
        self._queue_policy = None
        self._queue_backup = None
        self._queue_warning = ""
        self._queue_auto_approvals = []
        self._queue_tasks = []
        self._batch_task_ids = []
        self._ignore_next_terminal = False
        self._queue_stop_reason = ""
        self._queue_stop_error = ""
        self.queue_panel.clear()
        if self.history_store is not None:
            try:
                self.history_store.delete()
            except Exception as exc:
                self._log_history_warning(
                    f"Could not clear history: {type(exc).__name__}: {exc}")
                self._set_activity("Could not clear chat history.")
                return
        self._update_export_enabled(False)
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
        if self._queue_running:
            self._set_activity("Stop the running queue before changing settings.")
            return
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
        if self._active_turn is not None:
            self._active_turn.setdefault("assistant_parts", []).append(str(text))
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
        canonical_name = _canonical_qgis_tool_name(name)
        if (self._active_turn is not None
                and canonical_name in _VISUAL_CHANGE_TOOLS):
            self._active_turn["visual_change_tool_ran"] = True
        self._end_stream()
        self._hide_thinking()
        if self._last_tool_chip is not None and not self._last_tool_finished:
            self._last_tool_chip.mark_done_without_result()
            self._history_append(
                "tool", event="finished", tool_id=self._last_tool_id,
                name=self._last_tool_name, args=self._last_tool_args,
                result="(no result captured)")
            self._last_tool_finished = True
            reports = (self._active_turn or {}).get("tool_reports") or []
            if reports and reports[-1].get("result") is None:
                reports[-1]["result"] = "(no result captured)"
        chip = ToolChip(name, args, self.t)
        self._last_tool_chip = chip
        self._last_tool_id = secrets.token_hex(8)
        self._last_tool_name = str(name)
        self._last_tool_args = bounded_json_value(args)
        self._last_tool_finished = False
        if self._active_turn is not None:
            self._active_turn.setdefault("tool_reports", []).append({
                "name": str(name),
                "args": bounded_json_value(args),
                "result": None,
            })
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
        reports = (self._active_turn or {}).get("tool_reports") or []
        for report in reversed(reports):
            if report.get("result") is None:
                report["result"] = str(text)
                break
        if _canonical_qgis_tool_name(self._last_tool_name) == _SNAPSHOT_TOOL:
            self._record_explicit_snapshot(text)

    def _on_subagent_event(self, name, status):
        if self._active_turn is not None:
            self._active_turn.setdefault("subagent_events", []).append({
                "name": str(name), "status": str(status),
            })
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
        if self._ignore_next_terminal:
            self._ignore_next_terminal = False
            self._end_stream(persist=False)
            self._hide_thinking()
            QTimer.singleShot(0, self._queue_idle_checkpoint)
            return
        turn = self._active_turn
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
        self._capture_terminal_snapshot(turn)
        self._set_activity("Ready.")
        task_id = (turn or {}).get("queue_task_id")
        task = self._queue_task(task_id) if task_id else None
        self._apply_turn_report(task, turn, _result)
        self._active_turn = None
        self._finish_perf()
        if task_id:
            self._mark_queue_task(task, "done")
            QTimer.singleShot(0, self._queue_idle_checkpoint)

    def _on_error(self, message):
        if self._ignore_next_terminal:
            self._ignore_next_terminal = False
            self._end_stream(persist=False)
            self._hide_thinking()
            QTimer.singleShot(0, self._queue_idle_checkpoint)
            return
        turn = self._active_turn
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
        self._capture_terminal_snapshot(turn)
        self._set_activity("Error.")
        task_id = (turn or {}).get("queue_task_id")
        task = self._queue_task(task_id) if task_id else None
        self._apply_turn_report(task, turn)
        self._active_turn = None
        self._finish_perf()
        if task_id:
            self._stop_queue_after_error(task, str(message))

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
        self.action_btn.set_busy(bool(busy) or self._queue_running)
        self.stop_turn_btn.setVisible(bool(busy) and not self._queue_running)
        if busy:
            self._show_thinking()
        else:
            self._hide_thinking()
            QTimer.singleShot(0, self._queue_idle_checkpoint)

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
        approval_id = str(ap.get("approval_id") or secrets.token_hex(8))
        code = clipped_text(ap.get("code", ""))
        reasons = [clipped_text(reason, 1000)
                   for reason in (ap.get("reasons") or [])]
        card = ApprovalCard(code, reasons, self.t)
        self._approval_history[approval_id] = card
        self._live_approvals[approval_id] = {"card": card, "payload": ap}
        self._history_append(
            "approval", event="requested", approval_id=approval_id,
            code=code, reasons=reasons)

        task_id = (self._active_turn or {}).get("queue_task_id")
        task = self._queue_task(task_id) if task_id else None
        if self._queue_running and task is not None:
            task["status"] = "waiting_approval"
            self._history_append(
                "queue", event="awaiting_approval", task_id=task_id,
                reasons=reasons)
            self._sync_queue_panel()
            self._notify_batch_attention(
                "approval", "QGent is waiting for a destructive-step approval.")

        def decide(approved):
            if ap.get("cancelled") or ap["event"].is_set():
                card.set_cancelled(
                    ap.get("cancel_reason") or "This approval is no longer active.")
                self._live_approvals.pop(approval_id, None)
                return
            ap["approved"] = approved
            ap["event"].set()
            self._history_append(
                "approval", event="decided", approval_id=approval_id,
                code=code, reasons=reasons, approved=bool(approved))
            self._live_approvals.pop(approval_id, None)
            if task is not None and task.get("status") == "waiting_approval":
                task["status"] = "running"
                self._sync_queue_panel()

        card.decided.connect(decide)
        self._add_widget(card)
        card.attention()

    def _cancel_pending_approvals(self, reason):
        if self.bridge is not None:
            self.bridge.cancel_pending_approvals(str(reason))
        for approval_id, item in list(self._live_approvals.items()):
            card = item.get("card")
            if card is not None:
                card.set_cancelled(reason)
            self._history_append(
                "approval", event="cancelled", approval_id=approval_id,
                reason=str(reason))
        self._live_approvals.clear()

    def _on_approval_finished(self, payload):
        approval_id = str(payload.get("approval_id") or "")
        item = self._live_approvals.pop(approval_id, None)
        if item is None or not payload.get("cancelled"):
            return
        reason = str(payload.get("reason") or "Approval is no longer active.")
        card = item.get("card")
        if card is not None:
            card.set_cancelled(reason)
        self._history_append(
            "approval", event="cancelled", approval_id=approval_id,
            reason=reason)

    def _on_auto_approved(self, payload):
        if not (self._queue_running and payload.get("batch_scoped")):
            return
        record = {
            "code": clipped_text(payload.get("code", ""), 1000),
            "reasons": [clipped_text(reason, 1000)
                        for reason in (payload.get("reasons") or [])],
        }
        self._queue_auto_approvals.append(record)
        self._history_append(
            "queue", event="auto_approved", code=record["code"],
            reasons=record["reasons"])

    def _notify_batch_attention(self, kind, message):
        """Fan one queue event out through every supported native channel."""
        kind = str(kind)
        message = str(message)
        channels = []
        try:
            bar = self.iface.messageBar()
            bar.pushMessage(
                "QGent", message, level=Qgis.Info, duration=8)
            channels.append("message_bar")
        except Exception as exc:
            self._log_history_warning(
                "Could not show QGent message-bar notification: {}: {}".format(
                    type(exc).__name__, exc))
        try:
            QApplication.alert(self.iface.mainWindow(), 0)
            channels.append("taskbar_alert")
        except Exception as exc:
            self._log_history_warning(
                "Could not request QGent taskbar attention: {}: {}".format(
                    type(exc).__name__, exc))
        try:
            if (QSystemTrayIcon.isSystemTrayAvailable()
                    and QSystemTrayIcon.supportsMessages()):
                if self._tray_icon is None:
                    icon_path = os.path.join(
                        self.plugin_dir, "resources", "icon.svg")
                    self._tray_icon = QSystemTrayIcon(QIcon(icon_path), self)
                    self._tray_icon.setToolTip("QGent")
                    self._tray_icon.show()
                self._tray_icon.showMessage(
                    "QGent", message, QSystemTrayIcon.Information, 8000)
                channels.append("tray_balloon")
        except Exception:
            # Platform tray support is optional and deliberately silent.
            pass
        record = {"kind": kind, "message": message,
                  "channels": list(channels)}
        self._notification_events.append(record)
        self._history_append(
            "queue", event="attention", attention_kind=kind,
            message=message, channels=list(channels))
        self._set_activity(message)

    # ======================================================================
    # Message list helpers
    # ======================================================================
    def _add_assistant_message(self, text, persist=True, timestamp=None):
        bubble = MessageBubble("assistant", self.t)
        self._add_widget(bubble)
        if timestamp is not None:
            bubble.set_timestamp(timestamp)
        bubble.set_text(str(text))
        # Dynamic widgets can remain explicitly hidden in some QScrollArea
        # paths (notably an offscreen batch-idle callback); make the completed
        # assistant card visible after it has a measured document height.
        bubble.show()
        if persist:
            self._history_append("assistant", text=str(text))
        return bubble

    def _add_user_message(self, text, tags=None, attachments=None,
                          persist=True, timestamp=None):
        bubble = MessageBubble("user", self.t)
        bubble.set_tags(tags)
        self._add_widget(bubble)
        if timestamp is not None:
            bubble.set_timestamp(timestamp)
        bubble.set_text(text)
        if persist:
            self._history_append(
                "user", text=str(text), tags=list(tags or []),
                attachments=bounded_json_value(list(attachments or [])))

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
        self._live_approvals.clear()
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
        self._cancel_pending_approvals("QGent closed")
        if self.bridge is not None:
            self.bridge.clear_batch_permission_mode()
        self._queue_running = False
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
        if self._tray_icon is not None:
            try:
                self._tray_icon.hide()
                self._tray_icon.deleteLater()
            except RuntimeError:
                pass
            self._tray_icon = None


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
