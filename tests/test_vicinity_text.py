from __future__ import annotations

import math

import pytest

from vicinity_text import (
    DEGREE_SIGN,
    DOUBLE_PRIME,
    PRIME,
    assert_layout_unicode,
    format_coordinate_pair,
    format_dms,
)


class FakeLabel:
    def __init__(self, item_id: str, text: str) -> None:
        self._item_id = item_id
        self._text = text

    def id(self) -> str:
        return self._item_id

    def text(self) -> str:
        return self._text


class FakeShape:
    def id(self) -> str:
        return "shape"


class FakeLayout:
    def __init__(self, labels: list[FakeLabel]) -> None:
        self._labels = labels

    def items(self) -> list[object]:
        return [FakeShape(), *self._labels]

    def itemById(self, item_id: str) -> FakeLabel | None:
        return next((item for item in self._labels if item.id() == item_id), None)


def coordinate_layout(text: str) -> FakeLayout:
    return FakeLayout(
        [
            FakeLabel("corp_site_coordinates", text),
            FakeLabel("strip_site_coordinates", text),
            FakeLabel("other_label", "VICINITY MAP"),
        ]
    )


def test_rejected_coastal_fixture_uses_exact_unicode_dms() -> None:
    value = format_coordinate_pair(
        5 + 52 / 60 + 23.4 / 3600, 125 + 4 / 60 + 48.9 / 3600
    )
    assert value == "5°52′23.4″N, 125°04′48.9″E"
    assert (DEGREE_SIGN, PRIME, DOUBLE_PRIME) == ("°", "′", "″")


def test_rounding_carries_seconds_and_minutes() -> None:
    assert format_dms(12.99999999, "lat", 1) == "13°00′00.0″N"
    assert format_dms(179.99999999, "lon", 0) == "180°00′00″E"


def test_negative_coordinates_use_south_and_west() -> None:
    assert format_coordinate_pair(-14.65, -121.05) == "14°39′00.0″S, 121°03′00.0″W"


@pytest.mark.parametrize(
    ("value", "axis", "exception"),
    [
        (91, "lat", ValueError),
        (181, "lon", ValueError),
        (math.nan, "lat", ValueError),
        (math.inf, "lon", ValueError),
        (True, "lat", TypeError),
        ("north", "lat", TypeError),
        (0, "easting", ValueError),
    ],
)
def test_invalid_coordinates_fail_closed(
    value: object, axis: str, exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        format_dms(value, axis)


def test_layout_guard_accepts_filled_coordinate_labels() -> None:
    text = format_coordinate_pair(5.8731666667, 125.08025)
    assert assert_layout_unicode(coordinate_layout(text)) == {
        "corp_site_coordinates": text,
        "strip_site_coordinates": text,
    }


@pytest.mark.parametrize(
    "bad_text",
    [
        "5Â°52′23.4″N, 125Â°04′48.9″E",
        "5�°52′23.4″N, 125°04′48.9″E",
        "{{site_coordinates}}",
        "5°52'23.4\"N, 125°04'48.9\"E",
    ],
)
def test_layout_guard_rejects_mojibake_placeholders_and_ascii_quotes(
    bad_text: str,
) -> None:
    with pytest.raises(ValueError):
        assert_layout_unicode(coordinate_layout(bad_text))


def test_layout_guard_requires_both_stable_coordinate_ids() -> None:
    layout = FakeLayout([FakeLabel("corp_site_coordinates", format_dms(5, "lat"))])
    with pytest.raises(ValueError, match="strip_site_coordinates"):
        assert_layout_unicode(layout)
