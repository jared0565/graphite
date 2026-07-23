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

def test_const_and_enum_distinguish_int_from_float():
    assert matches_schema(1, {"type": "number", "const": 1}) is True
    assert matches_schema(1.0, {"type": "number", "const": 1}) is False
    assert matches_schema(1, {"type": "number", "const": 1.0}) is False
    assert matches_schema(1, {"type": "integer", "enum": [1, 2]}) is True
    assert matches_schema(1.0, {"type": "number", "enum": [1, 2]}) is False
    assert matches_schema(True, {"type": "integer", "const": 1}) is False


# Real governed schema constants (verbatim from the live-acceptance harness) that flow into
# execute_openrouter as output_schema. The validator MUST accept every one of these.
EDIT_SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "files": {
            "items": {
                "additionalProperties": False,
                "properties": {
                    "content": {"maxLength": 1048576, "type": "string"},
                    "path": {"enum": ["src/access.py", "tests/test_access.py"], "type": "string"},
                },
                "required": ["path", "content"],
                "type": "object",
            },
            "maxItems": 2,
            "minItems": 2,
            "type": "array",
        },
        "result": {"const": "GRAPHITE_EDIT_OK", "type": "string"},
    },
    "required": ["result", "files"],
    "type": "object",
}
REVIEW_SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "findings": {
            "items": {
                "additionalProperties": False,
                "properties": {
                    "category": {"enum": ["correctness", "security", "reliability",
                                          "maintainability", "test_coverage"], "type": "string"},
                    "severity": {"enum": ["low", "medium", "high", "critical"], "type": "string"},
                    "summary_sha256": {"pattern": "^[0-9a-f]{64}$", "type": "string"},
                },
                "required": ["severity", "category", "summary_sha256"],
                "type": "object",
            },
            "maxItems": 16,
            "type": "array",
        },
        "verdict": {"enum": ["pass", "fail"], "type": "string"},
    },
    "required": ["verdict", "findings"],
    "type": "object",
}
VERIFY_SCHEMA = {
    "additionalProperties": False,
    "properties": {"verification": {"const": "GRAPHITE_PROFILE_OK", "type": "string"}},
    "required": ["verification"],
    "type": "object",
}


def test_accepts_every_governed_schema():
    for schema in (EDIT_SCHEMA, REVIEW_SCHEMA, VERIFY_SCHEMA):
        assert is_supported_schema(schema) is True


def test_fails_closed_on_combinators_and_subschema_additionalproperties():
    assert is_supported_schema({"anyOf": [{"type": "string"}]}) is False
    assert is_supported_schema({"type": "object", "oneOf": [{"type": "object"}]}) is False
    assert is_supported_schema({"allOf": [{"type": "string"}]}) is False
    assert is_supported_schema({"not": {"type": "string"}}) is False
    assert is_supported_schema({"$ref": "#/$defs/x"}) is False
    assert is_supported_schema({"type": "object", "patternProperties": {"^x": {"type": "string"}}}) is False
    # additionalProperties as a subschema (not a bool) is a shape change -> fail closed
    assert is_supported_schema({"type": "object", "additionalProperties": {"type": "string"}}) is False
    # a combinator nested deep is still caught by recursion
    assert is_supported_schema({"type": "object", "properties": {"x": {"anyOf": [{"type": "string"}]}}}) is False
    assert is_supported_schema({}) is False
    assert is_supported_schema("nope") is False


def test_tolerates_refinement_keywords():
    assert is_supported_schema({"type": "string", "pattern": "^x$"}) is True
    assert is_supported_schema({"type": "string", "maxLength": 10}) is True
    assert is_supported_schema({"type": "array", "items": {"type": "string"}, "maxItems": 2}) is True
    assert is_supported_schema({"type": "object", "properties": {}, "additionalProperties": True}) is True
    assert is_supported_schema({"type": "string", "format": "email", "title": "X"}) is True  # unknown but harmless


def test_enforces_pattern_and_maxlength():
    hexs = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    assert matches_schema("a" * 64, hexs) is True
    assert matches_schema("A" * 64, hexs) is False   # uppercase not [0-9a-f]
    assert matches_schema("a" * 63, hexs) is False   # wrong length via anchored pattern
    ml = {"type": "string", "maxLength": 4}
    assert matches_schema("abcd", ml) is True
    assert matches_schema("abcde", ml) is False


def test_governed_review_response_conforms_and_rejects():
    assert matches_schema({"verdict": "pass", "findings": []}, REVIEW_SCHEMA) is True
    good = {"category": "security", "severity": "high", "summary_sha256": "a" * 64}
    assert matches_schema({"verdict": "fail", "findings": [good]}, REVIEW_SCHEMA) is True
    bad_hash = {"category": "security", "severity": "high", "summary_sha256": "NOTHEX"}
    assert matches_schema({"verdict": "fail", "findings": [bad_hash]}, REVIEW_SCHEMA) is False  # pattern
    assert matches_schema({"verdict": "maybe", "findings": []}, REVIEW_SCHEMA) is False          # enum


def test_governed_edit_response_conforms_and_rejects():
    ok = {"result": "GRAPHITE_EDIT_OK",
          "files": [{"path": "src/access.py", "content": "x"},
                    {"path": "tests/test_access.py", "content": "y"}]}
    assert matches_schema(ok, EDIT_SCHEMA) is True
    one_file = {"result": "GRAPHITE_EDIT_OK", "files": [{"path": "src/access.py", "content": "x"}]}
    assert matches_schema(one_file, EDIT_SCHEMA) is False   # minItems 2
    bad_path = {"result": "GRAPHITE_EDIT_OK",
                "files": [{"path": "evil.py", "content": "x"}, {"path": "tests/test_access.py", "content": "y"}]}
    assert matches_schema(bad_path, EDIT_SCHEMA) is False   # path enum


def test_fails_closed_on_uncompilable_pattern():
    # A malformed regex must be caught at the gate (before any paid call), not raise
    # re.error inside matches_schema at response time. The "never raises" guarantee on
    # matches_schema depends on this.
    assert is_supported_schema({"type": "string", "pattern": "["}) is False
    assert is_supported_schema({"type": "string", "pattern": "(unclosed"}) is False
    # caught recursively when nested inside properties / items
    assert is_supported_schema(
        {"type": "object", "properties": {"x": {"type": "string", "pattern": "["}}}) is False
    assert is_supported_schema(
        {"type": "array", "items": {"type": "string", "pattern": "(?P<n>"}}) is False
    # a valid pattern is still supported (guards against over-rejection)
    assert is_supported_schema({"type": "string", "pattern": "^[0-9a-f]{64}$"}) is True
