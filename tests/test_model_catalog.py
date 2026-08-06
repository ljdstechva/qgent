# -*- coding: utf-8 -*-
"""Self-check for catalogue lookups and backend-aware model presets.

Run directly: ``python qgis_chat_agent/tests/test_model_catalog.py``
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_catalog import (  # noqa: E402
    MODEL_PRESET_CUSTOM, MODEL_PRESET_LONG_CONTEXT, MODEL_PRESET_MAX_QUALITY,
    MODEL_PRESET_SPEED, MODEL_ROLES, accepted_model_ids,
    classify_model_preset, model_ids, model_preset_options,
    model_preset_values, normalize_model_id, repair_model_specs,
)


def _choices(values):
    return {role: {"model_id": values[role], "custom": False}
            for role in MODEL_ROLES}


def test_catalog():
    claude = model_ids("claude")
    # Every long-context and plan-mode alias is selectable, exactly once each.
    for value in ("sonnet", "sonnet[1m]", "haiku", "opus", "opus[1m]",
                  "opusplan", "fable"):
        assert claude.count(value) == 1, value
    assert model_ids("codex").count("gpt-5.6-luna") == 1
    # Bracketed ids survive normalisation by id and by full-name alias.
    assert normalize_model_id("claude", "opus[1m]") == "opus[1m]"
    assert normalize_model_id("claude", "claude-opus-5[1m]") == "opus[1m]"
    assert normalize_model_id("claude", "nope") == ""
    # Repair models must stay inside the catalogue the chat UI offers.
    for spec in repair_model_specs():
        assert spec["model"] in accepted_model_ids(spec["backend"]), spec


def test_presets_are_backend_aware():
    claude = [preset for _label, preset in model_preset_options("claude")]
    codex = [preset for _label, preset in model_preset_options("codex")]
    assert MODEL_PRESET_LONG_CONTEXT in claude
    # Codex has no long-context variants, so the preset is never offered there.
    assert MODEL_PRESET_LONG_CONTEXT not in codex
    assert claude[-1] == codex[-1] == MODEL_PRESET_CUSTOM
    for preset in claude + codex:
        if preset == MODEL_PRESET_CUSTOM:
            continue
        backend = "claude" if preset in claude else "codex"
        assert len(model_preset_values(backend, preset)) == len(MODEL_ROLES)


def test_classify_round_trip():
    # Every offered preset must classify back to itself, or the Settings combo
    # would show "Custom" straight after the user picked one.
    for backend in ("claude", "codex"):
        for _label, preset in model_preset_options(backend):
            if preset == MODEL_PRESET_CUSTOM:
                continue
            values = model_preset_values(backend, preset)
            assert classify_model_preset(backend, _choices(values)) == preset

    assert classify_model_preset("claude", _choices({
        "supervisor": "opus[1m]", "worker": "opus[1m]", "light": "haiku",
    })) == MODEL_PRESET_LONG_CONTEXT
    # An unlisted combination, and anything flagged Custom, stays Custom.
    assert classify_model_preset("claude", _choices({
        "supervisor": "opus", "worker": "haiku", "light": "fable",
    })) == MODEL_PRESET_CUSTOM
    speed = model_preset_values("claude", MODEL_PRESET_SPEED)
    marked = _choices(speed)
    marked["worker"]["custom"] = True
    assert classify_model_preset("claude", marked) == MODEL_PRESET_CUSTOM
    # Distinct presets must not collide on one signature.
    assert (model_preset_values("claude", MODEL_PRESET_SPEED)
            != model_preset_values("claude", MODEL_PRESET_MAX_QUALITY))


def demo():
    test_catalog()
    test_presets_are_backend_aware()
    test_classify_round_trip()
    print("model_catalog self-check: OK")


if __name__ == "__main__":
    demo()
