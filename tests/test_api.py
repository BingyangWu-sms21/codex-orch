from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from codex_orch.api import create_app
from codex_orch.domain import (
    DecisionKind,
    RequestKind,
    RequestPriority,
    TaskSpec,
    TaskStatus,
)
from codex_orch.scheduler import RunService
from codex_orch.store import ProjectStore
from tests.helpers import build_test_store, write_assistant_profile
from tests.test_run_service import FakeRunner


def test_tasks_page_and_create_task(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    app = create_app(store.paths.root, global_root=store.global_paths.root)
    client = TestClient(app)

    response = client.get("/tasks")
    assert response.status_code == 200
    assert "Create task" in response.text

    create_response = client.post(
        "/tasks",
        data={
            "id": "task-one",
            "title": "Task One",
            "agent": "default",
            "status": "ready",
            "description": "desc",
            "labels": "alpha,beta",
            "publish": "final.md",
            "model": "",
            "sandbox": "read-only",
            "workspace": "fresh-clone",
            "extra_writable_roots": "tool-cache\n../env-source",
            "result_schema": "",
            "compose": "- kind: literal\n  text: hello\n",
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303
    assert create_response.headers["location"] == "/tasks/task-one"

    task = ProjectStore(store.paths.root, global_root=store.global_paths.root).get_task("task-one")
    assert task.title == "Task One"
    assert task.workspace == "fresh-clone"
    assert task.extra_writable_roots == ["tool-cache", "../env-source"]


def test_task_detail_updates_workspace_fields(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="task-one",
            title="Task One",
            agent="default",
            status=TaskStatus.READY,
            publish=["final.md"],
        )
    )
    app = create_app(store.paths.root, global_root=store.global_paths.root)
    client = TestClient(app)

    update_response = client.post(
        "/tasks/task-one",
        data={
            "id": "task-one",
            "title": "Task One Updated",
            "agent": "default",
            "status": "ready",
            "description": "desc",
            "labels": "alpha,beta",
            "publish": "final.md",
            "model": "",
            "sandbox": "workspace-write",
            "workspace": "apps/challenge-hub",
            "extra_writable_roots": "tool-cache\n../shared-artifacts",
            "result_schema": "",
            "compose": "- kind: literal\n  text: hello\n",
        },
        follow_redirects=False,
    )

    assert update_response.status_code == 303
    task = ProjectStore(store.paths.root, global_root=store.global_paths.root).get_task("task-one")
    assert task.title == "Task One Updated"
    assert task.workspace == "apps/challenge-hub"
    assert task.extra_writable_roots == ["tool-cache", "../shared-artifacts"]


def test_graph_page_renders_existing_edges(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(id="a", title="A", agent="default", status=TaskStatus.READY, publish=["final.md"])
    )
    store.save_task(
        TaskSpec(
            id="b",
            title="B",
            agent="default",
            status=TaskStatus.READY,
            depends_on=[{"task": "a", "kind": "context", "consume": ["final.md"]}],
            publish=["final.md"],
        )
    )
    app = create_app(store.paths.root, global_root=store.global_paths.root)
    client = TestClient(app)

    response = client.get("/graph")
    assert response.status_code == 200
    assert "Dependency graph" in response.text
    assert '"from": "a"' in response.text


def test_assistant_page_and_response_form(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_profile(store, set_as_default=True)
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            publish=["final.md"],
        )
    )
    snapshot = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )
    request = store.create_assistant_request(
        run_id=snapshot.id,
        task_id="worker",
        request_kind=RequestKind.CLARIFICATION,
        question="Should I remove the wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["delete", "keep_wrapper"],
        context_artifacts=[],
        requested_control_actions=[],
        priority=RequestPriority.HIGH,
    )
    app = create_app(store.paths.root, global_root=store.global_paths.root)
    client = TestClient(app)

    response = client.get("/assistant")
    assert response.status_code == 200
    assert request.request_id in response.text

    save_response = client.post(
        "/assistant/respond",
        data={
            "request_id": request.request_id,
            "resolution_kind": "auto_reply",
            "answer": "Delete it.",
            "rationale": "The project does not require compatibility wrappers.",
            "confidence": "high",
            "citations": "~/.codex/AGENTS.md",
            "guidance_updates": "",
            "proposed_actions": "",
        },
        follow_redirects=False,
    )
    assert save_response.status_code == 303

    saved = ProjectStore(store.paths.root, global_root=store.global_paths.root)
    record = saved.find_assistant_request(request.request_id)
    assert record.response is not None
    assert record.response.answer == "Delete it."
