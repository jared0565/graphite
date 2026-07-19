"""Single-use automatic execution coordinator for approved route pools."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from .approval import ApprovalAuthority, ApprovalError, SignedApproval
from .contracts import PublicRecord
from .route_pool import (
    ApprovedRoutePool,
    RouteAttemptEvidence,
    RouteAuthority,
    RoutePoolError,
    RouteSelection,
    select_route,
)


@dataclass(frozen=True, slots=True)
class RouteExecutionEvidence(PublicRecord):
    candidate_id: str
    candidate_digest: str
    attempt_ordinal: int
    outcome_category: str
    input_tokens: int
    output_tokens: int
    duration_ms: int
    cost_microunits: int | None

    _public_fields: ClassVar[tuple[str, ...]] = (
        "candidate_id",
        "candidate_digest",
        "attempt_ordinal",
        "outcome_category",
        "input_tokens",
        "output_tokens",
        "duration_ms",
        "cost_microunits",
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.candidate_id, str)
            or not self.candidate_id
            or "\x00" in self.candidate_id
            or len(self.candidate_id) > 256
            or any(character.isspace() for character in self.candidate_id)
            or not isinstance(self.candidate_digest, str)
            or len(self.candidate_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.candidate_digest
            )
            or self.outcome_category
            not in {
                "succeeded",
                "capacity_unavailable",
                "provider_process_failure",
                "provider_unavailable",
                "provider_protocol",
                "timeout",
                "cancelled",
            }
            or isinstance(self.attempt_ordinal, bool)
            or not isinstance(self.attempt_ordinal, int)
            or not 1 <= self.attempt_ordinal <= 2
        ):
            raise RoutePoolError("route_evidence_invalid")
        for value, maximum in (
            (self.input_tokens, 262_144),
            (self.output_tokens, 32_768),
            (self.duration_ms, 86_400_000),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= maximum
            ):
                raise RoutePoolError("route_evidence_invalid")
        if self.cost_microunits is not None and (
            isinstance(self.cost_microunits, bool)
            or not isinstance(self.cost_microunits, int)
            or self.cost_microunits < 0
            or self.cost_microunits > 10**12
        ):
            raise RoutePoolError("route_evidence_invalid")


@dataclass(frozen=True, slots=True)
class RouteExecutionResult:
    candidate_id: str
    candidate_digest: str
    attempt_ordinal: int
    output: str = field(repr=False, compare=False)
    input_tokens: int
    output_tokens: int
    duration_ms: int
    cost_microunits: int | None


class RouteAttemptFailure(RuntimeError):
    """Trusted adapter failure carrying sanitized evidence only."""

    def __init__(self, evidence: RouteAttemptEvidence) -> None:
        if not isinstance(evidence, RouteAttemptEvidence):
            raise RoutePoolError("route_attempt_invalid")
        self.evidence = evidence
        super().__init__(evidence.failure_category)


class AuthorityLoader(Protocol):
    def __call__(self) -> tuple[RouteAuthority, ...]: ...


class RouteRunner(Protocol):
    def __call__(self, selection: RouteSelection) -> RouteExecutionResult: ...


EvidenceSink = Callable[[RouteExecutionEvidence], Any]


def _load_authority(loader: AuthorityLoader) -> tuple[RouteAuthority, ...]:
    try:
        authorities = loader()
    except Exception:
        raise RoutePoolError("route_authority_unavailable") from None
    if not isinstance(authorities, tuple):
        raise RoutePoolError("route_authority_invalid")
    return authorities


def _persist_evidence(
    sink: EvidenceSink,
    evidence: RouteExecutionEvidence,
) -> None:
    try:
        sink(evidence)
    except Exception:
        raise RoutePoolError("route_evidence_unavailable") from None


def _failure_evidence(
    selection: RouteSelection,
    failure: RouteAttemptEvidence,
) -> RouteExecutionEvidence:
    if (
        failure.candidate_id != selection.candidate.candidate_id
        or failure.candidate_digest != selection.candidate.digest
        or failure.attempt_ordinal != selection.attempt_ordinal
    ):
        raise RoutePoolError("route_attempt_invalid")
    return RouteExecutionEvidence(
        candidate_id=failure.candidate_id,
        candidate_digest=failure.candidate_digest,
        attempt_ordinal=failure.attempt_ordinal,
        outcome_category=failure.failure_category,
        input_tokens=failure.input_tokens,
        output_tokens=failure.output_tokens,
        duration_ms=failure.duration_ms,
        cost_microunits=failure.cost_microunits,
    )


def _validate_result(
    pool: ApprovedRoutePool,
    selection: RouteSelection,
    result: RouteExecutionResult,
) -> RouteExecutionEvidence:
    if (
        not isinstance(result, RouteExecutionResult)
        or result.candidate_id != selection.candidate.candidate_id
        or result.candidate_digest != selection.candidate.digest
        or result.attempt_ordinal != selection.attempt_ordinal
        or not isinstance(result.output, str)
        or not result.output
        or "\x00" in result.output
        or len(result.output) > 1_048_576
    ):
        raise RoutePoolError("route_result_invalid")
    values = (
        (result.input_tokens, selection.remaining_input_tokens),
        (result.output_tokens, selection.remaining_output_tokens),
        (result.duration_ms, selection.remaining_duration_ms),
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > maximum
        for value, maximum in values
    ):
        raise RoutePoolError("route_pool_budget_exhausted")
    if pool.max_cost_microunits is None:
        if result.cost_microunits is not None:
            raise RoutePoolError("route_pool_cost_invalid")
    elif (
        isinstance(result.cost_microunits, bool)
        or not isinstance(result.cost_microunits, int)
        or result.cost_microunits < 0
        or selection.remaining_cost_microunits is None
        or result.cost_microunits > selection.remaining_cost_microunits
    ):
        raise RoutePoolError("route_pool_budget_exhausted")
    return RouteExecutionEvidence(
        result.candidate_id,
        result.candidate_digest,
        result.attempt_ordinal,
        "succeeded",
        result.input_tokens,
        result.output_tokens,
        result.duration_ms,
        result.cost_microunits,
    )


def execute_approved_route_pool(
    *,
    pool: ApprovedRoutePool,
    signed_approval: SignedApproval,
    approval_authority: ApprovalAuthority,
    authority_loader: AuthorityLoader,
    runner: RouteRunner,
    evidence_sink: EvidenceSink,
    repository_quota_tokens: int,
    machine_quota_tokens: int,
    now: Callable[[], int],
) -> RouteExecutionResult:
    """Consume one pool and automatically advance once on safe capacity failure."""
    if (
        not isinstance(pool, ApprovedRoutePool)
        or not isinstance(approval_authority, ApprovalAuthority)
        or not callable(authority_loader)
        or not callable(runner)
        or not callable(evidence_sink)
        or not callable(now)
    ):
        raise RoutePoolError("route_execution_invalid")
    try:
        approval_authority.consume(
            signed_approval,
            pool,
            repository_quota_tokens=repository_quota_tokens,
            machine_quota_tokens=machine_quota_tokens,
        )
    except ApprovalError:
        raise
    attempts: list[RouteAttemptEvidence] = []
    while len(attempts) < pool.max_attempts:
        try:
            current_time = now()
        except Exception:
            raise RoutePoolError("route_pool_time_invalid") from None
        selection = select_route(
            pool,
            _load_authority(authority_loader),
            tuple(attempts),
            now=current_time,
        )
        try:
            result = runner(selection)
        except RouteAttemptFailure as exc:
            failure = exc.evidence
            evidence = _failure_evidence(selection, failure)
            _persist_evidence(evidence_sink, evidence)
            attempts.append(failure)
            if len(attempts) >= pool.max_attempts:
                raise RoutePoolError("route_pool_attempts_exhausted") from None
            continue
        except Exception:
            raise RoutePoolError("route_attempt_failed") from None
        evidence = _validate_result(pool, selection, result)
        _persist_evidence(evidence_sink, evidence)
        return result
    raise RoutePoolError("route_pool_attempts_exhausted")
