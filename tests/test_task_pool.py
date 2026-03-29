from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codex_orch.domain import DependencyKind, TaskSpec, TaskStatus
from codex_orch.task_pool import TaskPoolService
from tests.helpers import build_test_store


def test_apply_preset_creates_tasks(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    preset_path = store.paths.presets_dir / "bundle.yaml"
    preset_path.write_text(
        yaml.safe_dump(
            {
                "id": "bundle",
                "title": "Bundle",
                "variables": {"topic": {"required": True}},
                "tasks": [
                    {
                        "id": "analyze-${topic}",
                        "title": "Analyze ${topic}",
                        "agent": "explorer",
                        "status": "ready",
                        "compose": [{"kind": "literal", "text": "inspect ${topic}"}],
                        "publish": ["final.md"],
                    },
                    {
                        "id": "summarize-${topic}",
                        "title": "Summarize ${topic}",
                        "agent": "default",
                        "status": "ready",
                        "depends_on": [
                            {
                                "task": "analyze-${topic}",
                                "kind": "context",
                                "consume": ["final.md"],
                            }
                        ],
                        "compose": [{"kind": "literal", "text": "summarize ${topic}"}],
                        "publish": ["final.md"],
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    pool = TaskPoolService(store)
    created = pool.apply_preset("bundle", {"topic": "solver"})

    assert [task.id for task in created] == ["analyze-solver", "summarize-solver"]
    assert store.get_task("summarize-solver").depends_on[0].task == "analyze-solver"


def test_validate_graph_rejects_cycles(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    task_a = TaskSpec(
        id="task-a",
        title="Task A",
        agent="default",
        status=TaskStatus.READY,
        depends_on=[{"task": "task-b", "kind": "order", "consume": []}],
        publish=["final.md"],
    )
    task_b = TaskSpec(
        id="task-b",
        title="Task B",
        agent="default",
        status=TaskStatus.READY,
        depends_on=[{"task": "task-a", "kind": "order", "consume": []}],
        publish=["final.md"],
    )
    store.save_task(task_a)
    store.save_task(task_b)

    with pytest.raises(ValueError, match="cycle detected"):
        TaskPoolService(store).validate_graph()


def test_validate_graph_allows_unused_context_consumes(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="source",
            title="Source",
            agent="default",
            status=TaskStatus.READY,
            publish=["final.md", "notes.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="target",
            title="Target",
            agent="default",
            status=TaskStatus.READY,
            depends_on=[
                {
                    "task": "source",
                    "kind": "context",
                    "consume": ["final.md", "notes.md"],
                }
            ],
            compose=[{"kind": "from_dep", "task": "source", "path": "final.md"}],
            publish=["final.md"],
        )
    )

    TaskPoolService(store).validate_graph()


def test_validate_graph_rejects_from_dep_without_matching_dependency(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="target",
            title="Target",
            agent="default",
            status=TaskStatus.READY,
            compose=[{"kind": "from_dep", "task": "source", "path": "final.md"}],
            publish=["final.md"],
        )
    )

    with pytest.raises(ValueError, match="compose.from_dep references undeclared dependency source"):
        TaskPoolService(store).validate_graph()


def test_validate_graph_rejects_from_dep_with_order_dependency(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="source",
            title="Source",
            agent="default",
            status=TaskStatus.READY,
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="target",
            title="Target",
            agent="default",
            status=TaskStatus.READY,
            depends_on=[{"task": "source", "kind": "order", "consume": []}],
            compose=[{"kind": "from_dep", "task": "source", "path": "final.md"}],
            publish=["final.md"],
        )
    )

    with pytest.raises(ValueError, match="requires a context dependency"):
        TaskPoolService(store).validate_graph()


def test_validate_graph_rejects_from_dep_path_not_listed_in_consume(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="source",
            title="Source",
            agent="default",
            status=TaskStatus.READY,
            publish=["final.md", "notes.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="target",
            title="Target",
            agent="default",
            status=TaskStatus.READY,
            depends_on=[{"task": "source", "kind": "context", "consume": ["final.md"]}],
            compose=[{"kind": "from_dep", "task": "source", "path": "notes.md"}],
            publish=["final.md"],
        )
    )

    with pytest.raises(ValueError, match="must be listed in the matching context dependency consume"):
        TaskPoolService(store).validate_graph()


def test_validate_graph_rejects_duplicate_context_dependencies(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="source",
            title="Source",
            agent="default",
            status=TaskStatus.READY,
            publish=["final.md", "notes.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="target",
            title="Target",
            agent="default",
            status=TaskStatus.READY,
            depends_on=[
                {"task": "source", "kind": "context", "consume": ["final.md"]},
                {"task": "source", "kind": "context", "consume": ["notes.md"]},
            ],
            publish=["final.md"],
        )
    )

    with pytest.raises(ValueError, match="multiple context dependencies on task source"):
        TaskPoolService(store).validate_graph()


def test_add_and_remove_edges(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    pool = TaskPoolService(store)
    store.save_task(
        TaskSpec(
            id="source",
            title="Source",
            agent="default",
            status=TaskStatus.READY,
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="target",
            title="Target",
            agent="default",
            status=TaskStatus.READY,
            publish=["final.md"],
        )
    )

    store.add_edge(
        source_task_id="source",
        target_task_id="target",
        kind=DependencyKind.CONTEXT,
        consume=["final.md"],
    )
    edges = pool.list_edges()
    assert len(edges) == 1
    assert edges[0].source == "source"

    store.remove_edge(
        source_task_id="source",
        target_task_id="target",
        kind=DependencyKind.CONTEXT,
    )
    assert pool.list_edges() == []


def test_preview_preset_rejects_invalid_from_dep_contract(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    preset_path = store.paths.presets_dir / "bundle.yaml"
    preset_path.write_text(
        yaml.safe_dump(
            {
                "id": "bundle",
                "title": "Bundle",
                "variables": {},
                "tasks": [
                    {
                        "id": "source",
                        "title": "Source",
                        "agent": "default",
                        "status": "ready",
                        "publish": ["final.md"],
                    },
                    {
                        "id": "target",
                        "title": "Target",
                        "agent": "default",
                        "status": "ready",
                        "depends_on": [
                            {
                                "task": "source",
                                "kind": "order",
                                "consume": [],
                            }
                        ],
                        "compose": [
                            {"kind": "from_dep", "task": "source", "path": "final.md"}
                        ],
                        "publish": ["final.md"],
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requires a context dependency"):
        TaskPoolService(store).preview_preset("bundle", {})
