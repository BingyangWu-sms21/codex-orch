from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from codex_orch.assistant.base import AssistantBackendRequest, AssistantBackendResult
from codex_orch.domain import ConfidenceLevel, ControlActionKind, ResolutionKind

_ASSISTANT_OUTPUT_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "resolution_kind": {
            "type": "string",
            "enum": [
                ResolutionKind.AUTO_REPLY.value,
                ResolutionKind.HANDOFF_TO_HUMAN.value,
            ],
        },
        "answer": {"type": "string", "minLength": 1},
        "rationale": {"type": "string", "minLength": 1},
        "confidence": {
            "type": "string",
            "enum": [level.value for level in ConfidenceLevel],
        },
        "citations": {"type": "array", "items": {"type": "string"}},
        "proposed_guidance_updates": {
            "type": "array",
            "items": {"type": "string"},
        },
        "proposed_control_actions": {
            "type": "array",
            "items": {"type": "string", "enum": [kind.value for kind in ControlActionKind]},
        },
    },
    "required": [
        "resolution_kind",
        "answer",
        "rationale",
        "confidence",
        "citations",
        "proposed_guidance_updates",
        "proposed_control_actions",
    ],
}


class CodexCliAssistantBackend:
    def respond(self, request: AssistantBackendRequest) -> AssistantBackendResult:
        prompt = self._build_prompt(request)
        schema_path = self._write_schema_file(request.profile.profile_dir)
        try:
            result = subprocess.run(
                self._build_command(request, schema_path),
                cwd=request.profile.workspace_dir,
                env=self._build_environment(request),
                capture_output=True,
                text=True,
                check=False,
                input=prompt,
            )
        finally:
            schema_path.unlink(missing_ok=True)

        if result.returncode != 0:
            message = result.stderr.strip() or "codex assistant backend failed"
            raise RuntimeError(message)

        final_message = self._extract_final_agent_message(result.stdout)
        if not final_message:
            raise ValueError("codex assistant backend did not emit an agent_message")
        payload = json.loads(final_message)
        return AssistantBackendResult(
            resolution_kind=ResolutionKind(payload["resolution_kind"]),
            answer=payload["answer"],
            rationale=payload["rationale"],
            confidence=ConfidenceLevel(payload["confidence"]),
            citations=tuple(payload.get("citations", [])),
            proposed_guidance_updates=tuple(
                payload.get("proposed_guidance_updates", [])
            ),
            proposed_control_actions=tuple(
                ControlActionKind(raw)
                for raw in payload.get("proposed_control_actions", [])
            ),
        )

    def _build_command(
        self,
        request: AssistantBackendRequest,
        schema_path: Path,
    ) -> list[str]:
        command = [
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--cd",
            str(request.profile.workspace_dir),
        ]
        sandbox = request.profile.spec.sandbox
        if sandbox == "workspace-write":
            command.append("--full-auto")
            for visible_root in self._command_visible_roots(request):
                command.extend(["--add-dir", str(visible_root)])
        elif sandbox == "danger-full-access":
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.extend(["--sandbox", sandbox])
            for visible_root in self._command_visible_roots(request):
                command.extend(["--add-dir", str(visible_root)])
        if request.profile.spec.model is not None:
            command.extend(["--model", request.profile.spec.model])
        command.extend(["--output-schema", str(schema_path), "-"])
        return command

    def _build_environment(self, request: AssistantBackendRequest) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "CODEX_ORCH_PROGRAM_DIR": str(request.program_dir),
                "CODEX_ORCH_RUN_ID": request.assistant_request.run_id,
                "CODEX_ORCH_TASK_ID": request.task.id,
                "CODEX_ORCH_ASSISTANT_PROFILE_ID": request.profile.spec.id,
                "CODEX_ORCH_ASSISTANT_PROFILE_DIR": str(request.profile.profile_dir),
                "CODEX_ORCH_ASSISTANT_WORKSPACE_DIR": str(request.profile.workspace_dir),
                "CODEX_ORCH_RUN_DIR": str(self._run_dir(request)),
                "CODEX_ORCH_RUN_INSTANCES_DIR": str(self._run_instances_dir(request)),
            }
        )
        return env

    def _write_schema_file(self, profile_dir: Path) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix="assistant-schema-",
            dir=profile_dir,
            delete=False,
        ) as handle:
            json.dump(_ASSISTANT_OUTPUT_SCHEMA, handle, indent=2, sort_keys=True)
            handle.write("\n")
            return Path(handle.name)

    def _build_prompt(self, request: AssistantBackendRequest) -> str:
        instructions = request.profile.instructions_path.read_text(encoding="utf-8").strip()
        metadata_lines = [
            f"- project_name: {request.project.name}",
            f"- run_id: {request.assistant_request.run_id}",
            f"- requester_task_id: {request.task.id}",
            f"- requester_task_title: {request.task.title}",
            f"- requester_task_agent: {request.task.agent}",
            f"- request_kind: {request.assistant_request.request_kind.value}",
            f"- decision_kind: {request.assistant_request.decision_kind.value}",
            f"- priority: {request.assistant_request.priority.value}",
        ]
        if request.task.description:
            metadata_lines.append(f"- requester_task_description: {request.task.description}")
        if request.assistant_request.options:
            metadata_lines.append(
                f"- options: {', '.join(request.assistant_request.options)}"
            )
        if request.assistant_request.requested_control_actions:
            metadata_lines.append(
                "- requested_control_actions: "
                + ", ".join(
                    action.value
                    for action in request.assistant_request.requested_control_actions
                )
            )

        artifact_sections: list[str] = []
        for artifact in request.artifacts:
            artifact_sections.append(self._format_artifact_section(artifact))

        requester_instance_dir = self._run_instances_dir(request) / request.instance_id
        accessible_paths = [
            f"- assistant_profile_workspace: `{request.profile.workspace_dir}`",
            f"- program_dir: `{request.program_dir}`",
            f"- run_dir: `{self._run_dir(request)}`",
            f"- run_instances_dir: `{self._run_instances_dir(request)}`",
            f"- requester_instance_dir: `{requester_instance_dir}`",
            (
                "- Use the assistant profile workspace for persistent notes and preferences. "
                "Treat the program and run directories as observational context and do not modify them while answering this request."
            ),
        ]
        sections = [
            "# Assistant Profile Instructions",
            instructions or "(no profile instructions provided)",
            "# Run Context",
            "\n".join(metadata_lines),
            "# Accessible Paths",
            "\n".join(accessible_paths),
            "# Assistant Request",
            request.assistant_request.question.strip(),
            "# Artifact Context",
            "\n\n".join(artifact_sections)
            if artifact_sections
            else "No context artifacts were attached.",
            "# Response Contract",
            "\n".join(
                [
                    "Return only JSON matching the provided schema.",
                    "Choose resolution_kind=auto_reply when you can answer directly.",
                    "Choose resolution_kind=handoff_to_human when the decision depends on user preference, policy approval, or ambiguous product direction.",
                    "Keep citations grounded in the provided artifacts or stable repo/user guidance paths.",
                    "Keep proposed_control_actions empty unless a control-plane action is truly necessary.",
                    "Treat proposed_control_actions and proposed_guidance_updates as proposals only; codex-orch records them but does not execute them automatically.",
                ]
            ),
        ]
        return "\n\n".join(section for section in sections if section)

    def _format_artifact_section(self, artifact) -> str:
        lines = [
            f"## {artifact.source_reference}",
            f"Source path: {artifact.source_path}",
            f"Staged path: {artifact.staged_path}",
            f"Byte size: {artifact.byte_size}",
            f"SHA-256: {artifact.sha256}",
        ]
        if not artifact.is_text:
            lines.append("Content omitted: artifact is not UTF-8 text.")
            return "\n".join(lines)
        if artifact.inline_text is not None:
            lines.extend(["```text", artifact.inline_text.rstrip(), "```"])
            return "\n".join(lines)
        preview = artifact.preview_text.rstrip() if artifact.preview_text is not None else ""
        lines.extend(
            [
                "Preview only: artifact exceeded inline size limit.",
                "```text",
                preview,
                "```",
            ]
        )
        return "\n".join(lines)

    def _command_visible_roots(
        self,
        request: AssistantBackendRequest,
    ) -> tuple[Path, ...]:
        candidates = [
            request.program_dir,
            self._run_instances_dir(request),
        ]
        deduped: list[Path] = []
        seen: set[str] = {str(request.profile.workspace_dir)}
        for path in candidates:
            normalized = str(path)
            if normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(path)
        return tuple(deduped)

    def _run_dir(self, request: AssistantBackendRequest) -> Path:
        return request.program_dir / ".runs" / request.assistant_request.run_id

    def _run_instances_dir(self, request: AssistantBackendRequest) -> Path:
        return self._run_dir(request) / "instances"

    def _extract_final_agent_message(self, stdout: str) -> str:
        final_message = ""
        for raw_line in stdout.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if payload.get("type") != "item.completed":
                continue
            item = payload.get("item")
            if not isinstance(item, dict):
                continue
            if item.get("type") != "agent_message":
                continue
            text = item.get("text")
            if isinstance(text, str):
                final_message = text
        return final_message
