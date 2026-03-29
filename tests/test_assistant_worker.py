from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_orch import cli as cli_module
from codex_orch.assistant import AssistantBackendRequest, AssistantBackendResult, AssistantWorkerService
from codex_orch.cli import app
from codex_orch.domain import (
    AssistantRequest,
    ConfidenceLevel,
    DecisionKind,
    ManualGateStatus,
    RequestKind,
    RequestPriority,
    ResolutionKind,
    RunStatus,
    TaskSpec,
    TaskStatus,
)
from codex_orch.scheduler import RunService
from tests.helpers import build_test_store, write_assistant_profile
from tests.test_run_service import FakeRunner


class RecordingAssistantBackend:
    def __init__(self, result: AssistantBackendResult) -> None:
        self.result = result
        self.requests: list[AssistantBackendRequest] = []

    def respond(self, request: AssistantBackendRequest) -> AssistantBackendResult:
        self.requests.append(request)
        return self.result


def test_create_assistant_request_requires_effective_profile(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    snapshot = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )

    with pytest.raises(ValueError, match="has no effective assistant profile"):
        store.create_assistant_request(
            run_id=snapshot.id,
            task_id="worker",
            request_kind=RequestKind.CLARIFICATION,
            question="Should I keep the wrapper?",
            decision_kind=DecisionKind.POLICY,
            options=["keep", "delete"],
            context_artifacts=[],
            requested_control_actions=[],
            priority=RequestPriority.NORMAL,
        )


def test_create_assistant_request_requires_existing_context_artifact(
    tmp_path: Path,
) -> None:
    store = build_test_store(tmp_path)
    write_assistant_profile(store, set_as_default=True)
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    snapshot = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )

    with pytest.raises(ValueError, match="context artifact missing.md does not exist"):
        store.create_assistant_request(
            run_id=snapshot.id,
            task_id="worker",
            request_kind=RequestKind.CLARIFICATION,
            question="Should I keep the wrapper?",
            decision_kind=DecisionKind.POLICY,
            options=["keep", "delete"],
            context_artifacts=["missing.md"],
            requested_control_actions=[],
            priority=RequestPriority.NORMAL,
        )


def test_create_assistant_request_requires_loadable_profile(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_profile(store, set_as_default=True)
    store.get_profile_instructions_path("assistant-default").unlink()
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    snapshot = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )

    with pytest.raises(KeyError, match="missing instructions.md"):
        store.create_assistant_request(
            run_id=snapshot.id,
            task_id="worker",
            request_kind=RequestKind.CLARIFICATION,
            question="Should I keep the wrapper?",
            decision_kind=DecisionKind.POLICY,
            options=["keep", "delete"],
            context_artifacts=[],
            requested_control_actions=[],
            priority=RequestPriority.NORMAL,
        )


def test_assistant_worker_skips_request_without_effective_profile(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    run_service = RunService(store, FakeRunner())
    snapshot = run_service.create_snapshot(roots=["worker"], labels=[], user_inputs=None)
    request = AssistantRequest(
        request_id="req_legacy",
        run_id=snapshot.id,
        requester_task_id="worker",
        request_kind=RequestKind.CLARIFICATION,
        question="Should I keep the wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["keep", "delete"],
        context_artifacts=[],
        requested_control_actions=[],
        priority=RequestPriority.NORMAL,
    )
    store.save_assistant_request(snapshot.id, "worker", request)
    asyncio.run(run_service.run_snapshot(snapshot.id))

    backend = RecordingAssistantBackend(
        AssistantBackendResult(
            resolution_kind=ResolutionKind.AUTO_REPLY,
            answer="Delete it.",
            rationale="No compatibility consumers remain.",
        )
    )
    worker = AssistantWorkerService(store, backend=backend, run_service=run_service)

    stats = worker.run_once()

    assert stats.scanned == 1
    assert stats.skipped_no_profile == 1
    assert stats.processed == 0
    assert backend.requests == []
    assert store.find_assistant_request(request.request_id).response is None


def test_assistant_worker_prefers_task_profile_over_project_default(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_profile(store, "project-default")
    write_assistant_profile(store, "task-override")
    project = store.load_project()
    store.save_project(
        project.model_copy(update={"default_assistant_profile": "project-default"})
    )
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            assistant_profile="task-override",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )
    artifact_path = store.paths.root / "context" / "policy.md"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("Prefer deleting wrappers.\n", encoding="utf-8")

    run_service = RunService(store, FakeRunner())
    snapshot = run_service.create_snapshot(roots=["worker"], labels=[], user_inputs=None)
    store.create_assistant_request(
        run_id=snapshot.id,
        task_id="worker",
        request_kind=RequestKind.CLARIFICATION,
        question="Should I delete the wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["keep", "delete"],
        context_artifacts=["context/policy.md"],
        requested_control_actions=[],
        priority=RequestPriority.HIGH,
    )
    asyncio.run(run_service.run_snapshot(snapshot.id))

    backend = RecordingAssistantBackend(
        AssistantBackendResult(
            resolution_kind=ResolutionKind.HANDOFF_TO_HUMAN,
            answer="Escalate to human.",
            rationale="This change affects cross-team API compatibility.",
            confidence=ConfidenceLevel.HIGH,
        )
    )
    worker = AssistantWorkerService(store, backend=backend, run_service=run_service)

    stats = worker.run_once()

    assert stats.processed == 1
    assert stats.handed_off == 1
    assert len(backend.requests) == 1
    backend_request = backend.requests[0]
    assert backend_request.profile.spec.id == "task-override"
    assert backend_request.task.assistant_profile == "task-override"
    assert backend_request.artifacts[0].relative_path == "context/policy.md"
    assert backend_request.artifacts[0].content == "Prefer deleting wrappers.\n"


def test_assistant_worker_requires_registered_backend_for_profile(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_profile(store, set_as_default=True)
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    run_service = RunService(store, FakeRunner())
    snapshot = run_service.create_snapshot(roots=["worker"], labels=[], user_inputs=None)
    store.create_assistant_request(
        run_id=snapshot.id,
        task_id="worker",
        request_kind=RequestKind.CLARIFICATION,
        question="Should I delete the wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["keep", "delete"],
        context_artifacts=[],
        requested_control_actions=[],
        priority=RequestPriority.HIGH,
    )
    asyncio.run(run_service.run_snapshot(snapshot.id))

    worker = AssistantWorkerService(
        store,
        backend_registry={},
        run_service=run_service,
    )

    stats = worker.run_once()

    assert stats.scanned == 1
    assert stats.failed == 1
    assert stats.processed == 0
    assert store.list_assistant_requests(unresolved_only=True)


def test_assistant_worker_handoff_materializes_manual_gate(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_profile(store, "assistant-default")
    project = store.load_project()
    store.save_project(
        project.model_copy(update={"default_assistant_profile": "assistant-default"})
    )
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    run_service = RunService(store, FakeRunner())
    snapshot = run_service.create_snapshot(roots=["worker"], labels=[], user_inputs=None)
    request = store.create_assistant_request(
        run_id=snapshot.id,
        task_id="worker",
        request_kind=RequestKind.CLARIFICATION,
        question="Should I delete the wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["keep", "delete"],
        context_artifacts=[],
        requested_control_actions=[],
        priority=RequestPriority.HIGH,
    )
    asyncio.run(run_service.run_snapshot(snapshot.id))

    backend = RecordingAssistantBackend(
        AssistantBackendResult(
            resolution_kind=ResolutionKind.HANDOFF_TO_HUMAN,
            answer="I need a human decision.",
            rationale="This is a user preference boundary.",
        )
    )
    worker = AssistantWorkerService(store, backend=backend, run_service=run_service)

    stats = worker.run_once()

    gate_record = store.find_manual_gate_by_request_id(request.request_id)
    updated_snapshot = store.get_run(snapshot.id)
    assert stats.processed == 1
    assert stats.handed_off == 1
    assert gate_record.gate.status is ManualGateStatus.WAITING_FOR_HUMAN
    assert gate_record.human_request is not None
    assert updated_snapshot.status is RunStatus.WAITING


def test_assistant_worker_cli_once_auto_replies_and_resumes_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = build_test_store(tmp_path)
    write_assistant_profile(store, "assistant-default")
    project = store.load_project()
    store.save_project(
        project.model_copy(update={"default_assistant_profile": "assistant-default"})
    )
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    runner_backend = RecordingAssistantBackend(
        AssistantBackendResult(
            resolution_kind=ResolutionKind.AUTO_REPLY,
            answer="Delete it.",
            rationale="No compatibility layer is needed.",
            confidence=ConfidenceLevel.HIGH,
        )
    )
    fake_runner = FakeRunner()
    run_service = RunService(store, fake_runner)
    snapshot = run_service.create_snapshot(roots=["worker"], labels=[], user_inputs=None)
    request = store.create_assistant_request(
        run_id=snapshot.id,
        task_id="worker",
        request_kind=RequestKind.CLARIFICATION,
        question="Should I delete the wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["keep", "delete"],
        context_artifacts=[],
        requested_control_actions=[],
        priority=RequestPriority.HIGH,
    )
    asyncio.run(run_service.run_snapshot(snapshot.id))

    worker = AssistantWorkerService(
        store,
        backend=runner_backend,
        run_service=run_service,
    )
    monkeypatch.setattr(
        cli_module,
        "_assistant_worker_service",
        lambda program_dir: worker,
    )

    result = CliRunner().invoke(
        app,
        [
            "assistant",
            "worker",
            str(store.paths.root),
            "--once",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "auto_replied": 1,
        "failed": 0,
        "handed_off": 0,
        "processed": 1,
        "scanned": 1,
        "skipped_no_profile": 0,
    }
    assert store.find_assistant_request(request.request_id).response is not None
    assert store.get_run(snapshot.id).status is RunStatus.DONE
    assert "worker" in fake_runner.requests
