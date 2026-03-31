from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, unquote, urlparse
from urllib.request import url2pathname

import yaml
from jsonschema import ValidationError, validators
from referencing import Registry, Resource
from referencing import exceptions as referencing_exceptions
from referencing.jsonschema import specification_with

from codex_orch.input_values import JsonValue, ensure_json_value


@dataclass(frozen=True)
class OutputSchemaCompatibilityWarning:
    code: str
    object_path: str
    message: str


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


def validate_output_schema_compatibility(
    *,
    schema_path: Path,
    field_name: str,
) -> list[OutputSchemaCompatibilityWarning]:
    schema = load_json_schema(schema_path)
    validator_cls = validators.validator_for(schema)
    try:
        validator_cls.check_schema(schema)
    except Exception as exc:
        raise ValueError(f"{field_name} is not a valid JSON schema: {exc}") from exc
    warnings: list[OutputSchemaCompatibilityWarning] = []
    _collect_output_schema_compatibility_warnings(
        schema,
        warnings=warnings,
        object_path="$",
    )
    return warnings


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


_ALLOWED_SCHEMA_KEYS = {
    "$schema",
    "title",
    "description",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
}

_UNSUPPORTED_SCHEMA_KEYS = {
    "default",
    "oneOf",
    "allOf",
    "not",
    "patternProperties",
    "if",
    "then",
    "else",
    "$ref",
    "$defs",
}


def _collect_output_schema_compatibility_warnings(
    node: JsonValue,
    *,
    warnings: list[OutputSchemaCompatibilityWarning],
    object_path: str,
) -> None:
    if not isinstance(node, dict):
        return
    for key in node:
        if key in _UNSUPPORTED_SCHEMA_KEYS:
            warnings.append(
                OutputSchemaCompatibilityWarning(
                    code="output_schema.unsupported_keyword",
                    object_path=f"{object_path}.{key}",
                    message=f"keyword `{key}` is outside codex-orch's conservative Codex output-schema subset",
                )
            )
        elif key not in _ALLOWED_SCHEMA_KEYS:
            warnings.append(
                OutputSchemaCompatibilityWarning(
                    code="output_schema.unknown_keyword",
                    object_path=f"{object_path}.{key}",
                    message=f"keyword `{key}` is not recognized by codex-orch's conservative Codex output-schema subset",
                )
            )

    if "properties" in node:
        properties = node.get("properties")
        if node.get("type") != "object":
            warnings.append(
                OutputSchemaCompatibilityWarning(
                    code="output_schema.object_missing_type",
                    object_path=object_path,
                    message="object schemas with `properties` should declare `type: object`",
                )
            )
        if isinstance(properties, dict):
            required = node.get("required")
            if not isinstance(required, list):
                warnings.append(
                    OutputSchemaCompatibilityWarning(
                        code="output_schema.object_missing_required",
                        object_path=object_path,
                        message="object schemas with `properties` should declare `required` for every property",
                    )
                )
            else:
                missing = [key for key in properties if key not in required]
                if missing:
                    warnings.append(
                        OutputSchemaCompatibilityWarning(
                            code="output_schema.object_required_coverage",
                            object_path=object_path,
                            message=(
                                "object schema should list every property in `required`; "
                                f"missing {', '.join(missing)}"
                            ),
                        )
                    )
            for property_name, property_schema in properties.items():
                _collect_output_schema_compatibility_warnings(
                    property_schema,
                    warnings=warnings,
                    object_path=f"{object_path}.properties.{property_name}",
                )

    if "items" in node:
        if node.get("type") != "array":
            warnings.append(
                OutputSchemaCompatibilityWarning(
                    code="output_schema.array_missing_type",
                    object_path=object_path,
                    message="array schemas with `items` should declare `type: array`",
                )
            )
        _collect_output_schema_compatibility_warnings(
            node["items"],
            warnings=warnings,
            object_path=f"{object_path}.items",
        )

    if "const" in node and "type" not in node:
        warnings.append(
            OutputSchemaCompatibilityWarning(
                code="output_schema.const_missing_type",
                object_path=object_path,
                message="schemas using `const` should declare an explicit `type`",
            )
        )
    if "enum" in node and "type" not in node:
        warnings.append(
            OutputSchemaCompatibilityWarning(
                code="output_schema.enum_missing_type",
                object_path=object_path,
                message="schemas using `enum` should declare an explicit `type`",
            )
        )

    additional_properties = node.get("additionalProperties")
    if isinstance(additional_properties, dict):
        _collect_output_schema_compatibility_warnings(
            additional_properties,
            warnings=warnings,
            object_path=f"{object_path}.additionalProperties",
        )

    defs = node.get("$defs")
    if isinstance(defs, dict):
        for def_name, def_schema in defs.items():
            _collect_output_schema_compatibility_warnings(
                def_schema,
                warnings=warnings,
                object_path=f"{object_path}.$defs.{def_name}",
            )
