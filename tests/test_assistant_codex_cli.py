from __future__ import annotations

from pathlib import Path

from codex_orch.assistant.base import AssistantBackendRequest
from codex_orch.assistant.codex_cli import CodexCliAssistantBackend
from codex_orch.domain import DecisionKind, RequestKind, RequestPriority, TaskSpec, TaskStatus
from codex_orch.prompt_context import ensure_staged_assistant_artifact
from codex_orch.scheduler import RunService
from tests.helpers import build_test_store, write_assistant_profile
from tests.test_run_service import FakeRunner


def test_assistant_backend_exposes_program_and_run_nodes_context(
    tmp_path: Path,
) -> None:
    store = build_test_store(tmp_path)
    write_assistant_profile(store, set_as_default=True)
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

    snapshot = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )
    request = store.create_assistant_request(
        run_id=snapshot.id,
        task_id="worker",
        request_kind=RequestKind.CLARIFICATION,
        question="Should I delete the wrapper?",
        decision_kind=DecisionKind.POLICY,
        options=["delete", "keep_wrapper"],
        context_artifacts=["context/policy.md"],
        requested_control_actions=[],
        priority=RequestPriority.HIGH,
    )

    task = snapshot.nodes["worker"].task
    profile = store.load_assistant_profile("assistant-default")
    staged_artifact = ensure_staged_assistant_artifact(
        program_dir=store.paths.root,
        node_dir=store.get_node_dir(snapshot.id, "worker"),
        relative_path="context/policy.md",
    )
    backend_request = AssistantBackendRequest(
        program_dir=store.paths.root,
        profile=profile,
        project=store.load_project(),
        task=task,
        assistant_request=request,
        artifacts=(staged_artifact,),
    )
    backend = CodexCliAssistantBackend()

    command = backend._build_command(backend_request, profile.profile_dir / "schema.json")
    run_dir = store.paths.root / ".runs" / snapshot.id
    run_nodes_dir = run_dir / "nodes"
    assert command[:7] == [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--cd",
        str(profile.workspace_dir),
        "--full-auto",
    ]
    assert "--add-dir" in command
    assert str(store.paths.root) in command
    assert str(run_nodes_dir) in command
    assert command[-1] == "-"

    env = backend._build_environment(backend_request)
    assert env["CODEX_ORCH_RUN_DIR"] == str(run_dir)
    assert env["CODEX_ORCH_RUN_NODES_DIR"] == str(run_nodes_dir)

    prompt = backend._build_prompt(backend_request)
    assert "# Accessible Paths" in prompt
    assert f"- program_dir: `{store.paths.root}`" in prompt
    assert f"- run_nodes_dir: `{run_nodes_dir}`" in prompt
    assert f"- requester_node_dir: `{run_nodes_dir / 'worker'}`" in prompt
    assert "Treat the program and run directories as observational context" in prompt
    assert "## context/policy.md" in prompt
    assert f"Staged path: {staged_artifact.staged_path}" in prompt
    assert "- backend:" not in prompt
    assert "- profile_id:" not in prompt
    assert "- request_id:" not in prompt


def test_assistant_backend_formats_large_and_binary_artifacts(
    tmp_path: Path,
) -> None:
    store = build_test_store(tmp_path)
    write_assistant_profile(store, set_as_default=True)
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
    large_artifact = store.paths.root / "context" / "large.md"
    large_artifact.parent.mkdir(parents=True, exist_ok=True)
    large_artifact.write_text("A" * (17 * 1024), encoding="utf-8")
    binary_artifact = store.paths.root / "context" / "blob.bin"
    binary_artifact.write_bytes(b"\x00\x01\x02binary")

    snapshot = RunService(store, FakeRunner()).create_snapshot(
        roots=["worker"],
        labels=[],
        user_inputs=None,
    )
    request = store.create_assistant_request(
        run_id=snapshot.id,
        task_id="worker",
        request_kind=RequestKind.CLARIFICATION,
        question="Need artifact context?",
        decision_kind=DecisionKind.POLICY,
        options=["yes", "no"],
        context_artifacts=["context/large.md", "context/blob.bin"],
        requested_control_actions=[],
        priority=RequestPriority.NORMAL,
    )
    backend_request = AssistantBackendRequest(
        program_dir=store.paths.root,
        profile=store.load_assistant_profile("assistant-default"),
        project=store.load_project(),
        task=snapshot.nodes["worker"].task,
        assistant_request=request,
        artifacts=(
            ensure_staged_assistant_artifact(
                program_dir=store.paths.root,
                node_dir=store.get_node_dir(snapshot.id, "worker"),
                relative_path="context/large.md",
            ),
            ensure_staged_assistant_artifact(
                program_dir=store.paths.root,
                node_dir=store.get_node_dir(snapshot.id, "worker"),
                relative_path="context/blob.bin",
            ),
        ),
    )

    prompt = CodexCliAssistantBackend()._build_prompt(backend_request)

    assert "Preview only: artifact exceeded inline size limit." in prompt
    assert "Content omitted: artifact is not UTF-8 text." in prompt
    assert "A" * 512 in prompt
