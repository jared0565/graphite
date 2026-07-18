"""Conservative environment-backed settings for development routing."""
from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass
from typing import Mapping


class RoutingSettingsError(ValueError):
    """A stable routing settings validation failure."""


def _integer(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env.get(f"GRAPHITE_ROUTE_{name.upper()}")
    if raw is None:
        return default
    try:
        value = int(raw, 10)
    except (TypeError, ValueError) as exc:
        raise RoutingSettingsError(f"{name}_invalid") from exc
    if value < minimum or value > maximum:
        raise RoutingSettingsError(f"{name}_invalid")
    return value


def _float(env: Mapping[str, str], name: str, default: float, minimum: float, maximum: float) -> float:
    raw = env.get(f"GRAPHITE_ROUTE_{name.upper()}")
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RoutingSettingsError(f"{name}_invalid") from exc
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise RoutingSettingsError(f"{name}_invalid")
    return value


def _boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(f"GRAPHITE_ROUTE_{name.upper()}")
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RoutingSettingsError(f"{name}_invalid")


@dataclass(frozen=True)
class RoutingSettings:
    max_context_bytes: int = 262_144
    max_context_files: int = 32
    max_input_tokens: int = 32_768
    max_output_tokens: int = 4_096
    request_timeout_seconds: float = 180.0
    max_concurrency: int = 1
    repository_quota_tokens: int = 2_000_000
    machine_quota_tokens: int = 10_000_000
    shadow_rate_percent: int = 0
    shadow_quota_tokens: int = 200_000
    approval_ttl_seconds: int = 300
    retention_days: int = 90
    aggregate_opt_in: bool = False
    max_changed_files: int = 64
    max_changed_bytes: int = 1_048_576

    @classmethod
    def from_env(cls, overrides: Mapping[str, str] | None = None) -> "RoutingSettings":
        env = dict(os.environ)
        if overrides:
            env.update(overrides)
        settings = cls(
            max_context_bytes=_integer(env, "max_context_bytes", 262_144, 16_384, 4_194_304),
            max_context_files=_integer(env, "max_context_files", 32, 1, 128),
            max_input_tokens=_integer(env, "max_input_tokens", 32_768, 256, 262_144),
            max_output_tokens=_integer(env, "max_output_tokens", 4_096, 1, 32_768),
            request_timeout_seconds=_float(env, "request_timeout_seconds", 180.0, 5.0, 600.0),
            max_concurrency=_integer(env, "max_concurrency", 1, 1, 1),
            repository_quota_tokens=_integer(env, "repository_quota_tokens", 2_000_000, 10_000, 100_000_000),
            machine_quota_tokens=_integer(env, "machine_quota_tokens", 10_000_000, 10_000, 1_000_000_000),
            shadow_rate_percent=_integer(env, "shadow_rate_percent", 0, 0, 10),
            shadow_quota_tokens=_integer(env, "shadow_quota_tokens", 200_000, 0, 10_000_000),
            approval_ttl_seconds=_integer(env, "approval_ttl_seconds", 300, 30, 3_600),
            retention_days=_integer(env, "retention_days", 90, 1, 3_650),
            aggregate_opt_in=_boolean(env, "aggregate_opt_in", False),
            max_changed_files=_integer(env, "max_changed_files", 64, 1, 10_000),
            max_changed_bytes=_integer(
                env, "max_changed_bytes", 1_048_576, 1_024, 104_857_600
            ),
        )
        if settings.machine_quota_tokens < settings.repository_quota_tokens:
            raise RoutingSettingsError("machine_quota_tokens_invalid")
        return settings

    def to_dict(self) -> dict[str, int | float | bool]:
        return asdict(self)
