# -*- coding: utf-8 -*-
"""QGent model catalogue shared by QGIS and the stdlib External Doctor."""
from __future__ import annotations


MODEL_ROLES = ("supervisor", "worker", "light")
CUSTOM_MODEL_SENTINEL = "__qgent_custom_model__"
MODEL_PRESET_SPEED = "speed"
MODEL_PRESET_MAX_QUALITY = "max_quality"
MODEL_PRESET_LONG_CONTEXT = "long_context"
MODEL_PRESET_CUSTOM = "custom"
LIGHT_ROLE_TOOLTIP = (
    "Scout/verifier are read-and-report; bigger models only slow them down."
)

# Neither Claude Code 2.1.223 nor Codex CLI 0.146.1 exposes a headless model
# enumeration command, so this list is curated. ``model_watch`` discovers ids
# the installed CLIs know about and flags anything newer than what is listed
# here. ``id`` is the single ordinary chat value; ``aliases`` are accepted only
# for migration/validation and are never duplicate UI rows.
MODEL_CATALOG = {
    "claude": (
        {
            "id": "sonnet",
            "label": "Sonnet 5 — balanced (default)",
            "aliases": ("claude-sonnet-5", "claude-sonnet-4-6"),
        },
        {
            "id": "sonnet[1m]",
            "label": "Sonnet 5 (1M context) — long sessions",
            "aliases": ("claude-sonnet-5[1m]",),
        },
        {
            "id": "haiku",
            "label": "Haiku 4.5 — fastest, light tasks",
            "aliases": ("claude-haiku-4-5", "claude-haiku-4-5-20251001"),
        },
        {
            "id": "opus",
            "label": "Opus 5 — most capable",
            "aliases": ("claude-opus-5", "claude-opus-4-8"),
            "repair": {
                "model": "claude-opus-5", "effort": "max",
                "label": "Claude · Opus 5 · max",
            },
        },
        {
            "id": "opus[1m]",
            "label": "Opus 5 (1M context) — long sessions, most capable",
            "aliases": ("claude-opus-5[1m]",),
        },
        {
            "id": "opusplan",
            "label": "Opus Plan — Opus plans, Sonnet executes",
            "aliases": (),
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
        {
            "id": "gpt-5.6-luna",
            "label": "GPT-5.6 Luna — small, cheapest",
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

# Presets intentionally reuse the existing per-backend/per-role settings.  A
# preset is UI shorthand for an exact three-value signature, never a separate
# persisted setting.  A preset that omits a backend is not offered for it —
# see ``model_preset_options``.
MODEL_PRESET_OPTIONS = (
    ("Speed (recommended)", MODEL_PRESET_SPEED),
    ("Max quality", MODEL_PRESET_MAX_QUALITY),
    ("Long context (1M)", MODEL_PRESET_LONG_CONTEXT),
    ("Custom…", MODEL_PRESET_CUSTOM),
)

MODEL_PRESETS = {
    MODEL_PRESET_SPEED: {
        "claude": {
            "supervisor": "sonnet", "worker": "sonnet", "light": "haiku",
        },
        "codex": {
            "supervisor": "gpt-5.6-terra",
            "worker": "gpt-5.6-terra",
            "light": "gpt-5.6-terra",
        },
    },
    MODEL_PRESET_MAX_QUALITY: {
        "claude": {
            "supervisor": "fable", "worker": "sonnet", "light": "haiku",
        },
        "codex": {
            "supervisor": "gpt-5.6-sol",
            "worker": "gpt-5.6-sol",
            "light": "gpt-5.6-sol",
        },
    },
    # Claude Code only: Codex exposes no long-context model variants, so this
    # preset is simply not offered when the Codex backend is selected.
    MODEL_PRESET_LONG_CONTEXT: {
        "claude": {
            "supervisor": "opus[1m]", "worker": "opus[1m]", "light": "haiku",
        },
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


def model_preset_options(backend=None):
    """Return the preset rows in UI order, dropping any the backend lacks.

    ``Custom…`` always applies.  Without a backend the full list is returned.
    """
    if not backend:
        return MODEL_PRESET_OPTIONS
    return tuple(
        (label, preset) for label, preset in MODEL_PRESET_OPTIONS
        if preset == MODEL_PRESET_CUSTOM
        or len(model_preset_values(backend, preset)) == len(MODEL_ROLES))


def model_preset_values(backend, preset):
    """Return a copy of one preset's per-role ids, or an empty mapping."""
    backend = str(backend or "").lower()
    values = MODEL_PRESETS.get(str(preset or ""), {}).get(backend, {})
    return {role: values[role] for role in MODEL_ROLES if role in values}


def classify_model_preset(backend, choices):
    """Classify exact stored choices without inventing preset provenance."""
    backend = str(backend or "").lower()
    choices = choices if isinstance(choices, dict) else {}
    for _label, preset in model_preset_options(backend):
        if preset == MODEL_PRESET_CUSTOM:
            continue
        expected = model_preset_values(backend, preset)
        if len(expected) != len(MODEL_ROLES):
            continue
        matches = True
        for role in MODEL_ROLES:
            choice = choices.get(role, {})
            if not isinstance(choice, dict):
                matches = False
                break
            if bool(choice.get("custom")):
                matches = False
                break
            if str(choice.get("model_id") or "").strip() != expected[role]:
                matches = False
                break
        if matches:
            return preset
    return MODEL_PRESET_CUSTOM


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
