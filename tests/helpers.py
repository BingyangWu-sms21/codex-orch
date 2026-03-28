from __future__ import annotations

from pathlib import Path

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


def sample_task(task_id: str, *, title: str, agent: str = "default") -> TaskSpec:
    return TaskSpec(
        id=task_id,
        title=title,
        agent=agent,
        status=TaskStatus.READY,
        publish=["final.md"],
    )
