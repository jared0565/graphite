from __future__ import annotations

import hashlib
import json

import pytest

from graphite.routing.lifecycle import LifecycleProviderId, RuntimeKind
from graphite.routing.openrouter_probe import observe_openrouter
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


def test_openrouter_auth_http_failure_is_sanitized_and_not_retried() -> None:
    calls = 0

    def denied(**_kwargs: object) -> HttpProbeResult:
        nonlocal calls
        calls += 1
        raise ProviderProbeError("probe_http_status")

    with pytest.raises(ProviderProbeError, match="^probe_auth_unhealthy$"):
        observe_openrouter(endpoint="https://openrouter.ai/api/v1", api_key="secret", model_id="openai/gpt-5", routing_policy={"allow_fallbacks": False}, observed_at=1, policy_version="1.0.0", transport=denied)
    assert calls == 1
