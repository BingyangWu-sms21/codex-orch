from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from codex_orch.api import create_app
from codex_orch.domain import (
    NodeExecutionFailureKind,
    NodeExecutionTerminationReason,
    TaskSpec,
    TaskStatus,
)
from codex_orch.runner import NodeExecutionRequest, NodeExecutionResult
from codex_orch.scheduler import RunService
from codex_orch.store import ProjectStore
from tests.helpers import build_test_store
from tests.test_run_service import FakeRunner


def test_tasks_page_and_create_task(tmp_path) -> None:
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


def test_task_detail_updates_workspace_fields(tmp_path) -> None:
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


def test_runs_page_and_run_detail_render_instance_runtime(tmp_path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            publish=["final.md"],
        )
    )
    run = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )
    app = create_app(store.paths.root, global_root=store.global_paths.root)
    client = TestClient(app)

    response = client.get("/runs")
    assert response.status_code == 200
    assert run.id in response.text

    detail = client.get(f"/runs/{run.id}")
    assert detail.status_code == 200
    assert "Instances" in detail.text


def test_tasks_page_renders_warning_flash(tmp_path) -> None:
    store = build_test_store(tmp_path)
    app = create_app(store.paths.root, global_root=store.global_paths.root)
    client = TestClient(app)

    response = client.get("/tasks?warning=be-careful")

    assert response.status_code == 200
    assert "be-careful" in response.text


def test_run_detail_renders_failure_metadata(tmp_path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            publish=["final.md"],
        )
    )

    class RecoverableFailureRunner:
        async def run(self, request: NodeExecutionRequest) -> NodeExecutionResult:
            return NodeExecutionResult(
                success=False,
                return_code=1,
                final_message="",
                error="stream disconnected before completion",
                termination_reason=NodeExecutionTerminationReason.NONZERO_EXIT,
                failure_kind=NodeExecutionFailureKind.EXTERNAL_PROTOCOL,
                failure_summary="stream disconnected before completion",
                resume_recommended=True,
            )

    run = asyncio.run(
        RunService(store, RecoverableFailureRunner()).start_run(
            roots=["worker"],
            labels=[],
            user_inputs=None,
        )
    )
    app = create_app(store.paths.root, global_root=store.global_paths.root)
    client = TestClient(app)

    detail = client.get(f"/runs/{run.id}")

    assert detail.status_code == 200
    assert "external_protocol" in detail.text
    assert "stream disconnected before completion" in detail.text
    assert "resume recommended" in detail.text
