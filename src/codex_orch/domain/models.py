from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, Field, model_validator

from codex_orch.domain.assistant import DecisionKind


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class TaskStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    BLOCKED = "blocked"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    ARCHIVED = "archived"


class DependencyKind(StrEnum):
    CONTEXT = "context"
    ORDER = "order"


class ComposeStepKind(StrEnum):
    FILE = "file"
    USER_INPUT = "user_input"
    FROM_DEP = "from_dep"
    LITERAL = "literal"


class RunNodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunNodeWaitReason(StrEnum):
    ASSISTANT_PENDING = "assistant_pending"
    HANDOFF_TO_HUMAN = "handoff_to_human"
    MANUAL_GATE_BLOCKED = "manual_gate_blocked"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"


class NodeExecutionTerminationReason(StrEnum):
    COMPLETED = "completed"
    NONZERO_EXIT = "nonzero_exit"
    WALL_TIMEOUT = "wall_timeout"
    IDLE_TIMEOUT = "idle_timeout"
    TERMINATED = "terminated"
    ORPHANED = "orphaned"


def _validate_relative_file_path(raw_path: str) -> str:
    candidate = PurePosixPath(raw_path)
    if candidate.is_absolute():
        raise ValueError("paths must be relative")
    if ".." in candidate.parts:
        raise ValueError("paths must not escape the node directory")
    normalized = str(candidate)
    if normalized == ".":
        raise ValueError("path must point to a file")
    return normalized


def _validate_path_reference(raw_path: str, *, field_name: str) -> str:
    normalized = raw_path.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


class ProjectSpec(BaseModel):
    name: str
    workspace: str
    description: str = ""
    default_agent: str = "default"
    default_model: str | None = None
    default_sandbox: str = "workspace-write"
    max_concurrency: int = 2
    node_wall_timeout_sec: float | None = 3600.0
    node_idle_timeout_sec: float | None = 600.0
    node_terminate_grace_sec: float = 10.0
    user_inputs: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_project(self) -> ProjectSpec:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if not self.workspace.strip():
            raise ValueError("workspace must not be empty")
        if self.node_wall_timeout_sec is not None and self.node_wall_timeout_sec <= 0:
            raise ValueError("node_wall_timeout_sec must be > 0")
        if self.node_idle_timeout_sec is not None and self.node_idle_timeout_sec <= 0:
            raise ValueError("node_idle_timeout_sec must be > 0")
        if self.node_terminate_grace_sec <= 0:
            raise ValueError("node_terminate_grace_sec must be > 0")
        return self


class DependencyEdge(BaseModel):
    task: str
    kind: DependencyKind
    consume: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_dependency(self) -> DependencyEdge:
        if self.kind is DependencyKind.ORDER and self.consume:
            raise ValueError("order dependencies cannot consume artifacts")
        if self.kind is DependencyKind.CONTEXT and not self.consume:
            raise ValueError("context dependencies must consume at least one file")
        validated_paths = [_validate_relative_file_path(path) for path in self.consume]
        object.__setattr__(self, "consume", validated_paths)
        return self


class ComposeStepSpec(BaseModel):
    kind: ComposeStepKind
    path: str | None = None
    key: str | None = None
    task: str | None = None
    text: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_shape(
        cls, data: object
    ) -> dict[str, object] | object:
        if not isinstance(data, Mapping):
            return data
        raw = dict(data)
        if "kind" in raw:
            return raw
        if "file" in raw:
            return {"kind": "file", "path": raw["file"]}
        if "user_input" in raw:
            return {"kind": "user_input", "key": raw["user_input"]}
        if "literal" in raw:
            return {"kind": "literal", "text": raw["literal"]}
        if "from_dep" in raw:
            dep = raw["from_dep"]
            if isinstance(dep, Mapping):
                dep_raw = dict(dep)
                return {
                    "kind": "from_dep",
                    "task": dep_raw.get("task"),
                    "path": dep_raw.get("path"),
                }
        return raw

    @model_validator(mode="after")
    def validate_compose_step(self) -> ComposeStepSpec:
        if self.kind is ComposeStepKind.FILE:
            if self.path is None:
                raise ValueError("file steps require path")
            object.__setattr__(self, "path", _validate_relative_file_path(self.path))
        elif self.kind is ComposeStepKind.USER_INPUT:
            if self.key is None:
                raise ValueError("user_input steps require key")
        elif self.kind is ComposeStepKind.FROM_DEP:
            if self.task is None or self.path is None:
                raise ValueError("from_dep steps require task and path")
            object.__setattr__(self, "path", _validate_relative_file_path(self.path))
        elif self.kind is ComposeStepKind.LITERAL and self.text is None:
            raise ValueError("literal steps require text")
        return self


class TaskAssistantHints(BaseModel):
    preferred_roles: list[str] = Field(default_factory=list)
    decision_kind_overrides: dict[str, str] = Field(default_factory=dict)
    ask_when: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_hints(self) -> TaskAssistantHints:
        object.__setattr__(
            self,
            "preferred_roles",
            [
                _validate_path_reference(role_id, field_name="preferred_roles")
                for role_id in self.preferred_roles
            ],
        )
        normalized_overrides: dict[str, str] = {}
        for raw_kind, role_id in self.decision_kind_overrides.items():
            decision_kind = DecisionKind(raw_kind)
            normalized_overrides[decision_kind.value] = _validate_path_reference(
                role_id,
                field_name="decision_kind_overrides",
            )
        object.__setattr__(self, "decision_kind_overrides", normalized_overrides)
        return self


class TaskInteractionPolicy(BaseModel):
    allowed_assistant_roles: list[str] | None = None
    allow_human: bool = True

    @model_validator(mode="after")
    def validate_policy(self) -> TaskInteractionPolicy:
        if self.allowed_assistant_roles is None:
            return self
        object.__setattr__(
            self,
            "allowed_assistant_roles",
            [
                _validate_path_reference(role_id, field_name="allowed_assistant_roles")
                for role_id in self.allowed_assistant_roles
            ],
        )
        return self


class TaskSpec(BaseModel):
    id: str
    title: str
    agent: str
    status: TaskStatus = TaskStatus.DRAFT
    description: str = ""
    labels: list[str] = Field(default_factory=list)
    depends_on: list[DependencyEdge] = Field(default_factory=list)
    compose: list[ComposeStepSpec] = Field(default_factory=list)
    publish: list[str] = Field(default_factory=lambda: ["final.md"])
    assistant_hints: TaskAssistantHints = Field(default_factory=TaskAssistantHints)
    interaction_policy: TaskInteractionPolicy = Field(default_factory=TaskInteractionPolicy)
    model: str | None = None
    sandbox: str | None = None
    workspace: str | None = None
    extra_writable_roots: list[str] = Field(default_factory=list)
    result_schema: str | None = None

    @model_validator(mode="after")
    def validate_task(self) -> TaskSpec:
        validated_publish = [_validate_relative_file_path(path) for path in self.publish]
        object.__setattr__(self, "publish", validated_publish)
        if self.workspace is not None:
            object.__setattr__(
                self,
                "workspace",
                _validate_path_reference(self.workspace, field_name="workspace"),
            )
        validated_writable_roots = [
            _validate_path_reference(path, field_name="extra_writable_roots")
            for path in self.extra_writable_roots
        ]
        object.__setattr__(self, "extra_writable_roots", validated_writable_roots)
        if self.result_schema is not None:
            object.__setattr__(
                self,
                "result_schema",
                _validate_relative_file_path(self.result_schema),
            )
        return self


class PresetVariableSpec(BaseModel):
    description: str = ""
    default: str | None = None
    required: bool = True

    @model_validator(mode="after")
    def normalize_requirement(self) -> PresetVariableSpec:
        if self.default is not None:
            object.__setattr__(self, "required", False)
        return self


class PresetSpec(BaseModel):
    id: str
    title: str
    description: str = ""
    variables: dict[str, PresetVariableSpec] = Field(default_factory=dict)
    tasks: list[dict[str, object]] = Field(default_factory=list)


class PublishedArtifact(BaseModel):
    relative_path: str

    @model_validator(mode="after")
    def validate_artifact(self) -> PublishedArtifact:
        object.__setattr__(
            self,
            "relative_path",
            _validate_relative_file_path(self.relative_path),
        )
        return self


class NodeExecutionRuntime(BaseModel):
    pid: int | None = None
    cwd: str
    project_workspace_dir: str | None = None
    command: list[str]
    sandbox: str | None = None
    writable_roots: list[str] = Field(default_factory=list)
    started_at: str = Field(default_factory=utc_now_iso)
    finished_at: str | None = None
    last_stdout_at: str | None = None
    last_stderr_at: str | None = None
    last_event_at: str | None = None
    last_progress_at: str | None = None
    last_event_summary: str | None = None
    stdout_line_count: int = 0
    stderr_line_count: int = 0
    wall_timeout_sec: float | None = None
    idle_timeout_sec: float | None = None
    termination_reason: NodeExecutionTerminationReason | None = None
    return_code: int | None = None

    @model_validator(mode="after")
    def validate_runtime(self) -> NodeExecutionRuntime:
        if self.pid is not None and self.pid <= 0:
            raise ValueError("pid must be > 0")
        if not self.cwd.strip():
            raise ValueError("cwd must not be empty")
        if self.project_workspace_dir is not None and not self.project_workspace_dir.strip():
            raise ValueError("project_workspace_dir must not be empty")
        if not self.command:
            raise ValueError("command must not be empty")
        if self.sandbox is not None and not self.sandbox.strip():
            raise ValueError("sandbox must not be empty")
        validated_writable_roots = [
            _validate_path_reference(path, field_name="writable_roots")
            for path in self.writable_roots
        ]
        object.__setattr__(self, "writable_roots", validated_writable_roots)
        if self.stdout_line_count < 0:
            raise ValueError("stdout_line_count must be >= 0")
        if self.stderr_line_count < 0:
            raise ValueError("stderr_line_count must be >= 0")
        if self.wall_timeout_sec is not None and self.wall_timeout_sec <= 0:
            raise ValueError("wall_timeout_sec must be > 0")
        if self.idle_timeout_sec is not None and self.idle_timeout_sec <= 0:
            raise ValueError("idle_timeout_sec must be > 0")
        return self


class RunNodeState(BaseModel):
    task: TaskSpec
    status: RunNodeStatus = RunNodeStatus.PENDING
    waiting_reason: RunNodeWaitReason | None = None
    published: list[PublishedArtifact] = Field(default_factory=list)
    error: str | None = None
    attempt: int = 0
    termination_reason: NodeExecutionTerminationReason | None = None
    started_at: str | None = None
    finished_at: str | None = None


class RunSnapshot(BaseModel):
    id: str
    roots: list[str]
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    status: RunStatus = RunStatus.PENDING
    user_inputs: dict[str, str] = Field(default_factory=dict)
    prefect_flow_run_id: str | None = None
    nodes: dict[str, RunNodeState]
