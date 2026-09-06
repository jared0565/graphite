"""Tests for `graphite.routing.ollama_executor`, the single-shot loopback executor.

Named after the module so aramid's mutation stage 1 (`tests/test_<module>.py`)
has something to run; `-k ollama_executor` selected nothing before this file.
`test_routing_executor.py` and `test_routing_security.py` drive `execute_ollama`
end to end against a loopback server; this file pins the units underneath it
-- the endpoint gate, the bounded response reader, the deadline arithmetic,
the prompt shape and the canonical request bytes -- with plain data.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from graphite.routing.context_builder import (
    ContextBundle,
    ManifestItem,
    OutboundManifest,
    PrivateContextItem,
)
from graphite.routing.contracts import Effort
from graphite.routing.effort import EffortMappingError
from graphite.routing import ollama_executor
from graphite.routing.ollama_executor import (
    MAX_HEADER_BYTES,
    MAX_HEADERS,
    SYSTEM_CONTRACT,
    CanonicalProviderRequest,
    ExecutorError,
    _json,
    _prompt,
    _read_bounded,
    _remaining,
    _usage,
    _validate_endpoint,
    canonical_provider_request,
)


class _Response:
    """The two things `_read_bounded` reads from an HTTPResponse."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.headers = dict(headers or {})
        self._body = body
        self.reads: list[int] = []

    def read(self, amount: int) -> bytes:
        self.reads.append(amount)
        chunk, self._body = self._body[:amount], self._body[amount:]
        return chunk


def _context(private: bool = True) -> ContextBundle:
    item = ManifestItem("src/app.py", 6, "f" * 64, "explicit_target")
    manifest = OutboundManifest("1", (item,), 6, 0, "h" * 64)
    items = (PrivateContextItem("src/app.py", "x = 1\n"),) if private else ()
    return ContextBundle(manifest, items)


# --- ExecutorError ------------------------------------------------------------------


def test_executor_error_is_its_code_and_nothing_else() -> None:
    error = ExecutorError("provider_protocol")
    assert error.code == "provider_protocol"
    assert str(error) == "provider_protocol"


# --- _remaining --------------------------------------------------------------------------


def test_remaining_is_the_time_left_before_the_deadline() -> None:
    assert _remaining(10.0, lambda: 7.5) == 2.5


@pytest.mark.parametrize("now", [10.0, 10.5])
def test_remaining_raises_timeout_at_or_past_the_deadline(now: float) -> None:
    with pytest.raises(ExecutorError, match="timeout"):
        _remaining(10.0, lambda: now)


# --- _validate_endpoint -----------------------------------------------------------------


def test_validate_endpoint_accepts_loopback_on_an_allowed_port() -> None:
    assert _validate_endpoint("127.0.0.1", 11_434, frozenset({11_434})) == ("127.0.0.1", 11_434)
    assert _validate_endpoint("::1", 8080, frozenset({8080, 11_434})) == ("::1", 8080)


@pytest.mark.parametrize(
    ("host", "port", "allowed"),
    [
        ("localhost", 11_434, frozenset({11_434})),
        ("0.0.0.0", 11_434, frozenset({11_434})),  # noqa: S104 -- the wildcard host is the input being REFUSED
        ("127.0.0.1", 11_435, frozenset({11_434})),
        ("127.0.0.1", "11434", frozenset({11_434})),
        ("127.0.0.1", True, frozenset({1})),
        ("127.0.0.1", 11_434, frozenset()),
        ("127.0.0.1", 11_434, frozenset({11_434, 0})),
        ("127.0.0.1", 11_434, frozenset({11_434, 65_536})),
        (b"127.0.0.1", 11_434, frozenset({11_434})),
    ],
    ids=[
        "named-host",
        "wildcard-host",
        "port-not-allowed",
        "string-port",
        "bool-port",
        "no-allowed-ports",
        "allowed-port-zero",
        "allowed-port-too-high",
        "bytes-host",
    ],
)
def test_validate_endpoint_refuses_anything_but_canonical_loopback(
    host: object, port: object, allowed: frozenset[int]
) -> None:
    with pytest.raises(ExecutorError, match="executor_endpoint_invalid"):
        _validate_endpoint(host, port, allowed)


# --- _read_bounded ----------------------------------------------------------------------


def test_read_bounded_returns_the_whole_body_within_the_limit() -> None:
    response = _Response(b"hello", {"Content-Length": "5"})
    assert _read_bounded(response, maximum=64) == b"hello"  # type: ignore[arg-type]


def test_read_bounded_never_asks_for_more_than_the_limit_plus_one() -> None:
    response = _Response(b"x" * 10)
    assert _read_bounded(response, maximum=10) == b"x" * 10  # type: ignore[arg-type]
    assert max(response.reads) <= 11


def test_read_bounded_rejects_a_body_over_the_limit_even_without_a_length_header() -> None:
    with pytest.raises(ExecutorError, match="response_limit"):
        _read_bounded(_Response(b"x" * 11), maximum=10)  # type: ignore[arg-type]


@pytest.mark.parametrize("declared", ["11", "-1"])
def test_read_bounded_rejects_a_declared_length_outside_the_limit_before_reading(declared: str) -> None:
    response = _Response(b"", {"Content-Length": declared})
    with pytest.raises(ExecutorError, match="response_limit"):
        _read_bounded(response, maximum=10)  # type: ignore[arg-type]
    assert response.reads == []


def test_read_bounded_rejects_a_malformed_or_mismatched_declared_length() -> None:
    with pytest.raises(ExecutorError, match="provider_protocol"):
        _read_bounded(_Response(b"abc", {"Content-Length": "three"}), maximum=10)  # type: ignore[arg-type]
    with pytest.raises(ExecutorError, match="provider_protocol"):
        _read_bounded(_Response(b"abc", {"Content-Length": "4"}), maximum=10)  # type: ignore[arg-type]


def test_read_bounded_rejects_too_many_or_too_large_headers() -> None:
    many = {f"X-H{i}": "v" for i in range(MAX_HEADERS + 1)}
    with pytest.raises(ExecutorError, match="response_limit"):
        _read_bounded(_Response(b"", many), maximum=10)  # type: ignore[arg-type]
    large = {"X-Big": "v" * MAX_HEADER_BYTES}
    with pytest.raises(ExecutorError, match="response_limit"):
        _read_bounded(_Response(b"", large), maximum=10)  # type: ignore[arg-type]
    exactly = {f"X-H{i}": "v" for i in range(MAX_HEADERS)}
    assert _read_bounded(_Response(b"ok", exactly), maximum=10) == b"ok"  # type: ignore[arg-type]


# --- _json ----------------------------------------------------------------------------------


def test_json_decodes_utf8_json() -> None:
    assert _json('{"a": "é"}'.encode("utf-8")) == {"a": "é"}


@pytest.mark.parametrize("raw", [b"\xff\xfe", b"{not json"], ids=["not-utf8", "not-json"])
def test_json_maps_undecodable_input_to_provider_protocol(raw: bytes) -> None:
    with pytest.raises(ExecutorError, match="provider_protocol"):
        _json(raw)


def test_json_maps_a_recursion_error_to_provider_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    # Raised directly rather than provoked with deep nesting: how many nested
    # brackets overflow the decoder depends on the platform's C stack (100,000
    # overflow Windows' 1 MB stack and parse cleanly on Linux/macOS under 3.14,
    # which measures the real stack), so a literal would test the runner, not
    # the mapping.
    def overflow(text: str) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(ollama_executor.json, "loads", overflow)
    with pytest.raises(ExecutorError, match="provider_protocol"):
        _json(b"[]")


# --- _usage ---------------------------------------------------------------------------------


def test_usage_passes_through_an_integer_within_the_maximum_and_none_unchanged() -> None:
    assert _usage(None, 10) is None
    assert _usage(0, 10) == 0
    assert _usage(10, 10) == 10


@pytest.mark.parametrize("value", [-1, 11, True, 1.0, "5"])
def test_usage_rejects_anything_else_as_a_response_limit(value: object) -> None:
    with pytest.raises(ExecutorError, match="response_limit"):
        _usage(value, 10)


# --- _prompt ---------------------------------------------------------------------------------


def test_prompt_lays_out_objective_public_manifest_then_private_context() -> None:
    context = _context()
    prompt = _prompt("Explain the module", context)
    manifest_json = json.dumps(context.manifest.to_dict(), sort_keys=True, separators=(",", ":"))
    assert prompt == (
        "Task objective:\nExplain the module\n\nApproved context manifest:\n"
        + manifest_json
        + "\n\nApproved private context:\n--- src/app.py ---\nx = 1\n"
    )


def test_prompt_without_private_items_ends_after_the_heading() -> None:
    assert _prompt("o", _context(private=False)).endswith("\n\nApproved private context:\n")


@pytest.mark.parametrize("objective", ["", "nul\x00", "x" * 4_097, 5])
def test_prompt_rejects_an_unusable_objective(objective: object) -> None:
    with pytest.raises(ExecutorError, match="request_invalid"):
        _prompt(objective, _context())  # type: ignore[arg-type]


# --- canonical_provider_request ------------------------------------------------------------------


def test_canonical_request_is_sorted_compact_utf8_json_hashed_as_sent() -> None:
    manifest = SimpleNamespace(model_id="minimax-m3:cloud", max_output_tokens=256, effort=Effort.DEFAULT)
    request = canonical_provider_request(manifest=manifest, context=_context(), objective="Explain é")  # type: ignore[arg-type]
    assert isinstance(request, CanonicalProviderRequest)
    payload = json.loads(request.body.decode("utf-8"))
    assert payload == {
        "model": "minimax-m3:cloud",
        "messages": [
            {"role": "system", "content": SYSTEM_CONTRACT},
            {"role": "user", "content": _prompt("Explain é", _context())},
        ],
        "options": {"num_predict": 256},
        "stream": False,
    }
    assert request.body == json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert "é".encode("utf-8") in request.body  # ensure_ascii=False
    assert request.prompt_hash == hashlib.sha256(request.body).hexdigest()


def test_canonical_request_keeps_the_body_out_of_repr_and_equality() -> None:
    a = CanonicalProviderRequest(body=b"one", prompt_hash="h")
    b = CanonicalProviderRequest(body=b"two", prompt_hash="h")
    assert a == b
    assert "one" not in repr(a)


def test_canonical_request_refuses_a_model_or_effort_without_a_verified_payload() -> None:
    unknown_model = SimpleNamespace(model_id="unknown:latest", max_output_tokens=1, effort=Effort.DEFAULT)
    with pytest.raises(EffortMappingError):
        canonical_provider_request(manifest=unknown_model, context=_context(), objective="o")  # type: ignore[arg-type]
    unverified_effort = SimpleNamespace(model_id="minimax-m3:cloud", max_output_tokens=1, effort=Effort.MAX)
    with pytest.raises(EffortMappingError):
        canonical_provider_request(manifest=unverified_effort, context=_context(), objective="o")  # type: ignore[arg-type]
