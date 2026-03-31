from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urljoin, unquote, urlparse
from urllib.request import url2pathname

import yaml
from jsonschema import ValidationError, validators
from referencing import Registry, Resource
from referencing import exceptions as referencing_exceptions
from referencing.jsonschema import specification_with

from codex_orch.input_values import JsonValue, ensure_json_value


def load_json_schema(path: Path) -> JsonValue:
    if not path.exists():
        raise ValueError(f"schema does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"schema path is not a file: {path}")
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    else:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"schema file {path} is not valid JSON: {exc}") from exc
    return ensure_json_value(raw, field_name=f"schema {path}")


def validate_json_schema(
    payload: JsonValue,
    *,
    schema_path: Path,
    field_name: str,
) -> None:
    schema = load_json_schema(schema_path)
    validator_cls = validators.validator_for(schema)
    default_specification = specification_with(validator_cls.META_SCHEMA["$schema"])
    schema_uri = schema_path.resolve().as_uri()
    schema = _schema_with_absolute_id(
        schema,
        default_uri=schema_uri,
    )
    validator_cls.check_schema(schema)
    validator = validator_cls(
        schema,
        registry=Registry(
            retrieve=lambda uri: _load_schema_resource(
                uri,
                default_specification=default_specification,
            )
        ),
    )
    try:
        validator.validate(payload)
    except ValidationError as exc:
        raise ValueError(
            f"{field_name} does not match schema {schema_path}: {exc.message}"
        ) from exc
    except (
        referencing_exceptions.Unresolvable,
        referencing_exceptions.Unretrievable,
    ) as exc:
        raise ValueError(
            f"{field_name} uses an unresolved schema reference from {schema_path}: {exc}"
        ) from exc


def _load_schema_resource(
    uri: str,
    *,
    default_specification: object,
) -> Resource[JsonValue]:
    try:
        path = _file_uri_to_path(uri)
        contents = load_json_schema(path)
    except (ValueError, OSError) as exc:
        raise referencing_exceptions.Unretrievable(ref=uri) from exc
    contents = _schema_with_absolute_id(
        contents,
        default_uri=path.resolve().as_uri(),
    )
    return Resource.from_contents(
        contents,
        default_specification=default_specification,
    )


def _schema_with_absolute_id(
    schema: JsonValue,
    *,
    default_uri: str,
) -> JsonValue:
    if not isinstance(schema, dict):
        return schema
    schema_id = schema.get("$id")
    if not isinstance(schema_id, str) or not schema_id:
        return {
            **schema,
            "$id": default_uri,
        }
    absolute_schema_id = urljoin(default_uri, schema_id)
    if absolute_schema_id == schema_id:
        return schema
    return {
        **schema,
        "$id": absolute_schema_id,
    }


def _file_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise referencing_exceptions.Unretrievable(ref=uri)
    authority = ""
    if parsed.netloc and parsed.netloc != "localhost":
        authority = f"//{parsed.netloc}"
    return Path(authority + url2pathname(unquote(parsed.path)))
