# -*- coding: utf-8 -*-
"""Incremental parser for Claude Code ``--output-format stream-json``.

Feed raw stdout bytes as they arrive; ``feed`` yields fully-parsed JSON event
dicts (one per newline-delimited line). Unknown/partial lines are buffered.
Kept tolerant of unknown event types so a CLI upgrade can't crash the parser.
"""
import json


class StreamJsonParser:
    def __init__(self):
        self._buf = ""

    def feed(self, chunk_text):
        self._buf += chunk_text
        events = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # Partial/garbled line: if it doesn't look like the start of
                # JSON, drop it; otherwise put it back to await more bytes.
                if line.startswith("{"):
                    self._buf = line + "\n" + self._buf
                    break
        return events

    def reset(self):
        self._buf = ""
