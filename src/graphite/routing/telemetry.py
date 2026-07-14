"""Append-only verified evidence and typed privacy-safe aggregate export."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import (
    EvidenceProvenance,
    ExecutionReceipt,
    RiskTier,
    TaskCategory,
    VerifiedOutcome,
)
from .registry import BUNDLED_PROFILES
from .storage import AggregateRecord, AggregateStore, RepositoryStore


@dataclass(frozen=True)
class EvidenceCorrelation:
    task_id: str
    decision_id: str
    graph_fingerprint: str

    def __post_init__(self) -> None:
        if not self.task_id or not self.decision_id:
            raise ValueError("evidence_correlation_invalid")
        if len(self.graph_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.graph_fingerprint
        ):
            raise ValueError("evidence_correlation_invalid")


@dataclass(frozen=True)
class EvidenceSummary:
    sample_count: int
    success_count: int
    severe_failure_count: int
    human_count: int


def record_verified_outcome(
    store: RepositoryStore,
    outcome: VerifiedOutcome,
    correlation: EvidenceCorrelation,
) -> bool:
    """Append an outcome after binding it to the recorded execution identity."""
    linked = store.execution_evidence(outcome.execution_id)
    expected = {
        "task_id": correlation.task_id,
        "decision_id": correlation.decision_id,
        "graph_fingerprint": correlation.graph_fingerprint,
    }
    if linked != expected:
        raise ValueError("evidence_correlation_invalid")
    if outcome.provenance in {
        EvidenceProvenance.MACHINE_VERIFIED,
        EvidenceProvenance.CI_IMPORTED,
    } and any(
        value is None
        for value in (outcome.build_passed, outcome.tests_passed, outcome.security_passed)
    ):
        raise ValueError("evidence_verification_incomplete")
    success = bool(
        outcome.build_passed is True
        and outcome.tests_passed is True
        and outcome.security_passed is True
        and not outcome.escalated
        and not outcome.reverted
        and not outcome.severe_failure
    )
    if outcome.provenance is EvidenceProvenance.HUMAN:
        success = outcome.human_accepted is True and not outcome.reverted
    if outcome.provenance is EvidenceProvenance.REVERSION:
        success = False
    return store.record_outcome(
        outcome.outcome_id,
        outcome.execution_id,
        outcome.provenance.value,
        success,
        outcome.severe_failure,
        outcome.recorded_at,
    )


def evidence_summary(store: RepositoryStore) -> EvidenceSummary:
    """Derive current evidence without mutating historical events."""
    cutoff = store.latest_incident_review()
    rows = [
        row
        for row in store.outcome_evidence_rows()
        if cutoff is None or int(row["recorded_at"]) > cutoff
    ]
    human_count = sum(row["provenance"] == EvidenceProvenance.HUMAN.value for row in rows)
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row["execution_id"]), []).append(row)
    samples = successes = severe = 0
    for events in grouped.values():
        admitted = [
            row for row in events
            if row["provenance"] in {
                EvidenceProvenance.MACHINE_VERIFIED.value,
                EvidenceProvenance.CI_IMPORTED.value,
            }
        ]
        if not admitted:
            continue
        samples += 1
        reverted = any(
            row["provenance"] == EvidenceProvenance.REVERSION.value for row in events
        )
        latest = admitted[-1]
        successes += int(bool(latest["success"]) and not reverted)
        severe += int(any(bool(row["severe_failure"]) for row in admitted))
    return EvidenceSummary(samples, successes, severe, human_count)


def close_incident_review(
    store: RepositoryStore,
    execution_id: str,
    *,
    reviewed_at: int,
) -> bool:
    rows = store.outcome_evidence_rows()
    if not any(
        row["execution_id"] == execution_id and bool(row["severe_failure"])
        for row in rows
    ):
        raise ValueError("incident_review_invalid")
    return store.close_incident(execution_id, reviewed_at)


def _bucket(value: int | None) -> int:
    if value is None or value <= 0:
        return 0
    return min(20, int(math.log2(value)) + 1)


def export_sanitized_aggregate(
    aggregate: AggregateStore,
    receipt: ExecutionReceipt,
    *,
    category: TaskCategory,
    risk: RiskTier,
    policy_version: str,
    recorded_at: int,
) -> bool:
    """Build an aggregate solely from allowlisted typed fields."""
    if receipt.model_id not in BUNDLED_PROFILES:
        raise ValueError("model_id_invalid")
    if isinstance(recorded_at, bool) or not isinstance(recorded_at, int) or recorded_at < 0:
        raise ValueError("recorded_at_invalid")
    record = AggregateRecord(
        model_id=receipt.model_id,
        effort=receipt.effort.value,
        category=TaskCategory(category).value,
        risk=RiskTier(risk).value,
        outcome=receipt.outcome.value,
        input_bucket=_bucket(receipt.input_tokens),
        output_bucket=_bucket(receipt.output_tokens),
        latency_bucket=_bucket(receipt.latency_ms),
        policy_version=policy_version,
        recorded_day=recorded_at // 86_400,
    )
    return aggregate.write(record)

