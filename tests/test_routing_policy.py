"""Deterministic routing eligibility, scoring, and confidence tests."""
from __future__ import annotations

from dataclasses import replace

import pytest

from graphite.routing.contracts import Effort, RiskTier, TaskCategory, TaskProfile
from graphite.routing.policy import (
    CandidateMetrics,
    PolicyGates,
    rank_candidates,
    promotion_eligibility,
    wilson_lower_bound,
)
from graphite.routing.registry import parse_inventory


def _task(risk: RiskTier = RiskTier.LOW) -> TaskProfile:
    return TaskProfile(
        task_id="task-1",
        category=TaskCategory.FEATURE,
        risk=risk,
        complexity=3,
        impact_radius=4,
        risk_flags=(),
        context_requirements=("explicit_targets",),
        verification_requirements=("tests",),
    )


def _snapshot():
    return parse_inventory(
        {
            "models": [
                {
                    "name": "kimi-k2.7-code:cloud",
                    "model": "kimi-k2.7-code:cloud",
                    "digest": "a" * 64,
                    "details": {"context_length": 262_144},
                    "capabilities": ["completion", "tools", "thinking", "vision"],
                },
                {
                    "name": "kimi-k2.6:cloud",
                    "model": "kimi-k2.6:cloud",
                    "digest": "b" * 64,
                    "details": {"context_length": 262_144},
                    "capabilities": ["completion", "tools", "thinking", "vision"],
                },
            ]
        },
        refreshed_at=100,
        ttl_seconds=300,
    )


def _candidate(model_id: str = "kimi-k2.7-code:cloud", **changes) -> CandidateMetrics:
    values = {
        "model_id": model_id,
        "effort": Effort.DEFAULT,
        "repository_success_millis": None,
        "global_success_millis": 750,
        "expected_input_tokens": 4_000,
        "expected_output_tokens": 1_000,
        "expected_latency_ms": 3_000,
        "retry_rate_millis": 100,
        "escalation_rate_millis": 50,
        "quota_scarcity_millis": 0,
    }
    values.update(changes)
    return CandidateMetrics(**values)


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("graph_valid", "graph_invalid"),
        ("graph_fresh", "graph_stale"),
        ("registry_fresh", "registry_stale"),
        ("data_policy_allowed", "data_policy_blocked"),
        ("storage_available", "storage_unavailable"),
    ],
)
def test_global_hard_gates_fail_closed(field: str, reason: str) -> None:
    gates = replace(PolicyGates(), **{field: False})

    result = rank_candidates(_task(), _snapshot(), (_candidate(),), gates)

    assert result.selected is None
    assert result.manual_handoff is True
    assert reason in result.reasons


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (_candidate("unknown:cloud"), "model_profile_missing"),
        (_candidate(effort=Effort.MAX), "effort_unsupported"),
        (_candidate(expected_input_tokens=300_000), "context_exceeded"),
        (_candidate(expected_input_tokens=9_000, expected_output_tokens=2_000), "budget_exceeded"),
    ],
)
def test_candidate_hard_gates_produce_handoff(candidate: CandidateMetrics, reason: str) -> None:
    gates = PolicyGates(context_tokens=1_000, budget_tokens=10_000)

    result = rank_candidates(_task(), _snapshot(), (candidate,), gates)

    assert result.selected is None
    assert reason in result.reasons
    assert result.recommended_channels == ("claude_code", "codex")


def test_high_risk_provisional_profiles_are_ineligible() -> None:
    result = rank_candidates(
        _task(RiskTier.HIGH),
        _snapshot(),
        (_candidate(),),
        PolicyGates(),
    )

    assert result.selected is None
    assert "risk_ineligible" in result.reasons


def test_repository_evidence_overrides_global_prior() -> None:
    candidates = (
        _candidate("kimi-k2.7-code:cloud", repository_success_millis=600, global_success_millis=950),
        _candidate("kimi-k2.6:cloud", repository_success_millis=900, global_success_millis=500),
    )

    result = rank_candidates(_task(), _snapshot(), candidates, PolicyGates())

    assert result.selected is not None
    assert result.selected.model_id == "kimi-k2.6:cloud"


def test_retry_escalation_latency_and_quota_change_ranking() -> None:
    reliable = _candidate(
        "kimi-k2.7-code:cloud",
        global_success_millis=800,
        expected_latency_ms=2_000,
        retry_rate_millis=50,
        escalation_rate_millis=20,
    )
    expensive_failure = _candidate(
        "kimi-k2.6:cloud",
        global_success_millis=850,
        expected_latency_ms=20_000,
        retry_rate_millis=500,
        escalation_rate_millis=400,
        quota_scarcity_millis=500,
    )

    result = rank_candidates(_task(), _snapshot(), (expensive_failure, reliable), PolicyGates())

    assert result.selected is not None
    assert result.selected.model_id == reliable.model_id
    assert dict(result.selected.components)["reliability"] == 800


def test_ties_are_stable_by_exact_model_and_effort() -> None:
    candidates = (
        _candidate("kimi-k2.7-code:cloud"),
        _candidate("kimi-k2.6:cloud"),
    )

    first = rank_candidates(_task(), _snapshot(), candidates, PolicyGates())
    second = rank_candidates(_task(), _snapshot(), tuple(reversed(candidates)), PolicyGates())

    assert first == second
    assert [item.model_id for item in first.ranked] == [
        "kimi-k2.6:cloud",
        "kimi-k2.7-code:cloud",
    ]


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(global_success_millis=-1),
        _candidate(expected_latency_ms=-1),
        _candidate(retry_rate_millis=float("nan")),
    ],
)
def test_nonfinite_or_negative_metrics_fail_closed(candidate: CandidateMetrics) -> None:
    with pytest.raises(ValueError):
        rank_candidates(_task(), _snapshot(), (candidate,), PolicyGates())


@pytest.mark.parametrize(
    ("successes", "total", "expected"),
    [
        (0, 0, 0.0),
        (0, 10, 0.0),
        (10, 10, 0.722467),
        (50, 50, 0.928652),
        (95, 100, 0.88825),
    ],
)
def test_wilson_lower_bound_known_vectors(successes: int, total: int, expected: float) -> None:
    assert wilson_lower_bound(successes, total) == pytest.approx(expected, abs=0.000001)


def test_promotion_thresholds_and_high_risk_permanent_gate() -> None:
    low = promotion_eligibility(RiskTier.LOW, successes=50, total=50, severe_failures=0)
    medium = promotion_eligibility(RiskTier.MEDIUM, successes=100, total=100, severe_failures=0)
    high = promotion_eligibility(RiskTier.HIGH, successes=1_000, total=1_000, severe_failures=0)
    insufficient = promotion_eligibility(RiskTier.LOW, successes=49, total=49, severe_failures=0)
    severe = promotion_eligibility(RiskTier.LOW, successes=100, total=100, severe_failures=1)

    assert low.eligible is True
    assert medium.eligible is True
    assert high == replace(high, eligible=False, reason="high_risk_permanent_gate")
    assert insufficient.reason == "sample_size_insufficient"
    assert severe.reason == "severe_failure_open"
