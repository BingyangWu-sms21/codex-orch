Process the current backlog item from `inputs.current_candidate`.

After completing your work, you MUST create a blocking human interrupt
for review approval. See `runtime_guidance/decision_obligations.md`
for instructions.

Produce `result.json` with:
- `item_id`: the id of the processed item
- `status`: "fixed" | "skipped" | "deferred"
- `summary`: what was done
