from __future__ import annotations

import json
from importlib.metadata import version as package_version
from pathlib import Path

from typer.testing import CliRunner

from codex_orch.cli import app
from codex_orch.domain import TaskSpec, TaskStatus
from codex_orch.scheduler import RunService
from tests.helpers import build_test_store, write_assistant_profile
from tests.test_run_service import FakeRunner


def test_assistant_cli_round_trip(tmp_path: Path) -> None:
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
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
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


def test_assistant_request_create_fails_without_profile(tmp_path: Path) -> None:
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
        ],
        env={"CODEX_ORCH_GLOBAL_ROOT": str(store.global_paths.root)},
    )

    assert request_result.exit_code != 0
    assert "has no effective assistant profile" in request_result.output


def test_cli_version_flag() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == package_version("codex-orch")


def test_skill_export_and_install_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    export_root = tmp_path / "exported-skills"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    list_result = runner.invoke(app, ["skill", "list", "--json"])
    assert list_result.exit_code == 0
    skills = json.loads(list_result.stdout)
    assert any(skill["id"] == "operate-codex-orch" for skill in skills)
    assert all(skill["id"] != "request-assistant" for skill in skills)

    export_result = runner.invoke(
        app,
        [
            "skill",
            "export",
            "operate-codex-orch",
            str(export_root),
            "--json",
        ],
    )
    assert export_result.exit_code == 0
    exported_skill_dir = export_root / "operate-codex-orch"
    assert exported_skill_dir.exists()
    assert (exported_skill_dir / "SKILL.md").exists()
    assert (exported_skill_dir / "references" / "quickstart.md").exists()
    assert (exported_skill_dir / "references" / "operator-runbook.md").exists()
    assert not (exported_skill_dir / "scripts").exists()
    exported_skill_text = (exported_skill_dir / "SKILL.md").read_text(encoding="utf-8")
    exported_quickstart_text = (
        exported_skill_dir / "references" / "quickstart.md"
    ).read_text(encoding="utf-8")
    exported_runbook_text = (
        exported_skill_dir / "references" / "operator-runbook.md"
    ).read_text(encoding="utf-8")
    assert "pipx install codex-orch" in exported_skill_text
    assert "codex-orch --version" in exported_skill_text
    assert "Assume your current working directory is the target codex-orch program" in exported_skill_text
    assert "pipx install codex-orch" in exported_quickstart_text
    assert "codex-orch task list ." in exported_quickstart_text
    assert "codex-orch assistant request list . --json" in exported_runbook_text
    assert "uv run codex-orch" not in exported_skill_text
    assert "uv run codex-orch" not in exported_quickstart_text
    assert "uv run codex-orch" not in exported_runbook_text

    install_result = runner.invoke(
        app,
        [
            "skill",
            "install",
            "operate-codex-orch",
            "--repo-dir",
            str(repo_root),
            "--json",
        ],
    )
    assert install_result.exit_code == 0
    installed_skill_dir = repo_root / ".codex" / "skills" / "operate-codex-orch"
    assert installed_skill_dir.exists()
    assert (installed_skill_dir / "references" / "quickstart.md").exists()
    assert (installed_skill_dir / "references" / "operator-runbook.md").exists()
    assert not (installed_skill_dir / "scripts").exists()
    installed_quickstart_text = (
        installed_skill_dir / "references" / "quickstart.md"
    ).read_text(encoding="utf-8")
    assert "pipx install codex-orch" in installed_quickstart_text
    assert "uv run codex-orch" not in installed_quickstart_text

    legacy_export_result = runner.invoke(
        app,
        [
            "skill",
            "export",
            "request-assistant",
            str(export_root),
            "--json",
        ],
    )
    assert legacy_export_result.exit_code != 0
