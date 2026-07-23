"""Deterministic Unicode text helpers for the bundled vicinity-map layout."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from math import isfinite
from typing import Any, Iterable


DEGREE_SIGN = chr(0x00B0)
PRIME = chr(0x2032)
DOUBLE_PRIME = chr(0x2033)

_MOJIBAKE_MARKERS = (chr(0x00C2), chr(0xFFFD))
_COORDINATE_ITEM_IDS = ("corp_site_coordinates", "strip_site_coordinates")


def _coordinate_value(value: Any, axis: str) -> tuple[Decimal, str]:
    if isinstance(value, bool):
        raise TypeError(f"{axis} coordinate must be numeric, not bool")
    try:
        numeric = float(value)
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TypeError(f"{axis} coordinate must be numeric") from exc
    if not isfinite(numeric) or not decimal_value.is_finite():
        raise ValueError(f"{axis} coordinate must be finite")
    return decimal_value, "negative" if decimal_value < 0 else "positive"


def format_dms(value: Any, axis: str, precision: int = 1) -> str:
    """Format a numeric latitude or longitude with unambiguous Unicode DMS."""

    normalized_axis = str(axis).strip().lower()
    if normalized_axis not in {"latitude", "longitude", "lat", "lon"}:
        raise ValueError("axis must be latitude/lat or longitude/lon")
    if isinstance(precision, bool) or not isinstance(precision, int):
        raise TypeError("precision must be an integer")
    if not 0 <= precision <= 6:
        raise ValueError("precision must be between 0 and 6")

    decimal_value, sign = _coordinate_value(value, normalized_axis)
    is_latitude = normalized_axis in {"latitude", "lat"}
    maximum = Decimal(90 if is_latitude else 180)
    if abs(decimal_value) > maximum:
        label = "latitude" if is_latitude else "longitude"
        raise ValueError(f"{label} is outside its valid range")

    factor = 10**precision
    scaled_seconds = int(
        (abs(decimal_value) * Decimal(3600 * factor)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    degrees, remainder = divmod(scaled_seconds, 3600 * factor)
    minutes, seconds_scaled = divmod(remainder, 60 * factor)
    seconds_whole, seconds_fraction = divmod(seconds_scaled, factor)

    if degrees > int(maximum):
        label = "latitude" if is_latitude else "longitude"
        raise ValueError(f"rounded {label} is outside its valid range")

    if precision:
        seconds = f"{seconds_whole:02d}.{seconds_fraction:0{precision}d}"
    else:
        seconds = f"{seconds_whole:02d}"
    hemisphere = (
        ("S" if sign == "negative" else "N")
        if is_latitude
        else ("W" if sign == "negative" else "E")
    )
    return (
        f"{degrees}{DEGREE_SIGN}{minutes:02d}{PRIME}{seconds}{DOUBLE_PRIME}{hemisphere}"
    )


def format_coordinate_pair(latitude: Any, longitude: Any, precision: int = 1) -> str:
    """Return ``latitude, longitude`` using the vicinity-map DMS contract."""

    return (
        f"{format_dms(latitude, 'latitude', precision)}, "
        f"{format_dms(longitude, 'longitude', precision)}"
    )


def _iter_layout_text(layout: Any) -> Iterable[tuple[str, str]]:
    items = layout.items()
    for item in items:
        text_getter = getattr(item, "text", None)
        if not callable(text_getter):
            continue
        id_getter = getattr(item, "id", None)
        item_id = str(id_getter()) if callable(id_getter) else "<unidentified-label>"
        yield item_id, str(text_getter())


def assert_layout_unicode(
    layout: Any,
    coordinate_item_ids: tuple[str, str] = _COORDINATE_ITEM_IDS,
) -> dict[str, str]:
    """Fail closed on mojibake, template braces, or malformed coordinate labels."""

    inspected = 0
    for item_id, text in _iter_layout_text(layout):
        inspected += 1
        for marker in _MOJIBAKE_MARKERS:
            if marker in text:
                raise ValueError(
                    f"Invalid Unicode marker U+{ord(marker):04X} in layout label {item_id}"
                )
        if "{{" in text or "}}" in text:
            raise ValueError(
                f"Unresolved template placeholder in layout label {item_id}"
            )
    if inspected == 0:
        raise ValueError("Layout contains no inspectable text labels")

    coordinates: dict[str, str] = {}
    for item_id in coordinate_item_ids:
        item = layout.itemById(item_id)
        text_getter = getattr(item, "text", None) if item is not None else None
        if not callable(text_getter):
            raise ValueError(f"Missing coordinate label: {item_id}")
        value = str(text_getter())
        for symbol, name in (
            (DEGREE_SIGN, "degree sign"),
            (PRIME, "prime"),
            (DOUBLE_PRIME, "double prime"),
        ):
            if symbol not in value:
                raise ValueError(f"{item_id} is missing the Unicode {name}")
        coordinates[item_id] = value

    if len(set(coordinates.values())) != 1:
        raise ValueError("Corporate and strip coordinate labels do not match")
    return coordinates
