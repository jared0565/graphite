"""Cached, bounded Ollama inventory and exact model capability profiles."""
from __future__ import annotations

import http.client
import json
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Final, Mapping

from .contracts import Effort, ModelProfile, RiskTier
from .effort import EFFORT_PAYLOADS
from .storage import RepositoryStore

REGISTRY_SCHEMA_VERSION: Final = "1"
MAX_INVENTORY_BYTES: Final = 2 * 1024 * 1024
MAX_INVENTORY_MODELS: Final = 128
MAX_HEADER_BYTES: Final = 16 * 1024
MAX_HEADERS: Final = 32
DEFAULT_REGISTRY_TTL_SECONDS: Final = 3_600
OLLAMA_HOSTS: Final = frozenset({"127.0.0.1", "::1"})
OLLAMA_PORT: Final = 11_434

_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}(?::[a-z0-9][a-z0-9._-]{0,63})?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class RegistryError(RuntimeError):
    """A stable, path-free model registry failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class UsageClass(StrEnum):
    MEDIUM = "medium"
    HIGH = "high"


class ModelRole(StrEnum):
    CODING_PRIMARY = "coding_primary"
    CODING = "coding"
    AGENTIC = "agentic"
    REASONING = "reasoning"
    REVIEW = "review"
    LONG_CONTEXT = "long_context"


@dataclass(frozen=True)
class RegistryProfile:
    profile: ModelProfile
    effort_payloads: Mapping[Effort, Mapping[str, Any]]
    evidence_url: str
    evidence_accessed: str
    roles: tuple[ModelRole, ...]
    usage_class: UsageClass
    retirement_date: str | None = None

    def __post_init__(self) -> None:
        try:
            normalized_roles = tuple(ModelRole(role) for role in self.roles)
        except (TypeError, ValueError) as exc:
            raise ValueError("roles_invalid") from exc
        if not normalized_roles:
            raise ValueError("roles_empty")
        if len(set(normalized_roles)) != len(normalized_roles):
            raise ValueError("roles_duplicate")
        try:
            normalized_usage = UsageClass(self.usage_class)
        except (TypeError, ValueError) as exc:
            raise ValueError("usage_class_invalid") from exc
        accessed = _exact_iso_date(self.evidence_accessed, "evidence_accessed_invalid")
        if self.retirement_date is not None:
            retirement = _exact_iso_date(self.retirement_date, "retirement_date_invalid")
            if retirement <= accessed:
                raise ValueError("retirement_date_invalid")
        object.__setattr__(self, "roles", normalized_roles)
        object.__setattr__(self, "usage_class", normalized_usage)


def _exact_iso_date(value: object, code: str) -> date:
    if not isinstance(value, str):
        raise ValueError(code)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(code) from exc
    if parsed.isoformat() != value:
        raise ValueError(code)
    return parsed


BUNDLED_PROFILES: Final[dict[str, RegistryProfile]] = {
    "kimi-k2.7-code:cloud": RegistryProfile(
        profile=ModelProfile(
            model_id="kimi-k2.7-code:cloud",
            profile_version="2026-07-14.2",
            capabilities=("code", "completion", "tools", "thinking", "vision"),
            context_window_tokens=262_144,
            supported_efforts=(Effort.DEFAULT,),
            provisional=True,
        ),
        effort_payloads=EFFORT_PAYLOADS["kimi-k2.7-code:cloud"],
        evidence_url="https://ollama.com/library/kimi-k2.7-code",
        evidence_accessed="2026-07-14",
        roles=(ModelRole.CODING_PRIMARY, ModelRole.CODING),
        usage_class=UsageClass.HIGH,
    ),
    "minimax-m2.7:cloud": RegistryProfile(
        profile=ModelProfile(
            model_id="minimax-m2.7:cloud",
            profile_version="2026-07-14.2",
            capabilities=("code", "completion", "tools", "thinking"),
            context_window_tokens=204_800,
            supported_efforts=(Effort.DEFAULT,),
            provisional=True,
        ),
        effort_payloads=EFFORT_PAYLOADS["minimax-m2.7:cloud"],
        evidence_url="https://ollama.com/library/minimax-m2.7:cloud",
        evidence_accessed="2026-07-14",
        roles=(ModelRole.CODING, ModelRole.AGENTIC),
        usage_class=UsageClass.MEDIUM,
    ),
    "nemotron-3-super:cloud": RegistryProfile(
        profile=ModelProfile(
            model_id="nemotron-3-super:cloud",
            profile_version="2026-07-14.2",
            capabilities=("completion", "reasoning", "tools", "thinking"),
            context_window_tokens=262_144,
            supported_efforts=(Effort.DEFAULT,),
            provisional=True,
        ),
        effort_payloads=EFFORT_PAYLOADS["nemotron-3-super:cloud"],
        evidence_url="https://ollama.com/library/nemotron-3-super:cloud",
        evidence_accessed="2026-07-14",
        roles=(ModelRole.REASONING, ModelRole.REVIEW),
        usage_class=UsageClass.MEDIUM,
    ),
    "minimax-m3:cloud": RegistryProfile(
        profile=ModelProfile(
            model_id="minimax-m3:cloud",
            profile_version="2026-07-14.2",
            capabilities=(
                "architecture",
                "completion",
                "reasoning",
                "tools",
                "thinking",
                "vision",
            ),
            context_window_tokens=524_288,
            supported_efforts=(Effort.DEFAULT,),
            provisional=True,
        ),
        effort_payloads=EFFORT_PAYLOADS["minimax-m3:cloud"],
        evidence_url="https://ollama.com/library/minimax-m3:cloud",
        evidence_accessed="2026-07-14",
        roles=(ModelRole.LONG_CONTEXT, ModelRole.AGENTIC),
        usage_class=UsageClass.HIGH,
    ),
}


@dataclass(frozen=True)
class InventoryModel:
    model_id: str
    digest: str
    context_window_tokens: int
    capabilities: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "digest": self.digest,
            "context_window_tokens": self.context_window_tokens,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class RegistrySnapshot:
    schema_version: str
    refreshed_at: int
    expires_at: int
    models: tuple[InventoryModel, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "refreshed_at": self.refreshed_at,
            "expires_at": self.expires_at,
            "models": [model.to_dict() for model in self.models],
        }

    def to_storage_dict(self) -> dict[str, Any]:
        return self.to_dict()


def _bounded_integer(value: object, code: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise RegistryError(code)
    return value


def _model_from_payload(value: object) -> InventoryModel:
    if not isinstance(value, dict):
        raise RegistryError("registry_model_invalid")
    name = value.get("name")
    model = value.get("model")
    if (
        not isinstance(name, str)
        or not _MODEL_ID.fullmatch(name)
        or model != name
    ):
        raise RegistryError("registry_model_invalid")
    digest = value.get("digest")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise RegistryError("registry_digest_invalid")
    details = value.get("details", {})
    if not isinstance(details, dict):
        raise RegistryError("registry_details_invalid")
    context = details.get("context_length", 0)
    context = _bounded_integer(context, "registry_context_invalid", 0, 4_194_304)
    raw_capabilities = value.get("capabilities", [])
    if not isinstance(raw_capabilities, list) or len(raw_capabilities) > 16:
        raise RegistryError("registry_capabilities_invalid")
    capabilities: list[str] = []
    for capability in raw_capabilities:
        if not isinstance(capability, str) or not _CAPABILITY.fullmatch(capability):
            raise RegistryError("registry_capabilities_invalid")
        capabilities.append(capability)
    return InventoryModel(name, digest, context, tuple(sorted(set(capabilities))))


def parse_inventory(
    payload: object,
    *,
    refreshed_at: int,
    ttl_seconds: int = DEFAULT_REGISTRY_TTL_SECONDS,
) -> RegistrySnapshot:
    """Convert an Ollama tags payload to a sanitized bounded snapshot."""
    refreshed = _bounded_integer(refreshed_at, "registry_time_invalid", 0, 10**12)
    ttl = _bounded_integer(ttl_seconds, "registry_ttl_invalid", 1, 86_400)
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise RegistryError("registry_payload_invalid")
    raw_models = payload["models"]
    if len(raw_models) > MAX_INVENTORY_MODELS:
        raise RegistryError("registry_model_limit")
    models = tuple(sorted((_model_from_payload(item) for item in raw_models), key=lambda item: item.model_id))
    if len({model.model_id for model in models}) != len(models):
        raise RegistryError("registry_model_duplicate")
    return RegistrySnapshot(REGISTRY_SCHEMA_VERSION, refreshed, refreshed + ttl, models)


def _snapshot_from_storage(payload: object) -> RegistrySnapshot:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "refreshed_at",
        "expires_at",
        "models",
    }:
        raise RegistryError("registry_snapshot_invalid")
    if payload["schema_version"] != REGISTRY_SCHEMA_VERSION:
        raise RegistryError("registry_snapshot_version")
    refreshed = _bounded_integer(payload["refreshed_at"], "registry_time_invalid", 0, 10**12)
    expires = _bounded_integer(payload["expires_at"], "registry_time_invalid", 0, 10**12)
    if expires <= refreshed:
        raise RegistryError("registry_time_invalid")
    raw_models = payload["models"]
    if not isinstance(raw_models, list) or len(raw_models) > MAX_INVENTORY_MODELS:
        raise RegistryError("registry_model_limit")
    models: list[InventoryModel] = []
    for value in raw_models:
        if not isinstance(value, dict) or set(value) != {
            "model_id",
            "digest",
            "context_window_tokens",
            "capabilities",
        }:
            raise RegistryError("registry_model_invalid")
        models.append(
            _model_from_payload(
                {
                    "name": value["model_id"],
                    "model": value["model_id"],
                    "digest": value["digest"],
                    "details": {"context_length": value["context_window_tokens"]},
                    "capabilities": value["capabilities"],
                }
            )
        )
    ordered = tuple(sorted(models, key=lambda item: item.model_id))
    if len({model.model_id for model in ordered}) != len(ordered):
        raise RegistryError("registry_model_duplicate")
    return RegistrySnapshot(REGISTRY_SCHEMA_VERSION, refreshed, expires, ordered)


def load_cached_registry(store: RepositoryStore, *, now: int) -> RegistrySnapshot:
    """Load an offline snapshot without opening a socket or starting a process."""
    payload = store.load_registry_snapshot()
    if payload is None:
        raise RegistryError("registry_snapshot_missing")
    snapshot = _snapshot_from_storage(payload)
    current = _bounded_integer(now, "registry_time_invalid", 0, 10**12)
    if current > snapshot.expires_at:
        raise RegistryError("registry_snapshot_expired")
    return snapshot


def _read_response_bounded(response: http.client.HTTPResponse) -> bytes:
    headers = list(response.headers.items())
    if len(headers) > MAX_HEADERS:
        raise RegistryError("registry_headers_limit")
    header_bytes = sum(len(str(name)) + len(str(value)) + 4 for name, value in headers)
    if header_bytes > MAX_HEADER_BYTES:
        raise RegistryError("registry_headers_limit")
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise RegistryError("registry_headers_invalid") from exc
        if declared < 0 or declared > MAX_INVENTORY_BYTES:
            raise RegistryError("registry_body_limit")
    chunks: list[bytes] = []
    remaining = MAX_INVENTORY_BYTES + 1
    while remaining > 0:
        chunk = response.read(min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > MAX_INVENTORY_BYTES:
        raise RegistryError("registry_body_limit")
    return data


def refresh_model_inventory(
    store: RepositoryStore,
    *,
    now: int,
    ttl_seconds: int = DEFAULT_REGISTRY_TTL_SECONDS,
    host: str = "127.0.0.1",
    port: int = OLLAMA_PORT,
    timeout_seconds: float = 5.0,
) -> RegistrySnapshot:
    """Explicitly refresh the local Ollama inventory over canonical loopback."""
    if host not in OLLAMA_HOSTS or port != OLLAMA_PORT:
        raise RegistryError("registry_endpoint_invalid")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0.1 <= timeout_seconds <= 30:
        raise RegistryError("registry_timeout_invalid")
    connection: http.client.HTTPConnection | None = None
    try:
        connection = http.client.HTTPConnection(host, port, timeout=float(timeout_seconds))
        connection.request(
            "GET",
            "/api/tags",
            headers={"Accept": "application/json", "Connection": "close"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise RegistryError("registry_http_status")
        raw = _read_response_bounded(response)
    except RegistryError:
        raise
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise RegistryError("registry_unavailable") from exc
    finally:
        if connection is not None:
            connection.close()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RegistryError("registry_payload_invalid") from exc
    snapshot = parse_inventory(payload, refreshed_at=now, ttl_seconds=ttl_seconds)
    store.save_registry_snapshot(snapshot.to_storage_dict())
    return snapshot


def require_model_available(
    snapshot: RegistrySnapshot,
    model_id: str,
    *,
    expected_digest: str | None = None,
) -> InventoryModel:
    if model_id not in BUNDLED_PROFILES:
        raise RegistryError("model_profile_missing")
    for model in snapshot.models:
        if model.model_id == model_id:
            if expected_digest is not None and model.digest != expected_digest:
                raise RegistryError("model_identity_changed")
            return model
    raise RegistryError("model_unavailable")


def profile_is_eligible(model_id: str, risk: RiskTier | str) -> bool:
    entry = BUNDLED_PROFILES.get(model_id)
    if entry is None:
        return False
    try:
        normalized_risk = RiskTier(risk)
    except (TypeError, ValueError):
        return False
    if entry.profile.provisional and normalized_risk is RiskTier.HIGH:
        return False
    return True
