# -*- coding: utf-8 -*-
"""Read-only release/model checks. No installers, credentials or model calls."""
from __future__ import annotations

from configparser import ConfigParser
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from urllib.request import Request, urlopen

try:
    from .model_catalog import accepted_model_ids, clean_model_entries
    from .model_watch import check_models, cli_signature, new_model_ids, models_in_text
except ImportError:  # standalone checks
    from model_catalog import accepted_model_ids, clean_model_entries
    from model_watch import check_models, cli_signature, new_model_ids, models_in_text


CHECK_INTERVAL = 24 * 60 * 60
RETRY_INTERVAL = 60 * 60
MAX_DOWNLOAD = 2 * 1024 * 1024
SOURCES = {
    "qgent": "https://raw.githubusercontent.com/ljdstechva/qgent/main/metadata.txt",
    "claude": "https://api.github.com/repos/anthropics/claude-code/releases/latest",
    "codex": "https://api.github.com/repos/openai/codex/releases/latest",
}
LINKS = {
    "qgent": "https://github.com/ljdstechva/qgent#installation",
    "claude": "https://github.com/anthropics/claude-code/releases/latest",
    "codex": "https://github.com/openai/codex/releases/latest",
}
LABELS = {"qgent": "QGent", "claude": "Claude Code", "codex": "Codex CLI"}
_STATE_LOCK = threading.Lock()


def version_tuple(value):
    """Accept stable versions only; don't mistake a prerelease for an upgrade."""
    match = re.fullmatch(r"(?:rust-)?v?(\d+)\.(\d+)\.(\d+)", str(value).strip())
    if not match:
        raise ValueError("Unrecognized stable version")
    return tuple(int(part) for part in match.groups())


def metadata_version(text):
    parser = ConfigParser(interpolation=None)
    parser.read_string(text)
    value = parser.get("general", "version")
    version_tuple(value)
    return value


def _state_path(profile_dir):
    return Path(profile_dir) / "qgent" / "updates.json"


def load_state(profile_dir):
    try:
        state = json.loads(_state_path(profile_dir).read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("schema") != 1:
            return {}
        return state
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {"state_warning": "Saved update state could not be read; checking again."}


def save_state(profile_dir, state):
    path = _state_path(profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix="updates-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(state, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def fetch_text(url):
    # Only the fixed public sources above are requested. No account data is sent.
    request = Request(url, headers={"User-Agent": "QGent-update-check/1",
                                  "Accept": "application/vnd.github+json"})
    with urlopen(request, timeout=8) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("Update response exceeded the size limit")
    return data.decode("utf-8")


def run_cli(path, args, *, stdin_text=None):
    environment = os.environ.copy()
    if os.name == "nt":
        # QGIS's launcher trims PATH; npm CLI shims still need Node.js.
        node = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs"
        directories = [str(Path(path).resolve().parent)]
        if node.is_dir():
            directories.append(str(node))
        environment["PATH"] = os.pathsep.join(directories + [environment.get("PATH", "")])
    # Metadata discovery runs outside the project, with no user prompt.
    with tempfile.TemporaryDirectory(prefix="qgent-models-") as directory:
        result = subprocess.run(
            [str(path), *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15, check=False, env=environment,
            input=stdin_text, cwd=directory,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        # Don't persist raw stderr: a CLI may include account/config information.
        raise ValueError("CLI command failed (exit %s)" % result.returncode)
    return result.stdout


def installed_version(path, runner):
    text = runner(path, ["--version"]).strip()
    match = re.fullmatch(r"(?:codex-cli )?(\d+\.\d+\.\d+)(?: \(Claude Code\))?", text)
    if not match:
        raise ValueError("CLI did not return a recognized stable version")
    return match.group(1)


def codex_catalog(path, runner):
    """Recent Codex builds expose JSON; older builds retain the binary scan."""
    payload = json.loads(runner(path, ["debug", "models"]))
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ValueError("Unrecognized Codex model catalog")
    rows = payload["models"]
    if any(not isinstance(row, dict) or not isinstance(row.get("slug"), str)
           or not row["slug"] for row in rows):
        raise ValueError("Invalid Codex model catalog entry")
    hidden = {row["slug"] for row in rows if row.get("visibility") != "list"}
    # Structured slugs are authoritative: the fallback binary regex cannot
    # anticipate future families, multi-part suffixes or non-GPT model names.
    visible = {row["slug"] for row in rows if row.get("visibility") == "list"}
    entries = clean_model_entries([
        {"id": row["slug"], "label": row.get("display_name") or row["slug"]}
        for row in rows if row["slug"] in visible - hidden])
    return sorted(visible - hidden - set(accepted_model_ids("codex"))), hidden, entries


def claude_catalog(path, runner):
    """Use the Agent SDK initialize handshake, without an inference prompt.

    Protocol: anthropics/claude-agent-sdk-python, _internal/query.py.
    Safe mode disables hooks/plugins; no tools, MCP servers or session files.
    """
    request = {"type": "control_request", "request_id": "qgent-models",
               "request": {"subtype": "initialize", "hooks": {}}}
    text = runner(path, [
        "--print", "--input-format", "stream-json", "--output-format", "stream-json",
        "--verbose", "--no-session-persistence", "--safe-mode",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--tools", "", "--setting-sources", "",
    ], stdin_text=json.dumps(request) + "\n")
    for line in text.splitlines():
        payload = json.loads(line)
        if not isinstance(payload, dict) or payload.get("type") != "control_response":
            continue
        response = payload.get("response", {})
        if response.get("request_id") != "qgent-models":
            continue
        if response.get("subtype") != "success":
            raise ValueError("Claude model discovery failed")
        models = response.get("response", {}).get("models")
        if not isinstance(models, list) or not models:
            raise ValueError("Claude returned no structured model catalog")
        if any(not isinstance(row, dict) or not isinstance(row.get("value"), str)
               or not row["value"] for row in models):
            raise ValueError("Invalid Claude model catalog entry")
        # Do not retain the handshake's account details or any other fields.
        return clean_model_entries([
            {"id": row["value"], "label": row.get("displayName") or row["value"]}
            for row in models])
    raise ValueError("Claude returned no model initialization response")


def _dismissed(state):
    values = state.get("dismissed")
    return {item for item in values if isinstance(item, str)} if isinstance(values, list) else set()


def notice_ids(report):
    ids = [f"release:{name}:{row['latest']}" for name, row in report.get("releases", {}).items()
           if row.get("update")]
    ids.extend(f"model:{name}:{model}" for name, row in report.get("models", {}).items()
               for model in row.get("ids", []))
    return sorted(ids)


def dismiss(profile_dir, ids):
    with _STATE_LOCK:
        state = load_state(profile_dir)
        state["schema"] = 1
        state["dismissed"] = sorted(_dismissed(state) | set(ids))
        save_state(profile_dir, state)


def check_updates(profile_dir, plugin_dir, cli_paths, force=False, *,
                  fetcher=fetch_text, runner=run_cli, now=None, cancelled=lambda: False):
    """Return a cached/daily report; failed checks retry after one hour.

    Force bypasses the cache. Updating the plugin/catalog/CLI invalidates it.
    Each source fails independently so offline checks never mean 'up to date'.
    """
    now = time.time() if now is None else now
    state = load_state(profile_dir)
    current = metadata_version((Path(plugin_dir) / "metadata.txt").read_text(encoding="utf-8"))
    identity = hashlib.sha256(json.dumps([
        current, {name: cli_signature(cli_paths.get(name, "")) for name in ("claude", "codex")},
        {name: accepted_model_ids(name) for name in ("claude", "codex")},
    ], sort_keys=True).encode()).hexdigest()
    cached = state.get("report")
    if isinstance(cached, dict) and cached.get("identity") == identity:
        age = now - cached.get("checked_at", 0) if isinstance(cached.get("checked_at"), (int, float)) else -1
        interval = RETRY_INTERVAL if cached.get("errors") else CHECK_INTERVAL
        if not force and 0 <= age < interval:
            report = dict(cached, cached=True)
            report["notices"] = [item for item in notice_ids(report) if item not in _dismissed(state)]
            return report

    report = {"identity": identity, "checked_at": now, "cached": False,
              "releases": {}, "models": {}, "errors": []}
    if state.get("state_warning"):
        report["errors"].append(state["state_warning"])
    if cancelled():
        return None
    local = check_models(profile_dir, cli_paths, force=force)
    if local.get("state_error"):
        report["errors"].append(local["state_error"])
    for name in ("claude", "codex"):
        record = local["backends"][name]
        report["models"][name] = {"ids": record["new"], "source": "installed CLI strings (unconfirmed)"}
        if cli_paths.get(name) and record["error"]:
            report["errors"].append(f"{LABELS[name]} model scan: {record['error']}")

    for name, url in SOURCES.items():
        if cancelled():
            return None
        row = {"installed": current if name == "qgent" else "", "latest": "", "update": False,
               "error": "", "missing": name != "qgent" and not cli_paths.get(name)}
        report["releases"][name] = row
        if name != "qgent" and not row["missing"]:
            try:
                row["installed"] = installed_version(cli_paths[name], runner)
            except Exception as exc:
                row["error"] = f"Installed version check failed: {type(exc).__name__}."
        if cancelled():
            return None
        try:
            text = fetcher(url)
            if name == "qgent":
                row["latest"] = metadata_version(text)
            else:
                release = json.loads(text)
                if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
                    raise ValueError("Not a stable published release")
                row["latest"] = str(release["tag_name"])
                version_tuple(row["latest"])
                candidates = new_model_ids(name, models_in_text(name, str(release.get("body") or "")))
                report["models"][name]["release_ids"] = candidates
            if row["installed"]:
                row["update"] = version_tuple(row["latest"]) > version_tuple(row["installed"])
        except Exception as exc:
            row["error"] = (row["error"] + f" Upstream check failed: {type(exc).__name__}. Check your connection or try later.").strip()
        if row["error"]:
            report["errors"].append(f"{LABELS[name]}: {row['error']}")

    if cancelled():
        return None
    hidden_codex = set()
    if cli_paths.get("codex"):
        try:
            ids, hidden_codex, entries = codex_catalog(cli_paths["codex"], runner)
            report["models"]["codex"].update(ids=ids, entries=entries, source="Codex model catalog")
        except Exception as exc:
            report["errors"].append(
                f"Codex model catalog unavailable ({type(exc).__name__}); using unconfirmed CLI strings. "
                "Older Codex versions may need an update.")
    if cancelled():
        return None
    if cli_paths.get("claude"):
        try:
            entries = claude_catalog(cli_paths["claude"], runner)
            ids = sorted({row["id"] for row in entries} - set(accepted_model_ids("claude")))
            report["models"]["claude"].update(ids=ids, entries=entries, source="Claude Code model catalog")
        except Exception as exc:
            report["errors"].append(
                f"Claude model catalog unavailable ({type(exc).__name__}); "
                "keeping the saved model choices. CLI strings remain unconfirmed.")
    for name, row in report["models"].items():
        if name == "codex":
            row["release_ids"] = sorted(set(row.get("release_ids", [])) - hidden_codex)
        if "entries" not in row:
            row["ids"] = sorted(set(row["ids"]) | set(row.get("release_ids", [])))
        if row.get("release_ids") and "entries" not in row:
            row["source"] += " / release-note mentions"
    report["notices"] = [item for item in notice_ids(report) if item not in _dismissed(state)]
    if cancelled():
        return None
    # Serialize this transaction with message-bar dismissal on the GUI thread.
    # A re-read alone still allows a dismissal to be lost before os.replace.
    with _STATE_LOCK:
        state = {"schema": 1, "dismissed": sorted(_dismissed(load_state(profile_dir))), "report": report}
        report["notices"] = [item for item in notice_ids(report) if item not in _dismissed(state)]
        try:
            save_state(profile_dir, state)
        except OSError as exc:
            report["errors"].append(f"Could not save update results: {type(exc).__name__}.")
    return report


def report_text(report):
    if not report:
        return "No update check yet. Choose Check for updates."
    checked = datetime.fromtimestamp(report["checked_at"]).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = [f"Last checked: {checked}" + (" (cached)" if report.get("cached") else ""), ""]
    for name, row in report["releases"].items():
        installed = row["installed"] or ("not installed" if row.get("missing") else "unknown")
        latest = row["latest"] or "unknown"
        status = "Update available" if row["update"] else "No newer version found"
        if row["error"]:
            status = "Check incomplete"
        elif row.get("missing"):
            status = "Optional backend not installed"
        source = "GitHub main" if name == "qgent" else "latest release"
        lines.append(f"{LABELS[name]}: {installed} → {latest} ({source}) — {status}")
    lines.extend(["", "Model choices:"])
    for name, row in report["models"].items():
        if "entries" in row:
            labels = ", ".join(entry["label"] for entry in row["entries"])
            lines.append(f"{LABELS[name]} (CLI model catalog): {labels}")
        else:
            lines.append(f"{LABELS[name]} ({row['source']}): " + (", ".join(row["ids"]) or "none detected"))
        if row.get("release_ids"):
            lines.append("  Mentioned in latest release notes (unconfirmed): " + ", ".join(row["release_ids"]))
    lines.extend(["", "CLI catalog models are added to General → Models → Advanced automatically.",
                  "Saved model choices stay available after restart. Unconfirmed strings are not added.",
                  "Model discovery does not guarantee account access.",
                  "Existing model choices stay as selected. No software is installed by this check.",
                  "Use Install update and restart QGent above to install a newer QGent version."])
    if report["errors"]:
        lines.extend(["", "Checks needing attention:", *report["errors"]])
    return "\n".join(lines)
