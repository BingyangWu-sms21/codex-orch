Scan the codebase for maintenance issues.

Produce a prioritized backlog as `result.json` with:
- `backlog`: an ordered array of items, each with `id`, `title`, `severity`, and `location`
- `total_found`: count of issues discovered
- `summary`: one-paragraph overview

Order items by severity (critical first), then by estimated fix effort.
