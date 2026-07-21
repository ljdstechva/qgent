# -*- coding: utf-8 -*-
"""QGent visual theme: design tokens + stylesheet builder.

Everything derives from two accent colors (teal → indigo) plus the live Qt
palette, so the panel reads correctly in both light and dark QGIS themes.
Widgets opt in via ``objectName`` selectors; dynamic states use Qt properties
(``prop="..."``) with repolish.

Theme is sampled when the dock is built; switching the QGIS theme mid-session
requires reopening the panel (documented limitation).
"""
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QApplication

ACCENT = "#12A594"       # teal
ACCENT_2 = "#5B5BD6"     # indigo
DANGER = "#E5484D"
WARN = "#F5A623"
OK = "#30A46C"


def is_dark():
    pal = QApplication.instance().palette()
    return pal.window().color().lightness() < 128


class Tokens:
    """Resolved color tokens for the current palette."""

    def __init__(self):
        dark = is_dark()
        self.dark = dark
        self.accent = ACCENT
        self.accent2 = ACCENT_2
        self.danger = DANGER
        self.warn = WARN
        self.ok = OK
        if dark:
            self.bg = "#1B1D21"
            self.surface = "#24272C"          # cards / composer
            self.surface_hi = "#2C3037"       # hover
            self.border = "#3A3F47"
            self.text = "#E8EAED"
            self.text_muted = "#9AA0A8"
            self.user_bubble = "#1E3A36"      # teal-tinted
            self.user_border = "#2A544D"
            self.rail = ACCENT
            self.chip_bg = "#24272C"
            self.warn_bg = "#3A2E1B"
        else:
            self.bg = "#FAFBFC"
            self.surface = "#FFFFFF"
            self.surface_hi = "#F0F2F5"
            self.border = "#DFE3E8"
            self.text = "#1F2328"
            self.text_muted = "#6B7280"
            self.user_bubble = "#E4F5F1"
            self.user_border = "#BFE5DD"
            self.rail = ACCENT
            self.chip_bg = "#F4F6F8"
            self.warn_bg = "#FFF7E8"

    # -- helpers ------------------------------------------------------------
    def qcolor(self, hex_str, alpha=255):
        c = QColor(hex_str)
        c.setAlpha(alpha)
        return c


def build_qss(t):
    """Full stylesheet applied to the dock's root widget."""
    return f"""
/* ---- root ---- */
#QgentRoot {{ background: {t.bg}; }}

/* ---- header ---- */
#QgentWordmark {{
    color: {t.accent}; font-size: 15px; font-weight: 700;
    background: transparent;
}}
#QgentSubtitle {{ color: {t.text_muted}; font-size: 10px; background: transparent; }}
#QgentSessionPill {{
    color: {t.text_muted}; font-size: 10px;
    background: {t.chip_bg}; border: 1px solid {t.border};
    border-radius: 9px; padding: 2px 8px;
}}
QToolButton#QgentIconBtn {{
    border: none; border-radius: 6px; padding: 4px 7px;
    color: {t.text_muted}; background: transparent; font-size: 13px;
}}
QToolButton#QgentIconBtn:hover {{ background: {t.surface_hi}; color: {t.text}; }}

/* ---- messages ---- */
#QgentUserBubble {{
    background: {t.user_bubble}; border: 1px solid {t.user_border};
    border-radius: 12px;
}}
#QgentUserBubble QTextBrowser {{ background: transparent; border: none; color: {t.text}; }}
#QgentAssistant {{
    background: transparent;
    border-left: 2px solid {t.rail};
    border-radius: 0px;
}}
#QgentAssistant QTextBrowser {{ background: transparent; border: none; color: {t.text}; }}
#QgentAssistant[error="true"] {{ border-left: 2px solid {t.danger}; }}
QLabel#QgentTimestamp {{ color: {t.text_muted}; font-size: 9px; background: transparent; }}

/* ---- chips ---- */
#QgentToolChip {{
    background: {t.chip_bg}; border: 1px solid {t.border}; border-radius: 10px;
}}
#QgentToolChip QLabel {{ background: transparent; color: {t.text}; font-size: 11px; }}
#QgentToolChip QLabel#QgentChipState {{ font-size: 11px; }}
#QgentToolChip QTextBrowser {{
    background: {t.surface}; border: 1px solid {t.border};
    border-radius: 6px; color: {t.text}; font-size: 11px;
}}
#QgentSubagentChip {{
    background: {t.chip_bg}; border: 1px solid {t.border}; border-radius: 10px;
}}
#QgentSubagentChip QLabel {{ background: transparent; color: {t.text}; font-size: 11px; }}
QToolButton#QgentCopyBtn {{
    border: 1px solid {t.border}; border-radius: 5px; padding: 2px 8px;
    color: {t.text_muted}; background: {t.surface}; font-size: 10px;
}}
QToolButton#QgentCopyBtn:hover {{ color: {t.text}; background: {t.surface_hi}; }}

/* ---- selected-layer context ---- */
#QgentContextStrip {{ background: transparent; border: none; }}
QLabel#QgentContextChip, QLabel#QgentMessageTag {{
    color: {t.text_muted}; background: {t.chip_bg};
    border: 1px solid {t.border}; border-radius: 8px;
    padding: 2px 7px; font-size: 9px;
}}
#QgentMessageTags {{ background: transparent; }}

/* ---- approval card ---- */
#QgentApproval {{
    background: {t.warn_bg}; border: 1px solid {t.warn};
    border-left: 3px solid {t.warn}; border-radius: 10px;
}}
#QgentApproval QLabel {{ background: transparent; color: {t.text}; }}
#QgentApproval QTextBrowser {{
    background: {t.surface}; border: 1px solid {t.border};
    border-radius: 6px; color: {t.text};
}}
QPushButton#QgentApprove {{
    background: {t.warn}; color: #1F2328; border: none;
    border-radius: 7px; padding: 6px 14px; font-weight: 600;
}}
QPushButton#QgentApprove:hover {{ background: #FFB84D; }}
QPushButton#QgentDeny {{
    background: transparent; color: {t.text_muted};
    border: 1px solid {t.border}; border-radius: 7px; padding: 6px 14px;
}}
QPushButton#QgentDeny:hover {{ color: {t.danger}; border-color: {t.danger}; }}

/* ---- empty state ---- */
#QgentHero QLabel {{ background: transparent; }}
#QgentGreeting {{ color: {t.text}; font-size: 14px; font-weight: 600; }}
#QgentGreetingSub {{ color: {t.text_muted}; font-size: 11px; }}
QPushButton#QgentSuggestion {{
    background: {t.surface}; color: {t.text};
    border: 1px solid {t.border}; border-radius: 12px;
    padding: 7px 12px; font-size: 11px; text-align: left;
}}
QPushButton#QgentSuggestion:hover {{ border-color: {t.accent}; color: {t.accent}; }}

/* ---- composer ---- */
#QgentQueuePanel {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 10px;
}}
#QgentQueuePanel QWidget {{ background: transparent; }}
#QgentQueueTitle {{ color: {t.text}; font-size: 10px; font-weight: 700; }}
QToolButton#QgentQueueToggle, QToolButton#QgentQueueRowButton {{
    color: {t.text_muted}; background: transparent; border: none;
    border-radius: 4px; padding: 2px 4px;
}}
QToolButton#QgentQueueToggle:hover, QToolButton#QgentQueueRowButton:hover {{
    color: {t.text}; background: {t.surface_hi};
}}
#QgentQueueTask {{
    background: {t.chip_bg}; border: 1px solid {t.border}; border-radius: 7px;
}}
#QgentQueueTask[status="running"] {{ border-left: 3px solid {t.accent}; }}
#QgentQueueTask[status="waiting_approval"] {{ border-left: 3px solid {t.warn}; }}
#QgentQueueTask[status="done"] {{ border-left: 3px solid {t.ok}; }}
#QgentQueueTask[status="failed"] {{ border-left: 3px solid {t.danger}; }}
#QgentQueueText {{ color: {t.text}; font-size: 10px; }}
#QgentQueueState, #QgentQueueElapsed {{ color: {t.text_muted}; font-size: 9px; }}
QPushButton#QgentQueueRun, QPushButton#QgentSendAction {{
    color: #FFFFFF; background: {t.accent}; border: none;
    border-radius: 7px; padding: 6px 10px; font-weight: 600;
}}
QPushButton#QgentQueueRun:hover, QPushButton#QgentSendAction:hover {{
    background: {t.accent2};
}}
QPushButton#QgentQueueSecondary, QPushButton#QgentQueueStop,
QPushButton#QgentQueueAdd, QPushButton#QgentStopCurrent {{
    color: {t.text_muted}; background: transparent; border: 1px solid {t.border};
    border-radius: 7px; padding: 5px 8px;
}}
QPushButton#QgentQueueStop:hover, QPushButton#QgentStopCurrent:hover {{
    color: {t.danger}; border-color: {t.danger};
}}
QPushButton#QgentQueueSecondary:checked {{
    color: {t.warn}; border-color: {t.warn};
}}
QPushButton:disabled {{ color: {t.text_muted}; background: {t.surface_hi}; }}

#QgentComposer {{
    background: {t.surface}; border: 1.5px solid {t.border}; border-radius: 14px;
}}
#QgentComposer[focused="true"] {{ border: 1.5px solid {t.accent}; }}
#QgentComposer QPlainTextEdit {{
    background: transparent; border: none; color: {t.text}; font-size: 12px;
}}

/* ---- activity strip ---- */
#QgentActivity QLabel {{ color: {t.text_muted}; font-size: 10px; background: transparent; }}

/* ---- scroll area ---- */
#QgentScroll {{ background: transparent; border: none; }}
#QgentScroll QWidget#QgentMsgContainer {{ background: transparent; }}
QScrollBar:vertical {{
    background: transparent; width: 8px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {t.border}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {t.text_muted}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""


def repolish(widget):
    """Re-apply the stylesheet after a dynamic property change."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
