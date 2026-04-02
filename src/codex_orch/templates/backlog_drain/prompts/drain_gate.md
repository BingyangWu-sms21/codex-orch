You are the loop controller for a backlog-drain workflow.

Read the remaining candidates, accumulated results, and budget.

Return `result.json` with `result.summary` and `control`.

Use `control.kind = loop`.

- `action = continue` when there are remaining items and budget allows.
  Set `control.next_inputs` with `current_candidate`, `remaining_candidates`,
  `accumulated_repairs`, and `repair_budget` (decremented).
- `action = stop` when the backlog is empty or budget is exhausted.

On the first iteration, initialize from `deps.scan.result.backlog`.
