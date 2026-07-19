# -*- coding: utf-8 -*-
"""General settings and Doctor UI for QGent."""
import json
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QTextCursor
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
    QScrollArea, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .. import config
from ..agent.repair_backend import RepairBackend
from ..doctor import (
    DoctorActionWorker, DoctorService, DoctorWorker, repair_model_options)


class SettingsDialog(QDialog):
    def __init__(self, parent=None, doctor_context=None):
        super().__init__(parent)
        self.doctor_context = dict(doctor_context or {})
        self.doctor_service = None
        self.repair_backend = None
        self._diag_worker = None
        self._action_worker = None
        self._action_kind = ""
        self._diag_rerun = False
        self._pending_repair = False
        self._last_report = None
        self._current_proposal = None
        self._chat_busy = False
        self._repair_busy = False
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

        gm = QGroupBox("Models")
        mform = QFormLayout(gm)
        self.model_sup = self._model_combo()
        self.model_worker = self._model_combo()
        self.model_light = self._model_combo()
        mform.addRow("Supervisor / geoprocessor / cartographer", self.model_sup)
        mform.addRow("Worker (override)", self.model_worker)
        mform.addRow("Light (data-scout, qa-verifier)", self.model_light)
        note = QLabel(
            "Choose a model offered for the selected backend, or type a "
            "custom model id. Existing custom values remain valid.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid); font-size: 11px;")
        mform.addRow(note)
        outer.addWidget(gm)
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
            "Diagnose QGent, apply only known deterministic remedies, or ask "
            "a high-power model to propose a repair in a disposable copy. "
            "AI changes are never applied until you review the diff and click Approve.")
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
        self.diag_output.setMinimumHeight(180)
        self.diag_output.setPlaceholderText(
            "Run diagnostics to collect CLI, bridge, stream, history, "
            "byte-compile, and QGIS-log evidence.")
        dlay.addWidget(self.diag_output)
        outer.addWidget(diagnostics)

        ai = QGroupBox("AI error-fix — sandboxed proposal")
        alay = QVBoxLayout(ai)
        model_row = QFormLayout()
        self.repair_model = QComboBox()
        model_row.addRow("Repair model", self.repair_model)
        alay.addLayout(model_row)
        warning = QLabel(
            "High-effort models can take several minutes. The agent edits only "
            "a temporary copy; Approve creates a backup before applying anything.")
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #9a6500; font-size: 11px;")
        alay.addWidget(warning)
        self.error_description = QTextEdit()
        self.error_description.setPlaceholderText(
            "Describe the error (optional — diagnostics alone are valid input)")
        self.error_description.setMaximumHeight(90)
        alay.addWidget(self.error_description)
        action_row = QHBoxLayout()
        self.propose_btn = QPushButton("Diagnose && propose fix")
        self.propose_btn.clicked.connect(self._start_repair)
        action_row.addWidget(self.propose_btn)
        self.cancel_repair_btn = QPushButton("Cancel")
        self.cancel_repair_btn.setEnabled(False)
        self.cancel_repair_btn.clicked.connect(self._cancel_repair)
        action_row.addWidget(self.cancel_repair_btn)
        action_row.addStretch(1)
        alay.addLayout(action_row)
        self.repair_progress = QPlainTextEdit()
        self.repair_progress.setReadOnly(True)
        self.repair_progress.setMinimumHeight(130)
        self.repair_progress.setPlaceholderText("Streaming repair progress")
        alay.addWidget(self.repair_progress)
        self.proposal_output = QPlainTextEdit()
        self.proposal_output.setReadOnly(True)
        self.proposal_output.setMinimumHeight(180)
        self.proposal_output.setPlaceholderText(
            "The agent's explanation and unified diff will appear here for review.")
        alay.addWidget(self.proposal_output)
        review_row = QHBoxLayout()
        self.approve_btn = QPushButton("Approve")
        self.approve_btn.setEnabled(False)
        self.approve_btn.clicked.connect(self._approve_proposal)
        review_row.addWidget(self.approve_btn)
        self.deny_btn = QPushButton("Deny")
        self.deny_btn.setEnabled(False)
        self.deny_btn.clicked.connect(self._deny_proposal)
        review_row.addWidget(self.deny_btn)
        review_row.addStretch(1)
        alay.addLayout(review_row)
        outer.addWidget(ai)

        backups = QGroupBox("Backups and rollback")
        blay = QVBoxLayout(backups)
        self.backup_list = QListWidget()
        self.backup_list.setMaximumHeight(110)
        self.backup_list.itemSelectionChanged.connect(
            self._update_doctor_controls)
        blay.addWidget(self.backup_list)
        backup_row = QHBoxLayout()
        self.restore_btn = QPushButton("Restore selected backup")
        self.restore_btn.clicked.connect(self._restore_backup)
        backup_row.addWidget(self.restore_btn)
        self.reload_btn = QPushButton("Reload plugin")
        self.reload_btn.setEnabled(False)
        self.reload_btn.clicked.connect(self._reload_plugin)
        backup_row.addWidget(self.reload_btn)
        backup_row.addStretch(1)
        blay.addLayout(backup_row)
        fallback = QLabel(
            "If reload is unavailable or incomplete, restart QGIS to load the "
            "approved or restored files.")
        fallback.setWordWrap(True)
        fallback.setStyleSheet("color: palette(mid); font-size: 11px;")
        blay.addWidget(fallback)
        outer.addWidget(backups)
        outer.addStretch(1)
        scroll.setWidget(page)
        return scroll

    # ==================================================================
    # General settings
    # ==================================================================
    @staticmethod
    def _model_combo():
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        return combo

    def _update_backend_rows(self):
        self._update_cli_row()
        self._populate_model_combos()

    def _update_cli_row(self):
        is_codex = self.backend.currentData() == "codex"
        self.cli_label.setText(
            "Codex CLI path" if is_codex else "Claude CLI path")
        detected = config.detect_codex() if is_codex else config.detect_claude()
        self.cli_path.setText(detected)
        self._refresh_cli_status()

    def _refresh_cli_status(self):
        self.cli_status.setVisible(not bool(self.cli_path.text().strip()))

    def _populate_model_combos(self):
        models = config.model_ids(self.backend.currentData())
        for combo in (self.model_sup, self.model_worker, self.model_light):
            current = combo.currentText()
            combo.clear()
            combo.addItems(models)
            if current:
                combo.setCurrentText(current)

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
        self.model_sup.setCurrentText(config.get(config.K_MODEL_SUPERVISOR))
        self.model_worker.setCurrentText(config.get(config.K_MODEL_WORKER))
        self.model_light.setCurrentText(config.get(config.K_MODEL_LIGHT))
        pidx = self.perm.findData(config.get(config.K_PERMISSION_MODE))
        self.perm.setCurrentIndex(max(0, pidx))
        self.verifier.setChecked(config.get(config.K_VERIFIER))
        self.timeout.setValue(config.get(config.K_EXEC_TIMEOUT))
        self.max_result.setValue(config.get(config.K_MAX_RESULT))
        self.reduce_motion.setChecked(config.get(config.K_REDUCE_MOTION))

    def _save_and_accept(self):
        if self._long_operation_running():
            self._append_progress(
                "Finish or cancel the active Doctor operation before closing.\n")
            self.tabs.setCurrentIndex(1)
            return
        backend_kind = self.backend.currentData()
        config.set(config.K_BACKEND, backend_kind)
        if backend_kind == "codex":
            config.set(config.K_CODEX_PATH, self.cli_path.text().strip())
        else:
            config.set(config.K_CLAUDE_PATH, self.cli_path.text().strip())
        config.set(
            config.K_MODEL_SUPERVISOR,
            self.model_sup.currentText().strip() or "sonnet")
        config.set(
            config.K_MODEL_WORKER,
            self.model_worker.currentText().strip() or "sonnet")
        config.set(
            config.K_MODEL_LIGHT,
            self.model_light.currentText().strip() or "haiku")
        config.set(config.K_PERMISSION_MODE, self.perm.currentData())
        config.set(config.K_VERIFIER, self.verifier.isChecked())
        config.set(config.K_EXEC_TIMEOUT, self.timeout.value())
        config.set(config.K_MAX_RESULT, self.max_result.value())
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
                           self.propose_btn, self.repair_model,
                           self.restore_btn):
                widget.setEnabled(False)
            return
        self.doctor_service = DoctorService(self.doctor_context)
        self.repair_backend = RepairBackend(self)
        self.repair_backend.progress.connect(self._append_progress)
        self.repair_backend.proposal_ready.connect(self._on_proposal_ready)
        self.repair_backend.failed.connect(self._on_repair_failed)
        self.repair_backend.cancelled.connect(self._on_repair_cancelled)
        self.repair_backend.busy_changed.connect(self._on_repair_busy)
        self._refresh_repair_models()
        self._refresh_backups()
        self._update_doctor_controls()

    def _refresh_repair_models(self):
        self.repair_model.clear()
        cli_paths = self.doctor_context.get("cli_paths")
        cli_paths = cli_paths() if callable(cli_paths) else cli_paths
        for spec in repair_model_options(cli_paths):
            self.repair_model.addItem(spec["label"], spec)

    def _refresh_backups(self):
        self.backup_list.clear()
        if self.doctor_service is None:
            return
        for path in self.doctor_service.backups():
            item = QListWidgetItem(path.name)
            item.setData(Qt.UserRole, str(path))
            self.backup_list.addItem(item)

    def set_chat_busy(self, busy):
        self._chat_busy = bool(busy)
        self._update_doctor_controls()

    def _update_doctor_controls(self):
        available = self.doctor_service is not None
        diag_running = bool(
            self._diag_worker is not None and self._diag_worker.isRunning())
        action_running = bool(
            self._action_worker is not None and self._action_worker.isRunning())
        self.run_diag_btn.setEnabled(
            available and not diag_running and not action_running)
        failed_repairable = bool(
            self._last_report and any(
                not item.get("ok") and item.get("repairable")
                for item in self._last_report.get("checks", [])))
        self.auto_repair_btn.setEnabled(
            available and failed_repairable and not diag_running
            and not self._repair_busy and not action_running)
        can_propose = (
            available and self.repair_model.count() > 0
            and not self._chat_busy and not self._repair_busy
            and not diag_running and not action_running
            and self._current_proposal is None)
        self.propose_btn.setEnabled(can_propose)
        self.cancel_repair_btn.setEnabled(self._repair_busy)
        review = (self._current_proposal is not None and not self._repair_busy
                  and not action_running)
        self.approve_btn.setEnabled(review)
        self.deny_btn.setEnabled(review)
        self.restore_btn.setEnabled(
            available and self.backup_list.currentItem() is not None
            and not self._repair_busy and not diag_running and not action_running)

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
        pending = self._pending_repair
        self._pending_repair = False
        self._update_doctor_controls()
        if rerun:
            self._run_diagnostics()
        elif pending and self._last_report is not None:
            self._start_repair()

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

    def _start_repair(self):
        if self.repair_backend is None or self._chat_busy or self._repair_busy:
            return
        if self._last_report is None:
            self._pending_repair = True
            self._run_diagnostics()
            return
        spec = self.repair_model.currentData()
        if not spec:
            self._append_progress("No installed repair CLI/model is available.\n")
            return
        self._current_proposal = None
        self.proposal_output.clear()
        self.repair_progress.clear()
        self._update_doctor_controls()
        source_repo = self.doctor_context.get("source_repo", "")
        source_repo = source_repo() if callable(source_repo) else source_repo
        self.repair_backend.start(
            spec,
            self._last_report.get("bundle") or "",
            self.error_description.toPlainText(),
            self.doctor_context.get("plugin_dir"),
            source_repo,
            self.doctor_context.get("profile_dir"),
        )

    def _append_progress(self, text):
        cursor = self.repair_progress.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(str(text))
        self.repair_progress.setTextCursor(cursor)
        self.repair_progress.ensureCursorVisible()

    def _on_repair_busy(self, busy):
        self._repair_busy = bool(busy)
        self._update_doctor_controls()

    def _on_proposal_ready(self, proposal):
        self._current_proposal = proposal
        text = (
            "AGENT EXPLANATION\n"
            "=================\n"
            f"{proposal.get('explanation') or '(none)'}\n\n"
            "UNIFIED DIFF\n"
            "============\n"
            f"{proposal.get('diff') or '(none)'}")
        self.proposal_output.setPlainText(text)
        self._update_doctor_controls()

    def _on_repair_failed(self, message):
        self._append_progress(f"\nRepair failed:\n{message}\n")
        self._current_proposal = None
        self._update_doctor_controls()

    def _on_repair_cancelled(self):
        self._append_progress("\nRepair cancelled; disposable copy removed.\n")
        self._current_proposal = None
        self._update_doctor_controls()

    def _cancel_repair(self):
        if self.repair_backend is not None:
            self.repair_backend.cancel()

    def _approve_proposal(self):
        if self.repair_backend is None or self._current_proposal is None:
            return
        try:
            workspace = self.repair_backend.take_current_for_approval()
        except Exception as exc:
            self._append_progress(
                f"\nApply failed; pre-state restored: {type(exc).__name__}: {exc}\n")
            return
        self._current_proposal = None
        self._append_progress("\nApplying approved diff and creating backupâ€¦\n")

        def apply_workspace():
            try:
                return workspace.apply()
            finally:
                workspace.deny()

        self._start_doctor_action("apply", apply_workspace)

    def _deny_proposal(self):
        if self.repair_backend is not None:
            self.repair_backend.deny_current()
        self._current_proposal = None
        self.proposal_output.clear()
        self._append_progress(
            "\nDenied; disposable copy removed and real trees unchanged.\n")
        self._update_doctor_controls()

    def _restore_backup(self):
        if self.doctor_service is None or self.backup_list.currentItem() is None:
            return
        path = self.backup_list.currentItem().data(Qt.UserRole)
        self._append_progress("\nRestoring and hash-verifying selected backupâ€¦\n")
        self._start_doctor_action(
            "restore", lambda: self.doctor_service.restore_backup(path))

    def _start_doctor_action(self, kind, action):
        if self._action_worker is not None and self._action_worker.isRunning():
            return
        self._action_kind = str(kind)
        worker = DoctorActionWorker(action, self)
        self._action_worker = worker
        worker.completed.connect(self._on_doctor_action_completed)
        worker.failed.connect(self._on_doctor_action_failed)
        worker.finished.connect(self._on_doctor_action_finished)
        worker.start()
        self._update_doctor_controls()

    def _on_doctor_action_completed(self, result):
        if self._action_kind == "apply":
            heading = "\nApproved and applied with backup:\n"
        else:
            heading = "\nBackup restored and hash-verified:\n"
        self._append_progress(heading + json.dumps(result, indent=2) + "\n")
        self.reload_btn.setEnabled(True)
        self._refresh_backups()

    def _on_doctor_action_failed(self, message):
        if self._action_kind == "apply":
            self._append_progress(
                f"\nApply failed; pre-state restored: {message}\n")
        else:
            self._append_progress(f"\nRestore failed: {message}\n")

    def _on_doctor_action_finished(self):
        worker = self._action_worker
        self._action_worker = None
        self._action_kind = ""
        if worker is not None:
            worker.deleteLater()
        self._update_doctor_controls()

    def _reload_plugin(self):
        callback = self.doctor_context.get("reload_plugin")
        if callable(callback):
            callback()
            self.accept()
        else:
            self._append_progress(
                "\nReload callback unavailable; restart QGIS to load changes.\n")

    def _long_operation_running(self):
        return (self._repair_busy
                or (self._diag_worker is not None
                    and self._diag_worker.isRunning())
                or (self._action_worker is not None
                    and self._action_worker.isRunning()))

    def reject(self):
        if self._long_operation_running():
            self.tabs.setCurrentIndex(1)
            self._append_progress(
                "Finish diagnostics or use Cancel for the repair before closing.\n")
            return
        super().reject()

    def closeEvent(self, event):
        if self._long_operation_running():
            self.tabs.setCurrentIndex(1)
            self._append_progress(
                "Finish diagnostics or use Cancel for the repair before closing.\n")
            event.ignore()
            return
        super().closeEvent(event)

    def shutdown(self):
        if self.repair_backend is not None:
            self.repair_backend.shutdown()
