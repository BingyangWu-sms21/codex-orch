# Operator Runbook

Assume your shell is already in the target codex-orch program directory and
`codex-orch` is available on `PATH`.

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

## Recovery and debugging

Use these files as the first stop when a run is stuck or surprising:

- `.runs/<run-id>/state/run.json`
- `.runs/<run-id>/state/instances/<instance-id>.json`
- `.runs/<run-id>/events/*.json`
- `.runs/<run-id>/inbox/interrupts/<interrupt-id>.json`
- `.runs/<run-id>/inbox/replies/<interrupt-id>.json`
- `.runs/<run-id>/instances/<instance-id>/session.json`
- `.runs/<run-id>/instances/<instance-id>/attempts/<attempt-no>/runtime.json`
- `.runs/<run-id>/instances/<instance-id>/attempts/<attempt-no>/events.jsonl`
- `.runs/<run-id>/instances/<instance-id>/attempts/<attempt-no>/stderr.log`

Common failure patterns:

- missing assistant profile or context artifact: interrupt remains unresolved or the assistant worker skips it
- instance waiting on replies: inspect unresolved interrupts and the latest attempt runtime
- published artifact missing: inspect attempt prompt, outputs, and instance `published/`
