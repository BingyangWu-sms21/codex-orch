from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from codex_orch.domain import (
    DecisionKind,
    InterruptAudience,
    InterruptReplyKind,
    RequestKind,
    RequestPriority,
    RunInstanceStatus,
    RunStatus,
)
from codex_orch.runner import NodeExecutionRequest, NodeExecutionResult
from codex_orch.scheduler import RunService
from codex_orch.store import ProjectStore
from codex_orch.task_pool import TaskPoolService
from tests.helpers import instance_for_task
from tests.test_run_service import FakeRunner


def _copy_example_program(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "examples" / "quality_convergence_program"
    destination = tmp_path / "quality-convergence-program"
    shutil.copytree(source, destination)
    return destination


class QualityExampleRunner(FakeRunner):
    async def run(self, request: NodeExecutionRequest) -> NodeExecutionResult:
        self.prompts[request.task.id] = request.prompt
        self.requests[request.task.id] = request
        final_path = request.attempt_dir / "final.md"

        if request.task.id == "baseline_run":
            payload = {
                "result": {
                    "feature_summary": "Checkout flow intermittently fails before confirmation.",
                    "suspected_failures": ["browser flow fails after submit"],
                    "evidence_gaps": ["missing stable end-to-end proof"],
                    "attempt_budget": 1,
                    "next_focus": "stabilize the quality evidence",
                }
            }
            return self._complete_with_json(request, payload)

        if request.task.id == "quality_attempt":
            payload = {
                "result": {
                    "attempt_goal": ["add_e2e_test", "stabilize_checkout_flow"],
                    "diagnosis": {
                        "kind": "coverage_gap",
                        "summary": "Existing tests do not yet prove the full browser path.",
                    },
                    "changes": {
                        "implementation_files": ["src/web/checkout.py"],
                        "test_files_added": ["tests/e2e/test_checkout_flow.py"],
                    },
                    "quality_progress": {
                        "new_coverage": ["browser_checkout_submit"],
                        "remaining_gaps": [],
                    },
                    "self_check": {
                        "commands": ["pytest tests/e2e/test_checkout_flow.py"],
                        "passed": True,
                        "evidence_strength": "high",
                    },
                    "handoff": {
                        "assistant_consulted": [],
                        "human_required": False,
                        "reason": None,
                    },
                    "recommendation": {
                        "loop_decision_hint": "stop",
                        "acceptance_readiness": "possibly_ready",
                    },
                }
            }
            return self._complete_with_json(request, payload)

        if request.task.id == "quality_loop_gate":
            payload = {
                "result": {
                    "summary": "Quality evidence looks strong enough for human acceptance.",
                    "stop_reason": "ready_for_acceptance",
                    "remaining_gaps": [],
                },
                "control": {
                    "kind": "loop",
                    "action": "stop",
                },
            }
            return self._complete_with_json(request, payload)

        if (
            request.task.id == "acceptance_gate"
            and request.resume_session_id is None
            and request.instance_id not in self._first_attempt_interrupts
        ):
            self._first_attempt_interrupts.add(request.instance_id)
            self.store.create_interrupt(
                run_id=request.run_id,
                instance_id=request.instance_id,
                audience=InterruptAudience.HUMAN,
                blocking=True,
                request_kind=RequestKind.APPROVAL,
                question="Should we accept the current quality state for this feature?",
                decision_kind=DecisionKind.REVIEW,
                options=["approved", "revise", "expand_scope"],
                context_artifacts=[],
                reply_schema="schemas/acceptance-decision.schema.json",
                priority=RequestPriority.HIGH,
                metadata={
                    "acceptance_packet": "Baseline and latest attempt suggest the checkout browser path is now covered.",
                },
            )
            final_path.write_text("Waiting for final human acceptance.\n", encoding="utf-8")
            return NodeExecutionResult(
                success=True,
                return_code=0,
                final_message="Waiting for final human acceptance.",
                session_id=f"session-{request.instance_id}",
            )

        if request.task.id == "acceptance_gate" and request.resume_session_id is not None:
            latest_reply = json.loads(
                (
                    request.attempt_dir
                    / "context"
                    / "refs"
                    / "runtime"
                    / "latest_reply.json"
                ).read_text(encoding="utf-8")
            )
            decision = latest_reply["reply"]["payload"]["decision"]
            payload = {
                "result": {
                    "human_decision": decision,
                },
                "control": {
                    "kind": "route",
                    "labels": [decision],
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


def test_quality_convergence_example_program_validates() -> None:
    program_dir = Path(__file__).resolve().parents[1] / "examples" / "quality_convergence_program"
    store = ProjectStore(program_dir)

    report = TaskPoolService(store).validate_program()

    assert report.blocking is False
    assert report.errors == ()


def test_quality_convergence_example_acceptance_waits_for_human_and_routes_on_resume(
    tmp_path: Path,
) -> None:
    program_dir = _copy_example_program(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = ProjectStore(program_dir)
    runner = QualityExampleRunner(store)
    service = RunService(store, runner)

    run = asyncio.run(
        service.start_run(
            roots=["publish_summary"],
            labels=[],
            user_inputs={"repo_workspace": str(workspace)},
        )
    )

    assert run.status is RunStatus.WAITING
    acceptance = instance_for_task(run, "acceptance_gate")
    assert acceptance.status is RunInstanceStatus.WAITING

    records = store.list_instance_interrupts(run.id, acceptance.instance_id)
    assert len(records) == 1
    interrupt = records[0].interrupt
    assert interrupt.audience is InterruptAudience.HUMAN
    assert interrupt.reply_schema == "schemas/acceptance-decision.schema.json"

    store.save_interrupt_reply(
        interrupt.interrupt_id,
        audience=InterruptAudience.HUMAN,
        reply_kind=InterruptReplyKind.ANSWER,
        text="Approved. The evidence is sufficient.",
        payload={"decision": "approved", "note": "The checkout path is covered now."},
    )

    resumed = asyncio.run(service.resume_run(run.id))

    assert resumed.status is RunStatus.DONE
    resumed_acceptance = instance_for_task(resumed, "acceptance_gate")
    publish_summary = instance_for_task(resumed, "publish_summary")
    assert resumed_acceptance.status is RunInstanceStatus.DONE
    assert resumed_acceptance.attempt == 2
    assert publish_summary.status is RunInstanceStatus.DONE
    assert [instance for instance in resumed.instances.values() if instance.task_id == "revision_requested"] == []
    assert [instance for instance in resumed.instances.values() if instance.task_id == "scope_review"] == []

    acceptance_result = store.maybe_get_instance_result(resumed.id, resumed_acceptance.instance_id)
    assert acceptance_result == {
        "control": {"kind": "route", "labels": ["approved"]},
        "result": {"human_decision": "approved"},
    }
    latest_reply = json.loads(
        (
            store.get_attempt_dir(resumed.id, resumed_acceptance.instance_id, 2)
            / "context"
            / "refs"
            / "runtime"
            / "latest_reply.json"
        ).read_text(encoding="utf-8")
    )
    assert latest_reply["reply"]["payload"] == {
        "decision": "approved",
        "note": "The checkout path is covered now.",
    }
    assert "## Ref: runtime.latest_reply" in runner.prompts["acceptance_gate"]
    assert store.find_interrupt(interrupt.interrupt_id).interrupt.status.value == "applied"
