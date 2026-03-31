from __future__ import annotations

from pathlib import Path

from codex_orch.compose_refs import ComposeRefKind, parse_compose_ref
from codex_orch.domain import ComposeStepKind, RunInstanceState, RunRecord
from codex_orch.prompt_context import (
    StagedPromptFile,
    ensure_staged_compose_program_file,
    ensure_staged_generated_text,
    ensure_staged_ref_file,
    read_staged_text,
)
from codex_orch.store import ProjectStore


class PromptComposer:
    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    def render(
        self,
        *,
        run: RunRecord,
        instance: RunInstanceState,
        node_dir: Path,
    ) -> str:
        task = run.tasks[instance.task_id]
        sections: list[str] = []
        for step in task.compose:
            if step.kind is ComposeStepKind.FILE and step.path is not None:
                sections.append(
                    self._format_staged_file_section(
                        f"File Prompt: {step.path}",
                        ensure_staged_compose_program_file(
                            program_dir=self.store.paths.root,
                            node_dir=node_dir,
                            relative_path=step.path,
                        ),
                    )
                )
            elif step.kind is ComposeStepKind.REF and step.ref is not None:
                staged = self._stage_ref(
                    run=run,
                    instance=instance,
                    ref=step.ref,
                    node_dir=node_dir,
                )
                sections.append(self._format_ref_section(step.ref, staged))
            elif step.kind is ComposeStepKind.LITERAL and step.text is not None:
                sections.append(
                    self._format_section(
                        "Literal Context",
                        step.text.rstrip(),
                    )
                )
        return "\n\n".join(section for section in sections if section)

    def _stage_ref(
        self,
        *,
        run: RunRecord,
        instance: RunInstanceState,
        ref: str,
        node_dir: Path,
    ) -> StagedPromptFile:
        task = run.tasks[instance.task_id]
        parsed = parse_compose_ref(ref)
        if parsed.kind is ComposeRefKind.INPUT:
            assert parsed.input_key is not None
            resolved_input = self._resolve_input_value(
                run=run,
                input_scope_id=instance.input_scope_id,
                input_key=parsed.input_key,
            )
            if resolved_input is None:
                raise ValueError(f"missing run input {parsed.input_key}")
            safe_key = self._safe_ref_segment(parsed.input_key)
            return ensure_staged_generated_text(
                node_dir=node_dir,
                source_kind="compose_input_ref",
                source_reference=ref,
                staged_relative_path=f"context/refs/inputs/{safe_key}.txt",
                text=resolved_input,
            )

        assert parsed.scope is not None
        dependency = next(
            (candidate for candidate in task.depends_on if candidate.scope == parsed.scope),
            None,
        )
        if dependency is None:
            raise ValueError(f"dependency scope {parsed.scope} is not available")
        upstream_instance_id = instance.dependency_instances.get(dependency.task)
        if upstream_instance_id is None:
            raise ValueError(
                f"dependency {dependency.task} is not resolved for instance {instance.instance_id}"
            )

        if parsed.kind is ComposeRefKind.DEP_RESULT:
            source_path = self.store.get_result_state_path(run.id, upstream_instance_id)
            return ensure_staged_ref_file(
                node_dir=node_dir,
                source_kind="compose_dep_result_ref",
                source_reference=ref,
                source_path=source_path,
                staged_relative_path=f"context/refs/deps/{self._safe_ref_segment(parsed.scope)}/result.json",
                require_text=True,
                missing_error=(
                    f"compose.ref {ref} is missing materialized result for dependency instance {upstream_instance_id}"
                ),
                non_file_error=(
                    f"compose.ref {ref} did not resolve to a regular materialized result file"
                ),
            )

        assert parsed.artifact_path is not None
        return ensure_staged_ref_file(
            node_dir=node_dir,
            source_kind="compose_dep_artifact_ref",
            source_reference=ref,
            source_path=(
                self.store.get_instance_published_dir(run.id, upstream_instance_id)
                / parsed.artifact_path
            ),
            staged_relative_path=(
                f"context/refs/deps/{self._safe_ref_segment(parsed.scope)}/artifacts/{parsed.artifact_path}"
            ),
            require_text=False,
            missing_error=(
                f"compose.ref {ref} is missing published artifact {parsed.artifact_path}"
            ),
            non_file_error=(
                f"compose.ref {ref} did not resolve to a regular published artifact"
            ),
        )

    def _format_section(self, title: str, body: str) -> str:
        if not body:
            return ""
        return f"## {title}\n\n{body}"

    def _format_staged_file_section(
        self,
        title: str,
        staged_file: StagedPromptFile,
    ) -> str:
        content = read_staged_text(staged_file).rstrip()
        lines = [
            f"## {title}",
            "",
            f"- staged_path: `{staged_file.staged_path}`",
            f"- byte_size: {staged_file.byte_size}",
            f"- sha256: `{staged_file.sha256}`",
            "",
            "### Content",
            "",
            content,
        ]
        return "\n".join(lines).rstrip()

    def _format_ref_section(
        self,
        ref: str,
        staged_file: StagedPromptFile,
    ) -> str:
        content_type = self._content_type(staged_file.staged_path)
        lines = [
            f"## Ref: {ref}",
            "",
            f"- staged_path: `{staged_file.staged_path}`",
            f"- content_type: `{content_type}`",
            f"- byte_size: {staged_file.byte_size}",
            f"- sha256: `{staged_file.sha256}`",
            "",
            "Read this file directly if you need its contents; it is not inlined here.",
        ]
        return "\n".join(lines).rstrip()

    def _content_type(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".json":
            return "application/json"
        if suffix in {".md", ".markdown"}:
            return "text/markdown"
        if suffix in {".txt", ".log", ".yaml", ".yml"}:
            return "text/plain"
        return "application/octet-stream"

    def _safe_ref_segment(self, raw: str) -> str:
        return raw.replace("/", "__")

    def _resolve_input_value(
        self,
        *,
        run: RunRecord,
        input_scope_id: str,
        input_key: str,
    ) -> str | None:
        input_scope = run.input_scopes[input_scope_id]
        if input_key in input_scope.values:
            return input_scope.values[input_key]
        return run.user_inputs.get(input_key)
