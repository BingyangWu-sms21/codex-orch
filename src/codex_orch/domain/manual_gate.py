from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, Field, model_validator


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


class ManualGateReason(StrEnum):
    HANDOFF_TO_HUMAN = "handoff_to_human"
    CONTROL_ACTION = "control_action"


class ManualGateStatus(StrEnum):
    WAITING_FOR_HUMAN = "waiting_for_human"
    ANSWERED = "answered"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"


class HumanRequest(BaseModel):
    gate_id: str
    request_id: str
    run_id: str
    requester_task_id: str
    question: str
    assistant_summary: str
    assistant_rationale: str
    citations: list[str] = Field(default_factory=list)
    context_artifacts: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_utc_now_iso)

    @model_validator(mode="after")
    def validate_human_request(self) -> HumanRequest:
        if not self.gate_id.strip():
            raise ValueError("gate_id must not be empty")
        if not self.request_id.strip():
            raise ValueError("request_id must not be empty")
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if not self.requester_task_id.strip():
            raise ValueError("requester_task_id must not be empty")
        if not self.question.strip():
            raise ValueError("question must not be empty")
        if not self.assistant_summary.strip():
            raise ValueError("assistant_summary must not be empty")
        if not self.assistant_rationale.strip():
            raise ValueError("assistant_rationale must not be empty")
        validated_artifacts = [
            _validate_relative_program_path(path) for path in self.context_artifacts
        ]
        object.__setattr__(self, "context_artifacts", validated_artifacts)
        return self


class HumanResponse(BaseModel):
    gate_id: str
    request_id: str
    answer: str
    created_at: str = Field(default_factory=_utc_now_iso)

    @model_validator(mode="after")
    def validate_human_response(self) -> HumanResponse:
        if not self.gate_id.strip():
            raise ValueError("gate_id must not be empty")
        if not self.request_id.strip():
            raise ValueError("request_id must not be empty")
        if not self.answer.strip():
            raise ValueError("answer must not be empty")
        return self


class ManualGate(BaseModel):
    gate_id: str
    request_id: str
    run_id: str
    requester_task_id: str
    reason: ManualGateReason
    status: ManualGateStatus = ManualGateStatus.WAITING_FOR_HUMAN
    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)
    resolved_at: str | None = None

    @model_validator(mode="after")
    def validate_manual_gate(self) -> ManualGate:
        if not self.gate_id.strip():
            raise ValueError("gate_id must not be empty")
        if not self.request_id.strip():
            raise ValueError("request_id must not be empty")
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if not self.requester_task_id.strip():
            raise ValueError("requester_task_id must not be empty")
        return self
