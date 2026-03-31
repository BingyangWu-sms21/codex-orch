from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import time
import uuid
from itertools import product
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent

from codex_orch.domain import (
    ControlEnvelope,
    ControlEnvelopeKind,
    DependencyKind,
    InterruptReplyKind,
    InterruptStatus,
    LoopAction,
    NodeExecutionFailureKind,
    NodeExecutionRuntime,
    NodeExecutionTerminationReason,
    ProjectSpec,
    PublishedArtifact,
    RunInputScopeState,
    RunInstanceState,
    RunInstanceStatus,
    RunInstanceWaitReason,
    RunRecord,
    RunStatus,
    RunTaskPathTemplateState,
    TaskControlMode,
    TaskKind,
    TaskSpec,
)
from codex_orch.input_values import JsonValue, render_input_template
from codex_orch.runner import NodeExecutionRequest, NodeExecutionResult, TaskRunner
from codex_orch.scheduler.composer import PromptComposer
from codex_orch.store import ProjectStore
from codex_orch.task_pool import TaskPoolService


class RunService:
    def __init__(self, store: ProjectStore, runner: TaskRunner) -> None:
        self.store = store
        self.runner = runner
        self.task_pool = TaskPoolService(store)
        self.composer = PromptComposer(store)
        self._run_locks: dict[str, asyncio.Lock] = {}

    def create_snapshot(
        self,
        *,
        roots: list[str],
        labels: list[str],
        user_inputs: dict[str, JsonValue] | None = None,
    ) -> RunRecord:
        self.task_pool.validate_graph()
        raw_project = self.store.load_project()
        selected = self.task_pool.select_subgraph(roots=roots, labels=labels)
        merged_inputs = self.store.load_default_user_inputs()
        if user_inputs is not None:
            merged_inputs.update(user_inputs)
        project = self._materialize_project(raw_project, merged_inputs)

        tasks = self.store.load_task_map()
        resolved_roots = set(roots)
        if labels:
            resolved_roots.update(
                task.id for task in tasks.values() if set(task.labels) & set(labels)
            )
        run_id = self._new_run_id()
        base_input_scope = RunInputScopeState(
            input_scope_id=self._new_input_scope_id(),
        )
        materialized_tasks = {
            task_id: self._materialize_task(project, task, merged_inputs)
            for task_id, task in selected.items()
        }
        run = RunRecord(
            id=run_id,
            roots=sorted(resolved_roots),
            user_inputs=merged_inputs,
            project=project,
            project_workspace_template=raw_project.workspace,
            tasks=materialized_tasks,
            task_path_templates={
                task_id: RunTaskPathTemplateState(
                    workspace=task.workspace,
                    extra_writable_roots=list(task.extra_writable_roots),
                )
                for task_id, task in selected.items()
            },
            input_scopes={base_input_scope.input_scope_id: base_input_scope},
            instances={},
        )
        self.store.append_event(
            run.id,
            "run_created",
            payload={
                "roots": run.roots,
                "task_ids": sorted(run.tasks),
                "base_input_scope_id": base_input_scope.input_scope_id,
            },
        )
        self.store.append_event(
            run.id,
            "input_scope_created",
            payload={
                "input_scope_id": base_input_scope.input_scope_id,
                "seed_task_ids": [],
                "created_by_instance_id": None,
            },
        )
        self._sync_instances(run)
        self._refresh_runnable_instances(run)
        self.store.save_run(run)
        return run

    async def start_run(
        self,
        *,
        roots: list[str],
        labels: list[str],
        user_inputs: dict[str, JsonValue] | None = None,
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
            self._sync_instances(run)
            self._refresh_runnable_instances(run)
            self._finalize_run_state(run)
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
                instance.failure_kind = NodeExecutionFailureKind.UNKNOWN
                instance.failure_summary = "run aborted by user"
                instance.resume_recommended = False
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
        lock = self._run_lock(run_id)
        async with lock:
            run = self.store.get_run(run_id)
            if self._requeue_recoverable_instances(run):
                self._refresh_runnable_instances(run)
                self._finalize_run_state(run)
                self.store.save_run(run)
        return await self.run_snapshot(run_id)

    async def force_retry_instance(
        self, run_id: str, instance_id: str
    ) -> RunRecord:
        run = self.store.get_run(run_id)
        instance = run.instances.get(instance_id)
        if instance is None:
            raise ValueError(f"Instance {instance_id!r} not found in run {run_id!r}")
        if instance.status is not RunInstanceStatus.FAILED:
            raise ValueError(
                f"Instance {instance_id!r} has status {instance.status!r}, expected 'failed'"
            )
        instance.status = RunInstanceStatus.RUNNABLE
        instance.waiting_reason = None
        instance.error = None
        instance.started_at = None
        instance.finished_at = None
        instance.termination_reason = None
        instance.failure_kind = None
        instance.failure_summary = None
        instance.resume_recommended = False
        self.store.append_event(
            run.id,
            "instance_requeued",
            instance_id=instance.instance_id,
            payload={"task_id": instance.task_id, "from_status": "failed", "forced": True},
        )
        self.store.save_run(run)
        return await self.resume_run(run_id)

    async def run_snapshot(self, run_id: str) -> RunRecord:
        while True:
            lock = self._run_lock(run_id)
            async with lock:
                run = self.store.get_run(run_id)
                self._sync_instances(run)
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
            project = self._materialize_project_for_input_scope(
                run,
                instance.input_scope_id,
            )
            task = self._materialize_task_for_input_scope(
                run,
                instance.task_id,
                instance.input_scope_id,
                project=project,
            )
            next_attempt = instance.attempt + 1
            attempt_dir = self.store.get_attempt_dir(run_id, instance_id, next_attempt)
            self._materialize_attempt_context(task, attempt_dir)
            prompt = self._build_attempt_prompt(
                run,
                instance,
                attempt_dir,
                project=project,
                task=task,
            )
            project_workspace_dir = self._resolve_project_workspace_dir(project)
            workspace_dir = self._resolve_task_workspace_dir(project, task)
            extra_writable_roots = tuple(
                Path(root)
                for root in self._resolve_task_extra_writable_roots(
                    task,
                    workspace_dir,
                )
            )
            rendered_prompt = self.composer.render(
                run=run,
                instance=instance,
                node_dir=attempt_dir,
            )
            full_prompt = "\n\n".join(
                section for section in [rendered_prompt, prompt] if section
            )
            instance.status = RunInstanceStatus.RUNNING
            instance.waiting_reason = None
            instance.error = None
            instance.failure_kind = None
            instance.failure_summary = None
            instance.resume_recommended = False
            instance.attempt = next_attempt
            instance.termination_reason = None
            instance.started_at = datetime.now(UTC).isoformat()
            instance.finished_at = None
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
                project=project,
                task=task,
                prompt=full_prompt,
            )

        result = await self.runner.run(request)

        async with lock:
            run = self.store.get_run(run_id)
            instance = run.instances[instance_id]
            task = run.tasks[instance.task_id]
            instance.finished_at = datetime.now(UTC).isoformat()
            instance.termination_reason = self._result_termination_reason(result)
            instance.failure_kind = result.failure_kind
            instance.failure_summary = result.failure_summary
            instance.resume_recommended = result.resume_recommended
            if result.session_id is not None:
                instance.session_id = result.session_id

            if request.resume_session_id is not None or result.session_id is not None:
                self._mark_resolved_interrupts_applied(run.id, instance_id)

            blocking_interrupts = self._refresh_instance_interrupts(run.id, instance)
            if blocking_interrupts:
                instance.status = RunInstanceStatus.WAITING
                instance.waiting_reason = RunInstanceWaitReason.INTERRUPTS_PENDING
                instance.error = "interrupt replies are still pending"
                self.store.append_event(
                    run.id,
                    "instance_waiting",
                    instance_id=instance_id,
                    payload={"interrupt_ids": blocking_interrupts},
                )
            elif result.success:
                try:
                    self._materialize_instance_result(
                        run.id,
                        task,
                        instance_id,
                        instance.attempt,
                    )
                    instance.published = self._promote_artifacts(
                        run.id,
                        instance,
                        task,
                        instance.attempt,
                    )
                except ValueError as exc:
                    self.store.delete_instance_result(run.id, instance_id)
                    self._mark_instance_failed(
                        run,
                        instance_id,
                        error=str(exc),
                        termination_reason=NodeExecutionTerminationReason.NONZERO_EXIT,
                        finished_at=instance.finished_at or datetime.now(UTC).isoformat(),
                        failure_kind=NodeExecutionFailureKind.TASK_RUNTIME,
                        failure_summary=str(exc),
                        resume_recommended=False,
                    )
                else:
                    instance.status = RunInstanceStatus.DONE
                    instance.waiting_reason = None
                    instance.error = None
                    instance.failure_kind = None
                    instance.failure_summary = None
                    instance.resume_recommended = False
                    self.store.append_event(
                        run.id,
                        "instance_done",
                        instance_id=instance_id,
                        payload={"task_id": instance.task_id},
                    )
            else:
                self.store.delete_instance_result(run.id, instance_id)
                self._mark_instance_failed(
                    run,
                    instance_id,
                    error=result.error or "codex exec failed",
                    termination_reason=instance.termination_reason,
                    finished_at=instance.finished_at or datetime.now(UTC).isoformat(),
                    failure_kind=result.failure_kind,
                    failure_summary=result.failure_summary,
                    resume_recommended=result.resume_recommended,
                )

            self.store.append_event(
                run.id,
                "attempt_finished",
                instance_id=instance_id,
                payload={
                    "attempt": instance.attempt,
                    "termination_reason": instance.termination_reason.value,
                    "success": result.success,
                    "failure_kind": (
                        None
                        if instance.failure_kind is None
                        else instance.failure_kind.value
                    ),
                    "failure_summary": instance.failure_summary,
                    "resume_recommended": instance.resume_recommended,
                },
            )
            self.store.save_run(run)

    def _build_attempt_prompt(
        self,
        run: RunRecord,
        instance: RunInstanceState,
        attempt_dir: Path,
        *,
        project: ProjectSpec | None = None,
        task: TaskSpec | None = None,
    ) -> str:
        effective_project = (
            self._materialize_project_for_input_scope(run, instance.input_scope_id)
            if project is None
            else project
        )
        effective_task = (
            self._materialize_task_for_input_scope(
                run,
                instance.task_id,
                instance.input_scope_id,
                project=effective_project,
            )
            if task is None
            else task
        )
        if instance.session_id is None:
            return self._execution_contract_appendix(
                task=effective_task,
                attempt_dir=attempt_dir,
                project_workspace_dir=self._resolve_project_workspace_dir(
                    effective_project
                ),
                workspace_dir=self._resolve_task_workspace_dir(
                    effective_project,
                    effective_task,
                ),
                extra_writable_roots=tuple(
                    Path(root)
                    for root in self._resolve_task_extra_writable_roots(
                        effective_task,
                        self._resolve_task_workspace_dir(
                            effective_project,
                            effective_task,
                        ),
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
            task = run.tasks[instance.task_id]
            self._refresh_instance_interrupts(run.id, instance)
            if instance.status is RunInstanceStatus.WAITING:
                if instance.blocking_interrupts:
                    instance.waiting_reason = RunInstanceWaitReason.INTERRUPTS_PENDING
                    continue
                instance.status = RunInstanceStatus.RUNNABLE
                instance.waiting_reason = None
                instance.error = None
                instance.failure_kind = None
                instance.failure_summary = None
                instance.resume_recommended = False
                self.store.append_event(
                    run.id,
                    "instance_resumed",
                    instance_id=instance.instance_id,
                    payload={"task_id": instance.task_id},
                )
                continue
            if (
                instance.status is RunInstanceStatus.SKIPPED
                and instance.error == "upstream dependency failed"
            ):
                if self._dependency_failed(run, instance):
                    continue
                instance.status = RunInstanceStatus.PENDING
                instance.waiting_reason = None
                instance.error = None
            if instance.status is not RunInstanceStatus.PENDING:
                continue
            if self._dependency_failed(run, instance):
                instance.status = RunInstanceStatus.SKIPPED
                instance.waiting_reason = None
                instance.error = "upstream dependency failed"
                continue
            if self._dependencies_satisfied(run, instance, task):
                instance.status = RunInstanceStatus.RUNNABLE
                instance.waiting_reason = None
                instance.failure_kind = None
                instance.failure_summary = None
                instance.resume_recommended = False
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
        task: TaskSpec,
    ) -> bool:
        for dependency in task.depends_on:
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
        task = run.tasks[instance.task_id]
        for dependency in task.depends_on:
            upstream_instance_id = instance.dependency_instances.get(dependency.task)
            if upstream_instance_id is None:
                return False
            upstream = run.instances[upstream_instance_id]
            if upstream.status in {RunInstanceStatus.FAILED, RunInstanceStatus.SKIPPED}:
                return True
        return False

    def _sync_instances(self, run: RunRecord) -> None:
        lineage_cache: dict[str, dict[str, str]] = {}
        while True:
            created = self._sync_input_scopes(run)
            existing_keys = {
                (
                    instance.task_id,
                    tuple(sorted(instance.dependency_instances.items())),
                    instance.input_scope_id,
                )
                for instance in run.instances.values()
            }
            for input_scope_id in sorted(run.input_scopes):
                for task_id in sorted(run.tasks):
                    task = run.tasks[task_id]
                    for dependency_instances, activation_bindings in self._candidate_instance_bindings(
                        run,
                        task,
                        input_scope_id,
                        lineage_cache,
                    ):
                        key = (
                            task_id,
                            tuple(sorted(dependency_instances.items())),
                            input_scope_id,
                        )
                        if key in existing_keys:
                            continue
                        instance = RunInstanceState(
                            instance_id=self._new_instance_id(task_id),
                            task_id=task_id,
                            input_scope_id=input_scope_id,
                            dependency_instances=dependency_instances,
                            activation_bindings=activation_bindings,
                        )
                        run.instances[instance.instance_id] = instance
                        existing_keys.add(key)
                        self.store.append_event(
                            run.id,
                            "instance_created",
                            instance_id=instance.instance_id,
                            payload={
                                "task_id": task_id,
                                "input_scope_id": input_scope_id,
                                "dependency_instances": dependency_instances,
                                "activation_bindings": activation_bindings,
                            },
                        )
                        lineage_cache.clear()
                        created = True
            if not created:
                return

    def _candidate_instance_bindings(
        self,
        run: RunRecord,
        task: TaskSpec,
        input_scope_id: str,
        lineage_cache: dict[str, dict[str, str]],
    ) -> list[tuple[dict[str, str], dict[str, str]]]:
        input_scope = run.input_scopes[input_scope_id]
        is_base_scope = input_scope_id == self._base_input_scope_id(run)
        is_seed_task = task.id in input_scope.seed_task_ids
        if not task.depends_on:
            if is_base_scope or is_seed_task:
                return [({}, {})]
            return []

        dependency_candidates: list[list[RunInstanceState]] = []
        control_owner = self._task_control_owner(run.tasks, task.id)
        for dependency in task.depends_on:
            candidates = self._dependency_candidate_instances(
                run,
                dependency.task,
                input_scope_id=input_scope_id,
                control_owner=control_owner,
                target_task_id=task.id,
            )
            if not candidates:
                return []
            dependency_candidates.append(candidates)

        bindings: list[tuple[dict[str, str], dict[str, str]]] = []
        for combo in product(*dependency_candidates):
            dependency_instances = {
                dependency.task: candidate.instance_id
                for dependency, candidate in zip(task.depends_on, combo, strict=True)
            }
            merged_lineage: dict[str, str] = {}
            activation_bindings: dict[str, str] = {}
            valid = True
            for candidate in combo:
                if not self._merge_mapping(
                    merged_lineage,
                    self._instance_lineage(run, candidate.instance_id, lineage_cache),
                ):
                    valid = False
                    break
                if not self._merge_mapping(
                    activation_bindings,
                    candidate.activation_bindings,
                ):
                    valid = False
                    break
            if not valid:
                continue
            if (
                not is_base_scope
                and not is_seed_task
                and not any(candidate.input_scope_id == input_scope_id for candidate in combo)
            ):
                continue
            if control_owner is not None:
                controller_task_id, _ = control_owner
                controller_instance_id = dependency_instances.get(controller_task_id)
                if controller_instance_id is None:
                    continue
                current = activation_bindings.get(controller_task_id)
                if current is not None and current != controller_instance_id:
                    continue
                activation_bindings[controller_task_id] = controller_instance_id
            bindings.append((dependency_instances, activation_bindings))
        return bindings

    def _dependency_candidate_instances(
        self,
        run: RunRecord,
        dependency_task_id: str,
        *,
        input_scope_id: str,
        control_owner: tuple[str, str] | None,
        target_task_id: str,
    ) -> list[RunInstanceState]:
        scoped_candidates = sorted(
            (
                instance
                for instance in run.instances.values()
                if instance.task_id == dependency_task_id
                and instance.input_scope_id == input_scope_id
            ),
            key=lambda instance: instance.instance_id,
        )
        if control_owner is not None and control_owner[0] == dependency_task_id:
            if not scoped_candidates:
                return []
            if control_owner[1] == "route":
                return [
                    instance
                    for instance in scoped_candidates
                    if self._controller_instance_selects_route_target(
                        run,
                        instance,
                        target_task_id,
                    )
                ]
            return [
                instance
                for instance in scoped_candidates
                if self._controller_instance_stops_to_target(
                    run,
                    instance,
                    target_task_id,
                )
            ]
        if scoped_candidates:
            return scoped_candidates
        base_input_scope_id = self._base_input_scope_id(run)
        if input_scope_id == base_input_scope_id:
            return []
        if not self._input_scope_can_reuse_base_dependency(
            run,
            input_scope_id=input_scope_id,
            dependency_task_id=dependency_task_id,
        ):
            return []
        return [
            instance
            for instance in sorted(
                (
                    candidate
                    for candidate in run.instances.values()
                    if candidate.task_id == dependency_task_id
                    and candidate.input_scope_id == base_input_scope_id
                ),
                key=lambda instance: instance.instance_id,
            )
        ]

    def _task_control_owner(
        self,
        tasks: dict[str, TaskSpec],
        task_id: str,
    ) -> tuple[str, str] | None:
        for candidate in tasks.values():
            if candidate.kind is not TaskKind.CONTROLLER or candidate.control is None:
                continue
            if candidate.control.mode is TaskControlMode.ROUTE:
                for route in candidate.control.routes:
                    if task_id in route.targets:
                        return candidate.id, "route"
                continue
            if task_id in candidate.control.stop_targets:
                return candidate.id, "stop"
        return None

    def _input_scope_can_reuse_base_dependency(
        self,
        run: RunRecord,
        *,
        input_scope_id: str,
        dependency_task_id: str,
    ) -> bool:
        input_scope = run.input_scopes[input_scope_id]
        return any(
            dependency_task_id in self._task_ancestors(run.tasks, seed_task_id)
            for seed_task_id in input_scope.seed_task_ids
        )

    def _task_ancestors(
        self,
        tasks: dict[str, TaskSpec],
        task_id: str,
    ) -> set[str]:
        ancestors: set[str] = set()
        stack = [task_id]
        while stack:
            current = stack.pop()
            for dependency in tasks[current].depends_on:
                if dependency.task not in tasks or dependency.task in ancestors:
                    continue
                ancestors.add(dependency.task)
                stack.append(dependency.task)
        return ancestors

    def _controller_instance_selects_route_target(
        self,
        run: RunRecord,
        instance: RunInstanceState,
        target_task_id: str,
    ) -> bool:
        task = run.tasks[instance.task_id]
        if (
            task.kind is not TaskKind.CONTROLLER
            or task.control is None
            or task.control.mode is not TaskControlMode.ROUTE
        ):
            return False
        control = self._controller_control_for_instance(run, instance.instance_id)
        if control is None or control.kind is not ControlEnvelopeKind.ROUTE:
            return False
        for route in task.control.routes:
            if route.label in control.labels and target_task_id in route.targets:
                return True
        return False

    def _controller_instance_stops_to_target(
        self,
        run: RunRecord,
        instance: RunInstanceState,
        target_task_id: str,
    ) -> bool:
        task = run.tasks[instance.task_id]
        if (
            task.kind is not TaskKind.CONTROLLER
            or task.control is None
            or task.control.mode is not TaskControlMode.LOOP
        ):
            return False
        control = self._controller_control_for_instance(run, instance.instance_id)
        if (
            control is None
            or control.kind is not ControlEnvelopeKind.LOOP
            or control.action is not LoopAction.STOP
        ):
            return False
        return target_task_id in task.control.stop_targets

    def _controller_control_for_instance(
        self,
        run: RunRecord,
        instance_id: str,
    ) -> ControlEnvelope | None:
        payload = self.store.maybe_get_instance_result(run.id, instance_id)
        if not isinstance(payload, dict):
            return None
        control = payload.get("control")
        if not isinstance(control, dict):
            return None
        task = run.tasks[run.instances[instance_id].task_id]
        try:
            return ControlEnvelope.model_validate(control).validate_against_task(task)
        except ValueError:
            return None

    def _sync_input_scopes(self, run: RunRecord) -> bool:
        created = False
        existing_by_creator = {
            input_scope.created_by_instance_id: input_scope.input_scope_id
            for input_scope in run.input_scopes.values()
            if input_scope.created_by_instance_id is not None
        }
        for instance in sorted(run.instances.values(), key=lambda candidate: candidate.instance_id):
            if instance.status is not RunInstanceStatus.DONE:
                continue
            if instance.instance_id in existing_by_creator:
                continue
            task = run.tasks[instance.task_id]
            if (
                task.kind is not TaskKind.CONTROLLER
                or task.control is None
                or task.control.mode is not TaskControlMode.LOOP
            ):
                continue
            control = self._controller_control_for_instance(run, instance.instance_id)
            if (
                control is None
                or control.kind is not ControlEnvelopeKind.LOOP
                or control.action is not LoopAction.CONTINUE
            ):
                continue
            current_scope = run.input_scopes[instance.input_scope_id]
            next_scope = RunInputScopeState(
                input_scope_id=self._new_input_scope_id(),
                parent_input_scope_id=current_scope.input_scope_id,
                seed_task_ids=list(task.control.continue_targets),
                values={
                    **current_scope.values,
                    **control.next_inputs,
                },
                created_by_instance_id=instance.instance_id,
            )
            run.input_scopes[next_scope.input_scope_id] = next_scope
            existing_by_creator[instance.instance_id] = next_scope.input_scope_id
            self.store.append_event(
                run.id,
                "input_scope_created",
                instance_id=instance.instance_id,
                payload={
                    "input_scope_id": next_scope.input_scope_id,
                    "parent_input_scope_id": current_scope.input_scope_id,
                    "seed_task_ids": next_scope.seed_task_ids,
                    "created_by_instance_id": instance.instance_id,
                },
            )
            created = True
        return created

    def _base_input_scope_id(self, run: RunRecord) -> str:
        for input_scope in run.input_scopes.values():
            if (
                input_scope.parent_input_scope_id is None
                and input_scope.created_by_instance_id is None
            ):
                return input_scope.input_scope_id
        raise ValueError("run is missing a base input scope")

    def _instance_lineage(
        self,
        run: RunRecord,
        instance_id: str,
        lineage_cache: dict[str, dict[str, str]],
    ) -> dict[str, str]:
        cached = lineage_cache.get(instance_id)
        if cached is not None:
            return dict(cached)
        instance = run.instances[instance_id]
        lineage = {instance.task_id: instance.instance_id}
        for dependency_instance_id in instance.dependency_instances.values():
            if dependency_instance_id not in run.instances:
                raise ValueError(
                    f"instance {instance.instance_id} references missing dependency instance {dependency_instance_id}"
                )
            dependency_lineage = self._instance_lineage(
                run,
                dependency_instance_id,
                lineage_cache,
            )
            if not self._merge_mapping(lineage, dependency_lineage):
                raise ValueError(
                    f"instance {instance.instance_id} has ambiguous lineage"
                )
        lineage_cache[instance_id] = dict(lineage)
        return lineage

    def _merge_mapping(
        self,
        target: dict[str, str],
        source: dict[str, str],
    ) -> bool:
        for key, value in source.items():
            current = target.get(key)
            if current is not None and current != value:
                return False
            target[key] = value
        return True

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
        failure_kind: NodeExecutionFailureKind | None = None,
        failure_summary: str | None = None,
        resume_recommended: bool = False,
    ) -> None:
        instance = run.instances[instance_id]
        instance.status = RunInstanceStatus.FAILED
        instance.waiting_reason = None
        instance.error = error
        instance.finished_at = finished_at
        instance.termination_reason = termination_reason
        instance.failure_kind = (
            NodeExecutionFailureKind.UNKNOWN
            if failure_kind is None
            else failure_kind
        )
        instance.failure_summary = failure_summary or error
        instance.resume_recommended = resume_recommended
        self.store.append_event(
            run.id,
            "instance_failed",
            instance_id=instance_id,
            payload={
                "error": error,
                "termination_reason": termination_reason.value,
                "failure_kind": instance.failure_kind.value,
                "failure_summary": instance.failure_summary,
                "resume_recommended": instance.resume_recommended,
            },
        )

    def _reconcile_finished_runtime(
        self,
        run: RunRecord,
        instance_id: str,
        runtime: NodeExecutionRuntime,
    ) -> None:
        instance = run.instances[instance_id]
        task = run.tasks[instance.task_id]
        termination_reason = self._resolve_runtime_termination_reason(runtime)
        instance.finished_at = runtime.finished_at
        instance.termination_reason = termination_reason
        instance.failure_kind = runtime.failure_kind
        instance.failure_summary = runtime.failure_summary
        instance.resume_recommended = runtime.resume_recommended
        if termination_reason is NodeExecutionTerminationReason.COMPLETED:
            blocking_interrupts = self._refresh_instance_interrupts(run.id, instance)
            if blocking_interrupts:
                instance.status = RunInstanceStatus.WAITING
                instance.waiting_reason = RunInstanceWaitReason.INTERRUPTS_PENDING
                instance.error = "interrupt replies are still pending"
                return
            try:
                self._materialize_instance_result(
                    run.id,
                    task,
                    instance_id,
                    instance.attempt,
                )
                instance.published = self._promote_artifacts(
                    run.id,
                    instance,
                    task,
                    instance.attempt,
                )
            except ValueError as exc:
                self.store.delete_instance_result(run.id, instance_id)
                self._mark_instance_failed(
                    run,
                    instance_id,
                    error=str(exc),
                    termination_reason=NodeExecutionTerminationReason.NONZERO_EXIT,
                    finished_at=runtime.finished_at or datetime.now(UTC).isoformat(),
                    failure_kind=NodeExecutionFailureKind.TASK_RUNTIME,
                    failure_summary=str(exc),
                    resume_recommended=False,
                )
            else:
                instance.status = RunInstanceStatus.DONE
                instance.waiting_reason = None
                instance.error = None
                instance.failure_kind = None
                instance.failure_summary = None
                instance.resume_recommended = False
            return
        self.store.delete_instance_result(run.id, instance_id)
        self._mark_instance_failed(
            run,
            instance_id,
            error=runtime.failure_summary or self._termination_reason_message(termination_reason),
            termination_reason=termination_reason,
            finished_at=runtime.finished_at or datetime.now(UTC).isoformat(),
            failure_kind=runtime.failure_kind,
            failure_summary=runtime.failure_summary,
            resume_recommended=runtime.resume_recommended,
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

    def _requeue_recoverable_instances(self, run: RunRecord) -> bool:
        changed = False
        for instance in run.instances.values():
            if instance.status is not RunInstanceStatus.FAILED:
                continue
            if not instance.resume_recommended:
                continue
            instance.status = RunInstanceStatus.RUNNABLE
            instance.waiting_reason = None
            instance.error = None
            instance.started_at = None
            instance.finished_at = None
            instance.termination_reason = None
            instance.failure_kind = None
            instance.failure_summary = None
            instance.resume_recommended = False
            self.store.append_event(
                run.id,
                "instance_requeued",
                instance_id=instance.instance_id,
                payload={"task_id": instance.task_id, "from_status": "failed"},
            )
            changed = True
        return changed

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
        task: TaskSpec,
        attempt_no: int,
    ) -> list[PublishedArtifact]:
        attempt_dir = self.store.get_attempt_dir(run_id, instance.instance_id, attempt_no)
        published_dir = self.store.get_instance_published_dir(run_id, instance.instance_id)
        if published_dir.exists():
            shutil.rmtree(published_dir)
        published_dir.mkdir(parents=True, exist_ok=True)

        artifacts: list[PublishedArtifact] = []
        for relative_path in task.publish:
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

    def _materialize_instance_result(
        self,
        run_id: str,
        task: TaskSpec,
        instance_id: str,
        attempt_no: int,
    ) -> None:
        payload = self._load_attempt_result_payload(
            run_id=run_id,
            instance_id=instance_id,
            task=task,
            attempt_no=attempt_no,
        )
        if payload is None:
            self.store.delete_instance_result(run_id, instance_id)
            return
        control_envelope: ControlEnvelope | None = None
        if task.kind is TaskKind.CONTROLLER:
            control_envelope = self._validate_controller_result(task, payload)
        self.store.save_instance_result(run_id, instance_id, payload)
        self.store.append_event(
            run_id,
            "result_materialized",
            instance_id=instance_id,
            payload={"task_id": task.id},
        )
        if task.kind is not TaskKind.CONTROLLER:
            return
        assert control_envelope is not None
        self.store.append_event(
            run_id,
            "control_emitted",
            instance_id=instance_id,
            payload={"kind": control_envelope.kind.value},
        )
        assert task.control is not None
        if control_envelope.kind is ControlEnvelopeKind.ROUTE:
            assert task.control.mode is TaskControlMode.ROUTE
            label_set = set(control_envelope.labels)
            for route in task.control.routes:
                self.store.append_event(
                    run_id,
                    "route_selected" if route.label in label_set else "route_unselected",
                    instance_id=instance_id,
                    payload={"label": route.label, "targets": route.targets},
                )
            return
        assert task.control.mode is TaskControlMode.LOOP
        if control_envelope.action is LoopAction.CONTINUE:
            self.store.append_event(
                run_id,
                "loop_continued",
                instance_id=instance_id,
                payload={
                    "continue_targets": task.control.continue_targets,
                    "next_inputs": control_envelope.next_inputs,
                },
            )
            return
        self.store.append_event(
            run_id,
            "loop_stopped",
            instance_id=instance_id,
            payload={"stop_targets": task.control.stop_targets},
        )

    def _load_attempt_result_payload(
        self,
        *,
        run_id: str,
        instance_id: str,
        task: TaskSpec,
        attempt_no: int,
    ) -> object | None:
        result_path = self.store.get_attempt_dir(run_id, instance_id, attempt_no) / "result.json"
        if not result_path.exists():
            if task.kind is TaskKind.CONTROLLER:
                raise ValueError(f"controller task {task.id} did not produce result.json")
            if task.result_schema is not None or "result.json" in task.publish:
                raise ValueError(f"task {task.id} did not produce required result.json")
            return None
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"task {task.id} produced invalid result.json: {exc}") from exc

    def _validate_controller_result(
        self,
        task: TaskSpec,
        payload: object,
    ) -> ControlEnvelope:
        if not isinstance(payload, dict):
            raise ValueError(f"controller task {task.id} result.json must be a JSON object")
        control = payload.get("control")
        if not isinstance(control, dict):
            raise ValueError(f"controller task {task.id} result.json must include control")
        assert task.control is not None
        try:
            return ControlEnvelope.model_validate(control).validate_against_task(task)
        except ValueError as exc:
            raise ValueError(
                f"controller task {task.id} emitted invalid control: {exc}"
            ) from exc

    def _materialize_project(
        self,
        project: ProjectSpec,
        user_inputs: dict[str, JsonValue],
    ) -> ProjectSpec:
        workspace = render_input_template(
            project.workspace,
            inputs=user_inputs,
            field_name="project.workspace",
        )
        if not workspace.strip():
            raise ValueError("project.workspace must not resolve to an empty path")
        workspace_dir = Path(workspace).resolve()
        if not workspace_dir.exists():
            raise ValueError(f"project workspace does not exist: {workspace_dir}")
        if not workspace_dir.is_dir():
            raise ValueError(f"project workspace is not a directory: {workspace_dir}")
        if project.workspace == str(workspace_dir):
            return project
        return project.model_copy(update={"workspace": str(workspace_dir)})

    def _inputs_for_input_scope(
        self,
        run: RunRecord,
        input_scope_id: str,
    ) -> dict[str, JsonValue]:
        input_scope = run.input_scopes[input_scope_id]
        return {
            **run.user_inputs,
            **input_scope.values,
        }

    def _project_workspace_template(self, run: RunRecord) -> str:
        if run.project_workspace_template is not None:
            return run.project_workspace_template
        return run.project.workspace

    def _task_path_template(
        self,
        run: RunRecord,
        task_id: str,
    ) -> RunTaskPathTemplateState:
        template = run.task_path_templates.get(task_id)
        if template is not None:
            return template
        task = run.tasks[task_id]
        return RunTaskPathTemplateState(
            workspace=task.workspace,
            extra_writable_roots=list(task.extra_writable_roots),
        )

    def _materialize_project_for_input_scope(
        self,
        run: RunRecord,
        input_scope_id: str,
    ) -> ProjectSpec:
        raw_workspace = self._project_workspace_template(run)
        raw_project = run.project.model_copy(update={"workspace": raw_workspace})
        return self._materialize_project(
            raw_project,
            self._inputs_for_input_scope(run, input_scope_id),
        )

    def _materialize_task_for_input_scope(
        self,
        run: RunRecord,
        task_id: str,
        input_scope_id: str,
        *,
        project: ProjectSpec | None = None,
    ) -> TaskSpec:
        effective_project = (
            self._materialize_project_for_input_scope(run, input_scope_id)
            if project is None
            else project
        )
        task = run.tasks[task_id]
        path_template = self._task_path_template(run, task_id)
        raw_task = task.model_copy(
            update={
                "workspace": path_template.workspace,
                "extra_writable_roots": list(path_template.extra_writable_roots),
            }
        )
        return self._materialize_task(
            effective_project,
            raw_task,
            self._inputs_for_input_scope(run, input_scope_id),
        )

    def _materialize_task(
        self,
        project: ProjectSpec,
        task: TaskSpec,
        user_inputs: dict[str, JsonValue],
    ) -> TaskSpec:
        effective_sandbox = task.sandbox or project.default_sandbox
        updates: dict[str, object] = {}
        if task.sandbox is None:
            updates["sandbox"] = project.default_sandbox
        if task.model is None and project.default_model is not None:
            updates["model"] = project.default_model
        materialized_workspace = (
            None
            if task.workspace is None
            else render_input_template(
                task.workspace,
                inputs=user_inputs,
                field_name=f"task {task.id} workspace",
            )
        )
        if materialized_workspace is not None and not materialized_workspace.strip():
            raise ValueError(f"task {task.id} workspace must not resolve to an empty path")
        materialized_extra_writable_roots = [
            render_input_template(
                raw_root,
                inputs=user_inputs,
                field_name=f"task {task.id} extra_writable_roots",
            )
            for raw_root in task.extra_writable_roots
        ]
        for resolved_root in materialized_extra_writable_roots:
            if not resolved_root.strip():
                raise ValueError(
                    f"task {task.id} extra_writable_roots must not resolve to an empty path"
                )
        materialized_task = task.model_copy(
            update={
                "workspace": materialized_workspace,
                "extra_writable_roots": materialized_extra_writable_roots,
            }
        )
        workspace_dir = self._resolve_task_workspace_dir(project, materialized_task)
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
        extra_writable_roots = self._resolve_task_extra_writable_roots(
            materialized_task,
            workspace_dir,
        )
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
        self._write_interrupt_help_doc(attempt_dir=attempt_dir, task=task)

    def _interrupt_help_doc_path(self, attempt_dir: Path) -> Path:
        return attempt_dir / "context" / "interrupt" / "requesting-help.md"

    def _write_interrupt_help_doc(self, *, attempt_dir: Path, task: TaskSpec) -> None:
        helper_path = self._interrupt_help_doc_path(attempt_dir)
        roles = self.store.list_assistant_roles()
        if task.interaction_policy.allowed_assistant_roles is None:
            allowed_roles = sorted(roles)
        else:
            allowed_roles = sorted(set(task.interaction_policy.allowed_assistant_roles))
        role_lines = [
            f"- `{role_id}`: {roles[role_id].spec.description or roles[role_id].spec.title or role_id}"
            for role_id in sorted(roles)
        ]
        if not role_lines:
            role_lines = ["- no assistant roles are currently registered"]
        registered_roles_text = "\n".join(role_lines)
        if task.interaction_policy.allowed_assistant_roles is None:
            allowed_roles_text = "all registered roles"
        elif allowed_roles:
            allowed_roles_text = ", ".join(f"`{role_id}`" for role_id in allowed_roles)
        else:
            allowed_roles_text = "none"
        helper_path.parent.mkdir(parents=True, exist_ok=True)
        helper_path.write_text(
            dedent(
                f"""\
                # Requesting External Help

                This attempt can create runtime interrupts without hand-writing inbox files.

                ## Current Task State

                - task_id: `{task.id}`
                - allowed_assistant_roles: {allowed_roles_text}
                - allow_human: `{str(task.interaction_policy.allow_human).lower()}`

                ## Registered Assistant Roles

                {registered_roles_text}

                ## Get A Recommendation

                ```bash
                codex-orch interrupt recommend \\
                  --program-dir "$CODEX_ORCH_PROGRAM_DIR" \\
                  --run-id "$CODEX_ORCH_RUN_ID" \\
                  --task-id "$CODEX_ORCH_TASK_ID" \\
                  --audience assistant \\
                  --kind clarification \\
                  --decision-kind policy
                ```

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
                  --target-role <role-id> \\
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
                - Assistant interrupts must resolve to a concrete assistant role.
                - Start with `codex-orch interrupt recommend`, then use `--target-role` when creating the interrupt.
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
        if task.kind is TaskKind.CONTROLLER:
            assert task.control is not None
            controller_lines = [
                "### Controller Result Contract",
                "- Your final response must be a JSON object saved as `result.json`.",
                "- Use `control` as the only scheduler-facing control surface.",
                "- You may base control on run inputs, dependency results/artifacts, runtime observations, and any replies applied on resume.",
            ]
            if task.control.mode is TaskControlMode.ROUTE:
                controller_lines.extend(
                    [
                        '- Set `control.kind = "route"`.',
                        "- Set `control.labels` to the symbolic labels that should activate downstream route targets.",
                    ]
                )
            else:
                controller_lines.extend(
                    [
                        '- Set `control.kind = "loop"`.',
                        '- Set `control.action` to `"continue"` or `"stop"`.',
                        "- When continuing, include JSON-valued `control.next_inputs` for the next iteration input scope.",
                    ]
                )
            sections.append("\n".join(controller_lines))
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

    def _new_input_scope_id(self) -> str:
        return f"scope-{uuid.uuid4().hex[:8]}"
