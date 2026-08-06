# -*- coding: utf-8 -*-
"""General settings and Doctor UI for QGent."""
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QToolButton,
    QScrollArea, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .. import config
from ..doctor import DoctorService, DoctorWorker, repair_model_options
from ..doctor_core import (
    ensure_recovery_entrypoint, launch_detached, write_doctor_request)
from .widgets import FAST_MODE_TOOLTIP


class SettingsDialog(QDialog):
    def __init__(self, parent=None, doctor_context=None):
        super().__init__(parent)
        config.migrate_model_settings()
        self.doctor_context = dict(doctor_context or {})
        self.doctor_service = None
        self._diag_worker = None
        self._diag_rerun = False
        self._pending_external_launch = False
        self._last_report = None
        self._chat_busy = False
        self._model_controls = {}
        self._model_memory = {
            backend: {
                role: dict(config.get_model_choice(backend, role))
                for role in config.MODEL_ROLES
            }
            for backend in ("claude", "codex")
        }
        self._model_original = {
            backend: {
                role: dict(self._model_memory[backend][role])
                for role in config.MODEL_ROLES
            }
            for backend in ("claude", "codex")
        }
        self._active_model_backend = None
        self._loading_models = False
        self.setWindowTitle("QGent — Settings")
        self.setMinimumSize(720, 680)
        self._build()
        self._load()
        self._init_doctor()

    # ==================================================================
    # Construction
    # ==================================================================
    def _build(self):
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_general_tab(), "General")
        self.tabs.addTab(self._build_doctor_tab(), "Doctor")
        outer.addWidget(self.tabs, 1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._save_and_accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)

    def _build_general_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)

        gb = QGroupBox("Agent backend")
        form = QFormLayout(gb)
        self.backend = QComboBox()
        self.backend.addItem("Claude Code (Claude Pro/Max)", "claude")
        self.backend.addItem("Codex (ChatGPT) — single-agent", "codex")
        form.addRow("Backend", self.backend)

        cli_row = QHBoxLayout()
        self.cli_path = QLineEdit()
        browse = QPushButton("…")
        browse.setMaximumWidth(32)
        browse.clicked.connect(self._browse_cli)
        cli_row.addWidget(self.cli_path)
        cli_row.addWidget(browse)
        self.cli_label = QLabel("Claude CLI path")
        form.addRow(self.cli_label, cli_row)
        self.cli_status = QLabel(
            "CLI not found — install it or choose its executable.")
        self.cli_status.setWordWrap(True)
        self.cli_status.setStyleSheet("color: #b26a00; font-size: 11px;")
        form.addRow("", self.cli_status)
        self.cli_path.textChanged.connect(self._refresh_cli_status)
        outer.addWidget(gb)

        self.models_group = QGroupBox("Models — Claude Code")
        mform = QFormLayout(self.models_group)
        self.model_preset = QComboBox()
        self._populate_model_presets("claude")
        mform.addRow("Preset", self.model_preset)
        self.fast_mode = QCheckBox("Use Fast mode for new turns")
        self.fast_mode.setToolTip(FAST_MODE_TOOLTIP)
        mform.addRow("Fast mode", self.fast_mode)

        self.models_advanced_toggle = QToolButton()
        self.models_advanced_toggle.setText("Advanced")
        self.models_advanced_toggle.setCheckable(True)
        self.models_advanced_toggle.setToolButtonStyle(
            Qt.ToolButtonTextBesideIcon)
        self.models_advanced_toggle.setArrowType(Qt.RightArrow)
        mform.addRow(self.models_advanced_toggle)

        self.models_advanced_container = QWidget()
        advanced_form = QFormLayout(self.models_advanced_container)
        advanced_form.setContentsMargins(18, 0, 0, 0)
        sup_row, self.model_sup, self.model_sup_custom = (
            self._create_model_selector("supervisor"))
        worker_row, self.model_worker, self.model_worker_custom = (
            self._create_model_selector("worker"))
        light_row, self.model_light, self.model_light_custom = (
            self._create_model_selector("light"))
        self.model_sup_label = QLabel("Supervisor / geoprocessor / cartographer")
        self.model_worker_label = QLabel("Worker (override)")
        self.model_light_label = QLabel("Light (data-scout, qa-verifier)")
        self.model_light_label.setToolTip(config.LIGHT_ROLE_TOOLTIP)
        light_row.setToolTip(config.LIGHT_ROLE_TOOLTIP)
        self.model_light.setToolTip(config.LIGHT_ROLE_TOOLTIP)
        advanced_form.addRow(self.model_sup_label, sup_row)
        advanced_form.addRow(self.model_worker_label, worker_row)
        advanced_form.addRow(self.model_light_label, light_row)
        note = QLabel(
            "Each model appears once. Choose Custom… only when you need to "
            "enter an unlisted raw CLI model id.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid); font-size: 11px;")
        advanced_form.addRow(note)
        mform.addRow(self.models_advanced_container)
        self.codex_single_agent_note = QLabel(
            "Codex runs single-agent — only the main model is used.")
        self.codex_single_agent_note.setWordWrap(True)
        self.codex_single_agent_note.setStyleSheet(
            "color: palette(mid); font-size: 11px;")
        mform.addRow(self.codex_single_agent_note)
        outer.addWidget(self.models_group)
        self.model_preset.currentIndexChanged.connect(
            self._on_model_preset_changed)
        self.models_advanced_toggle.toggled.connect(
            self._set_models_advanced_visible)
        self._set_models_advanced_visible(False)
        self.backend.currentIndexChanged.connect(self._update_backend_rows)

        gs = QGroupBox("Safety & execution")
        sform = QFormLayout(gs)
        self.perm = QComboBox()
        self.perm.addItem(
            "Ask before destructive ops (recommended)", "ask_destructive")
        self.perm.addItem("Ask before every execute", "ask_always")
        self.perm.addItem("Auto-run everything (no prompts)", "auto")
        sform.addRow("Approval", self.perm)
        self.verifier = QCheckBox("Run QA-verifier on multi-step tasks")
        sform.addRow("Verification", self.verifier)
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 600)
        self.timeout.setSuffix(" s")
        sform.addRow("Execution timeout", self.timeout)
        self.max_result = QSpinBox()
        self.max_result.setRange(500, 200000)
        self.max_result.setSingleStep(500)
        self.max_result.setSuffix(" chars")
        sform.addRow("Max tool result size", self.max_result)
        outer.addWidget(gs)

        ga = QGroupBox("Appearance")
        aform = QFormLayout(ga)
        self.show_map_snapshots = QCheckBox("Show map snapshots in chat")
        aform.addRow("Maps", self.show_map_snapshots)
        self.reduce_motion = QCheckBox("Reduce motion (disable animations)")
        aform.addRow("Motion", self.reduce_motion)
        outer.addWidget(ga)
        outer.addStretch(1)
        return page

    def _build_doctor_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        page = QWidget()
        outer = QVBoxLayout(page)

        intro = QLabel(
            "Run live diagnostics and deterministic self-heal here. AI repair "
            "runs in a detached External Doctor console so it remains available "
            "while QGIS is closed or restarting.")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        diagnostics = QGroupBox("Diagnostics and deterministic self-heal")
        dlay = QVBoxLayout(diagnostics)
        dbuttons = QHBoxLayout()
        self.run_diag_btn = QPushButton("Run diagnostics")
        self.run_diag_btn.clicked.connect(self._run_diagnostics)
        dbuttons.addWidget(self.run_diag_btn)
        self.auto_repair_btn = QPushButton("Auto-repair failed checks")
        self.auto_repair_btn.setEnabled(False)
        self.auto_repair_btn.clicked.connect(self._auto_repair)
        dbuttons.addWidget(self.auto_repair_btn)
        self.copy_diag_btn = QPushButton("Copy")
        self.copy_diag_btn.clicked.connect(self._copy_diagnostics)
        dbuttons.addWidget(self.copy_diag_btn)
        dbuttons.addStretch(1)
        dlay.addLayout(dbuttons)
        self.diag_output = QPlainTextEdit()
        self.diag_output.setReadOnly(True)
        self.diag_output.setMinimumHeight(220)
        self.diag_output.setPlaceholderText(
            "Run diagnostics to collect CLI, bridge, stream, history, "
            "byte-compile, and QGIS-log evidence.")
        dlay.addWidget(self.diag_output)
        outer.addWidget(diagnostics)

        external = QGroupBox("External Doctor — detached repair")
        elay = QVBoxLayout(external)
        model_row = QFormLayout()
        self.repair_model = QComboBox()
        model_row.addRow("Repair model", self.repair_model)
        elay.addLayout(model_row)
        explanation = QLabel(
            "The External Doctor prepares a disposable proposal, writes and "
            "prints a unified diff, and changes real files only after you type "
            "yes in its console. It backs up and verifies every approved apply.")
        explanation.setWordWrap(True)
        explanation.setStyleSheet("color: palette(mid); font-size: 11px;")
        elay.addWidget(explanation)
        self.error_description = QTextEdit()
        self.error_description.setPlaceholderText(
            "Describe the error (optional — diagnostics alone are valid input)")
        self.error_description.setMaximumHeight(100)
        elay.addWidget(self.error_description)
        self.launch_external_btn = QPushButton(
            "Runs outside QGIS — you can close or restart QGIS during the repair.")
        self.launch_external_btn.clicked.connect(self._launch_external_doctor)
        elay.addWidget(self.launch_external_btn)
        self.external_launch_status = QLabel("")
        self.external_launch_status.setWordWrap(True)
        self.external_launch_status.setStyleSheet(
            "color: palette(mid); font-size: 11px;")
        elay.addWidget(self.external_launch_status)
        recovery = QLabel(
            "If QGent won't load, run qgent-doctor.bat from your QGIS profile "
            "folder.")
        recovery.setWordWrap(True)
        recovery.setStyleSheet("color: #9a6500; font-size: 11px;")
        elay.addWidget(recovery)
        outer.addWidget(external)

        outer.addStretch(1)
        scroll.setWidget(page)
        return scroll

    # ==================================================================
    # General settings
    # ==================================================================
    def _create_model_selector(self, role):
        row = QWidget()
        row.setMinimumHeight(24)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        combo = QComboBox()
        combo.setEditable(False)
        combo.setMinimumHeight(24)
        custom = QLineEdit()
        custom.setMinimumHeight(24)
        custom.setPlaceholderText("Raw model id")
        custom.setVisible(False)
        layout.addWidget(combo, 1)
        layout.addWidget(custom, 1)
        self._model_controls[role] = {
            "row": row, "combo": combo, "custom": custom,
            "preserve_raw_choice": False, "preserved_choice": None,
        }
        combo.currentIndexChanged.connect(
            lambda _index, selected_role=role:
            self._toggle_model_custom(selected_role))
        custom.textEdited.connect(
            lambda _text, selected_role=role:
            self._mark_raw_model_edited(selected_role))
        return row, combo, custom

    def _update_backend_rows(self):
        if self._loading_models:
            return
        backend = self.backend.currentData() or "claude"
        if (self._active_model_backend
                and self._active_model_backend != backend):
            self._remember_visible_model_choices()
        self._active_model_backend = backend
        self._update_cli_row()
        self._populate_model_combos(backend)
        self._update_model_mode(backend)
        self._sync_model_preset(backend)

    def _update_cli_row(self):
        is_codex = self.backend.currentData() == "codex"
        self.cli_label.setText(
            "Codex CLI path" if is_codex else "Claude CLI path")
        detected = config.detect_codex() if is_codex else config.detect_claude()
        self.cli_path.setText(detected)
        self._refresh_cli_status()

    def _refresh_cli_status(self):
        self.cli_status.setVisible(not bool(self.cli_path.text().strip()))

    def _populate_model_combos(self, backend):
        self._loading_models = True
        try:
            for role in config.MODEL_ROLES:
                controls = self._model_controls[role]
                combo = controls["combo"]
                custom_edit = controls["custom"]
                choice = self._model_memory[backend][role]
                combo.blockSignals(True)
                combo.clear()
                for label, model_id in config.model_options(backend):
                    combo.addItem(label, model_id)
                combo.addItem("Custom…", config.CUSTOM_MODEL_SENTINEL)
                raw_value = str(choice.get("model_id") or "").strip()
                explicit_custom = bool(choice.get("custom"))
                exact_catalog_id = raw_value in config.model_ids(backend)
                show_raw = explicit_custom or not exact_catalog_id
                if show_raw:
                    index = combo.findData(config.CUSTOM_MODEL_SENTINEL)
                    custom_edit.setText(raw_value)
                    controls["preserve_raw_choice"] = True
                    controls["preserved_choice"] = {
                        "model_id": raw_value, "custom": explicit_custom,
                    }
                else:
                    index = combo.findData(raw_value)
                    custom_edit.clear()
                    controls["preserve_raw_choice"] = False
                    controls["preserved_choice"] = None
                combo.setCurrentIndex(max(0, index))
                combo.blockSignals(False)
                custom_edit.setVisible(show_raw)
        finally:
            self._loading_models = False

    def _populate_model_presets(self, backend):
        """Rebuild the preset rows: not every preset applies to every backend."""
        self.model_preset.blockSignals(True)
        try:
            self.model_preset.clear()
            for label, preset in config.model_preset_options(backend):
                self.model_preset.addItem(label, preset)
        finally:
            self.model_preset.blockSignals(False)

    def _sync_model_preset(self, backend):
        self._populate_model_presets(backend)
        preset = config.classify_model_preset(
            backend, self._model_memory.get(backend, {}))
        index = self.model_preset.findData(preset)
        self.model_preset.blockSignals(True)
        try:
            self.model_preset.setCurrentIndex(max(0, index))
        finally:
            self.model_preset.blockSignals(False)
        self._set_models_advanced_expanded(
            preset == config.MODEL_PRESET_CUSTOM)

    def _on_model_preset_changed(self):
        if self._loading_models:
            return
        backend = self._active_model_backend
        preset = self.model_preset.currentData()
        if backend not in self._model_memory:
            return
        if preset == config.MODEL_PRESET_CUSTOM:
            self._set_models_advanced_expanded(True)
            return
        values = config.model_preset_values(backend, preset)
        if len(values) != len(config.MODEL_ROLES):
            return
        self._model_memory[backend] = {
            role: {"model_id": values[role], "custom": False}
            for role in config.MODEL_ROLES
        }
        self._populate_model_combos(backend)
        self._set_models_advanced_expanded(False)

    def _set_models_advanced_visible(self, expanded):
        expanded = bool(expanded)
        self.models_advanced_container.setVisible(expanded)
        self.models_advanced_toggle.setArrowType(
            Qt.DownArrow if expanded else Qt.RightArrow)

    def _set_models_advanced_expanded(self, expanded):
        expanded = bool(expanded)
        self.models_advanced_toggle.blockSignals(True)
        try:
            self.models_advanced_toggle.setChecked(expanded)
        finally:
            self.models_advanced_toggle.blockSignals(False)
        self._set_models_advanced_visible(expanded)

    def _mark_model_preset_custom(self):
        index = self.model_preset.findData(config.MODEL_PRESET_CUSTOM)
        self.model_preset.blockSignals(True)
        try:
            self.model_preset.setCurrentIndex(max(0, index))
        finally:
            self.model_preset.blockSignals(False)
        self._set_models_advanced_expanded(True)

    def _toggle_model_custom(self, role):
        if self._loading_models:
            return
        controls = self._model_controls[role]
        controls["preserve_raw_choice"] = False
        controls["preserved_choice"] = None
        custom = controls["combo"].currentData() == config.CUSTOM_MODEL_SENTINEL
        controls["custom"].setVisible(custom)
        if custom:
            controls["custom"].setFocus(Qt.OtherFocusReason)
        self._remember_visible_model_choices()
        self._mark_model_preset_custom()

    def _mark_raw_model_edited(self, role):
        if self._loading_models:
            return
        controls = self._model_controls[role]
        controls["preserve_raw_choice"] = False
        controls["preserved_choice"] = None
        self._remember_visible_model_choices()
        self._mark_model_preset_custom()

    def _remember_visible_model_choices(self):
        backend = self._active_model_backend
        if backend not in self._model_memory:
            return
        for role in config.MODEL_ROLES:
            controls = self._model_controls[role]
            value = controls["combo"].currentData()
            custom = value == config.CUSTOM_MODEL_SENTINEL
            if custom:
                value = controls["custom"].text().strip()
                preserved = controls.get("preserved_choice") or {}
                if (controls.get("preserve_raw_choice")
                        and value == preserved.get("model_id")):
                    custom = bool(preserved.get("custom"))
                else:
                    custom = bool(value)
                    value = value or config.default_model(backend, role)
            self._model_memory[backend][role] = {
                "model_id": str(value or ""),
                "custom": bool(custom),
            }

    def _update_model_mode(self, backend):
        is_codex = backend == "codex"
        caption = "Codex runs single-agent — only the main model is used."
        self.models_group.setTitle(
            "Models — Codex" if is_codex else "Models — Claude Code")
        self.codex_single_agent_note.setVisible(is_codex)
        worker_row = self._model_controls["worker"]["row"]
        worker_row.setEnabled(not is_codex)
        self.model_worker_label.setEnabled(not is_codex)
        worker_row.setToolTip(caption if is_codex else "")
        self.model_worker_label.setToolTip(caption if is_codex else "")

        light_row = self._model_controls["light"]["row"]
        light_row.setEnabled(not is_codex)
        self.model_light_label.setEnabled(not is_codex)
        light_row.setToolTip(config.LIGHT_ROLE_TOOLTIP)
        self.model_light_label.setToolTip(config.LIGHT_ROLE_TOOLTIP)
        self.model_light.setToolTip(config.LIGHT_ROLE_TOOLTIP)

    def _browse_cli(self):
        start = self.cli_path.text() or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Select CLI executable", start)
        if path:
            self.cli_path.setText(path)

    def _load(self):
        idx = self.backend.findData(config.get(config.K_BACKEND))
        self.backend.setCurrentIndex(max(0, idx))
        self._update_backend_rows()
        pidx = self.perm.findData(config.get(config.K_PERMISSION_MODE))
        self.perm.setCurrentIndex(max(0, pidx))
        self.verifier.setChecked(config.get(config.K_VERIFIER))
        self.fast_mode.setChecked(config.get(config.K_FAST_MODE))
        self.timeout.setValue(config.get(config.K_EXEC_TIMEOUT))
        self.max_result.setValue(config.get(config.K_MAX_RESULT))
        self.show_map_snapshots.setChecked(
            config.get(config.K_SHOW_MAP_SNAPSHOTS))
        self.reduce_motion.setChecked(config.get(config.K_REDUCE_MOTION))

    def _save_and_accept(self):
        if self._long_operation_running():
            self.diag_output.appendPlainText(
                "Finish diagnostics before closing Settings.\n")
            self.tabs.setCurrentIndex(1)
            return
        backend_kind = self.backend.currentData()
        self._remember_visible_model_choices()
        config.set(config.K_BACKEND, backend_kind)
        if backend_kind == "codex":
            config.set(config.K_CODEX_PATH, self.cli_path.text().strip())
        else:
            config.set(config.K_CLAUDE_PATH, self.cli_path.text().strip())
        for stored_backend in ("claude", "codex"):
            for role in config.MODEL_ROLES:
                choice = self._model_memory[stored_backend][role]
                original = self._model_original[stored_backend][role]
                if (choice["model_id"] == original["model_id"]
                        and bool(choice["custom"]) == bool(original["custom"])):
                    continue
                config.set_model_choice(
                    stored_backend, role, choice["model_id"], choice["custom"])
        # Keep legacy keys synchronized for older callers, but all Goal 6 code
        # reads the per-backend keys above.
        legacy_keys = {
            "supervisor": config.K_MODEL_SUPERVISOR,
            "worker": config.K_MODEL_WORKER,
            "light": config.K_MODEL_LIGHT,
        }
        for role, legacy_key in legacy_keys.items():
            config.set(
                legacy_key, self._model_memory[backend_kind][role]["model_id"])
        config.set(config.K_PERMISSION_MODE, self.perm.currentData())
        config.set(config.K_VERIFIER, self.verifier.isChecked())
        config.set(config.K_FAST_MODE, self.fast_mode.isChecked())
        config.set(config.K_EXEC_TIMEOUT, self.timeout.value())
        config.set(config.K_MAX_RESULT, self.max_result.value())
        config.set(
            config.K_SHOW_MAP_SNAPSHOTS,
            self.show_map_snapshots.isChecked())
        config.set(config.K_REDUCE_MOTION, self.reduce_motion.isChecked())
        self.accept()

    # ==================================================================
    # Doctor
    # ==================================================================
    def _init_doctor(self):
        if not self.doctor_context:
            self.diag_output.setPlainText(
                "Doctor is available when Settings is opened from the live "
                "QGent panel.")
            for widget in (self.run_diag_btn, self.auto_repair_btn,
                           self.launch_external_btn, self.repair_model):
                widget.setEnabled(False)
            return
        self.doctor_service = DoctorService(self.doctor_context)
        self._refresh_repair_models()
        try:
            path = ensure_recovery_entrypoint(
                self._context_value("profile_dir", ""),
                self._context_value("plugin_dir", ""),
                self._context_value(
                    "python_executable", config.python_executable()),
                self._context_value("source_repo", ""))
            self.external_launch_status.setText(
                f"Recovery entry point ready: {path}")
        except Exception as exc:
            self.external_launch_status.setText(
                f"Recovery entry point could not be prepared: "
                f"{type(exc).__name__}: {exc}")
        self._update_doctor_controls()

    def _context_value(self, key, default=None):
        value = self.doctor_context.get(key, default)
        return value() if callable(value) else value

    def _refresh_repair_models(self):
        self.repair_model.clear()
        cli_paths = self._context_value("cli_paths", None)
        for spec in repair_model_options(cli_paths):
            self.repair_model.addItem(spec["label"], spec)

    def set_chat_busy(self, busy):
        self._chat_busy = bool(busy)
        self._update_doctor_controls()

    def _update_doctor_controls(self):
        available = self.doctor_service is not None
        diag_running = bool(
            self._diag_worker is not None and self._diag_worker.isRunning())
        self.run_diag_btn.setEnabled(available and not diag_running)
        failed_repairable = bool(
            self._last_report and any(
                not item.get("ok") and item.get("repairable")
                for item in self._last_report.get("checks", [])))
        self.auto_repair_btn.setEnabled(
            available and failed_repairable and not diag_running)
        self.launch_external_btn.setEnabled(
            available and self.repair_model.count() > 0
            and not self._chat_busy and not diag_running)

    def _run_diagnostics(self):
        if self.doctor_service is None:
            return
        if self._diag_worker is not None and self._diag_worker.isRunning():
            self._diag_rerun = True
            return
        self.diag_output.setPlainText("Running diagnostics…")
        self._diag_worker = DoctorWorker(self.doctor_service, self)
        self._diag_worker.completed.connect(self._on_diagnostics_complete)
        self._diag_worker.failed.connect(self._on_diagnostics_failed)
        self._diag_worker.finished.connect(self._on_diagnostics_finished)
        self._diag_worker.start()
        self._update_doctor_controls()

    def _on_diagnostics_complete(self, report):
        self._last_report = report
        self.diag_output.setPlainText(report.get("bundle") or "")
        self._update_doctor_controls()

    def _on_diagnostics_failed(self, message):
        self.diag_output.setPlainText(f"Diagnostics failed: {message}")

    def _on_diagnostics_finished(self):
        worker = self._diag_worker
        self._diag_worker = None
        if worker is not None:
            worker.deleteLater()
        rerun = self._diag_rerun
        self._diag_rerun = False
        pending = self._pending_external_launch
        self._pending_external_launch = False
        self._update_doctor_controls()
        if rerun:
            self._run_diagnostics()
        elif pending and self._last_report is not None:
            self._launch_external_doctor()

    def _auto_repair(self):
        if self.doctor_service is None or self._last_report is None:
            return
        try:
            actions = self.doctor_service.auto_repair(self._last_report)
            self.diag_output.appendPlainText(
                "\nAuto-repair actions:\n- " + "\n- ".join(actions)
                + "\n\nRe-running diagnostics…")
        except Exception as exc:
            self.diag_output.appendPlainText(
                f"\nAuto-repair failed: {type(exc).__name__}: {exc}")
            return
        self._last_report = None
        self._run_diagnostics()

    def _copy_diagnostics(self):
        QApplication.clipboard().setText(self.diag_output.toPlainText())

    def _launch_external_doctor(self):
        if self.doctor_service is None or self._chat_busy:
            return
        if self._last_report is None:
            self._pending_external_launch = True
            self._run_diagnostics()
            return
        spec = self.repair_model.currentData()
        if not spec:
            self.external_launch_status.setText(
                "No installed repair CLI/model is available.")
            return
        profile_dir = self._context_value("profile_dir", "")
        plugin_dir = self._context_value("plugin_dir", "")
        source_repo = self._context_value("source_repo", "")
        python_executable = self._context_value(
            "python_executable", config.python_executable())
        cli_paths = {
            "claude": config.detect_claude(),
            "codex": config.detect_codex(),
        }
        logs = list(self._context_value("log_entries", []) or [])[-200:]
        repair = dict(spec)
        request_payload = {
            "diagnostics_bundle": self._last_report.get("bundle") or "",
            "log_ring_buffer": logs,
            "user_description": self.error_description.toPlainText().strip(),
            "repair": repair,
            "cli_paths": cli_paths,
            "plugin_paths": {
                "installed_tree": str(plugin_dir),
                "source_repo": str(source_repo),
                "profile_dir": str(profile_dir),
            },
            "qgis": {
                "executable_path": str(self._context_value(
                    "qgis_executable_path", "")),
                "project_path": str(self._context_value(
                    "project_filename", "")),
            },
        }
        try:
            request_path = write_doctor_request(profile_dir, request_payload)
            recovery = ensure_recovery_entrypoint(
                profile_dir, plugin_dir, python_executable, source_repo)
            doctor_cli = os.path.join(str(plugin_dir), "doctor_cli.py")
            command = [
                str(python_executable), doctor_cli,
                "--profile-dir", str(profile_dir),
                "--plugin-dir", str(plugin_dir),
                "--source-repo", str(source_repo),
                "--request", str(request_path),
            ]
            pid = launch_detached(command, cwd=plugin_dir)
            self.external_launch_status.setText(
                f"External Doctor launched (PID {pid}). Handoff saved; "
                f"recovery BAT ready at {os.path.basename(str(recovery))}.")
        except Exception as exc:
            self.external_launch_status.setText(
                f"External Doctor launch failed: {type(exc).__name__}: {exc}")
        self._update_doctor_controls()

    def _long_operation_running(self):
        return bool(self._diag_worker is not None
                    and self._diag_worker.isRunning())

    def reject(self):
        if self._long_operation_running():
            self.tabs.setCurrentIndex(1)
            self.diag_output.appendPlainText(
                "Finish diagnostics before closing Settings.\n")
            return
        super().reject()

    def closeEvent(self, event):
        if self._long_operation_running():
            self.tabs.setCurrentIndex(1)
            self.diag_output.appendPlainText(
                "Finish diagnostics before closing Settings.\n")
            event.ignore()
            return
        super().closeEvent(event)

    def shutdown(self):
        pass
