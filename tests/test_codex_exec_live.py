from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from codex_orch.domain import ProjectSpec, TaskSpec, TaskStatus
from codex_orch.runner import CodexExecRunner, NodeExecutionRequest
from codex_orch.store import ProjectStore


@pytest.mark.skipif(
    os.environ.get("CODEX_ORCH_LIVE_TEST") != "1",
    reason="set CODEX_ORCH_LIVE_TEST=1 to run the live Codex CLI test",
)
def test_codex_exec_live_round_trip(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True)

    program_dir = tmp_path / "program"
    store = ProjectStore(program_dir, global_root=tmp_path / ".global")
    store.save_project(
        ProjectSpec(
            name="live-test",
            workspace=str(workspace),
            default_agent="default",
            default_sandbox="read-only",
        )
    )

    task = TaskSpec(
        id="hello",
        title="Hello",
        agent="default",
        status=TaskStatus.READY,
        compose=[{"kind": "literal", "text": "Respond with the single word hello."}],
        publish=["final.md"],
    )
    runner = CodexExecRunner()
    result = asyncio.run(
        runner.run(
            NodeExecutionRequest(
                run_id="live-run",
                instance_id="hello-1",
                attempt_no=1,
                program_dir=program_dir,
                project_workspace_dir=workspace,
                workspace_dir=workspace,
                extra_writable_roots=tuple(),
                instance_dir=store.get_instance_dir("live-run", "hello-1"),
                attempt_dir=store.get_attempt_dir("live-run", "hello-1", 1),
                resume_session_id=None,
                project=store.load_project(),
                task=task,
                prompt="Respond with the single word hello.",
            )
        )
    )

    assert result.success
    assert "hello" in result.final_message.lower()
