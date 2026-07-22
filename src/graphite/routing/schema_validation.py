"""Minimal in-house JSON-Schema-subset validator for governed structured output.

Supports only the subset the governed output schemas use; fails closed
(``is_supported_schema`` returns False) on any keyword outside it, so an
unsupported schema is rejected at request construction rather than silently
unchecked. Zero third-party dependencies by design: the router money-path
must stay auditable and free of supply-chain surface.
"""
from __future__ import annotations

_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean", "null"})
_OBJECT_KEYS = frozenset({"type", "properties", "required", "additionalProperties"})
_ARRAY_KEYS = frozenset({"type", "items", "minItems", "maxItems"})
_SCALAR_KEYS = frozenset({"type", "enum", "const"})


def is_supported_schema(schema: object) -> bool:
    """True iff schema is a dict using only the supported subset, recursively."""
    if not isinstance(schema, dict) or not schema:
        return False
    type_value = schema.get("type")
    if type_value == "object":
        if not set(schema) <= _OBJECT_KEYS:
            return False
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return False
        if not all(isinstance(k, str) and is_supported_schema(v) for k, v in properties.items()):
            return False
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(k, str) for k in required):
            return False
        if schema.get("additionalProperties", False) is not False:
            return False
        return True
    if type_value == "array":
        if not set(schema) <= _ARRAY_KEYS:
            return False
        items = schema.get("items")
        if items is not None and not is_supported_schema(items):
            return False
        for bound_key in ("minItems", "maxItems"):
            bound = schema.get(bound_key)
            if bound is not None and (isinstance(bound, bool) or not isinstance(bound, int) or bound < 0):
                return False
        return True
    if not set(schema) <= _SCALAR_KEYS:
        return False
    if type_value is not None and type_value not in _SCALAR_TYPES:
        return False
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        return False
    if type_value is None and "const" not in schema and "enum" not in schema:
        return False
    return True


def matches_schema(value: object, schema: object) -> bool:
    """True iff value conforms to a supported schema. Fails closed on unknowns."""
    if not isinstance(schema, dict):
        return False
    type_value = schema.get("type")
    if type_value == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        if not all(key in value for key in schema.get("required", [])):
            return False
        if schema.get("additionalProperties", False) is False and not set(value) <= set(properties):
            return False
        return all(
            matches_schema(value[key], subschema)
            for key, subschema in properties.items()
            if key in value
        )
    if type_value == "array":
        if not isinstance(value, list):
            return False
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < minimum:
            return False
        if maximum is not None and len(value) > maximum:
            return False
        items = schema.get("items")
        return items is None or all(matches_schema(element, items) for element in value)
    if type_value is not None and not _matches_type(value, type_value):
        return False
    if "const" in schema and not _json_equal(value, schema["const"]):
        return False
    if "enum" in schema and not any(_json_equal(value, member) for member in schema["enum"]):
        return False
    return True


def _matches_type(value: object, type_value: str) -> bool:
    if type_value == "string":
        return isinstance(value, str)
    if type_value == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_value == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_value == "boolean":
        return isinstance(value, bool)
    if type_value == "null":
        return value is None
    return False


def _json_equal(value: object, target: object) -> bool:
    """Equality that does not conflate True==1 or 1==1.0 (JSON-kind aware)."""
    if isinstance(target, bool) or isinstance(value, bool):
        return value is target
    if isinstance(target, int) and not isinstance(value, float):
        return isinstance(value, int) and value == target
    return value == target
