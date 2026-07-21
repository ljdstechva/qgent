# -*- coding: utf-8 -*-
"""QGent message-list widgets.

Visual language (see theme.py):
  * user messages   — right-aligned teal-tinted rounded bubbles (≤ ~85% width)
  * assistant text  — full-width, accent side-rail, no box
  * tool calls      — pill chips with a live spinner → ✓/✕, expandable details
  * subagents       — shimmering chips with elapsed time
  * approvals       — amber cards that slide in and pulse for attention
  * questions       — structured choices that freeze into durable answers

Nothing here talks to the agent — the dock wires signals into these widgets.
"""
import json
import os
import time
from datetime import datetime

from qgis.PyQt.QtCore import Qt, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QColor, QDesktopServices, QPixmap
from qgis.PyQt.QtWidgets import (
    QFrame, QLabel, QVBoxLayout, QHBoxLayout, QToolButton, QTextBrowser,
    QPushButton, QPlainTextEdit, QWidget, QSizePolicy, QApplication,
    QLineEdit,
)

from . import theme
from .animations import (
    fade_in, animate_height, Spinner, ShimmerMixin, motion_enabled,
)

_CARET = "▍"


def _now():
    return datetime.now().strftime("%H:%M")


# ===========================================================================
# Messages
# ===========================================================================
class MessageBubble(QWidget):
    """A user or assistant message.

    The outer widget is a full-width row; the inner frame is the styled
    bubble/rail. Assistant text renders markdown and supports a streaming
    caret; heights are measured off the text document (scroll lives in the
    outer QScrollArea only).
    """

    def __init__(self, role, tokens, parent=None):
        super().__init__(parent)
        self.role = role
        self.t = tokens
        self._text = ""
        self._streaming = False
        self._caret_on = True
        self._tags = ()
        self.tags_wrap = None
        self.tags_layout = None

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self.frame = QFrame()
        inner = QVBoxLayout(self.frame)

        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(True)
        self.body.setFrameShape(QFrame.NoFrame)
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.stamp = QLabel(_now())
        self.stamp.setObjectName("QgentTimestamp")

        if role == "user":
            self.frame.setObjectName("QgentUserBubble")
            inner.setContentsMargins(12, 8, 12, 6)
            row.addStretch(15)           # ≤ ~85% width, right-aligned
            row.addWidget(self.frame, 85)
            self.tags_wrap = QWidget()
            self.tags_wrap.setObjectName("QgentMessageTags")
            self.tags_layout = QHBoxLayout(self.tags_wrap)
            self.tags_layout.setContentsMargins(0, 0, 0, 2)
            self.tags_layout.setSpacing(4)
            self.tags_wrap.hide()
            inner.addWidget(self.tags_wrap)
            stamp_row = QHBoxLayout()
            stamp_row.addStretch(1)
            stamp_row.addWidget(self.stamp)
        else:
            self.frame.setObjectName("QgentAssistant")
            inner.setContentsMargins(10, 2, 2, 2)
            row.addWidget(self.frame, 1)
            stamp_row = QHBoxLayout()
            stamp_row.addWidget(self.stamp)
            stamp_row.addStretch(1)
        inner.addWidget(self.body)
        inner.addLayout(stamp_row)

        # Streaming caret blink (assistant only). Re-render is cheap between
        # deltas because the doc is already laid out.
        self._blink = QTimer(self)
        self._blink.setInterval(500)
        self._blink.timeout.connect(self._toggle_caret)

    # -- content ------------------------------------------------------------
    def set_error(self):
        self.frame.setProperty("error", "true")
        theme.repolish(self.frame)

    def set_streaming(self, on):
        if self.role != "assistant":
            return
        self._streaming = on
        if on and motion_enabled():
            self._blink.start()
        else:
            self._blink.stop()
            self._caret_on = False
        self._render()

    def set_text(self, text):
        self._text = text
        self._render()

    def append_text(self, delta):
        self._text += delta
        self._render()

    def text(self):
        return self._text

    def set_tags(self, tags):
        """Render and freeze the layer-selection tags for this message."""
        if self.role != "user" or self.tags_layout is None:
            return
        self._tags = tuple(str(tag) for tag in (tags or []) if str(tag).strip())
        while self.tags_layout.count():
            item = self.tags_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for tag in self._tags:
            shown = tag if tag.startswith("📄 ") else "📎 " + tag
            pill = QLabel(shown)
            pill.setObjectName("QgentMessageTag")
            pill.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.tags_layout.addWidget(pill)
        self.tags_layout.addStretch(1)
        self.tags_wrap.setVisible(bool(self._tags))

    def tags(self):
        return self._tags

    def set_timestamp(self, text):
        """Set a persisted display timestamp when restoring history."""
        self.stamp.setText(str(text or ""))

    def _toggle_caret(self):
        self._caret_on = not self._caret_on
        self._render()

    def _render(self):
        shown = self._text
        if self._streaming and (self._caret_on or not motion_enabled()):
            shown += _CARET
        if self.role == "assistant":
            self.body.setMarkdown(shown)
        else:
            self.body.setPlainText(shown)
        self._autosize()

    # -- sizing (fixes the clipped-welcome bug) -----------------------------
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._autosize()

    def showEvent(self, event):
        super().showEvent(event)
        # First layout pass may have measured at width 0 — re-measure async.
        QTimer.singleShot(0, self._autosize)

    def _autosize(self):
        vw = self.body.viewport().width()
        if vw <= 10:
            vw = max(self.width() - 40, 220)
        doc = self.body.document()
        doc.setTextWidth(vw)
        h = int(doc.size().height()) + 6
        self.body.setFixedHeight(max(22, h))


# ===========================================================================
# Tool chip
# ===========================================================================
class ToolChip(QFrame):
    """Pill for one tool call: spinner while running → ✓/✕, click to expand."""

    def __init__(self, name, args, tokens, parent=None):
        super().__init__(parent)
        self.t = tokens
        self.setObjectName("QgentToolChip")
        self._expanded = False
        self._raw = _short_json(args)
        self._result_raw = ""
        self._history_static = False
        self._finished = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 5, 10, 5)
        lay.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(7)
        self.spinner = Spinner(tokens.accent)
        head.addWidget(self.spinner)
        self.state_lbl = QLabel("")            # becomes ✓ / ✕
        self.state_lbl.setObjectName("QgentChipState")
        self.state_lbl.hide()
        head.addWidget(self.state_lbl)
        self.name_lbl = QLabel(_pretty_tool_name(name))
        f = self.name_lbl.font()
        f.setBold(True)
        self.name_lbl.setFont(f)
        head.addWidget(self.name_lbl)
        head.addStretch(1)
        self.chevron = QLabel("▸")
        self.chevron.setStyleSheet(f"color: {tokens.text_muted}; background: transparent;")
        head.addWidget(self.chevron)
        lay.addLayout(head)

        # details (collapsed by default, animated reveal)
        self.details_wrap = QWidget()
        dw = QVBoxLayout(self.details_wrap)
        dw.setContentsMargins(0, 0, 0, 0)
        dw.setSpacing(4)
        self.details = QTextBrowser()
        self.details.setMinimumHeight(60)
        self.details.setMaximumHeight(200)
        self.details.setMarkdown("**Arguments**\n```json\n" + self._raw + "\n```")
        dw.addWidget(self.details)
        copy_row = QHBoxLayout()
        copy_row.addStretch(1)
        self.copy_btn = QToolButton()
        self.copy_btn.setObjectName("QgentCopyBtn")
        self.copy_btn.setText("Copy")
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.clicked.connect(self._copy)
        copy_row.addWidget(self.copy_btn)
        dw.addLayout(copy_row)
        self.details_wrap.setMaximumHeight(0)
        self.details_wrap.setVisible(False)
        lay.addWidget(self.details_wrap)

        self.setCursor(Qt.PointingHandCursor)

    # -- results ------------------------------------------------------------
    def set_result(self, text, animate=True):
        self._result_raw = text
        self._finished = True
        ok = not _looks_like_error(text)
        self.spinner.stop()
        self.spinner.hide()
        color = self.t.ok if ok else self.t.danger
        self.state_lbl.setText("✓" if ok else "✕")
        self.state_lbl.setStyleSheet(
            f"color: {color}; font-weight: 700; background: transparent;")
        self.state_lbl.show()
        if animate:
            fade_in(self.state_lbl, 150)
        md = ("**Arguments**\n```json\n" + self._raw + "\n```\n\n"
              "**Result**\n```\n" + _truncate(text, 1500) + "\n```")
        self.details.setMarkdown(md)

    def mark_done_without_result(self):
        """Turn finished but no explicit result routed here."""
        if not self._finished:
            self.set_result("(no result captured)")

    def set_static_result(self, text):
        """Restore a final inert state without leaving a live spinner."""
        self._history_static = True
        self.set_result(text or "(no result captured)", animate=False)

    def is_static_state(self):
        return self._history_static and self._finished and self.spinner.isHidden()

    # -- expand/collapse ----------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._toggle()
        super().mousePressEvent(event)

    def _toggle(self):
        self._expanded = not self._expanded
        self.chevron.setText("▾" if self._expanded else "▸")
        if self._expanded:
            self.details_wrap.setVisible(True)
            target = self.details_wrap.sizeHint().height()
            animate_height(self.details_wrap, 0, min(target, 240))
        else:
            animate_height(self.details_wrap, self.details_wrap.height(), 0,
                           on_done=lambda: self.details_wrap.setVisible(False))

    def _copy(self):
        QApplication.clipboard().setText(
            "ARGS:\n" + self._raw + "\n\nRESULT:\n" + self._result_raw)
        self.copy_btn.setText("Copied ✓")
        QTimer.singleShot(1200, lambda: self.copy_btn.setText("Copy"))


# ===========================================================================
# Subagent chip
# ===========================================================================
class SubagentChip(QFrame, ShimmerMixin):
    _ICONS = {"data-scout": "🔎", "geoprocessor": "🛠", "cartographer": "🗺",
              "qa-verifier": "✅"}

    def __init__(self, name, tokens, parent=None):
        super().__init__(parent)
        self.t = tokens
        self.name = name
        self.setObjectName("QgentSubagentChip")
        self._t0 = time.monotonic()

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 5, 10, 5)
        lay.setSpacing(7)
        icon = self._ICONS.get(name, "🤖")
        self.label = QLabel(f"{icon}  <b>{name}</b> · working…")
        lay.addWidget(self.label)
        lay.addStretch(1)

        self._init_shimmer(tokens.qcolor(tokens.accent))
        self._history_static = False

    def set_finished(self):
        self.stop_shimmer()
        icon = self._ICONS.get(self.name, "🤖")
        elapsed = time.monotonic() - self._t0
        self.label.setText(
            f"{icon}  <b>{self.name}</b> · done "
            f"<span style='color:{self.t.text_muted}'>· {elapsed:.0f}s</span>")

    def set_static(self, status="finished", elapsed_s=None):
        """Restore a terminal subagent chip with no shimmer animation."""
        self.stop_shimmer()
        self._history_static = True
        icon = self._ICONS.get(self.name, "🤖")
        if status == "finished":
            suffix = " · done"
            if elapsed_s is not None:
                try:
                    suffix += f" · {float(elapsed_s):.0f}s"
                except (TypeError, ValueError):
                    pass
        else:
            suffix = " · interrupted"
        self.label.setText(f"{icon}  <b>{self.name}</b>{suffix}")

    def is_static_state(self):
        return self._history_static and self._shimmer_pos is None

    def paintEvent(self, event):
        super().paintEvent(event)
        self._draw_shimmer()


# ===========================================================================
# Approval card
# ===========================================================================
class ApprovalCard(QFrame):
    decided = pyqtSignal(bool)

    def __init__(self, code, reasons, tokens, parent=None):
        super().__init__(parent)
        self.t = tokens
        self.setObjectName("QgentApproval")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        head = QLabel("⚠ <b>Approval required</b> — this code looks destructive:")
        head.setWordWrap(True)
        self.head = head
        lay.addWidget(head)

        reason_txt = "\n".join(f"•  {r}" for r in reasons) \
            or "•  (permission mode: ask always)"
        rlbl = QLabel(reason_txt)
        rlbl.setWordWrap(True)
        rlbl.setStyleSheet(f"color: {tokens.text_muted}; background: transparent;")
        lay.addWidget(rlbl)

        viewer = QTextBrowser()
        viewer.setMarkdown("```python\n" + _truncate(code, 4000) + "\n```")
        viewer.setMaximumHeight(190)
        lay.addWidget(viewer)

        row = QHBoxLayout()
        row.addStretch(1)
        self.deny_btn = QPushButton("Deny")
        self.deny_btn.setObjectName("QgentDeny")
        self.deny_btn.setCursor(Qt.PointingHandCursor)
        self.approve_btn = QPushButton("Approve & run")
        self.approve_btn.setObjectName("QgentApprove")
        self.approve_btn.setCursor(Qt.PointingHandCursor)
        self.approve_btn.setDefault(True)
        row.addWidget(self.deny_btn)
        row.addWidget(self.approve_btn)
        lay.addLayout(row)

        self.approve_btn.clicked.connect(lambda: self._decide(True))
        self.deny_btn.clicked.connect(lambda: self._decide(False))
        self._history_static = False
        self._static_note = QLabel("")
        self._static_note.setWordWrap(True)
        self._static_note.setStyleSheet(
            f"color: {tokens.text_muted}; background: transparent;")
        self._static_note.hide()
        lay.addWidget(self._static_note)

    def attention(self):
        """Border pulse — call after the card is added and visible."""
        from .animations import pulse_border
        t = self.t

        def qss(alpha):
            c = QColor(t.warn)
            rgba = f"rgba({c.red()},{c.green()},{c.blue()},{alpha:.2f})"
            return (f"#QgentApproval {{ background: {t.warn_bg};"
                    f" border: 1px solid {rgba}; border-left: 3px solid {rgba};"
                    f" border-radius: 10px; }}")
        pulse_border(self, qss, t.warn)

    def _decide(self, approved):
        self.approve_btn.setEnabled(False)
        self.deny_btn.setEnabled(False)
        self.approve_btn.setText("✅ Approved" if approved else "🚫 Denied")
        self.decided.emit(approved)

    def set_static_outcome(self, approved=None):
        """Render a restored approval as an inert outcome note."""
        self._history_static = True
        self.approve_btn.setEnabled(False)
        self.deny_btn.setEnabled(False)
        self.approve_btn.hide()
        self.deny_btn.hide()
        if approved is True:
            text = "✅ Approved · restored from chat history"
            self.head.setText("✅ <b>Approved code action</b>")
        elif approved is False:
            text = "🚫 Denied · restored from chat history"
            self.head.setText("🚫 <b>Denied code action</b>")
        else:
            text = "⚠ Approval not completed before the previous session ended"
            self.head.setText("⚠ <b>Incomplete code approval</b>")
        self._static_note.setText(text)
        self._static_note.show()

    def set_cancelled(self, reason="approval was cancelled"):
        """Freeze a live card after its bridge wait has already ended."""
        self._history_static = True
        self.approve_btn.setEnabled(False)
        self.deny_btn.setEnabled(False)
        self.approve_btn.hide()
        self.deny_btn.hide()
        self.head.setText("&#9940; <b>Approval cancelled</b>")
        self._static_note.setText(str(reason or "approval was cancelled"))
        self._static_note.show()

    def is_static_state(self):
        return (self._history_static and not self.approve_btn.isVisible()
                and not self.deny_btn.isVisible())


# ===========================================================================
# Structured clarifying question
# ===========================================================================
class QuestionCard(QFrame):
    """Two-to-five concrete choices with an optional inline Other answer."""

    answered = pyqtSignal(str, str)  # answer, answer_kind (option | other)

    def __init__(self, question, options, allow_other, tokens, parent=None):
        super().__init__(parent)
        self.t = tokens
        self.question = str(question or "").strip()
        self.options = tuple(str(option or "").strip() for option in options)
        self.allow_other = bool(allow_other)
        self._terminal = False
        self._outcome = "pending"
        self._answer = ""
        self._answer_kind = ""
        self._option_buttons = {}
        self.setObjectName("QgentQuestion")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)

        self.head = QLabel("Clarification needed")
        self.head.setObjectName("QgentQuestionHead")
        layout.addWidget(self.head)
        self.question_label = QLabel(self.question)
        self.question_label.setObjectName("QgentQuestionText")
        self.question_label.setTextFormat(Qt.PlainText)
        self.question_label.setWordWrap(True)
        layout.addWidget(self.question_label)

        for option in self.options:
            button = QPushButton(option)
            button.setObjectName("QgentQuestionOption")
            button.setProperty("chosen", "false")
            button.setProperty("muted", "false")
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(
                lambda _checked=False, value=option:
                self._request_answer(value, "option"))
            self._option_buttons[option] = button
            layout.addWidget(button)

        self.other_btn = None
        self.other_row = None
        self.other_input = None
        self.other_submit = None
        if self.allow_other:
            self.other_btn = QPushButton("Other…")
            self.other_btn.setObjectName("QgentQuestionOption")
            self.other_btn.setProperty("chosen", "false")
            self.other_btn.setProperty("muted", "false")
            self.other_btn.setCursor(Qt.PointingHandCursor)
            self.other_btn.clicked.connect(self._show_other)
            layout.addWidget(self.other_btn)

            self.other_row = QWidget()
            self.other_row.setObjectName("QgentQuestionOtherRow")
            row = QHBoxLayout(self.other_row)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            self.other_input = QLineEdit()
            self.other_input.setObjectName("QgentQuestionOtherInput")
            self.other_input.setPlaceholderText("Type a short answer")
            self.other_submit = QPushButton("Submit")
            self.other_submit.setObjectName("QgentQuestionSubmit")
            self.other_submit.setCursor(Qt.PointingHandCursor)
            row.addWidget(self.other_input, 1)
            row.addWidget(self.other_submit)
            self.other_submit.clicked.connect(self._submit_other)
            self.other_input.returnPressed.connect(self._submit_other)
            self.other_row.hide()
            layout.addWidget(self.other_row)

        self.answer_note = QLabel("")
        self.answer_note.setObjectName("QgentQuestionAnswer")
        self.answer_note.setTextFormat(Qt.PlainText)
        self.answer_note.setWordWrap(True)
        self.answer_note.hide()
        layout.addWidget(self.answer_note)

    def _show_other(self):
        if self._terminal or self.other_row is None:
            return
        self.other_row.show()
        self.other_input.setFocus()

    def _submit_other(self):
        if self._terminal or self.other_input is None:
            return
        answer = self.other_input.text().strip()
        if answer:
            self._request_answer(answer, "other")

    def _request_answer(self, answer, answer_kind):
        if not self._terminal:
            self.answered.emit(str(answer), str(answer_kind))

    def set_answer(self, answer, answer_kind="option", restored=False):
        self.set_terminal(
            "answered", answer=answer, answer_kind=answer_kind,
            restored=restored)

    def set_timeout(self, restored=False):
        self.set_terminal("timeout", restored=restored)

    def set_cancelled(self, reason="Question cancelled", restored=False):
        self.set_terminal("cancelled", reason=reason, restored=restored)

    def set_pending_static(self, restored=True):
        self.set_terminal("pending", restored=restored)

    def set_terminal(self, outcome, answer="", answer_kind="", reason="",
                     restored=False):
        """Freeze the card into one inert terminal or restored state."""
        outcome = str(outcome or "pending")
        answer = str(answer or "")
        answer_kind = str(answer_kind or "")
        self._terminal = True
        self._outcome = outcome
        self._answer = answer
        self._answer_kind = answer_kind

        chosen = None
        if outcome == "answered" and answer_kind == "option":
            chosen = self._option_buttons.get(answer)
        elif outcome == "answered" and answer_kind == "other":
            chosen = self.other_btn

        buttons = list(self._option_buttons.values())
        if self.other_btn is not None:
            buttons.append(self.other_btn)
        for button in buttons:
            button.setEnabled(False)
            button.setProperty("chosen", "true" if button is chosen else "false")
            button.setProperty("muted", "false" if button is chosen else "true")
            theme.repolish(button)

        if self.other_input is not None:
            if outcome == "answered" and answer_kind == "other":
                self.other_input.setText(answer)
                self.other_input.setCursorPosition(0)
                self.other_row.show()
            self.other_input.setEnabled(False)
            self.other_submit.setEnabled(False)

        suffix = " · restored from chat history" if restored else ""
        if outcome == "answered":
            self.head.setText("Clarification answered")
            self.answer_note.setText("Answer: " + answer + suffix)
        elif outcome == "timeout":
            self.head.setText("No answer received")
            self.answer_note.setText(
                "No answer — QGent will make the safest assumption" + suffix)
        elif outcome == "cancelled":
            self.head.setText("Question cancelled")
            self.answer_note.setText(str(reason or "Question cancelled") + suffix)
        else:
            self.head.setText("Incomplete clarification")
            self.answer_note.setText("No answer was recorded" + suffix)
        self.answer_note.show()

    def is_static_state(self):
        return self._terminal and all(
            not button.isEnabled() for button in self._option_buttons.values())

    def outcome(self):
        return self._outcome

    def answer(self):
        return self._answer


class StatusNote(QLabel):
    """Subtle inert line for session fallback and restored status events."""

    def __init__(self, text, tokens, parent=None):
        super().__init__(str(text), parent)
        self.setObjectName("QgentHistoryNote")
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(
            f"color: {tokens.text_muted}; background: transparent; "
            "font-size: 10px; padding: 2px 6px;")


class _SnapshotPreview(QLabel):
    """Clickable image surface used by :class:`SnapshotCard`."""

    activated = pyqtSignal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            self.activated.emit()
        super().mouseReleaseEvent(event)


class SnapshotCard(QFrame):
    """Durable map preview with a quiet missing-file fallback."""

    MAX_CARD_WIDTH = 320
    MAX_PREVIEW_HEIGHT = 360

    def __init__(self, path, caption, tokens, parent=None, opener=None):
        super().__init__(parent)
        self.t = tokens
        self.path = os.path.abspath(os.path.normpath(str(path or "")))
        self.caption = str(caption or "Map after this step")
        self._opener = opener or self._open_in_system_viewer
        self._source_pixmap = QPixmap(self.path) if self.path else QPixmap()
        self._missing = self._source_pixmap.isNull()

        self.setObjectName("QgentSnapshotCard")
        self.setProperty("missing", "true" if self._missing else "false")
        self.setMinimumWidth(180)
        self.setMaximumWidth(self.MAX_CARD_WIDTH)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 7, 7, 7)
        layout.setSpacing(5)

        self.preview = _SnapshotPreview()
        self.preview.setObjectName("QgentSnapshotPreview")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        if self._missing:
            self.preview.setText("Map snapshot unavailable")
            self.preview.setEnabled(False)
            self.preview.setMinimumHeight(92)
        else:
            self.preview.setToolTip("Open the full-size map snapshot")
            self.preview.setCursor(Qt.PointingHandCursor)
            self.preview.activated.connect(self.open_full_size)
        layout.addWidget(self.preview)

        self.caption_label = QLabel(self.caption)
        self.caption_label.setObjectName("QgentSnapshotCaption")
        self.caption_label.setWordWrap(True)
        layout.addWidget(self.caption_label)
        QTimer.singleShot(0, self._layout_preview)

    def is_placeholder(self):
        return self._missing

    def open_full_size(self):
        """Open the durable PNG; failures remain quiet inside the card."""
        if self._missing or not os.path.isfile(self.path):
            return False
        try:
            return bool(self._opener(self.path))
        except Exception:
            return False

    @staticmethod
    def _open_in_system_viewer(path):
        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_preview()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._layout_preview)

    def _layout_preview(self):
        available = max(120, min(
            self.MAX_CARD_WIDTH - 14, self.width() - 14))
        if self._missing:
            self.preview.setMinimumWidth(available)
            return
        shown = self._source_pixmap.scaled(
            available, self.MAX_PREVIEW_HEIGHT,
            Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.preview.setPixmap(shown)
        self.preview.setFixedSize(available, shown.height())


# ===========================================================================
# Selected-layer context
# ===========================================================================
class ContextChip(QLabel):
    """One compact live-selection chip."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setObjectName("QgentContextChip")
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)


class AttachmentChip(QFrame):
    """One removable, in-place file attachment in the live context strip."""

    remove_requested = pyqtSignal(str)

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.item = dict(item or {})
        self.path = str(self.item.get("path") or "")
        self.setObjectName("QgentAttachmentChip")
        self.setProperty(
            "warning", "true" if self.item.get("warning") else "false")
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 1, 2, 1)
        row.setSpacing(2)
        self.label = QLabel(ContextStrip._item_label(self.item))
        self.label.setObjectName("QgentAttachmentLabel")
        row.addWidget(self.label)
        self.remove_btn = QToolButton()
        self.remove_btn.setObjectName("QgentAttachmentRemove")
        self.remove_btn.setText("×")
        self.remove_btn.setToolTip(
            "Remove this file from the next request")
        self.remove_btn.setCursor(Qt.PointingHandCursor)
        self.remove_btn.clicked.connect(
            lambda _checked=False: self.remove_requested.emit(self.path))
        row.addWidget(self.remove_btn)

        details = [self.path]
        kind = str(self.item.get("file_kind") or "file")
        try:
            details.append(f"{kind} · {int(self.item.get('size') or 0)} bytes")
        except (TypeError, ValueError):
            details.append(kind)
        warning = str(self.item.get("warning") or "").strip()
        if warning:
            details.append("Warning: " + warning)
        tooltip = "\n".join(value for value in details if value)
        self.setToolTip(tooltip)
        self.label.setToolTip(tooltip)


class ContextStrip(QFrame):
    """Live selected-node and pending-file strip above the composer."""

    remove_requested = pyqtSignal(str)

    MAX_VISIBLE = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("QgentContextStrip")
        self._items = ()
        self._labels = ()
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(2, 0, 2, 0)
        self._layout.setSpacing(4)
        self.hide()

    def set_items(self, items):
        self._items = tuple(dict(item) for item in (items or []))
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        selected = [item for item in self._items
                    if item.get("kind") != "attachment"]
        attachments = [item for item in self._items
                       if item.get("kind") == "attachment"]
        visible_items = list(selected[:self.MAX_VISIBLE])
        if len(selected) > self.MAX_VISIBLE:
            visible_items.append({
                "kind": "summary",
                "name": f"+{len(selected) - self.MAX_VISIBLE} more",
            })
        # Attachments are never hidden behind the selected-layer cap because
        # every pending file must remain individually removable.
        visible_items.extend(attachments)
        self._labels = tuple(self._item_label(item) for item in visible_items)
        for item, label in zip(visible_items, self._labels):
            if item.get("kind") == "attachment":
                chip = AttachmentChip(item)
                chip.remove_requested.connect(self.remove_requested.emit)
            else:
                chip = ContextChip(label)
            self._layout.addWidget(chip)
        self._layout.addStretch(1)
        self.setVisible(bool(visible_items))

    def displayed_labels(self):
        return self._labels

    def items(self):
        return tuple(dict(item) for item in self._items)

    @staticmethod
    def _item_label(item):
        name = str(item.get("name") or "unnamed")
        if item.get("kind") == "attachment":
            prefix = "⚠ " if item.get("warning") else ""
            return f"{prefix}📄 {name}"
        if item.get("kind") == "group":
            count = int(item.get("layer_count") or 0)
            return f"📁 {name} ({count} layers)"
        if item.get("kind") == "summary":
            return name
        return f"🗂 {name}"


# ===========================================================================
# Sequential task queue
# ===========================================================================
class QueueTaskRow(QFrame):
    """One task owned by the dock's sequential queue state machine."""

    move_requested = pyqtSignal(str, int)
    remove_requested = pyqtSignal(str)
    stop_requested = pyqtSignal(str)

    _STATUS = {
        "queued": ("\u23f8", "Queued"),
        "running": ("\u25b6", "Running"),
        "waiting_approval": ("\u26a0", "Waiting for approval"),
        "waiting_question": ("?", "Waiting for answer"),
        "done": ("\u2713", "Done"),
        "failed": ("\u2715", "Failed"),
        "skipped": ("\u21aa", "Skipped"),
    }

    def __init__(self, task, tokens, parent=None):
        super().__init__(parent)
        self.t = tokens
        self.task_id = str(task.get("id") or "")
        self._started_at = None
        self._elapsed_s = None
        self.setObjectName("QgentQueueTask")

        row = QHBoxLayout(self)
        row.setContentsMargins(7, 5, 7, 5)
        row.setSpacing(5)
        self.state_label = QLabel()
        self.state_label.setObjectName("QgentQueueState")
        self.state_label.setFixedWidth(18)
        row.addWidget(self.state_label)
        self.text_label = QLabel()
        self.text_label.setObjectName("QgentQueueText")
        self.text_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        row.addWidget(self.text_label, 1)
        self.elapsed_label = QLabel("")
        self.elapsed_label.setObjectName("QgentQueueElapsed")
        row.addWidget(self.elapsed_label)
        self.tokens_label = QLabel("")
        self.tokens_label.setObjectName("QgentQueueTokens")
        row.addWidget(self.tokens_label)

        self.up_btn = self._button("\u2191", "Move up")
        self.down_btn = self._button("\u2193", "Move down")
        self.remove_btn = self._button("\u2715", "Remove from queue")
        self.stop_btn = self._button("Stop", "Stop this task")
        row.addWidget(self.up_btn)
        row.addWidget(self.down_btn)
        row.addWidget(self.remove_btn)
        row.addWidget(self.stop_btn)
        self.up_btn.clicked.connect(
            lambda: self.move_requested.emit(self.task_id, -1))
        self.down_btn.clicked.connect(
            lambda: self.move_requested.emit(self.task_id, 1))
        self.remove_btn.clicked.connect(
            lambda: self.remove_requested.emit(self.task_id))
        self.stop_btn.clicked.connect(
            lambda: self.stop_requested.emit(self.task_id))

        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._update_elapsed)
        self.update_task(task)

    def _button(self, text, tooltip):
        button = QToolButton(self)
        button.setObjectName("QgentQueueRowButton")
        button.setText(text)
        button.setToolTip(tooltip)
        button.setCursor(Qt.PointingHandCursor)
        return button

    def update_task(self, task):
        text = " ".join(str(task.get("text") or "").split())
        self.text_label.setText(_truncate(text, 60))
        self.text_label.setToolTip(str(task.get("text") or ""))
        status = str(task.get("status") or "queued")
        icon, label = self._STATUS.get(status, ("?", status.title()))
        self.state_label.setText(icon)
        error = str(task.get("error") or "")
        self.state_label.setToolTip(label + ((" - " + error) if error else ""))
        self.setProperty("status", status)
        theme.repolish(self)

        self._started_at = task.get("started_at")
        self._elapsed_s = task.get("elapsed_s")
        tokens = task.get("tokens")
        if (isinstance(tokens, int) and not isinstance(tokens, bool)
                and tokens >= 0):
            self.tokens_label.setText("{:,} tok".format(tokens))
            self.tokens_label.setToolTip("Reported CLI token usage")
        else:
            self.tokens_label.setText("")
            self.tokens_label.setToolTip("")
        queued = status == "queued"
        running = status in (
            "running", "waiting_approval", "waiting_question")
        self.up_btn.setVisible(queued)
        self.down_btn.setVisible(queued)
        self.remove_btn.setVisible(queued)
        self.stop_btn.setVisible(running)
        if running and self._started_at is not None:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
        self._update_elapsed()

    def set_position(self, can_move_up, can_move_down):
        self.up_btn.setEnabled(bool(can_move_up))
        self.down_btn.setEnabled(bool(can_move_down))

    def _update_elapsed(self):
        elapsed = self._elapsed_s
        if elapsed is None and self._started_at is not None:
            elapsed = max(0.0, time.monotonic() - float(self._started_at))
        self.elapsed_label.setText(_format_elapsed(elapsed) if elapsed is not None else "")


class QueuePanel(QFrame):
    """Collapsible queue controls; task data remains owned by ``ChatDock``."""

    run_requested = pyqtSignal()
    pause_requested = pyqtSignal(bool)
    stop_all_requested = pyqtSignal()
    move_requested = pyqtSignal(str, int)
    remove_requested = pyqtSignal(str)
    stop_requested = pyqtSignal(str)

    def __init__(self, tokens, parent=None):
        super().__init__(parent)
        self.t = tokens
        self._rows = {}
        self._batch_running = False
        self.setObjectName("QgentQueuePanel")
        root = QVBoxLayout(self)
        root.setContentsMargins(7, 6, 7, 7)
        root.setSpacing(5)

        head = QHBoxLayout()
        self.toggle_btn = QToolButton()
        self.toggle_btn.setObjectName("QgentQueueToggle")
        self.toggle_btn.setText("\u25be")
        self.toggle_btn.setCursor(Qt.PointingHandCursor)
        self.title = QLabel("QUEUE")
        self.title.setObjectName("QgentQueueTitle")
        head.addWidget(self.toggle_btn)
        head.addWidget(self.title)
        head.addStretch(1)
        root.addLayout(head)

        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(5)
        self.tasks_widget = QWidget()
        self.tasks_layout = QVBoxLayout(self.tasks_widget)
        self.tasks_layout.setContentsMargins(0, 0, 0, 0)
        self.tasks_layout.setSpacing(3)
        body_layout.addWidget(self.tasks_widget)

        controls = QHBoxLayout()
        self.run_btn = QPushButton("Run queue")
        self.run_btn.setObjectName("QgentQueueRun")
        self.pause_btn = QPushButton("Pause after current")
        self.pause_btn.setObjectName("QgentQueueSecondary")
        self.pause_btn.setCheckable(True)
        self.stop_all_btn = QPushButton("Stop all")
        self.stop_all_btn.setObjectName("QgentQueueStop")
        controls.addWidget(self.run_btn)
        controls.addWidget(self.pause_btn)
        controls.addWidget(self.stop_all_btn)
        body_layout.addLayout(controls)
        root.addWidget(self.body)

        self.toggle_btn.clicked.connect(self._toggle)
        self.run_btn.clicked.connect(self.run_requested)
        self.pause_btn.toggled.connect(self.pause_requested)
        self.stop_all_btn.clicked.connect(self.stop_all_requested)
        self.hide()
        self.set_batch_running(False)

    def set_tasks(self, tasks):
        tasks = list(tasks or [])
        ids = [str(task.get("id") or "") for task in tasks]
        for task in tasks:
            task_id = str(task.get("id") or "")
            row = self._rows.get(task_id)
            if row is None:
                row = QueueTaskRow(task, self.t, self.tasks_widget)
                row.move_requested.connect(self.move_requested)
                row.remove_requested.connect(self.remove_requested)
                row.stop_requested.connect(self.stop_requested)
                self._rows[task_id] = row
            else:
                row.update_task(task)
        for task_id in list(self._rows):
            if task_id not in ids:
                row = self._rows.pop(task_id)
                row.setParent(None)
                row.deleteLater()

        while self.tasks_layout.count():
            self.tasks_layout.takeAt(0)
        for task in tasks:
            self.tasks_layout.addWidget(self._rows[str(task.get("id") or "")])

        queued = [str(task.get("id") or "") for task in tasks
                  if task.get("status") == "queued"]
        for pos, task_id in enumerate(queued):
            self._rows[task_id].set_position(
                pos > 0, pos < len(queued) - 1)
        self.title.setText(f"QUEUE ({len(tasks)})")
        self.setVisible(bool(tasks))
        self.run_btn.setEnabled(bool(queued) and not self._batch_running)

    def set_batch_running(self, running):
        self._batch_running = bool(running)
        if running:
            self.run_btn.setEnabled(False)
        self.pause_btn.setEnabled(bool(running))
        self.stop_all_btn.setEnabled(bool(running))
        if not running:
            self.set_paused(False)

    def set_paused(self, paused):
        self.pause_btn.blockSignals(True)
        self.pause_btn.setChecked(bool(paused))
        self.pause_btn.setText("Resume queue" if paused else "Pause after current")
        self.pause_btn.blockSignals(False)

    def clear(self):
        self.set_tasks([])
        self.set_batch_running(False)

    def _toggle(self):
        visible = not self.body.isVisible()
        self.body.setVisible(visible)
        self.toggle_btn.setText("\u25be" if visible else "\u25b8")


# ===========================================================================
# Composer pieces
# ===========================================================================
class ChatInput(QPlainTextEdit):
    """Auto-growing input: Enter sends, Shift+Enter inserts a newline."""

    send_requested = pyqtSignal()
    focus_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("Ask QGent anything about this project…")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(36)
        self.textChanged.connect(self._grow)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (
                event.modifiers() & Qt.ShiftModifier):
            self.send_requested.emit()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.focus_changed.emit(True)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.focus_changed.emit(False)

    def _grow(self):
        h = int(self.document().size().height()) + 14
        self.setFixedHeight(max(36, min(h, 120)))


class SendStopButton(QPushButton):
    """Circular accent button that morphs between send (➤) and stop (■)."""

    def __init__(self, tokens, parent=None):
        super().__init__(parent)
        self.t = tokens
        self._busy = False
        self.setObjectName("QgentSendAction")
        self.setText("Send")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Send (Enter)")

    def set_busy(self, busy):
        self._busy = bool(busy)
        self.setText("Add to queue" if busy else "Send")
        self.setToolTip(
            "Add this request to the queue" if busy else "Send (Enter)")

    def is_busy_state(self):
        return self._busy


class SuggestionChip(QPushButton):
    """Clickable starter prompt shown in the empty state."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setObjectName("QgentSuggestion")
        self.setCursor(Qt.PointingHandCursor)


# ===========================================================================
# helpers
# ===========================================================================
def _pretty_tool_name(name):
    return name.replace("mcp__qgis__", "").replace("mcp__", "")


def _looks_like_error(text):
    head = (text or "").lstrip()[:40].upper()
    return head.startswith(("ERROR", "DENIED", "TIMEOUT", "COULD NOT REACH"))


def _short_json(obj):
    try:
        return _truncate(json.dumps(obj, indent=2, ensure_ascii=False), 1500)
    except (TypeError, ValueError):
        return _truncate(str(obj), 1500)


def _format_elapsed(value):
    seconds = max(0, int(float(value or 0)))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:d}:{seconds:02d}"


def _truncate(text, n):
    text = text if isinstance(text, str) else str(text)
    return text if len(text) <= n else text[:n] + f"\n…[+{len(text) - n} chars]"
