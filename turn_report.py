# -*- coding: utf-8 -*-
"""Evidence-only per-turn reports and persisted batch-summary rendering."""
from __future__ import annotations

import math
import ntpath
import re


_EXPORT_EXTENSIONS = (
    "pdf", "png", "jpg", "jpeg", "tif", "tiff", "csv", "gpkg", "shp",
    "kml", "geojson", "xlsx", "docx", "pptx",
)


def build_turn_report(turn, terminal_payload=None):
    """Build a report from events observed during exactly one active turn."""
    state = turn if isinstance(turn, dict) else {}
    terminal = terminal_payload if isinstance(terminal_payload, dict) else {}
    assistant_text = "".join(str(part) for part in (
        state.get("assistant_parts") or []))
    tools = [item for item in (state.get("tool_reports") or [])
             if isinstance(item, dict)]
    events = [item for item in (state.get("subagent_events") or [])
              if isinstance(item, dict)]

    usage = terminal.get("qgent_usage")
    usage = dict(usage) if isinstance(usage, dict) else None
    tokens = _nonnegative_int((usage or {}).get("total_tokens"))
    layers = _created_layers(tools, assistant_text)
    files = _exported_files(tools, assistant_text)
    verdict = _qa_verdict(assistant_text, events)
    return {
        "fast": state.get("fast") is True,
        "usage": usage,
        "tokens": tokens,
        "verdict": verdict,
        "layers_created": layers,
        "files_exported": files,
    }


def batch_token_totals(rows):
    """Return a complete batch total, or ``None`` if any run lacks usage.

    Skipped tasks never started a CLI turn and therefore contribute zero.
    A done or failed task with no terminal usage makes the batch total unknown;
    summing only the reported rows would incorrectly present a subtotal as a
    complete total.
    """
    total = 0
    unavailable = 0
    for row in rows or []:
        if str(row.get("status") or "") == "skipped":
            continue
        value = _nonnegative_int(row.get("tokens"))
        if value is None:
            unavailable += 1
        else:
            total += value
    return (None if unavailable else total), unavailable


def render_batch_summary(rows, policy, backup_path, warning, wall_time,
                         auto_approvals, stopped=False, stop_reason="",
                         fast_mode=False):
    """Render one durable assistant-style Markdown audit record."""
    records = [dict(row) for row in (rows or [])]
    reason = str(stop_reason or "")
    heading = "Queue stopped on error" if (
        stopped and reason == "error") else (
            "Queue stopped" if stopped else "Queue complete")
    lines = [
        "**{}**".format(heading),
        "",
        "| # | Task | Status | Verdict | Layers created | Files exported | Elapsed | Tokens |",
        "| ---: | --- | --- | --- | --- | --- | ---: | ---: |",
    ]
    for index, row in enumerate(records, 1):
        layers = "<br>".join(_layer_text(item) for item in (
            row.get("layers_created") or []))
        files = "<br>".join(str(path) for path in (
            row.get("files_exported") or []))
        elapsed = row.get("elapsed_s")
        elapsed_text = ("{:.1f}s".format(float(elapsed))
                        if isinstance(elapsed, (int, float)) else "")
        tokens = _nonnegative_int(row.get("tokens"))
        token_text = "{:,}".format(tokens) if tokens is not None else ""
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                index,
                _markdown_cell(_task_text(row.get("text", ""))),
                _markdown_cell(row.get("status", "")),
                _markdown_cell(row.get("verdict", "")),
                _markdown_cell(layers, preserve_breaks=True),
                _markdown_cell(files, preserve_breaks=True),
                _markdown_cell(elapsed_text),
                token_text,
            )
        )

    passed = sum(1 for row in records if row.get("status") == "done")
    failed = sum(1 for row in records if row.get("status") == "failed")
    skipped = sum(1 for row in records if row.get("status") == "skipped")
    total_tokens, unavailable_tokens = batch_token_totals(records)
    total_text = "{:,}".format(total_tokens) if total_tokens is not None else ""

    lines.extend([
        "",
        "- Approval policy: {}".format(_markdown_text(policy or "pause")),
        "- Fast mode: {}".format("ON" if fast_mode is True else "OFF"),
        "- Pre-run backup: `{}`".format(_markdown_code(backup_path)),
    ])
    if warning:
        lines.append("- Warning: {}".format(_markdown_text(warning)))
    if stopped and reason:
        lines.append("- Stop reason: {}".format(_markdown_text(reason)))
    lines.extend([
        "- Passed: {}; failed: {}; skipped: {}".format(
            passed, failed, skipped),
        "- Wall time: {:.1f}s".format(float(wall_time or 0.0)),
        "- Total tokens: {}".format(total_text),
        "- Auto-approved destructive operations: {}".format(
            len(auto_approvals or [])),
    ])
    if unavailable_tokens:
        lines.append("- Token usage unavailable for: {} task{}".format(
            unavailable_tokens, "" if unavailable_tokens == 1 else "s"))
    for index, approval in enumerate(auto_approvals or [], 1):
        reasons = "; ".join(str(item) for item in (
            approval.get("reasons") or [])) or "no scan reason"
        lines.append("  {}. {}".format(index, _markdown_text(reasons)))
    return "\n".join(lines)


def _created_layers(tool_reports, assistant_text):
    layers = []
    for report in tool_reports:
        layers.extend(_layers_from_text(report.get("result", "")))
    layers.extend(_layers_from_text(assistant_text))
    deduped = []
    positions = {}
    for layer in layers:
        name = str(layer.get("name") or "").strip(" `*_.,;:")
        count = _nonnegative_int(layer.get("count"))
        if not name:
            continue
        key = name.casefold()
        if key in positions:
            current = deduped[positions[key]]
            if current.get("count") is None and count is not None:
                current["count"] = count
            continue
        positions[key] = len(deduped)
        deduped.append({"name": name, "count": count})
    return deduped


def _layers_from_text(value):
    text = _visible_text(value)
    matches = []
    for match in re.finditer(
            r"(?im)^\s*(?:created|output)\s+layer\s*:\s*"
            r"[`*]*([^\r\n`*]+?)[`*]*\s*$", text):
        tail = text[match.end():match.end() + 500]
        count_match = re.search(
            r"(?im)^\s*feature\s+count\s*:\s*([0-9][0-9,]*)\s*$",
            tail)
        matches.append({
            "name": match.group(1).strip(),
            "count": _integer_text(count_match.group(1)) if count_match else None,
        })
    pattern = re.compile(
        r"(?is)(?:created\s+(?:layer\s+)?|added\s+(?:the\s+result\s+)?as\s+)"
        r"[`*]*([A-Za-z0-9_.() \-]+?)[`*]*\s*(?:\u2014|--?|:)\s*"
        r"([0-9][0-9,]*)\s+(?:[A-Za-z]+\s+){0,2}features?\b")
    for match in pattern.finditer(text):
        matches.append({
            "name": match.group(1).strip(),
            "count": _integer_text(match.group(2)),
        })
    return matches


def _exported_files(tool_reports, assistant_text):
    paths = []
    for report in tool_reports:
        name = str(report.get("name") or "").lower()
        if name.rsplit("__", 1)[-1] != "stat_path":
            continue
        args = report.get("args") if isinstance(report.get("args"), dict) else {}
        path = str(args.get("path") or "").strip()
        result = _visible_text(report.get("result", ""))
        exists = re.search(
            r"(?i)[\"']?exists[\"']?\s*[:=]\s*true\b", result)
        is_file = re.search(
            r"(?i)[\"']?is_file[\"']?\s*[:=]\s*true\b", result)
        if path and ntpath.isabs(path) and exists and is_file:
            paths.append(path)

    extension_group = "|".join(re.escape(item) for item in _EXPORT_EXTENSIONS)
    path_pattern = re.compile(
        r"(?i)((?:[A-Z]:[\\/]|\\\\)[^`<>\"|?\r\n]*?\."
        r"(?:" + extension_group + r"))(?=$|[\s`),;])")
    for line in _visible_text(assistant_text).splitlines():
        if re.search(
                r"(?i)\b(failed|failure|error|not\s+exported|could\s+not)\b",
                line):
            continue
        if not re.search(r"(?i)\b(exported|saved|wrote)\b", line):
            continue
        paths.extend(match.group(1).strip() for match in path_pattern.finditer(line))
    return _dedupe_paths(paths)


def _qa_verdict(assistant_text, events):
    started = set()
    completed = set()
    for event in events:
        name = _agent_name(event.get("name"))
        status = str(event.get("status") or "").lower()
        if status == "started":
            started.add(name)
        elif status == "finished" and name in started:
            completed.add(name)
    if "qa-verifier" not in completed:
        return ""
    matches = []
    for line in str(assistant_text or "").splitlines():
        match = re.search(
            r"(?i)\bqa[-_ ]verifier(?:'s)?\b[^\r\n]{0,80}"
            r"\bverdict\b\s*:?\s*\**(PASS|FAIL|UNVERIFIABLE)\b",
            line)
        if match:
            matches.append(match.group(1))
    return matches[-1].upper() if matches else ""


def _agent_name(value):
    name = str(value or "").strip().lower().replace("_", " ")
    name = re.sub(r"\s+", "-", name)
    return name


def _visible_text(value):
    text = str(value or "")
    return text.replace("\\r\\n", "\n").replace("\\n", "\n")


def _task_text(value):
    text = " ".join(str(value or "").split())
    return text if len(text) <= 100 else text[:97] + "..."


def _layer_text(layer):
    if not isinstance(layer, dict):
        return str(layer)
    name = str(layer.get("name") or "")
    count = _nonnegative_int(layer.get("count"))
    return "{} ({})".format(name, count) if count is not None else name


def _markdown_cell(value, preserve_breaks=False):
    text = str(value or "").replace("\\", "\\\\").replace("|", "\\|")
    text = text.replace("\r", " ").replace("\n", "<br>" if preserve_breaks else " ")
    return text


def _markdown_text(value):
    return str(value or "").replace("\r", " ").replace("\n", " ")


def _markdown_code(value):
    return str(value or "").replace("`", "\\`").replace("\r", " ").replace("\n", " ")


def _nonnegative_int(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if value < 0 or not math.isfinite(value) or not value.is_integer():
        return None
    return int(value)


def _integer_text(value):
    try:
        number = int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _dedupe_paths(paths):
    result = []
    seen = set()
    for path in paths:
        text = str(path or "").strip(" `.,;)")
        key = ntpath.normcase(ntpath.normpath(text))
        if text and ntpath.isabs(text) and key not in seen:
            seen.add(key)
            result.append(text)
    return result
