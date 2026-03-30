from __future__ import annotations

from pathlib import Path

import yaml

from codex_orch.assistant.base import AssistantBackendRequest
from codex_orch.assistant.codex_cli import (
    CodexCliAssistantBackend,
    _assistant_output_schema,
)
from codex_orch.domain import (
    AssistantRequest,
    DecisionKind,
    RequestKind,
    RequestPriority,
    TaskSpec,
    TaskStatus,
)
from codex_orch.prompt_context import ensure_staged_assistant_artifact
from codex_orch.scheduler import RunService
from tests.helpers import build_test_store, write_assistant_role
from tests.test_run_service import FakeRunner


def test_load_assistant_role_supports_program_relative_asset_paths(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    role_dir = store.get_assistant_role_dir("policy")
    role_dir.mkdir(parents=True, exist_ok=True)
    instructions_path = role_dir / "instructions.md"
    instructions_path.write_text("Prefer concise answers.\n", encoding="utf-8")
    managed_asset_path = role_dir / "preferences.yaml"
    managed_asset_path.write_text("version: 1\npreferences:\n  naming: explicit\n", encoding="utf-8")
    store.get_assistant_role_spec_path("policy").write_text(
        yaml.safe_dump(
            {
                "id": "policy",
                "title": "policy",
                "backend": "codex_cli",
                "sandbox": "workspace-write",
                "instructions": "assistant_roles/policy/instructions.md",
                "managed_assets": ["assistant_roles/policy/preferences.yaml"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    role = store.load_assistant_role("policy")

    assert role.instructions_path == instructions_path
    assert role.managed_asset_paths == (managed_asset_path,)


def test_assistant_backend_exposes_program_and_run_instances_context(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store, managed_asset_contents="version: 1\npreferences:\n  naming: explicit\n")
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            compose=[{"kind": "file", "path": "prompts/implement.md"}],
            publish=["final.md"],
        )
    )
    artifact_path = store.paths.root / "context" / "policy.md"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("Prefer deleting wrappers.\n", encoding="utf-8")

    run = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )
    instance = next(iter(run.instances.values()))
    request = AssistantRequest(
        request_id="int_1",
        run_id=run.id,
        requester_task_id="worker",
        request_kind=RequestKind.CLARIFICATION,
        question="Should I delete the wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["delete", "keep_wrapper"],
        context_artifacts=["context/policy.md"],
        priority=RequestPriority.HIGH,
    )
    artifact = ensure_staged_assistant_artifact(
        program_dir=store.paths.root,
        node_dir=store.get_instance_dir(run.id, instance.instance_id),
        relative_path="context/policy.md",
    )
    backend_request = AssistantBackendRequest(
        program_dir=store.paths.root,
        role=store.load_assistant_role("policy"),
        project=store.load_project(),
        task=instance.task,
        instance_id=instance.instance_id,
        assistant_request=request,
        artifacts=(artifact,),
        allow_human_handoff=True,
    )

    prompt = CodexCliAssistantBackend()._build_prompt(backend_request)

    assert "- run_instances_dir:" in prompt
    assert instance.instance_id in prompt
    assert "Prefer deleting wrappers." in prompt
    assert "Managed Role Assets" in prompt
    assert "naming: explicit" in prompt


def test_assistant_backend_formats_large_and_binary_artifacts(tmp_path: Path) -> None:
    store = build_test_store(tmp_path)
    write_assistant_role(store)
    store.save_task(
        TaskSpec(
            id="worker",
            title="Worker",
            agent="default",
            status=TaskStatus.READY,
            publish=["final.md"],
        )
    )
    large_artifact = store.paths.root / "context" / "large.md"
    large_artifact.parent.mkdir(parents=True, exist_ok=True)
    large_artifact.write_text("A" * (17 * 1024), encoding="utf-8")
    binary_artifact = store.paths.root / "context" / "blob.bin"
    binary_artifact.write_bytes(b"\x00\x01\x02binary")

    run = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )
    instance = next(iter(run.instances.values()))
    instance_dir = store.get_instance_dir(run.id, instance.instance_id)
    large = ensure_staged_assistant_artifact(
        program_dir=store.paths.root,
        node_dir=instance_dir,
        relative_path="context/large.md",
    )
    blob = ensure_staged_assistant_artifact(
        program_dir=store.paths.root,
        node_dir=instance_dir,
        relative_path="context/blob.bin",
    )

    backend = CodexCliAssistantBackend()
    large_text = backend._format_artifact_section(large)
    blob_text = backend._format_artifact_section(blob)

    assert "Preview only: artifact exceeded inline size limit." in large_text
    assert "Content omitted: artifact is not UTF-8 text." in blob_text


def test_assistant_backend_schema_blocks_handoff_when_human_is_disallowed() -> None:
    schema = _assistant_output_schema(allow_human_handoff=False)

    assert schema["properties"]["resolution_kind"]["enum"] == ["auto_reply"]
