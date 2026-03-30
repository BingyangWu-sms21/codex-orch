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
from tests.helpers import build_test_store, write_assistant_profile
from tests.test_run_service import FakeRunner


def test_interrupt_cli_round_trip(tmp_path) -> None:
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
    assert record["reply"] is None


def test_inbox_reply_cli_round_trip(tmp_path) -> None:
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
    run = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )
    instance = next(iter(run.instances.values()))
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
        reply_schema=None,
        priority=RequestPriority.HIGH,
        metadata={},
    )
    runner = CliRunner()

    reply_result = runner.invoke(
        app,
        [
            "inbox",
            "reply",
            str(store.paths.root),
            interrupt.interrupt_id,
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
    record = store.find_interrupt(interrupt.interrupt_id)
    assert record.reply is not None
    assert record.reply.text == "Delete it."


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
