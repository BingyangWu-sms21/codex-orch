from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import TypeAlias

import yaml

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

_INPUT_TEMPLATE_RE = re.compile(r"\$\{inputs\.([^}]+)\}")


def ensure_json_value(value: object, *, field_name: str) -> JsonValue:
    if value is None or isinstance(value, str) or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must not contain NaN or Infinity")
        return value
    if isinstance(value, list):
        return [
            ensure_json_value(item, field_name=f"{field_name}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        normalized: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field_name} object keys must be strings")
            normalized[key] = ensure_json_value(item, field_name=f"{field_name}.{key}")
        return normalized
    raise ValueError(
        f"{field_name} must be a JSON value, got {type(value).__name__}"
    )


def ensure_json_object(value: object, *, field_name: str) -> JsonObject:
    normalized = ensure_json_value(value, field_name=field_name)
    if not isinstance(normalized, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return normalized


def load_input_file_value(path: Path) -> JsonValue:
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"input file {path} is not valid JSON: {exc}") from exc
        return ensure_json_value(raw, field_name=f"input file {path}")
    if suffix in {".yaml", ".yml"}:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return ensure_json_value(raw, field_name=f"input file {path}")
    return path.read_text(encoding="utf-8")


def parse_json_input_override(raw_value: str, *, field_name: str) -> JsonValue:
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON: {exc}") from exc
    return ensure_json_value(parsed, field_name=field_name)


def render_input_template(
    raw_value: str,
    *,
    inputs: dict[str, JsonValue],
    field_name: str,
) -> str:
    def replace(match: re.Match[str]) -> str:
        input_key = match.group(1).strip()
        if not input_key:
            raise ValueError(f"{field_name} contains an empty inputs binding")
        if input_key not in inputs:
            raise ValueError(
                f"{field_name} references missing input key {input_key}"
            )
        value = inputs[input_key]
        if not isinstance(value, str):
            raise ValueError(
                f"{field_name} references inputs.{input_key}, which must be a string for path binding"
            )
        return value

    return _INPUT_TEMPLATE_RE.sub(replace, raw_value)
