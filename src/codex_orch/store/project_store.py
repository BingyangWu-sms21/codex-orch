from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from codex_orch.domain import (
    AssistantProfileSpec,
    DependencyEdge,
    DependencyKind,
    InterruptAudience,
    InterruptReply,
    InterruptReplyKind,
    InterruptRequest,
    InterruptStatus,
    NodeExecutionRuntime,
    PresetSpec,
    ProjectSpec,
    RunEvent,
    RunInstanceState,
    RunRecord,
    TaskSpec,
)
from codex_orch.store.layout import (
    GlobalPaths,
    ProgramPaths,
    ensure_global_layout,
    ensure_program_layout,
)


@dataclass(frozen=True)
class ResolvedPreset:
    source: str
    preset: PresetSpec


@dataclass(frozen=True)
class ResolvedAssistantProfile:
    profile_dir: Path
    instructions_path: Path
    workspace_dir: Path
    spec: AssistantProfileSpec


@dataclass(frozen=True)
class InterruptRecord:
    run_id: str
    instance_id: str
    task_id: str
    interrupt: InterruptRequest
    reply: InterruptReply | None


def _read_yaml(path: Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _write_yaml(path: Path, payload: object) -> None:
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=False)


def _read_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


class ProjectStore:
    def __init__(
        self,
        program_dir: Path,
        *,
        global_root: Path | None = None,
    ) -> None:
        self.paths: ProgramPaths = ensure_program_layout(program_dir)
        self.global_paths: GlobalPaths = ensure_global_layout(global_root)

    def load_project(self) -> ProjectSpec:
        payload = _read_yaml(self.paths.project_file)
        if payload is None:
            raise ValueError("project.yaml is empty")
        return ProjectSpec.model_validate(payload)

    def save_project(self, project: ProjectSpec) -> None:
        _write_yaml(self.paths.project_file, project.model_dump(mode="json"))

    def list_tasks(self) -> list[TaskSpec]:
        tasks: list[TaskSpec] = []
        for path in sorted(self.paths.tasks_dir.glob("*.yaml")):
            payload = _read_yaml(path)
            if payload is None:
                continue
            tasks.append(TaskSpec.model_validate(payload))
        return tasks

    def load_task_map(self) -> dict[str, TaskSpec]:
        return {task.id: task for task in self.list_tasks()}

    def get_task(self, task_id: str) -> TaskSpec:
        path = self._task_path(task_id)
        if not path.exists():
            raise KeyError(f"task {task_id} does not exist")
        payload = _read_yaml(path)
        if payload is None:
            raise ValueError(f"task file {path} is empty")
        return TaskSpec.model_validate(payload)

    def save_task(self, task: TaskSpec) -> None:
        _write_yaml(self._task_path(task.id), task.model_dump(mode="json"))

    def delete_task(self, task_id: str) -> None:
        path = self._task_path(task_id)
        if path.exists():
            path.unlink()

    def list_edges(self) -> list[tuple[str, DependencyEdge]]:
        edges: list[tuple[str, DependencyEdge]] = []
        for task in self.list_tasks():
            for dependency in task.depends_on:
                edges.append((task.id, dependency))
        return edges

    def add_edge(
        self,
        *,
        source_task_id: str,
        target_task_id: str,
        kind: DependencyKind,
        consume: list[str],
    ) -> TaskSpec:
        target = self.get_task(target_task_id)
        filtered = [
            dependency
            for dependency in target.depends_on
            if dependency.task != source_task_id or dependency.kind is not kind
        ]
        filtered.append(
            DependencyEdge(task=source_task_id, kind=kind, consume=consume)
        )
        updated = target.model_copy(update={"depends_on": filtered})
        self.save_task(updated)
        return updated

    def remove_edge(
        self,
        *,
        source_task_id: str,
        target_task_id: str,
        kind: DependencyKind,
    ) -> TaskSpec:
        target = self.get_task(target_task_id)
        filtered = [
            dependency
            for dependency in target.depends_on
            if dependency.task != source_task_id or dependency.kind is not kind
        ]
        updated = target.model_copy(update={"depends_on": filtered})
        self.save_task(updated)
        return updated

    def list_presets(self) -> dict[str, ResolvedPreset]:
        resolved: dict[str, ResolvedPreset] = {}
        for path in sorted(self.global_paths.presets_dir.glob("*.yaml")):
            payload = _read_yaml(path)
            if payload is None:
                continue
            preset = PresetSpec.model_validate(payload)
            resolved[preset.id] = ResolvedPreset(source="global", preset=preset)
        for path in sorted(self.paths.presets_dir.glob("*.yaml")):
            payload = _read_yaml(path)
            if payload is None:
                continue
            preset = PresetSpec.model_validate(payload)
            resolved[preset.id] = ResolvedPreset(source="local", preset=preset)
        return resolved

    def get_preset(self, preset_id: str) -> ResolvedPreset:
        presets = self.list_presets()
        if preset_id not in presets:
            raise KeyError(f"preset {preset_id} does not exist")
        return presets[preset_id]

    def save_preset(self, preset: PresetSpec, *, local: bool = True) -> None:
        target_dir = self.paths.presets_dir if local else self.global_paths.presets_dir
        _write_yaml(target_dir / f"{preset.id}.yaml", preset.model_dump(mode="json"))

    def delete_preset(self, preset_id: str, *, local: bool = True) -> None:
        target_dir = self.paths.presets_dir if local else self.global_paths.presets_dir
        path = target_dir / f"{preset_id}.yaml"
        if path.exists():
            path.unlink()

    def get_profile_dir(self, profile_id: str) -> Path:
        return self.global_paths.profiles_dir / profile_id

    def get_profile_spec_path(self, profile_id: str) -> Path:
        return self.get_profile_dir(profile_id) / "profile.yaml"

    def get_profile_instructions_path(self, profile_id: str) -> Path:
        return self.get_profile_dir(profile_id) / "instructions.md"

    def get_profile_workspace_dir(self, profile_id: str) -> Path:
        workspace_dir = self.get_profile_dir(profile_id) / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return workspace_dir

    def load_assistant_profile(self, profile_id: str) -> ResolvedAssistantProfile:
        path = self.get_profile_spec_path(profile_id)
        if not path.exists():
            raise KeyError(f"assistant profile {profile_id} does not exist")
        payload = _read_yaml(path)
        if payload is None:
            raise ValueError(f"profile file {path} is empty")
        spec = AssistantProfileSpec.model_validate(payload)
        if spec.id != profile_id:
            raise ValueError(
                f"profile file {path} has id {spec.id}, expected {profile_id}"
            )
        instructions_path = self.get_profile_instructions_path(profile_id)
        if not instructions_path.exists():
            raise KeyError(
                f"assistant profile {profile_id} is missing instructions.md"
            )
        return ResolvedAssistantProfile(
            profile_dir=self.get_profile_dir(profile_id),
            instructions_path=instructions_path,
            workspace_dir=self.get_profile_workspace_dir(profile_id),
            spec=spec,
        )

    def resolve_assistant_profile_id_for_task(self, task: TaskSpec) -> str | None:
        if task.assistant_profile is not None:
            return task.assistant_profile
        return self.load_project().default_assistant_profile

    def load_default_user_inputs(self) -> dict[str, str]:
        project = self.load_project()
        inputs: dict[str, str] = {}
        for key, relative_path in project.user_inputs.items():
            input_path = self.paths.root / relative_path
            inputs[key] = input_path.read_text(encoding="utf-8")
        return inputs

    def save_run(self, run: RunRecord) -> None:
        run.updated_at = datetime.now(UTC).isoformat()
        _write_json(
            self.get_run_state_path(run.id),
            {
                **run.model_dump(mode="json", exclude={"instances"}),
                "instance_ids": sorted(run.instances),
            },
        )
        for instance_id, instance in run.instances.items():
            _write_json(
                self.get_instance_state_path(run.id, instance_id),
                instance.model_dump(mode="json"),
            )

    def list_runs(self) -> list[RunRecord]:
        runs: list[RunRecord] = []
        for path in sorted(self.paths.runs_dir.glob("*/state/run.json")):
            runs.append(self.get_run(path.parents[1].name))
        return runs

    def get_run(self, run_id: str) -> RunRecord:
        run_path = self.get_run_state_path(run_id)
        if not run_path.exists():
            raise KeyError(f"run {run_id} does not exist")
        summary = _read_json(run_path)
        instance_ids = summary.pop("instance_ids", [])
        if not isinstance(instance_ids, list):
            raise ValueError("run state instance_ids must be a list")
        instances: dict[str, RunInstanceState] = {}
        for instance_id in instance_ids:
            payload = _read_json(self.get_instance_state_path(run_id, str(instance_id)))
            instance = RunInstanceState.model_validate(payload)
            instances[instance.instance_id] = instance
        return RunRecord.model_validate({**summary, "instances": instances})

    def get_run_dir(self, run_id: str) -> Path:
        run_dir = self.paths.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def get_run_state_dir(self, run_id: str) -> Path:
        path = self.get_run_dir(run_id) / "state"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_run_state_path(self, run_id: str) -> Path:
        return self.get_run_state_dir(run_id) / "run.json"

    def get_instances_state_dir(self, run_id: str) -> Path:
        path = self.get_run_state_dir(run_id) / "instances"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_instance_state_path(self, run_id: str, instance_id: str) -> Path:
        return self.get_instances_state_dir(run_id) / f"{instance_id}.json"

    def get_events_dir(self, run_id: str) -> Path:
        path = self.get_run_dir(run_id) / "events"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def append_event(
        self,
        run_id: str,
        event_type: str,
        *,
        instance_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> RunEvent:
        events_dir = self.get_events_dir(run_id)
        sequence = len(list(events_dir.glob("*.json"))) + 1
        event = RunEvent(
            event_id=f"{sequence:06d}-{event_type}",
            run_id=run_id,
            event_type=event_type,
            instance_id=instance_id,
            payload={} if payload is None else payload,
        )
        _write_json(
            events_dir / f"{sequence:06d}-{event_type}.json",
            event.model_dump(mode="json"),
        )
        return event

    def list_events(self, run_id: str) -> list[RunEvent]:
        events: list[RunEvent] = []
        for path in sorted(self.get_events_dir(run_id).glob("*.json")):
            events.append(RunEvent.model_validate(_read_json(path)))
        return events

    def get_instances_dir(self, run_id: str) -> Path:
        path = self.get_run_dir(run_id) / "instances"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_instance_dir(self, run_id: str, instance_id: str) -> Path:
        path = self.get_instances_dir(run_id) / instance_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_attempts_dir(self, run_id: str, instance_id: str) -> Path:
        path = self.get_instance_dir(run_id, instance_id) / "attempts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_attempt_dir(self, run_id: str, instance_id: str, attempt_no: int) -> Path:
        path = self.get_attempts_dir(run_id, instance_id) / f"{attempt_no:04d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_session_path(self, run_id: str, instance_id: str) -> Path:
        return self.get_instance_dir(run_id, instance_id) / "session.json"

    def maybe_get_session_id(self, run_id: str, instance_id: str) -> str | None:
        path = self.get_session_path(run_id, instance_id)
        if not path.exists():
            return None
        payload = _read_json(path)
        if not isinstance(payload, dict):
            return None
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id
        return None

    def get_attempt_runtime_path(
        self,
        run_id: str,
        instance_id: str,
        attempt_no: int,
    ) -> Path:
        return self.get_attempt_dir(run_id, instance_id, attempt_no) / "runtime.json"

    def maybe_get_attempt_runtime(
        self,
        run_id: str,
        instance_id: str,
        attempt_no: int,
    ) -> NodeExecutionRuntime | None:
        path = self.get_attempt_runtime_path(run_id, instance_id, attempt_no)
        if not path.exists():
            return None
        return NodeExecutionRuntime.model_validate(_read_json(path))

    def get_instance_published_dir(self, run_id: str, instance_id: str) -> Path:
        path = self.get_instance_dir(run_id, instance_id) / "published"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_inbox_dir(self, run_id: str) -> Path:
        path = self.get_run_dir(run_id) / "inbox"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_interrupts_dir(self, run_id: str) -> Path:
        path = self.get_inbox_dir(run_id) / "interrupts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_replies_dir(self, run_id: str) -> Path:
        path = self.get_inbox_dir(run_id) / "replies"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_interrupt_path(self, run_id: str, interrupt_id: str) -> Path:
        return self.get_interrupts_dir(run_id) / f"{interrupt_id}.json"

    def get_reply_path(self, run_id: str, interrupt_id: str) -> Path:
        return self.get_replies_dir(run_id) / f"{interrupt_id}.json"

    def create_interrupt(
        self,
        *,
        run_id: str,
        instance_id: str,
        audience: InterruptAudience,
        blocking: bool,
        request_kind,
        question: str,
        decision_kind,
        options: list[str],
        context_artifacts: list[str],
        reply_schema: str | None,
        priority,
        metadata: dict[str, object] | None = None,
    ) -> InterruptRequest:
        run = self.get_run(run_id)
        if instance_id not in run.instances:
            raise KeyError(f"instance {instance_id} does not exist in run {run_id}")
        interrupt = InterruptRequest(
            interrupt_id=self._new_event_id(prefix="int"),
            run_id=run_id,
            instance_id=instance_id,
            task_id=run.instances[instance_id].task_id,
            audience=audience,
            blocking=blocking,
            request_kind=request_kind,
            question=question,
            decision_kind=decision_kind,
            options=options,
            context_artifacts=context_artifacts,
            reply_schema=reply_schema,
            priority=priority,
            metadata={} if metadata is None else metadata,
        )
        self.save_interrupt(interrupt)
        self.append_event(
            run_id,
            "interrupt_requested",
            instance_id=instance_id,
            payload={
                "interrupt_id": interrupt.interrupt_id,
                "audience": interrupt.audience.value,
                "blocking": interrupt.blocking,
            },
        )
        return interrupt

    def save_interrupt(self, interrupt: InterruptRequest) -> None:
        _write_json(
            self.get_interrupt_path(interrupt.run_id, interrupt.interrupt_id),
            interrupt.model_dump(mode="json"),
        )

    def save_interrupt_reply(
        self,
        interrupt_id: str,
        *,
        audience: InterruptAudience,
        reply_kind: InterruptReplyKind,
        text: str,
        payload: dict[str, object] | None = None,
        rationale: str | None = None,
        confidence=None,
        citations: list[str] | None = None,
    ) -> InterruptReply:
        record = self.find_interrupt(interrupt_id)
        reply = InterruptReply(
            interrupt_id=interrupt_id,
            audience=audience,
            reply_kind=reply_kind,
            text=text,
            payload={} if payload is None else payload,
            rationale=rationale,
            confidence=confidence,
            citations=[] if citations is None else citations,
        )
        _write_json(
            self.get_reply_path(record.run_id, interrupt_id),
            reply.model_dump(mode="json"),
        )
        interrupt = record.interrupt.model_copy(
            update={
                "status": InterruptStatus.RESOLVED,
                "resolved_at": datetime.now(UTC).isoformat(),
            }
        )
        self.save_interrupt(interrupt)
        self.append_event(
            record.run_id,
            "interrupt_resolved",
            instance_id=record.instance_id,
            payload={
                "interrupt_id": interrupt_id,
                "reply_kind": reply.reply_kind.value,
                "audience": audience.value,
            },
        )
        return reply

    def mark_interrupt_applied(self, run_id: str, interrupt_id: str) -> InterruptRequest:
        interrupt = self.get_interrupt(run_id, interrupt_id)
        updated = interrupt.model_copy(
            update={
                "status": InterruptStatus.APPLIED,
                "applied_at": datetime.now(UTC).isoformat(),
            }
        )
        self.save_interrupt(updated)
        self.append_event(
            run_id,
            "interrupt_applied",
            instance_id=updated.instance_id,
            payload={"interrupt_id": interrupt_id},
        )
        return updated

    def get_interrupt(self, run_id: str, interrupt_id: str) -> InterruptRequest:
        path = self.get_interrupt_path(run_id, interrupt_id)
        if not path.exists():
            raise KeyError(f"interrupt {interrupt_id} does not exist in run {run_id}")
        return InterruptRequest.model_validate(_read_json(path))

    def maybe_get_interrupt_reply(
        self,
        run_id: str,
        interrupt_id: str,
    ) -> InterruptReply | None:
        path = self.get_reply_path(run_id, interrupt_id)
        if not path.exists():
            return None
        return InterruptReply.model_validate(_read_json(path))

    def list_interrupts(
        self,
        *,
        run_id: str | None = None,
        audience: InterruptAudience | None = None,
        unresolved_only: bool = False,
    ) -> list[InterruptRecord]:
        run_ids: list[str]
        if run_id is not None:
            run_ids = [run_id]
        else:
            run_ids = [path.name for path in sorted(self.paths.runs_dir.iterdir()) if path.is_dir()]
        records: list[InterruptRecord] = []
        for current_run_id in run_ids:
            interrupts_dir = self.get_interrupts_dir(current_run_id)
            for path in sorted(interrupts_dir.glob("*.json")):
                interrupt = InterruptRequest.model_validate(_read_json(path))
                if audience is not None and interrupt.audience is not audience:
                    continue
                if unresolved_only and interrupt.status is not InterruptStatus.OPEN:
                    continue
                reply = self.maybe_get_interrupt_reply(current_run_id, interrupt.interrupt_id)
                records.append(
                    InterruptRecord(
                        run_id=current_run_id,
                        instance_id=interrupt.instance_id,
                        task_id=interrupt.task_id,
                        interrupt=interrupt,
                        reply=reply,
                    )
                )
        return records

    def find_interrupt(self, interrupt_id: str) -> InterruptRecord:
        for record in self.list_interrupts():
            if record.interrupt.interrupt_id == interrupt_id:
                return record
        raise KeyError(f"interrupt {interrupt_id} does not exist")

    def list_instance_interrupts(
        self,
        run_id: str,
        instance_id: str,
        *,
        blocking_only: bool = False,
        unresolved_only: bool = False,
    ) -> list[InterruptRecord]:
        records: list[InterruptRecord] = []
        for record in self.list_interrupts(run_id=run_id, unresolved_only=unresolved_only):
            if record.instance_id != instance_id:
                continue
            if blocking_only and not record.interrupt.blocking:
                continue
            records.append(record)
        return records

    def maybe_get_run_task(self, run_id: str, task_id: str) -> TaskSpec | None:
        try:
            run = self.get_run(run_id)
        except KeyError:
            return None
        for instance in run.instances.values():
            if instance.task_id == task_id:
                return instance.task
        return None

    def get_instance_for_task(self, run_id: str, task_id: str) -> RunInstanceState:
        run = self.get_run(run_id)
        for instance in run.instances.values():
            if instance.task_id == task_id:
                return instance
        raise KeyError(f"task {task_id} does not exist in run {run_id}")

    def resolve_assistant_profile(self, run_id: str, task_id: str) -> ResolvedAssistantProfile | None:
        task = self.maybe_get_run_task(run_id, task_id)
        if task is None:
            try:
                task = self.get_task(task_id)
            except KeyError:
                return None
        profile_id = self.resolve_assistant_profile_id_for_task(task)
        if profile_id is None:
            return None
        return self.load_assistant_profile(profile_id)

    def _task_path(self, task_id: str) -> Path:
        return self.paths.tasks_dir / f"{task_id}.yaml"

    def _new_event_id(self, *, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"
