from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codex_orch.domain import (
    ConfidenceLevel,
    DecisionKind,
    ManualGateStatus,
    NodeExecutionRuntime,
    NodeExecutionTerminationReason,
    RequestKind,
    RunNodeStatus,
    RunStatus,
    RequestPriority,
    RunNodeWaitReason,
    TaskSpec,
    TaskStatus,
    ResolutionKind,
)
from codex_orch.runner import NodeExecutionRequest, NodeExecutionResult
from codex_orch.scheduler import RunService
from tests.helpers import build_test_store, write_assistant_profile


class FakeRunner:
    def __init__(self) -> None:
        self.prompts: dict[str, str] = {}
        self.requests: dict[str, NodeExecutionRequest] = {}

    async def run(self, request: NodeExecutionRequest) -> NodeExecutionResult:
        self.prompts[request.task.id] = request.prompt
        self.requests[request.task.id] = request
        final_path = request.node_dir / "final.md"
        if request.task.id == "analyze":
            final_path.write_text("analysis result\n", encoding="utf-8")
            return NodeExecutionResult(success=True, return_code=0, final_message="analysis result")
        final_path.write_text(request.prompt, encoding="utf-8")
        return NodeExecutionResult(success=True, return_code=0, final_message=request.prompt)


class RejectingRunner(FakeRunner):
    async def run(self, request: NodeExecutionRequest) -> NodeExecutionResult:
        del request
        raise ValueError("extra writable roots require a writable sandbox")


def test_run_service_materializes_context_dependencies(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="analyze",
            title="Analyze",
            agent="explorer",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/analyze.md"}],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="implement",
            title="Implement",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "analyze", "kind": "context", "consume": ["final.md"]}],
            compose=[
                {"kind": "file", "path": "prompts/implement.md"},
                {"kind": "from_dep", "task": "analyze", "path": "final.md"},
                {"kind": "user_input", "key": "brief"},
            ],
            publish=["final.md"],
        )
    )

    runner = FakeRunner()
    service = RunService(store, runner)
    snapshot = asyncio.run(service.start_run(roots=["implement"], labels=[], user_inputs=None))

    assert snapshot.status.value == "done"
    assert snapshot.prefect_flow_run_id is not None
    snapshot_payload = snapshot.model_dump(mode="json")
    assert "project_name" not in snapshot_payload
    assert "selected_labels" not in snapshot_payload
    assert "prefect_flow_run_name" not in snapshot_payload
    assert snapshot.nodes["analyze"].status.value == "done"
    assert snapshot.nodes["implement"].status.value == "done"
    assert snapshot_payload["nodes"]["analyze"]["published"] == [
        {"relative_path": "final.md"}
    ]
    assert snapshot_payload["nodes"]["implement"]["published"] == [
        {"relative_path": "final.md"}
    ]

    implement_final = (
        store.get_node_dir(snapshot.id, "implement") / "final.md"
    ).read_text(encoding="utf-8")
    assert "## File Prompt: prompts/implement.md" in implement_final
    assert "## Dependency Context: analyze/final.md" in implement_final
    assert "## User Input: brief" in implement_final
    assert "## Execution Contract" in implement_final
    assert "analysis result" in implement_final
    assert "brief input" in implement_final


def test_create_snapshot_rejects_invalid_from_dep_contract(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="analyze",
            title="Analyze",
            agent="explorer",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/analyze.md"}],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="implement",
            title="Implement",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "analyze", "kind": "order", "consume": []}],
            compose=[
                {"kind": "file", "path": "prompts/implement.md"},
                {"kind": "from_dep", "task": "analyze", "path": "final.md"},
            ],
            publish=["final.md"],
        )
    )

    with pytest.raises(ValueError, match="requires a context dependency"):
        RunService(store, FakeRunner()).create_snapshot(
            roots=["implement"],
            labels=[],
            user_inputs=None,
        )


def test_create_snapshot_materializes_workspace_and_writable_roots(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    (store.paths.root / "fresh-clone").mkdir()
    store.save_task(
        TaskSpec(
            id="refactor",
            title="Refactor",
            agent="worker",
            status=TaskStatus.READY,
            sandbox="workspace-write",
            workspace="fresh-clone",
            extra_writable_roots=["tool-cache", "../env-source", "tool-cache"],
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    snapshot = RunService(store, FakeRunner()).create_snapshot(
        roots=["refactor"],
        labels=[],
        user_inputs=None,
    )

    task = snapshot.nodes["refactor"].task
    assert task.workspace == str((store.paths.root / "fresh-clone").resolve())
    assert task.extra_writable_roots == [
        str((store.paths.root / "fresh-clone" / "tool-cache").resolve()),
        str((store.paths.root / "env-source").resolve()),
    ]


def test_run_service_uses_task_workspace_override(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    (store.paths.root / "fresh-clone").mkdir()
    store.save_task(
        TaskSpec(
            id="refactor",
            title="Refactor",
            agent="worker",
            status=TaskStatus.READY,
            sandbox="workspace-write",
            workspace="fresh-clone",
            extra_writable_roots=["tool-cache"],
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    runner = FakeRunner()
    service = RunService(store, runner)

    snapshot = asyncio.run(service.start_run(roots=["refactor"], labels=[], user_inputs=None))

    assert snapshot.status is RunStatus.DONE
    request = runner.requests["refactor"]
    assert request.project_workspace_dir == store.paths.root.resolve()
    assert request.workspace_dir == (store.paths.root / "fresh-clone").resolve()
    assert request.extra_writable_roots == (
        (store.paths.root / "fresh-clone" / "tool-cache").resolve(),
    )


def test_resume_run_keeps_materialized_workspace_after_project_change(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    (store.paths.root / "fresh-clone").mkdir()
    store.save_task(
        TaskSpec(
            id="refactor",
            title="Refactor",
            agent="worker",
            status=TaskStatus.READY,
            workspace="fresh-clone",
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    runner = FakeRunner()
    service = RunService(store, runner)
    snapshot = service.create_snapshot(roots=["refactor"], labels=[], user_inputs=None)
    original_workspace = snapshot.nodes["refactor"].task.workspace
    assert original_workspace is not None
    project = store.load_project()
    store.save_project(project.model_copy(update={"workspace": str(tmp_path / "different-root")}))

    resumed = asyncio.run(service.resume_run(snapshot.id))

    assert resumed.status is RunStatus.DONE
    assert runner.requests["refactor"].workspace_dir == Path(original_workspace)


def test_create_snapshot_rejects_read_only_extra_writable_roots(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="refactor",
            title="Refactor",
            agent="worker",
            status=TaskStatus.READY,
            sandbox="read-only",
            extra_writable_roots=["shared-cache"],
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    with pytest.raises(
        ValueError,
        match="cannot use extra writable roots with read-only sandbox",
    ):
        RunService(store, RejectingRunner()).create_snapshot(
            roots=["refactor"],
            labels=[],
            user_inputs=None,
        )


def test_create_snapshot_requires_existing_workspace_directory(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="refactor",
            title="Refactor",
            agent="worker",
            status=TaskStatus.READY,
            workspace="fresh-clone",
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    with pytest.raises(ValueError, match="workspace does not exist"):
        RunService(store, FakeRunner()).create_snapshot(
            roots=["refactor"],
            labels=[],
            user_inputs=None,
        )


def test_run_service_waits_for_assistant_reply(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_profile(store, set_as_default=True)
    store.save_task(
        TaskSpec(
            id="refactor",
            title="Refactor",
            agent="worker",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    runner = FakeRunner()
    service = RunService(store, runner)
    snapshot = service.create_snapshot(roots=["refactor"], labels=[], user_inputs=None)
    request = store.create_assistant_request(
        run_id=snapshot.id,
        task_id="refactor",
        request_kind=RequestKind.CLARIFICATION,
        question="Can I delete the legacy wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["delete", "keep_wrapper"],
        context_artifacts=[],
        requested_control_actions=[],
        priority=RequestPriority.HIGH,
    )

    waiting_snapshot = asyncio.run(service.run_snapshot(snapshot.id))
    assert waiting_snapshot.status.value == "waiting"
    assert waiting_snapshot.nodes["refactor"].status.value == "waiting"
    assert (
        waiting_snapshot.nodes["refactor"].waiting_reason
        is RunNodeWaitReason.ASSISTANT_PENDING
    )

    store.save_assistant_response_by_request_id(
        request.request_id,
        resolution_kind=ResolutionKind.AUTO_REPLY,
        answer="Delete it.",
        rationale="No compatibility layer is needed.",
        confidence=ConfidenceLevel.HIGH,
        citations=["~/.codex/AGENTS.md"],
        proposed_guidance_updates=[],
        proposed_control_actions=[],
    )

    resumed_snapshot = asyncio.run(service.resume_run(snapshot.id))
    assert resumed_snapshot.status.value == "done"
    assert resumed_snapshot.nodes["refactor"].status.value == "done"
    prompt = runner.prompts["refactor"]
    assert "## Assistant Continuation Context" in prompt
    assert "### Original Question\nCan I delete the legacy wrapper?" in prompt
    assert "### Assistant Answer\nDelete it." in prompt
    assert "### Assistant Rationale\nNo compatibility layer is needed." in prompt
    assert "- ~/.codex/AGENTS.md" in prompt
    assert "## Execution Contract" in prompt


def test_run_service_does_not_inject_assistant_packet_into_downstream_task(
    tmp_path: Path,
) -> None:
    store = build_test_store(tmp_path)
    write_assistant_profile(store, set_as_default=True)
    store.save_task(
        TaskSpec(
            id="analyze",
            title="Analyze",
            agent="explorer",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/analyze.md"}],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="implement",
            title="Implement",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "analyze", "kind": "context", "consume": ["final.md"]}],
            compose=[
                {"kind": "file", "path": "prompts/implement.md"},
                {"kind": "from_dep", "task": "analyze", "path": "final.md"},
            ],
            publish=["final.md"],
        )
    )

    runner = FakeRunner()
    service = RunService(store, runner)
    snapshot = service.create_snapshot(roots=["implement"], labels=[], user_inputs=None)
    request = store.create_assistant_request(
        run_id=snapshot.id,
        task_id="analyze",
        request_kind=RequestKind.CLARIFICATION,
        question="Should I delete the legacy wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["delete", "keep_wrapper"],
        context_artifacts=[],
        requested_control_actions=[],
        priority=RequestPriority.HIGH,
    )

    waiting_snapshot = asyncio.run(service.run_snapshot(snapshot.id))
    assert waiting_snapshot.status.value == "waiting"
    assert waiting_snapshot.nodes["analyze"].status.value == "waiting"
    assert waiting_snapshot.nodes["implement"].status.value == "pending"

    store.save_assistant_response_by_request_id(
        request.request_id,
        resolution_kind=ResolutionKind.AUTO_REPLY,
        answer="Delete it.",
        rationale="No compatibility layer is needed.",
        confidence=ConfidenceLevel.HIGH,
        citations=["~/.codex/AGENTS.md"],
        proposed_guidance_updates=[],
        proposed_control_actions=[],
    )

    resumed_snapshot = asyncio.run(service.resume_run(snapshot.id))
    assert resumed_snapshot.status.value == "done"
    assert resumed_snapshot.nodes["analyze"].status.value == "done"
    assert resumed_snapshot.nodes["implement"].status.value == "done"

    analyze_prompt = runner.prompts["analyze"]
    implement_prompt = runner.prompts["implement"]
    assert "## Assistant Continuation Context" in analyze_prompt
    assert "Delete it." in analyze_prompt
    assert "## Assistant Continuation Context" not in implement_prompt
    assert "Delete it." not in implement_prompt


def test_run_service_resumes_after_manual_gate_approval(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_profile(store, set_as_default=True)
    legacy_doc = store.paths.root / "docs" / "legacy-wrapper.md"
    legacy_doc.parent.mkdir(parents=True, exist_ok=True)
    legacy_doc.write_text("Legacy wrapper notes.\n", encoding="utf-8")
    store.save_task(
        TaskSpec(
            id="refactor",
            title="Refactor",
            agent="worker",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    runner = FakeRunner()
    service = RunService(store, runner)
    snapshot = service.create_snapshot(roots=["refactor"], labels=[], user_inputs=None)
    request = store.create_assistant_request(
        run_id=snapshot.id,
        task_id="refactor",
        request_kind=RequestKind.CLARIFICATION,
        question="Should I delete the legacy wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["delete", "keep_wrapper"],
        context_artifacts=["docs/legacy-wrapper.md"],
        requested_control_actions=[],
        priority=RequestPriority.HIGH,
    )
    store.save_assistant_response_by_request_id(
        request.request_id,
        resolution_kind=ResolutionKind.HANDOFF_TO_HUMAN,
        answer="I need a human decision.",
        rationale="The boundary is ambiguous.",
        confidence=ConfidenceLevel.MEDIUM,
        citations=["~/.codex/AGENTS.md"],
        proposed_guidance_updates=[],
        proposed_control_actions=[],
    )

    waiting_snapshot = asyncio.run(service.run_snapshot(snapshot.id))
    assert waiting_snapshot.status.value == "waiting"
    assert (
        waiting_snapshot.nodes["refactor"].waiting_reason
        is RunNodeWaitReason.HANDOFF_TO_HUMAN
    )

    gate_record = store.find_manual_gate_by_request_id(request.request_id)
    assert gate_record.gate.status is ManualGateStatus.WAITING_FOR_HUMAN
    assert gate_record.human_request is not None

    store.save_human_response_by_request_id(
        request.request_id,
        answer="Delete it.",
    )
    store.update_manual_gate_status_by_request_id(
        request.request_id,
        ManualGateStatus.APPROVED,
    )

    resumed_snapshot = asyncio.run(service.resume_run(snapshot.id))
    assert resumed_snapshot.status.value == "done"
    assert resumed_snapshot.nodes["refactor"].status.value == "done"
    updated_gate = store.find_manual_gate_by_request_id(request.request_id)
    assert updated_gate.gate.status is ManualGateStatus.APPLIED
    prompt = runner.prompts["refactor"]
    assert "## Human-Approved Continuation Context" in prompt
    assert "Apply this decision only to this task continuation." in prompt
    assert "### Original Question\nShould I delete the legacy wrapper?" in prompt
    assert "### Assistant Summary\nI need a human decision." in prompt
    assert "### Assistant Rationale\nThe boundary is ambiguous." in prompt
    assert "### Human Answer\nDelete it." in prompt
    assert "- ~/.codex/AGENTS.md" in prompt
    assert "- `docs/legacy-wrapper.md`" in prompt


def test_run_service_does_not_inject_manual_gate_packet_into_downstream_task(
    tmp_path: Path,
) -> None:
    store = build_test_store(tmp_path)
    write_assistant_profile(store, set_as_default=True)
    legacy_doc = store.paths.root / "docs" / "legacy-wrapper.md"
    legacy_doc.parent.mkdir(parents=True, exist_ok=True)
    legacy_doc.write_text("Legacy wrapper notes.\n", encoding="utf-8")
    store.save_task(
        TaskSpec(
            id="analyze",
            title="Analyze",
            agent="explorer",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/analyze.md"}],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="implement",
            title="Implement",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "analyze", "kind": "context", "consume": ["final.md"]}],
            compose=[
                {"kind": "file", "path": "prompts/implement.md"},
                {"kind": "from_dep", "task": "analyze", "path": "final.md"},
            ],
            publish=["final.md"],
        )
    )

    runner = FakeRunner()
    service = RunService(store, runner)
    snapshot = service.create_snapshot(roots=["implement"], labels=[], user_inputs=None)
    request = store.create_assistant_request(
        run_id=snapshot.id,
        task_id="analyze",
        request_kind=RequestKind.CLARIFICATION,
        question="Should I delete the legacy wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["delete", "keep_wrapper"],
        context_artifacts=["docs/legacy-wrapper.md"],
        requested_control_actions=[],
        priority=RequestPriority.HIGH,
    )
    store.save_assistant_response_by_request_id(
        request.request_id,
        resolution_kind=ResolutionKind.HANDOFF_TO_HUMAN,
        answer="I need a human decision.",
        rationale="The boundary is ambiguous.",
        confidence=ConfidenceLevel.MEDIUM,
        citations=["~/.codex/AGENTS.md"],
        proposed_guidance_updates=[],
        proposed_control_actions=[],
    )

    waiting_snapshot = asyncio.run(service.run_snapshot(snapshot.id))
    assert waiting_snapshot.status.value == "waiting"
    assert waiting_snapshot.nodes["analyze"].status.value == "waiting"
    assert waiting_snapshot.nodes["implement"].status.value == "pending"

    store.save_human_response_by_request_id(
        request.request_id,
        answer="Delete it.",
    )
    store.update_manual_gate_status_by_request_id(
        request.request_id,
        ManualGateStatus.APPROVED,
    )

    resumed_snapshot = asyncio.run(service.resume_run(snapshot.id))
    assert resumed_snapshot.status.value == "done"
    assert resumed_snapshot.nodes["analyze"].status.value == "done"
    assert resumed_snapshot.nodes["implement"].status.value == "done"

    analyze_prompt = runner.prompts["analyze"]
    implement_prompt = runner.prompts["implement"]
    assert "## Human-Approved Continuation Context" in analyze_prompt
    assert "Delete it." in analyze_prompt
    assert "## Human-Approved Continuation Context" not in implement_prompt
    assert "Delete it." not in implement_prompt


def test_run_service_fails_after_manual_gate_rejection(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_profile(store, set_as_default=True)
    store.save_task(
        TaskSpec(
            id="refactor",
            title="Refactor",
            agent="worker",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    service = RunService(store, FakeRunner())
    snapshot = service.create_snapshot(roots=["refactor"], labels=[], user_inputs=None)
    request = store.create_assistant_request(
        run_id=snapshot.id,
        task_id="refactor",
        request_kind=RequestKind.CLARIFICATION,
        question="Should I delete the legacy wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["delete", "keep_wrapper"],
        context_artifacts=[],
        requested_control_actions=[],
        priority=RequestPriority.HIGH,
    )
    store.save_assistant_response_by_request_id(
        request.request_id,
        resolution_kind=ResolutionKind.HANDOFF_TO_HUMAN,
        answer="I need a human decision.",
        rationale="The boundary is ambiguous.",
        confidence=ConfidenceLevel.MEDIUM,
        citations=["~/.codex/AGENTS.md"],
        proposed_guidance_updates=[],
        proposed_control_actions=[],
    )
    asyncio.run(service.run_snapshot(snapshot.id))

    store.save_human_response_by_request_id(
        request.request_id,
        answer="Do not continue.",
    )
    store.update_manual_gate_status_by_request_id(
        request.request_id,
        ManualGateStatus.REJECTED,
    )

    rejected_snapshot = asyncio.run(service.resume_run(snapshot.id))
    assert rejected_snapshot.status.value == "failed"
    assert rejected_snapshot.nodes["refactor"].status.value == "failed"


def test_reconcile_run_marks_orphaned_running_node_failed(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="refactor",
            title="Refactor",
            agent="worker",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    service = RunService(store, FakeRunner())
    snapshot = service.create_snapshot(roots=["refactor"], labels=[], user_inputs=None)
    node = snapshot.nodes["refactor"]
    node.status = RunNodeStatus.RUNNING
    node.attempt = 1
    node.started_at = datetime.now(UTC).isoformat()
    snapshot.status = RunStatus.RUNNING
    store.save_run(snapshot)
    store.save_runtime(
        snapshot.id,
        "refactor",
        NodeExecutionRuntime(
            pid=999_999_999,
            cwd=str(store.paths.root),
            command=["codex", "exec", "--json"],
        ),
    )

    reconciled = asyncio.run(service.reconcile_run(snapshot.id))

    assert reconciled.status is RunStatus.FAILED
    assert reconciled.nodes["refactor"].status is RunNodeStatus.FAILED
    assert (
        reconciled.nodes["refactor"].termination_reason
        is NodeExecutionTerminationReason.ORPHANED
    )


def test_resume_run_reconciles_orphaned_node_before_rerun(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="refactor",
            title="Refactor",
            agent="worker",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    service = RunService(store, FakeRunner())
    snapshot = service.create_snapshot(roots=["refactor"], labels=[], user_inputs=None)
    node = snapshot.nodes["refactor"]
    node.status = RunNodeStatus.RUNNING
    node.attempt = 1
    node.started_at = datetime.now(UTC).isoformat()
    snapshot.status = RunStatus.RUNNING
    store.save_run(snapshot)
    store.save_runtime(
        snapshot.id,
        "refactor",
        NodeExecutionRuntime(
            pid=999_999_999,
            cwd=str(store.paths.root),
            command=["codex", "exec", "--json"],
        ),
    )

    resumed = asyncio.run(service.resume_run(snapshot.id))

    assert resumed.status is RunStatus.DONE
    assert resumed.nodes["refactor"].status is RunNodeStatus.DONE
    assert resumed.nodes["refactor"].attempt == 2
    assert (
        resumed.nodes["refactor"].termination_reason
        is NodeExecutionTerminationReason.COMPLETED
    )


def test_abort_run_marks_active_nodes_failed(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="refactor",
            title="Refactor",
            agent="worker",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    service = RunService(store, FakeRunner())
    snapshot = service.create_snapshot(roots=["refactor"], labels=[], user_inputs=None)
    node = snapshot.nodes["refactor"]
    node.status = RunNodeStatus.RUNNING
    node.attempt = 1
    node.started_at = datetime.now(UTC).isoformat()
    snapshot.status = RunStatus.RUNNING
    store.save_run(snapshot)
    store.save_runtime(
        snapshot.id,
        "refactor",
        NodeExecutionRuntime(
            pid=999_999_999,
            cwd=str(store.paths.root),
            command=["codex", "exec", "--json"],
        ),
    )

    aborted = asyncio.run(service.abort_run(snapshot.id))

    assert aborted.status is RunStatus.FAILED
    assert aborted.nodes["refactor"].status is RunNodeStatus.FAILED
    assert (
        aborted.nodes["refactor"].termination_reason
        is NodeExecutionTerminationReason.TERMINATED
    )


def test_reconcile_ignores_waiting_assistant_node(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_profile(store, set_as_default=True)
    store.save_task(
        TaskSpec(
            id="refactor",
            title="Refactor",
            agent="worker",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    service = RunService(store, FakeRunner())
    snapshot = service.create_snapshot(roots=["refactor"], labels=[], user_inputs=None)
    request = store.create_assistant_request(
        run_id=snapshot.id,
        task_id="refactor",
        request_kind=RequestKind.CLARIFICATION,
        question="Need confirmation?",
        decision_kind=DecisionKind.POLICY,
        options=["yes", "no"],
        context_artifacts=[],
        requested_control_actions=[],
        priority=RequestPriority.NORMAL,
    )
    del request
    node = snapshot.nodes["refactor"]
    node.status = RunNodeStatus.WAITING
    node.waiting_reason = RunNodeWaitReason.ASSISTANT_PENDING
    node.error = "assistant request is waiting for a reply"
    snapshot.status = RunStatus.WAITING
    store.save_run(snapshot)
    store.save_runtime(
        snapshot.id,
        "refactor",
        NodeExecutionRuntime(
            pid=999_999_999,
            cwd=str(store.paths.root),
            command=["codex", "exec", "--json"],
            started_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            last_progress_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        ),
    )

    reconciled = asyncio.run(service.reconcile_run(snapshot.id))

    assert reconciled.status is RunStatus.WAITING
    assert reconciled.nodes["refactor"].status is RunNodeStatus.WAITING
    assert (
        reconciled.nodes["refactor"].waiting_reason
        is RunNodeWaitReason.ASSISTANT_PENDING
    )
