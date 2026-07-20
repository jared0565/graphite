"""Bounded, non-inference OpenRouter lifecycle observation."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .lifecycle import LifecycleProviderId, ProviderRuntimeIdentity, RuntimeKind
from .probe_runner import HttpProbeEndpoint, HttpProbeResult, ProbeEndpointPurpose, ProviderProbeError, run_http_probe

CANONICAL_ENDPOINT = "https://openrouter.ai/api/v1"
API_CONTRACT_VERSION = "1.0.0"
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_PRICE = re.compile(r"^(0|[0-9]{1,10}(\.[0-9]{1,18})?|\.[0-9]{1,18})$")
HttpProbe = Callable[..., HttpProbeResult]


def _digest(value: object) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):
        raise ProviderProbeError("probe_request_invalid") from None
    if len(encoded) > 16 * 1024:
        raise ProviderProbeError("probe_request_invalid")
    return hashlib.sha256(encoded).hexdigest()


def _json(result: HttpProbeResult) -> object:
    if not isinstance(result, HttpProbeResult):
        raise ProviderProbeError("probe_protocol_invalid")
    try:
        return json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ProviderProbeError("probe_protocol_invalid") from None


@dataclass(frozen=True, slots=True)
class OpenRouterPricing:
    """Catalog-reported per-token USD prices as exact decimal strings."""

    prompt: str
    completion: str

    def __post_init__(self) -> None:
        for value in (self.prompt, self.completion):
            if (
                not isinstance(value, str)
                or len(value) > 64
                or _PRICE.fullmatch(value) is None
            ):
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


@dataclass(frozen=True, slots=True)
class OpenRouterObservation:
    identity: ProviderRuntimeIdentity
    pricing: OpenRouterPricing


def completion_cost_microunits(
    pricing: OpenRouterPricing, *, input_tokens: int, output_tokens: int
) -> int:
    """Ceiling of the exact-decimal USD cost expressed in microunits."""
    if not isinstance(pricing, OpenRouterPricing):
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


def _observe(
    *, endpoint: str, api_key: str, model_id: str, routing_policy: Mapping[str, object],
    observed_at: int, policy_version: str, timeout_seconds: float = 10.0,
    transport: HttpProbe = run_http_probe, clock: Callable[[], float] = time.monotonic,
) -> tuple[ProviderRuntimeIdentity, dict[str, object]]:
    if endpoint != CANONICAL_ENDPOINT:
        raise ProviderProbeError("probe_endpoint_invalid")
    if not isinstance(api_key, str) or not api_key or len(api_key) > 4096 or any(char in api_key for char in "\r\n\x00") or not isinstance(model_id, str) or _MODEL_ID.fullmatch(model_id) is None or not isinstance(routing_policy, Mapping):
        raise ProviderProbeError("probe_request_invalid")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0.1 <= timeout_seconds <= 30:
        raise ProviderProbeError("probe_request_invalid")
    routing_digest = _digest(dict(routing_policy))
    deadline = clock() + float(timeout_seconds)
    authorization = f"Bearer {api_key}"

    def call(purpose: ProbeEndpointPurpose) -> object:
        remaining = deadline - clock()
        if remaining <= 0:
            raise ProviderProbeError("probe_timeout")
        target = HttpProbeEndpoint(LifecycleProviderId.OPENROUTER, "https", "openrouter.ai", 443, purpose)
        try:
            result = transport(endpoint=target, timeout_seconds=remaining, authorization=authorization)
        except ProviderProbeError as exc:
            if purpose is ProbeEndpointPurpose.OPENROUTER_AUTH_KEY and exc.code == "probe_http_status":
                raise ProviderProbeError("probe_auth_unhealthy") from None
            raise
        except Exception:
            raise ProviderProbeError("probe_failed") from None
        return _json(result)

    auth = call(ProbeEndpointPurpose.OPENROUTER_AUTH_KEY)
    if not isinstance(auth, dict):
        raise ProviderProbeError("probe_auth_unhealthy")
    models = call(ProbeEndpointPurpose.OPENROUTER_MODELS)
    data = models.get("data") if isinstance(models, dict) else None
    if not isinstance(data, list) or len(data) > 2048:
        raise ProviderProbeError("probe_protocol_invalid")
    matched = next(
        (
            item
            for item in data
            if isinstance(item, dict) and item.get("id") == model_id
        ),
        None,
    )
    if matched is None:
        raise ProviderProbeError("probe_model_unavailable")
    try:
        identity = ProviderRuntimeIdentity(LifecycleProviderId.OPENROUTER, RuntimeKind.REMOTE_HTTPS, API_CONTRACT_VERSION, hashlib.sha256(endpoint.encode("ascii")).hexdigest(), _digest(model_id), routing_digest, ("credential_health", "models_metadata"), policy_version, observed_at)
    except ValueError:
        raise ProviderProbeError("probe_request_invalid") from None
    return identity, matched


def observe_openrouter(
    *, endpoint: str, api_key: str, model_id: str, routing_policy: Mapping[str, object],
    observed_at: int, policy_version: str, timeout_seconds: float = 10.0,
    transport: HttpProbe = run_http_probe, clock: Callable[[], float] = time.monotonic,
) -> ProviderRuntimeIdentity:
    """Observe exact OpenRouter auth/model metadata; never invoke inference."""
    return _observe(
        endpoint=endpoint, api_key=api_key, model_id=model_id,
        routing_policy=routing_policy, observed_at=observed_at,
        policy_version=policy_version, timeout_seconds=timeout_seconds,
        transport=transport, clock=clock,
    )[0]


def observe_openrouter_with_pricing(
    *, endpoint: str, api_key: str, model_id: str, routing_policy: Mapping[str, object],
    observed_at: int, policy_version: str, timeout_seconds: float = 10.0,
    transport: HttpProbe = run_http_probe, clock: Callable[[], float] = time.monotonic,
) -> OpenRouterObservation:
    """Observe auth/model metadata and bind the catalog's exact pricing; never invoke inference."""
    identity, entry = _observe(
        endpoint=endpoint, api_key=api_key, model_id=model_id,
        routing_policy=routing_policy, observed_at=observed_at,
        policy_version=policy_version, timeout_seconds=timeout_seconds,
        transport=transport, clock=clock,
    )
    raw = entry.get("pricing")
    if not isinstance(raw, dict):
        raise ProviderProbeError("probe_protocol_invalid")
    pricing = OpenRouterPricing(prompt=raw.get("prompt"), completion=raw.get("completion"))
    return OpenRouterObservation(identity, pricing)
