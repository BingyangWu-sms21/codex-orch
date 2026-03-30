from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, Field, model_validator

from codex_orch.domain.assistant import (
    ConfidenceLevel,
    DecisionKind,
    RequestKind,
    RequestPriority,
)
from codex_orch.domain.models import (
    NodeExecutionTerminationReason,
    ProjectSpec,
    PublishedArtifact,
    RunStatus,
    TaskSpec,
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _validate_relative_program_path(raw_path: str) -> str:
    candidate = PurePosixPath(raw_path)
    if candidate.is_absolute():
        raise ValueError("paths must be relative")
    if ".." in candidate.parts:
        raise ValueError("paths must not escape the program directory")
    normalized = str(candidate)
    if normalized == ".":
        raise ValueError("path must point to a file")
    return normalized


class RunInstanceStatus(StrEnum):
    PENDING = "pending"
    RUNNABLE = "runnable"
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunInstanceWaitReason(StrEnum):
    INTERRUPTS_PENDING = "interrupts_pending"


class InterruptAudience(StrEnum):
    ASSISTANT = "assistant"
    HUMAN = "human"


class InterruptStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    APPLIED = "applied"


class InterruptReplyKind(StrEnum):
    ANSWER = "answer"
    HANDOFF_TO_HUMAN = "handoff_to_human"


class RunEvent(BaseModel):
    event_id: str
    run_id: str
    event_type: str
    created_at: str = Field(default_factory=_utc_now_iso)
    instance_id: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_event(self) -> RunEvent:
        if not self.event_id.strip():
            raise ValueError("event_id must not be empty")
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if not self.event_type.strip():
            raise ValueError("event_type must not be empty")
        if self.instance_id is not None and not self.instance_id.strip():
            raise ValueError("instance_id must not be blank")
        return self


class InterruptRequest(BaseModel):
    interrupt_id: str
    run_id: str
    instance_id: str
    task_id: str
    audience: InterruptAudience
    blocking: bool = True
    request_kind: RequestKind
    question: str
    decision_kind: DecisionKind | None = None
    options: list[str] = Field(default_factory=list)
    context_artifacts: list[str] = Field(default_factory=list)
    reply_schema: str | None = None
    priority: RequestPriority = RequestPriority.NORMAL
    metadata: dict[str, object] = Field(default_factory=dict)
    status: InterruptStatus = InterruptStatus.OPEN
    created_at: str = Field(default_factory=_utc_now_iso)
    resolved_at: str | None = None
    applied_at: str | None = None

    @model_validator(mode="after")
    def validate_interrupt(self) -> InterruptRequest:
        if not self.interrupt_id.strip():
            raise ValueError("interrupt_id must not be empty")
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if not self.instance_id.strip():
            raise ValueError("instance_id must not be empty")
        if not self.task_id.strip():
            raise ValueError("task_id must not be empty")
        if not self.question.strip():
            raise ValueError("question must not be empty")
        validated_artifacts = [
            _validate_relative_program_path(path) for path in self.context_artifacts
        ]
        object.__setattr__(self, "context_artifacts", validated_artifacts)
        if self.reply_schema is not None:
            object.__setattr__(
                self,
                "reply_schema",
                _validate_relative_program_path(self.reply_schema),
            )
        return self


class InterruptReply(BaseModel):
    interrupt_id: str
    audience: InterruptAudience
    reply_kind: InterruptReplyKind
    text: str
    payload: dict[str, object] = Field(default_factory=dict)
    rationale: str | None = None
    confidence: ConfidenceLevel | None = None
    citations: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_utc_now_iso)

    @model_validator(mode="after")
    def validate_reply(self) -> InterruptReply:
        if not self.interrupt_id.strip():
            raise ValueError("interrupt_id must not be empty")
        if not self.text.strip():
            raise ValueError("text must not be empty")
        if self.rationale is not None and not self.rationale.strip():
            raise ValueError("rationale must not be blank")
        return self


class RunInstanceState(BaseModel):
    instance_id: str
    task_id: str
    task: TaskSpec
    dependency_instances: dict[str, str] = Field(default_factory=dict)
    status: RunInstanceStatus = RunInstanceStatus.PENDING
    waiting_reason: RunInstanceWaitReason | None = None
    published: list[PublishedArtifact] = Field(default_factory=list)
    error: str | None = None
    attempt: int = 0
    session_id: str | None = None
    blocking_interrupts: list[str] = Field(default_factory=list)
    termination_reason: NodeExecutionTerminationReason | None = None
    started_at: str | None = None
    finished_at: str | None = None

    @model_validator(mode="after")
    def validate_instance(self) -> RunInstanceState:
        if not self.instance_id.strip():
            raise ValueError("instance_id must not be empty")
        if not self.task_id.strip():
            raise ValueError("task_id must not be empty")
        if self.task.id != self.task_id:
            raise ValueError("task.id must match task_id")
        return self


class RunRecord(BaseModel):
    id: str
    roots: list[str]
    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)
    status: RunStatus = RunStatus.PENDING
    user_inputs: dict[str, str] = Field(default_factory=dict)
    project: ProjectSpec
    instances: dict[str, RunInstanceState]

    @model_validator(mode="after")
    def validate_run(self) -> RunRecord:
        if not self.id.strip():
            raise ValueError("id must not be empty")
        if not self.roots:
            raise ValueError("roots must not be empty")
        if not self.instances:
            raise ValueError("instances must not be empty")
        return self
