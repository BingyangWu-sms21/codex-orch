# Backlog Drain Program

Example program demonstrating iterative backlog processing with loop control,
human-blocking review gates, and program asset update proposals.

## Workflow Topology

```
scan_and_prioritize
        |
    drain_gate  <------+
      /     \          |
 (continue) (stop)     |
     |         |       |
process_item   |       |
     |    drain_summary|
     +-----------------+
```

## How It Works

1. **scan_and_prioritize** scans the codebase and produces a prioritized
   backlog of maintenance items.

2. **drain_gate** (loop controller) picks the next item from the remaining
   backlog and passes it via `next_inputs`:
   - `current_item`: the item to process this iteration
   - `remaining_backlog`: items not yet processed
   - `accumulated_results`: results from previous iterations
   - `backlog_budget`: decremented each iteration

3. **process_item** implements and verifies the fix for one item. It must
   create a blocking human interrupt for review approval before completing
   (`required_decisions` enforces this). It may also propose
   `program_asset_update` proposals to update `inputs/known_findings.yaml`.

4. When the backlog is empty or budget is exhausted, drain_gate emits
   `action=stop` and activates **drain_summary**, which summarizes all
   processed items.

## Key Patterns Demonstrated

- **Loop with typed next_inputs**: Each iteration carries structured state
  (current item, remaining backlog, accumulated results) through the loop.
- **required_decisions**: `process_item` declares a `review` decision
  obligation with `audience: human`, so the engine will fail the instance if
  the worker does not create a matching blocking interrupt.
- **program_asset_update proposals**: The worker can propose updates to
  program-owned registries like `inputs/known_findings.yaml` through the
  assistant proposal mechanism.

## Running

```bash
codex-orch validate
codex-orch run start --root scan_and_prioritize --root drain_gate
```
