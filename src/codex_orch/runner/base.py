from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from codex_orch.domain import (
    NodeExecutionTerminationReason,
    ProjectSpec,
    TaskSpec,
)


@dataclass(frozen=True)
class NodeExecutionRequest:
    run_id: str
    instance_id: str
    attempt_no: int
    program_dir: Path
    project_workspace_dir: Path
    workspace_dir: Path
    extra_writable_roots: tuple[Path, ...]
    instance_dir: Path
    attempt_dir: Path
    resume_session_id: str | None
    project: ProjectSpec
    task: TaskSpec
    prompt: str


@dataclass(frozen=True)
class NodeExecutionResult:
    success: bool
    return_code: int
    final_message: str
    session_id: str | None = None
    error: str | None = None
    termination_reason: NodeExecutionTerminationReason | None = None


class TaskRunner(Protocol):
    async def run(self, request: NodeExecutionRequest) -> NodeExecutionResult:
        ...
