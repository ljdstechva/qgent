# -*- coding: utf-8 -*-
"""QGent animation toolkit.

All helpers are Qt5/Qt6-safe (via qgis.PyQt), self-contained, and honour the
"reduce motion" setting: when reduced, every helper snaps straight to its end
state so behaviour is identical minus the motion.

Object-lifetime rules (the source of a real crash, keep them):
  * Never combine ``DeleteWhenStopped`` with a stored wrapper that is later
    method-called — Qt deletes the C++ object at finish and the next call
    raises "wrapped C/C++ object has been deleted". Reusable animations
    (scroll, height) are therefore persistent: created once, parented,
    retargeted per call.
  * Every callback that can fire after its widget was destroyed (new session
    clears the message list mid-animation) is guarded with ``_alive``.
"""
import math

from qgis.PyQt.QtCore import (
    Qt, QEasingCurve, QPropertyAnimation, QVariantAnimation, QTimer,
)
from qgis.PyQt.QtGui import QColor, QPainter, QPen
from qgis.PyQt.QtWidgets import QGraphicsOpacityEffect, QWidget

try:
    from qgis.PyQt import sip
except ImportError:  # standalone PyQt5 (tests)
    import sip

from .. import config


def motion_enabled():
    return not config.get(config.K_REDUCE_MOTION)


def _alive(qobj):
    """True if the wrapped C++ object behind ``qobj`` still exists."""
    if qobj is None:
        return False
    try:
        return not sip.isdeleted(qobj)
    except TypeError:  # not a sip-wrapped object
        return True


# ---------------------------------------------------------------------------
# One-shot entrance / reveal
# ---------------------------------------------------------------------------
def fade_in(widget, duration=180):
    """Fade a freshly-added widget from 0 → 1 opacity, then drop the effect.

    The effect is removed on finish because QGraphicsOpacityEffect degrades
    QTextBrowser rendering quality if left installed.
    """
    if not motion_enabled() or not _alive(widget):
        return
    eff = QGraphicsOpacityEffect(widget)
    eff.setOpacity(0.0)
    widget.setGraphicsEffect(eff)
    # Parented to the widget → Qt keeps it alive and destroys it with the
    # widget; no Python-side reference needed.
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)

    def _done():
        if _alive(widget):
            widget.setGraphicsEffect(None)
    anim.finished.connect(_done)
    anim.start(QPropertyAnimation.DeleteWhenStopped)


def animate_height(widget, start, end, duration=150, on_done=None):
    """Animate ``maximumHeight`` (expand/collapse reveals).

    One persistent animation per widget, retargeted on each call, so rapid
    expand/collapse toggling can't stack conflicting animations or touch a
    deleted one.
    """
    if not _alive(widget):
        return
    if not motion_enabled():
        widget.setMaximumHeight(end)
        if on_done:
            on_done()
        return
    anim = getattr(widget, "_qgent_height", None)
    if not _alive(anim):
        anim = QPropertyAnimation(widget, b"maximumHeight", widget)
        widget._qgent_height = anim
    else:
        anim.stop()
        try:
            anim.finished.disconnect()
        except TypeError:
            pass  # nothing connected
    anim.setDuration(duration)
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(QEasingCurve.InOutQuad)
    if on_done:
        def _done():
            if _alive(widget):
                on_done()
        anim.finished.connect(_done)
    anim.start()


def smooth_scroll_to_bottom(scroll_area, duration=120):
    """Animate the vertical scrollbar to max; retargets if already running.

    Uses ONE persistent QPropertyAnimation per scroll area (created lazily,
    parented, KeepWhenStopped). The previous DeleteWhenStopped + stop-the-old
    pattern crashed with "wrapped C/C++ object ... has been deleted" as soon
    as two scrolls were >duration apart.
    """
    if not _alive(scroll_area):
        return
    bar = scroll_area.verticalScrollBar()
    if not motion_enabled():
        bar.setValue(bar.maximum())
        return
    anim = getattr(scroll_area, "_qgent_scroll", None)
    if not _alive(anim):
        anim = QPropertyAnimation(bar, b"value", scroll_area)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        scroll_area._qgent_scroll = anim
    anim.stop()
    anim.setDuration(duration)
    anim.setStartValue(bar.value())
    anim.setEndValue(bar.maximum())
    anim.start()


def pulse_border(widget, base_qss_fn, color_hex, pulses=2, duration=600):
    """Pulse a widget's border opacity by rewriting its stylesheet.

    ``base_qss_fn(alpha_float)`` must return the full stylesheet for the given
    border alpha. One-shot attention grab (approval cards).
    """
    if not motion_enabled():
        return
    anim = QVariantAnimation(widget)
    anim.setDuration(duration * pulses)
    anim.setStartValue(0.0)
    anim.setEndValue(float(pulses))
    anim.setEasingCurve(QEasingCurve.Linear)

    def _tick(v):
        if not _alive(widget):
            anim.stop()
            return
        # triangle wave 1 → 0.35 → 1 per pulse
        phase = abs(math.sin(math.pi * v))
        alpha = 0.35 + 0.65 * phase
        widget.setStyleSheet(base_qss_fn(alpha))

    def _done():
        if _alive(widget):
            widget.setStyleSheet(base_qss_fn(1.0))

    anim.valueChanged.connect(_tick)
    anim.finished.connect(_done)
    anim.start(QVariantAnimation.DeleteWhenStopped)


def staggered(widgets, delay_step=80, duration=180):
    """Fade widgets in one after another (suggestion chips).

    The delayed timer may fire after the widget was destroyed (fast "new
    session") — fade_in's own _alive guard makes that a no-op.
    """
    if not motion_enabled():
        return
    for i, w in enumerate(widgets):
        QTimer.singleShot(i * delay_step, lambda w=w: fade_in(w, duration))


# ---------------------------------------------------------------------------
# Continuous indicators
# ---------------------------------------------------------------------------
class Spinner(QWidget):
    """Small rotating arc; call stop() when the work is done."""

    def __init__(self, color_hex, diameter=13, parent=None):
        super().__init__(parent)
        self._color = QColor(color_hex)
        self._angle = 0
        self.setFixedSize(diameter, diameter)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(800)
        self._anim.setStartValue(0)
        self._anim.setEndValue(360)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._on_tick)
        if motion_enabled():
            self._anim.start()
        else:
            self._angle = 90  # static arc

    def _on_tick(self, v):
        self._angle = int(v)
        self.update()

    def stop(self):
        self._anim.stop()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(self._color, 2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        rect = self.rect().adjusted(2, 2, -2, -2)
        # 100°-long arc starting at the animated angle
        p.drawArc(rect, -self._angle * 16, 100 * 16)
        p.end()


class ThinkingDots(QWidget):
    """Three bouncing dots shown between send and the first token."""

    def __init__(self, color_hex, parent=None):
        super().__init__(parent)
        self._color = QColor(color_hex)
        self._t = 0.0
        self.setFixedSize(34, 14)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(900)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._on_tick)

    def start(self):
        if motion_enabled():
            self._anim.start()
        self.show()

    def halt(self):
        self._anim.stop()
        self.hide()

    def _on_tick(self, v):
        self._t = float(v)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        r = 2.4
        baseline = self.height() / 2 + 2
        for i in range(3):
            phase = (self._t - i * 0.15) % 1.0
            # only the first 45% of the cycle bounces; rest rests
            lift = math.sin(min(phase / 0.45, 1.0) * math.pi) * 4 if phase < 0.45 else 0
            c = QColor(self._color)
            c.setAlphaF(0.45 + 0.55 * (lift / 4 if lift else 0))
            p.setBrush(c)
            x = 6 + i * 11
            p.drawEllipse(int(x - r), int(baseline - lift - r), int(r * 2), int(r * 2))
        p.end()


class ShimmerMixin:
    """Adds a moving highlight band to a QFrame while `working` is True.

    Host class must call ``_init_shimmer(highlight_qcolor)`` in __init__ and
    ``super().paintEvent(e)`` from its own paintEvent before ``_draw_shimmer``.
    """

    def _init_shimmer(self, highlight):
        self._shimmer_pos = -0.3
        self._shimmer_color = highlight
        self._shimmer_anim = QVariantAnimation(self)
        self._shimmer_anim.setDuration(1200)
        self._shimmer_anim.setStartValue(-0.3)
        self._shimmer_anim.setEndValue(1.3)
        self._shimmer_anim.setLoopCount(-1)
        self._shimmer_anim.valueChanged.connect(self._shimmer_tick)
        if motion_enabled():
            self._shimmer_anim.start()

    def _shimmer_tick(self, v):
        self._shimmer_pos = float(v)
        self.update()

    def stop_shimmer(self):
        self._shimmer_anim.stop()
        self._shimmer_pos = None
        self.update()

    def _draw_shimmer(self):
        if self._shimmer_pos is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        band_w = self.width() * 0.25
        x = self._shimmer_pos * self.width()
        from qgis.PyQt.QtGui import QLinearGradient, QBrush
        grad = QLinearGradient(x - band_w, 0, x + band_w, 0)
        edge = QColor(self._shimmer_color)
        edge.setAlpha(0)
        mid = QColor(self._shimmer_color)
        mid.setAlpha(46)
        grad.setColorAt(0.0, edge)
        grad.setColorAt(0.5, mid)
        grad.setColorAt(1.0, edge)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 9, 9)
        p.end()
