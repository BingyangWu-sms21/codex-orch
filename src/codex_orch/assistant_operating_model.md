# Assistant Operating Model

This document defines what information belongs in each assistant control-plane
surface and how update proposals should work.

## Surfaces

### Role Instructions

`assistant_roles/<role-id>/instructions.md`

Use this file for role identity and operating style:

- what the role is responsible for
- how the role should reason and communicate
- role-specific boundaries or escalation rules
- stable role behavior that should apply across tasks

Do not use role instructions for:

- task-specific routing policy
- transient scratch notes
- long lists of evolving user preferences when they belong in managed assets

### Managed Assets

`assistant_roles/<role-id>/role.yaml -> managed_assets`

Use managed assets for long-term role-visible guidance such as:

- durable user preferences
- stable product or coding guidance
- role-specific reminders that should be repo-visible and manually reviewed

Managed assets are authoritative long-term guidance. They are repo-visible and
must be updated by editing the repo, not by runtime auto-writeback.

Do not use managed assets for:

- transient notes
- per-run observations
- routing policy that should live on tasks or role policy fields

### Routing Policy

Routing policy is structured configuration, not free-form memory.

Relevant locations:

- role routing policy: `assistant_roles/<role-id>/role.yaml -> policy`
- task routing hints: `tasks/<task-id>.yaml -> assistant_hints`
- task routing constraints: `tasks/<task-id>.yaml -> interaction_policy`

Use routing policy for:

- which kinds of questions a role is good at
- task-local preferred roles
- task-local hard limits on which roles or human fallback are allowed

Do not store routing policy only as prose in instructions or managed assets.

### Role Workspace

`program/.codex-orch/assistant_roles/<role-id>/workspace/`

This is private scratch space. It is useful for:

- transient notes
- tool outputs
- reusable scratch artifacts

It is not authoritative truth and should not be treated as long-term managed
guidance.

## Update Proposals

Assistant may propose updates, but proposals are not applied automatically.

Formal path:

1. Assistant returns a structured proposal.
2. codex-orch records that proposal under the run.
3. A human or external coding agent reviews it.
4. If accepted, the repo is edited manually.
5. The operator marks the proposal status in codex-orch.

## Allowed Proposal Kinds

### `instruction_update`

Use when the current role's `instructions.md` should be adjusted.

Allowed scope:

- current assistant role only

### `managed_asset_update`

Use when one of the current role's declared managed assets should change.

Allowed scope:

- current assistant role only
- target must reference one of the current role's declared `managed_assets`

### `routing_policy_update`

Use when the current requester task should change `assistant_hints` or
`interaction_policy`.

Allowed scope:

- current requester task only
- target section must be either `assistant_hints` or `interaction_policy`

## Proposal Writing Guidance

Each proposal should include:

- a short summary
- rationale
- suggested content
- the target surface and file

Prefer content that a human or external coding agent can directly apply with
minimal interpretation.

`snippet` mode means "insert or merge this into the target".

`full_replacement` mode means "replace the target surface with this content".
