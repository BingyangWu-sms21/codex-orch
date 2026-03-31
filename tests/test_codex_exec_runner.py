from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from textwrap import dedent

import pytest

from codex_orch.domain import (
    NodeExecutionFailureKind,
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
            session_id = "session-123"
            if mode == "success":
                print(json.dumps({"type": "session.created", "session_id": session_id}), flush=True)
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
                print(json.dumps({"type": "session.created", "session_id": session_id}), flush=True)
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
                print(json.dumps({"type": "session.created", "session_id": session_id}), flush=True)
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
                print(json.dumps({"type": "session.created", "session_id": session_id}), flush=True)
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
            if mode == "auth-fail":
                print(
                    json.dumps(
                        {
                            "type": "error",
                            "message": "unexpected status 401 Unauthorized: Invalid API key",
                        }
                    ),
                    flush=True,
                )
                print(
                    json.dumps(
                        {
                            "type": "turn.failed",
                            "error": {
                                "message": "unexpected status 401 Unauthorized: Invalid API key",
                            },
                        }
                    ),
                    flush=True,
                )
                raise SystemExit(1)
            if mode == "network-fail":
                print(
                    json.dumps(
                        {
                            "type": "error",
                            "message": "Request failed after 3 retries: dns error: failed to lookup address information",
                        }
                    ),
                    flush=True,
                )
                print(
                    json.dumps(
                        {
                            "type": "turn.failed",
                            "error": {
                                "message": "Request failed after 3 retries: dns error: failed to lookup address information",
                            },
                        }
                    ),
                    flush=True,
                )
                raise SystemExit(1)
            if mode == "protocol-fail":
                print(
                    json.dumps(
                        {
                            "type": "error",
                            "message": "stream disconnected before completion: stream closed before response.completed",
                        }
                    ),
                    flush=True,
                )
                print(
                    json.dumps(
                        {
                            "type": "turn.failed",
                            "error": {
                                "message": "stream disconnected before completion: stream closed before response.completed",
                            },
                        }
                    ),
                    flush=True,
                )
                raise SystemExit(1)
            if mode == "schema-fail":
                print(
                    json.dumps(
                        {
                            "type": "error",
                            "message": "Invalid schema for response_format codex_output_schema: invalid_json_schema",
                        }
                    ),
                    flush=True,
                )
                print(
                    json.dumps(
                        {
                            "type": "turn.failed",
                            "error": {
                                "message": "Invalid schema for response_format codex_output_schema: invalid_json_schema",
                            },
                        }
                    ),
                    flush=True,
                )
                raise SystemExit(1)
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
    resume_session_id: str | None = None,
    extra_writable_root_names: tuple[str, ...] = (),
    wall_timeout_sec: float | None = 3600.0,
    idle_timeout_sec: float | None = 600.0,
    terminate_grace_sec: float = 10.0,
) -> NodeExecutionRequest:
    program_dir = tmp_path / "program"
    project_workspace_dir = tmp_path / "project-workspace"
    workspace_dir = tmp_path / "workspace"
    instance_dir = tmp_path / "instance"
    attempt_dir = instance_dir / "attempts" / "0001"
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
        instance_id="discover-1",
        attempt_no=1,
        program_dir=program_dir,
        project_workspace_dir=project_workspace_dir,
        workspace_dir=workspace_dir,
        extra_writable_roots=tuple(extra_writable_roots),
        instance_dir=instance_dir,
        attempt_dir=attempt_dir,
        resume_session_id=resume_session_id,
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
        str(tmp_path / "instance" / "attempts" / "0001"),
        "-",
    ]


def test_build_command_uses_resume_subcommand_for_followup_attempts(tmp_path: Path) -> None:
    runner = CodexExecRunner()

    command = runner._build_command(
        _build_request(
            tmp_path,
            sandbox="workspace-write",
            resume_session_id="session-123",
        )
    )

    assert command == [
        "codex",
        "exec",
        "resume",
        "session-123",
        "--json",
        "--skip-git-repo-check",
        "--full-auto",
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


def test_runner_writes_runtime_and_session_for_successful_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CODEX_MODE", "success")

    runner = CodexExecRunner()
    request = _build_request(tmp_path, sandbox="workspace-write")
    result = asyncio.run(runner.run(request))

    assert result.success
    assert result.session_id == "session-123"
    assert (request.attempt_dir / "prompt.md").read_text(encoding="utf-8") == "hello"
    assert (request.attempt_dir / "final.md").read_text(encoding="utf-8") == "hello from fake codex"
    runtime = NodeExecutionRuntime.model_validate(
        json.loads((request.attempt_dir / "runtime.json").read_text(encoding="utf-8"))
    )
    assert runtime.cwd == str(request.workspace_dir)
    assert runtime.termination_reason is NodeExecutionTerminationReason.COMPLETED
    session_payload = json.loads((request.instance_dir / "session.json").read_text(encoding="utf-8"))
    assert session_payload["session_id"] == "session-123"


def test_runner_passes_prompt_via_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CODEX_MODE", "echo-stdin")

    runner = CodexExecRunner()
    request = _build_request(tmp_path, sandbox="workspace-write")
    result = asyncio.run(runner.run(request))

    assert result.success
    assert result.final_message == "hello"


def test_runner_handles_long_stdout_jsonl_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CODEX_MODE", "long-line")

    runner = CodexExecRunner()
    request = _build_request(tmp_path, sandbox="workspace-write")
    result = asyncio.run(runner.run(request))

    assert result.success
    runtime = NodeExecutionRuntime.model_validate(
        json.loads((request.attempt_dir / "runtime.json").read_text(encoding="utf-8"))
    )
    assert runtime.stdout_line_count == 3
    assert runtime.last_event_summary == "item.completed:agent_message:hello from fake codex"


def test_runner_applies_wall_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CODEX_MODE", "sleep")

    runner = CodexExecRunner()
    request = _build_request(
        tmp_path,
        sandbox="workspace-write",
        wall_timeout_sec=0.1,
        idle_timeout_sec=None,
    )
    result = asyncio.run(runner.run(request))

    assert not result.success
    assert result.termination_reason is NodeExecutionTerminationReason.WALL_TIMEOUT


def test_runner_applies_idle_timeout_and_forces_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CODEX_MODE", "ignore-term")

    runner = CodexExecRunner()
    request = _build_request(
        tmp_path,
        sandbox="workspace-write",
        wall_timeout_sec=None,
        idle_timeout_sec=0.1,
        terminate_grace_sec=0.1,
    )
    result = asyncio.run(runner.run(request))

    assert not result.success
    assert result.termination_reason is NodeExecutionTerminationReason.IDLE_TIMEOUT


@pytest.mark.parametrize(
    ("mode", "failure_kind", "resume_recommended"),
    [
        ("auth-fail", NodeExecutionFailureKind.EXTERNAL_AUTH, True),
        ("network-fail", NodeExecutionFailureKind.EXTERNAL_NETWORK, True),
        ("protocol-fail", NodeExecutionFailureKind.EXTERNAL_PROTOCOL, True),
        ("schema-fail", NodeExecutionFailureKind.OUTPUT_SCHEMA, False),
    ],
)
def test_runner_classifies_codex_backend_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    failure_kind: NodeExecutionFailureKind,
    resume_recommended: bool,
) -> None:
    bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)

    runner = CodexExecRunner()
    request = _build_request(tmp_path, sandbox="workspace-write")
    result = asyncio.run(runner.run(request))

    assert not result.success
    assert result.failure_kind is failure_kind
    assert result.resume_recommended is resume_recommended
    runtime = NodeExecutionRuntime.model_validate(
        json.loads((request.attempt_dir / "runtime.json").read_text(encoding="utf-8"))
    )
    assert runtime.failure_kind is failure_kind
    assert runtime.resume_recommended is resume_recommended
    assert runtime.failure_summary is not None


def test_runner_marks_missing_codex_binary_as_runner_invocation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    runner = CodexExecRunner()
    request = _build_request(tmp_path, sandbox="workspace-write")
    result = asyncio.run(runner.run(request))

    assert not result.success
    assert result.failure_kind is NodeExecutionFailureKind.RUNNER_INVOCATION
    assert result.resume_recommended is False
