# Protocol Reference

## Purpose

This skill is only a thin wrapper around the stable codex-orch helper:

```bash
codex-orch assistant request create ...
```

The helper owns the protocol envelope. The worker only provides semantic fields.

## Runtime context

When a worker runs inside codex-orch, these environment variables should already
exist:

- `CODEX_ORCH_PROGRAM_DIR`
- `CODEX_ORCH_RUN_ID`
- `CODEX_ORCH_TASK_ID`
- `CODEX_ORCH_NODE_DIR`

`CODEX_ORCH_WORKSPACE_DIR` is the worker cwd for the current task. It is not the
same thing as the full writable scope. For `workspace-write` tasks, codex-orch also
grants node-local write access so helper commands can still materialize protocol
artifacts under `CODEX_ORCH_NODE_DIR`.

## Field ownership

- `codex-orch` helper owns:
  - `request_id`
  - `run_id`
  - `requester_task_id`
  - `created_at`
- Worker owns:
  - `request_kind`
  - `question`
  - `decision_kind`
  - `options`
  - `context_artifacts`
  - `requested_control_actions`
  - `priority`

## Typical examples

Policy clarification:

```bash
scripts/request_assistant.sh \
  --kind clarification \
  --decision-kind policy \
  --question "Can I delete the legacy wrapper?" \
  --option delete \
  --option keep_wrapper \
  --artifact .runs/run_123/nodes/draftRefactorContract/published/final.md
```

Scope clarification:

```bash
scripts/request_assistant.sh \
  --kind question \
  --decision-kind scope \
  --question-file /tmp/scope-question.md
```

## Anti-patterns

- Do not invent `request_id` values.
- Do not hand-edit `assistant_request.json`.
- Do not pass absolute paths in `--artifact`.
- Do not request assistant help for information that is already explicit in the current prompt or published artifacts.
