from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from codex_orch.domain import (
    AssistantRequest,
    ConfidenceLevel,
    ControlActionKind,
    ProjectSpec,
    ResolutionKind,
    TaskSpec,
)
from codex_orch.prompt_context import StagedPromptFile as AssistantArtifactContext
from codex_orch.store import ResolvedAssistantProfile


@dataclass(frozen=True)
class AssistantBackendRequest:
    program_dir: Path
    profile: ResolvedAssistantProfile
    project: ProjectSpec
    task: TaskSpec
    instance_id: str
    assistant_request: AssistantRequest
    artifacts: tuple[AssistantArtifactContext, ...]


@dataclass(frozen=True)
class AssistantBackendResult:
    resolution_kind: ResolutionKind
    answer: str
    rationale: str
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM
    citations: tuple[str, ...] = ()
    proposed_guidance_updates: tuple[str, ...] = ()
    proposed_control_actions: tuple[ControlActionKind, ...] = ()


class AssistantBackend(Protocol):
    def respond(self, request: AssistantBackendRequest) -> AssistantBackendResult:
        ...
