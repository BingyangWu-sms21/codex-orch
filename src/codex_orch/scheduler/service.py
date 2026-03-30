from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent

from codex_orch.domain import (
    ComposeStepKind,
    DependencyKind,
    InterruptReplyKind,
    InterruptStatus,
    NodeExecutionRuntime,
    NodeExecutionTerminationReason,
    ProjectSpec,
    PublishedArtifact,
    RunInstanceState,
    RunInstanceStatus,
    RunInstanceWaitReason,
    RunRecord,
    RunStatus,
    TaskSpec,
    TaskStatus,
)
from codex_orch.prompt_context import ensure_staged_compose_program_file
from codex_orch.runner import NodeExecutionRequest, NodeExecutionResult, TaskRunner
from codex_orch.scheduler.composer import PromptComposer
from codex_orch.store import ProjectStore
from codex_orch.task_pool import TaskPoolService


class RunService:
    def __init__(self, store: ProjectStore, runner: TaskRunner) -> None:
        self.store = store
        self.runner = runner
        self.task_pool = TaskPoolService(store)
        self.composer = PromptComposer(store.paths.root)
        self._run_locks: dict[str, asyncio.Lock] = {}

    def create_snapshot(
        self,
        *,
        roots: list[str],
        labels: list[str],
        user_inputs: dict[str, str] | None = None,
    ) -> RunRecord:
        self.task_pool.validate_graph()
        project = self.store.load_project()
        selected = self.task_pool.select_subgraph(roots=roots, labels=labels)
        merged_inputs = self.store.load_default_user_inputs()
        if user_inputs is not None:
            merged_inputs.update(user_inputs)

        tasks = self.store.load_task_map()
        resolved_roots = set(roots)
        if labels:
            resolved_roots.update(
                task.id for task in tasks.values() if set(task.labels) & set(labels)
            )
        run_id = self._new_run_id()
        task_to_instance_id = {
            task_id: self._new_instance_id(task_id)
            for task_id in sorted(selected)
        }
        instances: dict[str, RunInstanceState] = {}
        for task_id, task in selected.items():
            materialized = self._materialize_task(project, task)
            instances[task_to_instance_id[task_id]] = RunInstanceState(
                instance_id=task_to_instance_id[task_id],
                task_id=task_id,
                task=materialized,
                dependency_instances={
                    dependency.task: task_to_instance_id[dependency.task]
                    for dependency in materialized.depends_on
                    if dependency.task in task_to_instance_id
                },
            )
        run = RunRecord(
            id=run_id,
            roots=sorted(resolved_roots),
            user_inputs=merged_inputs,
            project=project,
            instances=instances,
        )
        self._refresh_runnable_instances(run)
        self.store.save_run(run)
        self.store.append_event(
            run.id,
            "run_created",
            payload={"roots": run.roots, "instance_ids": sorted(run.instances)},
        )
        for instance in run.instances.values():
            self.store.append_event(
                run.id,
                "instance_created",
                instance_id=instance.instance_id,
                payload={"task_id": instance.task_id},
            )
            if instance.status is RunInstanceStatus.RUNNABLE:
                self.store.append_event(
                    run.id,
                    "instance_runnable",
                    instance_id=instance.instance_id,
                    payload={"task_id": instance.task_id},
                )
        return run

    async def start_run(
        self,
        *,
        roots: list[str],
        labels: list[str],
        user_inputs: dict[str, str] | None = None,
    ) -> RunRecord:
        run = self.create_snapshot(
            roots=roots,
            labels=labels,
            user_inputs=user_inputs,
        )
        return await self.run_snapshot(run.id)

    async def reconcile_run(self, run_id: str) -> RunRecord:
        lock = self._run_lock(run_id)
        async with lock:
            run = self.store.get_run(run_id)
            changed = False
            for instance in run.instances.values():
                if instance.status is not RunInstanceStatus.RUNNING:
                    continue
                runtime = self.store.maybe_get_attempt_runtime(
                    run_id,
                    instance.instance_id,
                    instance.attempt,
                )
                if runtime is None:
                    self._mark_instance_failed(
                        run,
                        instance.instance_id,
                        error="instance runtime is missing while instance is marked running",
                        termination_reason=NodeExecutionTerminationReason.ORPHANED,
                        finished_at=datetime.now(UTC).isoformat(),
                    )
                    changed = True
                    continue
                if runtime.finished_at is not None:
                    self._reconcile_finished_runtime(run, instance.instance_id, runtime)
                    changed = True
                    continue
                pid = runtime.pid
                if pid is None or not self._pid_exists(pid):
                    runtime.finished_at = datetime.now(UTC).isoformat()
                    runtime.return_code = -1
                    runtime.termination_reason = NodeExecutionTerminationReason.ORPHANED
                    self._mark_instance_failed(
                        run,
                        instance.instance_id,
                        error="worker process disappeared while instance was running",
                        termination_reason=NodeExecutionTerminationReason.ORPHANED,
                        finished_at=runtime.finished_at,
                    )
                    changed = True
                    continue
                stale_reason = self._stale_runtime_reason(runtime)
                if stale_reason is None:
                    continue
                self._terminate_pid(pid, run.project.node_terminate_grace_sec)
                runtime.finished_at = datetime.now(UTC).isoformat()
                runtime.return_code = -1
                runtime.termination_reason = stale_reason
                self._mark_instance_failed(
                    run,
                    instance.instance_id,
                    error=self._termination_reason_message(stale_reason),
                    termination_reason=stale_reason,
                    finished_at=runtime.finished_at,
                )
                changed = True
            self._refresh_runnable_instances(run)
            self._finalize_run_state(run)
            if changed:
                self.store.save_run(run)
            return run

    async def abort_run(self, run_id: str) -> RunRecord:
        lock = self._run_lock(run_id)
        async with lock:
            run = self.store.get_run(run_id)
            finished_at = datetime.now(UTC).isoformat()
            for instance in run.instances.values():
                if instance.status in {
                    RunInstanceStatus.DONE,
                    RunInstanceStatus.SKIPPED,
                    RunInstanceStatus.FAILED,
                }:
                    continue
                runtime = None
                if instance.attempt > 0:
                    runtime = self.store.maybe_get_attempt_runtime(
                        run.id,
                        instance.instance_id,
                        instance.attempt,
                    )
                if (
                    instance.status is RunInstanceStatus.RUNNING
                    and runtime is not None
                    and runtime.pid is not None
                ):
                    self._terminate_pid(runtime.pid, run.project.node_terminate_grace_sec)
                if instance.status in {
                    RunInstanceStatus.PENDING,
                    RunInstanceStatus.RUNNABLE,
                }:
                    instance.status = RunInstanceStatus.SKIPPED
                    instance.error = "run aborted by user"
                else:
                    instance.status = RunInstanceStatus.FAILED
                    instance.error = "run aborted by user"
                instance.waiting_reason = None
                instance.finished_at = finished_at
                instance.termination_reason = NodeExecutionTerminationReason.TERMINATED
                self._set_task_pool_status(instance.task_id, TaskStatus.FAILED)
            run.status = RunStatus.FAILED
            self.store.save_run(run)
            return run

    async def resume_run(self, run_id: str) -> RunRecord:
        run = await self.reconcile_run(run_id)
        if any(
            instance.status is RunInstanceStatus.RUNNING
            for instance in run.instances.values()
        ):
            return run
        return await self.run_snapshot(run_id)

    async def run_snapshot(self, run_id: str) -> RunRecord:
        while True:
            lock = self._run_lock(run_id)
            async with lock:
                run = self.store.get_run(run_id)
                self._refresh_runnable_instances(run)
                runnable_ids = [
                    instance.instance_id
                    for instance in run.instances.values()
                    if instance.status is RunInstanceStatus.RUNNABLE
                ]
                if not runnable_ids:
                    self._finalize_run_state(run)
                    self.store.save_run(run)
                    return run
                run.status = RunStatus.RUNNING
                self.store.save_run(run)
            semaphore = asyncio.Semaphore(run.project.max_concurrency)
            await asyncio.gather(
                *(
                    self._run_instance_with_semaphore(run_id, instance_id, semaphore)
                    for instance_id in runnable_ids
                )
            )

    async def _run_instance_with_semaphore(
        self,
        run_id: str,
        instance_id: str,
        semaphore: asyncio.Semaphore,
    ) -> None:
        async with semaphore:
            await self._execute_instance(run_id, instance_id)

    async def _execute_instance(self, run_id: str, instance_id: str) -> None:
        lock = self._run_lock(run_id)
        async with lock:
            run = self.store.get_run(run_id)
            instance = run.instances[instance_id]
            if instance.status is not RunInstanceStatus.RUNNABLE:
                return
            next_attempt = instance.attempt + 1
            attempt_dir = self.store.get_attempt_dir(run_id, instance_id, next_attempt)
            self._materialize_attempt_context(instance.task, attempt_dir)
            prompt = self._build_attempt_prompt(run, instance, attempt_dir)
            dependency_dirs = {
                dependency.task: self.store.get_instance_dir(
                    run_id,
                    instance.dependency_instances[dependency.task],
                )
                for dependency in instance.task.depends_on
                if dependency.task in instance.dependency_instances
            }
            project_workspace_dir = self._resolve_project_workspace_dir(run.project)
            workspace_dir = self._resolve_task_workspace_dir(run.project, instance.task)
            extra_writable_roots = tuple(
                Path(root)
                for root in self._resolve_task_extra_writable_roots(
                    instance.task,
                    workspace_dir,
                )
            )
            rendered_prompt = self.composer.render(
                instance.task,
                node_dir=attempt_dir,
                user_inputs=dict(run.user_inputs),
                dependency_node_dirs=dependency_dirs,
            )
            full_prompt = "\n\n".join(
                section for section in [rendered_prompt, prompt] if section
            )
            instance.status = RunInstanceStatus.RUNNING
            instance.waiting_reason = None
            instance.error = None
            instance.attempt = next_attempt
            instance.termination_reason = None
            instance.started_at = datetime.now(UTC).isoformat()
            self._set_task_pool_status(instance.task_id, TaskStatus.RUNNING)
            self.store.append_event(
                run.id,
                "attempt_started",
                instance_id=instance_id,
                payload={"attempt": next_attempt},
            )
            self.store.save_run(run)
            request = NodeExecutionRequest(
                run_id=run.id,
                instance_id=instance_id,
                attempt_no=next_attempt,
                program_dir=self.store.paths.root,
                project_workspace_dir=project_workspace_dir,
                workspace_dir=workspace_dir,
                extra_writable_roots=extra_writable_roots,
                instance_dir=self.store.get_instance_dir(run.id, instance_id),
                attempt_dir=attempt_dir,
                resume_session_id=instance.session_id,
                project=run.project,
                task=instance.task,
                prompt=full_prompt,
            )

        result = await self.runner.run(request)

        async with lock:
            run = self.store.get_run(run_id)
            instance = run.instances[instance_id]
            instance.finished_at = datetime.now(UTC).isoformat()
            instance.termination_reason = self._result_termination_reason(result)
            if result.session_id is not None:
                instance.session_id = result.session_id

            if request.resume_session_id is not None or result.session_id is not None:
                self._mark_resolved_interrupts_applied(run.id, instance_id)

            blocking_interrupts = self._refresh_instance_interrupts(run.id, instance)
            if blocking_interrupts:
                instance.status = RunInstanceStatus.WAITING
                instance.waiting_reason = RunInstanceWaitReason.INTERRUPTS_PENDING
                instance.error = "interrupt replies are still pending"
                self._set_task_pool_status(instance.task_id, TaskStatus.BLOCKED)
                self.store.append_event(
                    run.id,
                    "instance_waiting",
                    instance_id=instance_id,
                    payload={"interrupt_ids": blocking_interrupts},
                )
            elif result.success:
                try:
                    instance.published = self._promote_artifacts(
                        run.id,
                        instance,
                        instance.attempt,
                    )
                except ValueError as exc:
                    instance.status = RunInstanceStatus.FAILED
                    instance.waiting_reason = None
                    instance.error = str(exc)
                    self._set_task_pool_status(instance.task_id, TaskStatus.FAILED)
                else:
                    instance.status = RunInstanceStatus.DONE
                    instance.waiting_reason = None
                    instance.error = None
                    self._set_task_pool_status(instance.task_id, TaskStatus.DONE)
                    self.store.append_event(
                        run.id,
                        "instance_done",
                        instance_id=instance_id,
                        payload={"task_id": instance.task_id},
                    )
            else:
                instance.status = RunInstanceStatus.FAILED
                instance.waiting_reason = None
                instance.error = result.error
                self._set_task_pool_status(instance.task_id, TaskStatus.FAILED)
                self.store.append_event(
                    run.id,
                    "instance_failed",
                    instance_id=instance_id,
                    payload={"error": result.error},
                )

            self.store.append_event(
                run.id,
                "attempt_finished",
                instance_id=instance_id,
                payload={
                    "attempt": instance.attempt,
                    "termination_reason": instance.termination_reason.value,
                    "success": result.success,
                },
            )
            self.store.save_run(run)

    def _build_attempt_prompt(
        self,
        run: RunRecord,
        instance: RunInstanceState,
        attempt_dir: Path,
    ) -> str:
        if instance.session_id is None:
            return self._execution_contract_appendix(
                task=instance.task,
                attempt_dir=attempt_dir,
                project_workspace_dir=self._resolve_project_workspace_dir(run.project),
                workspace_dir=self._resolve_task_workspace_dir(run.project, instance.task),
                extra_writable_roots=tuple(
                    Path(root)
                    for root in self._resolve_task_extra_writable_roots(
                        instance.task,
                        self._resolve_task_workspace_dir(run.project, instance.task),
                    )
                ),
            )
        return self._resume_prompt(run.id, instance.instance_id)

    def _resume_prompt(self, run_id: str, instance_id: str) -> str:
        records = [
            record
            for record in self.store.list_instance_interrupts(run_id, instance_id)
            if record.interrupt.status is InterruptStatus.RESOLVED
        ]
        if not records:
            return "Continue the task using the existing session context."
        sections = [
            "## Resume Context",
            "Continue the task using the existing session context and the newly resolved external replies below.",
        ]
        for record in records:
            reply = record.reply
            if reply is None:
                continue
            if reply.audience.value == "assistant":
                sections.extend(
                    [
                        "### Assistant Reply",
                        f"Original Question: {record.interrupt.question.rstrip()}",
                        f"Answer: {reply.text.rstrip()}",
                    ]
                )
                if reply.rationale:
                    sections.append(f"Rationale: {reply.rationale.rstrip()}")
                if reply.citations:
                    sections.append(
                        "Citations: " + ", ".join(reply.citations)
                    )
            else:
                sections.extend(
                    [
                        "### Human Reply",
                        f"Original Question: {record.interrupt.question.rstrip()}",
                        f"Answer: {reply.text.rstrip()}",
                    ]
                )
                summary = record.interrupt.metadata.get("assistant_summary")
                rationale = record.interrupt.metadata.get("assistant_rationale")
                if isinstance(summary, str) and summary.strip():
                    sections.append(f"Assistant Summary: {summary.rstrip()}")
                if isinstance(rationale, str) and rationale.strip():
                    sections.append(f"Assistant Rationale: {rationale.rstrip()}")
        return "\n\n".join(sections)

    def _mark_resolved_interrupts_applied(self, run_id: str, instance_id: str) -> None:
        for record in self.store.list_instance_interrupts(run_id, instance_id):
            if record.interrupt.status is not InterruptStatus.RESOLVED:
                continue
            self.store.mark_interrupt_applied(run_id, record.interrupt.interrupt_id)

    def _refresh_instance_interrupts(
        self,
        run_id: str,
        instance: RunInstanceState,
    ) -> list[str]:
        interrupt_ids = [
            record.interrupt.interrupt_id
            for record in self.store.list_instance_interrupts(
                run_id,
                instance.instance_id,
                blocking_only=True,
            )
            if record.interrupt.status is InterruptStatus.OPEN
        ]
        instance.blocking_interrupts = interrupt_ids
        return interrupt_ids

    def _refresh_runnable_instances(self, run: RunRecord) -> None:
        for instance in run.instances.values():
            self._refresh_instance_interrupts(run.id, instance)
            if instance.status is RunInstanceStatus.WAITING:
                if instance.blocking_interrupts:
                    instance.waiting_reason = RunInstanceWaitReason.INTERRUPTS_PENDING
                    continue
                instance.status = RunInstanceStatus.RUNNABLE
                instance.waiting_reason = None
                instance.error = None
                self.store.append_event(
                    run.id,
                    "instance_resumed",
                    instance_id=instance.instance_id,
                    payload={"task_id": instance.task_id},
                )
                continue
            if instance.status is not RunInstanceStatus.PENDING:
                continue
            if self._dependency_failed(run, instance):
                instance.status = RunInstanceStatus.SKIPPED
                instance.waiting_reason = None
                instance.error = "upstream dependency failed"
                self._set_task_pool_status(instance.task_id, TaskStatus.BLOCKED)
                continue
            if self._dependencies_satisfied(run, instance):
                instance.status = RunInstanceStatus.RUNNABLE
                instance.waiting_reason = None
                self.store.append_event(
                    run.id,
                    "instance_runnable",
                    instance_id=instance.instance_id,
                    payload={"task_id": instance.task_id},
                )

    def _dependencies_satisfied(
        self,
        run: RunRecord,
        instance: RunInstanceState,
    ) -> bool:
        for dependency in instance.task.depends_on:
            upstream_instance_id = instance.dependency_instances.get(dependency.task)
            if upstream_instance_id is None:
                return False
            upstream = run.instances[upstream_instance_id]
            if upstream.status is not RunInstanceStatus.DONE:
                return False
            if dependency.kind is DependencyKind.CONTEXT:
                published_paths = {artifact.relative_path for artifact in upstream.published}
                if any(consumed not in published_paths for consumed in dependency.consume):
                    return False
        return True

    def _dependency_failed(self, run: RunRecord, instance: RunInstanceState) -> bool:
        for dependency in instance.task.depends_on:
            upstream_instance_id = instance.dependency_instances.get(dependency.task)
            if upstream_instance_id is None:
                return False
            upstream = run.instances[upstream_instance_id]
            if upstream.status in {RunInstanceStatus.FAILED, RunInstanceStatus.SKIPPED}:
                return True
        return False

    def _finalize_run_state(self, run: RunRecord) -> None:
        statuses = {instance.status for instance in run.instances.values()}
        if RunInstanceStatus.RUNNING in statuses:
            run.status = RunStatus.RUNNING
            return
        if RunInstanceStatus.FAILED in statuses:
            run.status = RunStatus.FAILED
            return
        if RunInstanceStatus.WAITING in statuses:
            run.status = RunStatus.WAITING
            return
        if RunInstanceStatus.RUNNABLE in statuses or RunInstanceStatus.PENDING in statuses:
            run.status = RunStatus.PENDING
            return
        run.status = RunStatus.DONE

    def _mark_instance_failed(
        self,
        run: RunRecord,
        instance_id: str,
        *,
        error: str,
        termination_reason: NodeExecutionTerminationReason,
        finished_at: str,
    ) -> None:
        instance = run.instances[instance_id]
        instance.status = RunInstanceStatus.FAILED
        instance.waiting_reason = None
        instance.error = error
        instance.finished_at = finished_at
        instance.termination_reason = termination_reason
        self._set_task_pool_status(instance.task_id, TaskStatus.FAILED)
        self.store.append_event(
            run.id,
            "instance_failed",
            instance_id=instance_id,
            payload={"error": error, "termination_reason": termination_reason.value},
        )

    def _reconcile_finished_runtime(
        self,
        run: RunRecord,
        instance_id: str,
        runtime: NodeExecutionRuntime,
    ) -> None:
        instance = run.instances[instance_id]
        termination_reason = self._resolve_runtime_termination_reason(runtime)
        instance.finished_at = runtime.finished_at
        instance.termination_reason = termination_reason
        if termination_reason is NodeExecutionTerminationReason.COMPLETED:
            blocking_interrupts = self._refresh_instance_interrupts(run.id, instance)
            if blocking_interrupts:
                instance.status = RunInstanceStatus.WAITING
                instance.waiting_reason = RunInstanceWaitReason.INTERRUPTS_PENDING
                instance.error = "interrupt replies are still pending"
                self._set_task_pool_status(instance.task_id, TaskStatus.BLOCKED)
                return
            try:
                instance.published = self._promote_artifacts(
                    run.id,
                    instance,
                    instance.attempt,
                )
            except ValueError as exc:
                self._mark_instance_failed(
                    run,
                    instance_id,
                    error=str(exc),
                    termination_reason=NodeExecutionTerminationReason.NONZERO_EXIT,
                    finished_at=runtime.finished_at or datetime.now(UTC).isoformat(),
                )
            else:
                instance.status = RunInstanceStatus.DONE
                instance.waiting_reason = None
                instance.error = None
                self._set_task_pool_status(instance.task_id, TaskStatus.DONE)
            return
        self._mark_instance_failed(
            run,
            instance_id,
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
            return "worker process disappeared while instance was running"
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
        instance: RunInstanceState,
        attempt_no: int,
    ) -> list[PublishedArtifact]:
        attempt_dir = self.store.get_attempt_dir(run_id, instance.instance_id, attempt_no)
        published_dir = self.store.get_instance_published_dir(run_id, instance.instance_id)
        if published_dir.exists():
            shutil.rmtree(published_dir)
        published_dir.mkdir(parents=True, exist_ok=True)

        artifacts: list[PublishedArtifact] = []
        for relative_path in instance.task.publish:
            source = attempt_dir / relative_path
            if not source.exists():
                raise ValueError(
                    f"task {instance.task_id} declared publish file {relative_path} but it was not produced"
                )
            destination = published_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            artifacts.append(PublishedArtifact(relative_path=relative_path))
        return artifacts

    def _materialize_task(self, project: ProjectSpec, task: TaskSpec) -> TaskSpec:
        effective_sandbox = task.sandbox or project.default_sandbox
        updates: dict[str, object] = {}
        if task.assistant_profile is None and project.default_assistant_profile is not None:
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

    def _materialize_attempt_context(self, task: TaskSpec, attempt_dir: Path) -> None:
        for step in task.compose:
            if step.kind is ComposeStepKind.FILE and step.path is not None:
                ensure_staged_compose_program_file(
                    program_dir=self.store.paths.root,
                    node_dir=attempt_dir,
                    relative_path=step.path,
                )
        self._write_interrupt_help_doc(attempt_dir=attempt_dir, task=task)

    def _interrupt_help_doc_path(self, attempt_dir: Path) -> Path:
        return attempt_dir / "context" / "interrupt" / "requesting-help.md"

    def _write_interrupt_help_doc(self, *, attempt_dir: Path, task: TaskSpec) -> None:
        helper_path = self._interrupt_help_doc_path(attempt_dir)
        assistant_profile = task.assistant_profile or "<none>"
        helper_path.parent.mkdir(parents=True, exist_ok=True)
        helper_path.write_text(
            dedent(
                f"""\
                # Requesting External Help

                This attempt can create runtime interrupts without hand-writing inbox files.

                ## Current Task State

                - task_id: `{task.id}`
                - effective_assistant_profile: `{assistant_profile}`

                ## Create An Assistant Interrupt

                ```bash
                codex-orch interrupt create \\
                  --program-dir "$CODEX_ORCH_PROGRAM_DIR" \\
                  --run-id "$CODEX_ORCH_RUN_ID" \\
                  --instance-id "$CODEX_ORCH_INSTANCE_ID" \\
                  --task-id "$CODEX_ORCH_TASK_ID" \\
                  --audience assistant \\
                  --kind clarification \\
                  --decision-kind policy \\
                  --question "Can I delete the legacy wrapper?" \\
                  --option delete \\
                  --option keep_wrapper
                ```

                ## Runtime Env Vars

                - `CODEX_ORCH_PROGRAM_DIR`
                - `CODEX_ORCH_RUN_ID`
                - `CODEX_ORCH_INSTANCE_ID`
                - `CODEX_ORCH_TASK_ID`
                - `CODEX_ORCH_INSTANCE_DIR`
                - `CODEX_ORCH_ATTEMPT_DIR`
                - `CODEX_ORCH_PROJECT_WORKSPACE_DIR`
                - `CODEX_ORCH_WORKSPACE_DIR`

                ## Semantics

                - Interrupts are resolved through the run inbox, not by mutating task-local files.
                - Blocking interrupts move this instance to `waiting` after the current attempt ends if they remain unresolved.
                - The next attempt resumes via the same Codex session after all blocking interrupts are resolved.
                """
            ).rstrip()
            + "\n",
            encoding="utf-8",
        )

    def _execution_contract_appendix(
        self,
        *,
        task: TaskSpec,
        attempt_dir: Path,
        project_workspace_dir: Path,
        workspace_dir: Path,
        extra_writable_roots: tuple[Path, ...],
    ) -> str:
        sandbox = task.sandbox or "workspace-write"
        helper_path = self._interrupt_help_doc_path(attempt_dir)
        sections = [
            "## Execution Contract",
            "\n".join(
                [
                    f"- workspace_dir (cwd): `{workspace_dir}`",
                    f"- project_workspace_dir: `{project_workspace_dir}`",
                    f"- instance_dir: `{attempt_dir.parent.parent}`",
                    f"- attempt_dir: `{attempt_dir}`",
                    f"- sandbox: `{sandbox}`",
                ]
            ),
        ]
        writable_roots = self._declared_writable_roots(
            sandbox=sandbox,
            workspace_dir=workspace_dir,
            attempt_dir=attempt_dir,
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
                    "### Interrupt Contract",
                    f"- Read `{helper_path}` before creating runtime interrupts.",
                    "- Use `codex-orch interrupt create`; do not hand-write inbox files.",
                    "- Interrupt replies are applied only to this instance resume path, not downstream tasks.",
                    "- Persist final outputs under the attempt directory so codex-orch can publish them on success.",
                ]
            )
        )
        return "\n\n".join(sections)

    def _declared_writable_roots(
        self,
        *,
        sandbox: str,
        workspace_dir: Path,
        attempt_dir: Path,
        extra_writable_roots: tuple[Path, ...],
    ) -> tuple[Path, ...] | None:
        if sandbox == "danger-full-access":
            return None
        if sandbox == "read-only":
            return tuple()
        candidates = [workspace_dir, attempt_dir, *extra_writable_roots]
        deduped: list[Path] = []
        seen: set[str] = set()
        for path in candidates:
            normalized = str(path)
            if normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(path)
        return tuple(deduped)

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

    def _set_task_pool_status(self, task_id: str, status: TaskStatus) -> None:
        task = self.store.get_task(task_id)
        updated = task.model_copy(update={"status": status})
        self.store.save_task(updated)

    def _run_lock(self, run_id: str) -> asyncio.Lock:
        lock = self._run_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._run_locks[run_id] = lock
        return lock

    def _new_run_id(self) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        suffix = uuid.uuid4().hex[:8]
        return f"{timestamp}-{suffix}"

    def _new_instance_id(self, task_id: str) -> str:
        return f"{task_id}-{uuid.uuid4().hex[:8]}"
