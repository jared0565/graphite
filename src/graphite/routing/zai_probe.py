"""Local (no-network) z.ai identity + operator-pinned pricing for governed verification."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .contracts import CliIdentity, ProviderId
from .lifecycle import (
    LifecycleProviderId,
    ProviderCompatibilityPolicy,
    ProviderRuntimeIdentity,
    RuntimeKind,
)
from .probe_runner import ProviderProbeError

ZAI_HOST = "api.z.ai"
ZAI_CANONICAL_ENDPOINT = "https://api.z.ai/api/paas/v4"
ZAI_MODEL = "glm-5.2"
ZAI_API_CONTRACT_VERSION = "1.0.0"
ZAI_ADAPTER_PROTOCOL_VERSION = "1.0.0"
ZAI_CAPABILITIES = ("remote_inference",)
# z.ai model ids are single-segment (no vendor/model slash, unlike OpenRouter).
_ZAI_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PRICE = re.compile(r"^(0|[0-9]{1,10}(\.[0-9]{1,18})?|\.[0-9]{1,18})$")

# Compatibility policy for lifecycle observe(): promotes a DISCOVERED z.ai
# runtime identity to VERIFICATION_REQUIRED once it matches this shape.
# No `_OPENROUTER_POLICY` (or any other per-provider policy constant)
# exists elsewhere in this codebase to clone; policies are otherwise built
# ad hoc at each call site. This constant is the zai-owned analog of where
# such a policy would live for OpenRouter (openrouter_probe.py).
_ZAI_POLICY = ProviderCompatibilityPolicy(
    provider=LifecycleProviderId.ZAI,
    runtime_kind=RuntimeKind.REMOTE_HTTPS,
    policy_version="1.0.0",
    minimum_version="1.0.0",
    maximum_version_exclusive="2.0.0",
    required_capabilities=ZAI_CAPABILITIES,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ZaiPricing:
    """Operator-pinned per-token USD prices as exact decimal strings."""

    prompt: str
    completion: str

    def __post_init__(self) -> None:
        for value in (self.prompt, self.completion):
            if not isinstance(value, str) or len(value) > 64 or _PRICE.fullmatch(value) is None:
                raise ProviderProbeError("probe_protocol_invalid")
            try:
                parsed = Decimal(value)
            except InvalidOperation:
                raise ProviderProbeError("probe_protocol_invalid") from None
            if not Decimal(0) <= parsed <= Decimal(1):
                raise ProviderProbeError("probe_protocol_invalid")

    @property
    def digest(self) -> str:
        return _digest({"completion": self.completion, "prompt": self.prompt})


def zai_cost_microunits(pricing: ZaiPricing, *, input_tokens: int, output_tokens: int) -> int:
    """Ceiling of the exact-decimal USD cost in microunits."""
    if not isinstance(pricing, ZaiPricing):
        raise ProviderProbeError("probe_request_invalid")
    for value in (input_tokens, output_tokens):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100_000_000:
            raise ProviderProbeError("probe_request_invalid")
    cost = (
        Decimal(input_tokens) * Decimal(pricing.prompt)
        + Decimal(output_tokens) * Decimal(pricing.completion)
    ) * Decimal(1_000_000)
    whole = int(cost)
    return whole if cost == whole else whole + 1


@dataclass(frozen=True, slots=True)
class ZaiPreflight:
    identity: CliIdentity
    runtime: ProviderRuntimeIdentity
    pricing: ZaiPricing


def preflight_zai(*, model_id: str, observed_at: int, policy_version: str) -> ZaiPreflight:
    """Construct the z.ai runtime + CLI identity and pinned pricing locally (no network)."""
    if not isinstance(model_id, str) or _ZAI_MODEL_ID.fullmatch(model_id) is None:
        raise ProviderProbeError("probe_request_invalid")
    pricing = ZaiPricing(prompt="0.0000014", completion="0.0000044")
    endpoint_digest = hashlib.sha256(ZAI_CANONICAL_ENDPOINT.encode("ascii")).hexdigest()
    model_digest = _digest(model_id)
    try:
        runtime = ProviderRuntimeIdentity(
            LifecycleProviderId.ZAI,
            RuntimeKind.REMOTE_HTTPS,
            ZAI_API_CONTRACT_VERSION,
            endpoint_digest,
            model_digest,
            None,                       # routing_policy_digest forbidden for zai
            ZAI_CAPABILITIES,
            policy_version,
            observed_at,
        )
    except ValueError:
        raise ProviderProbeError("probe_request_invalid") from None
    composite = _digest(
        {"endpoint": endpoint_digest, "model": model_digest, "pricing": pricing.digest}
    )
    identity = CliIdentity(
        ProviderId.ZAI,
        composite,
        ZAI_API_CONTRACT_VERSION,
        ZAI_ADAPTER_PROTOCOL_VERSION,
    )
    return ZaiPreflight(identity, runtime, pricing)
