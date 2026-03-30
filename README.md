# codex-orch

`codex-orch` is a local-first task orchestrator for Codex CLI.

It keeps tasks, presets, dependency edges, run snapshots, assistant protocol
artifacts, and published outputs on disk instead of in a database. A run
materializes a subgraph from a task pool, freezes it into a snapshot, then
executes nodes with `codex exec` while `Prefect` provides orchestration,
concurrency, and run metadata.

The current implementation is still a static DAG snapshot runtime. The target
controller-driven branching and loop runtime is documented separately in
[docs/controller-runtime.md](./docs/controller-runtime.md).

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
- Built-in worker assistant escalation contract plus bundled `operate-codex-orch` operator skill
- Local web board for task lists, kanban, dependency graph, presets, runs, assistant inbox, and manual gates

## Repository layout

```text
codex-orch/
├── src/codex_orch/
├── tests/
├── docs/
└── examples/
```

Key docs:

- [docs/spec.md](./docs/spec.md): current implemented storage and execution
  model
- [docs/controller-runtime.md](./docs/controller-runtime.md): target
  controller-driven runtime for branching, loops, and interrupt-backed resume

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

For local development in this repository:

```bash
uv sync --extra dev
uv run codex-orch task list /path/to/program
uv run codex-orch web /path/to/program
```

For operators working from a codex-orch program directory after the package is
published to PyPI:

```bash
pipx install codex-orch
codex-orch --version
codex-orch task list .
```

## Assistant helper flow

Worker-side assistant escalation is built into each node's runtime prompt plus a
node-local helper doc at:

```text
.runs/<run-id>/nodes/<task-id>/context/assistant/requesting-help.md
```

Workers should use the stable CLI helper instead of hand-writing protocol
files:

```bash
codex-orch assistant request create \
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
- `CODEX_ORCH_PROJECT_WORKSPACE_DIR`
- `CODEX_ORCH_WORKSPACE_DIR`

Assistant artifacts passed with `--artifact` must be relative to
`CODEX_ORCH_PROGRAM_DIR`.

An `auto_reply` is reinjected only into the same task continuation on resume. A
`handoff_to_human` reply materializes `manual_gate.json` and
`human_request.json` for that node and pauses execution until a human responds
and the gate is approved or rejected.

Assistant or human responses are written back with:

```bash
codex-orch assistant respond /path/to/program <request-id> \
  --resolution-kind auto_reply \
  --answer "Delete it." \
  --rationale "The repository does not need compatibility wrappers."
```

Control-plane actions are stored separately from replies:

```bash
codex-orch assistant action create /path/to/program <request-id> \
  --action-kind append_guidance_proposal \
  --requested-by assistant \
  --target-kind user_guidance \
  --target-path "~/.codex/AGENTS.md" \
  --reason "Promote a repeated decision to long-term guidance."
```

## Skill export

`codex-orch` ships a canonical `operate-codex-orch` skill template for
external or user-controlled agents operating a program from outside a worker
node. It is not the worker-side escalation mechanism. The skill assumes the
operator is in the target program directory and that `codex-orch` was installed
separately, typically with `pipx install codex-orch`.

Maintainers can still export it from this repository or install it into a
repo-local `.codex/skills/` folder explicitly:

```bash
uv run codex-orch skill list
uv run codex-orch skill export operate-codex-orch /tmp/exported-skills
uv run codex-orch skill install operate-codex-orch --repo-dir /path/to/repo
```

The exported skill contains:

- `SKILL.md`
- `references/quickstart.md`
- `references/operator-runbook.md`

## Manual gates

When an assistant response chooses `handoff_to_human`, `codex-orch` now
materializes:

- `manual_gate.json`
- `human_request.json`
- `human_response.json` after a human replies

The CLI exposes the minimal human-control surface:

```bash
codex-orch manual-gate list /path/to/program
codex-orch manual-gate show /path/to/program <gate-id>
codex-orch manual-gate respond /path/to/program <gate-id> \
  --answer "Delete the wrapper."
codex-orch manual-gate approve /path/to/program <gate-id> --resume
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
