from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from string import Template

from codex_orch.compose_refs import ComposeRefKind, parse_compose_ref
from codex_orch.domain import (
    ComposeStepKind,
    DependencyEdge,
    DependencyKind,
    PresetSpec,
    ProjectSpec,
    TaskControlMode,
    TaskKind,
    TaskSpec,
    TaskStatus,
)
from codex_orch.input_values import JsonValue, referenced_input_template_keys
from codex_orch.schema_utils import (
    OutputSchemaCompatibilityWarning,
    validate_output_schema_compatibility,
)
from codex_orch.store import ProjectStore


@dataclass(frozen=True)
class GraphEdgeView:
    source: str
    target: str
    kind: str
    consume: tuple[str, ...]
    scope: str


@dataclass(frozen=True)
class ProgramValidationIssue:
    severity: str
    code: str
    message: str
    location: str
    reference_url: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "location": self.location,
            "reference_url": self.reference_url,
        }


@dataclass(frozen=True)
class ProgramValidationReport:
    errors: tuple[ProgramValidationIssue, ...] = ()
    warnings: tuple[ProgramValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and not self.warnings

    @property
    def blocking(self) -> bool:
        return bool(self.errors)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "blocking": self.blocking,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
        }


class TaskPoolService:
    _OUTPUT_SCHEMA_REFERENCE_URL = (
        "https://platform.openai.com/docs/guides/structured-outputs"
    )

    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    def list_tasks(
        self,
        *,
        statuses: set[TaskStatus] | None = None,
        label: str | None = None,
    ) -> list[TaskSpec]:
        tasks = self.store.list_tasks()
        filtered: list[TaskSpec] = []
        for task in tasks:
            if statuses is not None and task.status not in statuses:
                continue
            if label is not None and label not in task.labels:
                continue
            filtered.append(task)
        return filtered

    def get_task(self, task_id: str) -> TaskSpec:
        return self.store.get_task(task_id)

    def save_task(self, task: TaskSpec) -> TaskSpec:
        self.store.save_task(task)
        self.validate_graph()
        return task

    def delete_task(self, task_id: str) -> None:
        self.store.delete_task(task_id)
        self.validate_graph()

    def list_edges(self) -> list[GraphEdgeView]:
        views: list[GraphEdgeView] = []
        for target, dependency in self.store.list_edges():
            views.append(
                GraphEdgeView(
                    source=dependency.task,
                    target=target,
                    kind=dependency.kind.value,
                    consume=tuple(dependency.consume),
                    scope=dependency.scope,
                )
            )
        return views

    def validate_graph(self) -> None:
        report = self.validate_program()
        if report.errors:
            raise ValueError(report.errors[0].message)

    def validate_program(self) -> ProgramValidationReport:
        tasks = self.store.load_task_map()
        return self._validate_program_report(tasks=tasks)

    def preview_preset(
        self,
        preset_id: str,
        values: dict[str, str],
    ) -> list[TaskSpec]:
        resolved = self.store.get_preset(preset_id)
        bindings = self._build_bindings(resolved.preset, values)
        rendered_tasks: list[TaskSpec] = []
        for raw_task in resolved.preset.tasks:
            rendered = self._render_value(raw_task, bindings)
            if not isinstance(rendered, dict):
                raise ValueError("rendered preset task must be an object")
            rendered_tasks.append(TaskSpec.model_validate(rendered))
        self._validate_task_graph({task.id: task for task in rendered_tasks})
        return rendered_tasks

    def apply_preset(
        self,
        preset_id: str,
        values: dict[str, str],
        *,
        overwrite: bool = False,
    ) -> list[TaskSpec]:
        tasks = self.preview_preset(preset_id, values)
        existing = self.store.load_task_map()
        for task in tasks:
            if task.id in existing and not overwrite:
                raise ValueError(f"task {task.id} already exists")
        for task in tasks:
            self.store.save_task(task)
        self.validate_graph()
        return tasks

    def select_subgraph(
        self,
        *,
        roots: Iterable[str] = (),
        labels: Iterable[str] = (),
    ) -> dict[str, TaskSpec]:
        tasks = self.store.load_task_map()
        root_ids = set(roots)
        label_set = set(labels)
        if label_set:
            root_ids.update(task.id for task in tasks.values() if label_set & set(task.labels))
        if not root_ids:
            raise ValueError("at least one root task id or label is required")

        selected: dict[str, TaskSpec] = {}
        stack = list(root_ids)
        while stack:
            task_id = stack.pop()
            if task_id in selected:
                continue
            if task_id not in tasks:
                raise ValueError(f"task {task_id} does not exist")
            task = tasks[task_id]
            selected[task_id] = task
            stack.extend(dependency.task for dependency in task.depends_on)
            if task.kind is TaskKind.CONTROLLER and task.control is not None:
                if task.control.mode is TaskControlMode.ROUTE:
                    for route in task.control.routes:
                        stack.extend(route.targets)
                else:
                    stack.extend(task.control.continue_targets)
                    stack.extend(task.control.stop_targets)
        return selected

    def routed_targets(self, tasks: dict[str, TaskSpec]) -> dict[str, str]:
        targets: dict[str, str] = {}
        for task in tasks.values():
            if task.kind is not TaskKind.CONTROLLER or task.control is None:
                continue
            if task.control.mode is TaskControlMode.ROUTE:
                for route in task.control.routes:
                    for target in route.targets:
                        targets[target] = task.id
            else:
                for target in (*task.control.continue_targets, *task.control.stop_targets):
                    targets[target] = task.id
        return targets

    def _build_bindings(
        self,
        preset: PresetSpec,
        values: dict[str, str],
    ) -> dict[str, str]:
        bindings: dict[str, str] = {}
        for key, spec in preset.variables.items():
            if key in values:
                bindings[key] = values[key]
                continue
            if spec.default is not None:
                bindings[key] = spec.default
                continue
            if spec.required:
                raise ValueError(f"missing preset variable {key}")
        extra_values = {key: value for key, value in values.items() if key not in bindings}
        bindings.update(extra_values)
        return bindings

    def _render_value(
        self,
        value: object,
        bindings: dict[str, str],
    ) -> object:
        if isinstance(value, str):
            return Template(value).substitute(bindings)
        if isinstance(value, list):
            return [self._render_value(item, bindings) for item in value]
        if isinstance(value, dict):
            return {
                key: self._render_value(item, bindings)
                for key, item in value.items()
            }
        return value

    def _validate_dependencies_exist(
        self,
        tasks: dict[str, TaskSpec],
        *,
        allow_external: bool = False,
    ) -> None:
        for task in tasks.values():
            for dependency in task.depends_on:
                if dependency.task not in tasks and not allow_external:
                    raise ValueError(
                        f"task {task.id} depends on missing task {dependency.task}"
                    )

    def _validate_task_graph(
        self,
        tasks: dict[str, TaskSpec],
        *,
        allow_external: bool = False,
    ) -> None:
        self._validate_dependencies_exist(tasks, allow_external=allow_external)
        self._validate_controller_routes(tasks)
        self._validate_compose_refs(tasks)
        self._validate_cycles(tasks)

    def _validate_program_report(
        self,
        *,
        tasks: dict[str, TaskSpec],
    ) -> ProgramValidationReport:
        errors: list[ProgramValidationIssue] = []
        warnings: list[ProgramValidationIssue] = []

        try:
            self._validate_task_graph(tasks)
        except ValueError as exc:
            errors.append(
                ProgramValidationIssue(
                    severity="error",
                    code="graph.invalid",
                    message=str(exc),
                    location="graph",
                )
            )

        if not self.store.paths.project_file.exists():
            return ProgramValidationReport(
                errors=tuple(errors),
                warnings=tuple(warnings),
            )

        project = self.store.load_project()
        try:
            default_inputs = self.store.load_default_user_inputs()
        except (OSError, ValueError) as exc:
            errors.append(
                ProgramValidationIssue(
                    severity="error",
                    code="inputs.default_load_failed",
                    message=f"failed to load default user inputs: {exc}",
                    location="project.user_inputs",
                )
            )
            return ProgramValidationReport(
                errors=tuple(errors),
                warnings=tuple(warnings),
            )

        errors.extend(self._validate_path_bound_inputs(project, tasks, default_inputs))
        schema_errors, schema_warnings = self._collect_result_schema_issues(tasks)
        errors.extend(schema_errors)
        warnings.extend(schema_warnings)
        return ProgramValidationReport(
            errors=tuple(errors),
            warnings=tuple(warnings),
        )

    def _collect_result_schema_issues(
        self,
        tasks: dict[str, TaskSpec],
    ) -> tuple[list[ProgramValidationIssue], list[ProgramValidationIssue]]:
        errors: list[ProgramValidationIssue] = []
        warnings: list[ProgramValidationIssue] = []
        for task in tasks.values():
            if task.result_schema is None:
                continue
            field_name = f"task {task.id} result_schema"
            schema_path = self.store.paths.root / task.result_schema
            try:
                compatibility_warnings = validate_output_schema_compatibility(
                    schema_path=schema_path,
                    field_name=field_name,
                )
            except ValueError as exc:
                errors.append(
                    ProgramValidationIssue(
                        severity="error",
                        code="result_schema.invalid",
                        message=str(exc),
                        location=f"tasks/{task.id}.yaml:result_schema",
                    )
                )
                continue
            for item in compatibility_warnings:
                issue = self._schema_warning_issue(task.id, task.result_schema, item)
                if issue.severity == "error":
                    errors.append(issue)
                else:
                    warnings.append(issue)
        return errors, warnings

    def _validate_path_bound_inputs(
        self,
        project: ProjectSpec,
        tasks: dict[str, TaskSpec],
        default_inputs: dict[str, JsonValue],
    ) -> list[ProgramValidationIssue]:
        errors: list[ProgramValidationIssue] = []
        issue = self._validate_path_template_inputs(
            field_name="project.workspace",
            location="project.workspace",
            raw_value=project.workspace,
            default_inputs=default_inputs,
        )
        if issue is not None:
            errors.append(issue)
        for task in tasks.values():
            if task.workspace is not None:
                issue = self._validate_path_template_inputs(
                    field_name=f"task {task.id} workspace",
                    location=f"tasks/{task.id}.yaml:workspace",
                    raw_value=task.workspace,
                    default_inputs=default_inputs,
                )
                if issue is not None:
                    errors.append(issue)
            for index, raw_root in enumerate(task.extra_writable_roots):
                issue = self._validate_path_template_inputs(
                    field_name=f"task {task.id} extra_writable_roots[{index}]",
                    location=f"tasks/{task.id}.yaml:extra_writable_roots[{index}]",
                    raw_value=raw_root,
                    default_inputs=default_inputs,
                )
                if issue is not None:
                    errors.append(issue)
        return errors

    def _validate_path_template_inputs(
        self,
        *,
        field_name: str,
        location: str,
        raw_value: str,
        default_inputs: dict[str, JsonValue],
    ) -> ProgramValidationIssue | None:
        for input_key in referenced_input_template_keys(raw_value):
            if input_key not in default_inputs:
                continue
            value = default_inputs[input_key]
            if not isinstance(value, str):
                return ProgramValidationIssue(
                    severity="error",
                    code="path_input.non_string",
                    message=(
                        f"{field_name} references inputs.{input_key}, which must resolve to a string"
                    ),
                    location=location,
                )
            if value != value.strip():
                return ProgramValidationIssue(
                    severity="error",
                    code="path_input.whitespace",
                    message=(
                        f"{field_name} references inputs.{input_key}, whose default value has leading or trailing whitespace"
                    ),
                    location=location,
                )
        return None

    def _schema_warning_issue(
        self,
        task_id: str,
        schema_relative_path: str,
        warning: OutputSchemaCompatibilityWarning,
    ) -> ProgramValidationIssue:
        return ProgramValidationIssue(
            severity=warning.severity,
            code=warning.code,
            message=warning.message,
            location=(
                f"tasks/{task_id}.yaml:result_schema -> {schema_relative_path} "
                f"at {warning.object_path}"
            ),
            reference_url=self._OUTPUT_SCHEMA_REFERENCE_URL,
        )

    def _validate_compose_refs(self, tasks: dict[str, TaskSpec]) -> None:
        for task in tasks.values():
            dependencies_by_scope = {dependency.scope: dependency for dependency in task.depends_on}
            for step in task.compose:
                if step.kind is not ComposeStepKind.REF or step.ref is None:
                    continue
                parsed = parse_compose_ref(step.ref)
                if parsed.kind in {
                    ComposeRefKind.INPUT,
                    ComposeRefKind.RUNTIME_REPLIES,
                    ComposeRefKind.RUNTIME_LATEST_REPLY,
                }:
                    continue
                assert parsed.scope is not None
                dependency = dependencies_by_scope.get(parsed.scope)
                if dependency is None:
                    raise ValueError(
                        f"task {task.id} compose.ref {step.ref} references unknown dependency scope {parsed.scope}"
                    )
                if parsed.kind is ComposeRefKind.DEP_RESULT:
                    continue
                assert parsed.artifact_path is not None
                if dependency.kind is not DependencyKind.CONTEXT:
                    raise ValueError(
                        f"task {task.id} compose.ref {step.ref} requires a context dependency"
                    )
                if parsed.artifact_path not in dependency.consume:
                    raise ValueError(
                        f"task {task.id} compose.ref {step.ref} must be listed in the matching context dependency consume"
                    )

    def _validate_controller_routes(self, tasks: dict[str, TaskSpec]) -> None:
        routed_targets: dict[str, str] = {}
        for task in tasks.values():
            if task.kind is not TaskKind.CONTROLLER:
                continue
            assert task.control is not None
            if task.control.mode is TaskControlMode.ROUTE:
                for route in task.control.routes:
                    for target_task_id in route.targets:
                        if target_task_id not in tasks:
                            raise ValueError(
                                f"controller task {task.id} routes to missing task {target_task_id}"
                            )
                        target_task = tasks[target_task_id]
                        if target_task_id in routed_targets:
                            raise ValueError(
                                f"task {target_task_id} is targeted by more than one controller control edge"
                            )
                        routed_targets[target_task_id] = task.id
                        if task.id not in {
                            dependency.task for dependency in target_task.depends_on
                        }:
                            raise ValueError(
                                f"controller route {task.id} -> {target_task_id} requires {target_task_id} to depend_on {task.id}"
                            )
                continue

            controller_ancestors = self._collect_ancestors(tasks, task.id)
            for target_task_id in task.control.stop_targets:
                if target_task_id not in tasks:
                    raise ValueError(
                        f"loop controller task {task.id} stop target {target_task_id} is missing"
                    )
                target_task = tasks[target_task_id]
                if target_task_id in routed_targets:
                    raise ValueError(
                        f"task {target_task_id} is targeted by more than one controller control edge"
                    )
                routed_targets[target_task_id] = task.id
                if task.id not in {dependency.task for dependency in target_task.depends_on}:
                    raise ValueError(
                        f"loop stop target {task.id} -> {target_task_id} requires {target_task_id} to depend_on {task.id}"
                    )

            for target_task_id in task.control.continue_targets:
                if target_task_id not in tasks:
                    raise ValueError(
                        f"loop controller task {task.id} continue target {target_task_id} is missing"
                    )
                if target_task_id in routed_targets:
                    raise ValueError(
                        f"task {target_task_id} is targeted by more than one controller control edge"
                    )
                if target_task_id not in controller_ancestors:
                    raise ValueError(
                        f"loop continue target {target_task_id} must be an ancestor of controller {task.id}"
                    )
                routed_targets[target_task_id] = task.id

    def _collect_ancestors(
        self,
        tasks: dict[str, TaskSpec],
        task_id: str,
    ) -> set[str]:
        ancestors: set[str] = set()
        stack = [task_id]
        while stack:
            current = stack.pop()
            for dependency in tasks[current].depends_on:
                if dependency.task not in tasks:
                    continue
                if dependency.task in ancestors:
                    continue
                ancestors.add(dependency.task)
                stack.append(dependency.task)
        return ancestors

    def _validate_cycles(self, tasks: dict[str, TaskSpec]) -> None:
        visited: set[str] = set()
        active: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visited:
                return
            if task_id in active:
                raise ValueError(f"cycle detected at task {task_id}")
            active.add(task_id)
            task = tasks[task_id]
            for dependency in task.depends_on:
                if dependency.task in tasks:
                    visit(dependency.task)
            active.remove(task_id)
            visited.add(task_id)

        for task_id in tasks:
            visit(task_id)
