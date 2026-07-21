# -*- coding: utf-8 -*-
"""Conversation-history export to Markdown and PDF.

The record-to-Markdown path is stdlib-only so it can be golden-tested without
QGIS. Qt imports are intentionally local to :func:`write_pdf`.
"""
from __future__ import annotations

import configparser
from datetime import date, datetime
import html
import json
import os
from pathlib import Path
import re
import tempfile


IN_PROGRESS_NOTE = "exported while a turn was in progress"


def read_history_jsonl(path):
    """Read every complete history record without modifying the JSONL file."""
    history_path = Path(path)
    if not history_path.is_file():
        return {"session": {}, "records": []}
    text = history_path.read_text(encoding="utf-8")
    session = {}
    records = []
    physical_lines = text.splitlines()
    for index, line in enumerate(physical_lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if index == len(physical_lines) - 1 and not text.endswith("\n"):
                break
            raise ValueError(
                "History line {} is not valid JSON.".format(index + 1))
        if not isinstance(record, dict) or not record.get("kind"):
            raise ValueError(
                "History line {} is not a record object.".format(index + 1))
        if record.get("kind") == "session":
            session = dict(record)
        else:
            records.append(record)
    return {"session": session, "records": records}


def plugin_version(plugin_dir):
    """Return the installed metadata version without importing QGIS."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path(plugin_dir) / "metadata.txt", encoding="utf-8")
    return parser.get("general", "version", fallback="unknown").strip()


def default_export_name(project_path, project_title, extension, today=None):
    """Build the contract filename using a Windows-safe project slug."""
    project_path = str(project_path or "").strip()
    project_title = str(project_title or "").strip()
    raw = Path(project_path).stem if project_path else project_title or "unsaved"
    slug = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", raw)
    slug = re.sub(r"\s+", "_", slug).strip(" ._") or "unsaved"
    slug = re.sub(r"_+", "_", slug)
    slug = slug[:80].rstrip(" ._") or "unsaved"
    day = today or date.today()
    suffix = str(extension or "").lower().lstrip(".")
    if suffix not in {"md", "pdf"}:
        raise ValueError("Export extension must be md or pdf.")
    return "qgent_chat_{}_{}.{}".format(slug, day.isoformat(), suffix)


def render_markdown(records, metadata, exported_at=None, in_progress=False):
    """Convert chronological Goal 3 history records to one Markdown document."""
    records = [dict(record) for record in (records or [])]
    meta = dict(metadata or {})
    exported = exported_at or datetime.now().astimezone()
    exported_text = _format_exported(exported)
    first_stamp, last_stamp = _date_range(records)

    lines = [
        "# QGent Chat Export",
        "",
        "| Field | Value |",
        "| --- | --- |",
        "| Project | {} |".format(_table(meta.get("project_name") or "unsaved")),
        "| Project path | {} |".format(
            _table(meta.get("project_path") or "(unsaved)")),
        "| Exported | {} |".format(_table(exported_text)),
        "| Backend | {} |".format(_table(meta.get("backend") or "unknown")),
        "| Model | {} |".format(_table(meta.get("model") or "unknown")),
        "| QGent version | {} |".format(
            _table(meta.get("version") or "unknown")),
        "| Chat date range | {} |".format(
            _table(_range_text(first_stamp, last_stamp))),
        "",
        "---",
        "",
    ]

    tool_finishes = {}
    approval_decisions = {}
    for index, record in enumerate(records):
        if record.get("kind") == "tool" and record.get("event") == "finished":
            tool_finishes[str(record.get("tool_id") or "legacy-{}".format(index))] = record
        if (record.get("kind") == "approval"
                and record.get("event") == "decided"):
            approval_decisions[
                str(record.get("approval_id") or "legacy-{}".format(index))
            ] = record

    rendered_tools = set()
    rendered_approvals = set()
    for index, record in enumerate(records):
        kind = str(record.get("kind") or "")
        stamp = _clock(record.get("t"))
        if kind == "user":
            lines.extend(["**You** - {}".format(stamp), ""])
            tags = record.get("tags") or []
            if tags:
                lines.extend([
                    "Attachments: " + ", ".join(str(tag) for tag in tags), "",
                ])
            lines.extend([_stored_text(record.get("text", "")), "", "---", ""])
        elif kind == "assistant":
            role = "QGent status" if record.get("style") == "status" else "QGent"
            lines.extend([
                "**{}** - {}".format(role, stamp), "",
                _stored_text(record.get("text", "")), "", "---", "",
            ])
        elif kind == "error":
            lines.extend([
                "> **Error - {}**".format(stamp), ">",
                *_quote_lines(_stored_text(record.get("text", ""))),
                "", "---", "",
            ])
        elif kind == "tool":
            tool_id = str(record.get("tool_id") or "legacy-{}".format(index))
            if tool_id in rendered_tools:
                continue
            if record.get("event") == "finished" and tool_id not in tool_finishes:
                tool_finishes[tool_id] = record
            finish = tool_finishes.get(tool_id) or {}
            start = record if record.get("event") != "finished" else {}
            source = start or finish
            result = finish.get("result", "(turn interrupted before result)")
            lines.extend(_tool_block(
                source.get("name") or finish.get("name") or "tool",
                stamp,
                source.get("args", finish.get("args", {})),
                result,
            ))
            lines.extend(["", "---", ""])
            rendered_tools.add(tool_id)
        elif kind == "subagent":
            event = str(record.get("event") or "event")
            elapsed = record.get("elapsed_s")
            suffix = " ({:.3f} s)".format(float(elapsed)) if (
                event == "finished" and isinstance(elapsed, (int, float))) else ""
            lines.extend([
                "**Subagent {}** - {}{} - {}".format(
                    _inline(record.get("name") or "subagent"), event, suffix, stamp),
                "",
            ])
        elif kind == "approval":
            approval_id = str(
                record.get("approval_id") or "legacy-{}".format(index))
            if approval_id in rendered_approvals:
                continue
            decision = approval_decisions.get(approval_id) or {}
            source = record if record.get("event") != "decided" else decision
            approved = decision.get("approved") if decision else None
            outcome = "Approved" if approved is True else (
                "Denied" if approved is False else "Pending")
            reasons = decision.get("reasons", source.get("reasons", [])) or []
            code = decision.get("code", source.get("code", ""))
            lines.extend(_approval_block(outcome, stamp, reasons, code))
            lines.extend(["", "---", ""])
            rendered_approvals.add(approval_id)
        elif kind == "queue":
            lines.extend(_queue_event_block(record, stamp))
            lines.extend(["", "---", ""])
        elif kind == "queue_summary":
            lines.extend([
                "**QGent batch summary** - {}".format(stamp), "",
                _stored_text(record.get("text", "")), "", "---", "",
            ])
        else:
            lines.extend([
                "> **Info: {} - {}**".format(_inline(kind or "record"), stamp),
                ">",
                *_quote_fence(json.dumps(record, ensure_ascii=False, indent=2), "json"),
                "", "---", "",
            ])

    if in_progress:
        lines.extend([
            "> **Note:** {}".format(IN_PROGRESS_NOTE), "",
        ])
    lines.extend([
        "*Exported from QGent {}*".format(meta.get("version") or "unknown"),
        "",
    ])
    return "\n".join(lines)


def write_markdown(path, markdown):
    """Atomically write UTF-8 Markdown and return its absolute path."""
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_bytes(target, str(markdown).encode("utf-8"))
    return str(target)


def write_pdf(path, markdown, title="QGent Chat Export"):
    """Render Markdown to an A4 PDF with Qt only and return its absolute path."""
    from qgis.PyQt.QtCore import QMarginsF, QSizeF
    from qgis.PyQt.QtGui import (
        QFont, QFontDatabase, QPageLayout, QPageSize, QPdfWriter,
        QTextDocument,
    )

    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".qgent-export-", suffix=".pdf", dir=str(target.parent))
    os.close(fd)
    try:
        writer = QPdfWriter(temporary)
        writer.setResolution(144)
        writer.setTitle(str(title))
        writer.setCreator("QGent")
        layout = QPageLayout(
            QPageSize(QPageSize.A4), QPageLayout.Portrait,
            QMarginsF(15, 15, 15, 15), QPageLayout.Millimeter,
        )
        writer.setPageLayout(layout)

        # Headless/offscreen QGIS can start with an empty Qt font database,
        # which otherwise turns every PDF glyph into an unsearchable square.
        database = QFontDatabase()
        windows_fonts = Path(
            os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates = (
            windows_fonts / "arial.ttf",
            windows_fonts / "segoeui.ttf",
            windows_fonts / "seguisym.ttf",
            windows_fonts / "seguiemj.ttf",
        )
        for candidate in candidates:
            if candidate.is_file():
                QFontDatabase.addApplicationFont(str(candidate))
        families = set(database.families())
        family = "Arial" if "Arial" in families else (
            "Segoe UI" if "Segoe UI" in families else "Sans Serif")

        document = QTextDocument()
        document.setDefaultFont(QFont(family, 10))
        document.setDefaultStyleSheet(
            "body { color: #18222c; } "
            "pre, code { font-family: 'Consolas', 'Courier New', monospace; "
            "font-size: 9pt; white-space: pre-wrap; } "
            "blockquote { color: #263746; border-left: 3px solid #4f7cac; "
            "padding-left: 8px; } "
            "table { border-collapse: collapse; } "
            "th, td { padding: 3px 6px; }"
        )
        document.setMarkdown(str(markdown))
        paint_rect = writer.pageLayout().paintRectPixels(writer.resolution())
        document.setPageSize(QSizeF(paint_rect.width(), paint_rect.height()))
        document.print_(writer)
        del writer
        if not os.path.isfile(temporary) or os.path.getsize(temporary) == 0:
            raise OSError("Qt produced an empty PDF.")
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return str(target)


def _atomic_bytes(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".qgent-export-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _tool_block(name, stamp, arguments, result):
    lines = [
        "> **Tool `{}` - {}**".format(_inline(name), stamp),
        ">", "> **Arguments**", ">",
        *_quote_fence(_stored_text(arguments), "json"),
        ">", "> **Result**", ">",
        *_quote_fence(_stored_text(result), "text"),
    ]
    return lines


def _approval_block(outcome, stamp, reasons, code):
    lines = [
        "> **Approval - {} - {}**".format(outcome, stamp),
        ">", "> **Reasons**",
    ]
    if reasons:
        lines.extend("> - " + str(reason).replace("\n", " ") for reason in reasons)
    else:
        lines.append("> - (none recorded)")
    lines.extend([">", "> **Code**", ">"])
    lines.extend(_quote_fence(_stored_text(code), "python"))
    return lines


def _queue_event_block(record, stamp):
    event = str(record.get("event") or "event").replace("_", " ").title()
    lines = ["> **Queue - {} - {}**".format(_inline(event), stamp)]
    details = []
    if record.get("task_id"):
        details.append("Task ID: `{}`".format(_inline(record.get("task_id"))))
    if record.get("text"):
        details.append("Task: {}".format(_one_line(record.get("text"))))
    for key, label in (
            ("message", "Message"), ("error", "Error"),
            ("warning", "Warning"), ("reason", "Reason"),
            ("policy", "Policy"), ("backup_path", "Backup"),
            ("verdict", "Verdict")):
        if record.get(key):
            details.append("{}: {}".format(label, _one_line(record.get(key))))
    if isinstance(record.get("elapsed_s"), (int, float)):
        details.append("Elapsed: {:.3f} s".format(float(record["elapsed_s"])))
    if (isinstance(record.get("tokens"), int)
            and not isinstance(record.get("tokens"), bool)):
        details.append("Tokens: {:,}".format(record["tokens"]))
    layers = record.get("layers_created") or []
    if layers:
        values = []
        for layer in layers:
            if isinstance(layer, dict):
                name = str(layer.get("name") or "")
                count = layer.get("count")
                values.append("{} ({})".format(name, count)
                              if isinstance(count, int) else name)
            else:
                values.append(str(layer))
        details.append("Layers created: " + ", ".join(values))
    files = record.get("files_exported") or []
    if files:
        details.append("Files exported: " + ", ".join(str(item) for item in files))
    reasons = record.get("reasons") or []
    if reasons:
        details.append("Approval reasons: " + "; ".join(str(item) for item in reasons))
    channels = record.get("channels") or []
    if channels:
        details.append("Notification channels: " + ", ".join(
            str(item) for item in channels))
    if not details:
        details.append("Event recorded.")
    lines.extend("> - " + detail for detail in details)
    return lines


def _quote_fence(value, language):
    text = str(value)
    runs = [len(match.group(0)) for match in re.finditer(r"`+", text)]
    fence = "`" * max(3, (max(runs) + 1) if runs else 3)
    return [
        "> {}{}".format(fence, language),
        *_quote_lines(text),
        "> " + fence,
    ]


def _quote_lines(value):
    lines = str(value).splitlines() or [""]
    return ["> " + line for line in lines]


def _stored_text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)


def _one_line(value):
    return " ".join(str(value or "").split())


def _inline(value):
    return html.escape(str(value), quote=False).replace("`", "\\`")


def _table(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _clock(value):
    text = str(value or "")
    try:
        return datetime.fromisoformat(text).strftime("%H:%M")
    except ValueError:
        return text[11:16] if len(text) >= 16 else "--:--"


def _date_range(records):
    stamps = [str(record.get("t")) for record in records if record.get("t")]
    return (stamps[0], stamps[-1]) if stamps else ("", "")


def _range_text(first, last):
    if not first:
        return "(empty)"
    return "{} - {}".format(_format_stamp(first), _format_stamp(last))


def _format_stamp(value):
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.strftime("%Y-%m-%d %H:%M %z")
    except ValueError:
        return str(value)


def _format_exported(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)
