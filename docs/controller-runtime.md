# Controller-Driven Runtime North Star

This document defines the target runtime model for controller-driven branching
and loops in `codex-orch`. The current runtime now implements explicit route
and tail-loop controller modes plus run-level input scopes. This document
remains future-facing for the remaining channel and richer workflow-state
pieces. The current implemented runtime is documented in [docs/spec.md](./spec.md).

For the future worker / assistant / human interaction control plane with named
assistant roles and managed role-scoped preferences, see
[docs/assistant-role-control-plane.md](./assistant-role-control-plane.md).

The goal of this design is to make branching, loops, and interrupt-driven
execution first-class without abandoning the project's local-first,
filesystem-backed architecture.

## Why the current model is not enough

Today's runtime already has a run-centered instance scheduler, attempt
directories, interrupt/inbox channels, Codex session resume, explicit
controller route/loop control, run-level input scopes, and materialized
per-instance results. That is sufficient for:

- fixed `order` / `context` dependencies plus controller-driven branching
- tail-controller loop continuation via next-iteration input scopes
- published-artifact handoff and path-first result/artifact refs
- pausing and resuming the same instance after assistant or human input

It is not sufficient for:

- richer routing based on declared channels and shared workflow state
- replay and reconciliation when routing decisions depend on side effects

The core issue is no longer basic runtime persistence or resume mechanics. The
gap is now the remaining shared-state surface beyond the implemented route/loop
MVP: declared channels, richer workflow-state composition, and eventual
controller capabilities beyond the current explicit route-vs-loop split.

## Core Principles

- Filesystem remains the only source of truth.
- Runtime truth is expressed as append-only events plus materialized state, not
  by mutating a small set of object files in place.
- Scheduling is instance-based, not task-based.
- Branching is controller-driven. Generic conditional edges are not a first-
  class authoring model.
- Interrupts are external input channels, not workflow nodes.
- The scheduler only reads materialized workflow state. It does not inspect raw
  assistant or human reply envelopes when making routing decisions.
- Only running node instances may perform side effects such as tests, repo
  inspection, tool usage, or external interaction.
- Resume continuity should come from Codex session resume semantics, not from
  reconstructing continuation prompts after the fact.

## Runtime Concepts

### Node Kinds

The runtime moves from a single "task" concept to two execution roles:

- `work`: normal worker/explorer style node that produces artifacts and a
  structured result
- `controller`: a special node that may also do work, but its primary contract
  is to emit structured control output used by the scheduler

Both kinds execute through the same attempt/session machinery. The difference is
in what the scheduler expects from their outputs.

### Instance Identity

`task_id` remains the author-facing logical identifier. Runtime execution is
keyed by `instance_id`.

- One `work` or `controller` definition may produce multiple instances over a
  run.
- Each instance owns its own attempts, state, interrupts, and Codex session.
- Author-facing references do not name `instance_id` directly.

### Workflow State

The scheduler consumes a materialized workflow state with three namespaces:

- `inputs`: run inputs plus loop-provided next-iteration inputs
- `results`: per-instance structured outputs
- `channels`: a small set of explicitly declared run-global channels

Default authoring uses namespaced results, not shared mutable global state.
Channels exist only for declared shared coordination cases such as aggregators,
controllers, or loop bookkeeping.

## Authoring Model

### Project Additions

`project.yaml` gains an optional `channels` section:

```yaml
channels:
  - name: release_plan
    schema: schemas/release-plan.json
  - name: review_summary
```

`schema` is optional. When present it is a JSON Schema file relative to the
program root.

### Task Additions

Task definitions gain the following controller fields:

```yaml
id: quality_gate
kind: controller
agent: worker
depends_on:
  - task: implement_candidate
    as: candidate
    kind: context
    consume:
      - result.json
compose:
  - kind: file
    path: prompts/quality_gate.md
publish:
  - final.md
  - result.json
control:
  mode: route
  routes:
    - label: fix
      targets: [apply_fix]
    - label: done
      targets: [publish_summary]
```

New semantics:

- `kind` defaults to `work`
- `depends_on[].as` is optional; if omitted, `task` is the dependency scope key
- `control` is valid only on `controller` nodes
- `controller` nodes must publish `result.json`
- `control.mode=route` maps emitted symbolic labels to downstream logical task ids
- `control.mode=loop` declares next-iteration continue seeds and same-iteration
  stop targets
- one controller instance emits exactly one control kind in the current runtime

### Dependency-Scoped References

Author-facing state reads use dependency scopes instead of raw task ids or
instance ids.

Reference rules:

- `deps.<scope>.result.<path>` reads the resolved upstream instance result for
  this instance
- `deps.<scope>.artifacts.<path>` reads declared published artifacts from the
  resolved upstream instance
- `inputs.<key>` reads run or loop-provided JSON values
- `channels.<name>` reads a declared global channel

If a dependency declares `as`, that alias becomes the scope key. Otherwise the
upstream `task` id is used.

## Controller Contract

A `controller` instance writes `result.json` with a required top-level
`control` object. This `control` object is the only scheduler-consumed payload
for branching and loops.

```json
{
  "result": {
    "tests_ran": ["integration"],
    "summary": "integration tests failed"
  },
  "control": {
    "kind": "route",
    "labels": ["fix"]
  }
}
```

Rules:

- controller `result.json` may carry arbitrary domain data under `result`
- controller `result_schema`, when present, validates the full JSON document
- `result.json.control` must conform to the built-in `ControlEnvelope` schema
- `control.kind=route` carries symbolic route labels matched against
  `control.routes`
- `control.kind=loop` carries `action=continue|stop`
- `control.next_inputs` is only valid when `control.kind=loop` and
  `action=continue`, and it carries JSON values
- controller outputs are materialized into workflow state before scheduling

The scheduler must fail the controller instance when:

- it emits a label that is not declared in `control.routes`
- its `result.json` does not conform to `result_schema` or the built-in
  `ControlEnvelope` schema
- it emits loop control incompatible with the controller's static
  `control.mode=loop` configuration

## Branching Semantics

Branching is `controller-only`.

- Normal dependency edges remain for static ordering and artifact flow.
- Dynamic branch activation comes only from controller route labels.
- Future conditional-edge syntax, if ever added, should compile into controller
  behavior rather than become a separate runtime model.

Routing algorithm:

1. Resolve all upstream dependencies for the controller instance.
2. Run the controller attempt(s) until it reaches `DONE` or `FAILED`.
3. Materialize the controller result and `ControlEnvelope`.
4. Activate all targets for every emitted label in the same input scope.
5. Record explicit route decision events for both selected and unselected
   labels.
6. Do not create placeholder `SKIPPED` instances for unselected branches.

Unselected branches remain visible through route decision events, not through
fake runtime instances.

## Loop Semantics

Loops use the same controller contract as branching.

- In the current runtime, a controller is either `mode=route` or `mode=loop`,
  not both.
- `loop.action == continue` creates a new input scope and instantiates the
  controller's configured `continue_targets` as next-iteration seeds.
- `loop.action == stop` activates `stop_targets`, if any, in the current input
  scope.
- `next_inputs` becomes the next iteration's `inputs` namespace as typed JSON
  values.

Loop invariants:

- every iteration creates new instance ids
- instance identity includes concrete dependency bindings plus `input_scope_id`
- same-iteration dependencies resolve against instances in the current input
  scope when present, otherwise they may fall back to static base-scope
  dependencies
- author-facing references remain dependency-scoped even though the runtime is
  instance-based

## Interrupt Model

Assistant and human interaction move from node-local pause artifacts to runtime
interrupt channels.

Interrupt rules:

- a running instance may create zero or more interrupts
- interrupts may target `assistant` or `human`
- interrupts may be `blocking` or non-blocking
- creating an interrupt does not immediately terminate the current attempt
- at attempt end, unresolved blocking interrupts move the instance to `WAITING`
- an instance becomes runnable again only after all of its blocking interrupts
  are resolved
- interrupt replies are first-class inbox events, not direct scheduler inputs
- replies must be consumed by the waiting instance and materialized into
  `results` or `channels` before any downstream routing reads them

This preserves the separation of concerns:

- inbox artifacts capture actor communication
- instance state captures how that communication affected execution
- routing only reads state, never raw reply files

## Resume and Attempts

Each instance owns a Codex session.

- the first attempt starts a new `codex exec` session
- later attempts use `codex exec resume`
- the runtime stores the stable Codex session handle alongside instance state
- continuity comes from Codex's own resume semantics and full instance-level
  history

Attempt rules:

- temporary attempt outputs may be discarded if blocking interrupts remain
- attempt-local artifacts are not the continuity mechanism
- durable state lives in run-level events, materialized state, inbox artifacts,
  and the Codex session mapping

## Filesystem Layout

The runtime becomes run-centered instead of node-centered.

```text
.runs/<run-id>/
├── events/
│   ├── 000001-run-created.json
│   ├── 000014-instance-created.json
│   ├── 000028-interrupt-requested.json
│   └── ...
├── state/
│   ├── run.json
│   ├── instances/
│   │   └── <instance-id>.json
│   ├── results/
│   │   └── <instance-id>.json
│   └── channels/
│       └── <channel-name>.json
├── inbox/
│   ├── interrupts/
│   │   └── <interrupt-id>.json
│   └── replies/
│       └── <interrupt-id>.json
└── instances/
    └── <instance-id>/
        ├── instance.json
        ├── session.json
        └── attempts/
            ├── 0001/
            │   ├── prompt.md
            │   ├── events.jsonl
            │   ├── stderr.log
            │   ├── runtime.json
            │   ├── final.md
            │   ├── result.json
            │   └── scratch/
            └── 0002/
                └── ...
```

Rules:

- `events/` is append-only
- `state/` is the scheduler's read model
- `inbox/` is actor input only
- `instances/` stores attempt execution context and Codex session bindings
- node-local attempt files are execution context only, not scheduler truth

## Event Model

The event log must be rich enough to reconstruct runtime decisions without
replaying side effects.

Minimum event set:

- `run_created`
- `instance_created`
- `instance_runnable`
- `attempt_started`
- `attempt_finished`
- `interrupt_requested`
- `interrupt_resolved`
- `instance_waiting`
- `instance_resumed`
- `result_materialized`
- `channel_updated`
- `control_emitted`
- `route_selected`
- `route_unselected`
- `loop_continued`
- `loop_stopped`
- `instance_failed`
- `run_completed`

Materialized state is derived from this log. Reconciliation and replay should
append new corrective events instead of silently rewriting history.

## Scheduler Responsibilities

The scheduler becomes a runtime loop over instance state rather than a one-time
Prefect graph compilation.

The scheduler is responsible for:

- scanning the event log and current materialized state
- finding runnable instances
- launching attempts
- transitioning instances between `PENDING`, `RUNNABLE`, `RUNNING`, `WAITING`,
  `DONE`, and `FAILED`
- materializing results and control outputs
- activating new instances from controller route labels or loop control

The scheduler is explicitly not responsible for:

- running conditional scripts with side effects
- interpreting raw assistant or human reply envelopes as route conditions
- storing execution truth only in memory

## Migration Notes

The north star intentionally breaks several current assumptions:

- run state is no longer keyed only by `task_id`
- a logical task may have multiple runtime instances
- task-local `assistant_request.json` / `manual_gate.json` are no longer the
  scheduler's source of truth
- manual gates and assistant replies are interrupt channel data, not resume
  packets applied directly to downstream logic
- unselected branches are represented by route events, not `SKIPPED` runtime
  instances

The current runtime already took a hard cut to the explicit controller/input-
scope model. Future work should continue that approach instead of preserving
older control-plane shapes at the cost of clarity.

## Acceptance Scenarios

The eventual implementation should cover at least these cases:

1. A controller runs tests, emits `labels=["fix"]`, and activates the fix
   branch without creating placeholder instances for the unselected branch.
2. A controller emits multiple labels and fans out to multiple downstream
   targets in one step.
3. A controller creates more than one blocking interrupt in a single attempt,
   waits, and resumes only after all of them are resolved.
4. A controller or work node resumes using the same Codex session instead of a
   synthetic continuation prompt.
5. A loop controller emits `continue` with `next_inputs`, producing a fresh
   iteration with new instance ids.
6. A loop controller emits `stop`, activates exit targets, and closes the loop
   cleanly.
7. A downstream node branches on materialized state only and does not inspect
   raw assistant or human reply artifacts.
