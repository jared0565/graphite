from __future__ import annotations

import hashlib
import json

import pytest

from graphite.routing.lifecycle import LifecycleProviderId, RuntimeKind
from graphite.routing.ollama_probe import observe_ollama
from graphite.routing.probe_runner import HttpProbeResult, ProbeEndpointPurpose, ProviderProbeError


class ScriptedHttp:
    def __init__(self, payloads: list[object]) -> None:
        self.payloads = payloads
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> HttpProbeResult:
        self.calls.append(kwargs)
        body = json.dumps(self.payloads.pop(0), separators=(",", ":")).encode()
        return HttpProbeResult(200, body, hashlib.sha256(body).hexdigest(), 0.01)


def test_ollama_observation_binds_tag_to_immutable_digest() -> None:
    digest = "a" * 64
    transport = ScriptedHttp([
        {"version": "0.12.0"},
        {"models": [{"name": "qwen3:8b", "model": "qwen3:8b", "digest": f"sha256:{digest}", "details": {}}]},
        {"capabilities": ["completion", "tools"]},
    ])
    identity = observe_ollama(host="127.0.0.1", port=11434, model_tag="qwen3:8b", observed_at=300, policy_version="1.0.0", transport=transport)
    assert identity.provider is LifecycleProviderId.OLLAMA
    assert identity.runtime_kind is RuntimeKind.LOCAL_HTTP
    assert identity.version == "0.12.0"
    assert identity.model_identity_digest == digest
    assert identity.capabilities == ("completion", "metadata_show", "metadata_tags", "tools", "version")
    assert [call["endpoint"].purpose for call in transport.calls] == [ProbeEndpointPurpose.OLLAMA_VERSION, ProbeEndpointPurpose.OLLAMA_TAGS, ProbeEndpointPurpose.OLLAMA_SHOW]
    assert json.loads(transport.calls[2]["request_body"]) == {"model": "qwen3:8b", "verbose": False}


def test_ollama_observation_rejects_missing_tag_without_fallback() -> None:
    transport = ScriptedHttp([{"version": "0.12.0"}, {"models": []}])
    with pytest.raises(ProviderProbeError, match="^probe_model_unavailable$"):
        observe_ollama(host="127.0.0.1", port=11434, model_tag="missing:1b", observed_at=1, policy_version="1.0.0", transport=transport)
    assert len(transport.calls) == 2


def test_ollama_observation_sanitizes_unexpected_transport_failure() -> None:
    def failing(**_kwargs: object) -> HttpProbeResult:
        raise RuntimeError("PRIVATE endpoint diagnostic")

    with pytest.raises(ProviderProbeError, match="^probe_failed$") as caught:
        observe_ollama(host="127.0.0.1", port=11434, model_tag="qwen3:8b", observed_at=1, policy_version="1.0.0", transport=failing)
    assert "PRIVATE" not in repr(caught.value)
