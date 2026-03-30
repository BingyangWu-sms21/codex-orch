from __future__ import annotations

import asyncio
from pathlib import Path

from codex_orch.assistant import (
    AssistantBackendResult,
    AssistantRoleRouter,
    AssistantWorkerService,
)
from codex_orch.domain import (
    ConfidenceLevel,
    InterruptAudience,
    InterruptReplyKind,
    RequestKind,
    RequestPriority,
    ResolutionKind,
    RunInstanceStatus,
    RunStatus,
    TaskSpec,
    TaskStatus,
    DecisionKind,
)
from codex_orch.scheduler import RunService
from tests.helpers import build_test_store, write_assistant_role
from tests.test_run_service import FakeRunner


class StubBackend:
    def __init__(self, result: AssistantBackendResult) -> None:
        self.result = result
        self.requests = []

    def respond(self, request):
        self.requests.append(request)
        return self.result


def _instance_for_task(run, task_id: str):
    matches = [instance for instance in run.instances.values() if instance.task_id == task_id]
    assert len(matches) == 1
    return matches[0]


def _create_assistant_interrupt(store, *, run_id: str, instance_id: str, task_id: str):
    recommendation, resolution = AssistantRoleRouter(store).resolve_assistant_target(
        run_id=run_id,
        task_id=task_id,
        request_kind=RequestKind.CLARIFICATION,
        decision_kind=DecisionKind.POLICY,
        requested_target_role_id=None,
    )
    return store.create_interrupt(
        run_id=run_id,
        instance_id=instance_id,
        audience=InterruptAudience.ASSISTANT,
        blocking=True,
        request_kind=RequestKind.CLARIFICATION,
        question="Should I keep the wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["keep", "delete"],
        context_artifacts=[],
        reply_schema=None,
        priority=RequestPriority.NORMAL,
        requested_target_role_id=resolution.requested_target_role_id,
        recommended_target_role_id=recommendation.recommended_target_role_id,
        resolved_target_role_id=resolution.resolved_target_role_id,
        target_resolution_reason=resolution.target_resolution_reason,
        metadata={},
    )


def test_assistant_worker_fails_when_resolved_role_is_missing(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store)
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
    instance = _instance_for_task(run, "worker")
    interrupt = _create_assistant_interrupt(
        store,
        run_id=run.id,
        instance_id=instance.instance_id,
        task_id="worker",
    )
    store.get_assistant_role_spec_path(interrupt.resolved_target_role_id or "policy").unlink()

    worker = AssistantWorkerService(
        store,
        backend=StubBackend(
            AssistantBackendResult(
                resolution_kind=ResolutionKind.AUTO_REPLY,
                answer="Delete it.",
                rationale="No wrapper is needed.",
            )
        ),
        run_service=RunService(store, FakeRunner()),
    )

    stats = worker.run_once()
    assert stats.failed == 1


def test_assistant_worker_auto_reply_resolves_interrupt_and_resumes_run(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store)
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

    runner = FakeRunner(store)
    run_service = RunService(store, runner)
    run = asyncio.run(run_service.start_run(roots=["worker"], labels=[], user_inputs=None))
    instance = _instance_for_task(run, "worker")
    assert run.status is RunStatus.WAITING

    backend = StubBackend(
        AssistantBackendResult(
            resolution_kind=ResolutionKind.AUTO_REPLY,
            answer="Delete it.",
            rationale="No wrapper is needed.",
            confidence=ConfidenceLevel.HIGH,
            citations=("~/.codex/AGENTS.md",),
        )
    )
    worker = AssistantWorkerService(store, backend=backend, run_service=run_service)

    stats = worker.run_once()
    updated = store.get_run(run.id)
    updated_instance = _instance_for_task(updated, "worker")
    records = store.list_instance_interrupts(run.id, instance.instance_id)

    assert stats.auto_replied == 1
    assert updated.status is RunStatus.DONE
    assert updated_instance.status is RunInstanceStatus.DONE
    assert updated_instance.attempt == 2
    assert records[0].reply is not None
    assert records[0].reply.reply_kind is InterruptReplyKind.ANSWER
    assert "Delete it." in runner.prompts["worker"]
    assert backend.requests[0].instance_id == instance.instance_id


def test_assistant_worker_handoff_creates_human_interrupt(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store)
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
    instance = _instance_for_task(run, "worker")
    interrupt = _create_assistant_interrupt(
        store,
        run_id=run.id,
        instance_id=instance.instance_id,
        task_id="worker",
    )

    backend = StubBackend(
        AssistantBackendResult(
            resolution_kind=ResolutionKind.HANDOFF_TO_HUMAN,
            answer="I need a human decision.",
            rationale="This is a product decision.",
        )
    )
    worker = AssistantWorkerService(
        store,
        backend=backend,
        run_service=RunService(store, FakeRunner()),
    )

    stats = worker.run_once()
    records = store.list_instance_interrupts(run.id, instance.instance_id)
    human_records = [record for record in records if record.interrupt.audience is InterruptAudience.HUMAN]
    original = store.find_interrupt(interrupt.interrupt_id)

    assert stats.handed_off == 1
    assert original.reply is not None
    assert original.reply.reply_kind is InterruptReplyKind.HANDOFF_TO_HUMAN
    assert len(human_records) == 1
    assert human_records[0].interrupt.metadata["assistant_summary"] == "I need a human decision."
    assert human_records[0].interrupt.resolved_target_role_id is None
