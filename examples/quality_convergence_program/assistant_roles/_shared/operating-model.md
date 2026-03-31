# Shared Assistant Operating Model for Quality Convergence

This example models feature-scoped quality convergence rather than one-shot bug fixing.

## Shared expectations

- `quality_attempt` should default to autonomy.
- Implementation changes, test changes, and test augmentation are all valid parts of convergence work.
- Ask assistant roles for judgment support, not for final acceptance.
- Final acceptance must remain human blocking.
- Treat managed assets as the authoritative source of durable role guidance.
- Treat role workspaces as scratch only.
- Proposals are review artifacts; they are not auto-applied.

## Surface ownership

- Put role capability in `assistant_roles/<role-id>/role.yaml -> policy`.
- Put stable role behavior in `assistant_roles/<role-id>/instructions.md`.
- Put durable user or project preferences in managed assets.
- Put task-local routing in task `assistant_hints` and `interaction_policy`.

## Handoff principles

- Use assistant for triage, review, and policy interpretation.
- Use human for acceptance, scope, and semantic approval decisions.
- If a question would change acceptance semantics or expand scope, prefer handoff to human.
