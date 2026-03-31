You are establishing the baseline quality picture for a feature-scoped quality convergence workflow.

Read the staged feature brief and acceptance criteria.

Your job in this step:
1. Summarize the current feature or workflow under repair.
2. Identify the most likely current failure symptoms or evidence gaps.
3. Define the initial quality scope for this run.
4. State what kinds of evidence are currently missing.
5. Produce a concise baseline report in `result.json` and a readable summary in `final.md`.

Do not perform broad repair work in this step. Focus on freezing the starting point for the convergence loop.

Your `result.json` should be a JSON object with a `result` field that captures:
- `feature_summary`
- `suspected_failures`
- `evidence_gaps`
- `attempt_budget`
- `next_focus`
