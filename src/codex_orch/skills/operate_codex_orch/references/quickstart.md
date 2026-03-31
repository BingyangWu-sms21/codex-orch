# Quickstart

Assume your shell is already in the target codex-orch program directory.

## Install the CLI

```bash
pipx install codex-orch
codex-orch --version
```

Upgrade later with:

```bash
pipx upgrade codex-orch
```

## Bootstrap a program in the current directory

```bash
codex-orch project init . my-program "$PWD"
```

## Program layout

Expect a program directory with:

- `project.yaml`
- `assistant_roles/_shared/operating-model.md`
- `tasks/`
- `presets/`
- `prompts/`
- `inputs/`
- `.runs/`

## Common inspection commands

```bash
codex-orch project validate . --json
codex-orch task list .
codex-orch task show . <task-id>
codex-orch edge list .
codex-orch run list .
codex-orch run show . <run-id> --json
```

## Run control

```bash
codex-orch run start . --root <task-id>
codex-orch run resume . <run-id>
codex-orch run reconcile . <run-id> --json
codex-orch run abort . <run-id> --json
```

## Inbox control

```bash
codex-orch interrupt list . --json
codex-orch inbox list . --json
codex-orch inbox show . <interrupt-id> --json
codex-orch inbox reply . <interrupt-id> --text "Answer" --resume
codex-orch inbox worker . --once --json
codex-orch proposal list . --json
```

## When to read the other references

- If you want to write or edit the program structure, read `references/program-authoring.md`.
- If you want to design assistant roles, routing, managed preferences, or update proposals, read `references/assistant-control-plane.md`.
- If you are handling a stuck run, inbox item, or proposal review, read `references/operator-runbook.md`.
- If you want a concrete end-to-end example, inspect `examples/quality_convergence_program/` in this repository.

## File-backed truth sources

When CLI output is not enough, inspect:

- `project.yaml`
- `tasks/*.yaml`
- `.runs/<run-id>/state/run.json`
- `.runs/<run-id>/state/instances/<instance-id>.json`
- `.runs/<run-id>/proposals/`
- `.runs/<run-id>/inbox/interrupts/`
- `.runs/<run-id>/inbox/replies/`
- `.runs/<run-id>/instances/<instance-id>/attempts/<attempt-no>/runtime.json`
- `.runs/<run-id>/instances/<instance-id>/published/`
