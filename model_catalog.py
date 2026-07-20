# -*- coding: utf-8 -*-
"""QGent model catalogue shared by QGIS and the stdlib External Doctor."""
from __future__ import annotations


MODEL_ROLES = ("supervisor", "worker", "light")
CUSTOM_MODEL_SENTINEL = "__qgent_custom_model__"

# Neither Claude Code 2.1.214 nor Codex CLI 0.144.1 exposes a headless model
# enumeration command. ``id`` is the single ordinary chat value; ``aliases``
# are accepted only for migration/validation and are never duplicate UI rows.
MODEL_CATALOG = {
    "claude": (
        {
            "id": "sonnet",
            "label": "Sonnet — balanced (default)",
            "aliases": ("claude-sonnet-5",),
        },
        {
            "id": "haiku",
            "label": "Haiku — fastest, light tasks",
            "aliases": ("claude-haiku-4-5-20251001",),
        },
        {
            "id": "opus",
            "label": "Opus 4.8 — most capable",
            "aliases": ("claude-opus-4-8",),
            "repair": {
                "model": "claude-opus-4-8", "effort": "max",
                "label": "Claude · Opus 4.8 · max",
            },
        },
        {
            "id": "fable",
            "label": "Fable 5 — frontier",
            "aliases": ("claude-fable-5",),
            "repair": {
                "model": "claude-fable-5", "effort": "max",
                "label": "Claude · Fable 5 · max",
            },
        },
    ),
    "codex": (
        {
            "id": "gpt-5.6-sol",
            "label": "GPT-5.6 Sol — most capable",
            "aliases": (),
            "repair": {
                "model": "gpt-5.6-sol", "effort": "xhigh",
                "label": "Codex · GPT-5.6 Sol · xhigh",
            },
        },
        {
            "id": "gpt-5.6-terra",
            "label": "GPT-5.6 Terra — fast",
            "aliases": (),
        },
    ),
}

MODEL_DEFAULTS = {
    "claude": {"supervisor": "sonnet", "worker": "sonnet", "light": "haiku"},
    "codex": {
        "supervisor": "gpt-5.6-sol",
        "worker": "gpt-5.6-sol",
        "light": "gpt-5.6-sol",
    },
}

MODEL_IDS_BY_BACKEND = {
    backend: tuple(entry["id"] for entry in entries)
    for backend, entries in MODEL_CATALOG.items()
}


def model_ids(backend):
    """Return exactly one selectable CLI id for each curated model."""
    return MODEL_IDS_BY_BACKEND.get(str(backend or "").lower(), ())


def model_options(backend):
    """Return ``(friendly label, CLI id)`` rows for the selected backend."""
    entries = MODEL_CATALOG.get(str(backend or "").lower(), ())
    return tuple((entry["label"], entry["id"]) for entry in entries)


def accepted_model_ids(backend):
    """Return curated ids plus known full-id aliases, without duplicate rows."""
    values = []
    for entry in MODEL_CATALOG.get(str(backend or "").lower(), ()):
        values.append(entry["id"])
        values.extend(entry.get("aliases") or ())
    return tuple(values)


def normalize_model_id(backend, value):
    """Map a known id/full-id alias to its single selectable CLI id."""
    text = str(value or "").strip()
    for entry in MODEL_CATALOG.get(str(backend or "").lower(), ()):
        if text == entry["id"] or text in (entry.get("aliases") or ()):
            return entry["id"]
    return ""


def default_model(backend, role="supervisor"):
    backend = str(backend or "").lower()
    role = str(role or "supervisor").lower()
    return MODEL_DEFAULTS.get(backend, {}).get(role, "")


def repair_model_specs():
    """Return repair choices from the same catalogue used by Settings."""
    specs = []
    for backend, entries in MODEL_CATALOG.items():
        for entry in entries:
            repair = entry.get("repair")
            if repair:
                item = dict(repair)
                item["backend"] = backend
                specs.append(item)
    return tuple(sorted(specs, key=lambda item: item["backend"] != "codex"))
