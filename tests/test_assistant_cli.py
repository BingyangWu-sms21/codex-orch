from __future__ import annotations

import json

from typer.testing import CliRunner

from codex_orch.cli import app
from codex_orch.domain import (
    InterruptAudience,
    InterruptReplyKind,
    DecisionKind,
    RequestKind,
    RequestPriority,
    TaskSpec,
    TaskStatus,
)
from codex_orch.scheduler import RunService
from tests.helpers import build_test_store, write_assistant_role
from tests.test_run_service import FakeRunner


def _instance_for_task(run, task_id: str):
    matches = [instance for instance in run.instances.values() if instance.task_id == task_id]
    assert len(matches) == 1
    return matches[0]


def test_interrupt_cli_round_trip(tmp_path) -> None:
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
    instance = next(iter(run.instances.values()))
    runner = CliRunner()

    create_result = runner.invoke(
        app,
        [
            "interrupt",
            "create",
            "--program-dir",
            str(store.paths.root),
            "--run-id",
            run.id,
            "--instance-id",
            instance.instance_id,
            "--task-id",
            "worker",
            "--audience",
            "assistant",
            "--kind",
            "clarification",
            "--decision-kind",
            "policy",
            "--question",
            "Can I delete the wrapper?",
            "--option",
            "delete",
            "--option",
            "keep_wrapper",
            "--json",
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )
    assert create_result.exit_code == 0
    payload = json.loads(create_result.stdout)
    interrupt_id = payload["interrupt_id"]

    show_result = runner.invoke(
        app,
        [
            "interrupt",
            "show",
            str(store.paths.root),
            interrupt_id,
            "--json",
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )
    assert show_result.exit_code == 0
    record = json.loads(show_result.stdout)
    assert record["interrupt"]["audience"] == "assistant"
    assert record["interrupt"]["resolved_target_role_id"] == "policy"
    assert record["reply"] is None


def test_interrupt_recommend_cli_returns_role_and_policy(tmp_path) -> None:
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
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "interrupt",
            "recommend",
            "--program-dir",
            str(store.paths.root),
            "--run-id",
            run.id,
            "--task-id",
            "worker",
            "--audience",
            "assistant",
            "--kind",
            "clarification",
            "--decision-kind",
            "policy",
            "--json",
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["recommended_target_role_id"] == "policy"
    assert payload["allow_human"] is True
    assert payload["allowed_assistant_roles"] == ["policy"]


def test_inbox_reply_cli_round_trip(tmp_path) -> None:
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
    instance = next(iter(run.instances.values()))
    runner = CliRunner()
    create_result = runner.invoke(
        app,
        [
            "interrupt",
            "create",
            "--program-dir",
            str(store.paths.root),
            "--run-id",
            run.id,
            "--instance-id",
            instance.instance_id,
            "--task-id",
            "worker",
            "--audience",
            "assistant",
            "--kind",
            "clarification",
            "--decision-kind",
            "policy",
            "--question",
            "Can I delete the wrapper?",
            "--json",
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )
    assert create_result.exit_code == 0
    interrupt = json.loads(create_result.stdout)

    reply_result = runner.invoke(
        app,
        [
            "inbox",
            "reply",
            str(store.paths.root),
            interrupt["interrupt_id"],
            "--text",
            "Delete it.",
            "--reply-kind",
            "answer",
            "--json",
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )

    assert reply_result.exit_code == 0
    payload = json.loads(reply_result.stdout)
    assert payload["reply_kind"] == InterruptReplyKind.ANSWER.value
    record = store.find_interrupt(interrupt["interrupt_id"])
    assert record.reply is not None
    assert record.reply.text == "Delete it."


def test_interrupt_create_cli_requires_target_when_no_recommendation(tmp_path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(
        store,
        role_id="review",
        request_kinds=["approval"],
        decision_kinds=["review"],
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
    instance = next(iter(run.instances.values()))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "interrupt",
            "create",
            "--program-dir",
            str(store.paths.root),
            "--run-id",
            run.id,
            "--instance-id",
            instance.instance_id,
            "--task-id",
            "worker",
            "--audience",
            "assistant",
            "--kind",
            "clarification",
            "--decision-kind",
            "policy",
            "--question",
            "Can I delete the wrapper?",
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )

    assert result.exit_code != 0
    assert "no assistant role recommendation is available" in (
        result.stdout + getattr(result, "stderr", "")
    )


def test_interrupt_create_cli_rejects_task_instance_mismatch_before_persisting(tmp_path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="planner",
            title="Planner",
            agent="default",
            status=TaskStatus.READY,
            interaction_policy={"allow_human": True},
            publish=["final.md"],
        )
    )
    store.save_task(
        TaskSpec(
            id="executor",
            title="Executor",
            agent="default",
            status=TaskStatus.READY,
            interaction_policy={"allow_human": False},
            publish=["final.md"],
        )
    )
    run = RunService(store, FakeRunner()).create_snapshot(
        roots=["planner", "executor"],
        labels=[],
        user_inputs=None,
    )
    executor = _instance_for_task(run, "executor")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "interrupt",
            "create",
            "--program-dir",
            str(store.paths.root),
            "--run-id",
            run.id,
            "--instance-id",
            executor.instance_id,
            "--task-id",
            "planner",
            "--audience",
            "human",
            "--kind",
            "question",
            "--decision-kind",
            "policy",
            "--question",
            "Need a human decision?",
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )

    assert result.exit_code != 0
    assert "task id planner does not match instance task executor" in (
        result.stdout + getattr(result, "stderr", "")
    )
    assert store.list_interrupts(run_id=run.id) == []


def test_run_show_cli_uses_instance_shape(tmp_path) -> None:
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
    run = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["run", "show", str(store.paths.root), run.id, "--json"],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "instances" in payload
    assert "project" in payload
