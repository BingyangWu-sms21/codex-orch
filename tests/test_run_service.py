from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from codex_orch.assistant import AssistantRoleRouter
from codex_orch.domain import (
    InterruptAudience,
    InterruptReplyKind,
    NodeExecutionFailureKind,
    NodeExecutionRuntime,
    NodeExecutionTerminationReason,
    ProjectSpec,
    RequestKind,
    RequestPriority,
    RunInstanceStatus,
    RunStatus,
    TaskSpec,
    TaskStatus,
    DecisionKind,
)
from codex_orch.domain.runtime import ControlEnvelope
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
        if request.task.id == "source":
            final_path.write_text("source prepared\n", encoding="utf-8")
            (request.attempt_dir / "result.json").write_text(
                json.dumps({"quality": "good"}, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return NodeExecutionResult(
                success=True,
                return_code=0,
                final_message="source prepared",
                session_id=f"session-{request.instance_id}",
            )
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
                "control": {"kind": "route", "labels": ["done"]},
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
        if request.task.id == "decide_route":
            decision = self._read_text(
                request.attempt_dir / "context" / "refs" / "inputs" / "decision.txt"
            )
            source_quality = json.loads(
                (
                    request.attempt_dir
                    / "context"
                    / "refs"
                    / "deps"
                    / "src"
                    / "result.json"
                ).read_text(encoding="utf-8")
            )["quality"]
            label = "fix" if decision == "fix" or source_quality == "needs_fix" else "done"
            payload = {
                "result": {"decision": decision, "source_quality": source_quality},
                "control": {"kind": "route", "labels": [label]},
            }
            return self._complete_with_json(
                request,
                payload,
            )
        if request.task.id == "prepare_context":
            final_path.write_text("shared context\n", encoding="utf-8")
            return NodeExecutionResult(
                success=True,
                return_code=0,
                final_message="shared context",
                session_id=f"session-{request.instance_id}",
            )
        if request.task.id == "loop_gate":
            budget = int(
                self._read_text(
                    request.attempt_dir
                    / "context"
                    / "refs"
                    / "inputs"
                    / "attempt_budget.txt"
                )
            )
            if budget > 1:
                payload = {
                    "result": {"remaining_budget": budget},
                    "control": {
                        "kind": "loop",
                        "action": "continue",
                        "next_inputs": {
                            "attempt_budget": str(budget - 1),
                            "attempt_note": f"remaining={budget - 1}",
                        },
                    },
                }
            else:
                payload = {
                    "result": {"remaining_budget": budget},
                    "control": {
                        "kind": "loop",
                        "action": "stop",
                    },
                }
            return self._complete_with_json(request, payload)
        if (
            self.store is not None
            and request.task.id == "policy_gate"
            and request.resume_session_id is None
            and request.instance_id not in self._first_attempt_interrupts
        ):
            self._first_attempt_interrupts.add(request.instance_id)
            recommendation, resolution = AssistantRoleRouter(self.store).resolve_assistant_target(
                run_id=request.run_id,
                task_id=request.task.id,
                request_kind=RequestKind.APPROVAL,
                decision_kind=DecisionKind.POLICY,
                requested_target_role_id=None,
            )
            self.store.create_interrupt(
                run_id=request.run_id,
                instance_id=request.instance_id,
                audience=InterruptAudience.ASSISTANT,
                blocking=True,
                request_kind=RequestKind.APPROVAL,
                question="Should we ship this change?",
                decision_kind=DecisionKind.POLICY,
                options=["ship", "revise"],
                context_artifacts=[],
                reply_schema=None,
                priority=RequestPriority.HIGH,
                requested_target_role_id=resolution.requested_target_role_id,
                recommended_target_role_id=recommendation.recommended_target_role_id,
                resolved_target_role_id=resolution.resolved_target_role_id,
                target_resolution_reason=resolution.target_resolution_reason,
                metadata={},
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
        if request.task.id == "policy_gate" and request.resume_session_id is not None:
            label = "done" if "Ship it." in request.prompt else "revise"
            payload = {
                "result": {"assistant_answer": label},
                "control": {"kind": "route", "labels": [label]},
            }
            return self._complete_with_json(request, payload)
        final_path.write_text(request.prompt, encoding="utf-8")
        return NodeExecutionResult(
            success=True,
            return_code=0,
            final_message=request.prompt,
            session_id=f"session-{request.instance_id}",
        )

    def _complete_with_json(
        self,
        request: NodeExecutionRequest,
        payload: dict[str, object],
    ) -> NodeExecutionResult:
        payload_text = json.dumps(payload, indent=2, sort_keys=True)
        (request.attempt_dir / "final.md").write_text(payload_text + "\n", encoding="utf-8")
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

    def _read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8").strip()


def _instance_for_task(run, task_id: str):
    matches = [instance for instance in run.instances.values() if instance.task_id == task_id]
    assert len(matches) == 1
    return matches[0]


def _instances_for_task(run, task_id: str):
    return sorted(
        [instance for instance in run.instances.values() if instance.task_id == task_id],
        key=lambda instance: instance.instance_id,
    )


def test_resume_run_requeues_recoverable_failed_instance_and_reopens_skipped_descendants(
    tmp_path: Path,
) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="source",
            title="Source",
            agent="default",
            status=TaskStatus.READY,
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            depends_on=[{"task": "source", "kind": "order", "consume": []}],
            publish=["final.md"],
        )
    )

    class RecoverableFailureRunner(FakeRunner):
        async def run(self, request: NodeExecutionRequest) -> NodeExecutionResult:
            self.prompts[request.task.id] = request.prompt
            self.requests[request.task.id] = request
            if request.task.id == "source" and request.attempt_no == 1:
                return NodeExecutionResult(
                    success=False,
                    return_code=1,
                    final_message="",
                    session_id=f"session-{request.instance_id}",
                    error="stream disconnected before completion",
                    termination_reason=NodeExecutionTerminationReason.NONZERO_EXIT,
                    failure_kind=NodeExecutionFailureKind.EXTERNAL_PROTOCOL,
                    failure_summary="stream disconnected before completion",
                    resume_recommended=True,
                )
            (request.attempt_dir / "final.md").write_text(
                f"{request.task.id} complete\n",
                encoding="utf-8",
            )
            return NodeExecutionResult(
                success=True,
                return_code=0,
                final_message=f"{request.task.id} complete",
                session_id=f"session-{request.instance_id}",
            )

    service = RunService(store, RecoverableFailureRunner())
    run = asyncio.run(
        service.start_run(
            roots=["worker"],
            labels=[],
            user_inputs=None,
        )
    )

    source = _instance_for_task(run, "source")
    worker = _instance_for_task(run, "worker")
    assert run.status is RunStatus.FAILED
    assert source.status is RunInstanceStatus.FAILED
    assert source.resume_recommended is True
    assert worker.status is RunInstanceStatus.SKIPPED

    resumed = asyncio.run(service.resume_run(run.id))
    source_after = _instance_for_task(resumed, "source")
    worker_after = _instance_for_task(resumed, "worker")

    assert resumed.status is RunStatus.DONE
    assert source_after.instance_id == source.instance_id
    assert source_after.status is RunInstanceStatus.DONE
    assert source_after.attempt == 2
    assert worker_after.status is RunInstanceStatus.DONE
    assert worker_after.attempt == 1


def test_run_execution_does_not_mutate_task_pool_status(tmp_path: Path) -> None:
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

    run = asyncio.run(
        RunService(store, FakeRunner()).start_run(
            roots=["worker"],
            labels=[],
            user_inputs=None,
        )
    )

    assert run.status is RunStatus.DONE
    assert store.get_task("worker").status is TaskStatus.READY


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


def test_create_snapshot_materializes_dynamic_project_and_task_paths(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    dynamic_root = tmp_path / "dynamic-root"
    task_workspace = dynamic_root / "repos" / "demo"
    extra_root = task_workspace / "logs" / "demo"
    extra_root.mkdir(parents=True, exist_ok=True)
    (store.paths.inputs_dir / "workspace_root.txt").write_text(
        str(dynamic_root),
        encoding="utf-8",
    )
    (store.paths.inputs_dir / "slug.txt").write_text("demo", encoding="utf-8")
    store.save_project(
        ProjectSpec(
            name="dynamic-program",
            workspace="${inputs.workspace_root}",
            default_agent="default",
            default_sandbox="workspace-write",
            user_inputs={
                "workspace_root": "inputs/workspace_root.txt",
                "slug": "inputs/slug.txt",
            },
            max_concurrency=2,
        )
    )
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            workspace="repos/${inputs.slug}",
            extra_writable_roots=["logs/${inputs.slug}"],
            publish=["final.md"],
        )
    )

    run = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )

    assert run.project.workspace == str(dynamic_root.resolve())
    assert run.tasks["worker"].workspace == str(task_workspace.resolve())
    assert run.tasks["worker"].extra_writable_roots == [str(extra_root.resolve())]
    assert run.project_workspace_template == "${inputs.workspace_root}"
    assert run.task_path_templates["worker"].workspace == "repos/${inputs.slug}"
    assert run.task_path_templates["worker"].extra_writable_roots == [
        "logs/${inputs.slug}"
    ]


def test_loop_continue_recomputes_dynamic_paths_for_new_input_scope(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    workspace_root_a = tmp_path / "workspace-a"
    workspace_root_b = tmp_path / "workspace-b"
    for workspace_root, slug in (
        (workspace_root_a, "a"),
        (workspace_root_b, "b"),
    ):
        (workspace_root / "repos" / slug / "logs" / slug).mkdir(
            parents=True,
            exist_ok=True,
        )
    store.save_project(
        ProjectSpec(
            name="dynamic-loop",
            workspace="${inputs.workspace_root}",
            default_agent="default",
            default_sandbox="workspace-write",
            max_concurrency=1,
        )
    )
    store.save_task(
        TaskSpec(
            id="plan_iteration",
            title="Plan Iteration",
            agent="worker",
            status=TaskStatus.READY,
            workspace="repos/${inputs.slug}",
            extra_writable_roots=["logs/${inputs.slug}"],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="loop_gate",
            title="Loop Gate",
            agent="worker",
            kind="controller",
            status=TaskStatus.READY,
            depends_on=[{"task": "plan_iteration", "kind": "order", "consume": []}],
            compose=[{"kind": "ref", "ref": "inputs.slug"}],
            control={
                "mode": "loop",
                "continue_targets": ["plan_iteration"],
                "stop_targets": ["publish_summary"],
            },
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="publish_summary",
            title="Publish Summary",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "loop_gate", "kind": "order", "consume": []}],
            publish=["final.md"],
        )
    )

    class DynamicPathLoopRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.plan_requests: list[NodeExecutionRequest] = []

        async def run(self, request: NodeExecutionRequest) -> NodeExecutionResult:
            if request.task.id == "plan_iteration":
                self.plan_requests.append(request)
            if request.task.id == "loop_gate":
                slug = self._read_text(
                    request.attempt_dir / "context" / "refs" / "inputs" / "slug.txt"
                )
                if slug == "a":
                    return self._complete_with_json(
                        request,
                        {
                            "result": {"slug": slug},
                            "control": {
                                "kind": "loop",
                                "action": "continue",
                                "next_inputs": {
                                    "workspace_root": str(workspace_root_b),
                                    "slug": "b",
                                },
                            },
                        },
                    )
                return self._complete_with_json(
                    request,
                    {
                        "result": {"slug": slug},
                        "control": {
                            "kind": "loop",
                            "action": "stop",
                        },
                    },
                )
            return await super().run(request)

    runner = DynamicPathLoopRunner()
    run = asyncio.run(
        RunService(store, runner).start_run(
            roots=["publish_summary"],
            labels=[],
            user_inputs={
                "workspace_root": str(workspace_root_a),
                "slug": "a",
            },
        )
    )

    assert run.status is RunStatus.DONE
    assert len(runner.plan_requests) == 2
    assert runner.plan_requests[0].project_workspace_dir == workspace_root_a.resolve()
    assert runner.plan_requests[0].workspace_dir == (
        workspace_root_a / "repos" / "a"
    ).resolve()
    assert runner.plan_requests[0].extra_writable_roots == (
        (workspace_root_a / "repos" / "a" / "logs" / "a").resolve(),
    )
    assert runner.plan_requests[1].project_workspace_dir == workspace_root_b.resolve()
    assert runner.plan_requests[1].workspace_dir == (
        workspace_root_b / "repos" / "b"
    ).resolve()
    assert runner.plan_requests[1].extra_writable_roots == (
        (workspace_root_b / "repos" / "b" / "logs" / "b").resolve(),
    )


def test_input_ref_stages_structured_default_input_as_json(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    (store.paths.inputs_dir / "config.yaml").write_text(
        "answer: 7\nnested:\n  ok: true\n",
        encoding="utf-8",
    )
    store.save_project(
        store.load_project().model_copy(
            update={
                "user_inputs": {
                    "brief": "inputs/brief.md",
                    "config": "inputs/config.yaml",
                }
            }
        )
    )
    store.save_task(
        TaskSpec(
            id="inspect_input",
            title="Inspect Input",
            agent="default",
            status=TaskStatus.READY,
            compose=[{"kind": "ref", "ref": "inputs.config"}],
            publish=["final.md"],
        )
    )

    runner = FakeRunner()
    asyncio.run(
        RunService(store, runner).start_run(
            roots=["inspect_input"],
            labels=[],
            user_inputs=None,
        )
    )

    staged_path = (
        runner.requests["inspect_input"].attempt_dir
        / "context"
        / "refs"
        / "inputs"
        / "config.json"
    )
    assert json.loads(staged_path.read_text(encoding="utf-8")) == {
        "answer": 7,
        "nested": {"ok": True},
    }


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
            publish=["final.md", "result.json"],
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
                "mode": "route",
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
        "control": {"kind": "route", "labels": ["done"]},
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
    assert '"kind": "route"' in staged_result
    assert '"labels": [' in staged_result
    event_types = [event.event_type for event in store.list_events(run.id)]
    assert "route_selected" in event_types
    assert "route_unselected" in event_types


def test_route_controller_can_branch_from_inputs_and_dependency_result(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="source",
            title="Source",
            agent="worker",
            status=TaskStatus.READY,
            compose=[{"kind": "literal", "text": "prepare source result"}],
            publish=["final.md", "result.json"],
        )
    )
    store.save_task(
        TaskSpec(
            id="decide_route",
            title="Decide Route",
            agent="worker",
            kind="controller",
            status=TaskStatus.READY,
            depends_on=[{"task": "source", "as": "src", "kind": "order", "consume": []}],
            compose=[
                {"kind": "ref", "ref": "inputs.decision"},
                {"kind": "ref", "ref": "deps.src.result"},
            ],
            control={
                "mode": "route",
                "routes": [
                    {"label": "fix", "targets": ["apply_fix"]},
                    {"label": "done", "targets": ["publish_summary"]},
                ],
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
            depends_on=[{"task": "decide_route", "kind": "order", "consume": []}],
            compose=[{"kind": "literal", "text": "fix branch"}],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="publish_summary",
            title="Publish Summary",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "decide_route", "as": "gate", "kind": "order", "consume": []}],
            compose=[{"kind": "ref", "ref": "deps.gate.result"}],
            publish=["final.md"],
        )
    )

    run = asyncio.run(
        RunService(store, FakeRunner()).start_run(
            roots=["publish_summary"],
            labels=[],
            user_inputs={"decision": "done"},
        )
    )

    assert run.status is RunStatus.DONE
    assert _instances_for_task(run, "apply_fix") == []
    controller = _instance_for_task(run, "decide_route")
    assert store.maybe_get_instance_result(run.id, controller.instance_id) == {
        "control": {"kind": "route", "labels": ["done"]},
        "result": {"decision": "done", "source_quality": "good"},
    }


def test_controller_consumes_assistant_reply_before_emitting_route_control(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store)
    store.save_task(
        TaskSpec(
            id="policy_gate",
            title="Policy Gate",
            agent="worker",
            kind="controller",
            status=TaskStatus.READY,
            compose=[{"kind": "literal", "text": "ask whether to ship"}],
            control={
                "mode": "route",
                "routes": [
                    {"label": "done", "targets": ["ship_release"]},
                    {"label": "revise", "targets": ["revise_release"]},
                ],
            },
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="ship_release",
            title="Ship Release",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "policy_gate", "kind": "order", "consume": []}],
            compose=[{"kind": "literal", "text": "ship it"}],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="revise_release",
            title="Revise Release",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "policy_gate", "kind": "order", "consume": []}],
            compose=[{"kind": "literal", "text": "revise it"}],
            publish=["final.md"],
        )
    )

    runner = FakeRunner(store)
    service = RunService(store, runner)
    run = asyncio.run(service.start_run(roots=["ship_release"], labels=[], user_inputs=None))
    gate = _instance_for_task(run, "policy_gate")

    assert run.status is RunStatus.WAITING
    records = store.list_instance_interrupts(run.id, gate.instance_id)
    assert len(records) == 1
    interrupt = records[0].interrupt

    store.save_interrupt_reply(
        interrupt.interrupt_id,
        audience=InterruptAudience.ASSISTANT,
        reply_kind=InterruptReplyKind.ANSWER,
        text="Ship it.",
        payload={},
        rationale="The policy role approves shipping now.",
    )

    resumed = asyncio.run(service.resume_run(run.id))
    resumed_gate = _instance_for_task(resumed, "policy_gate")

    assert resumed.status is RunStatus.DONE
    assert resumed_gate.status is RunInstanceStatus.DONE
    assert "## Resume Context" in runner.prompts["policy_gate"]
    assert "Ship it." in runner.prompts["policy_gate"]
    assert _instances_for_task(resumed, "revise_release") == []


def test_loop_continue_creates_new_input_scope_and_reuses_static_external_dependency(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="prepare_context",
            title="Prepare Context",
            agent="worker",
            status=TaskStatus.READY,
            compose=[{"kind": "literal", "text": "prepare shared context"}],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="plan_iteration",
            title="Plan Iteration",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "prepare_context", "kind": "order", "consume": []}],
            compose=[
                {"kind": "literal", "text": "plan work"},
                {"kind": "ref", "ref": "inputs.attempt_budget"},
                {"kind": "ref", "ref": "inputs.attempt_note"},
            ],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="implement_iteration",
            title="Implement Iteration",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "plan_iteration", "kind": "order", "consume": []}],
            compose=[{"kind": "literal", "text": "implement iteration"}],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="loop_gate",
            title="Loop Gate",
            agent="worker",
            kind="controller",
            status=TaskStatus.READY,
            depends_on=[{"task": "implement_iteration", "kind": "order", "consume": []}],
            compose=[{"kind": "ref", "ref": "inputs.attempt_budget"}],
            control={
                "mode": "loop",
                "continue_targets": ["plan_iteration"],
                "stop_targets": ["publish_summary"],
            },
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="publish_summary",
            title="Publish Summary",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "loop_gate", "kind": "order", "consume": []}],
            compose=[
                {"kind": "ref", "ref": "inputs.attempt_budget"},
                {"kind": "ref", "ref": "inputs.attempt_note"},
            ],
            publish=["final.md"],
        )
    )

    runner = FakeRunner()
    run = asyncio.run(
        RunService(store, runner).start_run(
            roots=["publish_summary"],
            labels=[],
            user_inputs={"attempt_budget": "2", "attempt_note": "seed"},
        )
    )

    assert run.status is RunStatus.DONE
    plan_instances = _instances_for_task(run, "plan_iteration")
    gate_instances = _instances_for_task(run, "loop_gate")
    prepare_context = _instance_for_task(run, "prepare_context")
    publish_summary = _instance_for_task(run, "publish_summary")

    assert len(plan_instances) == 2
    assert len(gate_instances) == 2
    assert len(run.input_scopes) == 2
    assert all(
        instance.dependency_instances["prepare_context"] == prepare_context.instance_id
        for instance in plan_instances
    )
    assert len({instance.input_scope_id for instance in plan_instances}) == 2
    publish_attempt_dir = store.get_attempt_dir(run.id, publish_summary.instance_id, 1)
    assert (
        publish_attempt_dir / "context" / "refs" / "inputs" / "attempt_budget.txt"
    ).read_text(encoding="utf-8") == "1"
    assert (
        publish_attempt_dir / "context" / "refs" / "inputs" / "attempt_note.txt"
    ).read_text(encoding="utf-8") == "remaining=1"
    event_types = [event.event_type for event in store.list_events(run.id)]
    assert "loop_continued" in event_types
    assert "loop_stopped" in event_types
    assert event_types.count("input_scope_created") == 2


def test_loop_continue_supports_structured_next_inputs(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="plan_iteration",
            title="Plan Iteration",
            agent="worker",
            status=TaskStatus.READY,
            compose=[{"kind": "ref", "ref": "inputs.state"}],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="loop_gate",
            title="Loop Gate",
            agent="worker",
            kind="controller",
            status=TaskStatus.READY,
            depends_on=[{"task": "plan_iteration", "kind": "order", "consume": []}],
            compose=[{"kind": "ref", "ref": "inputs.state"}],
            control={
                "mode": "loop",
                "continue_targets": ["plan_iteration"],
                "stop_targets": ["publish_summary"],
            },
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="publish_summary",
            title="Publish Summary",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "loop_gate", "kind": "order", "consume": []}],
            compose=[{"kind": "ref", "ref": "inputs.state"}],
            publish=["final.md"],
        )
    )

    class StructuredLoopRunner(FakeRunner):
        async def run(self, request: NodeExecutionRequest) -> NodeExecutionResult:
            self.prompts[request.task.id] = request.prompt
            self.requests[request.task.id] = request
            final_path = request.attempt_dir / "final.md"
            if request.task.id == "loop_gate":
                state = json.loads(
                    (
                        request.attempt_dir
                        / "context"
                        / "refs"
                        / "inputs"
                        / "state.json"
                    ).read_text(encoding="utf-8")
                )
                remaining = list(state["remaining"])
                history = list(state["history"])
                if len(remaining) > 1:
                    next_state = {
                        "remaining": remaining[1:],
                        "history": [*history, remaining[0]],
                    }
                    payload = {
                        "result": {"state": next_state},
                        "control": {
                            "kind": "loop",
                            "action": "continue",
                            "next_inputs": {
                                "state": next_state,
                            },
                        },
                    }
                else:
                    payload = {
                        "result": {"state": state},
                        "control": {
                            "kind": "loop",
                            "action": "stop",
                        },
                    }
                return self._complete_with_json(request, payload)
            final_path.write_text(request.prompt, encoding="utf-8")
            return NodeExecutionResult(
                success=True,
                return_code=0,
                final_message=request.prompt,
                session_id=f"session-{request.instance_id}",
            )

    runner = StructuredLoopRunner()
    run = asyncio.run(
        RunService(store, runner).start_run(
            roots=["publish_summary"],
            labels=[],
            user_inputs={"state": {"remaining": [2, 1], "history": []}},
        )
    )

    assert run.status is RunStatus.DONE
    plan_instances = _instances_for_task(run, "plan_iteration")
    assert len(plan_instances) == 2
    plan_states = [
        json.loads(
            (
                store.get_attempt_dir(run.id, instance.instance_id, 1)
                / "context"
                / "refs"
                / "inputs"
                / "state.json"
            ).read_text(encoding="utf-8")
        )
        for instance in plan_instances
    ]
    assert {"remaining": [2, 1], "history": []} in plan_states
    assert {"remaining": [1], "history": [2]} in plan_states
    publish_summary = _instance_for_task(run, "publish_summary")
    publish_attempt_dir = store.get_attempt_dir(run.id, publish_summary.instance_id, 1)
    assert json.loads(
        (
            publish_attempt_dir
            / "context"
            / "refs"
            / "inputs"
            / "state.json"
        ).read_text(encoding="utf-8")
    ) == {"remaining": [1], "history": [2]}


def test_runtime_reply_refs_stage_resolved_replies_for_resumed_attempt(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store)
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            compose=[
                {"kind": "ref", "ref": "runtime.replies"},
                {"kind": "ref", "ref": "runtime.latest_reply"},
            ],
            publish=["final.md"],
        )
    )

    runner = FakeRunner(store)
    service = RunService(store, runner)
    run = asyncio.run(service.start_run(roots=["worker"], labels=[], user_inputs=None))
    instance = _instance_for_task(run, "worker")
    interrupt = store.list_instance_interrupts(run.id, instance.instance_id)[0]
    store.save_interrupt_reply(
        interrupt.interrupt.interrupt_id,
        audience=InterruptAudience.ASSISTANT,
        reply_kind=InterruptReplyKind.ANSWER,
        text="Delete the wrapper.",
        payload={"decision": "delete"},
        rationale="The compatibility layer is stale.",
    )

    resumed = asyncio.run(service.resume_run(run.id))
    resumed_instance = _instance_for_task(resumed, "worker")
    attempt_dir = store.get_attempt_dir(resumed.id, resumed_instance.instance_id, 2)

    replies = json.loads(
        (
            attempt_dir / "context" / "refs" / "runtime" / "replies.json"
        ).read_text(encoding="utf-8")
    )
    latest_reply = json.loads(
        (
            attempt_dir / "context" / "refs" / "runtime" / "latest_reply.json"
        ).read_text(encoding="utf-8")
    )

    assert len(replies) == 1
    assert replies[0]["reply"]["payload"] == {"decision": "delete"}
    assert latest_reply["reply"]["payload"] == {"decision": "delete"}
    assert "## Ref: runtime.replies" in runner.prompts["worker"]


def test_control_envelope_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="channel_writes"):
        ControlEnvelope.model_validate(
            {
                "kind": "route",
                "labels": ["done"],
                "channel_writes": {"status": "ignored"},
            }
        )


def test_controller_result_fails_fast_on_unknown_control_keys(tmp_path: Path) -> None:
    class InvalidControlRunner(FakeRunner):
        async def run(self, request: NodeExecutionRequest) -> NodeExecutionResult:
            if request.task.id == "gate":
                return self._complete_with_json(
                    request,
                    {
                        "result": {"summary": "ready to publish"},
                        "control": {
                            "kind": "route",
                            "labels": ["done"],
                            "channel_writes": {"status": "ignored"},
                        },
                    },
                )
            return await super().run(request)

    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="gate",
            title="Gate",
            agent="worker",
            kind="controller",
            status=TaskStatus.READY,
            compose=[{"kind": "literal", "text": "emit controller result"}],
            control={
                "mode": "route",
                "routes": [{"label": "done", "targets": ["publish_summary"]}],
            },
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="publish_summary",
            title="Publish Summary",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "gate", "kind": "order", "consume": []}],
            compose=[{"kind": "literal", "text": "publish"}],
            publish=["final.md"],
        )
    )

    run = asyncio.run(
        RunService(store, InvalidControlRunner()).start_run(
            roots=["publish_summary"],
            labels=[],
            user_inputs=None,
        )
    )

    gate = _instance_for_task(run, "gate")

    assert run.status is RunStatus.FAILED
    assert gate.status is RunInstanceStatus.FAILED
    assert gate.error is not None
    assert "emitted invalid control" in gate.error
    assert "channel_writes" in gate.error
    assert _instances_for_task(run, "publish_summary") == []


def test_loop_route_descendants_do_not_reuse_base_scope_branch_instances(tmp_path: Path) -> None:
    class BranchIsolationRunner(FakeRunner):
        async def run(self, request: NodeExecutionRequest) -> NodeExecutionResult:
            if request.task.id == "route_gate":
                route_label = self._read_text(
                    request.attempt_dir / "context" / "refs" / "inputs" / "route_label.txt"
                )
                return self._complete_with_json(
                    request,
                    {
                        "result": {"selected_label": route_label},
                        "control": {"kind": "route", "labels": [route_label]},
                    },
                )
            if request.task.id == "loop_gate":
                remaining_loops = int(
                    self._read_text(
                        request.attempt_dir
                        / "context"
                        / "refs"
                        / "inputs"
                        / "remaining_loops.txt"
                    )
                )
                if remaining_loops > 0:
                    payload = {
                        "result": {"remaining_loops": remaining_loops},
                        "control": {
                            "kind": "loop",
                            "action": "continue",
                            "next_inputs": {
                                "remaining_loops": str(remaining_loops - 1),
                                "route_label": "skip_optional",
                            },
                        },
                    }
                else:
                    payload = {
                        "result": {"remaining_loops": remaining_loops},
                        "control": {"kind": "loop", "action": "stop"},
                    }
                return self._complete_with_json(request, payload)
            return await super().run(request)

    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="route_gate",
            title="Route Gate",
            agent="worker",
            kind="controller",
            status=TaskStatus.READY,
            compose=[{"kind": "ref", "ref": "inputs.route_label"}],
            control={
                "mode": "route",
                "routes": [
                    {"label": "take_optional", "targets": ["optional_step"]},
                    {"label": "skip_optional", "targets": ["skip_step"]},
                ],
            },
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="always_step",
            title="Always Step",
            agent="worker",
            status=TaskStatus.READY,
            compose=[{"kind": "literal", "text": "always run"}],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="optional_step",
            title="Optional Step",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "route_gate", "kind": "order", "consume": []}],
            compose=[{"kind": "literal", "text": "optional branch"}],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="skip_step",
            title="Skip Step",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "route_gate", "kind": "order", "consume": []}],
            compose=[{"kind": "literal", "text": "skip branch"}],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="join_branch",
            title="Join Branch",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[
                {"task": "optional_step", "kind": "order", "consume": []},
                {"task": "always_step", "kind": "order", "consume": []},
            ],
            compose=[{"kind": "literal", "text": "join optional branch"}],
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="loop_gate",
            title="Loop Gate",
            agent="worker",
            kind="controller",
            status=TaskStatus.READY,
            depends_on=[
                {"task": "route_gate", "kind": "order", "consume": []},
                {"task": "always_step", "kind": "order", "consume": []},
            ],
            compose=[{"kind": "ref", "ref": "inputs.remaining_loops"}],
            control={
                "mode": "loop",
                "continue_targets": ["route_gate", "always_step"],
                "stop_targets": ["publish_summary"],
            },
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="publish_summary",
            title="Publish Summary",
            agent="worker",
            status=TaskStatus.READY,
            depends_on=[{"task": "loop_gate", "kind": "order", "consume": []}],
            compose=[{"kind": "literal", "text": "publish summary"}],
            publish=["final.md"],
        )
    )

    run = asyncio.run(
        RunService(store, BranchIsolationRunner()).start_run(
            roots=["publish_summary", "join_branch"],
            labels=[],
            user_inputs={
                "remaining_loops": "1",
                "route_label": "take_optional",
            },
        )
    )

    route_gate_instances = _instances_for_task(run, "route_gate")
    optional_instances = _instances_for_task(run, "optional_step")
    skip_instances = _instances_for_task(run, "skip_step")
    join_instances = _instances_for_task(run, "join_branch")

    assert run.status is RunStatus.DONE
    assert len(route_gate_instances) == 2
    assert len(optional_instances) == 1
    assert len(skip_instances) == 1
    assert len(join_instances) == 1

    base_scope_id = optional_instances[0].input_scope_id
    next_scope_id = next(
        instance.input_scope_id
        for instance in route_gate_instances
        if instance.input_scope_id != base_scope_id
    )

    assert all(instance.input_scope_id != next_scope_id for instance in join_instances)
