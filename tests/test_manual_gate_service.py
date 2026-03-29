from __future__ import annotations

from pathlib import Path

from codex_orch.domain import (
    ConfidenceLevel,
    DecisionKind,
    ManualGateStatus,
    RequestKind,
    RequestPriority,
    ResolutionKind,
    TaskSpec,
    TaskStatus,
)
from codex_orch.scheduler import RunService
from tests.helpers import build_test_store, write_assistant_profile
from tests.test_run_service import FakeRunner


def test_handoff_response_materializes_manual_gate_artifacts(tmp_path: Path) -> None:
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
        question="Should I delete the wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["delete", "keep"],
        context_artifacts=[],
        requested_control_actions=[],
        priority=RequestPriority.HIGH,
    )

    response = store.save_assistant_response_by_request_id(
        request.request_id,
        resolution_kind=ResolutionKind.HANDOFF_TO_HUMAN,
        answer="I need a human decision.",
        rationale="The compatibility boundary is ambiguous.",
        confidence=ConfidenceLevel.MEDIUM,
        citations=["~/.codex/AGENTS.md"],
        proposed_guidance_updates=[],
        proposed_control_actions=[],
    )

    record = store.find_manual_gate_by_request_id(request.request_id)
    assert record.gate.status is ManualGateStatus.WAITING_FOR_HUMAN
    assert record.human_request is not None
    assert record.human_request.assistant_summary == response.answer
    assert record.human_request.assistant_rationale == response.rationale
    assert record.human_response is None
