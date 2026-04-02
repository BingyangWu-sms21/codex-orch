from __future__ import annotations

import shutil
from pathlib import Path


def templates_root() -> Path:
    return Path(__file__).parent / "templates"


def list_templates() -> list[str]:
    root = templates_root()
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir())


def copy_template(
    template_name: str,
    dest: Path,
    *,
    name: str,
    workspace: str,
) -> None:
    root = templates_root()
    template_dir = root / template_name
    if not template_dir.is_dir():
        available = ", ".join(list_templates()) or "(none)"
        raise ValueError(
            f"template {template_name!r} not found; available: {available}"
        )
    dest.mkdir(parents=True, exist_ok=True)
    _copy_tree(template_dir, dest)
    project_yaml = dest / "project.yaml"
    if project_yaml.exists():
        content = project_yaml.read_text(encoding="utf-8")
        content = content.replace("{{name}}", name)
        content = content.replace("{{workspace}}", workspace)
        project_yaml.write_text(content, encoding="utf-8")


def _copy_tree(src: Path, dst: Path) -> None:
    for item in sorted(src.iterdir()):
        dst_item = dst / item.name
        if item.is_dir():
            dst_item.mkdir(exist_ok=True)
            _copy_tree(item, dst_item)
        else:
            shutil.copy2(item, dst_item)
