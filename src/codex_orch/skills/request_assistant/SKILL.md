---
name: request-assistant
description: Create `assistant_request.json` artifacts for codex-orch runs without hand-writing protocol envelopes. Use when a Codex worker inside a codex-orch node needs clarification, policy guidance, approval, or control-plane help from the external assistant, and the request should be recorded through the stable helper contract instead of free-form JSON.
---

# Request Assistant

Use the bundled helper to create assistant requests.

Do not write `assistant_request.json` by hand.

## Default flow

1. Decide whether the node needs assistant help.
2. Write the human-readable question into a temp file if the text is long.
3. Call `scripts/request_assistant.sh` or `codex-orch assistant request create`.
4. Provide only semantic fields such as request kind, decision kind, options, and relevant artifact paths.
5. Let `codex-orch` fill `request_id`, `run_id`, `requester_task_id`, and timestamps.

## Minimal invocation

```bash
scripts/request_assistant.sh \
  --kind clarification \
  --decision-kind policy \
  --question-file /tmp/question.md \
  --option delete \
  --option keep_wrapper
```

## Notes

- Assume `CODEX_ORCH_PROGRAM_DIR`, `CODEX_ORCH_RUN_ID`, and `CODEX_ORCH_TASK_ID` are already present when the worker is running inside codex-orch.
- Prefer concise options when the human or assistant may need to choose between a few explicit outcomes.
- Use `--artifact` for published files that the assistant should inspect before answering.
- Read `references/protocol.md` if you need the exact field mapping or environment contract.
