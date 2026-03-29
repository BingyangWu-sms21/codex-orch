# Operator Runbook

Assume your shell is already in the target codex-orch program directory and
`codex-orch` is available on `PATH`.

## Assistant inbox

List unresolved assistant requests:

```bash
codex-orch assistant request list . --json
```

Show one request:

```bash
codex-orch assistant request show . <request-id> --json
```

Write back an assistant answer:

```bash
codex-orch assistant respond . <request-id> \
  --resolution-kind auto_reply \
  --answer "Delete it." \
  --rationale "The existing policy allows the change." \
  --confidence high
```

If the assistant proposes a control-plane action, record it separately with:

```bash
codex-orch assistant action create . <request-id> ...
```

## Manual gates

List unresolved gates:

```bash
codex-orch manual-gate list . --json
```

Show one gate:

```bash
codex-orch manual-gate show . <gate-id> --json
```

Record the human answer:

```bash
codex-orch manual-gate respond . <gate-id> \
  --answer-file /tmp/human-answer.md
```

Approve or reject and optionally resume:

```bash
codex-orch manual-gate approve . <gate-id> --resume --json
codex-orch manual-gate reject . <gate-id> --resume --json
```

## Recovery and debugging

Use these files as the first stop when a run is stuck or surprising:

- `.runs/<run-id>/snapshot.json`
- `.runs/<run-id>/nodes/<task-id>/meta.json`
- `.runs/<run-id>/nodes/<task-id>/runtime.json`
- `.runs/<run-id>/nodes/<task-id>/events.jsonl`
- `.runs/<run-id>/nodes/<task-id>/stderr.log`
- `.runs/<run-id>/nodes/<task-id>/assistant_request.json`
- `.runs/<run-id>/nodes/<task-id>/assistant_response.json`
- `.runs/<run-id>/nodes/<task-id>/manual_gate.json`
- `.runs/<run-id>/nodes/<task-id>/human_request.json`
- `.runs/<run-id>/nodes/<task-id>/human_response.json`

Common failure patterns:

- missing assistant profile or context artifact: request creation fails immediately
- node waiting on assistant reply: inspect `assistant_request.json` and unresolved request list
- node waiting on manual gate: inspect `manual_gate.json` and `human_request.json`
- published artifact missing: inspect node prompt, outputs, and `published/`
