from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_orch.compose_refs import parse_compose_ref
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


class TaskKind(StrEnum):
    WORK = "work"
    CONTROLLER = "controller"


class TaskControlMode(StrEnum):
    ROUTE = "route"
    LOOP = "loop"


class DependencyKind(StrEnum):
    CONTEXT = "context"
    ORDER = "order"


class ComposeStepKind(StrEnum):
    FILE = "file"
    REF = "ref"
    LITERAL = "literal"


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


class NodeExecutionFailureKind(StrEnum):
    EXTERNAL_AUTH = "external_auth"
    EXTERNAL_NETWORK = "external_network"
    EXTERNAL_PROTOCOL = "external_protocol"
    OUTPUT_SCHEMA = "output_schema"
    RUNNER_INVOCATION = "runner_invocation"
    TASK_RUNTIME = "task_runtime"
    UNKNOWN = "unknown"


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
    model_config = ConfigDict(populate_by_name=True)

    task: str
    kind: DependencyKind
    consume: list[str] = Field(default_factory=list)
    as_: str | None = Field(default=None, alias="as")

    @model_validator(mode="after")
    def validate_dependency(self) -> DependencyEdge:
        if self.kind is DependencyKind.ORDER and self.consume:
            raise ValueError("order dependencies cannot consume artifacts")
        if self.kind is DependencyKind.CONTEXT and not self.consume:
            raise ValueError("context dependencies must consume at least one file")
        if self.as_ is not None:
            object.__setattr__(
                self,
                "as_",
                _validate_path_reference(self.as_, field_name="depends_on.as"),
            )
        validated_paths = [_validate_relative_file_path(path) for path in self.consume]
        object.__setattr__(self, "consume", validated_paths)
        return self

    @property
    def scope(self) -> str:
        return self.as_ or self.task


class ComposeStepSpec(BaseModel):
    kind: ComposeStepKind
    path: str | None = None
    ref: str | None = None
    text: str | None = None

    @model_validator(mode="after")
    def validate_compose_step(self) -> ComposeStepSpec:
        if self.kind is ComposeStepKind.FILE:
            if self.path is None:
                raise ValueError("file steps require path")
            object.__setattr__(self, "path", _validate_relative_file_path(self.path))
            if self.ref is not None or self.text is not None:
                raise ValueError("file steps may only define path")
        elif self.kind is ComposeStepKind.REF:
            if self.ref is None:
                raise ValueError("ref steps require ref")
            object.__setattr__(
                self,
                "ref",
                _validate_path_reference(self.ref, field_name="compose.ref"),
            )
            parse_compose_ref(self.ref)
            if self.path is not None or self.text is not None:
                raise ValueError("ref steps may only define ref")
        elif self.kind is ComposeStepKind.LITERAL:
            if self.text is None:
                raise ValueError("literal steps require text")
            if self.path is not None or self.ref is not None:
                raise ValueError("literal steps may only define text")
        return self


class ControllerRouteSpec(BaseModel):
    label: str
    targets: list[str]

    @model_validator(mode="after")
    def validate_route(self) -> ControllerRouteSpec:
        if not self.label.strip():
            raise ValueError("control.routes[].label must not be empty")
        normalized_targets = [
            _validate_path_reference(target, field_name="control.routes[].targets")
            for target in self.targets
        ]
        if not normalized_targets:
            raise ValueError("control.routes[].targets must not be empty")
        object.__setattr__(self, "targets", normalized_targets)
        return self


class TaskControlSpec(BaseModel):
    mode: TaskControlMode
    routes: list[ControllerRouteSpec] = Field(default_factory=list)
    continue_targets: list[str] = Field(default_factory=list)
    stop_targets: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_control(self) -> TaskControlSpec:
        if self.mode is TaskControlMode.ROUTE:
            if not self.routes:
                raise ValueError("route controllers must declare at least one route")
            if self.continue_targets or self.stop_targets:
                raise ValueError(
                    "route controllers may not declare continue_targets or stop_targets"
                )
            labels = [route.label for route in self.routes]
            if len(set(labels)) != len(labels):
                raise ValueError("controller route labels must be unique per task")
            return self
        if self.routes:
            raise ValueError("loop controllers may not declare routes")
        normalized_continue_targets = [
            _validate_path_reference(
                target,
                field_name="control.continue_targets",
            )
            for target in self.continue_targets
        ]
        if not normalized_continue_targets:
            raise ValueError("loop controllers must declare at least one continue target")
        normalized_stop_targets = [
            _validate_path_reference(
                target,
                field_name="control.stop_targets",
            )
            for target in self.stop_targets
        ]
        if len(set(normalized_continue_targets)) != len(normalized_continue_targets):
            raise ValueError("loop continue targets must be unique per task")
        if len(set(normalized_stop_targets)) != len(normalized_stop_targets):
            raise ValueError("loop stop targets must be unique per task")
        overlap = sorted(set(normalized_continue_targets) & set(normalized_stop_targets))
        if overlap:
            raise ValueError(
                "loop continue and stop targets must not overlap: "
                + ", ".join(overlap)
            )
        object.__setattr__(self, "continue_targets", normalized_continue_targets)
        object.__setattr__(self, "stop_targets", normalized_stop_targets)
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
    kind: TaskKind = TaskKind.WORK
    status: TaskStatus = TaskStatus.DRAFT
    description: str = ""
    labels: list[str] = Field(default_factory=list)
    depends_on: list[DependencyEdge] = Field(default_factory=list)
    compose: list[ComposeStepSpec] = Field(default_factory=list)
    publish: list[str] = Field(default_factory=lambda: ["final.md"])
    control: TaskControlSpec | None = None
    assistant_hints: TaskAssistantHints = Field(default_factory=TaskAssistantHints)
    interaction_policy: TaskInteractionPolicy = Field(default_factory=TaskInteractionPolicy)
    model: str | None = None
    sandbox: str | None = None
    workspace: str | None = None
    extra_writable_roots: list[str] = Field(default_factory=list)
    result_schema: str | None = None

    @model_validator(mode="after")
    def validate_task(self) -> TaskSpec:
        if not self.id.strip():
            raise ValueError("id must not be empty")
        if not self.title.strip():
            raise ValueError("title must not be empty")
        if not self.agent.strip():
            raise ValueError("agent must not be empty")
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
        if self.kind is TaskKind.CONTROLLER:
            if self.control is None:
                raise ValueError("controller tasks must declare control")
        elif self.control is not None:
            raise ValueError("only controller tasks may declare control")
        scopes = [dependency.scope for dependency in self.depends_on]
        if len(set(scopes)) != len(scopes):
            raise ValueError("dependency scopes must be unique per task")
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
    failure_kind: NodeExecutionFailureKind | None = None
    failure_summary: str | None = None
    resume_recommended: bool = False
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
        if self.failure_summary is not None and not self.failure_summary.strip():
            raise ValueError("failure_summary must not be blank")
        if self.termination_reason is NodeExecutionTerminationReason.COMPLETED:
            if self.failure_kind is not None or self.failure_summary is not None:
                raise ValueError(
                    "completed runtime may not define failure_kind or failure_summary"
                )
        return self
