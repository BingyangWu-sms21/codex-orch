from __future__ import annotations

from dataclasses import dataclass

from codex_orch.domain import DecisionKind, InterruptAudience, RequestKind, TaskSpec
from codex_orch.store import ProjectStore, ResolvedAssistantRole


@dataclass(frozen=True)
class AssistantRoleRecommendation:
    allowed_role_ids: tuple[str, ...]
    ranked_role_ids: tuple[str, ...]
    recommended_target_role_id: str | None
    recommendation_reason: str | None
    allow_human: bool


@dataclass(frozen=True)
class AssistantTargetResolution:
    requested_target_role_id: str | None
    recommended_target_role_id: str | None
    resolved_target_role_id: str
    target_resolution_reason: str


class AssistantRoleRouter:
    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    def recommend(
        self,
        *,
        run_id: str,
        task_id: str,
        request_kind: RequestKind,
        decision_kind: DecisionKind | None,
    ) -> AssistantRoleRecommendation:
        task = self._resolve_task(run_id=run_id, task_id=task_id)
        roles = self.store.list_assistant_roles()
        self._validate_task_role_references(task, roles)

        allowed_role_ids = self._allowed_role_ids(task=task, roles=roles)
        preferred_roles = set(task.assistant_hints.preferred_roles)
        ask_when_signals = set(task.assistant_hints.ask_when)
        task_labels = set(task.labels)

        if decision_kind is not None:
            override_role_id = task.assistant_hints.decision_kind_overrides.get(
                decision_kind.value
            )
            if override_role_id is not None:
                if override_role_id not in allowed_role_ids:
                    raise ValueError(
                        f"task {task.id} decision_kind_overrides points to disallowed role {override_role_id}"
                    )
                return AssistantRoleRecommendation(
                    allowed_role_ids=allowed_role_ids,
                    ranked_role_ids=(override_role_id, *tuple(role_id for role_id in allowed_role_ids if role_id != override_role_id)),
                    recommended_target_role_id=override_role_id,
                    recommendation_reason=(
                        "task assistant_hints.decision_kind_overrides matched "
                        f"{decision_kind.value}"
                    ),
                    allow_human=task.interaction_policy.allow_human,
                )

        scored: list[tuple[tuple[int, int, int, int, int, int], str, tuple[str, ...]]] = []
        for role_id in allowed_role_ids:
            role = roles[role_id]
            request_match = request_kind in role.spec.policy.request_kinds
            decision_match = (
                decision_kind is not None
                and decision_kind in role.spec.policy.decision_kinds
            )
            label_match = bool(task_labels & set(role.spec.policy.task_labels_any))
            ask_when_match = bool(ask_when_signals & set(role.spec.policy.ask_when))
            preferred_match = role_id in preferred_roles

            reasons: list[str] = []
            if decision_match and request_match and decision_kind is not None:
                reasons.append(
                    f"matched decision_kind={decision_kind.value} and request_kind={request_kind.value}"
                )
            elif decision_match and decision_kind is not None:
                reasons.append(f"matched decision_kind={decision_kind.value}")
            elif request_match:
                reasons.append(f"matched request_kind={request_kind.value}")
            if label_match:
                reasons.append("matched task labels")
            if ask_when_match:
                reasons.append("matched assistant_hints.ask_when")
            score = (
                1 if decision_match and request_match else 0,
                1 if decision_match else 0,
                1 if request_match else 0,
                1 if label_match else 0,
                1 if ask_when_match else 0,
                1 if preferred_match else 0,
            )
            scored.append((score, role_id, tuple(reasons)))

        def sort_key(
            item: tuple[tuple[int, int, int, int, int, int], str, tuple[str, ...]]
        ) -> tuple[int, int, int, int, int, int, str]:
            score, role_id, _ = item
            return (
                -score[0],
                -score[1],
                -score[2],
                -score[3],
                -score[4],
                -score[5],
                role_id,
            )

        ranked = tuple(
            role_id
            for _, role_id, _ in sorted(
                scored,
                key=sort_key,
            )
        )
        top_scored = sorted(
            scored,
            key=sort_key,
        )
        positive = [
            item
            for item in top_scored
            if any(item[0][0:5])
        ]
        if not positive:
            return AssistantRoleRecommendation(
                allowed_role_ids=allowed_role_ids,
                ranked_role_ids=ranked,
                recommended_target_role_id=None,
                recommendation_reason=None,
                allow_human=task.interaction_policy.allow_human,
            )

        top_score = positive[0][0]
        top_same_without_preference = [
            item
            for item in positive
            if item[0][0:5] == top_score[0:5]
        ]
        reason_parts = list(positive[0][2]) or ["matched role policy"]
        if len(top_same_without_preference) > 1 and positive[0][0][5]:
            reason_parts.append("preferred_roles broke the tie")
        return AssistantRoleRecommendation(
            allowed_role_ids=allowed_role_ids,
            ranked_role_ids=ranked,
            recommended_target_role_id=positive[0][1],
            recommendation_reason="; ".join(reason_parts),
            allow_human=task.interaction_policy.allow_human,
        )

    def resolve_assistant_target(
        self,
        *,
        run_id: str,
        task_id: str,
        request_kind: RequestKind,
        decision_kind: DecisionKind | None,
        requested_target_role_id: str | None,
    ) -> tuple[AssistantRoleRecommendation, AssistantTargetResolution]:
        recommendation = self.recommend(
            run_id=run_id,
            task_id=task_id,
            request_kind=request_kind,
            decision_kind=decision_kind,
        )
        if requested_target_role_id is not None:
            if requested_target_role_id not in recommendation.allowed_role_ids:
                raise ValueError(
                    f"task {task_id} does not allow assistant role {requested_target_role_id}"
                )
            return recommendation, AssistantTargetResolution(
                requested_target_role_id=requested_target_role_id,
                recommended_target_role_id=recommendation.recommended_target_role_id,
                resolved_target_role_id=requested_target_role_id,
                target_resolution_reason=(
                    f"worker explicitly requested assistant role {requested_target_role_id}"
                ),
            )
        if recommendation.recommended_target_role_id is None:
            raise ValueError(
                "no assistant role recommendation is available; pass --target-role explicitly"
            )
        return recommendation, AssistantTargetResolution(
            requested_target_role_id=None,
            recommended_target_role_id=recommendation.recommended_target_role_id,
            resolved_target_role_id=recommendation.recommended_target_role_id,
            target_resolution_reason=(
                recommendation.recommendation_reason
                or f"recommended assistant role {recommendation.recommended_target_role_id}"
            ),
        )

    def validate_human_interrupt_allowed(self, *, run_id: str, task_id: str) -> None:
        task = self._resolve_task(run_id=run_id, task_id=task_id)
        if not task.interaction_policy.allow_human:
            raise ValueError(f"task {task.id} does not allow human interrupts")

    def _resolve_task(self, *, run_id: str, task_id: str) -> TaskSpec:
        task = self.store.maybe_get_run_task(run_id, task_id)
        if task is not None:
            return task
        return self.store.get_task(task_id)

    def _allowed_role_ids(
        self,
        *,
        task: TaskSpec,
        roles: dict[str, ResolvedAssistantRole],
    ) -> tuple[str, ...]:
        if task.interaction_policy.allowed_assistant_roles is None:
            return tuple(sorted(roles))
        return tuple(sorted(set(task.interaction_policy.allowed_assistant_roles)))

    def _validate_task_role_references(
        self,
        task: TaskSpec,
        roles: dict[str, ResolvedAssistantRole],
    ) -> None:
        known_role_ids = set(roles)
        references = set(task.assistant_hints.preferred_roles)
        references.update(task.assistant_hints.decision_kind_overrides.values())
        allowed = task.interaction_policy.allowed_assistant_roles
        if allowed is not None:
            references.update(allowed)
        unknown = sorted(reference for reference in references if reference not in known_role_ids)
        if unknown:
            raise ValueError(
                f"task {task.id} references unknown assistant roles: {', '.join(unknown)}"
            )

    def validate_interrupt_audience(
        self,
        *,
        run_id: str,
        task_id: str,
        audience: InterruptAudience,
    ) -> None:
        if audience is InterruptAudience.HUMAN:
            self.validate_human_interrupt_allowed(run_id=run_id, task_id=task_id)
