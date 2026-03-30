from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass

from codex_orch.assistant.base import (
    AssistantArtifactContext,
    AssistantBackend,
    AssistantBackendRequest,
)
from codex_orch.assistant.routing import AssistantRoleRouter
from codex_orch.domain import (
    AssistantBackendKind,
    AssistantRequest,
    DecisionKind,
    InterruptAudience,
    InterruptReplyKind,
    ResolutionKind,
    TaskSpec,
)
from codex_orch.prompt_context import ensure_staged_assistant_artifact
from codex_orch.scheduler import RunService
from codex_orch.store import InterruptRecord, ProjectStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssistantWorkerStats:
    scanned: int = 0
    processed: int = 0
    auto_replied: int = 0
    handed_off: int = 0
    skipped_no_role: int = 0
    failed: int = 0


class AssistantWorkerService:
    def __init__(
        self,
        store: ProjectStore,
        *,
        backend: AssistantBackend | None = None,
        backend_registry: Mapping[AssistantBackendKind, AssistantBackend] | None = None,
        run_service: RunService,
    ) -> None:
        self.store = store
        self.backends: dict[AssistantBackendKind, AssistantBackend] = {}
        if backend_registry is not None:
            self.backends.update(backend_registry)
        if backend is not None:
            self.backends.setdefault(AssistantBackendKind.CODEX_CLI, backend)
        self.run_service = run_service
        self.router = AssistantRoleRouter(store)
        self._reported_request_issues: dict[str, str] = {}

    def run_once(self) -> AssistantWorkerStats:
        scanned = 0
        processed = 0
        auto_replied = 0
        handed_off = 0
        skipped_no_role = 0
        failed = 0
        for record in self.store.list_interrupts(
            audience=InterruptAudience.ASSISTANT,
            unresolved_only=True,
        ):
            scanned += 1
            outcome = self._process_record(record)
            if outcome == "processed:auto_reply":
                processed += 1
                auto_replied += 1
            elif outcome == "processed:handoff":
                processed += 1
                handed_off += 1
            elif outcome == "skipped:no_role":
                skipped_no_role += 1
            else:
                failed += 1
        return AssistantWorkerStats(
            scanned=scanned,
            processed=processed,
            auto_replied=auto_replied,
            handed_off=handed_off,
            skipped_no_role=skipped_no_role,
            failed=failed,
        )

    def serve_forever(self, *, poll_interval_sec: float) -> None:
        while True:
            self.run_once()
            time.sleep(poll_interval_sec)

    def _process_record(self, record: InterruptRecord) -> str:
        interrupt_id = record.interrupt.interrupt_id
        resolved_role_id = record.interrupt.resolved_target_role_id
        if resolved_role_id is None:
            self._report_request_issue(
                interrupt_id,
                "assistant interrupt is missing resolved assistant role; leaving unresolved",
            )
            return "skipped:no_role"

        try:
            role = self.store.load_assistant_role(resolved_role_id)
            task = self._resolve_task(record)
            project = self.store.load_project()
            backend_request = AssistantBackendRequest(
                program_dir=self.store.paths.root,
                role=role,
                project=project,
                task=task,
                instance_id=record.instance_id,
                assistant_request=self._to_assistant_request(record),
                artifacts=tuple(self._load_artifacts(record)),
                allow_human_handoff=task.interaction_policy.allow_human,
            )
            backend = self._resolve_backend(role.spec.backend)
            result = backend.respond(backend_request)
            if result.resolution_kind not in {
                ResolutionKind.AUTO_REPLY,
                ResolutionKind.HANDOFF_TO_HUMAN,
            }:
                raise ValueError(
                    "automated assistant must return auto_reply or handoff_to_human"
                )
            if result.resolution_kind is ResolutionKind.AUTO_REPLY:
                self.store.save_interrupt_reply(
                    interrupt_id,
                    audience=InterruptAudience.ASSISTANT,
                    reply_kind=InterruptReplyKind.ANSWER,
                    text=result.answer,
                    payload={},
                    rationale=result.rationale,
                    confidence=result.confidence,
                    citations=list(result.citations),
                )
                self._reported_request_issues.pop(interrupt_id, None)
                asyncio.run(self.run_service.resume_run(record.run_id))
                return "processed:auto_reply"

            self.router.validate_human_interrupt_allowed(
                run_id=record.run_id,
                task_id=record.task_id,
            )
            self.store.save_interrupt_reply(
                interrupt_id,
                audience=InterruptAudience.ASSISTANT,
                reply_kind=InterruptReplyKind.HANDOFF_TO_HUMAN,
                text=result.answer,
                payload={},
                rationale=result.rationale,
                confidence=result.confidence,
                citations=list(result.citations),
            )
            self.store.create_interrupt(
                run_id=record.run_id,
                instance_id=record.instance_id,
                audience=InterruptAudience.HUMAN,
                blocking=record.interrupt.blocking,
                request_kind=record.interrupt.request_kind,
                question=record.interrupt.question,
                decision_kind=record.interrupt.decision_kind,
                options=list(record.interrupt.options),
                context_artifacts=list(record.interrupt.context_artifacts),
                reply_schema=record.interrupt.reply_schema,
                priority=record.interrupt.priority,
                requested_target_role_id=None,
                recommended_target_role_id=None,
                resolved_target_role_id=None,
                target_resolution_reason=None,
                metadata={
                    "assistant_summary": result.answer,
                    "assistant_rationale": result.rationale,
                    "assistant_citations": list(result.citations),
                },
            )
            self._reported_request_issues.pop(interrupt_id, None)
            return "processed:handoff"
        except Exception as exc:  # pragma: no cover - defensive
            self._report_request_issue(
                interrupt_id,
                f"assistant worker failed: {exc}",
                exc=exc,
            )
            return "failed"

    def _resolve_backend(
        self,
        backend_kind: AssistantBackendKind,
    ) -> AssistantBackend:
        backend = self.backends.get(backend_kind)
        if backend is None:
            raise ValueError(f"assistant backend {backend_kind.value} is not registered")
        return backend

    def _resolve_task(self, record: InterruptRecord) -> TaskSpec:
        task = self.store.maybe_get_run_task(record.run_id, record.task_id)
        if task is not None:
            return task
        return self.store.get_task(record.task_id)

    def _to_assistant_request(self, record: InterruptRecord) -> AssistantRequest:
        return AssistantRequest(
            request_id=record.interrupt.interrupt_id,
            run_id=record.run_id,
            requester_task_id=record.task_id,
            request_kind=record.interrupt.request_kind,
            question=record.interrupt.question,
            decision_kind=record.interrupt.decision_kind or DecisionKind.RECOVERY,
            options=list(record.interrupt.options),
            context_artifacts=list(record.interrupt.context_artifacts),
            priority=record.interrupt.priority,
        )

    def _load_artifacts(
        self,
        record: InterruptRecord,
    ) -> list[AssistantArtifactContext]:
        artifacts: list[AssistantArtifactContext] = []
        instance_dir = self.store.get_instance_dir(record.run_id, record.instance_id)
        for relative_path in record.interrupt.context_artifacts:
            artifacts.append(
                ensure_staged_assistant_artifact(
                    program_dir=self.store.paths.root,
                    node_dir=instance_dir,
                    relative_path=relative_path,
                )
            )
        return artifacts

    def _report_request_issue(
        self,
        request_id: str,
        message: str,
        *,
        exc: Exception | None = None,
    ) -> None:
        if self._reported_request_issues.get(request_id) == message:
            return
        self._reported_request_issues[request_id] = message
        if exc is None:
            logger.warning("%s [%s]", message, request_id)
            return
        logger.exception("%s [%s]", message, request_id)
