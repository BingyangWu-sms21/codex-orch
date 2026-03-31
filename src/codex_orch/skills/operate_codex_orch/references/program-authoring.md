# Program Authoring

Use this guide when you are writing or editing a codex-orch program.

This guide focuses on repo-visible authoring surfaces such as `project.yaml`, `tasks/`, `prompts/`, `inputs/`, `presets/`, and controller topology. It does not cover run operations in depth; for that, read `references/operator-runbook.md`. If the task involves assistant roles, routing, managed preferences, or human handoff, also read `references/assistant-control-plane.md`. If you want reusable orchestration examples, also read `references/workflow-patterns.md`.

## Authoring surfaces at a glance

| Surface | Use it for | Do not use it for |
| --- | --- | --- |
| `project.yaml` | Project defaults, workspace, user inputs, concurrency, and runtime timeouts | Task-local routing policy |
| `tasks/*.yaml` | Task graph, controller topology, compose steps, published artifacts, and task-local assistant hints/policy | Durable user preferences |
| `prompts/` | Stable prompt templates referenced by tasks | Runtime truth or routing truth |
| `inputs/` | Default typed input files loaded by `project.yaml` | Run results or temporary scratch notes |
| `presets/` | Reusable authoring bundles that create or preview tasks | In-flight runtime state |
| `assistant_roles/` | Assistant role registry, role instructions, shared operating model, and managed guidance assets | Transient scratch notes |
| `.runs/` | Runtime truth for concrete runs, instances, interrupts, and proposals | Authoring configuration |

## `project.yaml`

Use `project.yaml` for program-wide defaults and runtime budgets:

- `name`
- `workspace`
- `default_agent`
- `default_model`
- `default_sandbox`
- `max_concurrency`
- `node_wall_timeout_sec`
- `node_idle_timeout_sec`
- `node_terminate_grace_sec`
- `user_inputs`

Use `user_inputs` to point at default input files under `inputs/`. Those files are parsed by extension into typed JSON-compatible values at run creation time.

## `tasks/*.yaml`

Each task file defines one logical node in the author-facing graph.

Key fields:

- `kind: work | controller`
- `depends_on`
- `compose`
- `publish`
- `assistant_hints`
- `interaction_policy`
- `workspace`
- `extra_writable_roots`
- `result_schema`

### `result_schema` and Codex structured output

Use `result_schema` only when the task should return structured JSON through
Codex output-schema mode.

Authoring guidance:

- Prefer a conservative JSON Schema subset.
- Always declare explicit `type` alongside `const` or `enum`.
- For object nodes, declare:
  - `type: object`
  - `additionalProperties: false`
  - `required` covering every property
- For array nodes, declare `type: array` alongside `items`.
- Avoid relying on `default`, `$ref`, `$defs`, `oneOf`, `allOf`,
  `patternProperties`, `if/then/else`, and similar advanced JSON Schema
  features unless you have separately verified Codex accepts them.

Validation workflow:

- `codex-orch project validate . --json` reports:
  - blocking `errors` for invalid program authoring
  - non-blocking `warnings` for Codex output-schema compatibility concerns
- `project validate` exits:
  - `0` when there are no issues
  - `1` when there are warnings only
  - `2` when there are blocking errors
- `run start` and task editing continue on warnings, so use `project validate`
  as the author-facing preflight check before representative runs.

Reference URLs:

- OpenAI Structured Outputs guide:
  `https://platform.openai.com/docs/guides/structured-outputs`
- Responses vs Chat Completions guide:
  `https://platform.openai.com/docs/guides/responses-vs-chat-completions`

### `work` vs `controller`

Use `work` for normal worker-style nodes that produce artifacts and optional structured result files.

Use `controller` when the scheduler should consume structured control output from `result.json`.

Controller nodes declare `control.mode`:

- `route` for branch selection via route labels
- `loop` for tail-loop continuation via `continue` or `stop`

Only controllers may declare `control`.

## Compose references

Use `compose` to build the worker prompt from prompt files, literals, and runtime references.

Current `compose.ref` forms are:

- `deps.<scope>.result`
- `deps.<scope>.artifacts.<relative-path>`
- `inputs.<key>`
- `runtime.replies`
- `runtime.latest_reply`

Notes:

- Artifact refs require a matching `context` dependency.
- The referenced artifact path must also appear in that dependency's `consume` list.
- Dependency scopes come from `depends_on[].as`, or else the upstream task id.

## Common authoring patterns

For reusable end-to-end workflow shapes, read `references/workflow-patterns.md`.
For a concrete example program in this repository, inspect `examples/quality_convergence_program/`.

Typical building blocks are:

### Linear flow

Use ordinary `work` tasks with `order` or `context` dependencies when downstream work simply follows upstream work.

### Route controller

Use a `controller` with `control.mode: route` when a node should choose one or more downstream branches based on structured output.

### Loop controller

Use a `controller` with `control.mode: loop` when the workflow should continue iterating until a stop condition is reached.

A common pattern is a quality loop followed by a human-blocking acceptance route:

- `baseline_run`
- `quality_attempt`
- `quality_loop_gate`
- `acceptance_gate`

## Task-local assistant fields

Use task-local assistant fields when a node needs help from assistant roles or may escalate to human.

### `assistant_hints`

Use for soft routing guidance such as:

- preferred roles
- decision-kind-specific role overrides
- task-local signals that should bias role recommendation

### `interaction_policy`

Use for hard constraints such as:

- which assistant roles are allowed for this task
- whether human fallback is allowed

For the deeper assistant control-plane model, read `references/assistant-control-plane.md`.

## Validation and inspection workflow

After editing authoring surfaces:

1. Inspect tasks with `codex-orch task list .` and `codex-orch task show . <task-id>`.
2. Inspect edges with `codex-orch edge list .`.
3. Start a representative run and inspect `.runs/<run-id>/state/`.
4. Inspect prompts, published artifacts, interrupts, and proposals from that run.

Do not rely only on static YAML review; use run artifacts to confirm the runtime behavior you intended.

## Common authoring mistakes

- putting durable user preferences into task prompts instead of managed assistant assets
- writing routing policy only as prose instead of using `assistant_hints` and `interaction_policy`
- treating `.runs/` as an authoring surface
- treating assistant role workspace state as authoritative guidance
- putting too much role identity or escalation behavior into task files instead of role instructions

## Minimal example

A minimal linear flow might look like this:

```yaml
id: analyze
kind: work
agent: default
status: ready
compose:
  - kind: file
    path: prompts/analyze.md
publish:
  - final.md
```

A controller task might look like this:

```yaml
id: quality_loop_gate
kind: controller
agent: default
status: ready
depends_on:
  - task: quality_attempt
    as: latest
    kind: context
    consume:
      - result.json
compose:
  - kind: file
    path: prompts/quality_loop_gate.md
  - kind: ref
    ref: deps.latest.result
publish:
  - final.md
  - result.json
control:
  mode: loop
  continue_targets:
    - quality_attempt
  stop_targets:
    - acceptance_gate
```

A task with assistant routing hints might look like this:

```yaml
assistant_hints:
  preferred_roles:
    - test-triage
  decision_kind_overrides:
    policy: policy
interaction_policy:
  allowed_assistant_roles:
    - test-triage
    - review
    - policy
  allow_human: true
```
