from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from string import Template

from codex_orch.domain import DependencyEdge, PresetSpec, TaskSpec, TaskStatus
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
        self._validate_dependencies_exist(tasks)
        self._validate_cycles(tasks)

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
        self._validate_dependencies_exist(
            {task.id: task for task in rendered_tasks},
            allow_external=False,
        )
        self._validate_cycles({task.id: task for task in rendered_tasks})
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
