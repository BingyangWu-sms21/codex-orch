from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, Field, model_validator

from codex_orch.input_values import ensure_json_object


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


class RequestKind(StrEnum):
    QUESTION = "question"
    CLARIFICATION = "clarification"
    APPROVAL = "approval"
    CONTROL_REQUEST = "control_request"


class DecisionKind(StrEnum):
    POLICY = "policy"
    SCOPE = "scope"
    NAMING = "naming"
    SEQUENCING = "sequencing"
    RECOVERY = "recovery"
    REVIEW = "review"


class RequestPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class AssistantBackendKind(StrEnum):
    CODEX_CLI = "codex_cli"


class ResolutionKind(StrEnum):
    AUTO_REPLY = "auto_reply"
    HANDOFF_TO_HUMAN = "handoff_to_human"


class ConfidenceLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AssistantUpdateKind(StrEnum):
    INSTRUCTION_UPDATE = "instruction_update"
    MANAGED_ASSET_UPDATE = "managed_asset_update"
    ROUTING_POLICY_UPDATE = "routing_policy_update"
    PROGRAM_ASSET_UPDATE = "program_asset_update"


class AssistantUpdateContentMode(StrEnum):
    SNIPPET = "snippet"
    FULL_REPLACEMENT = "full_replacement"


class RoutingPolicySection(StrEnum):
    ASSISTANT_HINTS = "assistant_hints"
    INTERACTION_POLICY = "interaction_policy"


class AssistantUpdateStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    APPLIED = "applied"


class ControlActor(StrEnum):
    ASSISTANT = "assistant"
    HUMAN = "human"
    SYSTEM = "system"


class ControlActionKind(StrEnum):
    PAUSE_RUN = "pause_run"
    RESUME_RUN = "resume_run"
    RERUN_TASK = "rerun_task"
    CREATE_TASK = "create_task"
    UPDATE_TASK = "update_task"
    ARCHIVE_TASK = "archive_task"
    APPEND_GUIDANCE_PROPOSAL = "append_guidance_proposal"


class ApprovalMode(StrEnum):
    AUTO = "auto"
    MANUAL_REQUIRED = "manual_required"


class ControlActionStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class AssistantRolePolicy(BaseModel):
    request_kinds: list[RequestKind] = Field(default_factory=list)
    decision_kinds: list[DecisionKind] = Field(default_factory=list)
    task_labels_any: list[str] = Field(default_factory=list)
    ask_when: list[str] = Field(default_factory=list)


class AssistantRoleSpec(BaseModel):
    id: str
    title: str = ""
    description: str = ""
    backend: AssistantBackendKind = AssistantBackendKind.CODEX_CLI
    model: str | None = None
    sandbox: str = "workspace-write"
    instructions: str = "instructions.md"
    managed_assets: list[str] = Field(default_factory=list)
    policy: AssistantRolePolicy = Field(default_factory=AssistantRolePolicy)

    @model_validator(mode="after")
    def validate_role(self) -> AssistantRoleSpec:
        if not self.id.strip():
            raise ValueError("id must not be empty")
        if not self.sandbox.strip():
            raise ValueError("sandbox must not be empty")
        if not self.instructions.strip():
            raise ValueError("instructions must not be empty")
        object.__setattr__(
            self,
            "instructions",
            _validate_relative_program_path(self.instructions),
        )
        object.__setattr__(
            self,
            "managed_assets",
            [
                _validate_relative_program_path(path)
                for path in self.managed_assets
            ],
        )
        return self


class AssistantRequest(BaseModel):
    request_id: str
    run_id: str
    requester_task_id: str
    request_kind: RequestKind
    question: str
    decision_kind: DecisionKind
    options: list[str] = Field(default_factory=list)
    context_artifacts: list[str] = Field(default_factory=list)
    requested_control_actions: list[ControlActionKind] = Field(default_factory=list)
    priority: RequestPriority = RequestPriority.NORMAL
    created_at: str = Field(default_factory=_utc_now_iso)

    @model_validator(mode="after")
    def validate_request(self) -> AssistantRequest:
        if not self.request_id.strip():
            raise ValueError("request_id must not be empty")
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if not self.requester_task_id.strip():
            raise ValueError("requester_task_id must not be empty")
        if not self.question.strip():
            raise ValueError("question must not be empty")
        validated_artifacts = [
            _validate_relative_program_path(path) for path in self.context_artifacts
        ]
        object.__setattr__(self, "context_artifacts", validated_artifacts)
        return self


class AssistantUpdateTarget(BaseModel):
    role_id: str | None = None
    managed_asset_path: str | None = None
    task_id: str | None = None
    routing_section: RoutingPolicySection | None = None

    @model_validator(mode="after")
    def validate_target(self) -> AssistantUpdateTarget:
        if self.role_id is not None and not self.role_id.strip():
            raise ValueError("role_id must not be blank")
        if self.managed_asset_path is not None:
            object.__setattr__(
                self,
                "managed_asset_path",
                _validate_relative_program_path(self.managed_asset_path),
            )
        if self.task_id is not None and not self.task_id.strip():
            raise ValueError("task_id must not be blank")
        return self


class AssistantUpdateProposal(BaseModel):
    kind: AssistantUpdateKind
    summary: str
    rationale: str
    suggested_content_mode: AssistantUpdateContentMode
    suggested_content: str
    target: AssistantUpdateTarget

    @model_validator(mode="after")
    def validate_proposal(self) -> AssistantUpdateProposal:
        if not self.summary.strip():
            raise ValueError("summary must not be empty")
        if not self.rationale.strip():
            raise ValueError("rationale must not be empty")
        if not self.suggested_content.strip():
            raise ValueError("suggested_content must not be empty")
        if self.kind is AssistantUpdateKind.INSTRUCTION_UPDATE:
            if self.target.role_id is None:
                raise ValueError("instruction_update requires target.role_id")
            if any(
                value is not None
                for value in (
                    self.target.managed_asset_path,
                    self.target.task_id,
                    self.target.routing_section,
                )
            ):
                raise ValueError(
                    "instruction_update target may only set role_id"
                )
        elif self.kind is AssistantUpdateKind.MANAGED_ASSET_UPDATE:
            if self.target.role_id is None or self.target.managed_asset_path is None:
                raise ValueError(
                    "managed_asset_update requires target.role_id and target.managed_asset_path"
                )
            if any(
                value is not None
                for value in (
                    self.target.task_id,
                    self.target.routing_section,
                )
            ):
                raise ValueError(
                    "managed_asset_update target may only set role_id and managed_asset_path"
                )
        elif self.kind is AssistantUpdateKind.PROGRAM_ASSET_UPDATE:
            if self.target.managed_asset_path is None:
                raise ValueError(
                    "program_asset_update requires target.managed_asset_path"
                )
            if any(
                value is not None
                for value in (
                    self.target.role_id,
                    self.target.task_id,
                    self.target.routing_section,
                )
            ):
                raise ValueError(
                    "program_asset_update target may only set managed_asset_path"
                )
        else:
            if self.target.task_id is None or self.target.routing_section is None:
                raise ValueError(
                    "routing_policy_update requires target.task_id and target.routing_section"
                )
            if any(
                value is not None
                for value in (
                    self.target.role_id,
                    self.target.managed_asset_path,
                )
            ):
                raise ValueError(
                    "routing_policy_update target may only set task_id and routing_section"
                )
        return self


class AssistantResponse(BaseModel):
    request_id: str
    resolution_kind: ResolutionKind
    answer: str
    rationale: str
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM
    citations: list[str] = Field(default_factory=list)
    payload: dict[str, object] = Field(default_factory=dict)
    proposed_updates: list[AssistantUpdateProposal] = Field(default_factory=list)
    created_at: str = Field(default_factory=_utc_now_iso)

    @model_validator(mode="after")
    def validate_response(self) -> AssistantResponse:
        if not self.request_id.strip():
            raise ValueError("request_id must not be empty")
        if not self.answer.strip():
            raise ValueError("answer must not be empty")
        if not self.rationale.strip():
            raise ValueError("rationale must not be empty")
        object.__setattr__(
            self,
            "payload",
            ensure_json_object(self.payload, field_name="payload"),
        )
        return self


class AssistantUpdateProposalRecord(BaseModel):
    proposal_id: str
    run_id: str
    instance_id: str
    interrupt_id: str
    source_role_id: str
    requester_task_id: str
    proposal: AssistantUpdateProposal
    target_file_path: str
    status: AssistantUpdateStatus = AssistantUpdateStatus.PROPOSED
    created_at: str = Field(default_factory=_utc_now_iso)
    status_updated_at: str = Field(default_factory=_utc_now_iso)
    status_note: str | None = None

    @model_validator(mode="after")
    def validate_record(self) -> AssistantUpdateProposalRecord:
        for field_name in (
            "proposal_id",
            "run_id",
            "instance_id",
            "interrupt_id",
            "source_role_id",
            "requester_task_id",
            "target_file_path",
        ):
            raw_value = getattr(self, field_name)
            if not raw_value.strip():
                raise ValueError(f"{field_name} must not be empty")
        if self.status_note is not None and not self.status_note.strip():
            raise ValueError("status_note must not be blank")
        return self


class ControlTarget(BaseModel):
    kind: str
    path: str | None = None

    @model_validator(mode="after")
    def validate_target(self) -> ControlTarget:
        if not self.kind.strip():
            raise ValueError("target.kind must not be empty")
        if self.path is not None and not self.path.strip():
            raise ValueError("target.path must not be blank")
        return self


class AssistantControlAction(BaseModel):
    action_id: str
    request_id: str | None = None
    requested_by: ControlActor
    action_kind: ControlActionKind
    target: ControlTarget | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    reason: str
    approval_mode: ApprovalMode
    status: ControlActionStatus = ControlActionStatus.PROPOSED
    created_at: str = Field(default_factory=_utc_now_iso)

    @model_validator(mode="after")
    def validate_action(self) -> AssistantControlAction:
        if not self.action_id.strip():
            raise ValueError("action_id must not be empty")
        if not self.reason.strip():
            raise ValueError("reason must not be empty")
        return self
