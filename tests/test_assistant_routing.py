from __future__ import annotations

import pytest

from codex_orch.assistant import AssistantRoleRouter
from codex_orch.domain import DecisionKind, RequestKind, TaskSpec, TaskStatus
from codex_orch.scheduler import RunService
from tests.helpers import build_test_store, write_assistant_role
from tests.test_run_service import FakeRunner


def test_router_uses_preferred_roles_as_tie_break(tmp_path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store, role_id="policy")
    write_assistant_role(store, role_id="policy-alt")
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            assistant_hints={"preferred_roles": ["policy-alt"]},
            publish=["final.md"],
        )
    )
    run = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )

    recommendation = AssistantRoleRouter(store).recommend(
        run_id=run.id,
        task_id="worker",
        request_kind=RequestKind.CLARIFICATION,
        decision_kind=DecisionKind.POLICY,
    )

    assert recommendation.recommended_target_role_id == "policy-alt"
    assert recommendation.ranked_role_ids[0] == "policy-alt"


def test_router_rejects_explicit_override_outside_task_allowlist(tmp_path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store, role_id="policy")
    write_assistant_role(store, role_id="style")
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            interaction_policy={"allowed_assistant_roles": ["policy"]},
            publish=["final.md"],
        )
    )
    run = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )

    with pytest.raises(ValueError, match="does not allow assistant role style"):
        AssistantRoleRouter(store).resolve_assistant_target(
            run_id=run.id,
            task_id="worker",
            request_kind=RequestKind.CLARIFICATION,
            decision_kind=DecisionKind.POLICY,
            requested_target_role_id="style",
        )


def test_router_returns_no_recommendation_without_positive_role_signals(tmp_path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(
        store,
        role_id="review",
        request_kinds=["approval"],
        decision_kinds=["review"],
    )
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            publish=["final.md"],
        )
    )
    run = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )

    recommendation = AssistantRoleRouter(store).recommend(
        run_id=run.id,
        task_id="worker",
        request_kind=RequestKind.CLARIFICATION,
        decision_kind=DecisionKind.POLICY,
    )

    assert recommendation.allowed_role_ids == ("review",)
    assert recommendation.recommended_target_role_id is None


def test_router_blocks_human_when_task_policy_disables_it(tmp_path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store)
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            interaction_policy={"allow_human": False},
            publish=["final.md"],
        )
    )
    run = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )

    with pytest.raises(ValueError, match="does not allow human interrupts"):
        AssistantRoleRouter(store).validate_human_interrupt_allowed(
            run_id=run.id,
            task_id="worker",
        )
