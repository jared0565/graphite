"""Governed OpenRouter development executor tests."""
from __future__ import annotations

import hashlib
import json

import pytest

from graphite.routing.claude_executor import AdapterError
from graphite.routing.contracts import Effort, ProviderId
from graphite.routing.openrouter_executor import (
    ADAPTER_PROTOCOL_VERSION,
    API_CONTRACT_VERSION,
    CANONICAL_ENDPOINT,
    execute_openrouter,
    preflight_openrouter,
)
from graphite.routing.openrouter_probe import OpenRouterPricing
from graphite.routing.probe_runner import (
    HttpProbeResult,
    ProbeEndpointPurpose,
    ProviderProbeError,
)


class ScriptedHttp:
    def __init__(self, payloads: list[object]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> HttpProbeResult:
        self.calls.append(kwargs)
        body = json.dumps(self.payloads.pop(0), separators=(",", ":")).encode()
        return HttpProbeResult(200, body, hashlib.sha256(body).hexdigest(), 0.01)


def _fake_transport_with_models(data: list[object]) -> ScriptedHttp:
    return ScriptedHttp([{"ok": True}, {"data": data}])


def test_preflight_binds_endpoint_model_routing_and_pricing() -> None:
    preflight = preflight_openrouter(
        api_key="k", model_id="moonshotai/kimi-k3",
        routing_policy={"order": ["moonshotai"]}, observed_at=7, policy_version="1.0.0",
        transport=_fake_transport_with_models(
            [{"id": "moonshotai/kimi-k3", "pricing": {"prompt": "0.0000006", "completion": "0.0000025"}}]
        ),
    )
    assert preflight.identity.provider is ProviderId.OPENROUTER
    assert len(preflight.identity.executable_sha256) == 64
    assert preflight.identity.cli_version == API_CONTRACT_VERSION == "1.0.0"
    assert preflight.identity.adapter_protocol_version == ADAPTER_PROTOCOL_VERSION == "1.0.0"
    assert CANONICAL_ENDPOINT == "https://openrouter.ai/api/v1"
    expected = hashlib.sha256(json.dumps({
        "endpoint": preflight.runtime.runtime_digest,
        "model": preflight.runtime.model_identity_digest,
        "pricing": preflight.pricing.digest,
        "routing": preflight.runtime.routing_policy_digest,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert preflight.identity.executable_sha256 == expected


@pytest.mark.parametrize(
    ("probe_code", "adapter_code"),
    [
        ("probe_model_unavailable", "model_unavailable"),
        ("probe_auth_unhealthy", "auth_required"),
        ("probe_timeout", "unavailable"),
        ("probe_protocol_invalid", "unavailable"),
    ],
)
def test_preflight_maps_probe_failures_to_stable_adapter_codes(
    probe_code: str, adapter_code: str
) -> None:
    def failing(**_kwargs: object) -> HttpProbeResult:
        raise ProviderProbeError(probe_code)

    with pytest.raises(AdapterError, match=f"^{adapter_code}$"):
        preflight_openrouter(
            api_key="k", model_id="moonshotai/kimi-k3",
            routing_policy={}, observed_at=7, policy_version="1.0.0",
            transport=failing,
        )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


_SCHEMA = {
    "additionalProperties": False,
    "properties": {"result": {"const": "GRAPHITE_EDIT_OK", "type": "string"}},
    "required": ["result"],
    "type": "object",
}


def _completion(
    content: str,
    *,
    model: str = "moonshotai/kimi-k3",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    usage: bool = True,
) -> bytes:
    payload: dict[str, object] = {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }
    if usage:
        payload["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
    return json.dumps(payload).encode()


class _RecordingTransport:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> HttpProbeResult:
        self.calls.append(kwargs)
        return HttpProbeResult(200, self.body, hashlib.sha256(self.body).hexdigest(), 0.05)

    @property
    def endpoint(self) -> object:
        return self.calls[-1]["endpoint"]

    @property
    def request_body(self) -> bytes:
        return self.calls[-1]["request_body"]


def _execute(transport: _RecordingTransport, **overrides: object) -> object:
    kwargs: dict[str, object] = dict(
        api_key="k",
        prompt=b"do the approved task",
        requested_model="moonshotai/kimi-k3",
        expected_effective_model="moonshotai/kimi-k3",
        effort=Effort.HIGH,
        output_schema=_SCHEMA,
        output_schema_sha256=_canonical_sha256(_SCHEMA),
        pricing=OpenRouterPricing(prompt="0.000001", completion="0.000002"),
        max_output_tokens=4096,
        max_cost_microunits=10_000,
        timeout_seconds=120.0,
        transport=transport,
    )
    kwargs.update(overrides)
    return execute_openrouter(**kwargs)


def test_execute_builds_canonical_schema_bound_request_and_returns_canonical_json() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    outcome = _execute(transport)
    assert outcome.message == '{"result":"GRAPHITE_EDIT_OK"}'
    assert outcome.effective_model == "moonshotai/kimi-k3"
    assert outcome.input_tokens == 100
    assert outcome.output_tokens == 50
    assert outcome.cost_microunits == 200  # 100*1 + 50*2 microunits
    assert outcome.duration_seconds == 0.05
    assert len(transport.calls) == 1
    body = json.loads(transport.request_body)
    assert body["temperature"] == 0
    assert body["stream"] is False
    assert body["model"] == "moonshotai/kimi-k3"
    assert body["max_tokens"] == 4096
    assert body["reasoning"] == {"effort": "high"}
    assert body["messages"] == [{"content": "do the approved task", "role": "user"}]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["name"] == "graphite_response"
    assert body["response_format"]["json_schema"]["schema"] == _SCHEMA
    assert body["usage"] == {"include": True}
    assert transport.request_body == json.dumps(
        body, sort_keys=True, separators=(",", ":")
    ).encode()
    assert transport.endpoint.purpose is ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS
    assert transport.calls[-1]["authorization"] == "Bearer k"
    assert outcome.request_sha256 == hashlib.sha256(transport.request_body).hexdigest()
    assert outcome.response_sha256 == hashlib.sha256(transport.body).hexdigest()
    assert "GRAPHITE_EDIT_OK" not in repr(outcome)


def test_execute_fails_closed_over_cost_ceiling() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    with pytest.raises(AdapterError, match="^cost_ceiling_exceeded$"):
        _execute(transport, max_cost_microunits=199)  # cost is exactly 200


@pytest.mark.parametrize("content", ["prose, not JSON", "[1,2]", "42"])
def test_execute_rejects_non_object_content(content: str) -> None:
    transport = _RecordingTransport(_completion(content))
    with pytest.raises(AdapterError, match="^response_contract_invalid$"):
        _execute(transport)


def test_execute_rejects_wrong_model_echo() -> None:
    transport = _RecordingTransport(
        _completion('{"result":"GRAPHITE_EDIT_OK"}', model="other/model")
    )
    with pytest.raises(AdapterError, match="^model_mismatch$"):
        _execute(transport)


def test_execute_schema_digest_mismatch_never_reaches_transport() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    with pytest.raises(AdapterError, match="^request_invalid$"):
        _execute(transport, output_schema_sha256="0" * 64)
    assert transport.calls == []


def test_execute_requires_usage() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}', usage=False))
    with pytest.raises(AdapterError, match="^protocol$"):
        _execute(transport)


@pytest.mark.parametrize("effort", [Effort.XHIGH, Effort.MAX, Effort.DEFAULT])
def test_execute_rejects_unsupported_effort(effort: Effort) -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    with pytest.raises(AdapterError, match="^request_invalid$"):
        _execute(transport, effort=effort)
    assert transport.calls == []


def test_execute_requires_api_key_before_any_request() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    with pytest.raises(AdapterError, match="^auth_required$"):
        _execute(transport, api_key="")
    assert transport.calls == []


def test_execute_rejects_undecodable_prompt() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    with pytest.raises(AdapterError, match="^request_invalid$"):
        _execute(transport, prompt=b"\xff\xfe")
    assert transport.calls == []
