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
codex-orch project init . \
  --name my-program \
  --workspace "$PWD"
```

## Program layout

Expect a program directory with:

- `project.yaml`
- `tasks/`
- `presets/`
- `prompts/`
- `inputs/`
- `.runs/`

## Common inspection commands

```bash
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

## File-backed truth sources

When CLI output is not enough, inspect:

- `project.yaml`
- `tasks/*.yaml`
- `.runs/<run-id>/snapshot.json`
- `.runs/<run-id>/nodes/<task-id>/meta.json`
- `.runs/<run-id>/nodes/<task-id>/runtime.json`
- `.runs/<run-id>/nodes/<task-id>/published/`
