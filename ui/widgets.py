# -*- coding: utf-8 -*-
"""Message-list widgets for the chat panel.

All colour choices lean on the widget palette so the panel reads correctly in
both light and dark QGIS themes. Nothing here talks to the agent — the dock
wires signals to these widgets.
"""
import json

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QFrame, QLabel, QVBoxLayout, QHBoxLayout, QToolButton, QTextBrowser,
    QPushButton, QPlainTextEdit, QWidget, QSizePolicy,
)


class MessageBubble(QFrame):
    """A user or assistant message. Assistant text is markdown-rendered."""

    def __init__(self, role, parent=None):
        super().__init__(parent)
        self.role = role
        self._text = ""
        self.setFrameShape(QFrame.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)

        who = QLabel("You" if role == "user" else "Copilot")
        f = who.font()
        f.setBold(True)
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        who.setFont(f)
        who.setStyleSheet("color: palette(mid);")
        lay.addWidget(who)

        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(True)
        self.body.setFrameShape(QFrame.NoFrame)
        self.body.setStyleSheet("background: transparent;")
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lay.addWidget(self.body)

        tint = "rgba(120,150,255,0.10)" if role == "user" else "rgba(120,200,120,0.08)"
        self.setStyleSheet(f"MessageBubble {{ border-radius: 8px; background: {tint}; }}")

    def set_text(self, text):
        self._text = text
        if self.role == "assistant":
            self.body.setMarkdown(text)
        else:
            self.body.setPlainText(text)
        self._autosize()

    def append_text(self, delta):
        self.set_text(self._text + delta)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._autosize()

    def _autosize(self):
        # Grow the browser to fit its content so the outer scroll area owns
        # scrolling, not each bubble. Guard against a not-yet-laid-out width of
        # ~0, which would wrap every character and blow the height up.
        vw = self.body.viewport().width()
        if vw <= 10:
            vw = max(self.width() - 24, 240)
        self.body.document().setTextWidth(vw)
        h = int(self.body.document().size().height()) + 8
        self.body.setMinimumHeight(max(24, h))
        self.body.setMaximumHeight(max(24, h))


class ToolChip(QFrame):
    """Collapsible chip for a single tool call + its (optional) result."""

    def __init__(self, name, args, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "ToolChip { border-radius: 6px; background: rgba(128,128,128,0.10); }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)

        pretty = _pretty_tool_name(name)
        self.toggle = QToolButton()
        self.toggle.setText(f"⚙ {pretty}")
        self.toggle.setCheckable(True)
        self.toggle.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.toggle.setArrowType(Qt.RightArrow)
        self.toggle.toggled.connect(self._on_toggle)
        lay.addWidget(self.toggle)

        self.details = QTextBrowser()
        self.details.setFrameShape(QFrame.NoFrame)
        self.details.setVisible(False)
        self.details.setMaximumHeight(220)
        body = "**Arguments**\n```json\n" + _short_json(args) + "\n```"
        self.details.setMarkdown(body)
        self._args_md = body
        lay.addWidget(self.details)

    def set_result(self, text):
        self._args_md += "\n\n**Result**\n```\n" + _truncate(text, 1500) + "\n```"
        self.details.setMarkdown(self._args_md)

    def _on_toggle(self, checked):
        self.toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self.details.setVisible(checked)


class SubagentChip(QFrame):
    """Status chip for a delegated subagent."""

    _ICONS = {"data-scout": "🔎", "geoprocessor": "🛠", "cartographer": "🗺",
              "qa-verifier": "✅"}

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.name = name
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "SubagentChip { border-radius: 6px; background: rgba(120,160,255,0.12); }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        icon = self._ICONS.get(name, "🤖")
        self.label = QLabel(f"{icon} <b>{name}</b> — working…")
        lay.addWidget(self.label)
        lay.addStretch(1)

    def set_finished(self):
        icon = self._ICONS.get(self.name, "🤖")
        self.label.setText(f"{icon} <b>{self.name}</b> — done")


class ApprovalCard(QFrame):
    """Inline Approve/Deny gate for destructive execute_pyqgis code."""

    decided = pyqtSignal(bool)

    def __init__(self, code, reasons, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "ApprovalCard { border-radius: 8px; border: 1px solid rgba(230,150,60,0.8);"
            " background: rgba(230,150,60,0.10); }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)

        head = QLabel("⚠ <b>Approval required</b> — this code looks destructive:")
        head.setWordWrap(True)
        lay.addWidget(head)

        reason_txt = "\n".join(f"• {r}" for r in reasons) or "• (permission mode: ask always)"
        rlbl = QLabel(reason_txt)
        rlbl.setWordWrap(True)
        rlbl.setStyleSheet("color: palette(mid);")
        lay.addWidget(rlbl)

        viewer = QTextBrowser()
        viewer.setMarkdown("```python\n" + _truncate(code, 4000) + "\n```")
        viewer.setMaximumHeight(200)
        lay.addWidget(viewer)

        row = QHBoxLayout()
        row.addStretch(1)
        self.deny_btn = QPushButton("Deny")
        self.approve_btn = QPushButton("Approve & run")
        self.approve_btn.setDefault(True)
        row.addWidget(self.deny_btn)
        row.addWidget(self.approve_btn)
        lay.addLayout(row)

        self.approve_btn.clicked.connect(lambda: self._decide(True))
        self.deny_btn.clicked.connect(lambda: self._decide(False))

    def _decide(self, approved):
        self.approve_btn.setEnabled(False)
        self.deny_btn.setEnabled(False)
        verdict = "✅ Approved" if approved else "🚫 Denied"
        self.approve_btn.setText(verdict)
        self.decided.emit(approved)


class ChatInput(QPlainTextEdit):
    """Multi-line input: Enter sends, Shift+Enter inserts a newline."""

    send_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("Describe the GIS task…  (Enter to send, Shift+Enter for newline)")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.setMaximumHeight(120)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (
                event.modifiers() & Qt.ShiftModifier):
            self.send_requested.emit()
            return
        super().keyPressEvent(event)


# --- helpers ---------------------------------------------------------------
def _pretty_tool_name(name):
    return name.replace("mcp__qgis__", "").replace("mcp__", "")


def _short_json(obj):
    try:
        return _truncate(json.dumps(obj, indent=2, ensure_ascii=False), 1500)
    except (TypeError, ValueError):
        return _truncate(str(obj), 1500)


def _truncate(text, n):
    text = text if isinstance(text, str) else str(text)
    return text if len(text) <= n else text[:n] + f"\n…[+{len(text) - n} chars]"
