# -*- coding: utf-8 -*-
"""Settings dialog for QGent."""
import os

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QComboBox, QLineEdit, QPushButton,
    QSpinBox, QCheckBox, QDialogButtonBox, QHBoxLayout, QLabel, QFileDialog,
    QGroupBox,
)

from .. import config


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("QGent — Settings")
        self.setMinimumWidth(460)
        self._build()
        self._load()

    def _build(self):
        outer = QVBoxLayout(self)

        # backend group
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
        self.cli_status = QLabel("CLI not found — install it or choose its executable.")
        self.cli_status.setWordWrap(True)
        self.cli_status.setStyleSheet("color: #b26a00; font-size: 11px;")
        form.addRow("", self.cli_status)
        self.cli_path.textChanged.connect(self._refresh_cli_status)
        outer.addWidget(gb)

        # models group
        gm = QGroupBox("Models")
        mform = QFormLayout(gm)
        self.model_sup = self._model_combo()
        self.model_worker = self._model_combo()
        self.model_light = self._model_combo()
        mform.addRow("Supervisor / geoprocessor / cartographer", self.model_sup)
        mform.addRow("Worker (override)", self.model_worker)
        mform.addRow("Light (data-scout, qa-verifier)", self.model_light)
        note = QLabel("Choose a model offered for the selected backend, or type a "
                      "custom model id. Existing custom values remain valid.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid); font-size: 11px;")
        mform.addRow(note)
        outer.addWidget(gm)
        self.backend.currentIndexChanged.connect(self._update_backend_rows)

        # safety group
        gs = QGroupBox("Safety & execution")
        sform = QFormLayout(gs)
        self.perm = QComboBox()
        self.perm.addItem("Ask before destructive ops (recommended)", "ask_destructive")
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

        # appearance group
        ga = QGroupBox("Appearance")
        aform = QFormLayout(ga)
        self.reduce_motion = QCheckBox("Reduce motion (disable animations)")
        aform.addRow("Motion", self.reduce_motion)
        outer.addWidget(ga)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

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
        self.cli_label.setText("Codex CLI path" if is_codex else "Claude CLI path")
        detected = (config.detect_codex() if is_codex else config.detect_claude())
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
        path, _ = QFileDialog.getOpenFileName(self, "Select CLI executable", start)
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
        backend_kind = self.backend.currentData()
        config.set(config.K_BACKEND, backend_kind)
        if backend_kind == "codex":
            config.set(config.K_CODEX_PATH, self.cli_path.text().strip())
        else:
            config.set(config.K_CLAUDE_PATH, self.cli_path.text().strip())
        config.set(config.K_MODEL_SUPERVISOR,
                   self.model_sup.currentText().strip() or "sonnet")
        config.set(config.K_MODEL_WORKER,
                   self.model_worker.currentText().strip() or "sonnet")
        config.set(config.K_MODEL_LIGHT,
                   self.model_light.currentText().strip() or "haiku")
        config.set(config.K_PERMISSION_MODE, self.perm.currentData())
        config.set(config.K_VERIFIER, self.verifier.isChecked())
        config.set(config.K_EXEC_TIMEOUT, self.timeout.value())
        config.set(config.K_MAX_RESULT, self.max_result.value())
        config.set(config.K_REDUCE_MOTION, self.reduce_motion.isChecked())
        self.accept()
