"""Immutable provider-neutral route-pool approval and selection authority."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Final

from .contracts import Effort, PermissionMode, PublicRecord, RiskTier
from .lifecycle import (
    LifecycleProviderId,
    ProviderLifecycleState,
    RuntimeKind,
)

MAX_ROUTE_CANDIDATES: Final = 2
MAX_ROUTE_CAPABILITIES: Final = 32
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SAFE_CAPABILITY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_RISK_ORDER = {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2}
_RUNTIME_BY_PROVIDER = {
    LifecycleProviderId.CLAUDE_CODE: RuntimeKind.LOCAL_CLI,
    LifecycleProviderId.CODEX: RuntimeKind.LOCAL_CLI,
    LifecycleProviderId.OLLAMA: RuntimeKind.LOCAL_HTTP,
    LifecycleProviderId.OPENROUTER: RuntimeKind.REMOTE_HTTPS,
}
_FAILURE_CATEGORIES = frozenset(
    {
        "capacity_unavailable",
        "provider_process_failure",
        "provider_unavailable",
        "provider_protocol",
        "timeout",
        "cancelled",
    }
)


class RoutePoolError(RuntimeError):
    """Stable route-pool rejection containing no provider diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SideEffectState(StrEnum):
    NONE = "none"
    OBSERVED = "observed"
    UNKNOWN = "unknown"


def _identifier(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or "\x00" in value
        or any(character.isspace() for character in value)
    ):
        raise RoutePoolError(code)
    return value


def _digest(value: object, code: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _HEX_64.fullmatch(value) is None:
        raise RoutePoolError(code)
    return value


def _integer(
    value: object,
    code: str,
    *,
    minimum: int = 0,
    maximum: int = 10**12,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise RoutePoolError(code)
    return value


def _capabilities(value: object, code: str) -> tuple[str, ...]:
    if (
        not isinstance(value, (tuple, list))
        or not value
        or len(value) > MAX_ROUTE_CAPABILITIES
    ):
        raise RoutePoolError(code)
    normalized = tuple(value)
    if any(
        not isinstance(item, str) or _SAFE_CAPABILITY.fullmatch(item) is None
        for item in normalized
    ) or len(set(normalized)) != len(normalized):
        raise RoutePoolError(code)
    return tuple(sorted(normalized))


def _canonical_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovedRouteCandidate(PublicRecord):
    candidate_id: str
    provider: LifecycleProviderId
    runtime_kind: RuntimeKind
    lifecycle_identity_digest: str
    capability_snapshot_digest: str
    model_identity_digest: str
    routing_policy_digest: str | None
    requested_model: str
    effective_model: str
    effort: Effort
    permission_mode: PermissionMode
    risk_ceiling: RiskTier
    trust_policy_digest: str
    capabilities: tuple[str, ...]
    context_window_tokens: int
    snapshot_expires_at: int

    _public_fields: ClassVar[tuple[str, ...]] = (
        "candidate_id",
        "provider",
        "runtime_kind",
        "lifecycle_identity_digest",
        "capability_snapshot_digest",
        "model_identity_digest",
        "routing_policy_digest",
        "requested_model",
        "effective_model",
        "effort",
        "permission_mode",
        "risk_ceiling",
        "trust_policy_digest",
        "capabilities",
        "context_window_tokens",
        "snapshot_expires_at",
    )

    def __post_init__(self) -> None:
        try:
            provider = LifecycleProviderId(self.provider)
            runtime = RuntimeKind(self.runtime_kind)
            effort = Effort(self.effort)
            permission = PermissionMode(self.permission_mode)
            risk = RiskTier(self.risk_ceiling)
        except (TypeError, ValueError):
            raise RoutePoolError("route_candidate_invalid") from None
        routing_digest = _digest(
            self.routing_policy_digest,
            "route_candidate_invalid",
            optional=True,
        )
        if (
            _RUNTIME_BY_PROVIDER.get(provider) is not runtime
            or (provider is LifecycleProviderId.OPENROUTER) is (routing_digest is None)
        ):
            raise RoutePoolError("route_candidate_invalid")
        object.__setattr__(self, "candidate_id", _identifier(self.candidate_id, "route_candidate_invalid"))
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "runtime_kind", runtime)
        object.__setattr__(self, "effort", effort)
        object.__setattr__(self, "permission_mode", permission)
        object.__setattr__(self, "risk_ceiling", risk)
        for field_name in (
            "lifecycle_identity_digest",
            "capability_snapshot_digest",
            "model_identity_digest",
            "trust_policy_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _digest(getattr(self, field_name), "route_candidate_invalid"),
            )
        object.__setattr__(self, "routing_policy_digest", routing_digest)
        for field_name in ("requested_model", "effective_model"):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), "route_candidate_invalid"),
            )
        object.__setattr__(
            self,
            "capabilities",
            _capabilities(self.capabilities, "route_candidate_invalid"),
        )
        object.__setattr__(
            self,
            "context_window_tokens",
            _integer(
                self.context_window_tokens,
                "route_candidate_invalid",
                minimum=1,
                maximum=10_000_000,
            ),
        )
        object.__setattr__(
            self,
            "snapshot_expires_at",
            _integer(self.snapshot_expires_at, "route_candidate_invalid"),
        )

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class ApprovedRoutePool(PublicRecord):
    approval_id: str
    task_id: str
    decision_id: str
    candidates: tuple[ApprovedRouteCandidate, ...]
    required_capabilities: tuple[str, ...]
    task_risk: RiskTier
    permission_mode: PermissionMode
    trust_policy_digest: str
    graph_fingerprint: str
    context_manifest_hash: str
    repository_commit: str
    worktree_id: str
    allow_cross_provider: bool
    allowed_fallback_reasons: tuple[str, ...]
    max_attempts: int
    max_input_tokens: int
    max_output_tokens: int
    max_duration_ms: int
    max_cost_microunits: int | None
    policy_version: str
    issued_at: int
    expires_at: int
    nonce: str

    _public_fields: ClassVar[tuple[str, ...]] = (
        "approval_id",
        "task_id",
        "decision_id",
        "candidates",
        "required_capabilities",
        "task_risk",
        "permission_mode",
        "trust_policy_digest",
        "graph_fingerprint",
        "context_manifest_hash",
        "repository_commit",
        "worktree_id",
        "allow_cross_provider",
        "allowed_fallback_reasons",
        "max_attempts",
        "max_input_tokens",
        "max_output_tokens",
        "max_duration_ms",
        "max_cost_microunits",
        "policy_version",
        "issued_at",
        "expires_at",
        "nonce",
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.candidates, (tuple, list))
            or not 1 <= len(self.candidates) <= MAX_ROUTE_CANDIDATES
            or any(not isinstance(item, ApprovedRouteCandidate) for item in self.candidates)
        ):
            raise RoutePoolError("route_candidates_invalid")
        candidates = tuple(self.candidates)
        if len({item.candidate_id for item in candidates}) != len(candidates) or len(
            {item.digest for item in candidates}
        ) != len(candidates):
            raise RoutePoolError("route_candidates_invalid")
        try:
            risk = RiskTier(self.task_risk)
            permission = PermissionMode(self.permission_mode)
        except (TypeError, ValueError):
            raise RoutePoolError("route_pool_invalid") from None
        required = _capabilities(self.required_capabilities, "route_pool_invalid")
        if any(
            not set(required).issubset(candidate.capabilities)
            or candidate.permission_mode is not permission
            or _RISK_ORDER[candidate.risk_ceiling] < _RISK_ORDER[risk]
            or candidate.trust_policy_digest != self.trust_policy_digest
            for candidate in candidates
        ):
            raise RoutePoolError("route_pool_invalid")
        if not isinstance(self.allow_cross_provider, bool):
            raise RoutePoolError("route_pool_invalid")
        reasons = tuple(self.allowed_fallback_reasons)
        expected_reasons = ("capacity_unavailable",) if len(candidates) == 2 else ()
        if reasons != expected_reasons:
            raise RoutePoolError("route_pool_invalid")
        attempts = _integer(self.max_attempts, "route_pool_invalid", minimum=1, maximum=2)
        if attempts != len(candidates):
            raise RoutePoolError("route_pool_invalid")
        if (
            len({item.provider for item in candidates}) > 1
            and not self.allow_cross_provider
            and len(candidates) == 1
        ):
            raise RoutePoolError("route_pool_invalid")
        for field_name in (
            "approval_id",
            "task_id",
            "decision_id",
            "worktree_id",
            "policy_version",
            "nonce",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), "route_pool_invalid"),
            )
        for field_name in (
            "trust_policy_digest",
            "graph_fingerprint",
            "context_manifest_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _digest(getattr(self, field_name), "route_pool_invalid"),
            )
        if not isinstance(self.repository_commit, str) or _GIT_OBJECT.fullmatch(
            self.repository_commit
        ) is None:
            raise RoutePoolError("route_pool_invalid")
        issued = _integer(self.issued_at, "route_pool_invalid")
        expires = _integer(self.expires_at, "route_pool_invalid")
        if expires <= issued:
            raise RoutePoolError("route_pool_invalid")
        cost = self.max_cost_microunits
        if cost is not None:
            cost = _integer(cost, "route_pool_invalid", minimum=1)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "required_capabilities", required)
        object.__setattr__(self, "task_risk", risk)
        object.__setattr__(self, "permission_mode", permission)
        object.__setattr__(self, "allowed_fallback_reasons", reasons)
        object.__setattr__(self, "max_attempts", attempts)
        max_input_tokens = _integer(
            self.max_input_tokens,
            "route_pool_invalid",
            minimum=1,
            maximum=262_144,
        )
        max_output_tokens = _integer(
            self.max_output_tokens,
            "route_pool_invalid",
            minimum=1,
            maximum=32_768,
        )
        max_duration_ms = _integer(
            self.max_duration_ms,
            "route_pool_invalid",
            minimum=1,
            maximum=86_400_000,
        )
        if any(
            max_input_tokens + max_output_tokens > candidate.context_window_tokens
            or candidate.snapshot_expires_at < expires
            for candidate in candidates
        ):
            raise RoutePoolError("route_pool_invalid")
        object.__setattr__(
            self,
            "max_input_tokens",
            max_input_tokens,
        )
        object.__setattr__(
            self,
            "max_output_tokens",
            max_output_tokens,
        )
        object.__setattr__(
            self,
            "max_duration_ms",
            max_duration_ms,
        )
        object.__setattr__(self, "max_cost_microunits", cost)

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["candidates"] = [candidate.to_dict() for candidate in self.candidates]
        return payload

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class RouteAuthority(PublicRecord):
    candidate_id: str
    provider: LifecycleProviderId
    runtime_kind: RuntimeKind
    lifecycle_identity_digest: str
    capability_snapshot_digest: str
    state: ProviderLifecycleState

    _public_fields: ClassVar[tuple[str, ...]] = (
        "candidate_id",
        "provider",
        "runtime_kind",
        "lifecycle_identity_digest",
        "capability_snapshot_digest",
        "state",
    )

    def __post_init__(self) -> None:
        try:
            provider = LifecycleProviderId(self.provider)
            runtime = RuntimeKind(self.runtime_kind)
            state = ProviderLifecycleState(self.state)
        except (TypeError, ValueError):
            raise RoutePoolError("route_authority_invalid") from None
        if _RUNTIME_BY_PROVIDER.get(provider) is not runtime:
            raise RoutePoolError("route_authority_invalid")
        object.__setattr__(self, "candidate_id", _identifier(self.candidate_id, "route_authority_invalid"))
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "runtime_kind", runtime)
        object.__setattr__(self, "state", state)
        for field_name in ("lifecycle_identity_digest", "capability_snapshot_digest"):
            object.__setattr__(
                self,
                field_name,
                _digest(getattr(self, field_name), "route_authority_invalid"),
            )


@dataclass(frozen=True, slots=True)
class RouteAttemptEvidence(PublicRecord):
    candidate_id: str
    candidate_digest: str
    attempt_ordinal: int
    failure_category: str
    accepted_output: bool
    side_effect_state: SideEffectState
    input_tokens: int
    output_tokens: int
    duration_ms: int
    cost_microunits: int | None

    _public_fields: ClassVar[tuple[str, ...]] = (
        "candidate_id",
        "candidate_digest",
        "attempt_ordinal",
        "failure_category",
        "accepted_output",
        "side_effect_state",
        "input_tokens",
        "output_tokens",
        "duration_ms",
        "cost_microunits",
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.failure_category, str)
            or self.failure_category not in _FAILURE_CATEGORIES
            or not isinstance(self.accepted_output, bool)
        ):
            raise RoutePoolError("route_attempt_invalid")
        try:
            side_effects = SideEffectState(self.side_effect_state)
        except (TypeError, ValueError):
            raise RoutePoolError("route_attempt_invalid") from None
        object.__setattr__(self, "candidate_id", _identifier(self.candidate_id, "route_attempt_invalid"))
        object.__setattr__(
            self,
            "candidate_digest",
            _digest(self.candidate_digest, "route_attempt_invalid"),
        )
        object.__setattr__(
            self,
            "attempt_ordinal",
            _integer(self.attempt_ordinal, "route_attempt_invalid", minimum=1, maximum=2),
        )
        object.__setattr__(self, "side_effect_state", side_effects)
        for field_name in ("input_tokens", "output_tokens", "duration_ms"):
            object.__setattr__(
                self,
                field_name,
                _integer(getattr(self, field_name), "route_attempt_invalid"),
            )
        cost = self.cost_microunits
        if cost is not None:
            cost = _integer(cost, "route_attempt_invalid")
        object.__setattr__(self, "cost_microunits", cost)


@dataclass(frozen=True, slots=True)
class RouteSelection:
    candidate: ApprovedRouteCandidate
    attempt_ordinal: int
    remaining_input_tokens: int
    remaining_output_tokens: int
    remaining_duration_ms: int
    remaining_cost_microunits: int | None


def _validate_authority(
    candidate: ApprovedRouteCandidate,
    authority: RouteAuthority,
    *,
    now: int,
) -> None:
    if authority.state is not ProviderLifecycleState.ACTIVE:
        raise RoutePoolError("route_inactive")
    if (
        authority.provider is not candidate.provider
        or authority.runtime_kind is not candidate.runtime_kind
        or authority.candidate_id != candidate.candidate_id
    ):
        raise RoutePoolError("route_authority_invalid")
    if authority.lifecycle_identity_digest != candidate.lifecycle_identity_digest:
        raise RoutePoolError("route_identity_changed")
    if authority.capability_snapshot_digest != candidate.capability_snapshot_digest:
        raise RoutePoolError("route_snapshot_changed")
    if candidate.snapshot_expires_at <= now:
        raise RoutePoolError("route_snapshot_expired")


def select_route(
    pool: ApprovedRoutePool,
    authorities: tuple[RouteAuthority, ...],
    attempts: tuple[RouteAttemptEvidence, ...],
    *,
    now: int,
) -> RouteSelection:
    """Select one exact approved candidate; never discover or substitute routes."""
    if not isinstance(pool, ApprovedRoutePool):
        raise RoutePoolError("route_pool_invalid")
    current_time = _integer(now, "route_pool_time_invalid")
    if current_time >= pool.expires_at:
        raise RoutePoolError("route_pool_expired")
    if (
        not isinstance(authorities, tuple)
        or len(authorities) != len(pool.candidates)
        or any(not isinstance(item, RouteAuthority) for item in authorities)
        or tuple(item.candidate_id for item in authorities)
        != tuple(item.candidate_id for item in pool.candidates)
    ):
        raise RoutePoolError("route_authority_invalid")
    if (
        not isinstance(attempts, tuple)
        or len(attempts) >= pool.max_attempts
        or any(not isinstance(item, RouteAttemptEvidence) for item in attempts)
    ):
        raise RoutePoolError("route_pool_attempts_exhausted")
    ordinal = len(attempts) + 1
    candidate = pool.candidates[len(attempts)]
    _validate_authority(candidate, authorities[len(attempts)], now=current_time)
    used_input = sum(item.input_tokens for item in attempts)
    used_output = sum(item.output_tokens for item in attempts)
    used_duration = sum(item.duration_ms for item in attempts)
    costs = tuple(item.cost_microunits for item in attempts)
    if pool.max_cost_microunits is None:
        if any(value is not None for value in costs):
            raise RoutePoolError("route_pool_cost_invalid")
        used_cost: int | None = None
    else:
        if any(value is None for value in costs):
            raise RoutePoolError("route_pool_cost_invalid")
        used_cost = sum(value for value in costs if value is not None)
    if attempts and (
        used_input >= pool.max_input_tokens
        or used_output >= pool.max_output_tokens
        or used_duration >= pool.max_duration_ms
        or used_cost is not None
        and used_cost >= pool.max_cost_microunits
    ):
        raise RoutePoolError("route_pool_budget_exhausted")
    if attempts:
        previous = attempts[0]
        if previous.attempt_ordinal != 1 or previous.candidate_id != pool.candidates[0].candidate_id:
            raise RoutePoolError("route_attempt_invalid")
        if previous.candidate_digest != pool.candidates[0].digest:
            raise RoutePoolError("route_attempt_invalid")
        if previous.failure_category not in pool.allowed_fallback_reasons:
            raise RoutePoolError("fallback_reason_denied")
        if previous.accepted_output:
            raise RoutePoolError("fallback_output_accepted")
        if previous.side_effect_state is not SideEffectState.NONE:
            raise RoutePoolError("fallback_side_effects")
        if (
            candidate.provider is not pool.candidates[0].provider
            and not pool.allow_cross_provider
        ):
            raise RoutePoolError("cross_provider_denied")
    return RouteSelection(
        candidate,
        ordinal,
        pool.max_input_tokens - used_input,
        pool.max_output_tokens - used_output,
        pool.max_duration_ms - used_duration,
        None if used_cost is None else pool.max_cost_microunits - used_cost,
    )
