# codex-orch

`codex-orch` is a local-first task orchestrator for Codex CLI.

It keeps tasks, presets, runtime state, interrupts, and published outputs on
disk instead of in a database. A run materializes a frozen task snapshot,
creates seed runtime instances, and lets the built-in instance scheduler
dynamically activate downstream instances from concrete dependency bindings and
controller route selections.

The current implemented runtime is documented in [docs/spec.md](./docs/spec.md).
The future controller-driven branching and loop runtime is documented in
[docs/controller-runtime.md](./docs/controller-runtime.md). The future
assistant-role interaction control plane is documented in
[docs/assistant-role-control-plane.md](./docs/assistant-role-control-plane.md).

## Features

- File-backed task pool with CRUD operations
- Two dependency kinds: `order` and `context`
- `compose.ref` reads run inputs, materialized dependency results, and consumed dependency artifacts
- `controller` tasks with route-driven branch activation
- Global presets under `~/.codex-orch/` plus per-program presets
- Run-centered task snapshot, instance runtime, materialized results, and event log
- Codex session-aware resume via `codex exec resume`
- Runtime inbox with interrupt requests and replies for assistant and human interaction
- Built-in assistant worker plus bundled `operate-codex-orch` operator skill
- Local web UI for task and run inspection

## Repository layout

```text
codex-orch/
├── src/codex_orch/
├── tests/
├── docs/
└── examples/
```

Key docs:

- [docs/spec.md](./docs/spec.md): current implemented storage and execution model
- [docs/controller-runtime.md](./docs/controller-runtime.md): north star for the remaining controller runtime work, especially loops and channels
- [docs/assistant-role-control-plane.md](./docs/assistant-role-control-plane.md): target worker/assistant/human interaction control plane with named assistant roles and managed role-scoped preferences

## Program layout

Each orchestrated program lives in its own directory, for example:

```text
codex-programs/my-program/
├── assistant_roles/
│   └── _shared/operating-model.md
├── project.yaml
├── tasks/
├── presets/
├── prompts/
├── inputs/
├── .codex-orch/
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
codex-orch assistant-doc install . --json
```

## Interrupt helper flow

Worker-side external interaction is built into each attempt's runtime prompt
plus a helper doc at:

```text
.runs/<run-id>/instances/<instance-id>/attempts/<attempt-no>/context/interrupt/requesting-help.md
```

Workers should use the CLI helper instead of hand-writing inbox files:

```bash
codex-orch interrupt recommend \
  --program-dir /path/to/program \
  --run-id 20260319010101-deadbeef \
  --task-id executeRefactor \
  --audience assistant \
  --kind clarification \
  --decision-kind policy

codex-orch interrupt create \
  --program-dir /path/to/program \
  --run-id 20260319010101-deadbeef \
  --instance-id executeRefactor-a1b2c3d4 \
  --task-id executeRefactor \
  --audience assistant \
  --kind clarification \
  --decision-kind policy \
  --target-role policy \
  --question "Can I delete the legacy wrapper?" \
  --option delete \
  --option keep_wrapper
```

When a worker runs inside `codex-orch`, the helper can infer:

- `CODEX_ORCH_PROGRAM_DIR`
- `CODEX_ORCH_RUN_ID`
- `CODEX_ORCH_INSTANCE_ID`
- `CODEX_ORCH_TASK_ID`
- `CODEX_ORCH_INSTANCE_DIR`
- `CODEX_ORCH_ATTEMPT_DIR`
- `CODEX_ORCH_PROJECT_WORKSPACE_DIR`
- `CODEX_ORCH_WORKSPACE_DIR`

Context artifacts passed with `--artifact` must be relative to
`CODEX_ORCH_PROGRAM_DIR`.

Assistant interrupts now resolve to a concrete assistant role. Task-local
`interaction_policy` can restrict which roles are allowed and whether human
handoff is permitted.

Blocking interrupts do not immediately stop the current attempt. Instead,
`codex-orch` waits until the attempt ends, moves the instance to `waiting` if
any blocking interrupts remain unresolved, and resumes the same Codex session
after all blocking interrupts are resolved.

Assistant or human replies are written back through the inbox:

```bash
codex-orch inbox list /path/to/program --json
codex-orch inbox show /path/to/program <interrupt-id> --json
codex-orch inbox reply /path/to/program <interrupt-id> \
  --text "Delete it." \
  --reply-kind answer \
  --resume
```

The built-in assistant worker processes unresolved assistant interrupts:

```bash
codex-orch inbox worker /path/to/program --once --json
```

Assistant update proposals are recorded per run and can be reviewed manually:

```bash
codex-orch proposal list /path/to/program --run-id <run-id> --json
codex-orch proposal show /path/to/program <proposal-id> --json
codex-orch proposal mark /path/to/program <proposal-id> --status applied --note "updated manually"
```

## Skill export

`codex-orch` ships a canonical `operate-codex-orch` skill template for external
or user-controlled agents operating a program from outside a worker instance. It
is not the worker-side escalation mechanism.

Maintainers can export and install it with:

```bash
uv run codex-orch skill list
uv run codex-orch skill export operate-codex-orch /tmp/exported-skills
uv run codex-orch skill install operate-codex-orch --repo-dir /path/to/repo
```

## Waiting semantics

Run state still exposes `waiting`, but instance-level waiting is now driven by
blocking interrupts:

- unresolved blocking interrupts keep the instance in `waiting`
- resolved replies are injected only into that instance's next
  `codex exec resume` prompt
- assistant `handoff_to_human` becomes a new human interrupt on the same instance

## Status

This repository is intentionally local-first and single-user. The web UI is a
thin convenience layer over the same file-backed domain logic used by the CLI.
