# Proposing Program Asset Updates

When you discover findings that should persist across runs, use
`program_asset_update` proposals through the assistant interrupt mechanism.
These proposals are recorded under the run and reviewed by the operator;
they are never auto-applied.

## When to Propose

- A scan discovers a new known finding that should be tracked in
  `inputs/known_findings.yaml`.
- A repair resolves an existing finding and its registry entry should be updated.
- An exemption is no longer justified and should be removed from
  `inputs/exemptions.yaml`.

## How to Propose

Include a `proposed_updates` entry in your assistant interrupt reply with:

```json
{
  "kind": "program_asset_update",
  "summary": "Add newly discovered auth deprecation finding",
  "rationale": "Scan found auth.legacy_verify() is deprecated across 3 modules",
  "suggested_content_mode": "snippet",
  "suggested_content": "- id: auth-deprecated-api\n  severity: medium\n  description: auth.legacy_verify() deprecated\n",
  "target": {
    "managed_asset_path": "inputs/known_findings.yaml"
  }
}
```

Key fields:

- `kind`: must be `program_asset_update`
- `target.managed_asset_path`: program-relative path to the file to update
- `suggested_content_mode`: `snippet` (merge into existing) or
  `full_replacement` (replace entire file)

## Operator Review

After a run completes, the operator reviews proposals with:

```bash
codex-orch proposal list <program-dir> <run-id>
codex-orch proposal show <program-dir> <run-id> <proposal-id>
codex-orch proposal mark <program-dir> <run-id> <proposal-id> --status accepted
```

Accepted proposals must still be manually applied to the repo files.
