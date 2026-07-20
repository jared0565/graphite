"""Governed OpenRouter development executor tests."""
from __future__ import annotations

import hashlib
import json

import pytest

from graphite.routing.claude_executor import AdapterError
from graphite.routing.contracts import ProviderId
from graphite.routing.openrouter_executor import (
    ADAPTER_PROTOCOL_VERSION,
    API_CONTRACT_VERSION,
    CANONICAL_ENDPOINT,
    preflight_openrouter,
)
from graphite.routing.probe_runner import HttpProbeResult, ProviderProbeError


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
