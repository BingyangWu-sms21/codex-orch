---
name: operate-codex-orch
description: Operate or author a codex-orch program from the terminal. Use when an external agent or user-controlled coding assistant needs repo-specific instructions for setup, program inspection, authoring surfaces, runs, inbox handling, assistant control-plane design, workflow patterns, and recovery. This skill is for operators and program authors, not for workers running inside a codex-orch instance attempt.
---

# Operate codex-orch

Use this skill when you are acting as the operator or program author of a codex-orch program.

Prefer the CLI and on-disk artifacts as the source of truth.

Do not use this skill for workers running inside a codex-orch instance attempt. Worker-side escalation is built into codex-orch runtime context.

Assume your current working directory is the target codex-orch program, not the `codex-orch` source repository.

## Choose the relevant reference

Read only what you need:

- For install, bootstrap, program layout, and basic inspection: `references/quickstart.md`
- For runs, inbox handling, proposal review, and recovery: `references/operator-runbook.md`
- For writing or editing program structure: `references/program-authoring.md`
- For assistant roles, routing, managed preferences, and update proposals: `references/assistant-control-plane.md`
- For reusable orchestration examples and end-to-end workflow shapes: `references/workflow-patterns.md`

## Default operator flow

1. Ensure `codex-orch` is installed and on `PATH`: `pipx install codex-orch`
2. Verify the CLI is available: `codex-orch --version`
3. Read `references/quickstart.md` for bootstrap, layout, and common CLI entrypoints.
4. Read `references/operator-runbook.md` when dealing with runs, inbox items, proposals, or recovery.
5. Use `codex-orch` CLI commands directly instead of inventing or hand-editing runtime envelopes.
6. Inspect `.runs/`, `tasks/`, `project.yaml`, and instance/attempt artifacts when runtime state is ambiguous.

## Default authoring flow

1. Read `references/program-authoring.md` before editing `project.yaml`, `tasks/`, `prompts/`, `inputs/`, `presets/`, or controller topology.
2. If the task involves assistant roles or human handoff, also read `references/assistant-control-plane.md`.
3. If you need example orchestration shapes, also read `references/workflow-patterns.md`.
4. Edit repo-visible authoring surfaces instead of inventing ad hoc runtime conventions.
5. Validate behavior with CLI inspection and run artifacts.

## Notes

- Treat files under `.runs/` as runtime truth, not authoring config.
- Prefer `--json` output when another agent will consume the results.
- Use `interrupt` and `inbox` CLI commands for human-in-the-loop paths.
- Authoring truth lives in repo-visible program files such as `project.yaml`, `tasks/`, and `assistant_roles/`.
- This skill intentionally does not cover the web UI workflow.
