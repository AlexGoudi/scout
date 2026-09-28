"""Validate a document against a versioned JSON schema, using only the standard library.

Scout's one dependency is PyYAML and it is spent on the pipeline parser, so `jsonschema`
is not available and the subset below is implemented here instead. It is a subset, not an
approximation: the keywords it does not implement it **rejects**, so a schema that grows a
construct this validator cannot check fails loudly rather than passing vacuously. A
validator that silently ignores what it does not understand is worse than none, because it
converts an unvalidated field into a validated-looking one.

Errors accumulate rather than short-circuit, and each carries a JSON pointer, because the
brief is the contract between two stages and "invalid" without a location is not actionable
(FR-17).
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"
BRIEF_SCHEMA_VERSION = "1.0"

_SUPPORTED = frozenset({
    "$schema", "$id", "$defs", "$ref", "title", "description",
    "type", "enum", "const", "properties", "required", "additionalProperties",
    "items", "minItems", "minLength", "minimum", "maximum", "pattern",
})

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
    "null": type(None),
}


class SchemaError(RuntimeError):
    """The schema itself is unusable — missing, unparseable, or using an unsupported keyword."""


@dataclass(frozen=True)
class ValidationError(ValueError):
    """A document did not satisfy its schema, with every failure and where each was."""

    schema_id: str
    problems: Tuple[str, ...]

    def __str__(self) -> str:
        listed = "\n  ".join(self.problems)
        return f"{len(self.problems)} problem(s) against {self.schema_id}:\n  {listed}"


def load_schema(name: str, directory: Optional[Path] = None) -> Dict[str, Any]:
    path = Path(directory or SCHEMA_DIR) / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SchemaError(f"Cannot read schema {path}: {error}") from error


def brief_schema(directory: Optional[Path] = None) -> Dict[str, Any]:
    return load_schema(f"scout-brief-{BRIEF_SCHEMA_VERSION}.json", directory)


def validate(document: Any, schema: Dict[str, Any]) -> None:
    """Raise `ValidationError` listing every way `document` fails `schema`."""
    problems: List[str] = []
    _check(document, schema, schema, "$", problems)
    if problems:
        raise ValidationError(schema_id=str(schema.get("$id") or schema.get("title") or "schema"),
                              problems=tuple(problems))


def validate_brief(document: Any, directory: Optional[Path] = None) -> None:
    validate(document, brief_schema(directory))


def _check(value: Any, schema: Dict[str, Any], root: Dict[str, Any], pointer: str, problems: List[str]) -> None:
    unsupported = sorted(set(schema) - _SUPPORTED)
    if unsupported:
        raise SchemaError(
            f"{pointer}: schema uses keyword(s) {unsupported} this validator does not implement. Implement "
            f"them or drop them; silently ignoring a constraint makes an unchecked field look checked."
        )

    if "$ref" in schema:
        _check(value, _resolve(schema["$ref"], root), root, pointer, problems)
        return

    if "const" in schema and value != schema["const"]:
        problems.append(f"{pointer}: expected {schema['const']!r}, got {value!r}")
        return

    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{pointer}: {value!r} is not one of {schema['enum']}")
        return

    declared = schema.get("type")
    if declared and not _is_type(value, declared):
        problems.append(f"{pointer}: expected type {declared}, got {type(value).__name__}")
        return

    if isinstance(value, str):
        _check_string(value, schema, pointer, problems)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        _check_number(value, schema, pointer, problems)
    if isinstance(value, list):
        _check_array(value, schema, root, pointer, problems)
    if isinstance(value, dict):
        _check_object(value, schema, root, pointer, problems)


def _check_string(value: str, schema: Dict[str, Any], pointer: str, problems: List[str]) -> None:
    if "minLength" in schema and len(value) < schema["minLength"]:
        problems.append(f"{pointer}: shorter than {schema['minLength']} character(s)")
    pattern = schema.get("pattern")
    if pattern and not re.search(pattern, value):
        problems.append(f"{pointer}: {value!r} does not match {pattern}")


def _check_number(value: float, schema: Dict[str, Any], pointer: str, problems: List[str]) -> None:
    if "minimum" in schema and value < schema["minimum"]:
        problems.append(f"{pointer}: {value} is below the minimum {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        problems.append(f"{pointer}: {value} is above the maximum {schema['maximum']}")


def _check_array(value: List[Any], schema: Dict[str, Any], root: Dict[str, Any],
                 pointer: str, problems: List[str]) -> None:
    if "minItems" in schema and len(value) < schema["minItems"]:
        problems.append(f"{pointer}: has {len(value)} item(s), needs at least {schema['minItems']}")
    item_schema = schema.get("items")
    if isinstance(item_schema, dict):
        for position, item in enumerate(value):
            _check(item, item_schema, root, f"{pointer}[{position}]", problems)


def _check_object(value: Dict[str, Any], schema: Dict[str, Any], root: Dict[str, Any],
                  pointer: str, problems: List[str]) -> None:
    properties = schema.get("properties") or {}
    for name in schema.get("required") or []:
        if name not in value:
            problems.append(f"{pointer}: missing required property {name!r}")

    if schema.get("additionalProperties") is False:
        for name in sorted(set(value) - set(properties)):
            problems.append(f"{pointer}: property {name!r} is not allowed here")

    for name, item in value.items():
        if name in properties:
            _check(item, properties[name], root, f"{pointer}.{name}", problems)


def _resolve(reference: str, root: Dict[str, Any]) -> Dict[str, Any]:
    if not reference.startswith("#/"):
        raise SchemaError(f"Only local references are supported, got {reference!r}")
    node: Any = root
    for part in reference[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            raise SchemaError(f"Reference {reference!r} does not resolve in this schema")
        node = node[part]
    return node


def _is_type(value: Any, declared: Any) -> bool:
    names = declared if isinstance(declared, list) else [declared]
    for name in names:
        expected = _TYPES.get(name)
        if expected is None:
            raise SchemaError(f"Unknown schema type {name!r}")
        if name in ("integer", "number") and isinstance(value, bool):
            continue
        if name == "integer" and not isinstance(value, int):
            continue
        if isinstance(value, expected):
            return True
    return False
