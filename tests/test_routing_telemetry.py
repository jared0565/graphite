"""Verified evidence and privacy-safe aggregate learning tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.routing.contracts import (
    EvidenceProvenance,
    Effort,
    ExecutionOutcome,
    ExecutionReceipt,
    RiskTier,
    TaskCategory,
    VerifiedOutcome,
)
from graphite.routing.storage import AggregateStore, RepositoryStore
from graphite.routing.telemetry import (
    EvidenceCorrelation,
    CliTelemetryRecord,
    HumanVerdict,
    ReviewDefectClass,
    close_incident_review,
    evidence_summary,
    export_sanitized_aggregate,
    record_verified_outcome,
    record_cli_telemetry,
    summarize_cli_telemetry,
)


def _store(tmp_path: Path) -> RepositoryStore:
    root = tmp_path / "repo"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    store.record_task("task-1", "feature", "low", "a" * 64, 10)
    store.record_decision("decision-1", "task-1", "kimi-k2.7-code:cloud", "default", "1", "1", 11)
    store.insert_execution(
        execution_id="exec-1", idempotency_key="idem-1", task_id="task-1",
        decision_id="decision-1", approval_id=None, model_id="kimi-k2.7-code:cloud",
        effort="default", status="succeeded", reserved_tokens=100, created_at=12,
    )
    store.link_execution_evidence("exec-1", "task-1", "decision-1", "b" * 64)
    return store


def _outcome(
    outcome_id: str,
    provenance: EvidenceProvenance = EvidenceProvenance.MACHINE_VERIFIED,
    **changes: object,
) -> VerifiedOutcome:
    values: dict[str, object] = {
        "outcome_id": outcome_id,
        "execution_id": "exec-1",
        "provenance": provenance,
        "build_passed": True,
        "tests_passed": True,
        "security_passed": True,
        "human_accepted": None,
        "repair_count": 0,
        "escalated": False,
        "reverted": False,
        "severe_failure": False,
        "recorded_at": 20,
    }
    values.update(changes)
    return VerifiedOutcome(**values)


def _correlation(**changes: str) -> EvidenceCorrelation:
    values = {
        "task_id": "task-1",
        "decision_id": "decision-1",
        "graph_fingerprint": "b" * 64,
    }
    values.update(changes)
    return EvidenceCorrelation(**values)


def test_machine_evidence_requires_complete_matching_correlation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_verified_outcome(store, _outcome("outcome-1"), _correlation())
    assert evidence_summary(store).sample_count == 1
    assert evidence_summary(store).success_count == 1

    with pytest.raises(ValueError, match="evidence_correlation_invalid"):
        record_verified_outcome(
            store, _outcome("outcome-2"), _correlation(graph_fingerprint="c" * 64)
        )
    with pytest.raises(ValueError, match="evidence_verification_incomplete"):
        record_verified_outcome(
            store, _outcome("outcome-3", tests_passed=None), _correlation()
        )


@pytest.mark.parametrize(
    "provenance",
    [EvidenceProvenance.HUMAN, EvidenceProvenance.PAIRWISE, EvidenceProvenance.AMBIGUOUS],
)
def test_non_machine_evidence_is_retained_but_excluded_from_confidence(
    tmp_path: Path, provenance: EvidenceProvenance
) -> None:
    store = _store(tmp_path)
    record_verified_outcome(store, _outcome("outcome-1", provenance), _correlation())
    assert store.row_count("outcomes") == 1
    assert evidence_summary(store).sample_count == 0


def test_reversion_invalidates_prior_success_without_rewriting_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_verified_outcome(store, _outcome("success"), _correlation())
    record_verified_outcome(
        store,
        _outcome("revert", EvidenceProvenance.REVERSION, reverted=True, recorded_at=30),
        _correlation(),
    )
    summary = evidence_summary(store)
    assert store.row_count("outcomes") == 2
    assert (summary.sample_count, summary.success_count) == (1, 0)


def test_closed_severe_incident_starts_a_new_evidence_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_verified_outcome(
        store, _outcome("incident", severe_failure=True, tests_passed=False), _correlation()
    )
    assert evidence_summary(store).severe_failure_count == 1
    close_incident_review(store, "exec-1", reviewed_at=25)
    assert evidence_summary(store).sample_count == 0


def test_aggregate_is_typed_coarse_and_opt_in_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    receipt = ExecutionReceipt(
        execution_id="exec-1", approval_id="approval-1", model_id="kimi-k2.7-code:cloud",
        effort=Effort.DEFAULT, outcome=ExecutionOutcome.SUCCEEDED, input_tokens=1234,
        output_tokens=99, latency_ms=4567, prompt_hash="c" * 64,
        response_hash="d" * 64, failure_reason=None,
    )
    state = tmp_path / "machine-state"
    disabled = AggregateStore(store.root, opt_in=False, state_dir=state)
    assert export_sanitized_aggregate(
        disabled, receipt, category=TaskCategory.FEATURE, risk=RiskTier.LOW,
        policy_version="1", recorded_at=86_400,
    ) is False
    assert not disabled.path.exists()

    enabled = AggregateStore(store.root, opt_in=True, state_dir=state)
    assert export_sanitized_aggregate(
        enabled, receipt, category=TaskCategory.FEATURE, risk=RiskTier.LOW,
        policy_version="1", recorded_at=86_400,
    ) is True
    assert enabled.row_count() == 1
    raw = enabled.path.read_bytes()
    for forbidden in (b"repo", b"approval-1", b"exec-1", b"cccccccc", b"dddddddd"):
        assert forbidden not in raw


def test_aggregate_rejects_untyped_or_unknown_labels(tmp_path: Path) -> None:
    store = _store(tmp_path)
    receipt = ExecutionReceipt(
        execution_id="exec-1", approval_id="approval-1", model_id="evil/repo/path",
        effort=Effort.DEFAULT, outcome=ExecutionOutcome.SUCCEEDED, input_tokens=1,
        output_tokens=1, latency_ms=1, prompt_hash="c" * 64,
        response_hash=None, failure_reason=None,
    )
    aggregate = AggregateStore(store.root, opt_in=True, state_dir=tmp_path / "state")
    with pytest.raises(ValueError, match="model_id_invalid"):
        export_sanitized_aggregate(
            aggregate, receipt, category=TaskCategory.FEATURE, risk=RiskTier.LOW,
            policy_version="1", recorded_at=0,
        )


def _cli_record(**changes: object) -> CliTelemetryRecord:
    values: dict[str, object] = {
        "provider": "codex",
        "requested_model": "gpt-tested-codex",
        "effective_model": "gpt-tested-codex-2026-07-18",
        "effort": "high",
        "capability_snapshot_digest": "e" * 64,
        "category": "feature",
        "risk": "medium",
        "latency_ms": 1200,
        "input_tokens": 500,
        "output_tokens": 200,
        "changed_file_count": 3,
        "changed_byte_count": 1500,
        "validation_outcome": "passed",
        "review_defect_classes": (),
        "rework_count": 0,
        "human_verdict": HumanVerdict.ACCEPTED,
        "provenance": "machine_verified",
        "observed_at": 1_000,
    }
    values.update(changes)
    return CliTelemetryRecord(**values)


def test_cli_telemetry_schema_is_allowlisted_append_only_and_cost_unknown(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _cli_record(review_defect_classes=(ReviewDefectClass.MAINTAINABILITY,))
    assert record_cli_telemetry(store, record) is True
    assert record_cli_telemetry(store, record) is False
    assert store.row_count("cli_telemetry_events") == 1
    public = record.to_dict()
    assert public["cost_status"] == "unknown"
    forbidden = {"source", "prompt", "response", "diff", "path", "diagnostics", "secret"}
    assert forbidden.isdisjoint(public)
    raw = store.path.read_bytes()
    for sensitive in (b"super-secret", b"src/private.py", b"raw prompt", b"raw diff"):
        assert sensitive not in raw

    with pytest.raises(TypeError):
        CliTelemetryRecord(**{**record.__dict__, "source": "super-secret"})
    with pytest.raises(ValueError, match="telemetry_model_invalid"):
        _cli_record(requested_model="../../src/private.py")
    with pytest.raises(ValueError, match="telemetry_cost_status_invalid"):
        _cli_record(cost_status="0-usd")


def test_cli_telemetry_recency_weighting_is_deterministic() -> None:
    recent = _cli_record(observed_at=100 * 86_400)
    old_failure = _cli_record(
        observed_at=40 * 86_400,
        validation_outcome="failed",
        human_verdict=HumanVerdict.REJECTED,
    )
    first = summarize_cli_telemetry((old_failure, recent), now=100 * 86_400)
    second = summarize_cli_telemetry((recent, old_failure), now=100 * 86_400)
    assert first == second
    assert first.samples == 2
    assert first.success_millis > 500
