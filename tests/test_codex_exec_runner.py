from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from textwrap import dedent

import pytest

from codex_orch.domain import (
    NodeExecutionRuntime,
    NodeExecutionTerminationReason,
    ProjectSpec,
    TaskSpec,
    TaskStatus,
)
from codex_orch.runner import CodexExecRunner, NodeExecutionRequest


def _install_fake_codex(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex_path = bin_dir / "codex"
    codex_path.write_text(
        dedent(
            """\
            #!/usr/bin/env python3
            from __future__ import annotations

            import json
            import os
            import signal
            import sys
            import time

            mode = os.environ["FAKE_CODEX_MODE"]
            if mode == "success":
                print(
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item_1",
                                "type": "agent_message",
                                "text": "hello from fake codex",
                            },
                        }
                    ),
                    flush=True,
                )
                raise SystemExit(0)
            if mode == "echo-stdin":
                prompt = sys.stdin.read()
                print(
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item_0",
                                "type": "agent_message",
                                "text": prompt,
                            },
                        }
                    ),
                    flush=True,
                )
                raise SystemExit(0)
            if mode == "sleep":
                time.sleep(30)
                raise SystemExit(0)
            if mode == "ignore-term":
                print(
                    json.dumps(
                        {
                            "type": "item.started",
                            "item": {
                                "id": "item_2",
                                "type": "command_execution",
                                "command": "/bin/bash -lc 'rg --files | rg smoke'",
                                "status": "in_progress",
                            },
                        }
                    ),
                    flush=True,
                )
                signal.signal(signal.SIGTERM, lambda signum, frame: None)
                while True:
                    time.sleep(1)
            if mode == "long-line":
                print(
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item_3",
                                "type": "command_execution",
                                "command": "/bin/bash -lc 'make dev-minimal'",
                                "status": "completed",
                                "aggregated_output": "x" * 70000,
                            },
                        }
                    ),
                    flush=True,
                )
                print(
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item_4",
                                "type": "agent_message",
                                "text": "hello from fake codex",
                            },
                        }
                    ),
                    flush=True,
                )
                raise SystemExit(0)
            raise SystemExit(2)
            """
        ),
        encoding="utf-8",
    )
    codex_path.chmod(0o755)
    return bin_dir


def _build_request(
    tmp_path: Path,
    *,
    sandbox: str,
    extra_writable_root_names: tuple[str, ...] = (),
    wall_timeout_sec: float | None = 3600.0,
    idle_timeout_sec: float | None = 600.0,
    terminate_grace_sec: float = 10.0,
) -> NodeExecutionRequest:
    program_dir = tmp_path / "program"
    project_workspace_dir = tmp_path / "project-workspace"
    workspace_dir = tmp_path / "workspace"
    node_dir = tmp_path / "node"
    program_dir.mkdir()
    project_workspace_dir.mkdir()
    workspace_dir.mkdir()
    extra_writable_roots: list[Path] = []
    for root_name in extra_writable_root_names:
        root_dir = tmp_path / root_name
        root_dir.mkdir(parents=True)
        extra_writable_roots.append(root_dir)

    project = ProjectSpec(
        name="test-program",
        workspace=str(project_workspace_dir),
        default_agent="default",
        default_sandbox="read-only",
        node_wall_timeout_sec=wall_timeout_sec,
        node_idle_timeout_sec=idle_timeout_sec,
        node_terminate_grace_sec=terminate_grace_sec,
    )
    task = TaskSpec(
        id="discover",
        title="Discover",
        agent="worker",
        status=TaskStatus.READY,
        sandbox=sandbox,
        workspace=str(workspace_dir),
        extra_writable_roots=[str(root) for root in extra_writable_roots],
        publish=["final.md"],
    )
    return NodeExecutionRequest(
        run_id="run-1",
        program_dir=program_dir,
        project_workspace_dir=project_workspace_dir,
        workspace_dir=workspace_dir,
        extra_writable_roots=tuple(extra_writable_roots),
        node_dir=node_dir,
        project=project,
        task=task,
        prompt="hello",
    )


def test_build_command_skips_git_repo_check_for_workspace_write(tmp_path: Path) -> None:
    runner = CodexExecRunner()

    command = runner._build_command(_build_request(tmp_path, sandbox="workspace-write"))

    assert command == [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--cd",
        str(tmp_path / "workspace"),
        "--full-auto",
        "--add-dir",
        str(tmp_path / "node"),
        "-",
    ]


def test_build_command_includes_extra_writable_roots(tmp_path: Path) -> None:
    runner = CodexExecRunner()

    command = runner._build_command(
        _build_request(
            tmp_path,
            sandbox="workspace-write",
            extra_writable_root_names=("shared-cache", "env-source"),
        )
    )

    assert command == [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--cd",
        str(tmp_path / "workspace"),
        "--full-auto",
        "--add-dir",
        str(tmp_path / "node"),
        "--add-dir",
        str(tmp_path / "shared-cache"),
        "--add-dir",
        str(tmp_path / "env-source"),
        "-",
    ]


def test_build_command_omits_add_dir_for_danger_full_access(tmp_path: Path) -> None:
    runner = CodexExecRunner()

    command = runner._build_command(
        _build_request(
            tmp_path,
            sandbox="danger-full-access",
            extra_writable_root_names=("shared-cache",),
        )
    )

    assert command == [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--cd",
        str(tmp_path / "workspace"),
        "--dangerously-bypass-approvals-and-sandbox",
        "-",
    ]


def test_build_command_rejects_extra_writable_roots_for_read_only(tmp_path: Path) -> None:
    runner = CodexExecRunner()

    with pytest.raises(ValueError, match="extra writable roots require a writable sandbox"):
        runner._build_command(
            _build_request(
                tmp_path,
                sandbox="read-only",
                extra_writable_root_names=("shared-cache",),
            )
        )


def test_runner_writes_runtime_for_successful_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CODEX_MODE", "success")

    request = _build_request(tmp_path, sandbox="read-only")
    runner = CodexExecRunner()

    result = asyncio.run(runner.run(request))

    assert result.success
    assert result.termination_reason is NodeExecutionTerminationReason.COMPLETED
    runtime = NodeExecutionRuntime.model_validate(
        json.loads((request.node_dir / "runtime.json").read_text(encoding="utf-8"))
    )
    assert runtime.cwd == str(request.workspace_dir)
    assert runtime.project_workspace_dir == str(request.project_workspace_dir)
    assert runtime.command[-1] == "<prompt omitted; see prompt.md>"
    assert runtime.sandbox == "read-only"
    assert runtime.writable_roots == []
    assert runtime.last_event_summary == "item.completed:agent_message:hello from fake codex"
    assert runtime.termination_reason is NodeExecutionTerminationReason.COMPLETED
    assert (request.node_dir / "final.md").read_text(encoding="utf-8") == "hello from fake codex"


def test_runner_passes_prompt_via_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CODEX_MODE", "echo-stdin")

    request = _build_request(tmp_path, sandbox="read-only")
    runner = CodexExecRunner()

    result = asyncio.run(runner.run(request))

    assert result.success
    assert result.final_message == "hello"


def test_runner_handles_long_stdout_jsonl_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CODEX_MODE", "long-line")

    request = _build_request(tmp_path, sandbox="read-only")
    runner = CodexExecRunner()

    result = asyncio.run(runner.run(request))

    assert result.success
    assert result.termination_reason is NodeExecutionTerminationReason.COMPLETED
    runtime = NodeExecutionRuntime.model_validate(
        json.loads((request.node_dir / "runtime.json").read_text(encoding="utf-8"))
    )
    assert runtime.finished_at is not None
    assert runtime.termination_reason is NodeExecutionTerminationReason.COMPLETED
    assert (request.node_dir / "final.md").read_text(encoding="utf-8") == "hello from fake codex"


def test_runner_writes_writable_roots_for_workspace_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CODEX_MODE", "success")

    request = _build_request(
        tmp_path,
        sandbox="workspace-write",
        extra_writable_root_names=("shared-cache", "env-source"),
    )
    runner = CodexExecRunner()

    result = asyncio.run(runner.run(request))

    assert result.success
    runtime = NodeExecutionRuntime.model_validate(
        json.loads((request.node_dir / "runtime.json").read_text(encoding="utf-8"))
    )
    assert runtime.sandbox == "workspace-write"
    assert runtime.writable_roots == [
        str(request.workspace_dir),
        str(request.node_dir),
        str(tmp_path / "shared-cache"),
        str(tmp_path / "env-source"),
    ]


def test_runner_applies_wall_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CODEX_MODE", "sleep")

    request = _build_request(
        tmp_path,
        sandbox="read-only",
        wall_timeout_sec=0.5,
        idle_timeout_sec=30.0,
        terminate_grace_sec=0.1,
    )
    runner = CodexExecRunner()

    result = asyncio.run(runner.run(request))

    assert not result.success
    assert result.termination_reason is NodeExecutionTerminationReason.WALL_TIMEOUT
    assert result.error == "codex exec exceeded wall timeout"
    runtime = NodeExecutionRuntime.model_validate(
        json.loads((request.node_dir / "runtime.json").read_text(encoding="utf-8"))
    )
    assert runtime.finished_at is not None
    assert runtime.termination_reason is NodeExecutionTerminationReason.WALL_TIMEOUT


def test_runner_applies_idle_timeout_and_forces_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CODEX_MODE", "ignore-term")

    request = _build_request(
        tmp_path,
        sandbox="read-only",
        wall_timeout_sec=30.0,
        idle_timeout_sec=0.5,
        terminate_grace_sec=0.1,
    )
    runner = CodexExecRunner()

    result = asyncio.run(runner.run(request))

    assert not result.success
    assert result.termination_reason is NodeExecutionTerminationReason.IDLE_TIMEOUT
    runtime = NodeExecutionRuntime.model_validate(
        json.loads((request.node_dir / "runtime.json").read_text(encoding="utf-8"))
    )
    assert runtime.finished_at is not None
    assert runtime.last_event_summary is not None
    assert "item.started:command_execution:in_progress" in runtime.last_event_summary
    assert runtime.termination_reason is NodeExecutionTerminationReason.IDLE_TIMEOUT
