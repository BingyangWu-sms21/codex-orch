from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from codex_orch.cli import app
from codex_orch.domain import TaskSpec, TaskStatus
from codex_orch.scheduler import RunService
from tests.helpers import build_test_store
from tests.test_run_service import FakeRunner


def test_assistant_cli_round_trip(tmp_path: Path) -> None:
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
    snapshot = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )
    runner = CliRunner()

    request_result = runner.invoke(
        app,
        [
            "assistant",
            "request",
            "create",
            "--program-dir",
            str(store.paths.root),
            "--run-id",
            snapshot.id,
            "--task-id",
            "worker",
            "--kind",
            "clarification",
            "--decision-kind",
            "policy",
            "--question",
            "Can I remove the wrapper?",
            "--option",
            "delete",
            "--option",
            "keep_wrapper",
            "--json",
        ],
    )
    assert request_result.exit_code == 0
    assert "\"request_id\"" in request_result.stdout
    request = store.list_assistant_requests()[0].request

    respond_result = runner.invoke(
        app,
        [
            "assistant",
            "respond",
            str(store.paths.root),
            request.request_id,
            "--resolution-kind",
            "auto_reply",
            "--answer",
            "Delete it.",
            "--rationale",
            "No compatibility support is needed.",
            "--confidence",
            "high",
        ],
    )
    assert respond_result.exit_code == 0

    action_result = runner.invoke(
        app,
        [
            "assistant",
            "action",
            "create",
            str(store.paths.root),
            request.request_id,
            "--action-kind",
            "append_guidance_proposal",
            "--requested-by",
            "assistant",
            "--target-kind",
            "user_guidance",
            "--target-path",
            "~/.codex/AGENTS.md",
            "--reason",
            "Promote a repeated decision to long-term guidance.",
            "--approval-mode",
            "manual_required",
        ],
    )
    assert action_result.exit_code == 0

    status_result = runner.invoke(
        app,
        [
            "assistant",
            "action",
            "status",
            str(store.paths.root),
            request.request_id,
            "--status",
            "approved",
        ],
    )
    assert status_result.exit_code == 0

    record = store.find_assistant_request(request.request_id)
    assert record.response is not None
    assert record.response.answer == "Delete it."
    assert record.control_action is not None
    assert record.control_action.status.value == "approved"


def test_skill_export_and_install_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    export_root = tmp_path / "exported-skills"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    export_result = runner.invoke(
        app,
        [
            "skill",
            "export",
            "request-assistant",
            str(export_root),
            "--json",
        ],
    )
    assert export_result.exit_code == 0
    exported_skill_dir = export_root / "request-assistant"
    assert exported_skill_dir.exists()
    assert (exported_skill_dir / "SKILL.md").exists()
    assert (
        exported_skill_dir / "scripts" / "request_assistant.sh"
    ).read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")

    install_result = runner.invoke(
        app,
        [
            "skill",
            "install",
            "request-assistant",
            "--repo-dir",
            str(repo_root),
            "--json",
        ],
    )
    assert install_result.exit_code == 0
    installed_skill_dir = repo_root / ".codex" / "skills" / "request-assistant"
    assert installed_skill_dir.exists()
    assert (installed_skill_dir / "references" / "protocol.md").exists()
