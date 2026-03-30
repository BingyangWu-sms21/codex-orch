from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath


def _validate_relative_file_path(raw_path: str) -> str:
    candidate = PurePosixPath(raw_path)
    if candidate.is_absolute():
        raise ValueError("paths must be relative")
    if ".." in candidate.parts:
        raise ValueError("paths must not escape the run context")
    normalized = str(candidate)
    if normalized == ".":
        raise ValueError("path must point to a file")
    return normalized


def _validate_segment(raw_value: str, *, field_name: str) -> str:
    normalized = raw_value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if normalized.startswith(".") or normalized.endswith("."):
        raise ValueError(f"{field_name} must not start or end with '.'")
    return normalized


class ComposeRefKind(StrEnum):
    DEP_RESULT = "dep_result"
    DEP_ARTIFACT = "dep_artifact"
    INPUT = "input"


@dataclass(frozen=True)
class ParsedComposeRef:
    raw: str
    kind: ComposeRefKind
    scope: str | None = None
    artifact_path: str | None = None
    input_key: str | None = None


def parse_compose_ref(raw_ref: str) -> ParsedComposeRef:
    normalized = raw_ref.strip()
    if not normalized:
        raise ValueError("compose.ref must not be empty")

    if normalized.startswith("deps."):
        parts = normalized.split(".")
        if len(parts) < 3:
            raise ValueError(
                "deps refs must be deps.<scope>.result or deps.<scope>.artifacts.<path>"
            )
        scope = _validate_segment(parts[1], field_name="deps scope")
        namespace = parts[2]
        if namespace == "result":
            if len(parts) != 3:
                raise ValueError(
                    "field-level result refs are not supported; use deps.<scope>.result"
                )
            return ParsedComposeRef(
                raw=normalized,
                kind=ComposeRefKind.DEP_RESULT,
                scope=scope,
            )
        if namespace == "artifacts":
            if len(parts) < 4:
                raise ValueError(
                    "artifact refs must be deps.<scope>.artifacts.<relative-path>"
                )
            artifact_path = _validate_relative_file_path(".".join(parts[3:]))
            return ParsedComposeRef(
                raw=normalized,
                kind=ComposeRefKind.DEP_ARTIFACT,
                scope=scope,
                artifact_path=artifact_path,
            )
        raise ValueError(
            "deps refs must use .result or .artifacts.<relative-path>"
        )

    if normalized.startswith("inputs."):
        input_key = _validate_segment(normalized[len("inputs.") :], field_name="input key")
        return ParsedComposeRef(
            raw=normalized,
            kind=ComposeRefKind.INPUT,
            input_key=input_key,
        )

    raise ValueError(
        "compose.ref must start with deps.<scope>.result, "
        "deps.<scope>.artifacts.<relative-path>, or inputs.<key>"
    )
