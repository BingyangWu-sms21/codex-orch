# Creating Blocking Interrupts for Decision Obligations

When a task declares `required_decisions`, you MUST create matching blocking
interrupts before completing the task. The engine checks at attempt completion
and will reject your work with `failure_kind=decision_obligation` if any
required decision was not created.

## How to Create a Blocking Interrupt

Use `codex-orch interrupt create` with these required arguments:

```bash
codex-orch interrupt create \
  --program-dir "$CODEX_ORCH_PROGRAM_DIR" \
  --run-id "$CODEX_ORCH_RUN_ID" \
  --instance-id "$CODEX_ORCH_INSTANCE_ID" \
  --task-id "$CODEX_ORCH_TASK_ID" \
  --audience human \
  --kind approval \
  --decision-kind review \
  --question "Review the proposed changes before proceeding" \
  --blocking
```

Key fields:

- `--audience`: must match the `audience` in `required_decisions` (`human`,
  `assistant`, or either if `any`)
- `--decision-kind`: must match the `decision_kind` in `required_decisions`
- `--blocking`: critical; non-blocking interrupts do not satisfy the obligation
- `--question`: describe what the reviewer should evaluate

## Workflow

1. Complete your implementation and verification work.
2. Create the blocking interrupt with a clear summary of changes and risks.
3. The engine moves the instance to `waiting` state.
4. A human or assistant reviews and replies through the inbox.
5. The instance resumes with the reply available in `runtime.replies`.

## Common Mistakes

- Forgetting to create the interrupt entirely: task fails with
  `decision_obligation` error.
- Creating a non-blocking interrupt (`--blocking` omitted): does not satisfy the
  obligation.
- Mismatched `decision_kind` or `audience`: the engine matches these against the
  task declaration. `--decision-kind policy` does not satisfy a `review`
  obligation.
