from __future__ import annotations

from pathlib import Path

from codex_orch.domain import ComposeStepKind, TaskSpec


class PromptComposer:
    def __init__(self, program_dir: Path) -> None:
        self.program_dir = program_dir

    def render(
        self,
        task: TaskSpec,
        *,
        user_inputs: dict[str, str],
        dependency_node_dirs: dict[str, Path],
    ) -> str:
        sections: list[str] = []
        for step in task.compose:
            if step.kind is ComposeStepKind.FILE and step.path is not None:
                sections.append(
                    self._format_section(
                        f"File Prompt: {step.path}",
                        (self.program_dir / step.path).read_text(
                            encoding="utf-8"
                        ).rstrip(),
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
                published_path = (
                    dependency_node_dirs[step.task] / "published" / step.path
                )
                sections.append(
                    self._format_section(
                        f"Dependency Context: {step.task}/{step.path}",
                        published_path.read_text(encoding="utf-8").rstrip(),
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
