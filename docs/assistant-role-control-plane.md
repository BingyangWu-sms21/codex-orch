# Assistant Role Control Plane North Star

This document defines the target control plane for worker, assistant, and human
interaction in `codex-orch`.

The core role-based routing path is now implemented in the current runtime:

- program-local `assistant_roles/` registry
- role-targeted assistant interrupts
- task `assistant_hints` plus `interaction_policy`
- assistant dispatch that reads role instructions and managed assets

This document remains future-facing for the parts that are still intentionally
manual or deferred, such as managed asset promotion workflows and richer
controller/runtime integration.

For the current implemented runtime, see [docs/spec.md](./spec.md). For the
future controller-driven runtime that will consume richer workflow state, see
[docs/controller-runtime.md](./controller-runtime.md).

## Why the older model was not enough

The older profile-based runtime had three useful pieces:

- interrupts and inbox as the single interaction channel
- assistant execution config with backend/model/sandbox/workspace settings
- helper docs that tell a worker when to create an assistant interrupt

That was enough for "ask some assistant" and "fallback to a human".

It was not enough for:

- multiple named assistant roles with distinct responsibilities
- letting a worker explicitly choose who to ask
- giving the worker a system recommendation for who to ask and why
- separating private assistant scratch notes from managed long-term guidance
- accumulating user preferences in a controlled, role-scoped way

The core issue was that assistant selection lived as a task-level execution
config, while the needed model is interaction-level routing (`interrupt ->
recommended target role -> resolved target role`).

## Core Principles

- Worker remains the only executor of task work.
- Assistant and human are external input channels, not workflow nodes.
- Assistant targeting is interrupt-scoped, not task-scoped.
- Roles replace profiles as the long-term concept.
- System recommendations guide the worker, but the worker may explicitly
  override the assistant target.
- Long-term preferences are managed assets, not ad hoc notes in a private
  workspace.
- Private assistant scratch space may continue to exist, but it is never the
  authoritative source of user preference or policy.
- Human remains a generic fallback audience in this design; named human queues
  are explicitly out of scope.
- Promotion of long-term preference/guidance remains manual repo editing, not a
  runtime auto-apply workflow.

## Target-State Objects

### AssistantRole

`AssistantRole` is the primary author- and runtime-facing concept.

It replaces the older `assistant_profile` concept and owns:

- `id`
- `title`
- `description`
- execution config such as `backend`, `model`, `sandbox`
- role instructions
- responsibility policy used for recommendation
- read access to managed role-scoped preference assets
- private scratch workspace binding

North-star shape:

```yaml
id: policy
title: Product Policy Assistant
description: Handles user preference, policy, approval, and ambiguity.
backend: codex_cli
model: gpt-5.4
sandbox: workspace-write
instructions: assistant_roles/policy/instructions.md
managed_assets:
  - assistant_roles/policy/preferences.yaml
policy:
  request_kinds: [clarification, approval]
  decision_kinds: [policy, scope, sequencing]
  task_labels_any: [product, planning]
  ask_when:
    - missing_user_preference
    - approval_required
    - ambiguous_product_direction
```

### Role Registry

Each program has an explicit assistant role registry.

The north-star registry is program-local and repo-visible so that:

- worker guidance is inspectable
- role responsibilities can be reviewed in code review
- managed role assets can be edited manually in the repo

Suggested layout:

```text
assistant_roles/
└── <role-id>/
    ├── role.yaml
    ├── instructions.md
    └── preferences.yaml
```

The older global `profiles/` layout is no longer the primary public model.

### Interrupt Target

Assistant interrupts gain explicit targeting fields.

North-star interrupt additions:

- `requested_target_role_id`: optional explicit role chosen by the worker
- `recommended_target_role_id`: role recommended by the system
- `resolved_target_role_id`: the role the assistant worker actually dispatches to
- `target_resolution_reason`: short explanation of why that role was chosen

Rules:

- assistant interrupts always end with a concrete `resolved_target_role_id`
- if the worker explicitly sets `requested_target_role_id`, it wins unless the
  role is invalid or unavailable
- if the worker omits a target, the system recommendation becomes the resolved
  target

### TaskAssistantHints

Tasks may optionally declare hints that influence recommendations but do not act
as the sole source of truth.

Suggested shape:

```yaml
assistant_hints:
  preferred_roles: [policy]
  decision_kind_overrides:
    naming: style
    review: code-review
  ask_when:
    - policy
    - review
```

`TaskAssistantHints` are advisory. They are combined with role policy during
recommendation.

### ManagedPreferenceAsset

Each assistant role owns managed preference/guidance assets in its own scope.

This design intentionally chooses role-scoped managed assets, not:

- one global preference pool for all roles
- one project-wide pool shared by all roles
- profile-private workspace files as the source of truth

Suggested initial shape:

```yaml
version: 1
preferences:
  deletion_bias: conservative
  naming_style: explicit
guidance:
  - "Prefer preserving compatibility wrappers unless the user has approved removal."
  - "Escalate to human when product direction is ambiguous."
```

These assets are:

- repo-visible
- manually editable
- authoritative for long-term role behavior

## Recommendation Model

The main path is:

- system recommends a target role
- worker may explicitly override it

Recommendations come from `role policy + task hints`.

The recommendation algorithm should:

1. Start from the role registry.
2. Filter to roles compatible with `audience=assistant`.
3. Score by role policy:
   - matching `request_kind`
   - matching `decision_kind`
   - matching task labels / declared responsibilities
4. Apply task-local hints as tie-breakers or overrides.
5. Produce:
   - ranked candidates
   - one default recommendation
   - a short explanation for the top recommendation

The worker-facing helper and prompt should expose:

- recommended role id
- short reason
- explicit override syntax
- examples of what kinds of questions each role is for

## Worker Interaction Flow

The target-state worker loop is:

1. Worker reads task prompt plus interrupt helper guidance.
2. Worker decides whether this question should go to:
   - a recommended assistant role
   - another explicit assistant role
   - human
3. Worker creates an interrupt.
4. The interrupt records recommendation, request, and resolved target.
5. The assistant worker dispatches to the resolved assistant role.
6. The reply is injected only into the same instance resume path.

Worker guidance should explicitly answer:

- when to ask assistant at all
- which role is recommended for the current question
- when to skip assistant and go directly to human

## Assistant Execution Model

Assistant worker dispatch is role-aware in the north star.

Dispatch steps:

1. Load the resolved assistant role from the role registry.
2. Build the backend request from:
   - role instructions
   - managed role assets
   - task and interrupt metadata
   - staged context artifacts
3. Execute the role backend.
4. Return either:
   - direct answer
   - handoff to human

The role's private scratch workspace may be mounted for assistant execution, but
that workspace is explicitly not the source of managed preference truth.

## Managed Preference And Guidance Model

There are two distinct memory classes:

### Role Scratch

Private, mutable, execution-oriented storage for the assistant role.

Use cases:

- transient notes
- tool outputs
- reusable scratch artifacts

Properties:

- writable by the role backend
- not authoritative
- not assumed to be human-reviewed

### Managed Role Assets

Long-term preference/guidance assets owned by the role.

Use cases:

- durable user preferences
- stable policy reminders
- role-specific escalation guidance

Properties:

- repo-visible
- reviewed like other project assets
- read by the role backend as trusted input
- not directly mutated by runtime automation

## Proposal And Promotion Path

Assistant may still propose changes to long-term preference/guidance, but the
formal promotion path is manual repo editing.

North-star flow:

1. Assistant identifies a candidate preference/guidance update.
2. Assistant emits a structured proposal in its response metadata.
3. Human/operator reviews the proposal.
4. If accepted, the managed role asset is edited manually in the repo.

Explicit non-goals for this version:

- runtime auto-approval
- runtime auto-writeback of managed assets
- separate control-plane approval objects as the main path

This keeps governance simple and makes durable preference changes visible in the
normal repository review process.

## Human Interaction Model

Human remains a generic fallback audience.

This document intentionally does not define:

- named human queues
- human role registry
- human target recommendation

Human is simply the fallback when the assistant cannot answer directly or when
the question inherently requires human judgment.

## Relationship To The Current Runtime

Current implementation:

- interrupt resolves to a named assistant role
- assistant roles are explicitly declared in a program-local registry
- task `assistant_hints` influence recommendation
- task `interaction_policy` hard-limits allowed assistant roles and human
  fallback
- managed role assets replace private scratch as the authoritative source of
  long-term preference/guidance
- role scratch remains program-local private workspace state

## Migration Notes

Implemented in this round:

1. Assistant interrupts carry recommendation, request, and resolved target
   fields.
2. Assistant roles are loaded from a program-local registry.
3. Assistant worker dispatch resolves by role, not by task execution config.
4. Managed role assets are read as trusted role input.
5. Worker guidance degrades cleanly when no strong role recommendation exists.

Still intentionally deferred:

- runtime auto-promotion of managed assets
- named human routing
- controller/runtime integration that consumes richer role/control signals

## Acceptance Scenarios

1. A worker task hits a `policy` decision and sees a recommendation to ask the
   `policy` role, but can explicitly override to `style`.
2. Two tasks share the same role registry but produce different assistant
   recommendations because their task hints differ.
3. A role reads its managed preference asset plus its instructions when handling
   an interrupt.
4. A role emits a proposal to update managed guidance, but the repo remains
   unchanged until a human edits the managed asset manually.
5. An assistant chooses `handoff_to_human`, and the same worker instance later
   resumes with the human reply; no human sub-role routing is required.
