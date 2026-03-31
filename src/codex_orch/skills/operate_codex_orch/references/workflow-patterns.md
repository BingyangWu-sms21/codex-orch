# Workflow Patterns

Use this guide when you already understand the static authoring surfaces and want examples of how to compose them into practical codex-orch workflows.

This guide focuses on reusable orchestration shapes. For basic authoring surfaces, read `references/program-authoring.md`. For assistant roles, routing, and managed preferences, read `references/assistant-control-plane.md`.

## Pattern selection guide

Use these patterns as starting points, not rigid templates.

- Choose a **linear flow** when work simply progresses from one step to the next.
- Choose a **route controller** when downstream branches depend on structured decisions.
- Choose a **loop controller** when work must iterate until a stop condition is met.
- Combine **loop + route** when you need iterative quality convergence followed by human-blocking acceptance.

## Pattern: linear work chain

Use this when each task depends on a prior result and no branching or looping is needed.

Example shape:

```text
analyze_spec -> implement_change -> summarize_result
```

Typical fit:

- straightforward implementation flows
- one-pass transformations
- report generation pipelines

## Pattern: route controller

Use a route controller when one node should examine upstream results and choose one or more downstream branches.

Example shape:

```text
analyze_change -> decide_route -> {apply_fix | publish_summary}
```

Authoring shape:

- upstream work task produces result/artifacts
- controller reads those refs
- controller publishes `result.json` with `control.kind=route`
- `control.routes[]` maps labels to downstream task ids

Use this when:

- different branches should activate based on structured output
- you want branching to be explicit and inspectable in runtime events

## Pattern: loop controller

Use a loop controller when the workflow should continue iterating until a stop condition is reached.

Example shape:

```text
attempt_work -> decide_continue
```

with `decide_continue` activating:

- `continue_targets` on `continue`
- `stop_targets` on `stop`

Use this when:

- each iteration should get a fresh instance id
- loop-carried inputs should flow into the next iteration
- you want the stop condition to be explicit in structured controller output

## Pattern: worker -> assistant -> human escalation

Use this when a work node should proceed autonomously most of the time but occasionally needs judgment support or approval.

Example shape:

```text
quality_attempt
  -> assistant interrupt when judgment support is needed
  -> human interrupt when approval or semantic judgment is required
```

Key points:

- assistant and human are interaction channels, not workflow nodes
- the worker instance resumes after replies are applied
- task `interaction_policy` may restrict which assistant roles are allowed and whether human fallback is permitted

Use this when:

- most work should remain autonomous
- only specific decisions need external input
- the same instance should keep its local continuity after the reply

## Pattern: quality convergence loop with human-blocking acceptance

This is a good fit for feature-scoped test repair, test augmentation, and acceptance work.

Example shape:

```text
baseline_run
  -> quality_attempt
  -> quality_loop_gate
       continue -> quality_attempt
       stop     -> acceptance_gate
```

### Intent

This pattern treats the workflow as quality convergence, not merely one-shot bug fixing.

The loop may include:

- implementation fixes
- test fixes
- test augmentation
- harness improvements
- repeated verification
- assistant or human handoff at key decisions

### Suggested role split

#### `baseline_run`

Purpose:

- freeze the initial quality picture
- record current failures
- record current evidence gaps
- establish the initial scope for the convergence loop

Typical outputs:

- `baseline_report.json`
- `baseline_summary.md`

#### `quality_attempt`

Purpose:

- perform one quality-improving attempt
- modify implementation, tests, or harness as needed
- add or improve tests when evidence is insufficient
- run the most informative validation available
- escalate to assistant or human at explicit decision points

Typical outputs:

- `result.json`
- `attempt_summary.md`
- published artifacts containing test evidence

Typical behavior:

- default to autonomy
- use assistant for judgment support
- use human for semantic, scope, or approval decisions

#### `quality_loop_gate`

Purpose:

- inspect the latest attempt output
- decide whether quality is still converging
- continue iterating or stop and move to acceptance

Typical controller mode:

- `control.mode: loop`

Typical stop reasons recorded in controller result:

- ready for acceptance
- needs human scope or policy decision
- budget exhausted
- stalled convergence

#### `acceptance_gate`

Purpose:

- perform final human-blocking acceptance
- present the converged evidence package
- route to approval, revision, or scope expansion based on the human decision

Typical controller mode:

- `control.mode: route`

Typical properties:

- uses human interrupt with blocking reply
- does not allow assistant to replace final acceptance judgment

### Minimal authoring sketch

The following sketch is intentionally minimal. It shows task boundaries and routing surfaces, not a complete production program.
A fuller runnable example lives at `examples/quality_convergence_program/` in this repository.
That runnable example includes extra compose refs such as `runtime.replies`, `runtime.latest_reply`, and acceptance input context that are omitted here to keep the sketch focused on topology.

#### `tasks/baseline_run.yaml`

```yaml
id: baseline_run
title: Record baseline quality state
kind: work
agent: default
status: ready
compose:
  - kind: file
    path: prompts/baseline_run.md
publish:
  - final.md
  - result.json
```

#### `tasks/quality_attempt.yaml`

```yaml
id: quality_attempt
title: Perform one quality convergence attempt
kind: work
agent: default
status: ready
labels:
  - quality
  - testing
depends_on:
  - task: baseline_run
    as: baseline
    kind: context
    consume:
      - result.json
compose:
  - kind: file
    path: prompts/quality_attempt.md
  - kind: ref
    ref: deps.baseline.result
publish:
  - final.md
  - result.json
assistant_hints:
  preferred_roles:
    - test-triage
  decision_kind_overrides:
    policy: policy
    review: review
  ask_when:
    - failure_root_cause_unclear
    - test_strategy_unclear
    - behavior_semantics_unclear
interaction_policy:
  allowed_assistant_roles:
    - test-triage
    - review
    - policy
  allow_human: true
```

#### `tasks/quality_loop_gate.yaml`

```yaml
id: quality_loop_gate
title: Decide whether quality is still converging
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

#### `tasks/acceptance_gate.yaml`

```yaml
id: acceptance_gate
title: Request final human acceptance
kind: controller
agent: default
status: ready
depends_on:
  - task: quality_loop_gate
    as: loop_gate
    kind: order
    consume: []
  - task: quality_attempt
    as: latest
    kind: context
    consume:
      - result.json
  - task: baseline_run
    as: baseline
    kind: context
    consume:
      - result.json
compose:
  - kind: file
    path: prompts/acceptance_gate.md
  - kind: ref
    ref: deps.latest.result
  - kind: ref
    ref: deps.baseline.result
publish:
  - final.md
  - result.json
assistant_hints:
  preferred_roles:
    - policy
    - review
  ask_when:
    - summarize_acceptance_risks
    - prepare_human_decision
interaction_policy:
  allowed_assistant_roles:
    - policy
    - review
  allow_human: true
control:
  mode: route
  routes:
    - label: approved
      targets:
        - publish_summary
    - label: revise
      targets:
        - revision_requested
    - label: expand_scope
      targets:
        - scope_review
```

### Prompt responsibilities

A useful prompt split is:

- `baseline_run.md`: define current failures, current evidence, and missing coverage
- `quality_attempt.md`: improve quality by changing implementation, tests, or harness; ask assistant/human only at explicit decision points
- `quality_loop_gate.md`: decide continue or stop based on convergence, evidence strength, and remaining gaps
- `acceptance_gate.md`: prepare a human decision package and require final human-blocking acceptance

### Why this pattern is useful

Use this pattern when:

- test augmentation itself may require many repair cycles
- E2E or cross-module verification is part of the work, not just final packaging
- you want human time spent on high-leverage decisions, not on routine debugging

## Pattern: proposal-driven control-plane refinement

Use this when you expect assistant behavior to evolve over time but want changes to remain reviewable.

Example shape:

```text
worker task
  -> assistant role responds
  -> assistant includes proposed_updates
  -> operator reviews proposal
  -> repo is edited manually
```

Use this when:

- routing hints need refinement
- role instructions need sharper boundaries
- managed preferences should absorb repeated user guidance

Key rule:

- proposals are review artifacts, not runtime auto-writebacks

## Pattern comparison

| Pattern | Best for | Main mechanism |
| --- | --- | --- |
| Linear flow | one-pass sequential work | work tasks + static dependencies |
| Route controller | explicit branching | controller with `control.mode: route` |
| Loop controller | iterative refinement | controller with `control.mode: loop` |
| Worker -> assistant -> human | localized escalation | interrupts and resume |
| Quality convergence + acceptance | feature-scoped repair and test development | loop controller + human-blocking route controller |
| Proposal-driven refinement | evolving role behavior with governance | `proposed_updates[]` + manual repo edits |

## Common mistakes

- modeling assistant or human as normal workflow nodes instead of interrupts
- using a controller when a linear work chain is enough
- trying to make one controller both loop and route in the same task
- treating final acceptance as an assistant decision instead of a human-blocking gate
- treating test augmentation as only a final acceptance artifact step when it actually needs looped quality work
