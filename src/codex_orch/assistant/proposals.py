from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from codex_orch.domain import (
    AssistantUpdateKind,
    AssistantUpdateProposal,
    AssistantUpdateProposalRecord,
    TaskSpec,
)
from codex_orch.store import ProjectStore, ResolvedAssistantRole


@dataclass(frozen=True)
class DroppedProposal:
    proposal: AssistantUpdateProposal
    reason: str


def resolve_update_proposals(
    *,
    store: ProjectStore,
    run_id: str,
    instance_id: str,
    interrupt_id: str,
    role: ResolvedAssistantRole,
    task: TaskSpec,
    proposals: tuple[AssistantUpdateProposal, ...],
) -> tuple[list[AssistantUpdateProposalRecord], list[DroppedProposal]]:
    resolved: list[AssistantUpdateProposalRecord] = []
    dropped: list[DroppedProposal] = []
    for proposal_index, proposal in enumerate(proposals, start=1):
        try:
            resolved.append(
                _resolve_one(
                    store=store,
                    run_id=run_id,
                    instance_id=instance_id,
                    interrupt_id=interrupt_id,
                    proposal_index=proposal_index,
                    role=role,
                    task=task,
                    proposal=proposal,
                )
            )
        except ValueError as exc:
            dropped.append(DroppedProposal(proposal=proposal, reason=str(exc)))
    return resolved, dropped


def _resolve_one(
    *,
    store: ProjectStore,
    run_id: str,
    instance_id: str,
    interrupt_id: str,
    proposal_index: int,
    role: ResolvedAssistantRole,
    task: TaskSpec,
    proposal: AssistantUpdateProposal,
) -> AssistantUpdateProposalRecord:
    target_file_path = _resolve_target_file_path(
        store=store,
        role=role,
        task=task,
        proposal=proposal,
    )
    now = datetime.now(UTC).isoformat()
    return AssistantUpdateProposalRecord(
        proposal_id=f"prop-{interrupt_id}-{proposal_index:02d}",
        run_id=run_id,
        instance_id=instance_id,
        interrupt_id=interrupt_id,
        source_role_id=role.spec.id,
        requester_task_id=task.id,
        proposal=proposal,
        target_file_path=str(target_file_path),
        created_at=now,
        status_updated_at=now,
    )


def _resolve_target_file_path(
    *,
    store: ProjectStore,
    role: ResolvedAssistantRole,
    task: TaskSpec,
    proposal: AssistantUpdateProposal,
):
    target = proposal.target
    if proposal.kind is AssistantUpdateKind.INSTRUCTION_UPDATE:
        if target.role_id != role.spec.id:
            raise ValueError(
                f"instruction_update may only target current role {role.spec.id}"
            )
        return role.instructions_path
    if proposal.kind is AssistantUpdateKind.MANAGED_ASSET_UPDATE:
        if target.role_id != role.spec.id:
            raise ValueError(
                f"managed_asset_update may only target current role {role.spec.id}"
            )
        assert target.managed_asset_path is not None
        declared_asset_map = _declared_asset_map(role)
        target_asset_path = declared_asset_map.get(target.managed_asset_path)
        if target_asset_path is None:
            raise ValueError(
                f"managed_asset_update must target one of the current role's declared managed assets: {', '.join(sorted(declared_asset_map))}"
            )
        return target_asset_path
    if target.task_id != task.id:
        raise ValueError(
            f"routing_policy_update may only target current requester task {task.id}"
        )
    return store.paths.tasks_dir / f"{task.id}.yaml"


def _declared_asset_map(role: ResolvedAssistantRole) -> dict[str, Path]:
    return {
        declared: resolved
        for declared, resolved in zip(
            role.spec.managed_assets,
            role.managed_asset_paths,
            strict=True,
        )
    }
