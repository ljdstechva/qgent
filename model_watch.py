# -*- coding: utf-8 -*-
"""Detect models the installed CLIs know about but QGent's catalogue does not.

Neither Claude Code nor Codex CLI exposes a headless model list, so the model
ids are read straight out of the shipped executable.  That is deliberately a
*discovery* signal, not a catalogue: a hit only means "the CLI mentions this
id", so anything reported here is surfaced as a dismissible notice for a human
to confirm, never auto-selected.

Stdlib only, so the External Doctor can import it too.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re

try:
    from .model_catalog import MODEL_CATALOG, accepted_model_ids
except ImportError:  # standalone External Doctor import
    from model_catalog import MODEL_CATALOG, accepted_model_ids


SCHEMA_VERSION = 1
STATE_RELATIVE = ("qgent", "doctor", "model_watch.json")
# ponytail: 4 MiB chunks + a 64-byte overlap; raise the overlap only if a model
# id ever gets longer than that.
_CHUNK = 4 << 20
_OVERLAP = 64

_PATTERNS = {
    "claude": re.compile(rb"(?<![0-9a-z-])claude-[a-z]{3,12}-[0-9][0-9a-z.-]{0,24}"),
    "codex": re.compile(rb"(?<![0-9a-z.-])gpt-[0-9][0-9.]{0,8}(?:-[a-z]{2,16})?"),
}
# Adjacent strings in the executables run together with no separator, so a
# match can pick up the start of the next one.
_GLUED_TOKENS = ("openai", "gpt", "anthropic", "claude")
_TRAILING_JUNK = re.compile(r"(-v[0-9]+|-fast|-[0-9]{8})$")
_CLAUDE_ID = re.compile(r"^claude-(?P<family>[a-z]{3,12})-(?P<version>[0-9]+(?:[.-][0-9]+)*)$")
_CODEX_ID = re.compile(r"^gpt-(?P<version>[0-9]+(?:\.[0-9]+)*)(?:-(?P<family>[a-z]+))?$")


def _strip_glued(word):
    """Peel a run-together neighbour off the end of a variant name."""
    text = str(word or "")
    changed = True
    while changed:
        changed = False
        for token in _GLUED_TOKENS:
            if len(text) > len(token) and text.endswith(token):
                text = text[: -len(token)]
                changed = True
    return "" if any(token in text for token in _GLUED_TOKENS) else text


def parse_model_id(backend, value):
    """Return ``(canonical id, family, version tuple)`` or ``None``."""
    text = str(value or "").strip().lower()
    previous = None
    while text != previous:
        previous = text
        text = _TRAILING_JUNK.sub("", text)
    if str(backend).lower() == "claude":
        match = _CLAUDE_ID.match(text)
        family = match.group("family") if match else ""
    else:
        match = _CODEX_ID.match(text)
        raw_family = (match.group("family") or "") if match else ""
        family = _strip_glued(raw_family)
        if raw_family and not family:
            return None  # the whole variant name was a glued-on neighbour
        if family != raw_family:
            text = f"gpt-{match.group('version')}-{family}"
    if not match:
        return None
    parts = [int(item) for item in re.split(r"[.-]", match.group("version"))]
    # Dated snapshots (…-20251001) and build stamps are not version numbers.
    if any(part >= 1000 for part in parts):
        return None
    return text, family, tuple(parts)


def scan_cli_models(path, backend):
    """Return every plausible model id mentioned by a CLI executable."""
    pattern = _PATTERNS.get(str(backend).lower())
    if pattern is None or not path or not Path(path).is_file():
        return set()
    found = set()
    tail = b""
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            buffer = tail + chunk
            for match in pattern.finditer(buffer):
                parsed = parse_model_id(backend, match.group(0).decode("ascii"))
                if parsed:
                    found.add(parsed[0])
            tail = buffer[-_OVERLAP:]
    return found


def known_models(backend):
    """Return ``{family: max version}`` for everything the catalogue offers."""
    families = {}
    for value in accepted_model_ids(backend):
        parsed = parse_model_id(backend, value)
        if not parsed:
            continue
        _canonical, family, version = parsed
        families[family] = max(version, families.get(family, ()))
    return families


def new_model_ids(backend, discovered):
    """Report only ids at least as new as the newest model already offered.

    A brand-new family (Fable after Opus) counts, an older one (GPT-5.4) does
    not, and neither do the retired snapshots every CLI still carries strings
    for.
    """
    families = known_models(backend)
    if not families:
        return []
    ceiling = max(families.values())
    fresh = []
    for value in discovered:
        parsed = parse_model_id(backend, value)
        if not parsed:
            continue
        canonical, family, version = parsed
        if family in families:
            if version > families[family]:
                fresh.append(canonical)
        elif version > ceiling or (family and version >= ceiling):
            # A variant-less id at the current version ("gpt-5.6") is a prefix
            # fragment of a listed model, not a model.
            fresh.append(canonical)
    return sorted(set(fresh))


def cli_signature(path):
    """Cheap identity for a CLI build, so a rescan only follows an update."""
    try:
        stat = os.stat(str(path))
    except OSError:
        return ""
    return f"{Path(path).resolve()}|{stat.st_size}|{stat.st_mtime_ns}"


def _state_path(profile_dir):
    return Path(profile_dir).resolve().joinpath(*STATE_RELATIVE)


def load_state(profile_dir):
    try:
        payload = json.loads(_state_path(profile_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("scans", {})
    payload.setdefault("acknowledged", [])
    return payload


def save_state(profile_dir, state):
    path = _state_path(profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def check_models(profile_dir, cli_paths, force=False):
    """Scan each available CLI (cached per build) and report unseen models."""
    state = load_state(profile_dir)
    scans = state["scans"] if isinstance(state.get("scans"), dict) else {}
    acknowledged = {str(item) for item in state.get("acknowledged") or []}
    backends = {}
    for backend in ("claude", "codex"):
        path = str((cli_paths or {}).get(backend) or "")
        signature = cli_signature(path) if path else ""
        record = {"cli_path": path, "signature": signature, "scanned": False,
                  "new": [], "error": ""}
        cached = scans.get(backend) if isinstance(scans.get(backend), dict) else {}
        if not signature:
            record["error"] = "CLI not found."
        elif not force and cached.get("signature") == signature:
            record["new"] = list(cached.get("new") or [])
        else:
            try:
                record["new"] = new_model_ids(
                    backend, scan_cli_models(path, backend))
                record["scanned"] = True
            except OSError as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
        if not record["error"]:
            scans[backend] = {"signature": signature, "new": record["new"]}
        backends[backend] = record

    all_new = sorted({value for item in backends.values() for value in item["new"]})
    state["scans"] = scans
    state["schema_version"] = SCHEMA_VERSION
    state["last_check"] = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        save_state(profile_dir, state)
    except OSError:
        pass
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": state["last_check"],
        "backends": backends,
        "all_new": all_new,
        "new": [value for value in all_new if value not in acknowledged],
    }


def acknowledge(profile_dir, model_ids):
    """Remember ids the user dismissed so the notice does not reappear."""
    state = load_state(profile_dir)
    merged = sorted({str(item) for item in state.get("acknowledged") or []}
                    | {str(item) for item in model_ids or []})
    state["acknowledged"] = merged
    save_state(profile_dir, state)
    return merged


def summary_text(report):
    """One-line human summary used by the Doctor and the message bar."""
    report = report or {}
    if report.get("new"):
        return ("Models the installed CLIs offer but QGent does not list yet: "
                + ", ".join(report["new"])
                + ". Add them in model_catalog.py, or dismiss this notice.")
    errors = [f"{backend}: {item['error']}"
              for backend, item in (report.get("backends") or {}).items()
              if item.get("error")]
    listed = sum(len(MODEL_CATALOG.get(backend, ()))
                 for backend in MODEL_CATALOG)
    if errors:
        return f"No new models found ({listed} listed). " + "; ".join(errors)
    return f"No new models found; QGent's {listed} listed models are current."
