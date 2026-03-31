# codex-orch

`codex-orch` is a local-first workflow runtime for multi-step coding tasks that need more than a single agent run.

It lets you define agent workflows as plain files, keep execution state on disk, pause for assistant or human input, and resume the same run later. It is useful when you want explicit route/loop control, inspectable runtime state, and human-in-the-loop decisions without introducing a database or hosted control plane.

## What codex-orch is for

Use `codex-orch` when you want workflows like:

- iterative test repair and quality convergence
- controller-driven branching and loops
- assistant escalation for triage, review, or policy questions
- human-blocking approval or acceptance gates
- local-first orchestration with inspectable on-disk state

Assistant roles can turn repeated guidance into reviewable proposals and managed preferences, so you do not have to restate the same habits in every run.

You probably do **not** need `codex-orch` if:

- a single agent run or script is enough
- you need a hosted multi-user control plane
- you want cloud orchestration more than local inspectability

## Example workflow

A representative workflow looks like this:

```text
baseline_run
  -> quality_attempt
  -> quality_loop_gate
       continue -> quality_attempt
       stop     -> acceptance_gate
```

In this pattern:

- `quality_attempt` may change implementation, tests, or test harnesses
- assistant roles can help with triage, review, or policy interpretation
- final acceptance remains human-blocking

See [`examples/quality_convergence_program/`](./examples/quality_convergence_program/) for a concrete example program.

## Why local-first

`codex-orch` keeps tasks, runtime state, interrupts, replies, proposals, and published outputs on disk.

That gives you a few useful properties:

- runs are inspectable with ordinary files and CLI commands
- human or assistant replies can pause and later resume the same workflow
- route and loop decisions are explicit in runtime artifacts
- you do not need a database just to orchestrate richer coding workflows

## Quick start

### Try the included example

```bash
uv sync --extra dev
uv run codex-orch task list examples/quality_convergence_program
uv run codex-orch project validate examples/quality_convergence_program --json
uv run codex-orch web examples/quality_convergence_program
```

- `task list` shows the example workflow structure
- `project validate` checks that the example program is internally consistent
- `web` opens the local UI for inspecting the program and later runs

To actually run the example, provide a real repository workspace through `repo_workspace`:

```bash
uv run codex-orch run start examples/quality_convergence_program \
  --root publish_summary \
  --input-json repo_workspace="/absolute/path/to/target/repo" \
  --no-wait
```

### Operate an existing program

From a codex-orch program directory:

```bash
pipx install codex-orch
codex-orch task list .
codex-orch project validate . --json
codex-orch run list .
```

- `task list` confirms the current directory is a program and shows its tasks
- `project validate` checks authoring issues before you start a run
- `run list` shows existing runs, if any

If you want a concrete starting point, inspect [`examples/quality_convergence_program/`](./examples/quality_convergence_program/).

## Core concepts

A `codex-orch` program is a directory containing task definitions, prompts, inputs, assistant roles, and run artifacts.

Core pieces:

- **work task**: a normal worker-style node that produces artifacts or structured output
- **controller task**: a node whose `result.json` controls route or loop decisions
- **interrupts**: runtime requests for assistant or human input
- **run artifacts**: on-disk state under `.runs/` for instances, attempts, replies, and outputs

A typical program layout looks like this:

```text
codex-programs/my-program/
├── assistant_roles/
│   └── _shared/operating-model.md
├── project.yaml
├── tasks/
├── prompts/
├── inputs/
├── presets/
├── .codex-orch/
└── .runs/
```

## Assistant, human, and operator flow

Running tasks can request input from assistant roles or a human without leaving the workflow model.

A blocking interrupt does not immediately kill the current attempt. Instead, the instance finishes the attempt, moves to `waiting`, and resumes the same run after the reply is recorded. This is how assistant escalation and human-blocking acceptance gates work in practice.

On the human side, `codex-orch` does not assume a special UI. The “human” role can be handled either by a person using the CLI inbox commands or by an external coding agent using the bundled `operate-codex-orch` skill to inspect runs, answer inbox items, and review proposals.

Assistant routing is task-local and can be constrained by `interaction_policy`. Human input remains the final path for approvals, acceptance, and other decisions that should not be delegated.

Operators typically handle replies through the inbox:

```bash
codex-orch inbox list /path/to/program --json
codex-orch inbox reply /path/to/program <interrupt-id> \
  --text "Answer" \
  --reply-kind answer \
  --resume
```

The built-in assistant worker can process unresolved assistant interrupts:

```bash
codex-orch inbox worker /path/to/program --once --json
```

For the full operator and authoring flow, see:

- [`src/codex_orch/skills/operate_codex_orch/`](./src/codex_orch/skills/operate_codex_orch/)
- [docs/assistant-role-control-plane.md](./docs/assistant-role-control-plane.md)

## Learn more

- [docs/spec.md](./docs/spec.md): current implemented storage and execution model
- [docs/controller-runtime.md](./docs/controller-runtime.md): controller-runtime north star for remaining workflow-state work
- [docs/assistant-role-control-plane.md](./docs/assistant-role-control-plane.md): assistant/human interaction control plane and role model
- [`examples/quality_convergence_program/`](./examples/quality_convergence_program/): concrete example program
- [`src/codex_orch/skills/operate_codex_orch/`](./src/codex_orch/skills/operate_codex_orch/): operator and authoring references

## External coding-agent support

The `codex-orch` package bundles an `operate-codex-orch` skill for external coding agents acting on the human/operator side of a program.

Most users will want to install it directly into a repo-scoped or user-scoped Claude skills directory:

```bash
codex-orch skill install operate-codex-orch --repo-dir /path/to/repo
# or
codex-orch skill install operate-codex-orch --user
```

For the full operator and authoring flow, see:

- [`src/codex_orch/skills/operate_codex_orch/`](./src/codex_orch/skills/operate_codex_orch/)
- [docs/assistant-role-control-plane.md](./docs/assistant-role-control-plane.md)

## Status

`codex-orch` is intentionally local-first and currently optimized for single-user workflows. The web UI is a thin convenience layer over the same file-backed domain logic used by the CLI.
