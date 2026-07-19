# -*- coding: utf-8 -*-
"""Crash-tolerant, per-project JSONL conversation history for QGent.

This module deliberately has no QGIS imports so storage behavior can be tested
with an isolated settings directory. The dock supplies
``QgsApplication.qgisSettingsDirPath()`` and the current project filename.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile


LOG = logging.getLogger("QGent.history")
RESTORE_LIMIT = 500
MAX_TEXT_CHARS = 4000


def project_key(project_filename):
    """Return the contract-defined key for a saved or unsaved project."""
    filename = str(project_filename or "").strip()
    if not filename:
        return "unsaved"
    normalized = os.path.normcase(os.path.abspath(filename))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def clipped_text(value, limit=MAX_TEXT_CHARS):
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[+{len(text) - limit} chars]"


def bounded_json_value(value, limit=MAX_TEXT_CHARS):
    """Return a JSON-safe value, replacing oversized payloads with a preview."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return clipped_text(value, limit)
    if len(encoded) <= limit:
        return value
    return {
        "truncated": True,
        "preview": encoded[:limit],
        "omitted_chars": len(encoded) - limit,
    }


def json_safe_value(value):
    """Preserve normal payloads verbatim and stringify unsupported objects."""
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
    return value


class HistoryStore:
    """Append events and restore the most recent records for one project."""

    def __init__(self, settings_dir, project_filename, restore_limit=RESTORE_LIMIT):
        self.settings_dir = Path(settings_dir).resolve()
        self.history_dir = self.settings_dir / "qgent" / "history"
        self.key = project_key(project_filename)
        self.path = self.history_dir / f"{self.key}.jsonl"
        self.restore_limit = max(1, int(restore_limit))
        self.session = {"backend": "", "model": "", "session_id": None}
        self.last_warning = ""
        self.last_corrupt_path = None
        self.last_archived_path = None
        self.truncated_tail_ignored = False

    def load(self):
        """Validate the file and return its session header and capped events."""
        self.last_warning = ""
        self.last_corrupt_path = None
        self.truncated_tail_ignored = False
        records = self._read_all(quarantine=True)
        if records is None:
            return {"session": dict(self.session), "records": [],
                    "path": str(self.path), "warning": self.last_warning,
                    "corrupt_path": self._corrupt_path_text(),
                    "truncated_tail_ignored": False}

        session = None
        events = deque(maxlen=self.restore_limit)
        for record in records:
            if record.get("kind") == "session":
                session = record
            else:
                events.append(record)
        if session is not None:
            self.session = {
                "backend": str(session.get("backend") or ""),
                "model": str(session.get("model") or ""),
                "session_id": session.get("session_id") or None,
            }
        return {
            "session": dict(self.session),
            "records": list(events),
            "path": str(self.path),
            "warning": self.last_warning,
            "corrupt_path": self._corrupt_path_text(),
            "truncated_tail_ignored": self.truncated_tail_ignored,
        }

    def append(self, kind, **fields):
        """Append and fsync one rendered event, creating the header first."""
        self.history_dir.mkdir(parents=True, exist_ok=True)
        record = {"t": _iso_now(), "kind": str(kind)}
        for key, value in fields.items():
            record[str(key)] = json_safe_value(value)

        new_file = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            if new_file:
                handle.write(self._json_line(self._session_record()))
            handle.write(self._json_line(record))
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def update_session(self, backend, model, session_id):
        """Rewrite only the session header while preserving every event."""
        self.session = {
            "backend": str(backend or ""),
            "model": str(model or ""),
            "session_id": session_id or None,
        }
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        records = self._read_all(quarantine=True)
        if records is None:
            return
        events = [record for record in records if record.get("kind") != "session"]
        self.history_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.history_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(self._json_line(self._session_record()))
                for record in events:
                    handle.write(self._json_line(record))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def delete(self):
        """Delete the current project's active history file, if present."""
        self.last_archived_path = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            target = Path(str(self.path) + f".cleared-{stamp}")
            os.replace(self.path, target)
            self.last_archived_path = target
        self.session = {"backend": "", "model": "", "session_id": None}
        return self.last_archived_path

    def _read_all(self, quarantine):
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            self._record_corrupt(f"Unreadable history: {type(exc).__name__}: {exc}",
                                 quarantine)
            return None

        physical_lines = text.splitlines()
        records = []
        for index, line in enumerate(physical_lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeError) as exc:
                is_truncated_tail = (
                    index == len(physical_lines) - 1 and not text.endswith("\n"))
                if is_truncated_tail:
                    self.truncated_tail_ignored = True
                    self.last_warning = "Ignored a truncated trailing history line."
                    LOG.warning(self.last_warning)
                    break
                self._record_corrupt(
                    f"Corrupt history line {index + 1}: {type(exc).__name__}: {exc}",
                    quarantine)
                return None
            if not isinstance(record, dict) or not record.get("kind"):
                self._record_corrupt(
                    f"Corrupt history line {index + 1}: expected an object with kind.",
                    quarantine)
                return None
            records.append(record)
        return records

    def _record_corrupt(self, message, quarantine):
        self.last_warning = message
        LOG.warning(message)
        if not quarantine or not self.path.exists():
            return
        target = Path(str(self.path) + ".corrupt")
        suffix = 1
        while target.exists():
            target = Path(str(self.path) + f".corrupt.{suffix}")
            suffix += 1
        try:
            os.replace(self.path, target)
            self.last_corrupt_path = target
        except OSError as exc:
            self.last_warning += f" Quarantine failed: {type(exc).__name__}: {exc}"
            LOG.warning(self.last_warning)

    def _session_record(self):
        return {"kind": "session", **self.session}

    @staticmethod
    def _json_line(record):
        return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"

    def _corrupt_path_text(self):
        return str(self.last_corrupt_path) if self.last_corrupt_path else ""


def _iso_now():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")
