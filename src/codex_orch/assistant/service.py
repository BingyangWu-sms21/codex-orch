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
from codex_orch.domain import AssistantBackendKind, ResolutionKind, TaskSpec
from codex_orch.scheduler import RunService
from codex_orch.store import AssistantRequestRecord, ProjectStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssistantWorkerStats:
    scanned: int = 0
    processed: int = 0
    auto_replied: int = 0
    handed_off: int = 0
    skipped_no_profile: int = 0
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
        self._reported_request_issues: dict[str, str] = {}

    def run_once(self) -> AssistantWorkerStats:
        scanned = 0
        processed = 0
        auto_replied = 0
        handed_off = 0
        skipped_no_profile = 0
        failed = 0
        for record in self.store.list_assistant_requests(unresolved_only=True):
            scanned += 1
            outcome = self._process_record(record)
            if outcome == "processed:auto_reply":
                processed += 1
                auto_replied += 1
            elif outcome == "processed:handoff":
                processed += 1
                handed_off += 1
            elif outcome == "skipped:no_profile":
                skipped_no_profile += 1
            else:
                failed += 1
        return AssistantWorkerStats(
            scanned=scanned,
            processed=processed,
            auto_replied=auto_replied,
            handed_off=handed_off,
            skipped_no_profile=skipped_no_profile,
            failed=failed,
        )

    def serve_forever(self, *, poll_interval_sec: float) -> None:
        while True:
            self.run_once()
            time.sleep(poll_interval_sec)

    def _process_record(self, record: AssistantRequestRecord) -> str:
        request_id = record.request.request_id
        profile_id = self.store.resolve_assistant_profile_id(record.run_id, record.task_id)
        if profile_id is None:
            self._report_request_issue(
                request_id,
                "assistant request has no effective assistant profile; leaving unresolved",
            )
            return "skipped:no_profile"

        try:
            profile = self.store.load_assistant_profile(profile_id)
            task = self._resolve_task(record)
            project = self.store.load_project()
            backend_request = AssistantBackendRequest(
                program_dir=self.store.paths.root,
                profile=profile,
                project=project,
                task=task,
                assistant_request=record.request,
                artifacts=tuple(self._load_artifacts(record)),
            )
            backend = self._resolve_backend(profile.spec.backend)
            result = backend.respond(backend_request)
            if result.resolution_kind not in {
                ResolutionKind.AUTO_REPLY,
                ResolutionKind.HANDOFF_TO_HUMAN,
            }:
                raise ValueError(
                    "automated assistant must return auto_reply or handoff_to_human"
                )
            self.store.save_assistant_response_by_request_id(
                request_id,
                resolution_kind=result.resolution_kind,
                answer=result.answer,
                rationale=result.rationale,
                confidence=result.confidence,
                citations=list(result.citations),
                proposed_guidance_updates=list(result.proposed_guidance_updates),
                proposed_control_actions=list(result.proposed_control_actions),
            )
            self._reported_request_issues.pop(request_id, None)
            if result.resolution_kind is ResolutionKind.AUTO_REPLY:
                asyncio.run(self.run_service.resume_run(record.run_id))
                return "processed:auto_reply"
            return "processed:handoff"
        except Exception as exc:  # pragma: no cover - exercised by higher-level tests
            self._report_request_issue(
                request_id,
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

    def _resolve_task(self, record: AssistantRequestRecord) -> TaskSpec:
        task = self.store.maybe_get_run_task(record.run_id, record.task_id)
        if task is not None:
            return task
        return self.store.get_task(record.task_id)

    def _load_artifacts(
        self,
        record: AssistantRequestRecord,
    ) -> list[AssistantArtifactContext]:
        artifacts: list[AssistantArtifactContext] = []
        for relative_path in record.request.context_artifacts:
            absolute_path = self.store.paths.root / relative_path
            content = None
            if absolute_path.exists():
                content = absolute_path.read_text(encoding="utf-8")
            artifacts.append(
                AssistantArtifactContext(
                    relative_path=relative_path,
                    absolute_path=absolute_path,
                    content=content,
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
