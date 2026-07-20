#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QGent External Doctor: detached, stdlib-only recovery console."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys

try:  # Direct script launch from qgent-doctor.bat.
    from doctor_core import (
        AuditLog, QGIS_EXECUTABLE_NAMES, RepairWorkspace, atomic_write_json,
        clear_bytecode_caches, detect_cli_paths, ensure_recovery_entrypoint,
        external_diagnostics, launch_detached, list_backups, probe_cli,
        qgis_processes, require_no_qgis,
        quarantine_corrupt_histories, redact, refresh_template_state,
        render_diagnostics, run, wait_for_qgis_closed,
        write_detected_cli_state,
    )
    from model_catalog import repair_model_specs
except ImportError:  # Package import used by tests.
    from .doctor_core import (
        AuditLog, QGIS_EXECUTABLE_NAMES, RepairWorkspace, atomic_write_json,
        clear_bytecode_caches, detect_cli_paths, ensure_recovery_entrypoint,
        external_diagnostics, launch_detached, list_backups, probe_cli,
        qgis_processes, require_no_qgis,
        quarantine_corrupt_histories, redact, refresh_template_state,
        render_diagnostics, run, wait_for_qgis_closed,
        write_detected_cli_state,
    )
    from .model_catalog import repair_model_specs


EXIT_OK = 0
EXIT_DIAGNOSTIC_FAILURE = 2
EXIT_CANCELLED = 130


def _json_events(text):
    events = []
    for line in str(text or "").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _repair_prompt(diagnostics_bundle, user_description):
    return (
        "You are repairing the QGent QGIS plugin in a disposable copy.\n"
        "Work ONLY inside the current working directory. Do not access or "
        "modify the installed plugin, source repository, user profile, or "
        "any path outside this copy. Do not create commits.\n"
        "Inspect the evidence, make the smallest correct code/configuration "
        "change in this copy, and run focused checks when useful. Never "
        "create or modify claude_runtime/mcp-config.json.\n"
        "In the final response, summarize the diagnosis, changed files, and "
        "verification; the External Doctor computes the actual diff.\n\n"
        "DIAGNOSTICS BUNDLE\n==================\n"
        f"{diagnostics_bundle or '(diagnostics unavailable)'}\n\n"
        "USER DESCRIPTION\n================\n"
        f"{str(user_description or '').strip() or '(none)'}\n")


def _repair_command(spec, workdir):
    backend = str(spec.get("backend") or "")
    model = str(spec.get("model") or "")
    cli_path = str(spec.get("cli_path") or "")
    if not cli_path or not model:
        raise ValueError("CLI path and model are required")
    executable_name = Path(cli_path).name.casefold()
    if backend == "codex":
        if executable_name not in {"codex.exe", "codex.cmd", "codex.bat"}:
            raise ValueError("Resolved Codex repair executable has an invalid name")
        effort = str(spec.get("effort") or "xhigh")
        args = [
            "-a", "never", "exec", "-s", "workspace-write",
            "-C", str(workdir), "-c", 'windows.sandbox="elevated"',
            "--disable", "unified_exec", "--model", model,
            "-c", f'model_reasoning_effort="{effort}"',
            "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
            "--ignore-rules", "--json", "-",
        ]
    elif backend == "claude":
        if executable_name not in {"claude.exe", "claude.cmd", "claude.bat"}:
            raise ValueError("Resolved Claude repair executable has an invalid name")
        effort = str(spec.get("effort") or "max")
        args = [
            "-p", "--safe-mode", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages",
            "--no-session-persistence", "--model", model,
            "--effort", effort, "--permission-mode", "acceptEdits",
            "--allowedTools", "Read,Glob,Grep,Edit,Write",
            "--disallowedTools", "Bash,WebFetch,WebSearch",
        ]
    else:
        raise ValueError(f"Unsupported repair backend: {backend!r}")
    return cli_path, args


def _parse_repair_result(backend, stdout, stderr, returncode):
    events = _json_events(stdout)
    explanation = []
    completed = False
    failure = ""
    if backend == "claude":
        saw_delta = False
        for event in events:
            event_type = event.get("type")
            if event_type == "stream_event":
                inner = event.get("event") or {}
                delta = inner.get("delta") or {}
                if (inner.get("type") == "content_block_delta"
                        and delta.get("type") == "text_delta"):
                    text = str(delta.get("text") or "")
                    if text:
                        saw_delta = True
                        explanation.append(text)
            elif event_type == "assistant" and not saw_delta:
                for block in (event.get("message") or {}).get("content", []):
                    if block.get("type") == "text" and block.get("text"):
                        explanation.append(str(block["text"]))
            elif event_type == "result":
                completed = not bool(event.get("is_error"))
                if event.get("is_error"):
                    failure = str(event.get("result") or "")
                elif not explanation and event.get("result"):
                    explanation.append(str(event["result"]))
    else:
        for event in events:
            event_type = event.get("type")
            if event_type in ("item.completed", "item.updated"):
                item = event.get("item") or {}
                if (item.get("type") in ("agent_message", "assistant_message")
                        and event_type == "item.completed"):
                    text = str(item.get("text") or item.get("content") or "")
                    if text:
                        explanation.append(text)
            elif event_type in ("turn.completed", "thread.completed"):
                completed = True
            elif event_type in ("turn.failed", "error"):
                failure = json.dumps(event, ensure_ascii=False)
    if returncode != 0 or not completed or failure:
        message = failure or stderr.strip() or stdout.strip()
        raise RuntimeError(message or f"Repair CLI exited with code {returncode}")
    return "".join(explanation).strip(), len(events)


class ExternalDoctor:
    def __init__(self, profile_dir, plugin_dir, source_repo, request_path=None,
                 input_func=input, output_func=print):
        self.profile_dir = Path(profile_dir).resolve()
        self.plugin_dir = Path(plugin_dir).resolve()
        self.source_repo = Path(source_repo).resolve() if source_repo else None
        self.request_path = (Path(request_path).resolve() if request_path else
                             self.profile_dir / "qgent" / "doctor" /
                             "doctor_request.json")
        self.input = input_func
        self.output = output_func
        self.audit = AuditLog(self.profile_dir)
        self.request = self._load_request()
        self.last_diagnostics = None

    def _load_request(self):
        if not self.request_path.is_file():
            return {}
        try:
            value = json.loads(self.request_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self.audit.append("handoff.load", "FAIL", error=str(exc))
            return {}

    def _ensure_recovery_bat(self):
        return ensure_recovery_entrypoint(
            self.profile_dir, self.plugin_dir, sys.executable,
            self.source_repo or "")

    def _handoff_summary(self):
        if not self.request:
            return
        repair = self.request.get("repair") or {}
        self.output("Loaded in-app Doctor handoff:")
        self.output(f"  Description: {self.request.get('user_description') or '(none)'}")
        self.output(
            f"  Repair model: {repair.get('backend') or 'unknown'} / "
            f"{repair.get('model') or 'unknown'} / "
            f"{repair.get('effort') or 'default'}")
        self.audit.append(
            "handoff.consume", model=repair.get("model"),
            backend=repair.get("backend"), request=str(self.request_path))

    def diagnose(self):
        self.output("\nRunning External Doctor diagnostics...")
        report = external_diagnostics(
            self.plugin_dir, self.profile_dir, self.request,
            python_executable=sys.executable)
        self.last_diagnostics = report
        text = render_diagnostics(report)
        self.output(text)
        target = (self.profile_dir / "qgent" / "doctor" /
                  "latest_diagnostics.json")
        atomic_write_json(target, report)
        self.audit.append(
            "diagnose", "PASS" if report["all_pass"] else "FAIL",
            checks=len(report["checks"]), evidence=str(target))
        return report

    def _require_qgis_closed(self):
        result = wait_for_qgis_closed(
            input_func=self.input, output_func=self.output,
            process_provider=qgis_processes)
        self.audit.append(
            "qgis.wait_zero", "PASS", waited=result["waited"],
            killed=result["killed"], observed=result["observed"])
        return result

    def deterministic_menu(self):
        while True:
            self.output(
                "\nDeterministic repairs\n"
                "  1. Regenerate recovery BAT and MCP template state\n"
                "  2. Clear plugin bytecode caches (backed up)\n"
                "  3. Quarantine corrupt history files\n"
                "  4. Re-detect CLI paths for next plugin load\n"
                "  5. Back")
            choice = str(self.input("Choose 1-5: ") or "").strip()
            if choice == "5":
                return
            if choice not in {"1", "2", "3", "4"}:
                self.output("Invalid selection.")
                continue
            self._require_qgis_closed()
            if choice == "1":
                require_no_qgis(action="regenerate recovery state")
                bat = self._ensure_recovery_bat()
                state = refresh_template_state(
                    self.plugin_dir, self.profile_dir)
                result = {"recovery_bat": str(bat), "template_state": state}
                action = "repair.template_state"
            elif choice == "2":
                result = clear_bytecode_caches(
                    self.plugin_dir, self.profile_dir)
                action = "repair.bytecode_cache"
            elif choice == "3":
                result = {"quarantined": quarantine_corrupt_histories(
                    self.profile_dir)}
                action = "repair.history"
            else:
                result = write_detected_cli_state(
                    self.profile_dir)
                action = "repair.cli_paths"
            self.audit.append(action, "PASS", result=result)
            self.output(json.dumps(result, indent=2))

    def _available_repair_models(self):
        paths = detect_cli_paths()
        models = []
        for base in repair_model_specs():
            cli_path = paths.get(base["backend"])
            if cli_path and probe_cli(base["backend"], cli_path)["status"] == "PASS":
                item = dict(base)
                item["cli_path"] = cli_path
                models.append(item)
        return models

    def _select_repair_model(self):
        requested = dict(self.request.get("repair") or {})
        requested_key = (
            str(requested.get("backend") or ""),
            str(requested.get("model") or ""),
            str(requested.get("effort") or ""),
        )
        catalog = {
            (item["backend"], item["model"], item.get("effort") or ""): item
            for item in repair_model_specs()
        }
        if any(requested_key):
            trusted = catalog.get(requested_key)
            if trusted:
                cli_path = detect_cli_paths().get(trusted["backend"])
                probe = probe_cli(trusted["backend"], cli_path)
                if cli_path and probe["status"] == "PASS":
                    selected = dict(trusted)
                    selected["cli_path"] = cli_path
                    return selected
                raise RuntimeError(
                    f"The independently detected {trusted['backend']} CLI is "
                    f"not authenticated: {probe['detail']}")
            self.output(
                "Ignored an invalid/stale repair model in the handoff; "
                "choose a current catalog entry.")
        models = self._available_repair_models()
        if not models:
            raise RuntimeError("No authenticated repair CLI/model is available")
        self.output("\nRepair models:")
        for index, item in enumerate(models, 1):
            self.output(f"  {index}. {item['label']}")
        raw = str(self.input(f"Choose 1-{len(models)}: ") or "").strip()
        try:
            return models[int(raw) - 1]
        except (ValueError, IndexError):
            raise RuntimeError("Invalid repair model selection")

    def ai_repair(self):
        spec = self._select_repair_model()
        if self.last_diagnostics is None:
            self.diagnose()
        bundle = (self.request.get("diagnostics_bundle")
                  or render_diagnostics(self.last_diagnostics))
        description = self.request.get("user_description") or self.input(
            "Describe the error (optional): ")
        workspace = RepairWorkspace(
            self.plugin_dir, self.source_repo, self.profile_dir)
        try:
            workdir = workspace.prepare()
            program, args = _repair_command(spec, workdir)
            self.output(
                f"\nRepair CLI invocation: backend={spec['backend']}; "
                f"model={spec['model']}; effort={spec.get('effort') or 'default'}")
            self.audit.append(
                "ai.propose.start", backend=spec["backend"],
                model=spec["model"], effort=spec.get("effort"),
                workspace=str(workdir))
            result = run(
                [program, *args], cwd=workdir, timeout=900,
                stdin_text=_repair_prompt(bundle, description))
            explanation, event_count = _parse_repair_result(
                spec["backend"], result.stdout, result.stderr, result.returncode)
            proposal = workspace.build_proposal(explanation)
            if not proposal["changes"]:
                raise RuntimeError("Repair agent proposed no file changes")
            proposal_dir = self.profile_dir / "qgent" / "doctor" / "proposals"
            proposal_dir.mkdir(parents=True, exist_ok=True)
            diff_path = proposal_dir / (
                datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".diff")
            diff_path.write_text(proposal["diff"], encoding="utf-8")
            self.output("\nAGENT EXPLANATION\n=================")
            self.output(explanation or "(none)")
            self.output("\nUNIFIED DIFF\n============")
            self.output(proposal["diff"])
            self.output(f"\nDiff written to: {diff_path}")
            self.audit.append(
                "ai.propose.ready", "PASS", backend=spec["backend"],
                model=spec["model"], events=event_count,
                changed_files=[item["path"] for item in proposal["changes"]],
                diff=str(diff_path))
            approval = str(self.input(
                "Type yes to apply this exact reviewed diff; anything else denies: ")
                or "").strip()
            if approval != "yes":
                workspace.deny()
                self.audit.append("ai.apply", "DENIED", model=spec["model"])
                self.output("Denied; disposable copy removed. Real trees unchanged.")
                return {"applied": False, "denied": True, "diff": str(diff_path)}
            self._require_qgis_closed()
            applied = workspace.apply()
            self.audit.append(
                "ai.apply", "PASS", model=spec["model"], result=applied)
            self.output("\nApproved repair applied, backed up, hash-verified, and compiled.")
            self.output(json.dumps(applied, indent=2))
            if any(path.endswith(".py") for path in applied["changed_files"]):
                self.output("Restart QGIS is required for applied Python changes.")
            self._offer_relaunch()
            return {"applied": True, **applied}
        except KeyboardInterrupt:
            workspace.deny()
            self.audit.append("ai.repair", "CANCELLED")
            self.output("\nCancelled; disposable copy removed. Real trees unchanged.")
            raise
        except Exception:
            workspace.deny()
            raise

    def restore_backup(self):
        backups = [path for path in list_backups(self.profile_dir)
                   if json.loads((path / "manifest.json").read_text(
                       encoding="utf-8")).get("version") == 1]
        if not backups:
            self.output("No restorable QGent backups found.")
            return None
        self.output("\nBackups:")
        for index, path in enumerate(backups, 1):
            self.output(f"  {index}. {path.name}")
        raw = str(self.input(f"Choose 1-{len(backups)}: ") or "").strip()
        try:
            selected = backups[int(raw) - 1]
        except (ValueError, IndexError):
            raise RuntimeError("Invalid backup selection")
        self._require_qgis_closed()
        result = RepairWorkspace.restore_backup(
            selected, self.plugin_dir, self.source_repo, self.profile_dir)
        self.audit.append("backup.restore", "PASS", result=result)
        self.output(json.dumps(result, indent=2))
        self._offer_relaunch()
        return result

    def _offer_relaunch(self):
        qgis = dict(self.request.get("qgis") or {})
        executable = str(qgis.get("executable_path") or "")
        resolved = Path(executable).resolve() if executable else None
        if (resolved is None or not resolved.is_file()
                or resolved.name.casefold() not in QGIS_EXECUTABLE_NAMES):
            self.output("QGIS executable was not resolvable; relaunch offer skipped.")
            self.audit.append(
                "qgis.relaunch", "SKIPPED", reason="unresolved or invalid basename")
            return ""
        answer = str(self.input("Relaunch QGIS now? [y/N]: ") or "").strip().lower()
        if answer not in {"y", "yes"}:
            self.audit.append("qgis.relaunch", "DENIED")
            return ""
        command = [str(resolved)]
        project = str(qgis.get("project_path") or "")
        project_path = Path(project).resolve() if project else None
        if (project_path and project_path.is_file()
                and project_path.suffix.casefold() in {".qgs", ".qgz"}):
            command.append(str(project_path))
        pid = launch_detached(command, cwd=resolved.parent)
        self.audit.append("qgis.relaunch", "PASS", pid=pid, command=command)
        self.output(f"QGIS relaunched (PID {pid}).")
        return pid

    def run_menu(self):
        self._ensure_recovery_bat()
        self.audit.append("doctor.start", pid=os.getpid(), request=str(self.request_path))
        self.output("QGent External Doctor")
        self.output("Runs outside QGIS; repairs can survive a QGIS restart.")
        self._handoff_summary()
        while True:
            self.output(
                "\nMain menu\n"
                "  1. Diagnose\n"
                "  2. Deterministic repairs\n"
                "  3. AI repair (propose -> review diff -> typed yes)\n"
                "  4. Restore from backup\n"
                "  5. Exit")
            choice = str(self.input("Choose 1-5: ") or "").strip()
            try:
                if choice == "1":
                    self.diagnose()
                elif choice == "2":
                    self.deterministic_menu()
                elif choice == "3":
                    self.ai_repair()
                elif choice == "4":
                    self.restore_backup()
                elif choice == "5":
                    self.audit.append("doctor.exit", "PASS")
                    return EXIT_OK
                else:
                    self.output("Invalid selection.")
            except KeyboardInterrupt:
                self.audit.append("action.cancel", "CANCELLED")
                self.output("\nAction cancelled; no unapproved change was applied.")
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self.audit.append("action.error", "FAIL", error=redact(message))
                self.output("ERROR: " + message)


def build_parser():
    parser = argparse.ArgumentParser(description="QGent External Doctor")
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--plugin-dir", required=True)
    parser.add_argument("--source-repo", default="")
    parser.add_argument("--request", default="")
    parser.add_argument("--diagnose-only", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    app = ExternalDoctor(
        args.profile_dir, args.plugin_dir, args.source_repo,
        request_path=args.request or None)
    try:
        app._ensure_recovery_bat()
        if args.diagnose_only:
            app._handoff_summary()
            report = app.diagnose()
            return EXIT_OK if report["all_pass"] else EXIT_DIAGNOSTIC_FAILURE
        return app.run_menu()
    except KeyboardInterrupt:
        app.audit.append("doctor.exit", "CANCELLED")
        print("\nCancelled. No unapproved change was applied.")
        return EXIT_CANCELLED


if __name__ == "__main__":
    raise SystemExit(main())
