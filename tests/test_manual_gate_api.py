from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from codex_orch.api import create_app
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
from codex_orch.store import ProjectStore
from tests.helpers import build_test_store, write_assistant_profile
from tests.test_run_service import FakeRunner


def test_manual_gate_pages_and_forms(tmp_path: Path) -> None:
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
    store.save_assistant_response_by_request_id(
        request.request_id,
        resolution_kind=ResolutionKind.HANDOFF_TO_HUMAN,
        answer="I need a human decision.",
        rationale="The compatibility boundary is ambiguous.",
        confidence=ConfidenceLevel.MEDIUM,
        citations=["~/.codex/AGENTS.md"],
        proposed_guidance_updates=[],
        proposed_control_actions=[],
    )
    gate_id = store.find_manual_gate_by_request_id(request.request_id).gate.gate_id

    app = create_app(
        store.paths.root,
        global_root=store.global_paths.root,
        runner=FakeRunner(),
    )
    client = TestClient(app)

    page = client.get("/manual-gates")
    assert page.status_code == 200
    assert gate_id in page.text

    detail = client.get(f"/manual-gates/{gate_id}")
    assert detail.status_code == 200
    assert "Human request" in detail.text

    respond = client.post(
        f"/manual-gates/{gate_id}/respond",
        data={"answer": "Delete it."},
        follow_redirects=False,
    )
    assert respond.status_code == 303

    approve = client.post(
        f"/manual-gates/{gate_id}/approve",
        follow_redirects=False,
    )
    assert approve.status_code == 303

    saved = ProjectStore(store.paths.root, global_root=store.global_paths.root)
    record = saved.find_manual_gate_by_request_id(request.request_id)
    assert record.human_response is not None
    assert record.human_response.answer == "Delete it."
    assert record.gate.status in {
        ManualGateStatus.APPROVED,
        ManualGateStatus.APPLIED,
    }
