from __future__ import annotations

import asyncio
import json
from pathlib import Path

from codex_orch.assistant import AssistantRoleRouter
from codex_orch.domain import (
    InterruptAudience,
    InterruptReplyKind,
    NodeExecutionRuntime,
    NodeExecutionTerminationReason,
    RequestKind,
    RequestPriority,
    RunInstanceStatus,
    RunStatus,
    TaskSpec,
    TaskStatus,
    DecisionKind,
)
from codex_orch.runner import NodeExecutionRequest, NodeExecutionResult
from codex_orch.scheduler import RunService
from tests.helpers import build_test_store, write_assistant_role


class FakeRunner:
    def __init__(self, store=None) -> None:
        self.store = store
        self.prompts: dict[str, str] = {}
        self.requests: dict[str, NodeExecutionRequest] = {}
        self._first_attempt_interrupts: set[str] = set()

    async def run(self, request: NodeExecutionRequest) -> NodeExecutionResult:
        self.prompts[request.task.id] = request.prompt
        self.requests[request.task.id] = request
        final_path = request.attempt_dir / "final.md"
        if request.task.id == "analyze":
            final_path.write_text("analysis result\n", encoding="utf-8")
            return NodeExecutionResult(
                success=True,
                return_code=0,
                final_message="analysis result",
                session_id=f"session-{request.instance_id}",
            )
        if request.task.id == "gate":
            payload = {
                "result": {"summary": "ready to publish"},
                "control": {"labels": ["done"]},
            }
            payload_text = json.dumps(payload, indent=2, sort_keys=True)
            final_path.write_text(payload_text + "\n", encoding="utf-8")
            (request.attempt_dir / "result.json").write_text(
                payload_text + "\n",
                encoding="utf-8",
            )
            return NodeExecutionResult(
                success=True,
                return_code=0,
                final_message=payload_text,
                session_id=f"session-{request.instance_id}",
            )
        if (
            self.store is not None
            and request.task.id == "worker"
            and request.resume_session_id is None
            and request.instance_id not in self._first_attempt_interrupts
        ):
            self._first_attempt_interrupts.add(request.instance_id)
            recommendation, resolution = AssistantRoleRouter(self.store).resolve_assistant_target(
                run_id=request.run_id,
                task_id=request.task.id,
                request_kind=RequestKind.CLARIFICATION,
                decision_kind=DecisionKind.POLICY,
                requested_target_role_id=None,
            )
            self.store.create_interrupt(
                run_id=request.run_id,
                instance_id=request.instance_id,
                audience=InterruptAudience.ASSISTANT,
                blocking=True,
                request_kind=RequestKind.CLARIFICATION,
                question="Can I delete the wrapper?",
                decision_kind=DecisionKind.POLICY,
                options=["delete", "keep_wrapper"],
                context_artifacts=[],
                reply_schema=None,
                priority=RequestPriority.HIGH,
                requested_target_role_id=resolution.requested_target_role_id,
                recommended_target_role_id=recommendation.recommended_target_role_id,
                resolved_target_role_id=resolution.resolved_target_role_id,
                target_resolution_reason=resolution.target_resolution_reason,
                metadata={},
            )
        final_path.write_text(request.prompt, encoding="utf-8")
        return NodeExecutionResult(
            success=True,
            return_code=0,
            final_message=request.prompt,
            session_id=f"session-{request.instance_id}",
        )


def _instance_for_task(run, task_id: str):
    matches = [instance for instance in run.instances.values() if instance.task_id == task_id]
    assert len(matches) == 1
    return matches[0]


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
            depends_on=[
                {
                    "task": "analyze",
                    "as": "analysis",
                    "kind": "context",
                    "consume": ["final.md"],
                }
            ],
            compose=[
                {"kind": "file", "path": "prompts/implement.md"},
                {"kind": "ref", "ref": "deps.analysis.artifacts.final.md"},
                {"kind": "ref", "ref": "inputs.brief"},
            ],
            publish=["final.md"],
        )
    )

    runner = FakeRunner()
    service = RunService(store, runner)
    run = asyncio.run(service.start_run(roots=["implement"], labels=[], user_inputs=None))

    assert run.status is RunStatus.DONE
    analyze = _instance_for_task(run, "analyze")
    implement = _instance_for_task(run, "implement")
    assert analyze.status is RunInstanceStatus.DONE
    assert implement.status is RunInstanceStatus.DONE
    assert analyze.session_id is not None
    published = (
        store.get_instance_published_dir(run.id, implement.instance_id) / "final.md"
    ).read_text(encoding="utf-8")
    staged_dep = (
        store.get_attempt_dir(run.id, implement.instance_id, 1)
        / "context"
        / "refs"
        / "deps"
        / "analysis"
        / "artifacts"
        / "final.md"
    ).read_text(encoding="utf-8")
    staged_input = (
        store.get_attempt_dir(run.id, implement.instance_id, 1)
        / "context"
        / "refs"
        / "inputs"
        / "brief.txt"
    ).read_text(encoding="utf-8")
    assert "## File Prompt: prompts/implement.md" in published
    assert "## Ref: deps.analysis.artifacts.final.md" in published
    assert "## Ref: inputs.brief" in published
    assert "## Execution Contract" in published
    assert "Read this file directly if you need its contents" in published
    assert staged_dep == "analysis result\n"
    assert staged_input == "brief input\n"


def test_run_service_waits_for_blocking_interrupt_and_resumes_same_session(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store)
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="worker",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )

    runner = FakeRunner(store)
    service = RunService(store, runner)
    run = asyncio.run(service.start_run(roots=["worker"], labels=[], user_inputs=None))
    instance = _instance_for_task(run, "worker")

    assert run.status is RunStatus.WAITING
    assert instance.status is RunInstanceStatus.WAITING
    assert instance.session_id == f"session-{instance.instance_id}"
    records = store.list_instance_interrupts(run.id, instance.instance_id)
    assert len(records) == 1
    interrupt = records[0].interrupt

    store.save_interrupt_reply(
        interrupt.interrupt_id,
        audience=InterruptAudience.ASSISTANT,
        reply_kind=InterruptReplyKind.ANSWER,
        text="Delete it.",
        payload={},
        rationale="No compatibility wrapper is needed.",
    )

    resumed = asyncio.run(service.resume_run(run.id))
    resumed_instance = _instance_for_task(resumed, "worker")

    assert resumed.status is RunStatus.DONE
    assert resumed_instance.status is RunInstanceStatus.DONE
    assert resumed_instance.attempt == 2
    assert runner.requests["worker"].resume_session_id == f"session-{instance.instance_id}"
    assert "## Resume Context" in runner.prompts["worker"]
    assert "Delete it." in runner.prompts["worker"]
    assert store.find_interrupt(interrupt.interrupt_id).interrupt.status.value == "applied"


def test_reconcile_run_marks_orphaned_running_instance_failed(tmp_path: Path) -> None:
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
    run = service.create_snapshot(roots=["refactor"], labels=[], user_inputs=None)
    instance = _instance_for_task(run, "refactor")
    instance.status = RunInstanceStatus.RUNNING
    instance.attempt = 1
    store.save_run(run)

    runtime = NodeExecutionRuntime(
        pid=999999,
        cwd=str(store.paths.root),
        project_workspace_dir=str(store.paths.root),
        command=["codex", "exec", "-"],
        sandbox="read-only",
        writable_roots=[],
    )
    store.get_attempt_runtime_path(run.id, instance.instance_id, 1).write_text(
        json.dumps(runtime.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    reconciled = asyncio.run(service.reconcile_run(run.id))
    updated = _instance_for_task(reconciled, "refactor")

    assert updated.status is RunInstanceStatus.FAILED
    assert updated.termination_reason is NodeExecutionTerminationReason.ORPHANED
    assert reconciled.status is RunStatus.FAILED


def test_abort_run_marks_active_instances_failed(tmp_path: Path) -> None:
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
    run = service.create_snapshot(roots=["refactor"], labels=[], user_inputs=None)
    instance = _instance_for_task(run, "refactor")
    instance.status = RunInstanceStatus.RUNNABLE
    store.save_run(run)

    aborted = asyncio.run(service.abort_run(run.id))
    updated = _instance_for_task(aborted, "refactor")

    assert aborted.status is RunStatus.FAILED
    assert updated.status in {RunInstanceStatus.FAILED, RunInstanceStatus.SKIPPED}


def test_run_service_branches_from_controller_without_placeholder_instances(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="source",
            title="Source",
            agent="worker",
            status=TaskStatus.READY,
            compose=[{"kind": "literal", "text": "prepare inputs"}],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="gate",
            title="Gate",
            agent="worker",
            kind="controller",
            status=TaskStatus.READY,
            depends_on=[{"task": "source", "kind": "order", "consume": []}],
            compose=[{"kind": "literal", "text": "emit controller result"}],
            control={
                "routes": [
                    {"label": "fix", "targets": ["apply_fix"]},
                    {"label": "done", "targets": ["publish_summary"]},
                ]
            },
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="apply_fix",
            title="Apply Fix",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "gate", "kind": "order", "consume": []}],
            compose=[{"kind": "literal", "text": "fix things"}],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="publish_summary",
            title="Publish Summary",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "gate", "as": "gate", "kind": "order", "consume": []}],
            compose=[{"kind": "ref", "ref": "deps.gate.result"}],
            publish=["final.md"],
        )
    )

    runner = FakeRunner()
    run = asyncio.run(
        RunService(store, runner).start_run(
            roots=["publish_summary"],
            labels=[],
            user_inputs=None,
        )
    )

    assert run.status is RunStatus.DONE
    gate = _instance_for_task(run, "gate")
    publish = _instance_for_task(run, "publish_summary")
    assert [instance for instance in run.instances.values() if instance.task_id == "apply_fix"] == []
    assert store.maybe_get_instance_result(run.id, gate.instance_id) == {
        "control": {"labels": ["done"]},
        "result": {"summary": "ready to publish"},
    }
    assert "## Ref: deps.gate.result" in runner.prompts["publish_summary"]
    staged_result = (
        store.get_attempt_dir(run.id, publish.instance_id, 1)
        / "context"
        / "refs"
        / "deps"
        / "gate"
        / "result.json"
    ).read_text(encoding="utf-8")
    assert '"labels": [' in staged_result
    event_types = [event.event_type for event in store.list_events(run.id)]
    assert "route_selected" in event_types
    assert "route_unselected" in event_types
