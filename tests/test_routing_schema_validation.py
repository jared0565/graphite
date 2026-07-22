from graphite.routing.schema_validation import is_supported_schema, matches_schema

_EDIT = {
    "additionalProperties": False,
    "properties": {"result": {"const": "GRAPHITE_EDIT_OK", "type": "string"}},
    "required": ["result"],
    "type": "object",
}
_REVIEW = {
    "additionalProperties": False,
    "type": "object",
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "findings": {"type": "array", "items": {"type": "string"}},
    },
}

def test_supports_governed_edit_and_review_schemas():
    assert is_supported_schema(_EDIT) is True
    assert is_supported_schema(_REVIEW) is True

def test_rejects_unsupported_keywords_and_shapes():
    assert is_supported_schema({"type": "object", "properties": {}, "required": [],
                                "additionalProperties": False, "minProperties": 1}) is False
    assert is_supported_schema({"type": "object", "additionalProperties": True}) is False
    assert is_supported_schema({"anyOf": [{"type": "string"}]}) is False
    assert is_supported_schema({"type": "string", "pattern": "x"}) is False
    assert is_supported_schema({}) is False
    assert is_supported_schema("nope") is False

def test_matches_conforming_and_rejects_nonconforming():
    assert matches_schema({"result": "GRAPHITE_EDIT_OK"}, _EDIT) is True
    assert matches_schema({"result": "WRONG"}, _EDIT) is False           # const mismatch
    assert matches_schema({}, _EDIT) is False                            # missing required
    assert matches_schema({"result": "GRAPHITE_EDIT_OK", "x": 1}, _EDIT) is False  # extra key
    assert matches_schema({"verdict": "pass", "findings": []}, _REVIEW) is True
    assert matches_schema({"verdict": "maybe", "findings": []}, _REVIEW) is False   # enum miss
    assert matches_schema({"verdict": "pass", "findings": [1]}, _REVIEW) is False   # item type

def test_type_discrimination_bool_int_float():
    assert matches_schema(True, {"type": "integer"}) is False
    assert matches_schema(1, {"type": "boolean"}) is False
    assert matches_schema(1, {"type": "number"}) is True
    assert matches_schema(1.5, {"type": "integer"}) is False
    assert matches_schema(None, {"type": "null"}) is True

def test_array_bounds():
    schema = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2}
    assert matches_schema(["a"], schema) is True
    assert matches_schema([], schema) is False
    assert matches_schema(["a", "b", "c"], schema) is False
