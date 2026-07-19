"""Bounded, non-inference OpenRouter lifecycle observation."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping

from .lifecycle import LifecycleProviderId, ProviderRuntimeIdentity, RuntimeKind
from .probe_runner import HttpProbeEndpoint, HttpProbeResult, ProbeEndpointPurpose, ProviderProbeError, run_http_probe

CANONICAL_ENDPOINT = "https://openrouter.ai/api/v1"
API_CONTRACT_VERSION = "1.0.0"
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
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


def observe_openrouter(
    *, endpoint: str, api_key: str, model_id: str, routing_policy: Mapping[str, object],
    observed_at: int, policy_version: str, timeout_seconds: float = 10.0,
    transport: HttpProbe = run_http_probe, clock: Callable[[], float] = time.monotonic,
) -> ProviderRuntimeIdentity:
    """Observe exact OpenRouter auth/model metadata; never invoke inference."""
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
    identifiers = [item.get("id") for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if model_id not in identifiers:
        raise ProviderProbeError("probe_model_unavailable")
    try:
        return ProviderRuntimeIdentity(LifecycleProviderId.OPENROUTER, RuntimeKind.REMOTE_HTTPS, API_CONTRACT_VERSION, hashlib.sha256(endpoint.encode("ascii")).hexdigest(), _digest(model_id), routing_digest, ("credential_health", "models_metadata"), policy_version, observed_at)
    except ValueError:
        raise ProviderProbeError("probe_request_invalid") from None
