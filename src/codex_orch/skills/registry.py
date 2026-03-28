from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BuiltinSkill:
    skill_id: str
    resource_dir: Path
    export_dir_name: str
    description: str


def list_builtin_skills() -> list[BuiltinSkill]:
    root = Path(__file__).resolve().parent
    return [
        BuiltinSkill(
            skill_id="request-assistant",
            resource_dir=root / "request_assistant",
            export_dir_name="request-assistant",
            description="Create assistant requests without hand-writing protocol envelopes.",
        )
    ]


def get_builtin_skill(skill_id: str) -> BuiltinSkill:
    normalized = skill_id.strip().lower()
    for skill in list_builtin_skills():
        if skill.skill_id == normalized:
            return skill
    raise KeyError(f"builtin skill {skill_id} does not exist")


def export_builtin_skill(
    skill_id: str,
    destination_dir: Path,
    *,
    overwrite: bool = False,
) -> Path:
    skill = get_builtin_skill(skill_id)
    if not skill.resource_dir.exists():
        raise FileNotFoundError(f"missing builtin skill resource at {skill.resource_dir}")
    destination_dir.mkdir(parents=True, exist_ok=True)
    export_path = destination_dir / skill.export_dir_name
    if export_path.exists():
        if not overwrite:
            raise FileExistsError(f"skill export target already exists: {export_path}")
        shutil.rmtree(export_path)
    shutil.copytree(skill.resource_dir, export_path)
    return export_path


def install_builtin_skill(
    skill_id: str,
    *,
    repo_dir: Path | None = None,
    user_scope: bool = False,
    overwrite: bool = False,
) -> Path:
    if user_scope:
        destination_dir = Path.home() / ".codex" / "skills"
    else:
        if repo_dir is None:
            raise ValueError("repo_dir is required when installing to repo scope")
        destination_dir = repo_dir / ".codex" / "skills"
    return export_builtin_skill(
        skill_id,
        destination_dir,
        overwrite=overwrite,
    )
