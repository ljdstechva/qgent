# -*- coding: utf-8 -*-
"""Self-check for the model watcher's parsing, filtering, and caching.

Run directly: ``python qgis_chat_agent/tests/test_model_watch.py``
"""
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_watch import (  # noqa: E402
    acknowledge, check_models, known_models, new_model_ids, parse_model_id,
    scan_cli_models, summary_text,
)


def test_parse():
    assert parse_model_id("claude", "claude-opus-5") == ("claude-opus-5", "opus", (5,))
    # Snapshot dates and build stamps are stripped, not read as versions.
    assert parse_model_id("claude", "claude-haiku-4-5-20251001-v1") == (
        "claude-haiku-4-5", "haiku", (4, 5))
    assert parse_model_id("claude", "claude-opus-4-6-fast") == (
        "claude-opus-4-6", "opus", (4, 6))
    # A bare date is never a version.
    assert parse_model_id("claude", "claude-code-20250219") is None
    # Two ids glued together in the binary are not a third id.
    assert parse_model_id("claude", "claude-fable-5-mythos-5") is None
    assert parse_model_id("codex", "gpt-5.6-sol") == ("gpt-5.6-sol", "sol", (5, 6))
    # Run-together neighbours are peeled off the variant name.
    assert parse_model_id("codex", "gpt-5.6-lunagpt") == (
        "gpt-5.6-luna", "luna", (5, 6))
    assert parse_model_id("codex", "gpt-5.6-solopenai") == (
        "gpt-5.6-sol", "sol", (5, 6))
    assert parse_model_id("codex", "gpt-4o") is None


def test_new_model_ids():
    claude = known_models("claude")
    assert claude["opus"] >= (5,) and claude["haiku"] == (4, 5)
    fresh = new_model_ids("claude", {
        "claude-opus-5", "claude-opus-4-8", "claude-sonnet-4-6",
        "claude-haiku-4-5", "claude-opus-6", "claude-mythos-5",
        "claude-lyra-5",
    })
    # Listed and superseded ids stay quiet; a newer version and a new family at
    # the current ceiling are news.  Mythos 5 is Glasswing-only, so it is not.
    assert fresh == ["claude-lyra-5", "claude-opus-6"], fresh

    fresh = new_model_ids("codex", {
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-4.1",
        "gpt-5.6", "gpt-5.6-pro", "gpt-5.7-sol", "gpt-6",
    })
    # "gpt-5.6" is a prefix fragment of a listed model; "gpt-5.6-pro" is a
    # reasoning mode of the base model, not a slug. Neither is a model.
    assert fresh == ["gpt-5.7-sol", "gpt-6"], fresh


def test_scan_and_cache():
    with tempfile.TemporaryDirectory() as root:
        fake = Path(root) / "fake-cli.bin"
        fake.write_bytes(
            b"\x00noise claude-opus-7\x01claude-opus-4-6-v1 tail\xff")
        assert scan_cli_models(fake, "claude") == {
            "claude-opus-7", "claude-opus-4-6"}
        assert scan_cli_models(Path(root) / "missing.bin", "claude") == set()

        report = check_models(root, {"claude": str(fake), "codex": ""})
        assert report["new"] == ["claude-opus-7"], report["new"]
        assert report["backends"]["claude"]["scanned"] is True
        assert report["backends"]["codex"]["error"]
        assert "claude-opus-7" in summary_text(report)

        # Unchanged CLI build -> cached result, no rescan.
        again = check_models(root, {"claude": str(fake), "codex": ""})
        assert again["backends"]["claude"]["scanned"] is False
        assert again["new"] == ["claude-opus-7"]

        # Dismissal sticks; the raw finding is still recorded.
        acknowledge(root, ["claude-opus-7"])
        quiet = check_models(root, {"claude": str(fake), "codex": ""})
        assert quiet["new"] == []
        assert quiet["all_new"] == ["claude-opus-7"]
        assert "No new models found" in summary_text(quiet)

        # A CLI update invalidates the cache.
        os.utime(fake, (0, 0))
        rescanned = check_models(root, {"claude": str(fake), "codex": ""})
        assert rescanned["backends"]["claude"]["scanned"] is True


def demo():
    test_parse()
    test_new_model_ids()
    test_scan_and_cache()
    print("model_watch self-check: OK")


if __name__ == "__main__":
    demo()
