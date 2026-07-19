"""Bounded provider lifecycle observation without graph or execution authority."""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .routing.contracts import Effort
from .routing.lifecycle import (
    LifecycleProviderId,
    LifecycleReasonCode,
    ProviderCompatibilityPolicy,
    ProviderRuntimeIdentity,
    RuntimeKind,
)
from .routing.lifecycle_service import (
    LifecycleServiceError,
    ProviderLifecycleService,
    VerificationManifest,
)
from .routing.probe_runner import ProviderProbeError

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_TARGET_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_PROBE_REASON_CODES = frozenset(
    {
        "probe_auth_unhealthy",
        "probe_capability_missing",
        "probe_dns_busy",
        "probe_endpoint_invalid",
        "probe_executable_invalid",
        "probe_failed",
        "probe_http_status",
        "probe_identity_changed",
        "probe_model_unavailable",
        "probe_protocol_invalid",
        "probe_request_invalid",
        "probe_response_too_large",
        "probe_timeout",
        "probe_unavailable",
        "probe_version_invalid",
    }
)
_PERSISTENCE_REASONS = frozenset(
    {
        "lifecycle_invalidation_failed",
        "lifecycle_observation_invalid",
        "lifecycle_persistence_failed",
        "verification_manifest_invalid",
    }
)

Probe = Callable[[float, int], ProviderRuntimeIdentity]


@dataclass(frozen=True, slots=True)
class VerificationManifestRequest:
    """Exact hash-only inputs for optional non-executing manifest preparation."""

    requested_model: str
    expected_effective_model: str
    effort: Effort
    max_input_tokens: int
    max_output_tokens: int
    timeout_seconds: int
    expires_at: int
    fixture_repository_commit: str
    graph_fingerprint: str
    prompt_contract_hash: str
    response_contract_hash: str
    max_cost_microunits: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderObserverOptions:
    """Hard bounds for a provider observation worker."""

    enabled_providers: tuple[LifecycleProviderId, ...] = tuple(LifecycleProviderId)
    interval_seconds: float = 300.0
    timeout_seconds: float = 15.0
    max_observations_per_cycle: int = 4
    backoff_cap_seconds: float = 3_600.0
    jitter_ratio: float = 0.1

    def validate(self) -> None:
        try:
            providers = tuple(LifecycleProviderId(value) for value in self.enabled_providers)
        except (TypeError, ValueError):
            raise ValueError("observer_enabled_providers_invalid") from None
        if len(providers) > len(LifecycleProviderId) or len(set(providers)) != len(providers):
            raise ValueError("observer_enabled_providers_invalid")
        if not _bounded_number(self.interval_seconds, 1.0, 86_400.0):
            raise ValueError("observer_interval_invalid")
        if not _bounded_number(self.timeout_seconds, 0.1, 30.0):
            raise ValueError("observer_timeout_invalid")
        if (
            isinstance(self.max_observations_per_cycle, bool)
            or not isinstance(self.max_observations_per_cycle, int)
            or not 1 <= self.max_observations_per_cycle <= len(LifecycleProviderId)
        ):
            raise ValueError("observer_cycle_limit_invalid")
        if (
            not _bounded_number(self.backoff_cap_seconds, self.interval_seconds, 86_400.0)
            or not _bounded_number(self.jitter_ratio, 0.0, 0.5)
        ):
            raise ValueError("observer_backoff_invalid")


def _bounded_number(value: object, minimum: float, maximum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and minimum <= float(value) <= maximum
    )


@dataclass(frozen=True, slots=True)
class ProviderObservationTarget:
    """One repository lifecycle boundary and its non-inference probe."""

    target_id: str
    boundary_digest: str
    provider: LifecycleProviderId
    runtime_kind: RuntimeKind
    policy: ProviderCompatibilityPolicy
    lifecycle_service: ProviderLifecycleService
    probe: Probe
    machine_cache_key: str | None = None
    verification_request: VerificationManifestRequest | None = None

    def __post_init__(self) -> None:
        try:
            provider = LifecycleProviderId(self.provider)
            runtime_kind = RuntimeKind(self.runtime_kind)
        except (TypeError, ValueError):
            raise ValueError("observer_target_invalid") from None
        if (
            not isinstance(self.target_id, str)
            or _TARGET_ID.fullmatch(self.target_id) is None
            or not isinstance(self.boundary_digest, str)
            or _HEX_64.fullmatch(self.boundary_digest) is None
            or not isinstance(self.policy, ProviderCompatibilityPolicy)
            or self.policy.provider is not provider
            or self.policy.runtime_kind is not runtime_kind
            or not callable(self.probe)
            or (
                self.verification_request is not None
                and not isinstance(
                    self.verification_request, VerificationManifestRequest
                )
            )
        ):
            raise ValueError("observer_target_invalid")
        if self.machine_cache_key is not None and (
            runtime_kind is not RuntimeKind.LOCAL_CLI
            or not isinstance(self.machine_cache_key, str)
            or _HEX_64.fullmatch(self.machine_cache_key) is None
        ):
            raise ValueError("observer_cache_scope_invalid")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "runtime_kind", runtime_kind)


@dataclass(frozen=True, slots=True)
class ProviderObservationSummary:
    attempted: int
    deferred: int
    succeeded: int
    failed: int
    state_counts: dict[str, int]
    reason_counts: dict[str, int]
    prepared_manifests: tuple[VerificationManifest, ...] = ()

    def to_status(self) -> dict[str, object]:
        """Return the bounded aggregate safe for daemon status and health."""
        return {
            "attempted": self.attempted,
            "deferred": self.deferred,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "state_counts": dict(self.state_counts),
            "reason_counts": dict(self.reason_counts),
        }


@dataclass(slots=True)
class _Schedule:
    next_due: float = 0.0
    consecutive_failures: int = 0


class ProviderObserver:
    """Observe due providers once and persist only lifecycle transitions."""

    def __init__(self, options: ProviderObserverOptions | None = None) -> None:
        self.options = options or ProviderObserverOptions()
        self.options.validate()
        self._enabled = frozenset(
            LifecycleProviderId(value) for value in self.options.enabled_providers
        )
        self._schedules: dict[str, _Schedule] = {}

    def run_cycle(
        self,
        targets: Iterable[ProviderObservationTarget],
        *,
        now: float,
    ) -> ProviderObservationSummary:
        if not _bounded_number(now, 0.0, 10**12):
            raise ValueError("observer_time_invalid")
        normalized = tuple(targets)
        if len(normalized) > 1_000 or any(
            not isinstance(target, ProviderObservationTarget) for target in normalized
        ):
            raise ValueError("observer_targets_invalid")
        identifiers = [target.target_id for target in normalized]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("observer_targets_invalid")
        current_identifiers = set(identifiers)
        for target_id in tuple(self._schedules):
            if target_id not in current_identifiers:
                del self._schedules[target_id]

        enabled = [target for target in normalized if target.provider in self._enabled]
        due = sorted(
            (
                target
                for target in enabled
                if self._schedules.setdefault(target.target_id, _Schedule()).next_due
                <= float(now)
            ),
            key=lambda target: target.target_id,
        )
        selected = due[: self.options.max_observations_per_cycle]
        cache: dict[str, ProviderRuntimeIdentity | ProviderProbeError] = {}
        states: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        succeeded = 0
        prepared_manifests: list[VerificationManifest] = []

        for target in selected:
            schedule = self._schedules[target.target_id]
            try:
                identity = self._probe(target, int(now), cache)
                result = target.lifecycle_service.observe(
                    boundary_digest=target.boundary_digest,
                    identity=identity,
                    policy=target.policy,
                )
                if target.verification_request is not None:
                    if result.state.value != "verification_required":
                        raise LifecycleServiceError("verification_manifest_invalid")
                    request = target.verification_request
                    prepared_manifests.append(
                        target.lifecycle_service.prepare_verification_manifest(
                            boundary_digest=target.boundary_digest,
                            requested_model=request.requested_model,
                            expected_effective_model=request.expected_effective_model,
                            effort=request.effort,
                            max_input_tokens=request.max_input_tokens,
                            max_output_tokens=request.max_output_tokens,
                            timeout_seconds=request.timeout_seconds,
                            expires_at=request.expires_at,
                            fixture_repository_commit=request.fixture_repository_commit,
                            graph_fingerprint=request.graph_fingerprint,
                            prompt_contract_hash=request.prompt_contract_hash,
                            response_contract_hash=request.response_contract_hash,
                            max_cost_microunits=request.max_cost_microunits,
                        )
                    )
            except ProviderProbeError as exc:
                reason = self._record_unavailable(target, int(now), exc)
                states["unavailable"] += 1
                reasons[reason] += 1
                self._back_off(target.target_id, schedule, float(now))
            except LifecycleServiceError as exc:
                reasons[_lifecycle_reason(exc)] += 1
                self._back_off(target.target_id, schedule, float(now))
            except Exception:
                reasons["lifecycle_persistence_failed"] += 1
                self._back_off(target.target_id, schedule, float(now))
            else:
                states[result.state.value] += 1
                reasons[result.reason.value] += 1
                succeeded += 1
                schedule.consecutive_failures = 0
                schedule.next_due = float(now) + self._jittered_delay(
                    target.target_id, self.options.interval_seconds, 0
                )

        attempted = len(selected)
        return ProviderObservationSummary(
            attempted=attempted,
            deferred=max(0, len(due) - attempted),
            succeeded=succeeded,
            failed=attempted - succeeded,
            state_counts=dict(sorted(states.items())),
            reason_counts=dict(sorted(reasons.items())),
            prepared_manifests=tuple(prepared_manifests),
        )

    def _probe(
        self,
        target: ProviderObservationTarget,
        observed_at: int,
        cache: dict[str, ProviderRuntimeIdentity | ProviderProbeError],
    ) -> ProviderRuntimeIdentity:
        cache_key = target.machine_cache_key
        if cache_key is not None and cache_key in cache:
            cached = cache[cache_key]
            if isinstance(cached, ProviderProbeError):
                raise cached
            identity = cached
        else:
            try:
                identity = target.probe(float(self.options.timeout_seconds), observed_at)
            except ProviderProbeError as exc:
                sanitized = ProviderProbeError(_probe_reason(exc.code))
                if cache_key is not None:
                    cache[cache_key] = sanitized
                raise sanitized from None
            except Exception:
                sanitized = ProviderProbeError("probe_failed")
                if cache_key is not None:
                    cache[cache_key] = sanitized
                raise sanitized from None
            if cache_key is not None:
                cache[cache_key] = identity
        if (
            not isinstance(identity, ProviderRuntimeIdentity)
            or identity.provider is not target.provider
            or identity.runtime_kind is not target.runtime_kind
            or identity.policy_version != target.policy.policy_version
            or identity.observed_at != observed_at
        ):
            raise ProviderProbeError("probe_protocol_invalid")
        return identity

    def _record_unavailable(
        self,
        target: ProviderObservationTarget,
        observed_at: int,
        error: ProviderProbeError,
    ) -> str:
        probe_reason = _probe_reason(error.code)
        lifecycle_reason = (
            LifecycleReasonCode.CREDENTIAL_UNHEALTHY
            if probe_reason == "probe_auth_unhealthy"
            else LifecycleReasonCode.RUNTIME_MISSING
        )
        try:
            target.lifecycle_service.mark_unavailable(
                boundary_digest=target.boundary_digest,
                provider=target.provider,
                runtime_kind=target.runtime_kind,
                policy_version=target.policy.policy_version,
                observed_at=observed_at,
                reason=lifecycle_reason,
            )
        except LifecycleServiceError as exc:
            return _lifecycle_reason(exc)
        if lifecycle_reason is LifecycleReasonCode.CREDENTIAL_UNHEALTHY:
            return lifecycle_reason.value
        if probe_reason in {"probe_timeout", "probe_dns_busy"}:
            return probe_reason
        return lifecycle_reason.value if probe_reason == "probe_executable_invalid" else probe_reason

    def _back_off(self, target_id: str, schedule: _Schedule, now: float) -> None:
        schedule.consecutive_failures = min(schedule.consecutive_failures + 1, 30)
        delay = min(
            self.options.backoff_cap_seconds,
            self.options.interval_seconds * (2 ** schedule.consecutive_failures),
        )
        schedule.next_due = now + self._jittered_delay(
            target_id, delay, schedule.consecutive_failures
        )

    def _jittered_delay(self, target_id: str, delay: float, attempt: int) -> float:
        if self.options.jitter_ratio == 0:
            return float(delay)
        digest = hashlib.sha256(f"{target_id}:{attempt}".encode("ascii")).digest()
        unit = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)
        factor = 1.0 + ((unit * 2.0) - 1.0) * self.options.jitter_ratio
        return max(0.1, float(delay) * factor)


def _probe_reason(code: object) -> str:
    return code if isinstance(code, str) and code in _PROBE_REASON_CODES else "probe_failed"


def _lifecycle_reason(error: LifecycleServiceError) -> str:
    code = str(error)
    return code if code in _PERSISTENCE_REASONS else "lifecycle_persistence_failed"
