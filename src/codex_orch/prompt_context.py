from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

INLINE_TEXT_BYTE_LIMIT = 16 * 1024
PREVIEW_TEXT_BYTE_LIMIT = 4 * 1024


@dataclass(frozen=True)
class StagedPromptFile:
    source_kind: str
    source_reference: str
    source_path: Path
    staged_path: Path
    staged_relative_path: str
    byte_size: int
    sha256: str
    is_text: bool
    inline_text: str | None
    preview_text: str | None
    truncated: bool


def ensure_staged_assistant_artifact(
    *,
    program_dir: Path,
    node_dir: Path,
    relative_path: str,
) -> StagedPromptFile:
    return _ensure_staged_file(
        node_dir=node_dir,
        source_kind="assistant_request_artifact",
        source_reference=relative_path,
        source_path=program_dir / relative_path,
        staged_path=node_dir / "context" / "assistant_request" / relative_path,
        require_text=False,
        missing_error=(
            f"context artifact {relative_path} does not exist under program dir"
        ),
        non_file_error=(
            f"context artifact {relative_path} is not a regular file under program dir"
        ),
    )


def ensure_staged_compose_program_file(
    *,
    program_dir: Path,
    node_dir: Path,
    relative_path: str,
) -> StagedPromptFile:
    return _ensure_staged_file(
        node_dir=node_dir,
        source_kind="compose_program_file",
        source_reference=relative_path,
        source_path=program_dir / relative_path,
        staged_path=node_dir / "context" / "compose" / "program" / relative_path,
        require_text=True,
        missing_error=f"compose.file {relative_path} does not exist under program dir",
        non_file_error=(
            f"compose.file {relative_path} is not a regular file under program dir"
        ),
    )


def ensure_staged_dependency_file(
    *,
    node_dir: Path,
    dependency_task_id: str,
    dependency_node_dir: Path,
    relative_path: str,
) -> StagedPromptFile:
    return _ensure_staged_file(
        node_dir=node_dir,
        source_kind="compose_dependency_file",
        source_reference=f"{dependency_task_id}/{relative_path}",
        source_path=dependency_node_dir / "published" / relative_path,
        staged_path=(
            node_dir
            / "context"
            / "compose"
            / "deps"
            / dependency_task_id
            / relative_path
        ),
        require_text=True,
        missing_error=(
            f"compose.from_dep {dependency_task_id}:{relative_path} is missing from "
            "the dependency published artifacts"
        ),
        non_file_error=(
            f"compose.from_dep {dependency_task_id}:{relative_path} is not a regular "
            "published file"
        ),
    )


def upsert_context_manifest_entry(node_dir: Path, staged_file: StagedPromptFile) -> None:
    manifest_path = node_dir / "context" / "manifest.json"
    payload = {"entries": []}
    if manifest_path.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = payload.get("entries", [])
    if not isinstance(entries, list):
        entries = []
    entry_key = (staged_file.source_kind, staged_file.source_reference)
    filtered_entries = [
        entry
        for entry in entries
        if not (
            isinstance(entry, dict)
            and (
                entry.get("source_kind"),
                entry.get("source_reference"),
            )
            == entry_key
        )
    ]
    filtered_entries.append(
        {
            "source_kind": staged_file.source_kind,
            "source_reference": staged_file.source_reference,
            "source_path": str(staged_file.source_path),
            "staged_path": str(staged_file.staged_path),
            "staged_relative_path": staged_file.staged_relative_path,
            "byte_size": staged_file.byte_size,
            "sha256": staged_file.sha256,
            "content_kind": "text" if staged_file.is_text else "binary",
            "truncated": staged_file.truncated,
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"entries": filtered_entries}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_staged_text(staged_file: StagedPromptFile) -> str:
    raw = staged_file.staged_path.read_bytes()
    return _decode_text(raw, error_label=staged_file.source_reference)


def _ensure_staged_file(
    *,
    node_dir: Path,
    source_kind: str,
    source_reference: str,
    source_path: Path,
    staged_path: Path,
    require_text: bool,
    missing_error: str,
    non_file_error: str,
) -> StagedPromptFile:
    if staged_path.exists():
        if not staged_path.is_file():
            raise ValueError(f"staged prompt context is not a regular file: {staged_path}")
        raw = staged_path.read_bytes()
    else:
        if not source_path.exists():
            raise ValueError(missing_error)
        if not source_path.is_file():
            raise ValueError(non_file_error)
        raw = source_path.read_bytes()
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_bytes(raw)

    is_text, inline_text, preview_text, truncated = _classify_content(
        raw,
        require_text=require_text,
        error_label=source_reference,
    )
    staged_file = StagedPromptFile(
        source_kind=source_kind,
        source_reference=source_reference,
        source_path=source_path,
        staged_path=staged_path,
        staged_relative_path=str(staged_path.relative_to(node_dir)),
        byte_size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        is_text=is_text,
        inline_text=inline_text,
        preview_text=preview_text,
        truncated=truncated,
    )
    upsert_context_manifest_entry(node_dir, staged_file)
    return staged_file


def _classify_content(
    raw: bytes,
    *,
    require_text: bool,
    error_label: str,
) -> tuple[bool, str | None, str | None, bool]:
    decoded = _maybe_decode_text(raw, require_text=require_text, error_label=error_label)
    if decoded is None:
        return False, None, None, False
    if len(raw) <= INLINE_TEXT_BYTE_LIMIT:
        return True, decoded, None, False
    preview = raw[:PREVIEW_TEXT_BYTE_LIMIT].decode("utf-8", errors="replace")
    return True, None, preview, True


def _decode_text(raw: bytes, *, error_label: str) -> str:
    decoded = _maybe_decode_text(raw, require_text=True, error_label=error_label)
    assert decoded is not None
    return decoded


def _maybe_decode_text(
    raw: bytes,
    *,
    require_text: bool,
    error_label: str,
) -> str | None:
    if b"\x00" in raw:
        if require_text:
            raise ValueError(f"{error_label} must be UTF-8 text, not binary content")
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        if require_text:
            raise ValueError(f"{error_label} must be UTF-8 text")
        return None
