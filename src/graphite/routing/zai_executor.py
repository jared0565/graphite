"""Isolated hardened adapter for one bounded z.ai chat completion (plain text)."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field

from .claude_executor import AdapterError
from .lifecycle import LifecycleProviderId
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
from .zai_probe import ZAI_HOST, ZaiPricing, zai_cost_microunits

MAX_TOKEN_COUNT = 10_000_000
MAX_COST_MICROUNITS = 1_000_000_000

HttpProbe = Callable[..., HttpProbeResult]

_EXECUTION_TRANSPORT_CODES = {
    "probe_response_limit": "response_limit",
    "probe_timeout": "timeout",
    "probe_http_status": "http_status",
    "probe_redirect_rejected": "http_status",
}


@dataclass(frozen=True, slots=True)
class ZaiExecutionResult:
    """Sanitized outcome of one bounded z.ai plain-text execution."""

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


def execute_zai(
    *,
    api_key: str,
    prompt: bytes,
    requested_model: str,
    expected_effective_model: str,
    pricing: ZaiPricing,
    max_output_tokens: int,
    max_cost_microunits: int,
    timeout_seconds: float,
    transport: HttpProbe = run_http_probe,
) -> ZaiExecutionResult:
    """Perform exactly one bounded z.ai chat completion returning plain text; no retries."""
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
    if not isinstance(pricing, ZaiPricing):
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
    payload = {
        "max_tokens": max_output_tokens,
        "messages": [{"content": prompt_text, "role": "user"}],
        "model": requested,
        "stream": False,
        "temperature": 0,
        # glm-5.2 is a reasoning model; with thinking on it consumes the entire
        # output budget on reasoning_tokens and returns empty content
        # (finish_reason=length), so the bounded plain-text answer is never
        # emitted. Disable thinking so the exact response is produced
        # deterministically within the token budget (z.ai honors this).
        "thinking": {"type": "disabled"},
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(body) > MAX_INFERENCE_REQUEST_BYTES:
        raise AdapterError("request_invalid")
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.ZAI,
        "https",
        ZAI_HOST,
        443,
        ProbeEndpointPurpose.ZAI_CHAT_COMPLETIONS,
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
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        raise AdapterError("protocol")
    input_tokens = _token(usage.get("prompt_tokens"))
    output_tokens = _token(usage.get("completion_tokens"))
    try:
        cost = zai_cost_microunits(pricing, input_tokens=input_tokens, output_tokens=output_tokens)
    except ProviderProbeError:
        raise AdapterError("protocol") from None
    if cost > max_cost_microunits:
        raise AdapterError("cost_ceiling_exceeded")
    return ZaiExecutionResult(
        expected,
        content,
        input_tokens,
        output_tokens,
        cost,
        result.duration_seconds,
        hashlib.sha256(body).hexdigest(),
        hashlib.sha256(result.body).hexdigest(),
    )
