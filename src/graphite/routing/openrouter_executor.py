"""Hardened adapter for governed OpenRouter development execution."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from .claude_executor import AdapterError
from .contracts import CliIdentity, Effort, ProviderId
from .edit_apply import (
    EDIT_RESULT_MARKER,
    MAX_EDIT_FILE_BYTES,
    apply_whole_file_edit,
)
from .lifecycle import LifecycleProviderId, ProviderRuntimeIdentity
from .openrouter_probe import (
    CANONICAL_ENDPOINT,
    HttpProbe,
    OpenRouterPricing,
    completion_cost_microunits,
    observe_openrouter_with_pricing,
)
from .probe_runner import (
    MAX_INFERENCE_REQUEST_BYTES,
    MAX_INFERENCE_RESPONSE_BYTES,
    MAX_INFERENCE_TIMEOUT_SECONDS,
    HttpProbeEndpoint,
    HttpProbeResult,
    ProbeEndpointPurpose,
    ProviderProbeError,
    run_http_probe,
)
from .schema_validation import is_supported_schema, matches_schema

ADAPTER_PROTOCOL_VERSION: Final = "1.0.0"
API_CONTRACT_VERSION: Final = "1.0.0"
MAX_TOKEN_COUNT: Final = 10_000_000
MAX_OUTPUT_SCHEMA_BYTES: Final = 65_536
MAX_COST_MICROUNITS: Final = 1_000_000_000
_EXECUTION_EFFORTS: Final = frozenset({Effort.LOW, Effort.MEDIUM, Effort.HIGH})
_PROBE_FAILURE_CODES: Final = {
    "probe_model_unavailable": "model_unavailable",
    "probe_auth_unhealthy": "auth_required",
}
_EXECUTION_TRANSPORT_CODES: Final = {
    "probe_response_limit": "response_limit",
    "probe_timeout": "timeout",
    # A non-2xx / redirect from the completions POST is a provider-side
    # rejection of the request (e.g. an unsupported response_format), NOT a
    # transient network failure -- surface it distinctly so it is not
    # conflated with dns_busy/unavailable/failed under one opaque code.
    "probe_http_status": "http_status",
    "probe_redirect_rejected": "http_status",
}

__all__ = [
    "ADAPTER_PROTOCOL_VERSION",
    "API_CONTRACT_VERSION",
    "CANONICAL_ENDPOINT",
    "EDIT_RESULT_MARKER",
    "MAX_EDIT_FILE_BYTES",
    "OpenRouterExecutionResult",
    "OpenRouterPreflight",
    "apply_whole_file_edit",
    "execute_openrouter",
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


@dataclass(frozen=True, slots=True)
class OpenRouterExecutionResult:
    """Sanitized outcome of one schema-bound OpenRouter execution."""

    effective_model: str
    message: str = field(repr=False)
    input_tokens: int
    output_tokens: int
    cost_microunits: int
    duration_seconds: float
    request_sha256: str
    response_sha256: str


def _model_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(character.isspace() for character in value)
    ):
        raise AdapterError("request_invalid")
    return value


def _token(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKEN_COUNT:
        raise AdapterError("protocol")
    return value


def execute_openrouter(
    *,
    api_key: str,
    prompt: bytes,
    requested_model: str,
    expected_effective_model: str,
    effort: Effort,
    output_schema: dict[str, object],
    output_schema_sha256: str,
    pricing: OpenRouterPricing,
    max_output_tokens: int,
    max_cost_microunits: int,
    timeout_seconds: float,
    response_format_type: str = "json_schema",
    transport: HttpProbe = run_http_probe,
) -> OpenRouterExecutionResult:
    """Perform exactly one bounded, schema-bound chat completion; no retries.

    ``response_format_type`` selects the provider response contract:
    ``"json_schema"`` (default) sends the strict json_schema block;
    ``"json_object"`` sends only ``{"type": "json_object"}``. The output
    schema is canonicalized and its digest pinned in both modes -- json_object
    simply does not forward the schema to the provider as an enforcement
    mechanism. Response parsing (``json.loads`` of the completion content) is
    identical for both, so switching modes changes exactly one variable.
    """
    if not isinstance(api_key, str) or not api_key:
        raise AdapterError("auth_required")
    if len(api_key) > 4096 or any(character in api_key for character in "\r\n\x00"):
        raise AdapterError("request_invalid")
    if (
        not isinstance(prompt, bytes)
        or not prompt
        or len(prompt) > MAX_INFERENCE_REQUEST_BYTES
    ):
        raise AdapterError("request_invalid")
    try:
        prompt_text = prompt.decode("utf-8")
    except UnicodeDecodeError:
        raise AdapterError("request_invalid") from None
    requested = _model_name(requested_model)
    expected = _model_name(expected_effective_model)
    try:
        normalized_effort = Effort(effort)
    except (TypeError, ValueError):
        raise AdapterError("request_invalid") from None
    if normalized_effort not in _EXECUTION_EFFORTS:
        raise AdapterError("request_invalid")
    if response_format_type not in ("json_schema", "json_object"):
        raise AdapterError("request_invalid")
    if not isinstance(output_schema, dict) or not output_schema:
        raise AdapterError("request_invalid")
    if not is_supported_schema(output_schema):
        raise AdapterError("request_invalid")
    try:
        schema_canonical = json.dumps(
            output_schema, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError, RecursionError):
        raise AdapterError("request_invalid") from None
    if len(schema_canonical.encode()) > MAX_OUTPUT_SCHEMA_BYTES:
        raise AdapterError("request_invalid")
    if (
        not isinstance(output_schema_sha256, str)
        or hashlib.sha256(schema_canonical.encode()).hexdigest() != output_schema_sha256
    ):
        raise AdapterError("request_invalid")
    if not isinstance(pricing, OpenRouterPricing):
        raise AdapterError("request_invalid")
    for value, maximum in (
        (max_output_tokens, MAX_TOKEN_COUNT),
        (max_cost_microunits, MAX_COST_MICROUNITS),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise AdapterError("request_invalid")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0.1 <= timeout_seconds <= MAX_INFERENCE_TIMEOUT_SECONDS
    ):
        raise AdapterError("request_invalid")
    if response_format_type == "json_schema":
        response_format: dict[str, object] = {
            "json_schema": {
                "name": "graphite_response",
                "schema": output_schema,
                "strict": True,
            },
            "type": "json_schema",
        }
    else:
        response_format = {"type": "json_object"}
    payload = {
        "max_tokens": max_output_tokens,
        "messages": [{"content": prompt_text, "role": "user"}],
        "model": requested,
        "reasoning": {"effort": normalized_effort.value},
        "response_format": response_format,
        "stream": False,
        "temperature": 0,
        "usage": {"include": True},
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(body) > MAX_INFERENCE_REQUEST_BYTES:
        raise AdapterError("request_invalid")
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.OPENROUTER,
        "https",
        "openrouter.ai",
        443,
        ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS,
    )
    try:
        result = transport(
            endpoint=endpoint,
            timeout_seconds=timeout_seconds,
            request_body=body,
            authorization=f"Bearer {api_key}",
            max_response_bytes=MAX_INFERENCE_RESPONSE_BYTES,
        )
    except ProviderProbeError as error:
        raise AdapterError(
            _EXECUTION_TRANSPORT_CODES.get(error.code, "unavailable")
        ) from None
    except Exception:
        raise AdapterError("unavailable") from None
    if not isinstance(result, HttpProbeResult):
        raise AdapterError("unavailable")
    try:
        envelope = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise AdapterError("unavailable") from None
    if not isinstance(envelope, dict):
        raise AdapterError("protocol")
    reported_model = envelope.get("model")
    if not isinstance(reported_model, str) or not reported_model:
        raise AdapterError("model_identity_unverified")
    if reported_model != expected:
        raise AdapterError("model_mismatch")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise AdapterError("protocol")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise AdapterError("protocol")
    content = message.get("content")
    if not isinstance(content, str):
        raise AdapterError("protocol")
    try:
        structured = json.loads(content)
    except (json.JSONDecodeError, RecursionError):
        raise AdapterError("response_contract_invalid") from None
    if not isinstance(structured, dict):
        raise AdapterError("response_contract_invalid")
    if not matches_schema(structured, output_schema):
        raise AdapterError("response_contract_invalid")
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        raise AdapterError("protocol")
    input_tokens = _token(usage.get("prompt_tokens"))
    output_tokens = _token(usage.get("completion_tokens"))
    try:
        cost = completion_cost_microunits(
            pricing, input_tokens=input_tokens, output_tokens=output_tokens
        )
    except ProviderProbeError:
        raise AdapterError("protocol") from None
    if cost > max_cost_microunits:
        raise AdapterError("cost_ceiling_exceeded")
    return OpenRouterExecutionResult(
        expected,
        json.dumps(structured, sort_keys=True, separators=(",", ":")),
        input_tokens,
        output_tokens,
        cost,
        result.duration_seconds,
        hashlib.sha256(body).hexdigest(),
        hashlib.sha256(result.body).hexdigest(),
    )
