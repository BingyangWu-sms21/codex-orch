from __future__ import annotations

from pathlib import Path

import yaml

from codex_orch.assistant_docs import install_assistant_operating_model
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
    install_assistant_operating_model(store.paths.root, overwrite=False)
    return store


def write_assistant_role(
    store: ProjectStore,
    role_id: str = "policy",
    *,
    instructions: str = "Prefer concise answers.",
    description: str = "",
    sandbox: str = "workspace-write",
    request_kinds: list[str] | None = None,
    decision_kinds: list[str] | None = None,
    managed_asset_contents: str = "version: 1\npreferences:\n  deletion_bias: conservative\n",
) -> None:
    role_dir = store.get_assistant_role_dir(role_id)
    role_dir.mkdir(parents=True, exist_ok=True)
    (role_dir / "preferences.yaml").write_text(
        managed_asset_contents,
        encoding="utf-8",
    )
    store.get_assistant_role_spec_path(role_id).write_text(
        yaml.safe_dump(
            {
                "id": role_id,
                "title": role_id,
                "description": description,
                "backend": "codex_cli",
                "sandbox": sandbox,
                "instructions": "instructions.md",
                "managed_assets": ["preferences.yaml"],
                "policy": {
                    "request_kinds": request_kinds or ["clarification"],
                    "decision_kinds": decision_kinds or ["policy"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (role_dir / "instructions.md").write_text(
        instructions + "\n",
        encoding="utf-8",
    )
    store.get_assistant_role_workspace_dir(role_id)

def sample_task(task_id: str, *, title: str, agent: str = "default") -> TaskSpec:
    return TaskSpec(
        id=task_id,
        title=title,
        agent=agent,
        status=TaskStatus.READY,
        publish=["final.md"],
    )


def instance_for_task(run, task_id: str):
    matches = [instance for instance in run.instances.values() if instance.task_id == task_id]
    assert len(matches) == 1
    return matches[0]
