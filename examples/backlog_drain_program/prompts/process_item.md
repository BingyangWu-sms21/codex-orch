You are processing a single backlog item.

Read the current item from inputs. Implement the fix, run relevant tests, and verify correctness.

Before completing, you must create a blocking human interrupt to get review approval for your changes. Use `codex-orch interrupt create` with `--kind approval --decision-kind review`.

If the fix introduces changes to the known findings registry, propose a `program_asset_update` to update `inputs/known_findings.yaml` through the assistant interrupt.

Produce `result.json` with:
- `item_id`: the id of the processed item
- `status`: "fixed" | "skipped" | "deferred"
- `summary`: what was done
- `files_changed`: list of modified file paths
