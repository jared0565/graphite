"""Tests for `graphite.doctor_probes`, the deep-readiness probe helpers.

Named after the module so aramid's mutation stage 1 (`tests/test_<module>.py`)
has something to run; `-k doctor_probes` selected nothing before this file,
and the probes' existing coverage (`test_doctor.py`, 121 s) reaches them
only through subprocesses. This file pins the pure helpers -- failure
reports, stream excerpts, path checks, the PEP 508 marker evaluator, the
MCP transcript parser and the LLM worker payload -- with plain data.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from graphite import doctor_probes
from graphite.config import Config
from graphite.doctor_probes import (
    _MCP_LINE_LIMIT,
    _MCP_NESTING_LIMIT,
    _MCP_OUTPUT_LIMIT_BYTES,
    _blocked,
    _bounded_llm_text,
    _check_deadline,
    _condition_holds,
    _count,
    _degraded_probe,
    _enclosed_by_parentheses,
    _json_object,
    _json_object_without_duplicate_keys,
    _llm_failure,
    _llm_worker_input,
    _marker_values_compare,
    _mcp_transcript_complete,
    _normalized_distribution_name,
    _parse_mcp_responses,
    _path_is_within,
    _paths_overlap,
    _process_error_type,
    _requirement_applies,
    _requirement_extras,
    _stream_excerpt,
    _validate_json_nesting,
    _version_tuple,
)
from graphite.llm_probe import SYSTEM_PROMPT, USER_PROMPT
from graphite.probe_process import ProbeProcessError

_PY = f"{sys.version_info.major}.{sys.version_info.minor}"


def _rpc(id_: int | None = None, **fields: object) -> bytes:
    envelope: dict[str, object] = {"jsonrpc": "2.0"}
    if id_ is not None:
        envelope["id"] = id_
    envelope.update(fields)
    return json.dumps(envelope).encode("utf-8")


def _transcript() -> bytes:
    return b"\n".join([_rpc(1, result={"protocolVersion": "x"}), _rpc(2, result={"tools": []})]) + b"\n"


# --- reports --------------------------------------------------------------------------


def test_blocked_reports_the_code_and_only_carries_a_masked_cause_by_name() -> None:
    check = _blocked("timeout", "cleanup_timeout")
    assert (check.code, check.status) == ("deep_core", "blocked")
    assert dict(check.details) == {"error_type": "timeout", "code": "cleanup_timeout"}

    masked = {"error_type": "process", "code": "nonzero", "path": "C:/secret"}
    check = _blocked("timeout", "cleanup_timeout", masked=masked)
    assert dict(check.details) == {
        "error_type": "timeout",
        "code": "cleanup_timeout",
        "masked_error_type": "process",
        "masked_code": "nonzero",
    }


def test_blocked_leaves_the_masked_keys_absent_when_the_cause_is_malformed() -> None:
    check = _blocked("process", "nonzero", masked={"error_type": None, "code": "x"})
    assert "masked_error_type" not in check.details and "masked_code" not in check.details


@pytest.mark.parametrize(
    ("code", "error_type"),
    [("timeout", "timeout"), ("output_limit", "output_limit"), ("nonzero", "process"), ("anything", "process")],
)
def test_process_error_type_keeps_only_the_two_named_codes(code: str, error_type: str) -> None:
    assert _process_error_type(code) == error_type


def test_degraded_probe_names_the_failure_in_summary_and_details() -> None:
    check = _degraded_probe("deep_typescript", "TypeScript", "timed out")
    assert (check.code, check.label, check.status) == ("deep_typescript", "TypeScript", "degraded")
    assert check.summary == "The TypeScript deep probe timed out."
    assert dict(check.details) == {"code": "timed out"}


def test_llm_failure_folds_unknown_categories_into_provider_error() -> None:
    assert dict(_llm_failure("timeout").details) == {"category": "timeout"}
    assert dict(_llm_failure("path leaked").details) == {"category": "provider_error"}
    assert _llm_failure().status == "degraded"


# --- small helpers -----------------------------------------------------------------------


def test_json_object_accepts_only_a_utf8_json_object() -> None:
    assert _json_object(b'{"a": 1}') == {"a": 1}
    for payload in (b"[1]", b"{bad", b"\xff"):
        with pytest.raises(ProbeProcessError) as info:
            _json_object(payload)
        assert info.value.code == "unexpected"


def test_count_accepts_ints_within_the_requested_floor() -> None:
    assert _count(0) == 0
    assert _count(0, positive=True) is None
    assert _count(3, positive=True) == 3
    assert _count(-1) is None
    assert _count(True) is None
    assert _count(1.0) is None


def test_check_deadline_raises_timeout_at_the_limit() -> None:
    _check_deadline(10.0, lambda: 9.99)
    with pytest.raises(ProbeProcessError) as info:
        _check_deadline(10.0, lambda: 10.0)
    assert info.value.code == "timeout"


def test_stream_excerpt_keeps_the_head_by_default_and_the_tail_on_request() -> None:
    assert _stream_excerpt(b"short") == "short"
    assert _stream_excerpt(None) == ""
    text = "".join(str(i % 10) for i in range(20))
    assert _stream_excerpt(text, limit=5) == "01234...[15 more chars]"
    assert _stream_excerpt(text, limit=5, tail=True) == "[15 more chars]...56789"
    assert _stream_excerpt(b"\xff" * 3, limit=10) == "\ufffd" * 3


def test_paths_overlap_in_either_direction_only() -> None:
    root = Path("/repo").resolve()
    assert _paths_overlap(root, root / "sub")
    assert _paths_overlap(root / "sub", root)
    assert not _paths_overlap(root / "a", root / "b")
    assert _path_is_within(root / "a", root)
    assert not _path_is_within(root, root / "a")


# --- PEP 508 markers -------------------------------------------------------------------------


def test_normalized_distribution_name_folds_case_and_separators() -> None:
    assert _normalized_distribution_name("Py_JWT.Crypto") == "py-jwt-crypto"
    assert _normalized_distribution_name("a--b__c") == "a-b-c"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("3.14.0", (3, 14, 0)), ("3.14.0rc1", (3, 14, 0)), ("3.14rc1", (3, 14)), ("3", (3,))],
)
def test_version_tuple_keeps_the_leading_numeric_components(value: str, expected: tuple[int, ...]) -> None:
    assert _version_tuple(value) == expected


def test_version_tuple_rejects_a_value_with_no_numeric_lead() -> None:
    with pytest.raises(ValueError):
        _version_tuple("rc1")


def test_marker_values_compare_supports_all_six_operators_on_both_domains() -> None:
    assert _marker_values_compare("<", (3, 11), (3, 14))
    assert _marker_values_compare(">=", (3, 14), (3, 14))
    assert not _marker_values_compare("!=", "win32", "win32")
    assert _marker_values_compare(">", "linux", "darwin")
    assert _marker_values_compare("<=", "a", "a")
    assert _marker_values_compare("==", (1,), (1,))
    with pytest.raises(KeyError):
        _marker_values_compare("~=", (1,), (1,))


def test_condition_holds_evaluates_live_python_version_markers() -> None:
    assert _condition_holds(f'python_version == "{_PY}"', frozenset())
    assert _condition_holds(f'python_version >= "{_PY}"', frozenset())
    assert not _condition_holds('python_version < "3.0"', frozenset())
    assert _condition_holds(f'(python_version == "{_PY}.*")', frozenset())
    assert _condition_holds(f'sys_platform == "{sys.platform}"', frozenset())


@pytest.mark.parametrize(
    "condition",
    [
        'python_version ~= "3.0"',
        'python_version > "3.*"',  # prefix match only defines == and !=
        'os_name == "nt"',  # not a supported marker
        '(python_version == "3.0" and sys_platform == "x")',  # nested boolean
        'extra',
        'extra in "x"',
    ],
)
def test_condition_holds_fails_closed_on_what_it_cannot_evaluate(condition: str) -> None:
    with pytest.raises(ValueError):
        _condition_holds(condition, frozenset())


def test_requirement_extras_reads_the_bracket_before_any_marker() -> None:
    assert _requirement_extras("pyjwt[crypto]>=2; extra == 'other'") == frozenset({"crypto"})
    assert _requirement_extras("pkg[A_B, c.d,]") == frozenset({"a-b", "c-d"})
    assert _requirement_extras("plain>=1") == frozenset()
    with pytest.raises(ValueError):
        _requirement_extras("pkg[bad extra]")


def test_requirement_applies_honours_extras_and_precedence() -> None:
    assert _requirement_applies("anyio>=4")
    assert not _requirement_applies('cryptography; extra == "crypto"')
    assert _requirement_applies('cryptography; extra == "crypto"', frozenset({"crypto"}))
    assert _requirement_applies('cryptography; extra != "crypto"')
    assert _requirement_applies(f'x; extra == "a" or python_version == "{_PY}"')
    assert not _requirement_applies(f'x; extra == "a" and python_version == "{_PY}"')
    assert _requirement_applies(f'x; (extra == "a") or (python_version == "{_PY}")', frozenset({"a"}))


def test_requirement_applies_unwraps_only_a_matched_enclosing_pair_of_parentheses() -> None:
    # `(A) or (B)` starts with "(" and ends with ")" but is not enclosed by
    # them. Stripping both blindly left `A) or (B` -- an extra marker of that
    # shape failed closed and a plain one aborted the whole distribution walk.
    assert _requirement_applies(f'x; (extra == "a") or (python_version == "{_PY}")', frozenset())
    assert _requirement_applies(f'x; (python_version == "{_PY}") or (sys_platform == "never")')
    assert not _requirement_applies('x; (python_version == "0.0") or (sys_platform == "never")')
    assert _requirement_applies(f'x; ((python_version == "{_PY}"))')
    # A parenthesis inside a quoted value is data, not structure.
    assert _condition_holds('(sys_platform == "never)")', frozenset()) is False
    assert _requirement_applies('x; (sys_platform == "never)") or (sys_platform == "(never")') is False


def test_enclosed_by_parentheses_requires_both_ends_and_a_matching_first_pair() -> None:
    # Both ends are required TOGETHER. The 2026-09-06 drain's one survivor
    # turned the guard's `and` into `or`: `x(y)` then passed the guard and
    # counted as enclosed because its first "(" closes on the last character,
    # so `_requirement_applies` stripped the first and last characters of a
    # marker like `A or (B)` and raised on the mangled remainder. Only the
    # `x(y)` shape and the caller-level line below separate the two guards;
    # `(x)y` and the plain cases read the same under either.
    assert _enclosed_by_parentheses("(x)") is True
    assert _enclosed_by_parentheses("x(y)") is False
    assert _enclosed_by_parentheses("(x)y") is False
    assert _enclosed_by_parentheses("(a) or (b)") is False
    assert _enclosed_by_parentheses("x") is False
    assert _enclosed_by_parentheses("") is False
    assert _requirement_applies(f'x; python_version == "0.0" or (python_version == "{_PY}")')


def test_requirement_applies_rejects_an_unevaluable_extra_marker_but_raises_otherwise() -> None:
    assert _requirement_applies('x; extra in "a"') is False
    with pytest.raises(ValueError):
        _requirement_applies('x; os_name == "nt"')


# --- MCP transcript ---------------------------------------------------------------------------


def test_validate_json_nesting_ignores_brackets_inside_strings_and_bounds_depth() -> None:
    _validate_json_nesting('{"a": "[{\\"]", "b": [1, {"c": 2}]}')
    _validate_json_nesting("[" * _MCP_NESTING_LIMIT + "]" * _MCP_NESTING_LIMIT)
    for bad in ('{"a": [}', '{"a": 1', '"open', "[" * (_MCP_NESTING_LIMIT + 1) + "]" * (_MCP_NESTING_LIMIT + 1)):
        with pytest.raises(ValueError):
            _validate_json_nesting(bad)


def test_json_object_without_duplicate_keys_rejects_a_repeat() -> None:
    assert _json_object_without_duplicate_keys([("a", 1), ("b", 2)]) == {"a": 1, "b": 2}
    with pytest.raises(ValueError):
        _json_object_without_duplicate_keys([("a", 1), ("a", 2)])


def test_parse_mcp_responses_returns_both_replies_and_skips_notifications() -> None:
    output = _rpc(method="notifications/initialized") + b"\n" + _transcript()
    responses = _parse_mcp_responses(output, b"log line")
    assert set(responses) == {1, 2}
    assert responses[2]["result"] == {"tools": []}


@pytest.mark.parametrize(
    ("output", "stderr"),
    [
        (b"", b""),
        (_rpc(1, result={}), b""),
        (_rpc(1, result={}) + b"\n" + _rpc(1, result={}), b""),
        (_rpc(1, result={}) + b"\n" + _rpc(3, result={}), b""),
        (_rpc(1, result={}) + b"\n" + _rpc(2, error={"code": 1}), b""),
        (_rpc(1, result={}) + b"\n" + _rpc(2, result={}, extra=1), b""),
        (_rpc(1, result={}) + b"\n" + _rpc(True, result={}), b""),
        (_rpc(1, result={}) + b"\n" + b'{"jsonrpc": "1.0", "id": 2, "result": {}}', b""),
        (_rpc(1, result={}) + b"\n" + _rpc(method="") , b""),
        (_rpc(1, result={}) + b"\n" + _rpc(method="m", unexpected=1), b""),
        (_rpc(1, result={}) + b"\n" + b'{"jsonrpc": "2.0", "id": 2, "result": NaN}', b""),
        (_rpc(1, result={}) + b"\n" + b'{"jsonrpc": "2.0", "id": 2, "result": {}, "result": {}}', b""),
        (b"\n".join([_rpc(method="m")] * (_MCP_LINE_LIMIT + 1)), b""),
        (_transcript(), b"x" * _MCP_OUTPUT_LIMIT_BYTES),
    ],
    ids=[
        "empty",
        "one-reply",
        "duplicate-id",
        "unexpected-id",
        "error-reply",
        "extra-key",
        "bool-id",
        "wrong-version",
        "empty-method",
        "notification-extra-key",
        "nan",
        "duplicate-json-key",
        "too-many-lines",
        "over-byte-limit",
    ],
)
def test_parse_mcp_responses_rejects_anything_but_the_expected_two_replies(output: bytes, stderr: bytes) -> None:
    with pytest.raises(ValueError):
        _parse_mcp_responses(output, stderr)


def test_transcript_complete_only_once_both_replies_are_present() -> None:
    assert _mcp_transcript_complete(b"") is False
    assert _mcp_transcript_complete(_rpc(1, result={}) + b"\n") is False
    assert _mcp_transcript_complete(_transcript()[:-3]) is False  # half-written last line
    assert _mcp_transcript_complete(_transcript()) is True


# --- LLM worker input ---------------------------------------------------------------------------


def test_bounded_llm_text_passes_none_and_short_strings_only() -> None:
    assert _bounded_llm_text(None, limit=3) is None
    assert _bounded_llm_text("abc", limit=3) == "abc"
    with pytest.raises(ValueError):
        _bounded_llm_text("abcd", limit=3)
    with pytest.raises(ValueError):
        _bounded_llm_text(5, limit=3)


def test_llm_worker_input_is_the_compact_payload_the_worker_validates() -> None:
    cfg = Config(llm_mode="Local", llm_provider="ollama", llm_model="m", llm_api_key="k", seed=9)
    payload = json.loads(_llm_worker_input(cfg, 12.5).decode("utf-8"))
    assert payload == {
        "mode": "Local",
        "provider": "ollama",
        "model": "m",
        "base_url": None,
        "api_key": "k",
        "timeout_seconds": 12.5,
        "seed": 9,
        "system": SYSTEM_PROMPT,
        "user": USER_PROMPT,
    }


@pytest.mark.parametrize(
    "cfg",
    [
        Config(llm_mode="none"),
        Config(llm_mode="x" * 17),
        Config(llm_mode="local", llm_provider="p" * 129),
        Config(llm_mode="local", llm_model="m" * 513),
        Config(llm_mode="local", seed=True),
    ],
    ids=["mode-none", "mode-too-long", "provider-too-long", "model-too-long", "bool-seed"],
)
def test_llm_worker_input_rejects_a_configuration_the_worker_would_reject(cfg: Config) -> None:
    with pytest.raises(ValueError):
        _llm_worker_input(cfg, 5.0)


# --- the core probe slot ------------------------------------------------------------------------


def test_core_probe_slot_is_single_occupancy_and_released() -> None:
    assert doctor_probes._claim_core_probe_slot() is True
    try:
        assert doctor_probes._claim_core_probe_slot() is False
    finally:
        doctor_probes._release_core_probe_slot()
    assert doctor_probes._claim_core_probe_slot() is True
    doctor_probes._release_core_probe_slot()
