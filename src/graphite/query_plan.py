"""Canonical query plans: schema, assembly, and strict validation.

A plan is the deterministic, inference-free intermediate representation of a
structured `graphite query` invocation: which operation runs, which target
inputs it resolves, and which limits bound the traversal. Plans are built
internally from the query string (never by a model) and validated with the
zero-dependency fail-closed subset validator from routing/schema_validation.py,
so plan structure is enforced by the same machinery that guards governed
structured output. This module must not import from graphite.query — the verb
registry supplies operation names and target roles as plain data.
"""
from __future__ import annotations

from typing import Any

from .routing.schema_validation import matches_schema

PLAN_VERSION = 1

# Generous traversal bounds: large enough that real code graphs do not hit them
# in ordinary use, small enough that pathological graphs stay bounded. Reported
# via `graphite capabilities` and echoed in result `limits`/`truncated` fields.
DEFAULT_MAX_DEPTH = 32
DEFAULT_MAX_RESULTS = 200

_TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["role", "input"],
    "properties": {
        "role": {"type": "string", "enum": ["node", "source", "target"]},
        "input": {"type": "string", "minLength": 1},
    },
}

# Plan schema v1. Kept inside the subset the in-house validator supports
# (is_supported_schema-compatible: no combinators); pinned by test.
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["plan_version", "operation", "targets", "options", "source"],
    "properties": {
        "plan_version": {"const": PLAN_VERSION},
        "operation": {"type": "string", "minLength": 1},
        "targets": {"type": "array", "maxItems": 2, "items": _TARGET_SCHEMA},
        "options": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "max_depth": {"type": "integer"},
                "max_results": {"type": "integer"},
            },
        },
        "source": {
            "type": "object",
            "additionalProperties": False,
            "required": ["mode"],
            "properties": {"mode": {"const": "local-graph"}},
        },
    },
}


def make_plan(
    operation: str,
    targets: list[tuple[str, str]],
    options: dict[str, int],
) -> dict[str, Any]:
    """Assemble a plan document from (role, input) target pairs."""
    return {
        "plan_version": PLAN_VERSION,
        "operation": operation,
        "targets": [{"role": role, "input": text} for role, text in targets],
        "options": dict(options),
        "source": {"mode": "local-graph"},
    }


def plan_error(plan: object, expected_roles: dict[str, tuple[str, ...]]) -> str | None:
    """Reason `plan` is invalid, or None if it is executable.

    expected_roles maps every known operation to its required target roles in
    order; a plan whose operation is absent from the map is rejected, so a verb
    cannot be executed through a plan without being registered.
    """
    if not matches_schema(plan, PLAN_SCHEMA):
        return "plan does not match plan schema v1"
    assert isinstance(plan, dict)  # narrowed by the schema match above
    roles = expected_roles.get(plan["operation"])
    if roles is None:
        return f"unknown operation: {plan['operation']}"
    if tuple(target["role"] for target in plan["targets"]) != roles:
        return f"operation {plan['operation']} requires target roles {list(roles)}"
    for key, value in plan["options"].items():
        if value < 1:
            return f"option {key} must be a positive integer"
    return None
