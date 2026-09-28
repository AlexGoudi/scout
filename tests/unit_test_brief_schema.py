"""The stdlib JSON-schema validator, and what it refuses to do.

The interesting property is the last one. A validator that ignores a keyword it does not
implement turns an unchecked field into a validated-looking one, which is worse than no
validator at all — so this one raises `SchemaError` on an unsupported keyword and the test
pins that behaviour down.
"""

import pytest

from scout_impl.static.schema import SchemaError, ValidationError, brief_schema, validate

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "count"],
    "properties": {
        "name": {"type": "string", "minLength": 1, "pattern": "^[a-z]+$"},
        "count": {"type": "integer", "minimum": 0, "maximum": 10},
        "kind": {"enum": ["a", "b"]},
        "version": {"const": "1.0"},
        "items": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/item"}},
    },
    "$defs": {
        "item": {
            "type": "object",
            "required": ["id"],
            "additionalProperties": False,
            "properties": {"id": {"type": "string"}},
        }
    },
}


def test_a_valid_document_passes():
    validate({"name": "scout", "count": 3, "kind": "a", "items": [{"id": "x"}]}, SCHEMA)


def test_a_missing_required_property_is_reported_with_its_location():
    with pytest.raises(ValidationError) as raised:
        validate({"name": "scout"}, SCHEMA)
    assert "$: missing required property 'count'" in str(raised.value)


def test_an_unexpected_property_is_reported():
    with pytest.raises(ValidationError) as raised:
        validate({"name": "scout", "count": 1, "extra": True}, SCHEMA)
    assert "'extra' is not allowed" in str(raised.value)


def test_every_problem_is_reported_not_only_the_first():
    """Including both failures on one field: `""` is too short and does not match."""
    with pytest.raises(ValidationError) as raised:
        validate({"name": "", "count": 99, "kind": "z"}, SCHEMA)
    assert len(raised.value.problems) == 4
    assert sum(1 for problem in raised.value.problems if problem.startswith("$.name")) == 2


def test_type_enum_pattern_const_and_bounds_are_all_enforced():
    for document, expected in (
        ({"name": 7, "count": 1}, "expected type string"),
        ({"name": "Scout", "count": 1}, "does not match"),
        ({"name": "scout", "count": "3"}, "expected type integer"),
        ({"name": "scout", "count": -1}, "below the minimum"),
        ({"name": "scout", "count": 11}, "above the maximum"),
        ({"name": "scout", "count": 1, "kind": "z"}, "is not one of"),
        ({"name": "scout", "count": 1, "version": "2.0"}, "expected '1.0'"),
        ({"name": "scout", "count": 1, "items": []}, "needs at least 1"),
    ):
        with pytest.raises(ValidationError) as raised:
            validate(document, SCHEMA)
        assert expected in str(raised.value)


def test_a_boolean_is_not_an_integer():
    with pytest.raises(ValidationError):
        validate({"name": "scout", "count": True}, SCHEMA)


def test_a_local_reference_is_resolved_and_its_failures_are_located():
    with pytest.raises(ValidationError) as raised:
        validate({"name": "scout", "count": 1, "items": [{"wrong": "x"}]}, SCHEMA)
    assert "$.items[0]: missing required property 'id'" in str(raised.value)


def test_a_keyword_the_validator_does_not_implement_is_rejected_not_ignored():
    """Silently ignoring `oneOf` would make a field look checked when it is not."""
    with pytest.raises(SchemaError) as raised:
        validate({"a": 1}, {"type": "object", "properties": {"a": {"oneOf": [{"type": "integer"}]}}})
    assert "does not implement" in str(raised.value)


def test_the_brief_schema_loads_and_only_uses_keywords_the_validator_supports():
    """Loading and then validating an empty document walks the whole schema once."""
    with pytest.raises(ValidationError):
        validate({}, brief_schema())
