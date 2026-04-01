# Current storage and execution spec

This document describes the current implemented runtime. Controller-driven
branching and tail-loop control are now part of the runtime. For the remaining
north-star controller work, especially channels and richer workflow state, see
[docs/controller-runtime.md](./controller-runtime.md). For the future
assistant-role interaction control plane, see
[docs/assistant-role-control-plane.md](./assistant-role-control-plane.md).

## Global layer

`codex-orch` keeps reusable global assets under `~/.codex-orch/`.

```text
~/.codex-orch/
├── config.toml
└── presets/
```

## Program layer

Each program is a file-backed task pool with local prompts, inputs, presets,
assistant roles, local assistant scratch state, and run artifacts.

```text
program/
├── assistant_roles/
│   └── _shared/operating-model.md
├── .codex-orch/
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
- `project.workspace`, `task.workspace`, and `extra_writable_roots` may use
  `${inputs.<key>}` template bindings. Bound values must resolve to strings.

Dependencies live on the target task in `depends_on`. There are two dependency
kinds:

- `order`: execution order only
- `context`: execution order plus explicit artifact consumption

Task additions in the current runtime:

- `kind: work | controller`, default `work`
- `depends_on[].as`: optional dependency scope alias
- `control.mode = route | loop`: valid only on `controller` tasks
- route controllers declare `control.routes[]`
- loop controllers declare `control.continue_targets[]` and optional `control.stop_targets[]`
- `compose.ref`: runtime state references with path-first staging
- `required_decisions`: optional list of decision obligations that the instance
  must fulfill by creating matching blocking interrupts before completing

`compose.ref` supports:

- `deps.<scope>.result`
- `deps.<scope>.artifacts.<relative-path>`
- `inputs.<key>`
- `runtime.replies`
- `runtime.latest_reply`

`inputs.<key>` stages:

- `<key>.txt` for string values
- `<key>.json` for structured JSON values

`runtime.replies` and `runtime.latest_reply` stage JSON files for the current
instance's resolved-but-not-yet-applied replies.

Artifact refs still require an explicit `context` dependency and the referenced
artifact path must be listed in the matching `consume` list.

## Run model

When a run starts, `codex-orch` resolves a frozen task snapshot from the task
pool, writes it under `.runs/<run-id>/`, creates a base input scope plus seed
runtime instances, and lets the scheduler create additional instances later as
concrete dependency bindings, controller route selections, and loop-created
input scopes become available.

Task definitions inside the run are materialized and frozen for that run. Later
edits to the task pool do not mutate the in-flight run.

Run state is split into six areas:

- `state/run.json`: run metadata plus frozen task ids, input scope ids, and instance ids
- `state/tasks/<task-id>.json`: frozen task snapshot
- `state/inputs/<input-scope-id>.json`: materialized input scope snapshots
- `state/instances/<instance-id>.json`: instance state
- `state/results/<instance-id>.json`: materialized structured results
- `events/*.json`: append-only runtime events
- `proposals/*.json`: recorded assistant update proposals
- `inbox/interrupts/*.json` and `inbox/replies/*.json`: external interaction
  envelopes

Run inputs and loop-carried input scope values are typed JSON values. Default
input files are parsed by extension:

- `.json` -> JSON
- `.yaml` / `.yml` -> YAML converted to JSON-compatible values
- other files -> raw text string

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

`compose.ref` is path-first:

- runtime resolves each ref against the frozen run snapshot and materialized
  state
- the resolved source is staged into `attempts/<attempt-no>/context/refs/...`
- prompt text includes staged paths and metadata, not inline copies of the
  referenced content

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
codex-orch interrupt recommend \
  --kind clarification \
  --decision-kind policy

codex-orch interrupt create \
  --kind clarification \
  --decision-kind policy \
  --target-role policy \
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

Assistant execution also requires a shared operating model document at:

```text
assistant_roles/_shared/operating-model.md
```

New programs scaffold it during `project init`. Existing programs can install it
with:

```bash
codex-orch assistant-doc install /path/to/program
```

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
- `requested_target_role_id`
- `recommended_target_role_id`
- `resolved_target_role_id`
- `target_resolution_reason`
- `metadata`

Reply fields include:

- `interrupt_id`
- `audience`
- `reply_kind`
- `text`
- `payload`
- optional `rationale`, `confidence`, and `citations`

When `reply_schema` is set on the interrupt, `reply.payload` is validated
against that JSON Schema for `reply_kind=answer`.

Assistant replies may also carry structured `proposed_updates[]`. Valid
proposals are recorded under `.runs/<run-id>/proposals/` and invalid proposals
are dropped with corresponding runtime events. Proposal records are manual
operator input only and are never auto-applied by runtime automation.

Supported proposal kinds:

- `instruction_update`: update a role's instructions file
- `managed_asset_update`: update a role's managed asset file
- `routing_policy_update`: update a task's assistant hints or interaction policy
- `program_asset_update`: update a program-owned asset such as `inputs/*.yaml`
  or other repo-visible files; target path is program-relative and not scoped
  to any assistant role

Assistant replies with `reply_kind=handoff_to_human` are recorded on the
assistant interrupt and then materialize a new human interrupt on the same
instance, but only when the task's `interaction_policy.allow_human` is true.

## Waiting semantics

Run status still exposes `waiting`, but instance state now records
`waiting_reason=interrupts_pending`.

`resume_run()` applies these rules:

- unresolved blocking interrupts keep the instance in `waiting`
- once all blocking interrupts are resolved, the instance becomes runnable again
- resolved replies are injected only into that instance's next
  `codex exec resume` prompt
- resolved replies are also available through `compose.ref runtime.replies` and
  `compose.ref runtime.latest_reply` on that same resumed instance
- before rescheduling work, `resume_run()` first reconciles stale `running`
  instances using the active attempt `runtime.json`

## Controller control

`controller` tasks must produce `result.json` with a top-level `control`
object. The scheduler materializes that result, records control events, and
only then instantiates current-scope route/stop targets or next-scope continue
seeds.

Current controller authoring rules:

- route controllers use `control.mode: route` plus `control.routes[]`
- loop controllers use `control.mode: loop` plus `control.continue_targets[]`
  and optional `control.stop_targets[]`
- each dynamic target task may be owned by at most one controller control edge
- route targets and loop stop targets must explicitly depend on the controller
- loop continue targets are activation-only next-iteration seeds; they do not
  depend on the controller

Current controller output rules:

- route controllers emit `control.kind: route` plus `control.labels[]`
- loop controllers emit `control.kind: loop` plus `control.action`
- `control.next_inputs` is only valid for `loop.action=continue`
- `control.next_inputs` accepts JSON values, not only strings
- assistant/human replies are never scheduler inputs directly; a controller must
  first consume them and then emit materialized control

Current activation rules:

- route targets activate only for selected labels in the same input scope
- loop `stop` targets activate in the same input scope
- loop `continue` creates a fresh input scope and instantiates only that
  scope's configured continue seed tasks
- unselected routes do not get placeholder runtime instances
- repeated activation of the same logical task is deduped by concrete
  `dependency_instances + input_scope_id`
- different dependency bindings or different input scopes may produce multiple
  instances of the same logical task in one run

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
- `codex-orch proposal list/show/mark ...` to inspect and track manual handling
  of recorded assistant update proposals

## Workspace and access scope

`workspace` and writable scope are related but not identical:

- the effective worker cwd comes from `task.workspace` when present, otherwise
  `project.workspace`
- `workspace-write` tasks automatically receive attempt-local artifact write
  access so prompts, logs, `final.md`, and `result.json` can be materialized
- `extra_writable_roots` maps directly to Codex CLI `--add-dir`
- full filesystem access should use `sandbox: danger-full-access`

## Decision obligation semantics

Tasks may declare `required_decisions`, a list of decision obligations. Each
entry specifies a `decision_kind` and an `audience` (`human`, `assistant`, or
`any`).

When an instance completes successfully, the scheduler checks whether the
instance created a blocking interrupt matching each required decision. If any
required decision was never created, the instance is failed with
`failure_kind=decision_obligation` instead of transitioning to `done`.

This turns soft guidance like `ask_when` into an enforceable runtime contract.
The worker prompt may still decide *when* to create the interrupt, but the
engine guarantees that the interrupt was created before the task can succeed.

Example task declaration:

```yaml
required_decisions:
  - decision_kind: review
    audience: human
    description: "Repair plan must be reviewed by a human before proceeding"
```
