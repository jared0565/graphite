"""Append-only verified evidence and typed privacy-safe aggregate export."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .contracts import (
    EvidenceProvenance,
    ExecutionReceipt,
    Effort,
    ProviderId,
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


class ReviewDefectClass(StrEnum):
    CORRECTNESS = "correctness"
    SECURITY = "security"
    RELIABILITY = "reliability"
    MAINTAINABILITY = "maintainability"
    PERFORMANCE = "performance"
    TEST_COVERAGE = "test_coverage"
    REQUIREMENT_MISS = "requirement_miss"


class HumanVerdict(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REWORK = "rework"


_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_VALIDATION_OUTCOMES = frozenset({"passed", "failed", "blocked", "not_run"})


def _bounded_optional(value: int | None, code: str, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(code)
    return value


def _bounded_required(value: object, code: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(code)
    return value


@dataclass(frozen=True)
class CliTelemetryRecord:
    """The complete public telemetry schema; raw artifacts have no field to enter."""

    provider: ProviderId
    requested_model: str
    effective_model: str
    effort: Effort
    capability_snapshot_digest: str
    category: TaskCategory
    risk: RiskTier
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    changed_file_count: int
    changed_byte_count: int
    validation_outcome: str
    review_defect_classes: tuple[ReviewDefectClass, ...]
    rework_count: int
    human_verdict: HumanVerdict | None
    provenance: EvidenceProvenance
    observed_at: int
    cost_status: str = "unknown"

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", ProviderId(self.provider))
        object.__setattr__(self, "effort", Effort(self.effort))
        object.__setattr__(self, "category", TaskCategory(self.category))
        object.__setattr__(self, "risk", RiskTier(self.risk))
        object.__setattr__(self, "provenance", EvidenceProvenance(self.provenance))
        for value in (self.requested_model, self.effective_model):
            if not isinstance(value, str) or _SAFE_MODEL.fullmatch(value) is None:
                raise ValueError("telemetry_model_invalid")
        if (
            not isinstance(self.capability_snapshot_digest, str)
            or _HEX_64.fullmatch(self.capability_snapshot_digest) is None
        ):
            raise ValueError("telemetry_snapshot_invalid")
        _bounded_optional(self.latency_ms, "telemetry_latency_invalid", 86_400_000)
        _bounded_optional(self.input_tokens, "telemetry_usage_invalid", 100_000_000)
        _bounded_optional(self.output_tokens, "telemetry_usage_invalid", 100_000_000)
        for name, maximum in (
            ("changed_file_count", 100_000),
            ("changed_byte_count", 10**12),
            ("rework_count", 1_000),
            ("observed_at", 10**12),
        ):
            _bounded_required(getattr(self, name), f"telemetry_{name}_invalid", maximum)
        if self.validation_outcome not in _VALIDATION_OUTCOMES:
            raise ValueError("telemetry_validation_outcome_invalid")
        if not isinstance(self.review_defect_classes, tuple):
            raise ValueError("telemetry_defect_classes_invalid")
        defects = tuple(ReviewDefectClass(item) for item in self.review_defect_classes)
        if len(defects) > len(ReviewDefectClass) or len(set(defects)) != len(defects):
            raise ValueError("telemetry_defect_classes_invalid")
        object.__setattr__(self, "review_defect_classes", tuple(sorted(defects, key=str)))
        if self.human_verdict is not None:
            object.__setattr__(self, "human_verdict", HumanVerdict(self.human_verdict))
        if self.cost_status != "unknown":
            raise ValueError("telemetry_cost_status_invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider.value,
            "requested_model": self.requested_model,
            "effective_model": self.effective_model,
            "effort": self.effort.value,
            "capability_snapshot_digest": self.capability_snapshot_digest,
            "category": self.category.value,
            "risk": self.risk.value,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_status": self.cost_status,
            "changed_file_count": self.changed_file_count,
            "changed_byte_count": self.changed_byte_count,
            "validation_outcome": self.validation_outcome,
            "review_defect_classes": [item.value for item in self.review_defect_classes],
            "rework_count": self.rework_count,
            "human_verdict": None if self.human_verdict is None else self.human_verdict.value,
            "provenance": self.provenance.value,
            "observed_at": self.observed_at,
        }


def record_cli_telemetry(store: RepositoryStore, record: CliTelemetryRecord) -> bool:
    if not isinstance(record, CliTelemetryRecord):
        raise ValueError("cli_telemetry_record_invalid")
    return store.record_cli_telemetry(record.to_dict())


@dataclass(frozen=True)
class WeightedTelemetrySummary:
    samples: int
    weighted_samples_millis: int
    weighted_success_millis: int
    success_millis: int
    latest_observed_at: int | None


def summarize_cli_telemetry(
    records: tuple[CliTelemetryRecord, ...],
    *,
    now: int,
    half_life_days: int = 30,
) -> WeightedTelemetrySummary:
    """Compute deterministic recency-weighted evidence; never infer missing cost."""
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise ValueError("telemetry_time_invalid")
    if isinstance(half_life_days, bool) or not isinstance(half_life_days, int) or half_life_days < 1:
        raise ValueError("telemetry_half_life_invalid")
    weighted_total = weighted_success = 0
    latest: int | None = None
    for record in records:
        if not isinstance(record, CliTelemetryRecord) or record.observed_at > now:
            raise ValueError("cli_telemetry_record_invalid")
        age_days = (now - record.observed_at) // 86_400
        weight = max(1, round(1_000 * (0.5 ** (age_days / half_life_days))))
        weighted_total += weight
        success = (
            record.validation_outcome == "passed"
            and not record.review_defect_classes
            and record.human_verdict is not HumanVerdict.REJECTED
        )
        weighted_success += weight * int(success)
        latest = record.observed_at if latest is None else max(latest, record.observed_at)
    success_millis = 0 if weighted_total == 0 else weighted_success * 1_000 // weighted_total
    return WeightedTelemetrySummary(
        len(records), weighted_total, weighted_success, success_millis, latest
    )


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
