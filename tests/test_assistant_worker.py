from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codex_orch.assistant import (
    AssistantBackendResult,
    AssistantRoleRouter,
    AssistantWorkerService,
)
from codex_orch.domain import (
    AssistantUpdateKind,
    AssistantUpdateProposal,
    AssistantUpdateStatus,
    AssistantUpdateTarget,
    AssistantUpdateContentMode,
    RoutingPolicySection,
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


def test_assistant_worker_validates_structured_payload_against_reply_schema(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store)
    schema_dir = store.paths.root / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "reply.json").write_text(
        """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["decision"],
  "additionalProperties": false,
  "properties": {
    "decision": {
      "type": "string",
      "enum": ["delete", "keep_wrapper"]
    }
  }
}
""",
        encoding="utf-8",
    )
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
    recommendation, resolution = AssistantRoleRouter(store).resolve_assistant_target(
        run_id=run.id,
        task_id="worker",
        request_kind=RequestKind.CLARIFICATION,
        decision_kind=DecisionKind.POLICY,
        requested_target_role_id=None,
    )
    interrupt = store.create_interrupt(
        run_id=run.id,
        instance_id=instance.instance_id,
        audience=InterruptAudience.ASSISTANT,
        blocking=True,
        request_kind=RequestKind.CLARIFICATION,
        question="Can I delete the wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["delete", "keep_wrapper"],
        context_artifacts=[],
        reply_schema="schemas/reply.json",
        priority=RequestPriority.NORMAL,
        requested_target_role_id=resolution.requested_target_role_id,
        recommended_target_role_id=recommendation.recommended_target_role_id,
        resolved_target_role_id=resolution.resolved_target_role_id,
        target_resolution_reason=resolution.target_resolution_reason,
        metadata={},
    )

    worker = AssistantWorkerService(
        store,
        backend=StubBackend(
            AssistantBackendResult(
                resolution_kind=ResolutionKind.AUTO_REPLY,
                answer="Delete it.",
                rationale="The wrapper is stale.",
                payload={"decision": "delete"},
            )
        ),
        run_service=RunService(store, FakeRunner()),
    )

    stats = worker.run_once()
    record = store.find_interrupt(interrupt.interrupt_id)

    assert stats.auto_replied == 1
    assert record.reply is not None
    assert record.reply.payload == {"decision": "delete"}


def test_assistant_worker_resolves_relative_refs_in_reply_schema(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store)
    schema_dir = store.paths.root / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "defs.json").write_text(
        """{
  "$defs": {
    "decision": {
      "type": "string",
      "enum": ["delete", "keep_wrapper"]
    }
  }
}
""",
        encoding="utf-8",
    )
    (schema_dir / "reply.json").write_text(
        """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["decision"],
  "additionalProperties": false,
  "properties": {
    "decision": {
      "$ref": "defs.json#/$defs/decision"
    }
  }
}
""",
        encoding="utf-8",
    )
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
    recommendation, resolution = AssistantRoleRouter(store).resolve_assistant_target(
        run_id=run.id,
        task_id="worker",
        request_kind=RequestKind.CLARIFICATION,
        decision_kind=DecisionKind.POLICY,
        requested_target_role_id=None,
    )
    interrupt = store.create_interrupt(
        run_id=run.id,
        instance_id=instance.instance_id,
        audience=InterruptAudience.ASSISTANT,
        blocking=True,
        request_kind=RequestKind.CLARIFICATION,
        question="Can I delete the wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["delete", "keep_wrapper"],
        context_artifacts=[],
        reply_schema="schemas/reply.json",
        priority=RequestPriority.NORMAL,
        requested_target_role_id=resolution.requested_target_role_id,
        recommended_target_role_id=recommendation.recommended_target_role_id,
        resolved_target_role_id=resolution.resolved_target_role_id,
        target_resolution_reason=resolution.target_resolution_reason,
        metadata={},
    )

    worker = AssistantWorkerService(
        store,
        backend=StubBackend(
            AssistantBackendResult(
                resolution_kind=ResolutionKind.AUTO_REPLY,
                answer="Delete it.",
                rationale="The wrapper is stale.",
                payload={"decision": "delete"},
            )
        ),
        run_service=RunService(store, FakeRunner()),
    )

    stats = worker.run_once()
    record = store.find_interrupt(interrupt.interrupt_id)

    assert stats.auto_replied == 1
    assert record.reply is not None
    assert record.reply.payload == {"decision": "delete"}


def test_save_interrupt_reply_reports_relative_ref_schema_mismatch_as_value_error(
    tmp_path: Path,
) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store)
    schema_dir = store.paths.root / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "defs.json").write_text(
        """{
  "$defs": {
    "decision": {
      "type": "string",
      "enum": ["delete", "keep_wrapper"]
    }
  }
}
""",
        encoding="utf-8",
    )
    (schema_dir / "reply.json").write_text(
        """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["decision"],
  "additionalProperties": false,
  "properties": {
    "decision": {
      "$ref": "defs.json#/$defs/decision"
    }
  }
}
""",
        encoding="utf-8",
    )
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
    recommendation, resolution = AssistantRoleRouter(store).resolve_assistant_target(
        run_id=run.id,
        task_id="worker",
        request_kind=RequestKind.CLARIFICATION,
        decision_kind=DecisionKind.POLICY,
        requested_target_role_id=None,
    )
    interrupt = store.create_interrupt(
        run_id=run.id,
        instance_id=instance.instance_id,
        audience=InterruptAudience.ASSISTANT,
        blocking=True,
        request_kind=RequestKind.CLARIFICATION,
        question="Can I delete the wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["delete", "keep_wrapper"],
        context_artifacts=[],
        reply_schema="schemas/reply.json",
        priority=RequestPriority.NORMAL,
        requested_target_role_id=resolution.requested_target_role_id,
        recommended_target_role_id=recommendation.recommended_target_role_id,
        resolved_target_role_id=resolution.resolved_target_role_id,
        target_resolution_reason=resolution.target_resolution_reason,
        metadata={},
    )

    with pytest.raises(ValueError, match="does not match schema"):
        store.save_interrupt_reply(
            interrupt.interrupt_id,
            audience=InterruptAudience.ASSISTANT,
            reply_kind=InterruptReplyKind.ANSWER,
            text="Archive it.",
            payload={"decision": "archive"},
        )


def test_assistant_worker_fails_when_structured_payload_violates_reply_schema(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store)
    schema_dir = store.paths.root / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "reply.json").write_text(
        """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["decision"],
  "additionalProperties": false,
  "properties": {
    "decision": {
      "type": "string",
      "enum": ["delete", "keep_wrapper"]
    }
  }
}
""",
        encoding="utf-8",
    )
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
    recommendation, resolution = AssistantRoleRouter(store).resolve_assistant_target(
        run_id=run.id,
        task_id="worker",
        request_kind=RequestKind.CLARIFICATION,
        decision_kind=DecisionKind.POLICY,
        requested_target_role_id=None,
    )
    interrupt = store.create_interrupt(
        run_id=run.id,
        instance_id=instance.instance_id,
        audience=InterruptAudience.ASSISTANT,
        blocking=True,
        request_kind=RequestKind.CLARIFICATION,
        question="Can I delete the wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["delete", "keep_wrapper"],
        context_artifacts=[],
        reply_schema="schemas/reply.json",
        priority=RequestPriority.NORMAL,
        requested_target_role_id=resolution.requested_target_role_id,
        recommended_target_role_id=recommendation.recommended_target_role_id,
        resolved_target_role_id=resolution.resolved_target_role_id,
        target_resolution_reason=resolution.target_resolution_reason,
        metadata={},
    )

    worker = AssistantWorkerService(
        store,
        backend=StubBackend(
            AssistantBackendResult(
                resolution_kind=ResolutionKind.AUTO_REPLY,
                answer="Delete it.",
                rationale="The wrapper is stale.",
                payload={},
            )
        ),
        run_service=RunService(store, FakeRunner()),
    )

    stats = worker.run_once()
    record = store.find_interrupt(interrupt.interrupt_id)

    assert stats.failed == 1
    assert record.reply is None


def test_assistant_worker_records_valid_proposals(tmp_path: Path) -> None:
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
    _create_assistant_interrupt(
        store,
        run_id=run.id,
        instance_id=instance.instance_id,
        task_id="worker",
    )

    backend = StubBackend(
        AssistantBackendResult(
            resolution_kind=ResolutionKind.AUTO_REPLY,
            answer="Use explicit names.",
            rationale="That matches the role guidance.",
            proposed_updates=(
                AssistantUpdateProposal(
                    kind=AssistantUpdateKind.MANAGED_ASSET_UPDATE,
                    summary="Add naming preference",
                    rationale="The user consistently prefers explicit names.",
                    suggested_content_mode=AssistantUpdateContentMode.SNIPPET,
                    suggested_content="preferences:\n  naming_style: explicit\n",
                    target=AssistantUpdateTarget(
                        role_id="policy",
                        managed_asset_path="preferences.yaml",
                    ),
                ),
                AssistantUpdateProposal(
                    kind=AssistantUpdateKind.ROUTING_POLICY_UPDATE,
                    summary="Prefer policy role",
                    rationale="This task frequently asks policy questions.",
                    suggested_content_mode=AssistantUpdateContentMode.SNIPPET,
                    suggested_content="preferred_roles:\n  - policy\n",
                    target=AssistantUpdateTarget(
                        task_id="worker",
                        routing_section=RoutingPolicySection.ASSISTANT_HINTS,
                    ),
                ),
            ),
        )
    )
    worker = AssistantWorkerService(
        store,
        backend=backend,
        run_service=RunService(store, FakeRunner()),
    )

    stats = worker.run_once()
    proposals = store.list_proposals(run_id=run.id)
    events = store.list_events(run.id)

    assert stats.auto_replied == 1
    assert len(proposals) == 2
    assert proposals[0].status is AssistantUpdateStatus.PROPOSED
    assert proposals[0].proposal.kind is AssistantUpdateKind.MANAGED_ASSET_UPDATE
    assert proposals[1].proposal.kind is AssistantUpdateKind.ROUTING_POLICY_UPDATE
    assert any(event.event_type == "proposal_recorded" for event in events)


def test_assistant_worker_drops_invalid_proposals_without_blocking_answer(tmp_path: Path) -> None:
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
            resolution_kind=ResolutionKind.AUTO_REPLY,
            answer="Keep the wrapper.",
            rationale="Compatibility still matters.",
            proposed_updates=(
                AssistantUpdateProposal(
                    kind=AssistantUpdateKind.MANAGED_ASSET_UPDATE,
                    summary="Write undeclared asset",
                    rationale="Testing invalid proposal handling.",
                    suggested_content_mode=AssistantUpdateContentMode.SNIPPET,
                    suggested_content="guidance:\n  - keep wrappers\n",
                    target=AssistantUpdateTarget(
                        role_id="policy",
                        managed_asset_path="not-declared.yaml",
                    ),
                ),
            ),
        )
    )
    worker = AssistantWorkerService(
        store,
        backend=backend,
        run_service=RunService(store, FakeRunner()),
    )

    stats = worker.run_once()
    proposals = store.list_proposals(run_id=run.id)
    record = store.find_interrupt(interrupt.interrupt_id)
    events = store.list_events(run.id)

    assert stats.auto_replied == 1
    assert proposals == []
    assert record.reply is not None
    assert any(event.event_type == "proposal_dropped" for event in events)


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


def test_assistant_worker_fails_when_shared_operating_model_is_missing(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store)
    store.get_assistant_operating_model_path().unlink()
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
    _create_assistant_interrupt(
        store,
        run_id=run.id,
        instance_id=instance.instance_id,
        task_id="worker",
    )

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


def test_assistant_worker_records_program_asset_update_proposal(tmp_path: Path) -> None:
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
    _create_assistant_interrupt(
        store,
        run_id=run.id,
        instance_id=instance.instance_id,
        task_id="worker",
    )

    backend = StubBackend(
        AssistantBackendResult(
            resolution_kind=ResolutionKind.AUTO_REPLY,
            answer="Added new finding.",
            rationale="Scan detected a new issue.",
            proposed_updates=(
                AssistantUpdateProposal(
                    kind=AssistantUpdateKind.PROGRAM_ASSET_UPDATE,
                    summary="Add newly discovered finding",
                    rationale="Scan found a new maintenance issue.",
                    suggested_content_mode=AssistantUpdateContentMode.SNIPPET,
                    suggested_content="findings:\n  - id: F-042\n    severity: medium\n",
                    target=AssistantUpdateTarget(
                        managed_asset_path="inputs/known_findings.yaml",
                    ),
                ),
            ),
        )
    )
    worker = AssistantWorkerService(
        store,
        backend=backend,
        run_service=RunService(store, FakeRunner()),
    )

    stats = worker.run_once()
    proposals = store.list_proposals(run_id=run.id)
    events = store.list_events(run.id)

    assert stats.auto_replied == 1
    assert len(proposals) == 1
    assert proposals[0].status is AssistantUpdateStatus.PROPOSED
    assert proposals[0].proposal.kind is AssistantUpdateKind.PROGRAM_ASSET_UPDATE
    assert str(proposals[0].target_file_path).endswith("inputs/known_findings.yaml")
    assert any(event.event_type == "proposal_recorded" for event in events)
