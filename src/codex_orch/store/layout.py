from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GlobalPaths:
    root: Path
    presets_dir: Path
    profiles_dir: Path
    config_path: Path


@dataclass(frozen=True)
class ProgramPaths:
    root: Path
    project_file: Path
    tasks_dir: Path
    presets_dir: Path
    prompts_dir: Path
    inputs_dir: Path
    runs_dir: Path


def get_global_paths(root: Path | None = None) -> GlobalPaths:
    global_root = root if root is not None else Path.home() / ".codex-orch"
    return GlobalPaths(
        root=global_root,
        presets_dir=global_root / "presets",
        profiles_dir=global_root / "profiles",
        config_path=global_root / "config.toml",
    )


def ensure_global_layout(root: Path | None = None) -> GlobalPaths:
    paths = get_global_paths(root)
    paths.presets_dir.mkdir(parents=True, exist_ok=True)
    paths.profiles_dir.mkdir(parents=True, exist_ok=True)
    paths.root.mkdir(parents=True, exist_ok=True)
    return paths


def get_program_paths(program_dir: Path) -> ProgramPaths:
    root = program_dir.resolve()
    return ProgramPaths(
        root=root,
        project_file=root / "project.yaml",
        tasks_dir=root / "tasks",
        presets_dir=root / "presets",
        prompts_dir=root / "prompts",
        inputs_dir=root / "inputs",
        runs_dir=root / ".runs",
    )


def ensure_program_layout(program_dir: Path) -> ProgramPaths:
    paths = get_program_paths(program_dir)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.tasks_dir.mkdir(parents=True, exist_ok=True)
    paths.presets_dir.mkdir(parents=True, exist_ok=True)
    paths.prompts_dir.mkdir(parents=True, exist_ok=True)
    paths.inputs_dir.mkdir(parents=True, exist_ok=True)
    paths.runs_dir.mkdir(parents=True, exist_ok=True)
    return paths
