# codex-orch

`codex-orch` is a local-first task orchestrator for Codex CLI.

It keeps tasks, presets, dependency edges, run snapshots, assistant protocol
artifacts, and published outputs on disk instead of in a database. A run
materializes a subgraph from a task pool, freezes it into a snapshot, then
executes nodes with `codex exec` while `Prefect` provides orchestration,
concurrency, and run metadata.

## Features

- File-backed task pool with CRUD operations
- Two dependency kinds: `order` and `context`
- `compose.from_dep` reads only from explicitly consumed `context` artifacts
- Global presets under `~/.codex-orch/` plus per-program presets
- Prefect-backed run snapshots and resumable execution
- Published-artifact handoff between tasks
- Assistant control-plane protocol:
  `assistant_request.json`, `assistant_response.json`, `assistant_control_action.json`
- Manual-gate protocol:
  `manual_gate.json`, `human_request.json`, `human_response.json`
- Bundled `request-assistant` Codex skill with explicit export/install commands
- Local web board for task lists, kanban, dependency graph, presets, runs, assistant inbox, and manual gates

## Repository layout

```text
codex-orch/
├── src/codex_orch/
├── tests/
├── docs/
└── examples/
```

## Program layout

Each orchestrated program lives in its own directory, for example:

```text
codex-programs/my-program/
├── project.yaml
├── tasks/
├── presets/
├── prompts/
├── inputs/
└── .runs/
```

## Quick start

```bash
uv sync --extra dev
uv run codex-orch task list /path/to/program
uv run codex-orch web /path/to/program
```

## Assistant helper commands

Worker-side assistant requests are intended to be wrapped by a Codex skill, but
the stable contract is the CLI helper:

```bash
uv run codex-orch assistant request create \
  --program-dir /path/to/program \
  --run-id 20260319010101-deadbeef \
  --task-id executeRefactor \
  --kind clarification \
  --decision-kind policy \
  --question "Can I delete the legacy wrapper?" \
  --option delete \
  --option keep_wrapper
```

When a worker runs inside `codex-orch`, the helper can infer:

- `CODEX_ORCH_PROGRAM_DIR`
- `CODEX_ORCH_RUN_ID`
- `CODEX_ORCH_TASK_ID`
- `CODEX_ORCH_NODE_DIR`

Assistant or human responses are written back with:

```bash
uv run codex-orch assistant respond /path/to/program <request-id> \
  --resolution-kind auto_reply \
  --answer "Delete it." \
  --rationale "The repository does not need compatibility wrappers."
```

Control-plane actions are stored separately from replies:

```bash
uv run codex-orch assistant action create /path/to/program <request-id> \
  --action-kind append_guidance_proposal \
  --requested-by assistant \
  --target-kind user_guidance \
  --target-path "~/.codex/AGENTS.md" \
  --reason "Promote a repeated decision to long-term guidance."
```

## Skill export

`codex-orch` ships a canonical `request-assistant` skill template. Export it
into any target directory or install it into a repo-local `.codex/skills/`
folder explicitly:

```bash
uv run codex-orch skill list
uv run codex-orch skill export request-assistant /tmp/exported-skills
uv run codex-orch skill install request-assistant --repo-dir /path/to/repo
```

The exported skill contains:

- `SKILL.md`
- `scripts/request_assistant.sh`
- `references/protocol.md`

## Manual gates

When an assistant response chooses `handoff_to_human`, `codex-orch` now
materializes:

- `manual_gate.json`
- `human_request.json`
- `human_response.json` after a human replies

The CLI exposes the minimal human-control surface:

```bash
uv run codex-orch manual-gate list /path/to/program
uv run codex-orch manual-gate show /path/to/program <gate-id>
uv run codex-orch manual-gate respond /path/to/program <gate-id> \
  --answer "Delete the wrapper."
uv run codex-orch manual-gate approve /path/to/program <gate-id> --resume
```

The web UI exposes the same flow at `/manual-gates`.

## Waiting semantics

Run snapshots still use `waiting` at the top level, but node-level waiting now
records a specific reason:

- `assistant_pending`
- `handoff_to_human`
- `manual_gate_blocked`

This makes `resume` deterministic: unresolved gates stay blocked, approved gates
resume, and rejected gates fail the run instead of remaining ambiguous.

## Status

This repository is intentionally local-first and single-user. The web UI is a
thin convenience layer over the same file-backed domain logic used by the CLI.
