from __future__ import annotations

import json

from typer.testing import CliRunner

from codex_orch.cli import app
from codex_orch.domain import (
    AssistantUpdateKind,
    AssistantUpdateStatus,
    InterruptAudience,
    InterruptReplyKind,
    DecisionKind,
    ProjectSpec,
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


def _write_demo_proposal(store, *, run_id: str, instance_id: str) -> None:
    proposal_path = store.get_proposals_dir(run_id) / "prop-demo.json"
    proposal_path.write_text(
        json.dumps(
            {
                "proposal_id": "prop-demo",
                "run_id": run_id,
                "instance_id": instance_id,
                "interrupt_id": "int-demo",
                "source_role_id": "policy",
                "requester_task_id": "worker",
                "proposal": {
                    "kind": "routing_policy_update",
                    "summary": "Prefer policy",
                    "rationale": "This task often asks policy questions.",
                    "suggested_content_mode": "snippet",
                    "suggested_content": "preferred_roles:\\n  - policy\\n",
                    "target": {
                        "task_id": "worker",
                        "routing_section": "assistant_hints",
                    },
                },
                "target_file_path": str(store.paths.tasks_dir / "worker.yaml"),
                "status": "proposed",
                "created_at": "2026-03-30T00:00:00+00:00",
                "status_updated_at": "2026-03-30T00:00:00+00:00",
                "status_note": None,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


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


def test_run_start_cli_accepts_json_input_override(tmp_path) -> None:
    store = build_test_store(tmp_path)
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            compose=[{"kind": "ref", "ref": "inputs.config"}],
            publish=["final.md"],
        )
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "run",
            "start",
            str(store.paths.root),
            "--root",
            "worker",
            "--input-json",
            'config={"limit":2,"enabled":true}',
            "--no-wait",
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )

    assert result.exit_code == 0
    run = store.get_run(result.stdout.strip())
    assert run.user_inputs["config"] == {"limit": 2, "enabled": True}


def test_project_init_scaffolds_assistant_operating_model(tmp_path) -> None:
    program_dir = tmp_path / "program"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "project",
            "init",
            str(program_dir),
            "demo",
            str(workspace),
        ],
    )

    assert result.exit_code == 0
    assert (program_dir / "assistant_roles" / "_shared" / "operating-model.md").exists()


def test_project_init_preserves_existing_assistant_operating_model(tmp_path) -> None:
    program_dir = tmp_path / "program"
    workspace = tmp_path / "workspace"
    operating_model_path = program_dir / "assistant_roles" / "_shared" / "operating-model.md"
    workspace.mkdir()
    operating_model_path.parent.mkdir(parents=True, exist_ok=True)
    operating_model_path.write_text("custom operating model\n", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "project",
            "init",
            str(program_dir),
            "demo",
            str(workspace),
        ],
    )

    assert result.exit_code == 0
    assert operating_model_path.read_text(encoding="utf-8") == "custom operating model\n"


def test_project_validate_cli_rejects_incompatible_output_schema(tmp_path) -> None:
    store = build_test_store(tmp_path)
    schema_dir = store.paths.root / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "bad-result.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["summary"],
                "properties": {
                    "summary": {"type": "string"},
                    "details": {"type": "string"},
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            result_schema="schemas/bad-result.schema.json",
            publish=["final.md", "result.json"],
        )
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["project", "validate", str(store.paths.root), "--json"],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["blocking"] is False
    assert payload["warnings"]
    assert any("missing details" in issue["message"] for issue in payload["warnings"])


def test_project_validate_cli_rejects_path_bound_input_with_whitespace(tmp_path) -> None:
    store = build_test_store(tmp_path)
    store.save_project(
        ProjectSpec(
            name="test-program",
            workspace="${inputs.brief}",
            default_agent="default",
            default_sandbox="read-only",
            user_inputs={"brief": "inputs/brief.md"},
            max_concurrency=2,
        )
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["project", "validate", str(store.paths.root), "--json"],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["blocking"] is True
    assert any(
        "leading or trailing whitespace" in issue["message"]
        for issue in payload["errors"]
    )


def test_run_start_cli_allows_output_schema_warnings(tmp_path) -> None:
    store = build_test_store(tmp_path)
    schema_dir = store.paths.root / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "warn-result.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["summary"],
                "properties": {
                    "summary": {"type": "string"},
                    "details": {"type": "string"},
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            result_schema="schemas/warn-result.schema.json",
            publish=["final.md", "result.json"],
        )
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "run",
            "start",
            str(store.paths.root),
            "--root",
            "worker",
            "--no-wait",
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )

    assert result.exit_code == 0
    assert result.stdout.strip()
    assert "missing details" in (result.stdout + getattr(result, "stderr", ""))


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


def test_inbox_reply_cli_accepts_payload_and_validates_reply_schema(tmp_path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store)
    schema_dir = store.paths.root / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "reply.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["decision"],
                "additionalProperties": False,
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": ["delete", "keep_wrapper"],
                    }
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
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
    interrupt = store.create_interrupt(
        run_id=run.id,
        instance_id=instance.instance_id,
        audience=InterruptAudience.HUMAN,
        blocking=True,
        request_kind=RequestKind.CLARIFICATION,
        question="Should I delete the wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["delete", "keep_wrapper"],
        context_artifacts=[],
        reply_schema="schemas/reply.json",
        priority=RequestPriority.NORMAL,
        metadata={},
    )
    runner = CliRunner()

    bad_result = runner.invoke(
        app,
        [
            "inbox",
            "reply",
            str(store.paths.root),
            interrupt.interrupt_id,
            "--text",
            "Delete it.",
            "--payload-json",
            "{}",
            "--json",
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )
    assert bad_result.exit_code != 0

    reply_result = runner.invoke(
        app,
        [
            "inbox",
            "reply",
            str(store.paths.root),
            interrupt.interrupt_id,
            "--text",
            "Delete it.",
            "--payload-json",
            '{"decision":"delete"}',
            "--json",
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )

    assert reply_result.exit_code == 0
    payload = json.loads(reply_result.stdout)
    assert payload["payload"] == {"decision": "delete"}


def test_assistant_doc_install_cli_writes_program_copy(tmp_path) -> None:
    store = build_test_store(tmp_path)
    store.get_assistant_operating_model_path().unlink()
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "assistant-doc",
            "install",
            str(store.paths.root),
            "--json",
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["installed_path"].endswith("assistant_roles/_shared/operating-model.md")
    assert store.get_assistant_operating_model_path().exists()


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


def test_proposal_cli_lists_and_marks_recorded_proposals(tmp_path) -> None:
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
    _write_demo_proposal(
        store,
        run_id=run.id,
        instance_id=next(iter(run.instances.values())).instance_id,
    )
    runner = CliRunner()

    list_result = runner.invoke(
        app,
        [
            "proposal",
            "list",
            str(store.paths.root),
            "--run-id",
            run.id,
            "--kind",
            AssistantUpdateKind.ROUTING_POLICY_UPDATE.value,
            "--json",
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )
    assert list_result.exit_code == 0
    listed = json.loads(list_result.stdout)
    assert listed[0]["proposal_id"] == "prop-demo"

    mark_result = runner.invoke(
        app,
        [
            "proposal",
            "mark",
            str(store.paths.root),
            "prop-demo",
            "--status",
            AssistantUpdateStatus.APPLIED.value,
            "--note",
            "updated manually",
            "--json",
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )
    assert mark_result.exit_code == 0
    updated = json.loads(mark_result.stdout)
    assert updated["status"] == "applied"
    assert updated["status_note"] == "updated manually"


def test_proposal_mark_cli_rejects_blank_note_without_corrupting_record(tmp_path) -> None:
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
    _write_demo_proposal(
        store,
        run_id=run.id,
        instance_id=next(iter(run.instances.values())).instance_id,
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "proposal",
            "mark",
            str(store.paths.root),
            "prop-demo",
            "--status",
            AssistantUpdateStatus.APPLIED.value,
            "--note",
            "   ",
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )

    assert result.exit_code != 0
    assert "status_note must not be blank" in (result.stdout + getattr(result, "stderr", ""))
    proposal = store.find_proposal("prop-demo")
    assert proposal.status is AssistantUpdateStatus.PROPOSED
    assert proposal.status_note is None


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
