# -*- coding: utf-8 -*-
"""Settings dialog for QGIS Copilot."""
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
        self.setWindowTitle("QGIS Copilot — Settings")
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
        self.backend.currentIndexChanged.connect(self._update_cli_row)
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
        outer.addWidget(gb)

        # models group
        gm = QGroupBox("Models")
        mform = QFormLayout(gm)
        self.model_sup = QLineEdit()
        self.model_worker = QLineEdit()
        self.model_light = QLineEdit()
        mform.addRow("Supervisor / geoprocessor / cartographer", self.model_sup)
        mform.addRow("Worker (override)", self.model_worker)
        mform.addRow("Light (data-scout, qa-verifier)", self.model_light)
        note = QLabel("Aliases like <i>sonnet</i>, <i>haiku</i>, <i>opus</i> or full "
                      "model ids. Subagent models come from their .md frontmatter; "
                      "these apply to the main session.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid); font-size: 11px;")
        mform.addRow(note)
        outer.addWidget(gm)

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

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _update_cli_row(self):
        is_codex = self.backend.currentData() == "codex"
        self.cli_label.setText("Codex CLI path" if is_codex else "Claude CLI path")
        detected = (config.detect_codex() if is_codex else config.detect_claude())
        if not self.cli_path.text():
            self.cli_path.setText(detected)

    def _browse_cli(self):
        start = self.cli_path.text() or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, "Select CLI executable", start)
        if path:
            self.cli_path.setText(path)

    def _load(self):
        idx = self.backend.findData(config.get(config.K_BACKEND))
        self.backend.setCurrentIndex(max(0, idx))
        if config.get(config.K_BACKEND) == "codex":
            self.cli_path.setText(config.get(config.K_CODEX_PATH) or config.detect_codex())
        else:
            self.cli_path.setText(config.get(config.K_CLAUDE_PATH) or config.detect_claude())
        self._update_cli_row()

        self.model_sup.setText(config.get(config.K_MODEL_SUPERVISOR))
        self.model_worker.setText(config.get(config.K_MODEL_WORKER))
        self.model_light.setText(config.get(config.K_MODEL_LIGHT))

        pidx = self.perm.findData(config.get(config.K_PERMISSION_MODE))
        self.perm.setCurrentIndex(max(0, pidx))
        self.verifier.setChecked(config.get(config.K_VERIFIER))
        self.timeout.setValue(config.get(config.K_EXEC_TIMEOUT))
        self.max_result.setValue(config.get(config.K_MAX_RESULT))

    def _save_and_accept(self):
        backend_kind = self.backend.currentData()
        config.set(config.K_BACKEND, backend_kind)
        if backend_kind == "codex":
            config.set(config.K_CODEX_PATH, self.cli_path.text().strip())
        else:
            config.set(config.K_CLAUDE_PATH, self.cli_path.text().strip())
        config.set(config.K_MODEL_SUPERVISOR, self.model_sup.text().strip() or "sonnet")
        config.set(config.K_MODEL_WORKER, self.model_worker.text().strip() or "sonnet")
        config.set(config.K_MODEL_LIGHT, self.model_light.text().strip() or "haiku")
        config.set(config.K_PERMISSION_MODE, self.perm.currentData())
        config.set(config.K_VERIFIER, self.verifier.isChecked())
        config.set(config.K_EXEC_TIMEOUT, self.timeout.value())
        config.set(config.K_MAX_RESULT, self.max_result.value())
        self.accept()
