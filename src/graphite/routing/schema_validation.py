"""Minimal in-house JSON-Schema validator for governed structured output.

The governed output schemas are sha256-pinned constants (not attacker input), so this
validator's job is to enforce the constraints those schemas express, not to police keyword
novelty. It therefore fails closed ONLY on combinator keywords (anyOf/oneOf/allOf/not/$ref/
patternProperties/...) -- keywords that change what "valid" means and that this validator
cannot safely approximate. All other (refinement/annotation) keywords are tolerated:
matches_schema enforces the ones it understands and ignores the rest, which can only
under-enforce a single constraint, never accept a structurally wrong shape. Zero third-party
dependencies by design.
"""
from __future__ import annotations

import re

# Keywords that combine/redirect subschemas and thus change what "valid" means. This
# validator cannot approximate them, so a schema using any of them fails closed at
# is_supported_schema -> the executor rejects it with request_invalid before any call,
# rather than being silently under-validated.
_COMBINATORS = frozenset({
    "anyOf", "oneOf", "allOf", "not", "$ref", "$dynamicRef",
    "if", "then", "else", "patternProperties", "propertyNames",
    "dependencies", "dependentSchemas", "dependentRequired",
    "unevaluatedProperties", "unevaluatedItems", "contains",
    "prefixItems", "additionalItems",
})


def is_supported_schema(schema: object) -> bool:
    """True unless the schema uses a combinator keyword this validator cannot safely
    approximate (or a non-bool additionalProperties, which is a schema-valued shape
    change). Refinement/annotation keywords are tolerated. Governed schemas are pinned
    constants, so this gate catches a shape we would mis-validate, not unknown-but-harmless
    keys."""
    if not isinstance(schema, dict) or not schema:
        return False
    if not _COMBINATORS.isdisjoint(schema):
        return False
    if not isinstance(schema.get("additionalProperties", False), bool):
        return False
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        try:
            re.compile(pattern)
        except re.error:
            return False
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            return False
        if not all(isinstance(k, str) and is_supported_schema(v) for k, v in properties.items()):
            return False
    items = schema.get("items")
    if items is not None and not is_supported_schema(items):
        return False
    return True


def matches_schema(value: object, schema: object) -> bool:
    """True iff value conforms to every constraint in schema that this validator
    understands; unknown refinement keywords are ignored (never over-reject).

    Precondition: schema must already have passed is_supported_schema -- callers gate the
    schema once, then match many values against it. Given such a schema this never raises.
    Untrusted VALUES are always handled safely: a non-conforming (or wrong-typed) value
    returns False, never raises.
    """
    if not isinstance(schema, dict):
        return False
    type_value = schema.get("type")
    if type_value is not None and not _matches_type(value, type_value):
        return False
    if "const" in schema and not _json_equal(value, schema["const"]):
        return False
    if "enum" in schema and not any(_json_equal(value, member) for member in schema["enum"]):
        return False
    if isinstance(value, str) and not _matches_string(value, schema):
        return False
    if isinstance(value, dict) and _is_object_schema(schema) and not _matches_object(value, schema):
        return False
    if isinstance(value, list) and _is_array_schema(schema) and not _matches_array(value, schema):
        return False
    return True


def _is_object_schema(schema: dict) -> bool:
    return schema.get("type") == "object" or any(
        key in schema for key in ("properties", "required", "additionalProperties")
    )


def _is_array_schema(schema: dict) -> bool:
    return schema.get("type") == "array" or any(
        key in schema for key in ("items", "minItems", "maxItems")
    )


def _matches_string(value: str, schema: dict) -> bool:
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
        return False
    maximum = schema.get("maxLength")
    if _is_bound(maximum) and len(value) > maximum:
        return False
    minimum = schema.get("minLength")
    if _is_bound(minimum) and len(value) < minimum:
        return False
    return True


def _matches_object(value: dict, schema: dict) -> bool:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return False
    if not all(key in value for key in schema.get("required", [])):
        return False
    if schema.get("additionalProperties", False) is False and not set(value) <= set(properties):
        return False
    return all(
        matches_schema(value[key], subschema)
        for key, subschema in properties.items()
        if key in value
    )


def _matches_array(value: list, schema: dict) -> bool:
    minimum = schema.get("minItems")
    if _is_bound(minimum) and len(value) < minimum:
        return False
    maximum = schema.get("maxItems")
    if _is_bound(maximum) and len(value) > maximum:
        return False
    items = schema.get("items")
    if isinstance(items, dict):
        return all(matches_schema(element, items) for element in value)
    return True


def _is_bound(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _matches_type(value: object, type_value: object) -> bool:
    if isinstance(type_value, list):
        return any(_matches_type(value, member) for member in type_value)
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
    if type_value == "object":
        return isinstance(value, dict)
    if type_value == "array":
        return isinstance(value, list)
    return False


def _json_equal(value: object, target: object) -> bool:
    """Equality that does not conflate True==1, 1==1.0, or 1.0==1 (JSON-kind aware)."""
    if isinstance(value, bool) or isinstance(target, bool):
        return value is target
    if isinstance(value, (int, float)) and isinstance(target, (int, float)):
        return type(value) is type(target) and value == target
    return value == target
