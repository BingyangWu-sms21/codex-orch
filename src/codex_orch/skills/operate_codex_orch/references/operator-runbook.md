# Operator Runbook

Assume your shell is already in the target codex-orch program directory and `codex-orch` is available on `PATH`.

## Inbox

List unresolved inbox items:

```bash
codex-orch inbox list . --json
```

Show one interrupt:

```bash
codex-orch inbox show . <interrupt-id> --json
```

Write back an answer and resume the run:

```bash
codex-orch inbox reply . <interrupt-id> \
  --text "Delete it." \
  --reply-kind answer \
  --resume
```

Let the built-in assistant worker process unresolved assistant interrupts:

```bash
codex-orch inbox worker . --once --json
```

## Proposals

List recorded assistant update proposals:

```bash
codex-orch proposal list . --json
codex-orch proposal show . <proposal-id> --json
```

Mark a proposal after manual review or repo editing:

```bash
codex-orch proposal mark . <proposal-id> \
  --status applied \
  --note "updated manually"
```

### Proposal boundaries

Proposals are recorded, not auto-applied.

- `instruction_update` targets the current assistant role's `instructions.md`
- `managed_asset_update` targets one of the current assistant role's declared managed assets
- `routing_policy_update` targets the requester task's `assistant_hints` or `interaction_policy`

If you need the authoring model behind these surfaces, read `references/assistant-control-plane.md`.

## Recovery and debugging

Use these files as the first stop when a run is stuck or surprising:

- `.runs/<run-id>/state/run.json`
- `.runs/<run-id>/state/instances/<instance-id>.json`
- `.runs/<run-id>/events/*.json`
- `.runs/<run-id>/proposals/*.json`
- `.runs/<run-id>/inbox/interrupts/<interrupt-id>.json`
- `.runs/<run-id>/inbox/replies/<interrupt-id>.json`
- `assistant_roles/_shared/operating-model.md`
- `.runs/<run-id>/instances/<instance-id>/session.json`
- `.runs/<run-id>/instances/<instance-id>/attempts/<attempt-no>/runtime.json`
- `.runs/<run-id>/instances/<instance-id>/attempts/<attempt-no>/events.jsonl`
- `.runs/<run-id>/instances/<instance-id>/attempts/<attempt-no>/stderr.log`

Common failure patterns:

- missing assistant role or context artifact: interrupt remains unresolved or the assistant worker skips it
- missing assistant operating model: assistant worker fails until `assistant_roles/_shared/operating-model.md` is installed
- instance waiting on replies: inspect unresolved interrupts and the latest attempt runtime
- published artifact missing: inspect attempt prompt, outputs, and instance `published/`
- reply schema mismatch: assistant reply may fail validation and the interrupt remains unresolved
- human handoff blocked by policy: task `interaction_policy` may disallow human fallback

## Stuck run checklist

1. Inspect `.runs/<run-id>/state/run.json`.
2. Inspect the relevant `.runs/<run-id>/state/instances/<instance-id>.json`.
3. Check unresolved interrupts with `codex-orch inbox list . --json`.
4. Inspect the latest attempt's `runtime.json`, `events.jsonl`, and `stderr.log`.
5. Inspect `.runs/<run-id>/proposals/` if the assistant suggested authoring changes.
6. Decide whether to reply, resume, edit the repo manually, or abort the run.
