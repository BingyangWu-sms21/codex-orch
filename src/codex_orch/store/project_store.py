from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from codex_orch.domain import (
    ApprovalMode,
    AssistantControlAction,
    AssistantRequest,
    AssistantResponse,
    ConfidenceLevel,
    ControlActionKind,
    ControlActionStatus,
    ControlActor,
    ControlTarget,
    DecisionKind,
    DependencyEdge,
    DependencyKind,
    HumanRequest,
    HumanResponse,
    ManualGate,
    ManualGateReason,
    ManualGateStatus,
    NodeExecutionRuntime,
    PresetSpec,
    ProjectSpec,
    RunSnapshot,
    RequestKind,
    RequestPriority,
    ResolutionKind,
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
class AssistantRequestRecord:
    run_id: str
    task_id: str
    node_dir: Path
    request: AssistantRequest
    response: AssistantResponse | None
    control_action: AssistantControlAction | None


@dataclass(frozen=True)
class ManualGateRecord:
    run_id: str
    task_id: str
    node_dir: Path
    gate: ManualGate
    human_request: HumanRequest | None
    human_response: HumanResponse | None


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

    def load_default_user_inputs(self) -> dict[str, str]:
        project = self.load_project()
        inputs: dict[str, str] = {}
        for key, relative_path in project.user_inputs.items():
            input_path = self.paths.root / relative_path
            inputs[key] = input_path.read_text(encoding="utf-8")
        return inputs

    def save_run(self, snapshot: RunSnapshot) -> None:
        run_dir = self.get_run_dir(snapshot.id)
        run_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = run_dir / "snapshot.json"
        snapshot.updated_at = datetime.now(UTC).isoformat()
        _write_json(snapshot_path, snapshot.model_dump(mode="json"))

    def list_runs(self) -> list[RunSnapshot]:
        runs: list[RunSnapshot] = []
        for path in sorted(self.paths.runs_dir.glob("*/snapshot.json")):
            payload = _read_json(path)
            runs.append(RunSnapshot.model_validate(payload))
        return runs

    def get_run(self, run_id: str) -> RunSnapshot:
        path = self.get_run_dir(run_id) / "snapshot.json"
        if not path.exists():
            raise KeyError(f"run {run_id} does not exist")
        payload = _read_json(path)
        return RunSnapshot.model_validate(payload)

    def get_run_dir(self, run_id: str) -> Path:
        return self.paths.runs_dir / run_id

    def get_node_dir(self, run_id: str, task_id: str) -> Path:
        node_dir = self.get_run_dir(run_id) / "nodes" / task_id
        node_dir.mkdir(parents=True, exist_ok=True)
        return node_dir

    def get_runtime_path(self, run_id: str, task_id: str) -> Path:
        return self.get_node_dir(run_id, task_id) / "runtime.json"

    def save_runtime(
        self,
        run_id: str,
        task_id: str,
        runtime: NodeExecutionRuntime,
    ) -> None:
        _write_json(
            self.get_runtime_path(run_id, task_id),
            runtime.model_dump(mode="json"),
        )

    def maybe_get_runtime(
        self,
        run_id: str,
        task_id: str,
    ) -> NodeExecutionRuntime | None:
        path = self.get_runtime_path(run_id, task_id)
        if not path.exists():
            return None
        payload = _read_json(path)
        return NodeExecutionRuntime.model_validate(payload)

    def get_run_assistant_dir(self, run_id: str) -> Path:
        assistant_dir = self.get_run_dir(run_id) / "assistant"
        assistant_dir.mkdir(parents=True, exist_ok=True)
        return assistant_dir

    def get_assistant_request_path(self, run_id: str, task_id: str) -> Path:
        return self.get_node_dir(run_id, task_id) / "assistant_request.json"

    def get_assistant_response_path(self, run_id: str, task_id: str) -> Path:
        return self.get_node_dir(run_id, task_id) / "assistant_response.json"

    def get_assistant_control_action_path(self, run_id: str, task_id: str) -> Path:
        return self.get_node_dir(run_id, task_id) / "assistant_control_action.json"

    def get_guidance_proposal_path(self, run_id: str) -> Path:
        return self.get_run_assistant_dir(run_id) / "guidance_update_proposal.md"

    def get_manual_gate_path(self, run_id: str, task_id: str) -> Path:
        return self.get_node_dir(run_id, task_id) / "manual_gate.json"

    def get_human_request_path(self, run_id: str, task_id: str) -> Path:
        return self.get_node_dir(run_id, task_id) / "human_request.json"

    def get_human_response_path(self, run_id: str, task_id: str) -> Path:
        return self.get_node_dir(run_id, task_id) / "human_response.json"

    def create_assistant_request(
        self,
        *,
        run_id: str,
        task_id: str,
        request_kind: RequestKind,
        question: str,
        decision_kind: DecisionKind,
        options: list[str],
        context_artifacts: list[str],
        requested_control_actions: list[ControlActionKind],
        priority: RequestPriority,
    ) -> AssistantRequest:
        existing = self.maybe_get_assistant_request(run_id, task_id)
        if existing is not None:
            return existing
        request = AssistantRequest(
            request_id=self._new_event_id(prefix="req"),
            run_id=run_id,
            requester_task_id=task_id,
            request_kind=request_kind,
            question=question,
            decision_kind=decision_kind,
            options=options,
            context_artifacts=context_artifacts,
            requested_control_actions=requested_control_actions,
            priority=priority,
        )
        self.save_assistant_request(run_id, task_id, request)
        return request

    def save_assistant_request(
        self,
        run_id: str,
        task_id: str,
        request: AssistantRequest,
    ) -> None:
        _write_json(
            self.get_assistant_request_path(run_id, task_id),
            request.model_dump(mode="json"),
        )

    def maybe_get_assistant_request(
        self, run_id: str, task_id: str
    ) -> AssistantRequest | None:
        path = self.get_assistant_request_path(run_id, task_id)
        if not path.exists():
            return None
        payload = _read_json(path)
        return AssistantRequest.model_validate(payload)

    def maybe_get_assistant_response(
        self, run_id: str, task_id: str
    ) -> AssistantResponse | None:
        path = self.get_assistant_response_path(run_id, task_id)
        if not path.exists():
            return None
        payload = _read_json(path)
        return AssistantResponse.model_validate(payload)

    def maybe_get_assistant_control_action(
        self, run_id: str, task_id: str
    ) -> AssistantControlAction | None:
        path = self.get_assistant_control_action_path(run_id, task_id)
        if not path.exists():
            return None
        payload = _read_json(path)
        return AssistantControlAction.model_validate(payload)

    def maybe_get_manual_gate(self, run_id: str, task_id: str) -> ManualGate | None:
        path = self.get_manual_gate_path(run_id, task_id)
        if not path.exists():
            return None
        payload = _read_json(path)
        return ManualGate.model_validate(payload)

    def maybe_get_human_request(self, run_id: str, task_id: str) -> HumanRequest | None:
        path = self.get_human_request_path(run_id, task_id)
        if not path.exists():
            return None
        payload = _read_json(path)
        return HumanRequest.model_validate(payload)

    def maybe_get_human_response(self, run_id: str, task_id: str) -> HumanResponse | None:
        path = self.get_human_response_path(run_id, task_id)
        if not path.exists():
            return None
        payload = _read_json(path)
        return HumanResponse.model_validate(payload)

    def save_assistant_response_by_request_id(
        self,
        request_id: str,
        *,
        resolution_kind: ResolutionKind,
        answer: str,
        rationale: str,
        confidence: ConfidenceLevel,
        citations: list[str],
        proposed_guidance_updates: list[str],
        proposed_control_actions: list[ControlActionKind],
    ) -> AssistantResponse:
        record = self.find_assistant_request(request_id)
        response = AssistantResponse(
            request_id=request_id,
            resolution_kind=resolution_kind,
            answer=answer,
            rationale=rationale,
            confidence=confidence,
            citations=citations,
            proposed_guidance_updates=proposed_guidance_updates,
            proposed_control_actions=proposed_control_actions,
        )
        _write_json(
            self.get_assistant_response_path(record.run_id, record.task_id),
            response.model_dump(mode="json"),
        )
        if resolution_kind is ResolutionKind.HANDOFF_TO_HUMAN:
            self.ensure_manual_gate_for_request(request_id)
        return response

    def save_assistant_control_action_by_request_id(
        self,
        request_id: str,
        *,
        requested_by: ControlActor,
        action_kind: ControlActionKind,
        target_kind: str | None,
        target_path: str | None,
        payload: dict[str, object],
        reason: str,
        approval_mode: ApprovalMode,
    ) -> AssistantControlAction:
        record = self.find_assistant_request(request_id)
        target = None
        if target_kind is not None:
            target = ControlTarget(kind=target_kind, path=target_path)
        action = AssistantControlAction(
            action_id=self._new_event_id(prefix="act"),
            request_id=request_id,
            requested_by=requested_by,
            action_kind=action_kind,
            target=target,
            payload=payload,
            reason=reason,
            approval_mode=approval_mode,
        )
        _write_json(
            self.get_assistant_control_action_path(record.run_id, record.task_id),
            action.model_dump(mode="json"),
        )
        return action

    def update_assistant_control_action_status(
        self,
        request_id: str,
        status: ControlActionStatus,
    ) -> AssistantControlAction:
        record = self.find_assistant_request(request_id)
        action = record.control_action
        if action is None:
            raise KeyError(f"request {request_id} has no control action")
        updates: dict[str, ControlActionStatus | str] = {"status": status}
        if status in {
            ControlActionStatus.APPROVED,
            ControlActionStatus.APPLIED,
            ControlActionStatus.REJECTED,
            ControlActionStatus.FAILED,
        }:
            updates["applied_at"] = datetime.now(UTC).isoformat()
        updated = action.model_copy(update=updates)
        _write_json(
            self.get_assistant_control_action_path(record.run_id, record.task_id),
            updated.model_dump(mode="json"),
        )
        return updated

    def ensure_manual_gate_for_request(self, request_id: str) -> ManualGate:
        record = self.find_assistant_request(request_id)
        response = record.response
        if response is None:
            raise KeyError(f"request {request_id} has no assistant response")
        if response.resolution_kind is not ResolutionKind.HANDOFF_TO_HUMAN:
            raise ValueError(
                f"request {request_id} is not a handoff_to_human response"
            )
        gate = self.maybe_get_manual_gate(record.run_id, record.task_id)
        if gate is None:
            gate = ManualGate(
                gate_id=self._new_event_id(prefix="gate"),
                request_id=request_id,
                run_id=record.run_id,
                requester_task_id=record.task_id,
                reason=ManualGateReason.HANDOFF_TO_HUMAN,
            )
            self._save_manual_gate(record.run_id, record.task_id, gate)
        if self.maybe_get_human_request(record.run_id, record.task_id) is None:
            human_request = HumanRequest(
                gate_id=gate.gate_id,
                request_id=request_id,
                run_id=record.run_id,
                requester_task_id=record.task_id,
                question=record.request.question,
                assistant_summary=response.answer,
                assistant_rationale=response.rationale,
                citations=response.citations,
                context_artifacts=record.request.context_artifacts,
            )
            _write_json(
                self.get_human_request_path(record.run_id, record.task_id),
                human_request.model_dump(mode="json"),
            )
        return gate

    def save_human_response_by_request_id(
        self,
        request_id: str,
        *,
        answer: str,
    ) -> HumanResponse:
        record = self.find_assistant_request(request_id)
        gate = self.ensure_manual_gate_for_request(request_id)
        response = HumanResponse(
            gate_id=gate.gate_id,
            request_id=request_id,
            answer=answer,
        )
        _write_json(
            self.get_human_response_path(record.run_id, record.task_id),
            response.model_dump(mode="json"),
        )
        if gate.status in {
            ManualGateStatus.WAITING_FOR_HUMAN,
            ManualGateStatus.ANSWERED,
        }:
            gate = gate.model_copy(
                update={
                    "status": ManualGateStatus.ANSWERED,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            self._save_manual_gate(record.run_id, record.task_id, gate)
        return response

    def update_manual_gate_status_by_request_id(
        self,
        request_id: str,
        status: ManualGateStatus,
    ) -> ManualGate:
        record = self.find_assistant_request(request_id)
        gate = self.maybe_get_manual_gate(record.run_id, record.task_id)
        if gate is None:
            raise KeyError(f"request {request_id} has no manual gate")
        if status in {
            ManualGateStatus.APPROVED,
            ManualGateStatus.REJECTED,
        } and self.maybe_get_human_response(record.run_id, record.task_id) is None:
            raise ValueError("manual gate requires human_response.json before approval")
        updates: dict[str, ManualGateStatus | str] = {
            "status": status,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if status in {
            ManualGateStatus.APPROVED,
            ManualGateStatus.REJECTED,
            ManualGateStatus.APPLIED,
            ManualGateStatus.FAILED,
        }:
            updates["resolved_at"] = datetime.now(UTC).isoformat()
        updated = gate.model_copy(update=updates)
        self._save_manual_gate(record.run_id, record.task_id, updated)
        return updated

    def list_manual_gates(
        self,
        *,
        run_id: str | None = None,
        unresolved_only: bool = False,
    ) -> list[ManualGateRecord]:
        records: list[ManualGateRecord] = []
        run_dirs = (
            [self.get_run_dir(run_id)]
            if run_id is not None
            else list(sorted(self.paths.runs_dir.glob("*")))
        )
        for run_dir in run_dirs:
            if not run_dir.exists():
                continue
            resolved_run_id = run_dir.name
            nodes_root = run_dir / "nodes"
            if not nodes_root.exists():
                continue
            for node_dir in sorted(nodes_root.iterdir()):
                gate = self.maybe_get_manual_gate(resolved_run_id, node_dir.name)
                if gate is None:
                    continue
                if unresolved_only and gate.status not in {
                    ManualGateStatus.WAITING_FOR_HUMAN,
                    ManualGateStatus.ANSWERED,
                }:
                    continue
                records.append(
                    ManualGateRecord(
                        run_id=resolved_run_id,
                        task_id=node_dir.name,
                        node_dir=node_dir,
                        gate=gate,
                        human_request=self.maybe_get_human_request(
                            resolved_run_id,
                            node_dir.name,
                        ),
                        human_response=self.maybe_get_human_response(
                            resolved_run_id,
                            node_dir.name,
                        ),
                    )
                )
        return records

    def find_manual_gate_by_request_id(self, request_id: str) -> ManualGateRecord:
        record = self.find_assistant_request(request_id)
        gate = self.maybe_get_manual_gate(record.run_id, record.task_id)
        if gate is None:
            raise KeyError(f"request {request_id} has no manual gate")
        return ManualGateRecord(
            run_id=record.run_id,
            task_id=record.task_id,
            node_dir=record.node_dir,
            gate=gate,
            human_request=self.maybe_get_human_request(record.run_id, record.task_id),
            human_response=self.maybe_get_human_response(record.run_id, record.task_id),
        )

    def find_manual_gate_by_gate_id(self, gate_id: str) -> ManualGateRecord:
        for record in self.list_manual_gates():
            if record.gate.gate_id == gate_id:
                return record
        raise KeyError(f"manual gate {gate_id} does not exist")

    def list_assistant_requests(
        self,
        *,
        run_id: str | None = None,
        unresolved_only: bool = False,
    ) -> list[AssistantRequestRecord]:
        records: list[AssistantRequestRecord] = []
        run_dirs = [self.get_run_dir(run_id)] if run_id is not None else list(
            sorted(self.paths.runs_dir.glob("*"))
        )
        for run_dir in run_dirs:
            if not run_dir.exists():
                continue
            resolved_run_id = run_dir.name
            nodes_root = run_dir / "nodes"
            if not nodes_root.exists():
                continue
            for node_dir in sorted(nodes_root.iterdir()):
                request_path = node_dir / "assistant_request.json"
                if not request_path.exists():
                    continue
                request = AssistantRequest.model_validate(_read_json(request_path))
                response = self.maybe_get_assistant_response(resolved_run_id, node_dir.name)
                action = self.maybe_get_assistant_control_action(
                    resolved_run_id, node_dir.name
                )
                if unresolved_only and response is not None:
                    continue
                records.append(
                    AssistantRequestRecord(
                        run_id=resolved_run_id,
                        task_id=node_dir.name,
                        node_dir=node_dir,
                        request=request,
                        response=response,
                        control_action=action,
                    )
                )
        return records

    def find_assistant_request(self, request_id: str) -> AssistantRequestRecord:
        for record in self.list_assistant_requests():
            if record.request.request_id == request_id:
                return record
        raise KeyError(f"assistant request {request_id} does not exist")

    def assistant_request_pending(self, run_id: str, task_id: str) -> bool:
        request = self.maybe_get_assistant_request(run_id, task_id)
        if request is None:
            return False
        response = self.maybe_get_assistant_response(run_id, task_id)
        return response is None

    def manual_gate_requires_human(self, run_id: str, task_id: str) -> bool:
        gate = self.maybe_get_manual_gate(run_id, task_id)
        if gate is None:
            return False
        return gate.status in {
            ManualGateStatus.WAITING_FOR_HUMAN,
            ManualGateStatus.ANSWERED,
        }

    def _task_path(self, task_id: str) -> Path:
        return self.paths.tasks_dir / f"{task_id}.yaml"

    def _save_manual_gate(self, run_id: str, task_id: str, gate: ManualGate) -> None:
        _write_json(
            self.get_manual_gate_path(run_id, task_id),
            gate.model_dump(mode="json"),
        )

    def _new_event_id(self, *, prefix: str) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        suffix = uuid.uuid4().hex[:8]
        return f"{prefix}_{timestamp}_{suffix}"
