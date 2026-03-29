from __future__ import annotations

import asyncio
import json
import os
import signal
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from prefect import flow, task
from prefect.futures import PrefectFuture
from prefect.runtime import flow_run as prefect_flow_run
from prefect.task_runners import ThreadPoolTaskRunner

from codex_orch.domain import (
    ComposeStepKind,
    DependencyKind,
    ManualGateStatus,
    NodeExecutionRuntime,
    NodeExecutionTerminationReason,
    ProjectSpec,
    PublishedArtifact,
    ResolutionKind,
    RunNodeState,
    RunNodeStatus,
    RunNodeWaitReason,
    RunSnapshot,
    RunStatus,
    TaskSpec,
    TaskStatus,
)
from codex_orch.prompt_context import (
    ensure_staged_assistant_artifact,
    ensure_staged_compose_program_file,
)
from codex_orch.runner import NodeExecutionRequest, NodeExecutionResult, TaskRunner
from codex_orch.scheduler.composer import PromptComposer
from codex_orch.store import ProjectStore
from codex_orch.task_pool import TaskPoolService


@dataclass
class _RunFlowContext:
    project: ProjectSpec
    snapshot: RunSnapshot
    lock: threading.Lock


@dataclass(frozen=True)
class NodeExecutionOutcome:
    task_id: str
    status: RunNodeStatus
    error: str | None = None


class RunService:
    def __init__(self, store: ProjectStore, runner: TaskRunner) -> None:
        self.store = store
        self.runner = runner
        self.task_pool = TaskPoolService(store)
        self.composer = PromptComposer(store.paths.root)

    def create_snapshot(
        self,
        *,
        roots: list[str],
        labels: list[str],
        user_inputs: dict[str, str] | None = None,
    ) -> RunSnapshot:
        self.task_pool.validate_graph()
        project = self.store.load_project()
        selected = self.task_pool.select_subgraph(roots=roots, labels=labels)
        merged_inputs = self.store.load_default_user_inputs()
        if user_inputs is not None:
            merged_inputs.update(user_inputs)

        nodes = {
            task_id: RunNodeState(task=self._materialize_task(project, task))
            for task_id, task in selected.items()
        }
        run_id = self._new_run_id()
        snapshot = RunSnapshot(
            id=run_id,
            roots=sorted(roots),
            user_inputs=merged_inputs,
            nodes=nodes,
        )
        self._stage_snapshot_compose_context(snapshot)
        self.store.save_run(snapshot)
        return snapshot

    async def start_run(
        self,
        *,
        roots: list[str],
        labels: list[str],
        user_inputs: dict[str, str] | None = None,
    ) -> RunSnapshot:
        snapshot = self.create_snapshot(
            roots=roots,
            labels=labels,
            user_inputs=user_inputs,
        )
        return await self.run_snapshot(snapshot.id)

    async def reconcile_run(self, run_id: str) -> RunSnapshot:
        return await asyncio.to_thread(self._reconcile_run_sync, run_id)

    async def abort_run(self, run_id: str) -> RunSnapshot:
        return await asyncio.to_thread(self._abort_run_sync, run_id)

    async def resume_run(self, run_id: str) -> RunSnapshot:
        snapshot = await self.reconcile_run(run_id)
        if any(node.status is RunNodeStatus.RUNNING for node in snapshot.nodes.values()):
            return snapshot
        for node in snapshot.nodes.values():
            if node.status is RunNodeStatus.DONE:
                continue
            wait_reason = self._wait_reason_for_node(snapshot.id, node.task.id)
            gate_error = self._manual_gate_terminal_error(snapshot.id, node.task.id)
            if gate_error is not None:
                node.status = RunNodeStatus.FAILED
                node.waiting_reason = None
                node.error = gate_error
                self._set_task_pool_status(node.task.id, TaskStatus.FAILED)
                continue
            if node.status is RunNodeStatus.WAITING and wait_reason is not None:
                node.waiting_reason = wait_reason
                node.error = self._wait_reason_message(wait_reason)
                self._set_task_pool_status(node.task.id, TaskStatus.BLOCKED)
                continue
            node.status = RunNodeStatus.PENDING
            node.waiting_reason = None
            node.error = None
            node.started_at = None
            node.finished_at = None
            node.termination_reason = None
            self._set_task_pool_status(node.task.id, TaskStatus.READY)
        snapshot.status = RunStatus.PENDING
        self.store.save_run(snapshot)
        return await self.run_snapshot(run_id)

    async def run_snapshot(self, run_id: str) -> RunSnapshot:
        return await asyncio.to_thread(self._run_snapshot_sync, run_id)

    def _reconcile_run_sync(self, run_id: str) -> RunSnapshot:
        snapshot = self.store.get_run(run_id)
        project = self.store.load_project()
        changed = False
        for task_id, node in snapshot.nodes.items():
            runtime = self.store.maybe_get_runtime(run_id, task_id)
            if node.status is not RunNodeStatus.RUNNING:
                continue
            if runtime is None:
                self._mark_node_failed(
                    snapshot,
                    task_id,
                    error="node runtime is missing while task is marked running",
                    termination_reason=NodeExecutionTerminationReason.ORPHANED,
                    finished_at=datetime.now(UTC).isoformat(),
                )
                changed = True
                continue
            if runtime.finished_at is not None:
                self._reconcile_finished_runtime(snapshot, task_id, runtime)
                changed = True
                continue
            pid = runtime.pid
            if pid is None or not self._pid_exists(pid):
                runtime.finished_at = datetime.now(UTC).isoformat()
                runtime.return_code = -1
                runtime.termination_reason = NodeExecutionTerminationReason.ORPHANED
                self.store.save_runtime(run_id, task_id, runtime)
                self._mark_node_failed(
                    snapshot,
                    task_id,
                    error="worker process disappeared while node was marked running",
                    termination_reason=NodeExecutionTerminationReason.ORPHANED,
                    finished_at=runtime.finished_at,
                )
                changed = True
                continue
            stale_reason = self._stale_runtime_reason(runtime)
            if stale_reason is None:
                continue
            self._terminate_pid(pid, project.node_terminate_grace_sec)
            runtime.finished_at = datetime.now(UTC).isoformat()
            runtime.return_code = -1
            runtime.termination_reason = stale_reason
            self.store.save_runtime(run_id, task_id, runtime)
            self._mark_node_failed(
                snapshot,
                task_id,
                error=self._termination_reason_message(stale_reason),
                termination_reason=stale_reason,
                finished_at=runtime.finished_at,
            )
            changed = True
        if changed:
            self._finalize_snapshot_state(snapshot)
            self.store.save_run(snapshot)
        return snapshot

    def _abort_run_sync(self, run_id: str) -> RunSnapshot:
        snapshot = self.store.get_run(run_id)
        project = self.store.load_project()
        changed = False
        finished_at = datetime.now(UTC).isoformat()
        for task_id, node in snapshot.nodes.items():
            if node.status is RunNodeStatus.DONE:
                continue
            if node.status is RunNodeStatus.SKIPPED:
                continue
            if node.status is RunNodeStatus.FAILED:
                continue
            runtime = self.store.maybe_get_runtime(run_id, task_id)
            if node.status is RunNodeStatus.RUNNING and runtime is not None and runtime.pid is not None:
                self._terminate_pid(runtime.pid, project.node_terminate_grace_sec)
                runtime.finished_at = finished_at
                runtime.return_code = -1
                runtime.termination_reason = NodeExecutionTerminationReason.TERMINATED
                self.store.save_runtime(run_id, task_id, runtime)
            if node.status is RunNodeStatus.PENDING:
                node.status = RunNodeStatus.SKIPPED
                node.error = "run aborted by user"
            else:
                node.status = RunNodeStatus.FAILED
                node.error = "run aborted by user"
            node.waiting_reason = None
            node.finished_at = finished_at
            node.termination_reason = NodeExecutionTerminationReason.TERMINATED
            self._set_task_pool_status(node.task.id, TaskStatus.FAILED)
            self._write_node_meta(run_id, task_id, snapshot)
            changed = True
        if changed:
            snapshot.status = RunStatus.FAILED
            self.store.save_run(snapshot)
        return snapshot

    def _run_snapshot_sync(self, run_id: str) -> RunSnapshot:
        snapshot = self.store.get_run(run_id)
        project = self.store.load_project()
        run_context = _RunFlowContext(
            project=project,
            snapshot=snapshot,
            lock=threading.Lock(),
        )

        @task
        def execute_node(task_id: str) -> NodeExecutionOutcome:
            return self._execute_node(run_context, task_id)

        @flow(
            name="codex-orch-run",
            task_runner=ThreadPoolTaskRunner(max_workers=project.max_concurrency),
        )
        def run_flow() -> None:
            self._mark_snapshot_running(run_context)
            self._attach_prefect_flow_metadata(run_context)
            futures: dict[str, PrefectFuture[NodeExecutionOutcome]] = {}
            for task_id in self._topological_order(run_context.snapshot):
                dependency_futures = [
                    futures[dependency.task]
                    for dependency in run_context.snapshot.nodes[task_id].task.depends_on
                    if dependency.task in futures
                ]
                futures[task_id] = execute_node.with_options(
                    name=f"node-{task_id}"
                ).submit(task_id, wait_for=dependency_futures)
            for future in futures.values():
                future.result()
            self._finalize_snapshot(run_context)

        run_flow()
        return self.store.get_run(run_id)

    def _mark_snapshot_running(self, run_context: _RunFlowContext) -> None:
        with run_context.lock:
            run_context.snapshot.status = RunStatus.RUNNING
            self.store.save_run(run_context.snapshot)

    def _attach_prefect_flow_metadata(self, run_context: _RunFlowContext) -> None:
        flow_run_id = prefect_flow_run.get_id()
        with run_context.lock:
            run_context.snapshot.prefect_flow_run_id = flow_run_id
            self.store.save_run(run_context.snapshot)

    def _execute_node(
        self,
        run_context: _RunFlowContext,
        task_id: str,
    ) -> NodeExecutionOutcome:
        try:
            return self._execute_node_inner(run_context, task_id)
        except Exception as exc:  # pragma: no cover - defensive guard
            with run_context.lock:
                node = run_context.snapshot.nodes[task_id]
                node.status = RunNodeStatus.FAILED
                node.waiting_reason = None
                node.error = str(exc)
                node.finished_at = datetime.now(UTC).isoformat()
                self._set_task_pool_status(node.task.id, TaskStatus.FAILED)
                self._write_node_meta(run_context.snapshot.id, task_id, run_context.snapshot)
                self.store.save_run(run_context.snapshot)
            return NodeExecutionOutcome(
                task_id=task_id,
                status=RunNodeStatus.FAILED,
                error=str(exc),
            )

    def _execute_node_inner(
        self,
        run_context: _RunFlowContext,
        task_id: str,
    ) -> NodeExecutionOutcome:
        with run_context.lock:
            snapshot = run_context.snapshot
            node = snapshot.nodes[task_id]
            if self._published_artifacts_complete(snapshot.id, node.task):
                node.published = self._load_published_artifacts(node.task)
                node.status = RunNodeStatus.DONE
                node.waiting_reason = None
                node.error = None
                self._set_task_pool_status(node.task.id, TaskStatus.DONE)
                self._write_node_meta(snapshot.id, task_id, snapshot)
                self.store.save_run(snapshot)
                return NodeExecutionOutcome(task_id=task_id, status=node.status)

            if self._dependency_failed(snapshot, node.task):
                node.status = RunNodeStatus.SKIPPED
                node.waiting_reason = None
                node.error = "upstream dependency failed"
                node.finished_at = datetime.now(UTC).isoformat()
                self._set_task_pool_status(node.task.id, TaskStatus.BLOCKED)
                self._write_node_meta(snapshot.id, task_id, snapshot)
                self.store.save_run(snapshot)
                return NodeExecutionOutcome(task_id=task_id, status=node.status, error=node.error)

            if not self._dependencies_satisfied(snapshot, node.task):
                return NodeExecutionOutcome(task_id=task_id, status=node.status)

            gate_error = self._manual_gate_terminal_error(snapshot.id, task_id)
            if gate_error is not None:
                node.status = RunNodeStatus.FAILED
                node.waiting_reason = None
                node.error = gate_error
                node.finished_at = datetime.now(UTC).isoformat()
                self._set_task_pool_status(node.task.id, TaskStatus.FAILED)
                self._write_node_meta(snapshot.id, task_id, snapshot)
                self.store.save_run(snapshot)
                return NodeExecutionOutcome(
                    task_id=task_id,
                    status=node.status,
                    error=node.error,
                )

            wait_reason = self._wait_reason_for_node(snapshot.id, task_id)
            if wait_reason is not None:
                node.status = RunNodeStatus.WAITING
                node.waiting_reason = wait_reason
                node.error = self._wait_reason_message(wait_reason)
                self._set_task_pool_status(node.task.id, TaskStatus.BLOCKED)
                self._write_node_meta(snapshot.id, task_id, snapshot)
                self.store.save_run(snapshot)
                return NodeExecutionOutcome(
                    task_id=task_id,
                    status=node.status,
                    error=node.error,
                )

            dependency_dirs = {
                dependency.task: self.store.get_node_dir(snapshot.id, dependency.task)
                for dependency in node.task.depends_on
                if snapshot.nodes[dependency.task].status is RunNodeStatus.DONE
            }
            user_inputs = dict(snapshot.user_inputs)
            task = node.task
            node.status = RunNodeStatus.RUNNING
            node.waiting_reason = None
            node.error = None
            node.attempt += 1
            node.termination_reason = None
            node.started_at = datetime.now(UTC).isoformat()
            self._set_task_pool_status(node.task.id, TaskStatus.RUNNING)
            self._write_node_meta(snapshot.id, task_id, snapshot)
            self.store.save_run(snapshot)

        self._consume_approved_manual_gate(run_context.snapshot.id, task_id)

        result = asyncio.run(
            self._run_single_node(
                project=run_context.project,
                run_id=run_context.snapshot.id,
                task=task,
                user_inputs=user_inputs,
                dependency_dirs=dependency_dirs,
            )
        )

        with run_context.lock:
            snapshot = run_context.snapshot
            node = snapshot.nodes[task_id]
            node.finished_at = datetime.now(UTC).isoformat()
            node.termination_reason = self._result_termination_reason(result)
            gate_error = self._manual_gate_terminal_error(snapshot.id, task_id)
            wait_reason = self._wait_reason_for_node(snapshot.id, task_id)
            if gate_error is not None:
                node.status = RunNodeStatus.FAILED
                node.waiting_reason = None
                node.error = gate_error
                self._set_task_pool_status(node.task.id, TaskStatus.FAILED)
            elif wait_reason is not None:
                node.status = RunNodeStatus.WAITING
                node.waiting_reason = wait_reason
                node.error = self._wait_reason_message(wait_reason)
                self._set_task_pool_status(node.task.id, TaskStatus.BLOCKED)
            elif result.success:
                try:
                    node.published = self._promote_artifacts(snapshot.id, node.task)
                except ValueError as exc:
                    node.status = RunNodeStatus.FAILED
                    node.waiting_reason = None
                    node.error = str(exc)
                    self._set_task_pool_status(node.task.id, TaskStatus.FAILED)
                else:
                    node.status = RunNodeStatus.DONE
                    node.waiting_reason = None
                    node.error = None
                    self._set_task_pool_status(node.task.id, TaskStatus.DONE)
            else:
                node.status = RunNodeStatus.FAILED
                node.waiting_reason = None
                node.error = result.error
                self._set_task_pool_status(node.task.id, TaskStatus.FAILED)
            self._write_node_meta(snapshot.id, task_id, snapshot)
            self.store.save_run(snapshot)
            return NodeExecutionOutcome(task_id=task_id, status=node.status, error=node.error)

    async def _run_single_node(
        self,
        *,
        project: ProjectSpec,
        run_id: str,
        task: TaskSpec,
        user_inputs: dict[str, str],
        dependency_dirs: dict[str, Path],
    ) -> NodeExecutionResult:
        node_dir = self.store.get_node_dir(run_id, task.id)
        prompt = self.composer.render(
            task,
            node_dir=node_dir,
            user_inputs=user_inputs,
            dependency_node_dirs=dependency_dirs,
        )
        project_workspace_dir = self._resolve_project_workspace_dir(project)
        workspace_dir = self._resolve_task_workspace_dir(project, task)
        extra_writable_roots = tuple(
            Path(root)
            for root in self._resolve_task_extra_writable_roots(task, workspace_dir)
        )
        appendices = [
            self._execution_contract_appendix(
                task=task,
                node_dir=node_dir,
                project_workspace_dir=project_workspace_dir,
                workspace_dir=workspace_dir,
                extra_writable_roots=extra_writable_roots,
            ),
            self._assistant_auto_reply_prompt_appendix(run_id, task.id),
            self._manual_gate_prompt_appendix(run_id, task.id),
        ]
        prompt_sections = [prompt, *appendices]
        prompt = "\n\n".join(section for section in prompt_sections if section)
        return await self.runner.run(
            NodeExecutionRequest(
                run_id=run_id,
                program_dir=self.store.paths.root,
                project_workspace_dir=project_workspace_dir,
                workspace_dir=workspace_dir,
                extra_writable_roots=extra_writable_roots,
                node_dir=node_dir,
                project=project,
                task=task,
                prompt=prompt,
            )
        )

    def _finalize_snapshot(self, run_context: _RunFlowContext) -> None:
        with run_context.lock:
            snapshot = run_context.snapshot
            self._finalize_snapshot_state(snapshot)
            self.store.save_run(snapshot)

    def _finalize_snapshot_state(self, snapshot: RunSnapshot) -> None:
        self._mark_blocked_nodes(snapshot)
        statuses = {node.status for node in snapshot.nodes.values()}
        if RunNodeStatus.RUNNING in statuses:
            snapshot.status = RunStatus.RUNNING
            return
        if RunNodeStatus.FAILED in statuses:
            snapshot.status = RunStatus.FAILED
            return
        if RunNodeStatus.WAITING in statuses or RunNodeStatus.PENDING in statuses:
            snapshot.status = RunStatus.WAITING
            return
        if all(status is RunNodeStatus.DONE for status in statuses):
            snapshot.status = RunStatus.DONE
            return
        snapshot.status = RunStatus.FAILED

    def _mark_node_failed(
        self,
        snapshot: RunSnapshot,
        task_id: str,
        *,
        error: str,
        termination_reason: NodeExecutionTerminationReason,
        finished_at: str,
    ) -> None:
        node = snapshot.nodes[task_id]
        node.status = RunNodeStatus.FAILED
        node.waiting_reason = None
        node.error = error
        node.finished_at = finished_at
        node.termination_reason = termination_reason
        self._set_task_pool_status(node.task.id, TaskStatus.FAILED)
        self._write_node_meta(snapshot.id, task_id, snapshot)

    def _reconcile_finished_runtime(
        self,
        snapshot: RunSnapshot,
        task_id: str,
        runtime: NodeExecutionRuntime,
    ) -> None:
        node = snapshot.nodes[task_id]
        termination_reason = self._resolve_runtime_termination_reason(runtime)
        node.finished_at = runtime.finished_at
        node.termination_reason = termination_reason
        if termination_reason is NodeExecutionTerminationReason.COMPLETED:
            try:
                node.published = self._promote_artifacts(snapshot.id, node.task)
            except ValueError as exc:
                self._mark_node_failed(
                    snapshot,
                    task_id,
                    error=str(exc),
                    termination_reason=NodeExecutionTerminationReason.NONZERO_EXIT,
                    finished_at=runtime.finished_at or datetime.now(UTC).isoformat(),
                )
            else:
                node.status = RunNodeStatus.DONE
                node.waiting_reason = None
                node.error = None
                self._set_task_pool_status(node.task.id, TaskStatus.DONE)
                self._write_node_meta(snapshot.id, task_id, snapshot)
            return
        self._mark_node_failed(
            snapshot,
            task_id,
            error=self._termination_reason_message(termination_reason),
            termination_reason=termination_reason,
            finished_at=runtime.finished_at or datetime.now(UTC).isoformat(),
        )

    def _resolve_runtime_termination_reason(
        self,
        runtime: NodeExecutionRuntime,
    ) -> NodeExecutionTerminationReason:
        if runtime.termination_reason is not None:
            return runtime.termination_reason
        if runtime.return_code == 0:
            return NodeExecutionTerminationReason.COMPLETED
        if runtime.return_code is not None and runtime.return_code < 0:
            return NodeExecutionTerminationReason.TERMINATED
        return NodeExecutionTerminationReason.NONZERO_EXIT

    def _result_termination_reason(
        self,
        result: NodeExecutionResult,
    ) -> NodeExecutionTerminationReason:
        if result.termination_reason is not None:
            return result.termination_reason
        if result.success:
            return NodeExecutionTerminationReason.COMPLETED
        if result.return_code < 0:
            return NodeExecutionTerminationReason.TERMINATED
        return NodeExecutionTerminationReason.NONZERO_EXIT

    def _stale_runtime_reason(
        self,
        runtime: NodeExecutionRuntime,
    ) -> NodeExecutionTerminationReason | None:
        now = datetime.now(UTC)
        started_at = datetime.fromisoformat(runtime.started_at)
        if runtime.wall_timeout_sec is not None:
            if (now - started_at).total_seconds() >= runtime.wall_timeout_sec:
                return NodeExecutionTerminationReason.WALL_TIMEOUT
        progress_at_raw = runtime.last_progress_at or runtime.started_at
        progress_at = datetime.fromisoformat(progress_at_raw)
        if runtime.idle_timeout_sec is not None:
            if (now - progress_at).total_seconds() >= runtime.idle_timeout_sec:
                return NodeExecutionTerminationReason.IDLE_TIMEOUT
        return None

    def _termination_reason_message(
        self,
        termination_reason: NodeExecutionTerminationReason,
    ) -> str:
        if termination_reason is NodeExecutionTerminationReason.WALL_TIMEOUT:
            return "codex exec exceeded wall timeout"
        if termination_reason is NodeExecutionTerminationReason.IDLE_TIMEOUT:
            return "codex exec exceeded idle timeout"
        if termination_reason is NodeExecutionTerminationReason.ORPHANED:
            return "worker process disappeared while node was running"
        if termination_reason is NodeExecutionTerminationReason.TERMINATED:
            return "codex exec terminated"
        return "codex exec failed"

    def _pid_exists(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _terminate_pid(self, pid: int, grace_seconds: float) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not self._pid_exists(pid):
                return
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return

    def _promote_artifacts(
        self,
        run_id: str,
        task: TaskSpec,
    ) -> list[PublishedArtifact]:
        node_dir = self.store.get_node_dir(run_id, task.id)
        published_dir = node_dir / "published"
        if published_dir.exists():
            shutil.rmtree(published_dir)
        published_dir.mkdir(parents=True, exist_ok=True)

        artifacts: list[PublishedArtifact] = []
        for relative_path in task.publish:
            source = node_dir / relative_path
            if not source.exists():
                raise ValueError(
                    f"task {task.id} declared publish file {relative_path} but it was not produced"
                )
            destination = published_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            artifacts.append(PublishedArtifact(relative_path=relative_path))
        return artifacts

    def _published_artifacts_complete(self, run_id: str, task: TaskSpec) -> bool:
        published_dir = self.store.get_node_dir(run_id, task.id) / "published"
        return all((published_dir / relative_path).exists() for relative_path in task.publish)

    def _load_published_artifacts(self, task: TaskSpec) -> list[PublishedArtifact]:
        return [
            PublishedArtifact(relative_path=relative_path)
            for relative_path in task.publish
        ]

    def _dependencies_satisfied(self, snapshot: RunSnapshot, task: TaskSpec) -> bool:
        for dependency in task.depends_on:
            upstream = snapshot.nodes[dependency.task]
            if upstream.status is not RunNodeStatus.DONE:
                return False
            if dependency.kind is DependencyKind.CONTEXT:
                published_paths = {artifact.relative_path for artifact in upstream.published}
                if any(consumed not in published_paths for consumed in dependency.consume):
                    return False
        return True

    def _dependency_failed(self, snapshot: RunSnapshot, task: TaskSpec) -> bool:
        for dependency in task.depends_on:
            upstream = snapshot.nodes[dependency.task]
            if upstream.status in {RunNodeStatus.FAILED, RunNodeStatus.SKIPPED}:
                return True
        return False

    def _mark_blocked_nodes(self, snapshot: RunSnapshot) -> None:
        for node in snapshot.nodes.values():
            if node.status is not RunNodeStatus.PENDING:
                continue
            if not self._dependency_failed(snapshot, node.task):
                continue
            node.status = RunNodeStatus.SKIPPED
            node.waiting_reason = None
            node.error = "upstream dependency failed"
            self._set_task_pool_status(node.task.id, TaskStatus.BLOCKED)
            self._write_node_meta(snapshot.id, node.task.id, snapshot)

    def _wait_reason_for_node(
        self,
        run_id: str,
        task_id: str,
    ) -> RunNodeWaitReason | None:
        request = self.store.maybe_get_assistant_request(run_id, task_id)
        if request is None:
            return None
        response = self.store.maybe_get_assistant_response(run_id, task_id)
        if response is None:
            return RunNodeWaitReason.ASSISTANT_PENDING
        if response.resolution_kind is ResolutionKind.AUTO_REPLY:
            return None
        if response.resolution_kind is ResolutionKind.HANDOFF_TO_HUMAN:
            gate = self.store.ensure_manual_gate_for_request(request.request_id)
            if gate.status is ManualGateStatus.WAITING_FOR_HUMAN:
                return RunNodeWaitReason.HANDOFF_TO_HUMAN
            if gate.status is ManualGateStatus.ANSWERED:
                return RunNodeWaitReason.MANUAL_GATE_BLOCKED
            if gate.status in {
                ManualGateStatus.APPROVED,
                ManualGateStatus.APPLIED,
            }:
                return None
            return RunNodeWaitReason.MANUAL_GATE_BLOCKED
        return RunNodeWaitReason.MANUAL_GATE_BLOCKED

    def _manual_gate_terminal_error(self, run_id: str, task_id: str) -> str | None:
        request = self.store.maybe_get_assistant_request(run_id, task_id)
        if request is None:
            return None
        gate = self.store.maybe_get_manual_gate(run_id, task_id)
        if gate is None:
            return None
        if gate.status is ManualGateStatus.REJECTED:
            return "manual gate rejected by human"
        if gate.status is ManualGateStatus.FAILED:
            return "manual gate failed"
        return None

    def _consume_approved_manual_gate(self, run_id: str, task_id: str) -> None:
        request = self.store.maybe_get_assistant_request(run_id, task_id)
        if request is None:
            return
        gate = self.store.maybe_get_manual_gate(run_id, task_id)
        if gate is None:
            return
        if gate.status is not ManualGateStatus.APPROVED:
            return
        self.store.update_manual_gate_status_by_request_id(
            request.request_id,
            ManualGateStatus.APPLIED,
        )

    def _execution_contract_appendix(
        self,
        *,
        task: TaskSpec,
        node_dir: Path,
        project_workspace_dir: Path,
        workspace_dir: Path,
        extra_writable_roots: tuple[Path, ...],
    ) -> str:
        sandbox = task.sandbox or "workspace-write"
        sections = [
            "## Execution Contract",
            "\n".join(
                [
                    f"- workspace_dir (cwd): `{workspace_dir}`",
                    f"- project_workspace_dir: `{project_workspace_dir}`",
                    f"- node_dir: `{node_dir}`",
                    f"- sandbox: `{sandbox}`",
                ]
            ),
        ]
        writable_roots = self._declared_writable_roots(
            sandbox=sandbox,
            workspace_dir=workspace_dir,
            node_dir=node_dir,
            extra_writable_roots=extra_writable_roots,
        )
        if writable_roots is None:
            sections.append(
                "### Writable Roots\n- unrestricted via `danger-full-access` sandbox"
            )
        elif writable_roots:
            sections.append(
                "### Writable Roots\n"
                + "\n".join(f"- `{root}`" for root in writable_roots)
            )
        else:
            sections.append("### Writable Roots\n- none")
        sections.append(
            "### Publish Targets\n"
            + "\n".join(f"- `{path}`" for path in task.publish)
        )
        if task.result_schema is not None:
            sections.append(f"### Result Schema\n- `{task.result_schema}`")
        sections.append(
            "\n".join(
                [
                    "### Assistant Helper Contract",
                    "- Use `codex-orch assistant request create` to create assistant requests.",
                    "- Runtime env vars are available: `CODEX_ORCH_PROGRAM_DIR`, `CODEX_ORCH_RUN_ID`, `CODEX_ORCH_TASK_ID`, `CODEX_ORCH_NODE_DIR`, `CODEX_ORCH_PROJECT_WORKSPACE_DIR`, `CODEX_ORCH_WORKSPACE_DIR`.",
                    "- Pass assistant artifact paths relative to `CODEX_ORCH_PROGRAM_DIR`.",
                ]
            )
        )
        return "\n\n".join(sections)

    def _assistant_auto_reply_prompt_appendix(
        self, run_id: str, task_id: str
    ) -> str | None:
        request = self.store.maybe_get_assistant_request(run_id, task_id)
        if request is None:
            return None
        response = self.store.maybe_get_assistant_response(run_id, task_id)
        if response is None:
            return None
        if response.resolution_kind is not ResolutionKind.AUTO_REPLY:
            return None

        sections = [
            "## Assistant Continuation Context",
            (
                "Apply this assistant answer only to this task continuation. "
                "Use it as the resolved assistant guidance for this task."
            ),
            f"### Original Question\n{request.question.rstrip()}",
            f"### Assistant Answer\n{response.answer.rstrip()}",
            f"### Assistant Rationale\n{response.rationale.rstrip()}",
        ]
        if response.citations:
            sections.append(
                "### Citations\n"
                + "\n".join(f"- {citation}" for citation in response.citations)
            )
        if request.context_artifacts:
            sections.append(
                "### Context Artifacts\n"
                + self._assistant_context_artifact_lines(run_id, task_id, request.context_artifacts)
            )
        return "\n\n".join(sections)

    def _manual_gate_prompt_appendix(self, run_id: str, task_id: str) -> str | None:
        gate = self.store.maybe_get_manual_gate(run_id, task_id)
        if gate is None:
            return None
        if gate.status not in {
            ManualGateStatus.APPROVED,
            ManualGateStatus.APPLIED,
        }:
            return None
        human_request = self.store.maybe_get_human_request(run_id, task_id)
        human_response = self.store.maybe_get_human_response(run_id, task_id)
        if human_request is None or human_response is None:
            return None

        sections = [
            "## Human-Approved Continuation Context",
            (
                "Apply this decision only to this task continuation. "
                "The human answer is authoritative."
            ),
            f"### Original Question\n{human_request.question.rstrip()}",
            f"### Assistant Summary\n{human_request.assistant_summary.rstrip()}",
            f"### Assistant Rationale\n{human_request.assistant_rationale.rstrip()}",
            f"### Human Answer\n{human_response.answer.rstrip()}",
        ]
        if human_request.citations:
            sections.append(
                "### Citations\n"
                + "\n".join(f"- {citation}" for citation in human_request.citations)
            )
        if human_request.context_artifacts:
            sections.append(
                "### Context Artifacts\n"
                + self._assistant_context_artifact_lines(
                    run_id,
                    task_id,
                    human_request.context_artifacts,
                )
            )
        return "\n\n".join(sections)

    def _assistant_context_artifact_lines(
        self,
        run_id: str,
        task_id: str,
        relative_paths: list[str],
    ) -> str:
        node_dir = self.store.get_node_dir(run_id, task_id)
        lines: list[str] = []
        for relative_path in relative_paths:
            staged = ensure_staged_assistant_artifact(
                program_dir=self.store.paths.root,
                node_dir=node_dir,
                relative_path=relative_path,
            )
            lines.append(
                f"- source: `{relative_path}`; staged_path: `{staged.staged_path}`"
            )
        return "\n".join(lines)

    def _declared_writable_roots(
        self,
        *,
        sandbox: str,
        workspace_dir: Path,
        node_dir: Path,
        extra_writable_roots: tuple[Path, ...],
    ) -> tuple[Path, ...] | None:
        if sandbox == "danger-full-access":
            return None
        if sandbox == "read-only":
            return tuple()
        candidates = [workspace_dir, node_dir, *extra_writable_roots]
        deduped: list[Path] = []
        seen: set[str] = set()
        for path in candidates:
            normalized = str(path)
            if normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(path)
        return tuple(deduped)

    def _wait_reason_message(self, reason: RunNodeWaitReason) -> str:
        if reason is RunNodeWaitReason.ASSISTANT_PENDING:
            return "assistant request is waiting for a reply"
        if reason is RunNodeWaitReason.HANDOFF_TO_HUMAN:
            return "assistant handed off to human gate"
        return "manual gate is waiting for approval"

    def _topological_order(self, snapshot: RunSnapshot) -> list[str]:
        ordered: list[str] = []
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visited:
                return
            visited.add(task_id)
            for dependency in snapshot.nodes[task_id].task.depends_on:
                if dependency.task in snapshot.nodes:
                    visit(dependency.task)
            ordered.append(task_id)

        for task_id in sorted(snapshot.nodes):
            visit(task_id)
        return ordered

    def _set_task_pool_status(self, task_id: str, status: TaskStatus) -> None:
        task = self.store.get_task(task_id)
        updated = task.model_copy(update={"status": status})
        self.store.save_task(updated)

    def _write_node_meta(self, run_id: str, task_id: str, snapshot: RunSnapshot) -> None:
        node_dir = self.store.get_node_dir(run_id, task_id)
        node = snapshot.nodes[task_id]
        runtime_path = self.store.get_runtime_path(run_id, task_id)
        runtime = self.store.maybe_get_runtime(run_id, task_id)
        assistant_request = self.store.get_assistant_request_path(run_id, task_id)
        assistant_response = self.store.get_assistant_response_path(run_id, task_id)
        assistant_action = self.store.get_assistant_control_action_path(run_id, task_id)
        manual_gate = self.store.get_manual_gate_path(run_id, task_id)
        human_request = self.store.get_human_request_path(run_id, task_id)
        human_response = self.store.get_human_response_path(run_id, task_id)
        meta = {
            "run_id": run_id,
            "task": node.task.model_dump(mode="json"),
            "status": node.status.value,
            "waiting_reason": None
            if node.waiting_reason is None
            else node.waiting_reason.value,
            "error": node.error,
            "attempt": node.attempt,
            "termination_reason": None
            if node.termination_reason is None
            else node.termination_reason.value,
            "started_at": node.started_at,
            "finished_at": node.finished_at,
            "published": [artifact.model_dump(mode="json") for artifact in node.published],
            "runtime": str(runtime_path) if runtime_path.exists() else None,
            "runtime_summary": None
            if runtime is None
            else {
                "pid": runtime.pid,
                "cwd": runtime.cwd,
                "project_workspace_dir": runtime.project_workspace_dir,
                "sandbox": runtime.sandbox,
                "writable_roots": runtime.writable_roots,
                "last_progress_at": runtime.last_progress_at,
                "last_event_summary": runtime.last_event_summary,
                "termination_reason": None
                if runtime.termination_reason is None
                else runtime.termination_reason.value,
                "wall_timeout_sec": runtime.wall_timeout_sec,
                "idle_timeout_sec": runtime.idle_timeout_sec,
            },
            "assistant_request": str(assistant_request) if assistant_request.exists() else None,
            "assistant_response": str(assistant_response) if assistant_response.exists() else None,
            "assistant_control_action": str(assistant_action) if assistant_action.exists() else None,
            "manual_gate": str(manual_gate) if manual_gate.exists() else None,
            "human_request": str(human_request) if human_request.exists() else None,
            "human_response": str(human_response) if human_response.exists() else None,
        }
        (node_dir / "meta.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _materialize_task(self, project: ProjectSpec, task: TaskSpec) -> TaskSpec:
        effective_sandbox = task.sandbox or project.default_sandbox
        updates: dict[str, object] = {}
        if (
            task.assistant_profile is None
            and project.default_assistant_profile is not None
        ):
            updates["assistant_profile"] = project.default_assistant_profile
        if task.sandbox is None:
            updates["sandbox"] = project.default_sandbox
        if task.model is None and project.default_model is not None:
            updates["model"] = project.default_model
        workspace_dir = self._resolve_task_workspace_dir(project, task)
        if not workspace_dir.exists():
            raise ValueError(
                f"task {task.id} workspace does not exist: {workspace_dir}"
            )
        if not workspace_dir.is_dir():
            raise ValueError(
                f"task {task.id} workspace is not a directory: {workspace_dir}"
            )
        workspace = str(workspace_dir)
        if task.workspace != workspace:
            updates["workspace"] = workspace
        extra_writable_roots = self._resolve_task_extra_writable_roots(task, workspace_dir)
        if effective_sandbox == "read-only" and extra_writable_roots:
            raise ValueError(
                f"task {task.id} cannot use extra writable roots with read-only sandbox"
            )
        if task.extra_writable_roots != extra_writable_roots:
            updates["extra_writable_roots"] = extra_writable_roots
        if not updates:
            return task
        return task.model_copy(update=updates)

    def _stage_snapshot_compose_context(self, snapshot: RunSnapshot) -> None:
        for task_id, node in snapshot.nodes.items():
            node_dir = self.store.get_node_dir(snapshot.id, task_id)
            for step in node.task.compose:
                if step.kind is ComposeStepKind.FILE and step.path is not None:
                    ensure_staged_compose_program_file(
                        program_dir=self.store.paths.root,
                        node_dir=node_dir,
                        relative_path=step.path,
                    )

    def _resolve_project_workspace_dir(self, project: ProjectSpec) -> Path:
        return Path(project.workspace).resolve()

    def _resolve_task_workspace_dir(self, project: ProjectSpec, task: TaskSpec) -> Path:
        project_workspace_dir = self._resolve_project_workspace_dir(project)
        if task.workspace is None:
            return project_workspace_dir
        workspace_dir = Path(task.workspace)
        if not workspace_dir.is_absolute():
            workspace_dir = project_workspace_dir / workspace_dir
        return workspace_dir.resolve()

    def _resolve_task_extra_writable_roots(
        self,
        task: TaskSpec,
        workspace_dir: Path,
    ) -> list[str]:
        resolved_roots: list[str] = []
        seen: set[str] = set()
        for raw_root in task.extra_writable_roots:
            root_path = Path(raw_root)
            if not root_path.is_absolute():
                root_path = workspace_dir / root_path
            resolved_root = str(root_path.resolve())
            if resolved_root in seen:
                continue
            seen.add(resolved_root)
            resolved_roots.append(resolved_root)
        return resolved_roots

    def _new_run_id(self) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        suffix = uuid.uuid4().hex[:8]
        return f"{timestamp}-{suffix}"
