"""Bounded, non-generation Ollama lifecycle observation."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable

from .lifecycle import LifecycleProviderId, ProviderRuntimeIdentity, RuntimeKind
from .probe_runner import HttpProbeEndpoint, HttpProbeResult, ProbeEndpointPurpose, ProviderProbeError, run_http_probe
from .registry import RegistryError, find_inventory_model, parse_inventory

HttpProbe = Callable[..., HttpProbeResult]
_ALLOWED_CAPABILITIES = frozenset({"completion", "tools", "thinking", "vision", "embedding"})
_SEMANTIC_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")


def _json(result: HttpProbeResult) -> object:
    if not isinstance(result, HttpProbeResult):
        raise ProviderProbeError("probe_protocol_invalid")
    try:
        return json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ProviderProbeError("probe_protocol_invalid") from None


def observe_ollama(
    *, host: str, port: int, model_tag: str, observed_at: int, policy_version: str,
    timeout_seconds: float = 10.0, allowed_ports: frozenset[int] = frozenset({11434}),
    transport: HttpProbe = run_http_probe, clock: Callable[[], float] = time.monotonic,
) -> ProviderRuntimeIdentity:
    """Observe one exact Ollama tag through version/tags/show metadata only."""
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0.1 <= timeout_seconds <= 30:
        raise ProviderProbeError("probe_request_invalid")
    deadline = clock() + float(timeout_seconds)

    def call(purpose: ProbeEndpointPurpose, *, body: bytes | None = None) -> object:
        remaining = deadline - clock()
        if remaining <= 0:
            raise ProviderProbeError("probe_timeout")
        endpoint = HttpProbeEndpoint(LifecycleProviderId.OLLAMA, "http", host, port, purpose, allowed_ports)
        try:
            result = transport(endpoint=endpoint, timeout_seconds=remaining, request_body=body)
        except ProviderProbeError:
            raise
        except Exception:
            raise ProviderProbeError("probe_failed") from None
        return _json(result)

    version_payload = call(ProbeEndpointPurpose.OLLAMA_VERSION)
    if not isinstance(version_payload, dict) or set(version_payload) != {"version"} or not isinstance(version_payload["version"], str) or _SEMANTIC_VERSION.fullmatch(version_payload["version"]) is None:
        raise ProviderProbeError("probe_version_invalid")
    version = version_payload["version"]
    try:
        inventory = parse_inventory(call(ProbeEndpointPurpose.OLLAMA_TAGS), refreshed_at=observed_at)
        model = find_inventory_model(inventory, model_tag)
    except RegistryError as exc:
        code = "probe_model_unavailable" if exc.code == "model_unavailable" else "probe_protocol_invalid"
        raise ProviderProbeError(code) from None
    show_body = json.dumps({"model": model_tag, "verbose": False}, sort_keys=True, separators=(",", ":")).encode()
    show = call(ProbeEndpointPurpose.OLLAMA_SHOW, body=show_body)
    raw_capabilities = show.get("capabilities") if isinstance(show, dict) else None
    if not isinstance(raw_capabilities, list) or len(raw_capabilities) > 16 or any(not isinstance(item, str) or item not in _ALLOWED_CAPABILITIES for item in raw_capabilities):
        raise ProviderProbeError("probe_protocol_invalid")
    capabilities = tuple(set(raw_capabilities) | {"metadata_show", "metadata_tags", "version"})
    endpoint_identity = f"http://{host}:{port}".encode("ascii", "strict")
    try:
        return ProviderRuntimeIdentity(LifecycleProviderId.OLLAMA, RuntimeKind.LOCAL_HTTP, version, hashlib.sha256(endpoint_identity).hexdigest(), model.digest, None, capabilities, policy_version, observed_at)
    except ValueError:
        raise ProviderProbeError("probe_request_invalid") from None
