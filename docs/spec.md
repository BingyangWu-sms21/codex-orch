# Current storage and execution spec

This document describes the current implemented runtime. For the future
controller-driven runtime that supports first-class branching and loops, see
[docs/controller-runtime.md](./controller-runtime.md).

## Global layer

`codex-orch` keeps reusable global assets under `~/.codex-orch/`.

```text
~/.codex-orch/
├── config.toml
├── presets/
└── profiles/
```

## Program layer

Each program is a file-backed task pool with local prompts, inputs, presets,
and run artifacts.

```text
program/
├── project.yaml
├── tasks/
├── presets/
├── prompts/
├── inputs/
└── .runs/
```

## Task files

Each task is stored as `tasks/<task-id>.yaml`.

Execution boundary fields on each task:

- `workspace`: optional task-local cwd override. Relative values are resolved
  against `project.workspace` when the run is created.
- `extra_writable_roots`: optional extra writable directories for writable
  sandboxes. Relative values are resolved against the task's effective
  workspace.

Dependencies live on the target task in `depends_on`. There are two dependency
kinds:

- `order`: execution order only
- `context`: execution order plus explicit artifact consumption

Prompt composition can consume dependency artifacts with
`compose.kind == from_dep`, but only under an explicit `context` dependency:

- `from_dep.task` must name a task in `depends_on`
- that dependency must be `context`
- `from_dep.path` must be listed in the matching dependency edge's `consume`

## Run model

When a run starts, `codex-orch` resolves a static subgraph from the task pool,
creates one runtime instance per selected task, and writes run-centered state
under `.runs/<run-id>/`.

Task definitions inside the run are materialized and frozen for that run. Later
edits to the task pool do not mutate the in-flight run.

Run state is split into four areas:

- `state/run.json`: run metadata and instance ids
- `state/instances/<instance-id>.json`: instance state
- `events/*.json`: append-only runtime events
- `inbox/interrupts/*.json` and `inbox/replies/*.json`: external interaction
  envelopes

## Instance directories

Each runtime instance uses `.runs/<run-id>/instances/<instance-id>/`.

Standard files:

- `session.json` with the stable Codex session id for that instance
- `published/` with artifacts visible to downstream `context` dependencies
- `attempts/<attempt-no>/` for prompt, logs, runtime, final output, and scratch
  files

Each attempt directory contains:

- `prompt.md`
- `events.jsonl`
- `stderr.log`
- `runtime.json`
- `final.md`
- `result.json` when applicable
- `scratch/`

Only files copied into `published/` are visible to downstream `context`
dependencies.

`runtime.json` is the attempt-local liveness record for worker execution. It
tracks:

- worker `pid`
- actual `cwd`
- resolved `project_workspace_dir`
- sanitized `command`
- effective `sandbox`
- effective `writable_roots`
- `started_at` / `finished_at`
- `last_stdout_at` / `last_stderr_at`
- `last_event_at` / `last_progress_at`
- `last_event_summary`
- `stdout_line_count` / `stderr_line_count`
- timeout budget (`wall_timeout_sec`, `idle_timeout_sec`)
- terminal reason (`completed`, `nonzero_exit`, `wall_timeout`, `idle_timeout`,
  `terminated`, `orphaned`)

## Interrupt helpers

Worker-side escalation is modeled as runtime interrupts. Each attempt
materializes a helper doc at:

```text
.runs/<run-id>/instances/<instance-id>/attempts/<attempt-no>/context/interrupt/requesting-help.md
```

The stable entrypoint is:

```bash
codex-orch interrupt create \
  --kind clarification \
  --decision-kind policy \
  --question-file /tmp/question.md
```

The helper reads runtime context from:

- `CODEX_ORCH_PROGRAM_DIR`
- `CODEX_ORCH_RUN_ID`
- `CODEX_ORCH_INSTANCE_ID`
- `CODEX_ORCH_TASK_ID`
- `CODEX_ORCH_INSTANCE_DIR`
- `CODEX_ORCH_ATTEMPT_DIR`
- `CODEX_ORCH_PROJECT_WORKSPACE_DIR`
- `CODEX_ORCH_WORKSPACE_DIR`

Artifact paths passed with `--artifact` must be relative to
`CODEX_ORCH_PROGRAM_DIR`.

## Inbox protocol

Interrupt requests are stored under `inbox/interrupts/` and replies under
`inbox/replies/`.

Request fields include:

- `interrupt_id`
- `run_id`
- `instance_id`
- `task_id`
- `audience`
- `blocking`
- `request_kind`
- `question`
- `decision_kind`
- `options`
- `context_artifacts`
- `reply_schema`
- `priority`
- `metadata`

Reply fields include:

- `interrupt_id`
- `audience`
- `reply_kind`
- `text`
- `payload`
- optional `rationale`, `confidence`, and `citations`

Assistant replies with `reply_kind=handoff_to_human` are recorded on the
assistant interrupt and then materialize a new human interrupt on the same
instance.

## Waiting semantics

Run status still exposes `waiting`, but instance state now records
`waiting_reason=interrupts_pending`.

`resume_run()` applies these rules:

- unresolved blocking interrupts keep the instance in `waiting`
- once all blocking interrupts are resolved, the instance becomes runnable again
- resolved replies are injected only into that instance's next
  `codex exec resume` prompt
- before rescheduling work, `resume_run()` first reconciles stale `running`
  instances using the active attempt `runtime.json`

## Timeout and recovery semantics

Worker execution is guarded by two runner-level timeouts:

- `node_wall_timeout_sec`
- `node_idle_timeout_sec`

When a timeout is hit, `codex-orch` first sends a terminate signal, then
escalates to kill after the configured grace period if the worker does not
exit.

Recovery entrypoints:

- `codex-orch run reconcile <run-id>` to reconcile stale or orphaned `running`
  instances
- `codex-orch run abort <run-id>` to stop active worker processes and fail the
  run

## Workspace and access scope

`workspace` and writable scope are related but not identical:

- the effective worker cwd comes from `task.workspace` when present, otherwise
  `project.workspace`
- `workspace-write` tasks automatically receive attempt-local artifact write
  access so prompts, logs, `final.md`, and `result.json` can be materialized
- `extra_writable_roots` maps directly to Codex CLI `--add-dir`
- full filesystem access should use `sandbox: danger-full-access`
