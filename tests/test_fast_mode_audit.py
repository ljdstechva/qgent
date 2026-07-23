from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from export import render_markdown
from history import HistoryStore
from turn_report import build_turn_report, render_batch_summary


STAMP = "2026-07-23T10:30:00+08:00"


def _metadata() -> dict[str, str]:
    return {
        "project_name": "fixture",
        "project_path": r"D:\fixtures\fixture.qgz",
        "backend": "codex",
        "model": "gpt-5.6-sol",
        "version": "1.0",
    }


def test_turn_report_freezes_strict_fast_boolean() -> None:
    assert build_turn_report({"fast": True})["fast"] is True
    assert build_turn_report({"fast": False})["fast"] is False
    assert build_turn_report({})["fast"] is False
    assert build_turn_report({"fast": "true"})["fast"] is False


def test_history_round_trips_fast_boolean_without_schema_changes(tmp_path) -> None:
    store = HistoryStore(tmp_path, r"D:\fixtures\fixture.qgz")
    fast = store.append("user", text="fast", fast=True)
    normal = store.append("assistant", text="normal", fast=False)

    assert fast["fast"] is True
    assert normal["fast"] is False
    restored = store.load()["records"]
    assert [record["fast"] for record in restored] == [True, False]


def test_batch_summary_records_fast_mode_without_changing_rows() -> None:
    rows = [{"text": "buffer", "status": "done", "tokens": 12}]
    fast = render_batch_summary(
        rows, "pause", r"D:\backup", "", 1.25, [], fast_mode=True)
    normal = render_batch_summary(
        rows, "pause", r"D:\backup", "", 1.25, [])

    assert "- Fast mode: ON" in fast
    assert "- Fast mode: OFF" in normal
    assert "⚡" not in normal
    assert rows == [{"text": "buffer", "status": "done", "tokens": 12}]


def test_markdown_marks_only_fast_turn_records() -> None:
    records = [
        {"kind": "user", "t": STAMP, "text": "fast ask", "fast": True},
        {"kind": "assistant", "t": STAMP, "text": "fast answer", "fast": True},
        {"kind": "user", "t": STAMP, "text": "normal ask", "fast": False},
        {"kind": "assistant", "t": STAMP, "text": "normal answer"},
        {
            "kind": "queue_summary",
            "t": STAMP,
            "text": "- Fast mode: ON",
            "fast": True,
        },
    ]
    rendered = render_markdown(
        records,
        _metadata(),
        exported_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )

    assert rendered.count("⚡") == 3
    assert "**You** ⚡ - 10:30" in rendered
    assert "**QGent** ⚡ - 10:30" in rendered
    assert "**QGent batch summary** ⚡ - 10:30" in rendered
    assert "**You** - 10:30" in rendered
    assert "**QGent** - 10:30" in rendered


def test_normal_markdown_is_identical_for_absent_or_false_fast_flag() -> None:
    exported_at = datetime(2026, 7, 23, tzinfo=timezone.utc)
    absent = [
        {"kind": "user", "t": STAMP, "text": "ask"},
        {"kind": "assistant", "t": STAMP, "text": "answer"},
    ]
    explicit_false = [dict(record, fast=False) for record in absent]

    assert render_markdown(
        absent, _metadata(), exported_at=exported_at
    ) == render_markdown(
        explicit_false, _metadata(), exported_at=exported_at
    )


def test_runtime_notes_scope_fast_override_without_weakening_safety() -> None:
    runtime = Path(__file__).resolve().parents[1] / "claude_runtime"
    claude = (runtime / "CLAUDE.md").read_text(encoding="utf-8")
    codex = (runtime / "AGENTS.md").read_text(encoding="utf-8")

    for text in (claude, codex):
        assert "`## FAST MODE (user-enabled for this turn)`" in text
        assert "that turn only" in text
        assert "destructive-code approval" in text
        assert "`ask_user` rule" in text
        assert "evidence requirements" in text
        assert "honest\nfailure reporting" in text
    assert "do not\ndispatch subagents or `qa-verifier`" in claude
    assert "skip\nthe separate Codex self-verification pass" in codex
