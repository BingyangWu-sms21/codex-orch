from __future__ import annotations

from pathlib import Path

from codex_orch.domain import ComposeStepKind, TaskSpec
from codex_orch.prompt_context import (
    StagedPromptFile,
    ensure_staged_compose_program_file,
    ensure_staged_dependency_file,
    read_staged_text,
)


class PromptComposer:
    def __init__(self, program_dir: Path) -> None:
        self.program_dir = program_dir

    def render(
        self,
        task: TaskSpec,
        *,
        node_dir: Path,
        user_inputs: dict[str, str],
        dependency_node_dirs: dict[str, Path],
    ) -> str:
        sections: list[str] = []
        for step in task.compose:
            if step.kind is ComposeStepKind.FILE and step.path is not None:
                sections.append(
                    self._format_staged_file_section(
                        f"File Prompt: {step.path}",
                        ensure_staged_compose_program_file(
                            program_dir=self.program_dir,
                            node_dir=node_dir,
                            relative_path=step.path,
                        ),
                    )
                )
            elif step.kind is ComposeStepKind.USER_INPUT and step.key is not None:
                if step.key not in user_inputs:
                    raise ValueError(f"missing user input {step.key}")
                sections.append(
                    self._format_section(
                        f"User Input: {step.key}",
                        user_inputs[step.key].rstrip(),
                    )
                )
            elif (
                step.kind is ComposeStepKind.FROM_DEP
                and step.task is not None
                and step.path is not None
            ):
                if step.task not in dependency_node_dirs:
                    raise ValueError(f"dependency node {step.task} is not available")
                sections.append(
                    self._format_staged_file_section(
                        f"Dependency Context: {step.task}/{step.path}",
                        ensure_staged_dependency_file(
                            node_dir=node_dir,
                            dependency_task_id=step.task,
                            dependency_node_dir=dependency_node_dirs[step.task],
                            relative_path=step.path,
                        ),
                    )
                )
            elif step.kind is ComposeStepKind.LITERAL and step.text is not None:
                sections.append(
                    self._format_section(
                        "Literal Context",
                        step.text.rstrip(),
                    )
                )
        return "\n\n".join(section for section in sections if section)

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
