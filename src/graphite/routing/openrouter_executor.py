"""Hardened adapter for governed OpenRouter development execution."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from .claude_executor import AdapterError
from .contracts import CliIdentity, ProviderId
from .lifecycle import ProviderRuntimeIdentity
from .openrouter_probe import (
    CANONICAL_ENDPOINT,
    HttpProbe,
    OpenRouterPricing,
    observe_openrouter_with_pricing,
)
from .probe_runner import ProviderProbeError, run_http_probe

ADAPTER_PROTOCOL_VERSION: Final = "1.0.0"
API_CONTRACT_VERSION: Final = "1.0.0"
_PROBE_FAILURE_CODES: Final = {
    "probe_model_unavailable": "model_unavailable",
    "probe_auth_unhealthy": "auth_required",
}

__all__ = [
    "ADAPTER_PROTOCOL_VERSION",
    "API_CONTRACT_VERSION",
    "CANONICAL_ENDPOINT",
    "OpenRouterPreflight",
    "preflight_openrouter",
]


@dataclass(frozen=True, slots=True)
class OpenRouterPreflight:
    """Endpoint runtime identity plus pinned pricing for one governed model."""

    identity: CliIdentity
    runtime: ProviderRuntimeIdentity
    pricing: OpenRouterPricing


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def preflight_openrouter(
    *,
    api_key: str,
    model_id: str,
    routing_policy: Mapping[str, object],
    observed_at: int,
    policy_version: str,
    transport: HttpProbe = run_http_probe,
) -> OpenRouterPreflight:
    """Bind endpoint, model, routing policy, and pricing into one identity digest."""
    try:
        observation = observe_openrouter_with_pricing(
            endpoint=CANONICAL_ENDPOINT,
            api_key=api_key,
            model_id=model_id,
            routing_policy=routing_policy,
            observed_at=observed_at,
            policy_version=policy_version,
            transport=transport,
        )
    except ProviderProbeError as exc:
        raise AdapterError(_PROBE_FAILURE_CODES.get(exc.code, "unavailable")) from None
    runtime = observation.identity
    composite = _canonical_sha256(
        {
            "endpoint": runtime.runtime_digest,
            "model": runtime.model_identity_digest,
            "pricing": observation.pricing.digest,
            "routing": runtime.routing_policy_digest,
        }
    )
    identity = CliIdentity(
        ProviderId.OPENROUTER,
        composite,
        API_CONTRACT_VERSION,
        ADAPTER_PROTOCOL_VERSION,
    )
    return OpenRouterPreflight(identity, runtime, observation.pricing)
