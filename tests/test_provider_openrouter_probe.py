from __future__ import annotations

import hashlib
import json

import pytest

from graphite.routing.lifecycle import LifecycleProviderId, RuntimeKind
from graphite.routing.openrouter_probe import (
    CANONICAL_ENDPOINT,
    OpenRouterPricing,
    completion_cost_microunits,
    observe_openrouter,
    observe_openrouter_with_pricing,
)
from graphite.routing.probe_runner import HttpProbeResult, ProbeEndpointPurpose, ProviderProbeError


class ScriptedHttp:
    def __init__(self, payloads: list[object]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> HttpProbeResult:
        self.calls.append(kwargs)
        body = json.dumps(self.payloads.pop(0), separators=(",", ":")).encode()
        return HttpProbeResult(200, body, hashlib.sha256(body).hexdigest(), 0.01)


def test_openrouter_observation_binds_model_routing_and_auth_at_boundary() -> None:
    transport = ScriptedHttp([{"label": "private account"}, {"data": [{"id": "anthropic/claude-sonnet-5"}]}])
    identity = observe_openrouter(endpoint="https://openrouter.ai/api/v1", api_key="PRIVATE", model_id="anthropic/claude-sonnet-5", routing_policy={"allow_fallbacks": False, "order": ["Anthropic"]}, observed_at=400, policy_version="1.0.0", transport=transport)
    assert identity.provider is LifecycleProviderId.OPENROUTER
    assert identity.runtime_kind is RuntimeKind.REMOTE_HTTPS
    assert identity.version == "1.0.0"
    assert identity.model_identity_digest is not None
    assert identity.routing_policy_digest is not None
    assert identity.capabilities == ("credential_health", "models_metadata")
    assert [call["endpoint"].purpose for call in transport.calls] == [ProbeEndpointPurpose.OPENROUTER_AUTH_KEY, ProbeEndpointPurpose.OPENROUTER_MODELS]
    assert all(call["authorization"] == "Bearer PRIVATE" for call in transport.calls)
    assert "PRIVATE" not in repr(identity)
    assert "private account" not in repr(identity)


@pytest.mark.parametrize("endpoint", ["http://openrouter.ai/api/v1", "https://evil.test/api/v1", "https://openrouter.ai/api/v1/"])
def test_openrouter_observation_rejects_noncanonical_endpoint(endpoint: str) -> None:
    with pytest.raises(ProviderProbeError, match="^probe_endpoint_invalid$"):
        observe_openrouter(endpoint=endpoint, api_key="secret", model_id="openai/gpt-5", routing_policy={"allow_fallbacks": False}, observed_at=1, policy_version="1.0.0", transport=ScriptedHttp([]))


def test_openrouter_observation_reports_missing_model_without_inference() -> None:
    transport = ScriptedHttp([{"ok": True}, {"data": []}])
    with pytest.raises(ProviderProbeError, match="^probe_model_unavailable$"):
        observe_openrouter(endpoint="https://openrouter.ai/api/v1", api_key="secret", model_id="openai/gpt-5", routing_policy={"allow_fallbacks": False}, observed_at=1, policy_version="1.0.0", transport=transport)
    assert len(transport.calls) == 2


def _fake_transport_with_models(data: list[object]) -> ScriptedHttp:
    return ScriptedHttp([{"ok": True}, {"data": data}])


def test_observe_with_pricing_binds_catalog_pricing() -> None:
    observation = observe_openrouter_with_pricing(
        endpoint=CANONICAL_ENDPOINT, api_key="k", model_id="moonshotai/kimi-k3",
        routing_policy={"order": ["moonshotai"]}, observed_at=1, policy_version="1.0.0",
        transport=_fake_transport_with_models(
            [{"id": "moonshotai/kimi-k3", "pricing": {"prompt": "0.0000006", "completion": "0.0000025"}}]
        ),
    )
    assert observation.identity.model_identity_digest is not None
    assert observation.pricing.prompt == "0.0000006"
    assert observation.pricing.completion == "0.0000025"
    assert len(observation.pricing.digest) == 64


def test_observe_with_pricing_fails_closed_on_missing_pricing() -> None:
    with pytest.raises(ProviderProbeError, match="^probe_protocol_invalid$"):
        observe_openrouter_with_pricing(
            endpoint=CANONICAL_ENDPOINT, api_key="k", model_id="moonshotai/kimi-k3",
            routing_policy={}, observed_at=1, policy_version="1.0.0",
            transport=_fake_transport_with_models([{"id": "moonshotai/kimi-k3"}]),
        )


@pytest.mark.parametrize(
    ("prompt", "completion"),
    [
        ("-0.000001", "0.000002"),
        ("1.5", "0.000002"),
        ("0.000001", "abc"),
        ("0.000001", ""),
        ("1e-6", "0.000002"),
    ],
)
def test_pricing_rejects_malformed_or_out_of_range_values(prompt: str, completion: str) -> None:
    with pytest.raises(ProviderProbeError, match="^probe_protocol_invalid$"):
        OpenRouterPricing(prompt=prompt, completion=completion)


def test_completion_cost_rounds_up_in_exact_decimal() -> None:
    pricing = OpenRouterPricing(prompt="0.0000006", completion="0.0000025")
    # 10_000*0.0000006 + 1_000*0.0000025 = 0.0085 USD -> 8_500 microunits
    assert completion_cost_microunits(pricing, input_tokens=10_000, output_tokens=1_000) == 8_500
    # 1*0.0000006 = 0.6 microunits -> ceil -> 1
    assert completion_cost_microunits(pricing, input_tokens=1, output_tokens=0) == 1
    assert completion_cost_microunits(pricing, input_tokens=0, output_tokens=0) == 0
    with pytest.raises(ProviderProbeError, match="^probe_request_invalid$"):
        completion_cost_microunits(pricing, input_tokens=-1, output_tokens=0)
    with pytest.raises(ProviderProbeError, match="^probe_request_invalid$"):
        completion_cost_microunits(pricing, input_tokens=100_000_001, output_tokens=0)


def test_openrouter_auth_http_failure_is_sanitized_and_not_retried() -> None:
    calls = 0

    def denied(**_kwargs: object) -> HttpProbeResult:
        nonlocal calls
        calls += 1
        raise ProviderProbeError("probe_http_status")

    with pytest.raises(ProviderProbeError, match="^probe_auth_unhealthy$"):
        observe_openrouter(endpoint="https://openrouter.ai/api/v1", api_key="secret", model_id="openai/gpt-5", routing_policy={"allow_fallbacks": False}, observed_at=1, policy_version="1.0.0", transport=denied)
    assert calls == 1
