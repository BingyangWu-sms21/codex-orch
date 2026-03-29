from __future__ import annotations

import asyncio
from pathlib import Path

from typer.testing import CliRunner

from codex_orch.cli import app
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


def test_manual_gate_cli_round_trip(tmp_path: Path) -> None:
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
    runner = CliRunner()

    list_result = runner.invoke(
        app,
        [
            "manual-gate",
            "list",
            str(store.paths.root),
        ],
    )
    assert list_result.exit_code == 0
    assert gate_id in list_result.stdout

    show_result = runner.invoke(
        app,
        [
            "manual-gate",
            "show",
            str(store.paths.root),
            gate_id,
            "--json",
        ],
    )
    assert show_result.exit_code == 0
    assert "\"manual_gate\"" in show_result.stdout

    respond_result = runner.invoke(
        app,
        [
            "manual-gate",
            "respond",
            str(store.paths.root),
            gate_id,
            "--answer",
            "Delete it.",
        ],
    )
    assert respond_result.exit_code == 0

    approve_result = runner.invoke(
        app,
        [
            "manual-gate",
            "approve",
            str(store.paths.root),
            gate_id,
            "--json",
        ],
    )
    assert approve_result.exit_code == 0

    record = store.find_manual_gate_by_request_id(request.request_id)
    assert record.human_response is not None
    assert record.human_response.answer == "Delete it."
    assert record.gate.status is ManualGateStatus.APPROVED

    resolved_run = asyncio.run(RunService(store, FakeRunner()).resume_run(snapshot.id))
    assert resolved_run.status.value == "done"

    record = store.find_manual_gate_by_request_id(request.request_id)
    assert record.gate.status is ManualGateStatus.APPLIED
    run = store.get_run(snapshot.id)
    assert run.status.value == "done"
