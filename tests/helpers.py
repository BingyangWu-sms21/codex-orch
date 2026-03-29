from __future__ import annotations

from pathlib import Path

import yaml

from codex_orch.domain import ProjectSpec, TaskSpec, TaskStatus
from codex_orch.store import ProjectStore


def build_test_store(tmp_path: Path) -> ProjectStore:
    program_dir = tmp_path / "program"
    global_root = tmp_path / ".global"
    store = ProjectStore(program_dir, global_root=global_root)
    store.save_project(
        ProjectSpec(
            name="test-program",
            workspace=str(program_dir),
            default_agent="default",
            default_sandbox="read-only",
            user_inputs={"brief": "inputs/brief.md"},
            max_concurrency=2,
        )
    )
    (store.paths.inputs_dir / "brief.md").write_text("brief input\n", encoding="utf-8")
    (store.paths.prompts_dir / "analyze.md").write_text("analyze prompt\n", encoding="utf-8")
    (store.paths.prompts_dir / "implement.md").write_text(
        "implement prompt\n",
        encoding="utf-8",
    )
    return store


def write_assistant_profile(
    store: ProjectStore,
    profile_id: str = "assistant-default",
    *,
    instructions: str = "Prefer concise answers.",
    sandbox: str = "workspace-write",
    set_as_default: bool = False,
) -> None:
    profile_dir = store.get_profile_dir(profile_id)
    profile_dir.mkdir(parents=True, exist_ok=True)
    store.get_profile_spec_path(profile_id).write_text(
        yaml.safe_dump(
            {
                "id": profile_id,
                "title": profile_id,
                "backend": "codex_cli",
                "sandbox": sandbox,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    store.get_profile_instructions_path(profile_id).write_text(
        instructions + "\n",
        encoding="utf-8",
    )
    store.get_profile_workspace_dir(profile_id)
    if set_as_default:
        project = store.load_project()
        store.save_project(
            project.model_copy(update={"default_assistant_profile": profile_id})
        )


def sample_task(task_id: str, *, title: str, agent: str = "default") -> TaskSpec:
    return TaskSpec(
        id=task_id,
        title=title,
        agent=agent,
        status=TaskStatus.READY,
        publish=["final.md"],
    )
