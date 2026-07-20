"""Hardened adapter for governed OpenRouter development execution."""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .claude_executor import AdapterError
from .contracts import CliIdentity, Effort, ProviderId
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

ADAPTER_PROTOCOL_VERSION: Final = "1.0.0"
API_CONTRACT_VERSION: Final = "1.0.0"
MAX_TOKEN_COUNT: Final = 10_000_000
MAX_OUTPUT_SCHEMA_BYTES: Final = 65_536
MAX_COST_MICROUNITS: Final = 1_000_000_000
MAX_EDIT_FILE_BYTES: Final = 1_048_576
MAX_EDIT_TOTAL_BYTES: Final = 1_073_741_824
MAX_EDIT_PATH_LENGTH: Final = 512
MAX_EDIT_SCOPE_FILES: Final = 1_000
EDIT_RESULT_MARKER: Final = "GRAPHITE_EDIT_OK"
_TEMP_SUFFIX: Final = ".graphite-tmp"
_REPARSE_POINT: Final = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_EXECUTION_EFFORTS: Final = frozenset({Effort.LOW, Effort.MEDIUM, Effort.HIGH})
_PROBE_FAILURE_CODES: Final = {
    "probe_model_unavailable": "model_unavailable",
    "probe_auth_unhealthy": "auth_required",
}
_EXECUTION_TRANSPORT_CODES: Final = {
    "probe_response_limit": "response_limit",
    "probe_timeout": "timeout",
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
    transport: HttpProbe = run_http_probe,
) -> OpenRouterExecutionResult:
    """Perform exactly one bounded, schema-bound chat completion; no retries."""
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
    if not isinstance(output_schema, dict) or not output_schema:
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
    payload = {
        "max_tokens": max_output_tokens,
        "messages": [{"content": prompt_text, "role": "user"}],
        "model": requested,
        "reasoning": {"effort": normalized_effort.value},
        "response_format": {
            "json_schema": {
                "name": "graphite_response",
                "schema": output_schema,
                "strict": True,
            },
            "type": "json_schema",
        },
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
    if reported_model is not None:
        if not isinstance(reported_model, str):
            raise AdapterError("protocol")
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


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attributes & _REPARSE_POINT)


def _validate_edit_path(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_EDIT_PATH_LENGTH
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or value.startswith("/")
        or value.endswith(_TEMP_SUFFIX)
    ):
        raise AdapterError("edit_scope_violation")
    segments = tuple(value.split("/"))
    if any(segment in ("", ".", "..") for segment in segments):
        raise AdapterError("edit_scope_violation")
    return segments


def _cleanup_temps(temps: list[tuple[Path, Path]]) -> None:
    for temp, _target in temps:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def apply_whole_file_edit(
    *,
    workspace: Path,
    payload: object,
    edit_scope: tuple[str, ...],
    max_total_bytes: int,
) -> tuple[str, ...]:
    """Validate a whole-file payload completely, then apply it atomically.

    A validation failure leaves the workspace byte-identical; a mid-set
    replace failure restores every already-replaced file from held originals.
    """
    if not isinstance(workspace, Path):
        raise AdapterError("edit_scope_violation")
    try:
        workspace_root = workspace.resolve(strict=True)
    except OSError:
        raise AdapterError("edit_scope_violation") from None
    if not workspace_root.is_dir() or workspace_root.is_symlink():
        raise AdapterError("edit_scope_violation")
    if (
        not isinstance(edit_scope, tuple)
        or not edit_scope
        or len(edit_scope) > MAX_EDIT_SCOPE_FILES
        or len(set(edit_scope)) != len(edit_scope)
    ):
        raise AdapterError("edit_scope_violation")
    if (
        isinstance(max_total_bytes, bool)
        or not isinstance(max_total_bytes, int)
        or not 1 <= max_total_bytes <= MAX_EDIT_TOTAL_BYTES
    ):
        raise AdapterError("edit_scope_violation")
    if not isinstance(payload, dict) or set(payload) != {"files", "result"}:
        raise AdapterError("edit_scope_violation")
    if payload["result"] != EDIT_RESULT_MARKER:
        raise AdapterError("edit_scope_violation")
    files = payload["files"]
    if not isinstance(files, list) or len(files) != len(edit_scope):
        raise AdapterError("edit_scope_violation")
    planned: dict[str, tuple[Path, tuple[str, ...], bytes]] = {}
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"content", "path"}:
            raise AdapterError("edit_scope_violation")
        raw_path = item["path"]
        segments = _validate_edit_path(raw_path)
        if raw_path in planned:
            raise AdapterError("edit_scope_violation")
        content = item["content"]
        if not isinstance(content, str):
            raise AdapterError("edit_scope_violation")
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError:
            raise AdapterError("edit_scope_violation") from None
        if len(encoded) > MAX_EDIT_FILE_BYTES:
            raise AdapterError("edit_scope_violation")
        total += len(encoded)
        target = workspace_root
        for segment in segments:
            target = target / segment
        planned[raw_path] = (target, segments, encoded)
    if total > max_total_bytes or set(planned) != set(edit_scope):
        raise AdapterError("edit_scope_violation")
    originals: dict[Path, bytes] = {}
    for raw_path in sorted(planned):
        target, segments, _encoded = planned[raw_path]
        component = workspace_root
        for segment in segments:
            component = component / segment
            if component.is_symlink() or _is_reparse_point(component):
                raise AdapterError("edit_scope_violation")
            if not component.exists():
                raise AdapterError("edit_scope_violation")
        if not target.is_file():
            raise AdapterError("edit_scope_violation")
        if target.with_name(target.name + _TEMP_SUFFIX).exists():
            raise AdapterError("edit_scope_violation")
        try:
            originals[target] = target.read_bytes()
        except OSError:
            raise AdapterError("edit_apply_failed") from None
    temps: list[tuple[Path, Path]] = []
    try:
        for raw_path in sorted(planned):
            target, _segments, encoded = planned[raw_path]
            temp = target.with_name(target.name + _TEMP_SUFFIX)
            temps.append((temp, target))
            temp.write_bytes(encoded)
    except OSError:
        _cleanup_temps(temps)
        raise AdapterError("edit_apply_failed") from None
    replaced: list[Path] = []
    try:
        for temp, target in temps:
            os.replace(temp, target)
            replaced.append(target)
    except OSError:
        for target in replaced:
            try:
                target.write_bytes(originals[target])
            except OSError:
                pass
        _cleanup_temps(temps)
        raise AdapterError("edit_apply_failed") from None
    return tuple(sorted(planned))
