from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from string import Template

from codex_orch.domain import (
    ComposeStepKind,
    DependencyEdge,
    DependencyKind,
    PresetSpec,
    TaskSpec,
    TaskStatus,
)
from codex_orch.store import ProjectStore


@dataclass(frozen=True)
class GraphEdgeView:
    source: str
    target: str
    kind: str
    consume: tuple[str, ...]


class TaskPoolService:
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
                )
            )
        return views

    def validate_graph(self) -> None:
        tasks = self.store.load_task_map()
        self._validate_task_graph(tasks)

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
        return selected

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
        self._validate_compose_dependencies(tasks)
        self._validate_cycles(tasks)

    def _validate_compose_dependencies(self, tasks: dict[str, TaskSpec]) -> None:
        for task in tasks.values():
            dependency_tasks = {dependency.task for dependency in task.depends_on}
            context_dependencies: dict[str, DependencyEdge] = {}
            for dependency in task.depends_on:
                if dependency.kind is not DependencyKind.CONTEXT:
                    continue
                if dependency.task in context_dependencies:
                    raise ValueError(
                        f"task {task.id} has multiple context dependencies on task "
                        f"{dependency.task}"
                    )
                context_dependencies[dependency.task] = dependency

            for step in task.compose:
                if (
                    step.kind is not ComposeStepKind.FROM_DEP
                    or step.task is None
                    or step.path is None
                ):
                    continue
                if step.task not in dependency_tasks:
                    raise ValueError(
                        f"task {task.id} compose.from_dep references undeclared "
                        f"dependency {step.task}"
                    )
                dependency = context_dependencies.get(step.task)
                if dependency is None:
                    raise ValueError(
                        f"task {task.id} compose.from_dep {step.task}:{step.path} "
                        "requires a context dependency"
                    )
                if step.path not in dependency.consume:
                    raise ValueError(
                        f"task {task.id} compose.from_dep {step.task}:{step.path} "
                        "must be listed in the matching context dependency consume"
                    )

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
