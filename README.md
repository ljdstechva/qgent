# QGent — A QGIS AI Agent

A dockable **AI agent** inside QGIS. Describe a GIS task in plain language —
*"load this DEM and delineate the watershed"*, *"make an A3 vicinity map in
EPSG:32651"* — and an agent team plans and executes **PyQGIS / Processing**
directly in your open project, streaming its reasoning back into the panel and
asking approval before anything destructive.

Powered by your **existing Claude or ChatGPT subscription** through the
**Claude Code CLI** (primary) or **Codex CLI** (fallback). **No API keys, no
per-token billing.**

---

## How it works

```
QGIS ── Chat Dock ──spawns──▶ claude / codex CLI  (Supervisor + subagents)
  │                                   │ calls MCP tools
  │  Socket server (127.0.0.1, token) ◀── mcp_stdio_bridge.py (stdlib) ◀── CLI
  │        │ queued signal
  └ Main-thread executor ── runs PyQGIS on the GUI thread ── live project
```

- **Coarse tools, not many fine ones.** One `execute_pyqgis(code)` call runs a
  whole workflow in-process = one model round trip. Five MCP tools total:
  `execute_pyqgis`, `get_project_context`, `run_processing`,
  `get_layer_features`, `render_map_snapshot`.
- **The CLI is the brain.** It brings subscription auth, the agent loop, MCP
  client, Skills, subagents, and session resume for free.
- **Agent team (Claude backend):** a **Supervisor** (rules in `CLAUDE.md`) writes
  a **Goal Contract**, delegates to **data-scout / geoprocessor / cartographer**,
  and a read-only **qa-verifier** confirms the Definition of Done against the
  *live project* before you're told it's done. This is the anti-drift /
  anti-hallucination core.

## Architecture decisions (vs. the original plan)

| Topic | Decision | Why |
|---|---|---|
| Session continuity | Per-turn process spawn with `--resume <session_id>` | Robust and trivially cancellable; no fragile long-lived stdin pipe |
| Execution marshalling | stdlib socket server in a daemon thread → Qt signal + `threading.Event` → main-thread `MainThreadExecutor` | Avoids `invokeMethod` return-value pain; GUI stays responsive; parallel subagent calls serialize safely on the main thread |
| Approval gate | Lives on the **QGIS side** (socket server AST-scans code, signals the dock) — **not** in the stdlib bridge | The bridge is a separate process with no UI; enforcing here covers every subagent regardless of prompt |
| `<3 s` first token (success criterion #1) | Applies to the **direct** path | A subagent dispatch is a full extra model turn; delegated tasks are inherently slower by design |
| Heavy geoprocessing | 60 s executor timeout returned to the model + guidance to chunk / use `processing.run` | A single `exec` on the GUI thread can't be preempted; long ops should be split (a real `QgsTask` path is future work) |

## Install

1. **Install a CLI and log in** (one of):
   - Claude Code: `npm i -g @anthropic-ai/claude-code`, then `claude` and sign in
     with your Claude Pro/Max account. *(Recommended — full agent team.)*
   - Codex: install the Codex CLI, sign in with your ChatGPT account.
     *(Single-agent fallback — no subagents; see below.)*
2. **Install the plugin:** copy the `qgis_chat_agent/` folder into your QGIS
   plugins directory, or zip it and use *Plugins → Manage and Install → Install
   from ZIP*. Enable **QGent**.
3. Click the **QGent** toolbar button. Open **⚙ Settings** if the CLI
   path wasn't autodetected.

## Usage

Type a request and press Enter. Examples:
- *"Buffer the active layer by 100 m (project to UTM 51N first) and add it."*
- *"Categorized symbology on `landuse` by the `class` field, then a legend."*
- *"Build an A3 vicinity map centred on 14.676, 121.044 with scale bar + north arrow, export PDF."*

Tool calls appear as collapsible ⚙ chips; delegated subagents as 🤖 status
chips. Destructive code (file writes, deletes, `commitChanges`, overwrites) pops
an inline **Approve / Deny** card — configurable in Settings
(*ask-destructive* / *ask-always* / *auto*).

### Chat history

QGent restores the current project's conversation after QGIS restarts,
including message layer tags and final, inert tool/subagent/approval states.
History is JSON Lines under the active QGIS profile at
`<QGIS settings dir>/qgent/history/<project-key>.jsonl`; saved projects use a
SHA-256 key derived from the project filename and unsaved projects use
`unsaved.jsonl`. The **✚ New** button is the only normal action that deletes
the current project's active history and resets its CLI session.

### Doctor and repair backups

Open **Settings → Doctor** to run non-AI health checks for both CLIs, Python,
the live MCP bridge, Claude JSONL streaming, bundled agents/skills, history,
byte-compilation, and the plugin's recent QGIS log ring. **Auto-repair failed
checks** applies only the listed deterministic remedies (configuration/bridge,
CLI-path, cache, corrupt-history, and stale-session repairs) and then reruns the
full check set.

**Diagnose & propose fix** copies the installed plugin to a disposable TEMP
directory, excluding `claude_runtime/mcp-config.json`, and runs the selected
repair model there. The real installed and source trees are byte/metadata/Git-
state guarded while the proposal runs and revalidated when **Approve** is
clicked, so a stale reviewed diff is refused. QGent presents the model
explanation and unified diff; only an explicit **Approve** copies reviewed files
to both trees and creates a `doctor:` source commit. Apply and Restore run in
workers so the dialog event loop remains responsive. The configured source
checkout is
`D:\11 QGIS Agent\qgis_chat_agent` when that directory is present. **Deny**
removes the copy without changing either tree. Codex repair uses
`workspace-write`, an explicit disposable working root, the Windows elevated
restricted-token sandbox, and disabled `unified_exec`; adversarial canaries
verify that the copy remains writable while installed, source, profile, and
unrelated sibling paths cannot be read or written. It never uses
`danger-full-access`.

Approved changes are backed up under
`<QGIS settings dir>/qgent/backups/<timestamp>/`. Each backup contains a paired
installed/source pre-state manifest with SHA-256 hashes. **Restore selected
backup** verifies the restored bytes, creates a `doctor: restore` source commit
when the source checkout is present, and enables plugin reload. Applying or
restoring source changes requires Git on the machine.

## Safety

- Every `execute_pyqgis` payload is AST-scanned for destructive patterns
  **before** it runs, at the bridge — so the gate applies no matter which
  subagent issued it.
- The socket server binds `127.0.0.1` on an ephemeral port and authenticates
  every request with a per-session token, so other local processes can't drive
  your QGIS.
- The Claude backend is restricted to the QGIS MCP tools + `Task` + read-only
  builtins; `Bash`/`Write`/`Edit`/web tools are disallowed.

## Codex backend caveat

Codex has no `.claude/agents/` subagent system. On the Codex backend the plugin
runs **single-agent**: the same Goal Contract + a mandatory self-verification
pass are injected into the prompt (`CLAUDE.md` "Codex backend" section) instead
of a separate qa-verifier. The event-stream schema is experimental, so parsing
is best-effort. **Claude Code is the reference backend.**

## Layout

```
qgis_chat_agent/
├── metadata.txt, __init__.py, plugin.py, config.py, history.py, doctor.py
├── ui/            chat_dock.py, widgets.py, settings_dialog.py
├── agent/         backend_base.py, claude_code_backend.py, codex_backend.py,
│                  repair_backend.py, stream_parser.py
├── bridge/        qgis_socket_server.py, main_thread_executor.py, safety.py, mcp_stdio_bridge.py
├── context/       project_snapshot.py
├── claude_runtime/          # CLI working directory (bundled)
│   ├── CLAUDE.md            # Supervisor rules + Goal Contract template
│   ├── mcp-config.json.template
│   └── .claude/
│       ├── agents/         data-scout, geoprocessor, cartographer, qa-verifier
│       └── skills/         pyqgis-patterns, processing-recipes,
│                           cartography-print-layout, ph-environmental-maps
└── resources/     icon.svg
```

## UI

QGent v0.2 ships a redesigned, animated panel: teal→indigo accent theme that
adapts to light/dark QGIS themes, right-aligned user bubbles + side-rail
assistant messages, live tool-chip spinners with expandable details, shimmer
subagent chips with elapsed time, a thinking indicator, a morphing Send⇄Stop
button, smooth scrolling, and starter suggestion chips. Streamed tokens are
coalesced (40 ms) so long answers render without stutter. All motion can be
disabled via *Settings → Appearance → Reduce motion*.

> **Status: v0.2 (experimental).** The Claude Code streaming seam was validated
> against Claude Code 2.1.214 (`--include-partial-messages`); the parser is
> tolerant of unknown event types, but flag names can change across releases.
