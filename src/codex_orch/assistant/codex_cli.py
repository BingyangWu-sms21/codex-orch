from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import yaml
from pydantic import ValidationError

from codex_orch.assistant.base import AssistantBackendRequest, AssistantBackendResult
from codex_orch.domain import (
    AssistantUpdateProposal,
    ConfidenceLevel,
    ResolutionKind,
)
from codex_orch.input_values import ensure_json_object

logger = logging.getLogger(__name__)


def _parse_proposed_updates(raw_updates: list[object]) -> tuple[AssistantUpdateProposal, ...]:
    proposals: list[AssistantUpdateProposal] = []
    for proposal_index, raw in enumerate(raw_updates, start=1):
        try:
            proposals.append(AssistantUpdateProposal.model_validate(raw))
        except ValidationError as exc:
            logger.warning(
                "Dropping invalid assistant proposed_update #%s: %s",
                proposal_index,
                exc,
            )
    return tuple(proposals)


def _assistant_output_schema(*, allow_human_handoff: bool) -> dict[str, object]:
    allowed_resolution_kinds = [ResolutionKind.AUTO_REPLY.value]
    if allow_human_handoff:
        allowed_resolution_kinds.append(ResolutionKind.HANDOFF_TO_HUMAN.value)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "resolution_kind": {
                "type": "string",
                "enum": allowed_resolution_kinds,
            },
            "answer": {"type": "string", "minLength": 1},
            "rationale": {"type": "string", "minLength": 1},
            "confidence": {
                "type": "string",
                "enum": [level.value for level in ConfidenceLevel],
            },
            "citations": {"type": "array", "items": {"type": "string"}},
            "payload": {
                "type": "object",
            },
            "proposed_updates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "instruction_update",
                                "managed_asset_update",
                                "routing_policy_update",
                                "program_asset_update",
                            ],
                        },
                        "summary": {"type": "string", "minLength": 1},
                        "rationale": {"type": "string", "minLength": 1},
                        "suggested_content_mode": {
                            "type": "string",
                            "enum": ["snippet", "full_replacement"],
                        },
                        "suggested_content": {"type": "string", "minLength": 1},
                        "target": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "role_id": {"type": "string", "minLength": 1},
                                "managed_asset_path": {
                                    "type": "string",
                                    "minLength": 1,
                                },
                                "task_id": {"type": "string", "minLength": 1},
                                "routing_section": {
                                    "type": "string",
                                    "enum": [
                                        "assistant_hints",
                                        "interaction_policy",
                                    ],
                                },
                            },
                        },
                    },
                    "required": [
                        "kind",
                        "summary",
                        "rationale",
                        "suggested_content_mode",
                        "suggested_content",
                        "target",
                    ],
                },
            },
        },
        "required": [
            "resolution_kind",
            "answer",
            "rationale",
            "confidence",
            "citations",
            "payload",
            "proposed_updates",
        ],
    }


class CodexCliAssistantBackend:
    def respond(self, request: AssistantBackendRequest) -> AssistantBackendResult:
        prompt = self._build_prompt(request)
        schema_path = self._write_schema_file(
            request.role.role_dir,
            allow_human_handoff=request.allow_human_handoff,
        )
        try:
            result = subprocess.run(
                self._build_command(request, schema_path),
                cwd=request.role.workspace_dir,
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
            payload=ensure_json_object(
                payload.get("payload", {}),
                field_name="assistant payload",
            ),
            proposed_updates=_parse_proposed_updates(payload.get("proposed_updates", [])),
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
            str(request.role.workspace_dir),
        ]
        sandbox = request.role.spec.sandbox
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
        if request.role.spec.model is not None:
            command.extend(["--model", request.role.spec.model])
        command.extend(["--output-schema", str(schema_path), "-"])
        return command

    def _build_environment(self, request: AssistantBackendRequest) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "CODEX_ORCH_PROGRAM_DIR": str(request.program_dir),
                "CODEX_ORCH_RUN_ID": request.assistant_request.run_id,
                "CODEX_ORCH_TASK_ID": request.task.id,
                "CODEX_ORCH_ASSISTANT_ROLE_ID": request.role.spec.id,
                "CODEX_ORCH_ASSISTANT_ROLE_DIR": str(request.role.role_dir),
                "CODEX_ORCH_ASSISTANT_ROLE_WORKSPACE_DIR": str(request.role.workspace_dir),
                "CODEX_ORCH_ASSISTANT_MANAGED_ASSET_PATHS": os.pathsep.join(
                    str(path) for path in request.role.managed_asset_paths
                ),
                "CODEX_ORCH_ASSISTANT_OPERATING_MODEL_PATH": str(
                    request.shared_operating_model_path
                ),
                "CODEX_ORCH_RUN_DIR": str(self._run_dir(request)),
                "CODEX_ORCH_RUN_INSTANCES_DIR": str(self._run_instances_dir(request)),
            }
        )
        return env

    def _write_schema_file(
        self,
        role_dir: Path,
        *,
        allow_human_handoff: bool,
    ) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix="assistant-schema-",
            dir=role_dir,
            delete=False,
        ) as handle:
            json.dump(
                _assistant_output_schema(
                    allow_human_handoff=allow_human_handoff,
                ),
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            return Path(handle.name)

    def _build_prompt(self, request: AssistantBackendRequest) -> str:
        shared_operating_model = request.shared_operating_model_path.read_text(
            encoding="utf-8"
        ).strip()
        instructions = request.role.instructions_path.read_text(encoding="utf-8").strip()
        metadata_lines = [
            f"- project_name: {request.project.name}",
            f"- run_id: {request.assistant_request.run_id}",
            f"- assistant_role_id: {request.role.spec.id}",
            f"- assistant_role_title: {request.role.spec.title or request.role.spec.id}",
            f"- requester_task_id: {request.task.id}",
            f"- requester_task_title: {request.task.title}",
            f"- requester_task_agent: {request.task.agent}",
            f"- request_kind: {request.assistant_request.request_kind.value}",
            f"- decision_kind: {request.assistant_request.decision_kind.value}",
            f"- priority: {request.assistant_request.priority.value}",
            f"- allow_human_handoff: {str(request.allow_human_handoff).lower()}",
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

        managed_asset_sections = [
            self._format_managed_asset_section(path)
            for path in request.role.managed_asset_paths
        ]

        requester_instance_dir = self._run_instances_dir(request) / request.instance_id
        accessible_paths = [
            f"- assistant_role_dir: `{request.role.role_dir}`",
            f"- assistant_role_workspace: `{request.role.workspace_dir}`",
            f"- program_dir: `{request.program_dir}`",
            f"- run_dir: `{self._run_dir(request)}`",
            f"- run_instances_dir: `{self._run_instances_dir(request)}`",
            f"- requester_instance_dir: `{requester_instance_dir}`",
            (
                "- Managed role assets are the authoritative long-term guidance. "
                "Use the assistant role workspace only for private scratch notes, not as preference truth."
            ),
            (
                "- Treat the program and run directories as observational context and do not modify them while answering this request."
            ),
        ]
        routing_context_sections = [
            "## assistant_hints",
            "```yaml",
            yaml.safe_dump(
                request.task.assistant_hints.model_dump(
                    mode="json",
                    exclude_defaults=True,
                ),
                sort_keys=False,
            ).strip()
            or "{}",
            "```",
            "## interaction_policy",
            "```yaml",
            yaml.safe_dump(
                request.task.interaction_policy.model_dump(
                    mode="json",
                    exclude_defaults=True,
                ),
                sort_keys=False,
            ).strip()
            or "{}",
            "```",
        ]
        reply_schema_sections: list[str] = []
        if request.reply_schema_path is not None:
            schema_text = request.reply_schema_path.read_text(encoding="utf-8").strip()
            reply_schema_sections = [
                "# Reply Payload Schema",
                f"Reply payload path: `{request.reply_schema_path}`",
                (
                    schema_text
                    if schema_text
                    else "(reply schema file exists but is empty)"
                ),
            ]
        sections = [
            "# Assistant Operating Model",
            shared_operating_model or "(assistant operating model missing content)",
            "# Assistant Role Instructions",
            instructions or "(no role instructions provided)",
            "# Managed Role Assets",
            "\n\n".join(managed_asset_sections)
            if managed_asset_sections
            else "No managed role assets were configured.",
            "# Run Context",
            "\n".join(metadata_lines),
            "# Requester Task Routing Context",
            "\n".join(routing_context_sections),
            "# Accessible Paths",
            "\n".join(accessible_paths),
            "# Assistant Request",
            request.assistant_request.question.strip(),
            *reply_schema_sections,
            "# Artifact Context",
            "\n\n".join(artifact_sections)
            if artifact_sections
            else "No context artifacts were attached.",
            "# Response Contract",
            "\n".join(
                [
                    "Return only JSON matching the provided schema.",
                    "Choose resolution_kind=auto_reply when you can answer directly.",
                    (
                        "Choose resolution_kind=handoff_to_human when the decision depends on user preference, policy approval, or ambiguous product direction."
                        if request.allow_human_handoff
                        else "Human handoff is not allowed for this task. Always return resolution_kind=auto_reply."
                    ),
                    "Keep citations grounded in the provided artifacts or stable repo/user guidance paths.",
                    (
                        "Set payload to a JSON object matching the reply payload schema above."
                        if request.reply_schema_path is not None
                        else "Set payload to a JSON object. Use `{}` when no structured payload is needed."
                    ),
                    "Use proposed_updates for repository-facing update proposals about instructions, managed assets, routing policy, or program-owned assets such as inputs.",
                    "Treat proposed_updates as proposals only; codex-orch records them but does not execute them automatically.",
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

    def _format_managed_asset_section(self, path: Path) -> str:
        lines = [f"## {path.name}", f"Path: {path}"]
        try:
            content = path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            lines.append("Content omitted: managed asset is not UTF-8 text.")
            return "\n".join(lines)
        lines.extend(["```text", content, "```"])
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
        seen: set[str] = {str(request.role.workspace_dir)}
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
