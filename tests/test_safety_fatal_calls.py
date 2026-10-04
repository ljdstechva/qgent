"""The crash guard that blocks QGIS-killing calls before they reach exec().

Regression for the 2026-08-06 access violation: generated code called
``base.previewAsImage(QSize(400, 400))`` on an XYZ tile layer ("Satellite
Imagery (Esri)") and took the whole QGIS process down mid-turn.

Reproduced offscreen on QGIS 3.44 (exit 0xC0000005). The trigger is an active
hueSaturationFilter, which the same conversation had switched on two turns
earlier with ``setSaturation(-60)``; with saturation 0 the call is harmless,
and brightness alone is harmless. It is not provider-specific — a plain local
GeoTIFF crashes the same way. Since a static scan cannot see the layer's filter
state, the call is refused unconditionally.
"""
from __future__ import annotations

from bridge import safety


CRASHER = (
    "base = p.mapLayersByName('Satellite Imagery (Esri)')[0]\n"
    "img = base.previewAsImage(QSize(400,400))\n"
)


def test_blocks_the_call_that_crashed_qgis():
    reasons = safety.fatal_calls(CRASHER)
    assert len(reasons) == 1
    # The refusal must name the working alternative — it is the only thing the
    # model gets back, and it has to reroute from it.
    assert "QgsMapRendererSequentialJob" in reasons[0]


def test_blocks_it_even_when_the_code_does_not_parse():
    assert safety.fatal_calls(CRASHER + "def broken(:\n")


def test_reports_each_distinct_crasher_once():
    assert len(safety.fatal_calls(CRASHER * 3)) == 1


def test_leaves_ordinary_raster_work_alone():
    assert safety.fatal_calls(
        "job = QgsMapRendererSequentialJob(ms)\njob.start()\n") == []


def test_is_not_the_destructive_scan():
    # fatal_calls refuses; scan prompts. previewAsImage mutates nothing, so it
    # must not leak into the approval path.
    assert safety.scan(CRASHER) == []
