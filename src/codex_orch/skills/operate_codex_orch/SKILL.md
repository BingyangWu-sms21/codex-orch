---
name: operate-codex-orch
description: Operate a codex-orch program from the terminal. Use when an external agent or user-controlled coding assistant needs repo-specific instructions for setup, program inspection, runs, inbox handling, and recovery. This skill is for operators, not for workers running inside a codex-orch instance attempt.
---

# Operate codex-orch

Use this skill when you are acting as the operator of a codex-orch program.

Prefer the CLI and on-disk run artifacts as the source of truth.

Do not use this skill for workers running inside a codex-orch instance attempt.
Worker-side escalation is built into codex-orch runtime context.

Assume your current working directory is the target codex-orch program, not the
`codex-orch` source repository.

## Default flow

1. Ensure `codex-orch` is installed and on `PATH`: `pipx install codex-orch`
2. Verify the CLI is available: `codex-orch --version`
3. Read `references/quickstart.md` for bootstrap, program layout, and common CLI entrypoints.
4. Read `references/operator-runbook.md` for run control, inbox handling, and recovery steps.
5. Use `codex-orch` CLI commands directly instead of inventing or hand-editing JSON envelopes.
6. Inspect `.runs/`, `tasks/`, `project.yaml`, and instance/attempt artifacts when runtime state is ambiguous.

## Notes

- Treat files under `.runs/` as the runtime truth source.
- Prefer `--json` output when another agent will consume the results.
- Use `interrupt` and `inbox` CLI commands for human-in-the-loop paths.
- This skill intentionally does not cover the web UI workflow.
