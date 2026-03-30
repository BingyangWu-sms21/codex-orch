from __future__ import annotations

import shutil
from pathlib import Path

SHARED_ASSISTANT_OPERATING_MODEL_RELATIVE_PATH = (
    Path("assistant_roles") / "_shared" / "operating-model.md"
)


def builtin_assistant_operating_model_path() -> Path:
    return Path(__file__).with_name("assistant_operating_model.md")


def program_assistant_operating_model_path(program_dir: Path) -> Path:
    return program_dir.resolve() / SHARED_ASSISTANT_OPERATING_MODEL_RELATIVE_PATH


def install_assistant_operating_model(
    program_dir: Path,
    *,
    overwrite: bool = False,
) -> Path:
    destination = program_assistant_operating_model_path(program_dir)
    if destination.exists() and not overwrite:
        raise FileExistsError(
            f"assistant operating model already exists: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(builtin_assistant_operating_model_path(), destination)
    return destination
