from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_orch.domain.assistant import (
    ConfidenceLevel,
    DecisionKind,
    RequestKind,
    RequestPriority,
)
from codex_orch.domain.models import (
    TaskControlMode,
    NodeExecutionTerminationReason,
    ProjectSpec,
    PublishedArtifact,
    RunStatus,
    TaskSpec,
)
from codex_orch.input_values import ensure_json_object, ensure_json_value


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


class ControlEnvelopeKind(StrEnum):
    ROUTE = "route"
    LOOP = "loop"


class LoopAction(StrEnum):
    CONTINUE = "continue"
    STOP = "stop"


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
    requested_target_role_id: str | None = None
    recommended_target_role_id: str | None = None
    resolved_target_role_id: str | None = None
    target_resolution_reason: str | None = None
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
        for field_name in (
            "requested_target_role_id",
            "recommended_target_role_id",
            "resolved_target_role_id",
        ):
            raw_value = getattr(self, field_name)
            if raw_value is None:
                continue
            normalized_value = raw_value.strip()
            if not normalized_value:
                raise ValueError(f"{field_name} must not be blank")
            object.__setattr__(self, field_name, normalized_value)
        if self.target_resolution_reason is not None:
            normalized_reason = self.target_resolution_reason.strip()
            if not normalized_reason:
                raise ValueError("target_resolution_reason must not be blank")
            object.__setattr__(self, "target_resolution_reason", normalized_reason)
        if self.audience is InterruptAudience.ASSISTANT:
            if self.resolved_target_role_id is None:
                raise ValueError(
                    "assistant interrupts must include resolved_target_role_id"
                )
            if self.target_resolution_reason is None:
                raise ValueError(
                    "assistant interrupts must include target_resolution_reason"
                )
        else:
            for field_name in (
                "requested_target_role_id",
                "recommended_target_role_id",
                "resolved_target_role_id",
                "target_resolution_reason",
            ):
                if getattr(self, field_name) is not None:
                    raise ValueError(f"human interrupts must not set {field_name}")
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
        object.__setattr__(
            self,
            "payload",
            ensure_json_object(self.payload, field_name="payload"),
        )
        return self


class ControlEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ControlEnvelopeKind
    labels: list[str] = Field(default_factory=list)
    action: LoopAction | None = None
    next_inputs: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_control(self) -> ControlEnvelope:
        if self.kind is ControlEnvelopeKind.ROUTE:
            if self.action is not None or self.next_inputs:
                raise ValueError(
                    "route control may only define kind and labels"
                )
            normalized_labels: list[str] = []
            for label in self.labels:
                if not label.strip():
                    raise ValueError("route control labels must not be blank")
                normalized_labels.append(label)
            object.__setattr__(self, "labels", normalized_labels)
            return self

        if self.labels:
            raise ValueError("loop control may not define labels")
        if self.action is None:
            raise ValueError("loop control must define action")
        normalized_inputs: dict[str, object] = {}
        for key, value in self.next_inputs.items():
            normalized_key = key.strip()
            if not normalized_key:
                raise ValueError("loop next_inputs keys must not be blank")
            normalized_inputs[normalized_key] = ensure_json_value(
                value,
                field_name=f"loop next_inputs.{normalized_key}",
            )
        if self.action is LoopAction.CONTINUE:
            if not normalized_inputs:
                raise ValueError("loop continue must define next_inputs")
        elif normalized_inputs:
            raise ValueError("loop stop may not define next_inputs")
        object.__setattr__(self, "next_inputs", normalized_inputs)
        return self

    def validate_against_task(self, task: TaskSpec) -> ControlEnvelope:
        if task.kind.value != "controller" or task.control is None:
            raise ValueError(f"task {task.id} is not a controller")
        if self.kind.value != task.control.mode.value:
            raise ValueError(
                f"controller task {task.id} emitted control.kind={self.kind.value} "
                f"but task.control.mode={task.control.mode.value}"
            )
        if self.kind is ControlEnvelopeKind.ROUTE:
            assert task.control.mode is TaskControlMode.ROUTE
            declared_labels = {route.label for route in task.control.routes}
            undeclared = sorted(label for label in set(self.labels) if label not in declared_labels)
            if undeclared:
                raise ValueError(
                    f"controller task {task.id} emitted undeclared labels: {', '.join(undeclared)}"
                )
        else:
            assert task.control.mode is TaskControlMode.LOOP
        return self


class RunInputScopeState(BaseModel):
    input_scope_id: str
    parent_input_scope_id: str | None = None
    seed_task_ids: list[str] = Field(default_factory=list)
    values: dict[str, object] = Field(default_factory=dict)
    created_by_instance_id: str | None = None
    created_at: str = Field(default_factory=_utc_now_iso)

    @model_validator(mode="after")
    def validate_input_scope(self) -> RunInputScopeState:
        if not self.input_scope_id.strip():
            raise ValueError("input_scope_id must not be empty")
        if self.parent_input_scope_id is not None and not self.parent_input_scope_id.strip():
            raise ValueError("parent_input_scope_id must not be blank")
        if self.created_by_instance_id is not None and not self.created_by_instance_id.strip():
            raise ValueError("created_by_instance_id must not be blank")
        normalized_seed_task_ids: list[str] = []
        for task_id in self.seed_task_ids:
            normalized_task_id = task_id.strip()
            if not normalized_task_id:
                raise ValueError("seed_task_ids must not contain blanks")
            normalized_seed_task_ids.append(normalized_task_id)
        if len(set(normalized_seed_task_ids)) != len(normalized_seed_task_ids):
            raise ValueError("seed_task_ids must be unique per input scope")
        normalized_values: dict[str, object] = {}
        for key, value in self.values.items():
            normalized_key = key.strip()
            if not normalized_key:
                raise ValueError("input scope values keys must not be blank")
            normalized_values[normalized_key] = ensure_json_value(
                value,
                field_name=f"input scope values.{normalized_key}",
            )
        object.__setattr__(self, "seed_task_ids", normalized_seed_task_ids)
        object.__setattr__(self, "values", normalized_values)
        return self


class RunTaskPathTemplateState(BaseModel):
    workspace: str | None = None
    extra_writable_roots: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_task_path_templates(self) -> RunTaskPathTemplateState:
        if self.workspace is not None and not self.workspace.strip():
            raise ValueError("task path template workspace must not be blank")
        normalized_extra_writable_roots: list[str] = []
        for raw_root in self.extra_writable_roots:
            normalized_root = raw_root.strip()
            if not normalized_root:
                raise ValueError(
                    "task path template extra_writable_roots must not contain blanks"
                )
            normalized_extra_writable_roots.append(normalized_root)
        object.__setattr__(
            self,
            "extra_writable_roots",
            normalized_extra_writable_roots,
        )
        return self


class RunInstanceState(BaseModel):
    instance_id: str
    task_id: str
    input_scope_id: str
    dependency_instances: dict[str, str] = Field(default_factory=dict)
    activation_bindings: dict[str, str] = Field(default_factory=dict)
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
        if not self.input_scope_id.strip():
            raise ValueError("input_scope_id must not be empty")
        for mapping_name in ("dependency_instances", "activation_bindings"):
            raw_mapping = getattr(self, mapping_name)
            for key, value in raw_mapping.items():
                if not key.strip():
                    raise ValueError(f"{mapping_name} keys must not be empty")
                if not value.strip():
                    raise ValueError(f"{mapping_name} values must not be empty")
        return self


class RunRecord(BaseModel):
    id: str
    roots: list[str]
    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)
    status: RunStatus = RunStatus.PENDING
    user_inputs: dict[str, object] = Field(default_factory=dict)
    project: ProjectSpec
    project_workspace_template: str | None = None
    tasks: dict[str, TaskSpec]
    task_path_templates: dict[str, RunTaskPathTemplateState] = Field(default_factory=dict)
    input_scopes: dict[str, RunInputScopeState]
    instances: dict[str, RunInstanceState]

    @model_validator(mode="after")
    def validate_run(self) -> RunRecord:
        if not self.id.strip():
            raise ValueError("id must not be empty")
        if not self.roots:
            raise ValueError("roots must not be empty")
        if not self.tasks:
            raise ValueError("tasks must not be empty")
        if not self.input_scopes:
            raise ValueError("input_scopes must not be empty")
        normalized_user_inputs: dict[str, object] = {}
        for key, value in self.user_inputs.items():
            normalized_key = key.strip()
            if not normalized_key:
                raise ValueError("user_inputs keys must not be blank")
            normalized_user_inputs[normalized_key] = ensure_json_value(
                value,
                field_name=f"user_inputs.{normalized_key}",
            )
        object.__setattr__(self, "user_inputs", normalized_user_inputs)
        if self.project_workspace_template is not None and not self.project_workspace_template.strip():
            raise ValueError("project_workspace_template must not be blank")
        for input_scope_id, input_scope in self.input_scopes.items():
            if input_scope.input_scope_id != input_scope_id:
                raise ValueError("run input scope snapshot keys must match scope ids")
            if (
                input_scope.parent_input_scope_id is not None
                and input_scope.parent_input_scope_id not in self.input_scopes
            ):
                raise ValueError(
                    f"input scope {input_scope_id} references missing parent {input_scope.parent_input_scope_id}"
                )
            if (
                input_scope.created_by_instance_id is not None
                and input_scope.created_by_instance_id not in self.instances
            ):
                raise ValueError(
                    f"input scope {input_scope_id} references missing instance {input_scope.created_by_instance_id}"
                )
        for task_id, task in self.tasks.items():
            if task.id != task_id:
                raise ValueError("run task snapshot keys must match task ids")
        for task_id in self.task_path_templates:
            if not task_id.strip():
                raise ValueError("task_path_templates keys must not be blank")
            if task_id not in self.tasks:
                raise ValueError(
                    f"task_path_templates references missing task {task_id}"
                )
        for instance in self.instances.values():
            if instance.task_id not in self.tasks:
                raise ValueError(
                    f"instance {instance.instance_id} references missing task {instance.task_id}"
                )
            if instance.input_scope_id not in self.input_scopes:
                raise ValueError(
                    f"instance {instance.instance_id} references missing input scope {instance.input_scope_id}"
                )
        return self
