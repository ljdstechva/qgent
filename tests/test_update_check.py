"""Release/model checks against controlled sources: no network or real CLI."""
import json
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update_check as updates
import model_watch


@pytest.fixture
def setup(tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "metadata.txt").write_text("[general]\nversion=0.3.0\n", encoding="utf-8")
    paths = {}
    for name, content in (("claude", b"claude-opus-7"), ("codex", b"gpt-7-sol")):
        path = tmp_path / (name + ".bin")
        path.write_bytes(content)
        paths[name] = str(path)
    calls = []

    def fetch(url):
        calls.append(("fetch", url))
        if url == updates.SOURCES["qgent"]:
            return "[general]\nversion=0.4.0\n"
        return json.dumps({"tag_name": "v2.2.0" if "anthropics" in url else "rust-v0.161.0",
                           "draft": False, "prerelease": False, "body": ""})

    def run(path, args, *, stdin_text=None):
        calls.append(("run", args))
        if args == ["--version"]:
            return "2.1.287 (Claude Code)" if path == paths["claude"] else "codex-cli 0.160.0"
        if args[0] == "--print":
            assert "--safe-mode" in args and "--no-session-persistence" in args
            request = json.loads(stdin_text)
            assert request["type"] == "control_request"
            assert request["request"]["subtype"] == "initialize"
            assert "prompt" not in request
            model = Path(path).read_text().split()[0]
            return json.dumps({"type": "control_response", "response": {
                "subtype": "success", "request_id": request["request_id"],
                "response": {"models": [
                    {"value": model, "displayName": model},
                    {"value": "opus", "displayName": "Opus 7"}],
                    "account": {"private": "must not persist"}}}})
        assert args == ["debug", "models"]
        return json.dumps({"models": [
            {"slug": "gpt-6.1-sol", "visibility": "list"},
            {"slug": "gpt-6-astra", "visibility": "list"},
            {"slug": "gpt-7-secret", "visibility": "hide"},
            {"slug": "gpt-5.5", "visibility": "list"},
            {"slug": "gpt-5.6-sol", "visibility": "list"},
        ]})

    def check(**kwargs):
        options = dict(fetcher=fetch, runner=run, now=100000)
        options.update(kwargs)
        return updates.check_updates(tmp_path, plugin, paths, **options)

    return tmp_path, plugin, paths, calls, check


def test_versions_and_candidate_provenance(setup):
    _, _, _, calls, check = setup
    report = check()
    assert all(row["update"] for row in report["releases"].values())
    assert report["models"]["codex"]["ids"] == ["gpt-5.5", "gpt-6-astra", "gpt-6.1-sol"]
    assert report["models"]["codex"]["source"] == "Codex model catalog"
    assert report["models"]["claude"]["ids"] == ["claude-opus-7"]
    assert len(report["notices"]) == 7
    assert not report["errors"]
    assert "No software is installed" in updates.report_text(report)
    assert all(args in (["--version"], ["debug", "models"]) or args[0] == "--print"
               for kind, args in calls if kind == "run")
    assert report["models"]["claude"]["source"] == "Claude Code model catalog"
    assert {row["id"] for row in report["models"]["codex"]["entries"]} == {
        "gpt-5.5", "gpt-5.6-sol", "gpt-6-astra", "gpt-6.1-sol"}
    assert "must not persist" not in json.dumps(report)
    assert updates.version_tuple("rust-v0.160.10") > updates.version_tuple("0.160.9")
    for invalid in ("v1.2.3-beta.1", "1.2", "garbage", "1.2.3\nextra"):
        with pytest.raises(ValueError):
            updates.version_tuple(invalid)


def test_cache_daily_manual_and_clock_rewind(setup):
    _, _, _, calls, check = setup
    first = check()
    calls.clear()
    assert check(now=100100)["cached"]
    assert calls == []
    assert not check(force=True)["cached"]
    calls.clear()
    assert not check(now=first["checked_at"] + updates.CHECK_INTERVAL)["cached"]
    assert calls
    assert not check(now=10)["cached"]


def test_cli_and_catalog_changes_invalidate_cache(setup, monkeypatch):
    _, _, paths, calls, check = setup
    check()
    calls.clear()
    Path(paths["claude"]).write_bytes(b"claude-opus-8 x")
    assert check()["models"]["claude"]["ids"] == ["claude-opus-8"]
    assert calls
    calls.clear()
    original = updates.accepted_model_ids
    monkeypatch.setattr(updates, "accepted_model_ids", lambda name: original(name) + ("new-catalog-entry",))
    assert not check()["cached"]
    assert calls


def test_persistent_dismissal_only_silences_current_findings(setup):
    root, _, paths, _, check = setup
    first = check()
    updates.dismiss(root, first["notices"])
    assert check()["notices"] == []
    assert check(force=True)["notices"] == []
    Path(paths["claude"]).write_bytes(b"claude-opus-8 extra")
    assert check()["notices"] == ["model:claude:claude-opus-8"]


def test_offline_not_up_to_date_and_hourly_retry(setup):
    _, _, _, calls, check = setup

    def offline(_url):
        raise TimeoutError("must not leak remote response data")

    report = check(fetcher=offline)
    assert len(report["errors"]) == 3
    assert "Check incomplete" in updates.report_text(report)
    assert "No newer version found" not in updates.report_text(report)
    assert "must not leak" not in json.dumps(report)
    calls.clear()
    assert check(now=100100)["cached"]
    assert calls == []
    assert not check(now=100000 + updates.RETRY_INTERVAL)["cached"]


def test_corrupt_state_missing_clis_and_prerelease(setup):
    root, plugin, _, _, check = setup
    path = root / "qgent" / "updates.json"
    path.parent.mkdir()
    path.write_text("not json", encoding="utf-8")
    assert any("Saved update state" in error for error in check()["errors"])
    report = updates.check_updates(root, plugin, {}, force=True,
                                  fetcher=lambda _: json.dumps({"prerelease": True, "draft": False, "tag_name": "v99.0.0"}))
    assert not any(row["update"] for row in report["releases"].values())
    assert report["releases"]["codex"]["missing"]
    assert len(report["errors"]) == 3


def test_old_codex_fallback_and_unconfirmed_release_mentions(setup):
    _, _, _, _, check = setup

    def runner(path, args):
        if args == ["--version"]:
            return "2.1.287 (Claude Code)" if "claude" in path else "codex-cli 0.140.0"
        raise ValueError("debug models unsupported")

    def fetch(_url):
        return json.dumps({"draft": False, "prerelease": False, "tag_name": "v2.2.0",
                           "body": "Support for claude-sonnet-7 and gpt-8-sol."})

    report = check(runner=runner, fetcher=fetch)
    assert report["models"]["codex"]["ids"] == ["gpt-7-sol", "gpt-8-sol"]
    assert "unconfirmed" in report["models"]["codex"]["source"]
    assert report["models"]["claude"]["release_ids"] == ["claude-sonnet-7"]
    assert any("Older Codex" in error for error in report["errors"])


def test_failed_cache_write_visible_and_cancellation_stops_work(setup, monkeypatch):
    _, _, _, calls, check = setup

    def fail(*_args):
        raise PermissionError("private path")

    monkeypatch.setattr(updates, "save_state", fail)
    assert any("Could not save" in error for error in check()["errors"])
    calls.clear()
    assert check(cancelled=lambda: True) is None
    assert calls == []


def test_partial_failure_does_not_hide_other_sources(setup):
    _, _, _, _, check = setup

    def fetch(url):
        if url == updates.SOURCES["qgent"]:
            return "[general]\nversion=0.4.0\n"
        raise ConnectionError()

    report = check(fetcher=fetch)
    assert report["releases"]["qgent"]["update"]
    assert len(report["errors"]) == 2
    assert report["models"]["codex"]["source"] == "Codex model catalog"


def test_local_model_cache_refilters_current_catalog(setup, monkeypatch):
    root, _, paths, _, _ = setup
    first = model_watch.check_models(root, paths)
    assert "gpt-7-sol" in first["all_new"]
    original = model_watch.accepted_model_ids
    monkeypatch.setattr(model_watch, "accepted_model_ids", lambda backend: original(backend) + ("gpt-7-sol",))
    assert "gpt-7-sol" not in model_watch.check_models(root, paths)["all_new"]


def test_structured_catalog_preserves_exact_future_slugs():
    payload = {"models": [
        {"slug": "gpt-7-codex-mini", "visibility": "list"},
        {"slug": "future-model-preview", "visibility": "list"},
        {"slug": "gpt-5.6-sol", "visibility": "list"},
        {"slug": "gpt-7-secret", "visibility": "hide"},
    ]}
    ids, hidden, entries = updates.codex_catalog("fixture", lambda *_: json.dumps(payload))
    assert ids == ["future-model-preview", "gpt-7-codex-mini"]
    assert hidden == {"gpt-7-secret"}
    assert "gpt-7-secret" not in {row["id"] for row in entries}


def test_hidden_catalog_slug_cannot_return_via_release_notes(setup):
    _, _, _, _, check = setup

    def fetch(url):
        if url == updates.SOURCES["qgent"]:
            return "[general]\nversion=0.3.0\n"
        return json.dumps({"draft": False, "prerelease": False, "tag_name": "v2.2.0",
                           "body": "Do not enable gpt-7-secret."})

    report = check(fetcher=fetch)
    assert "gpt-7-secret" not in report["models"]["codex"]["ids"]
    assert "gpt-7-secret" not in report["models"]["codex"]["release_ids"]
    assert all("gpt-7-secret" not in notice for notice in report["notices"])
    assert "gpt-7-secret" not in updates.report_text(report)
    assert "gpt-7-secret" not in {row["id"] for row in report["models"]["codex"]["entries"]}


def test_claude_catalog_failure_keeps_candidates_unselectable(setup):
    _, _, _, _, check = setup
    def unavailable(*_args, **_kwargs):
        raise ValueError("Unavailable")
    report = check(runner=unavailable)
    assert "entries" not in report["models"]["claude"]
    assert "entries" not in report["models"]["codex"]
    assert any("Claude model catalog unavailable" in error for error in report["errors"])


@pytest.mark.parametrize("models", [[None], [{}], [{"value": 42, "slug": 42}], [{"value": "", "slug": ""}]])
def test_malformed_catalog_is_not_a_successful_empty_refresh(models):
    with pytest.raises(ValueError):
        updates.codex_catalog("fixture", lambda *_: json.dumps({"models": models}))
    response = {"type": "control_response", "response": {
        "subtype": "success", "request_id": "qgent-models", "response": {"models": models}}}
    with pytest.raises(ValueError):
        updates.claude_catalog("fixture", lambda *_, **__: json.dumps(response))


def test_dismissal_during_report_save_is_not_lost(setup, monkeypatch):
    root, _, _, _, check = setup
    initial = check()
    saving = threading.Event()
    proceed = threading.Event()
    dismiss_started = threading.Event()
    dismiss_done = threading.Event()
    failures = []
    original_save = updates.save_state

    def delayed_save(profile, state):
        if threading.current_thread().name == "update-worker":
            saving.set()
            assert proceed.wait(4)
        original_save(profile, state)

    def refresh():
        try:
            check(force=True)
        except Exception as exc:
            failures.append(exc)

    def acknowledge():
        try:
            dismiss_started.set()
            updates.dismiss(root, initial["notices"])
            dismiss_done.set()
        except Exception as exc:
            failures.append(exc)

    monkeypatch.setattr(updates, "save_state", delayed_save)
    worker = threading.Thread(target=refresh, name="update-worker")
    worker.start()
    assert saving.wait(4)
    dismiss_thread = threading.Thread(target=acknowledge)
    dismiss_thread.start()
    assert dismiss_started.wait(4)
    try:
        assert not dismiss_done.is_set()
    finally:
        proceed.set()
        worker.join(4)
        dismiss_thread.join(4)
    assert not worker.is_alive() and not dismiss_thread.is_alive()
    assert not failures
    assert dismiss_done.is_set()
    assert set(updates.load_state(root)["dismissed"]) == set(initial["notices"])
    assert check()["notices"] == []
