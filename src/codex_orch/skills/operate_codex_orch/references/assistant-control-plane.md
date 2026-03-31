# Assistant Control Plane

Use this guide when you are designing assistant roles, task-to-assistant routing, managed preferences, human handoff, or assistant update proposals inside a codex-orch program.

This guide explains which assistant-related information belongs on which program surface. For general task and controller authoring, read `references/program-authoring.md`.

## Surface map

| Content | Put it here |
| --- | --- |
| What a role is generally good at | `assistant_roles/<role-id>/role.yaml -> policy` |
| Which role a task prefers to ask | `tasks/<task-id>.yaml -> assistant_hints` |
| Which roles or human fallback a task allows | `tasks/<task-id>.yaml -> interaction_policy` |
| Role identity, style, and stable operating boundaries | `assistant_roles/<role-id>/instructions.md` |
| Durable user preferences or stable role guidance | a managed asset declared in `assistant_roles/<role-id>/role.yaml` |
| Shared assistant control-plane rules across roles | `assistant_roles/_shared/operating-model.md` |
| Private scratch notes and transient artifacts | `.codex-orch/assistant_roles/<role-id>/workspace/` |
| Assistant suggestions to update the above surfaces | `proposed_updates[]` recorded on the run |

## Role registry

Each assistant role lives under `assistant_roles/<role-id>/`.

The main file is `assistant_roles/<role-id>/role.yaml`.

Use `role.yaml` for structured role metadata such as:

- `id`
- `title`
- `description`
- `backend`
- `model`
- `sandbox`
- `instructions`
- `managed_assets`
- `policy`

Use `policy` for role-level routing capability:

- `request_kinds`
- `decision_kinds`
- `task_labels_any`
- `ask_when`

This is the right place to say what a role is generally good at. Do not put task-specific routing truth only in prose.

## Task-local routing

Task-local routing belongs in `tasks/<task-id>.yaml`.

### `assistant_hints`

Use `assistant_hints` for soft routing guidance:

- `preferred_roles`
- `decision_kind_overrides`
- `ask_when`

These hints influence recommendation, but they are not the only source of routing truth.

### `interaction_policy`

Use `interaction_policy` for hard limits:

- `allowed_assistant_roles`
- `allow_human`

Use this section when a task must restrict which assistant roles are valid, or when human fallback must be enabled or disabled.

## Instructions vs managed assets vs shared operating model

These surfaces serve different purposes.

### `assistant_roles/<role-id>/instructions.md`

Use role instructions for stable role behavior:

- what the role is responsible for
- how it should reason and communicate
- role-specific boundaries
- role-specific escalation style

Do not use role instructions for:

- task-local routing truth
- transient run observations
- long lists of durable user preferences when those belong in managed assets

### Managed assets

Managed assets are declared in `assistant_roles/<role-id>/role.yaml -> managed_assets`.

Use them for durable, repo-visible guidance such as:

- user decision preferences
- stable product or quality guidance
- long-term reminders that should be reviewed like other project assets

Managed assets are authoritative long-term guidance.

### `assistant_roles/_shared/operating-model.md`

Use the shared operating model for assistant rules that should apply across roles, such as:

- which surface owns which kind of information
- general proposal governance
- general handoff principles
- global control-plane norms

If a rule should apply to every assistant role in the program, prefer the shared operating model over duplicating it in every role instruction file.

### Role workspace

The assistant role workspace under `.codex-orch/assistant_roles/<role-id>/workspace/` is private scratch space.

Use it for:

- transient notes
- temporary tool outputs
- reusable scratch artifacts

Do not treat the role workspace as the authoritative source of user preference or policy.

## Handoff model

A common path is:

- worker task asks assistant
- assistant answers directly, or
- assistant hands off to human when user judgment is required

Human fallback is still constrained by the requester task's `interaction_policy`.

Use assistant for judgment support, review, recovery guidance, and policy interpretation. Use human for final approval, scope decisions, semantic changes, or other decisions that should not be delegated.

## Update proposals

Assistants may suggest updates to long-term control-plane surfaces, but those updates are not applied automatically.

Formal path:

1. Assistant returns a structured proposal.
2. codex-orch records it under the run.
3. A human or external coding agent reviews it.
4. If accepted, the repo is edited manually.
5. The operator marks the proposal status.

## Proposal kinds and target boundaries

### `instruction_update`

Use this when the current role's `instructions.md` should change.

Allowed target:

- the current assistant role only

### `managed_asset_update`

Use this when one of the current role's declared managed assets should change.

Allowed target:

- one of the current role's declared `managed_assets`

### `routing_policy_update`

Use this when the current requester task should change its routing behavior.

Allowed target:

- the current requester task only
- target section must be `assistant_hints` or `interaction_policy`

## Recommended authoring pattern

A good default pattern is:

1. Put role capability in `role.yaml -> policy`.
2. Put stable role behavior in `instructions.md`.
3. Put durable user preferences in managed assets.
4. Put task-specific routing in task YAML.
5. Put cross-role control-plane rules in `_shared/operating-model.md`.
6. Use proposals when assistants discover improvements, but keep repo updates manual and reviewable.

## Minimal example

A task-local routing example:

```yaml
assistant_hints:
  preferred_roles:
    - test-triage
  decision_kind_overrides:
    policy: policy
  ask_when:
    - failure_root_cause_unclear
    - behavior_semantics_unclear
interaction_policy:
  allowed_assistant_roles:
    - test-triage
    - review
    - policy
  allow_human: true
```

A role registry example:

```yaml
id: policy
title: Product and Acceptance Policy
backend: codex_cli
sandbox: workspace-write
instructions: instructions.md
managed_assets:
  - preferences.yaml
policy:
  request_kinds:
    - clarification
    - approval
  decision_kinds:
    - policy
    - scope
```

A managed asset might contain durable guidance such as:

```yaml
version: 1
preferences:
  acceptance_requires_human: true
  allow_test_changes_during_quality_loop: true
guidance:
  - Escalate to human when acceptance semantics change.
  - Treat test augmentation as part of quality convergence, not merely final evidence packaging.
```

## Common mistakes

- storing durable preferences only in task prompts or role workspace
- writing task-local routing logic only in role instructions
- duplicating global control-plane rules in many role files instead of using the shared operating model
- treating proposals as auto-apply writebacks
- allowing assistant to substitute for human on final acceptance decisions
