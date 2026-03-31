# Quality Convergence Program Example

This example demonstrates a feature-scoped quality convergence workflow in `codex-orch`.

Main shape:

```text
baseline_run
  -> quality_attempt
  -> quality_loop_gate
       continue -> quality_attempt
       stop     -> acceptance_gate
```

After the loop stops, `acceptance_gate` routes to:

- `approved -> publish_summary`
- `revise -> revision_requested`
- `expand_scope -> scope_review`

## What this example is for

Use this example when you want to model work that includes:

- implementation fixes
- test fixes
- test augmentation
- assistant escalation for triage/review/policy
- final human-blocking acceptance

The workflow treats test augmentation as part of quality convergence, not merely final evidence packaging.

## Before running it

Set the target repository root in one of two ways:

1. Edit `inputs/repo_workspace.txt`
2. Or override it at run start:

```bash
codex-orch run start . --root publish_summary --input-json repo_workspace="/absolute/path/to/target/repo"
```

## Included surfaces

- `project.yaml` uses `${inputs.repo_workspace}` for workspace selection.
- `tasks/` defines the quality loop and acceptance route.
- `prompts/` contains practical prompt splits for baseline, attempt, loop gate, and acceptance gate.
- `assistant_roles/` includes `test-triage`, `review`, and `policy` roles plus a shared operating model.
- `schemas/acceptance-decision.schema.json` shows a structured human acceptance decision shape.
