"""Deterministic routing eligibility, scoring, and confidence tests."""
from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest

import graphite.routing.policy as policy_module
import graphite.routing.registry as registry_module
from graphite.routing.contracts import Effort, RiskTier, TaskCategory, TaskProfile
from graphite.routing.policy import (
    CandidateMetrics,
    PolicyGates,
    rank_candidates,
    promotion_eligibility,
    wilson_lower_bound,
)
from graphite.routing.registry import (
    BUNDLED_PROFILES,
    ModelRole,
    RegistryProfile,
    UsageClass,
    lifecycle_is_eligible,
    parse_inventory,
)


def _task(
    risk: RiskTier = RiskTier.LOW,
    category: TaskCategory = TaskCategory.FEATURE,
) -> TaskProfile:
    return TaskProfile(
        task_id="task-1",
        category=category,
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
                    "name": "minimax-m2.7:cloud",
                    "model": "minimax-m2.7:cloud",
                    "digest": "b" * 64,
                    "details": {"context_length": 204_800},
                    "capabilities": ["code", "completion", "tools", "thinking"],
                },
                {
                    "name": "nemotron-3-super:cloud",
                    "model": "nemotron-3-super:cloud",
                    "digest": "c" * 64,
                    "details": {"context_length": 262_144},
                    "capabilities": ["completion", "reasoning", "tools", "thinking"],
                },
                {
                    "name": "minimax-m3:cloud",
                    "model": "minimax-m3:cloud",
                    "digest": "d" * 64,
                    "details": {"context_length": 524_288},
                    "capabilities": [
                        "architecture",
                        "completion",
                        "reasoning",
                        "tools",
                        "thinking",
                        "vision",
                    ],
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


def _all_candidates() -> tuple[CandidateMetrics, ...]:
    return tuple(_candidate(model_id) for model_id in BUNDLED_PROFILES)


@pytest.mark.parametrize(
    ("retirement_date", "expected"),
    [
        (None, True),
        ("2026-08-14", True),
        ("2026-08-13", False),
        ("2026-07-14", False),
    ],
)
def test_lifecycle_eligibility_requires_more_than_minimum_runway(
    retirement_date: str | None,
    expected: bool,
) -> None:
    assert lifecycle_is_eligible(retirement_date, "2026-07-14") is expected


@pytest.mark.parametrize(
    ("retirement_date", "current_date"),
    [
        ("not-a-date", "2026-07-14"),
        ("2026-8-14", "2026-07-14"),
        ("2026-08-14", "not-a-date"),
        ("2026-08-14", "2026-7-14"),
    ],
)
def test_lifecycle_eligibility_rejects_malformed_dates(
    retirement_date: str,
    current_date: str,
) -> None:
    with pytest.raises(ValueError, match="^lifecycle_date_invalid$"):
        lifecycle_is_eligible(retirement_date, current_date)


@pytest.mark.parametrize("runway", [True, 30.0, "30", -1, 366])
def test_lifecycle_eligibility_rejects_invalid_runway(runway: object) -> None:
    with pytest.raises(ValueError, match="^lifecycle_runway_invalid$"):
        lifecycle_is_eligible(
            "2026-08-14",
            "2026-07-14",
            minimum_runway_days=runway,
        )


def test_retiring_model_hands_off_before_inventory_lookup_or_scoring(monkeypatch) -> None:
    model_id = "kimi-k2.7-code:cloud"
    retiring: RegistryProfile = replace(
        BUNDLED_PROFILES[model_id],
        retirement_date="2026-08-13",
    )
    replacement = MappingProxyType({**BUNDLED_PROFILES, model_id: retiring})
    monkeypatch.setattr(policy_module, "BUNDLED_PROFILES", replacement)
    monkeypatch.setattr(registry_module, "BUNDLED_PROFILES", replacement)
    monkeypatch.setattr(
        policy_module,
        "_score",
        lambda *args: (_ for _ in ()).throw(AssertionError("scoring must not run")),
    )

    result = rank_candidates(
        _task(),
        parse_inventory({"models": []}, refreshed_at=100, ttl_seconds=300),
        (_candidate(model_id),),
        PolicyGates(),
    )

    assert result.manual_handoff is True
    assert result.reasons == ("model_retiring",)


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
        _candidate("minimax-m2.7:cloud", repository_success_millis=900, global_success_millis=500),
    )

    result = rank_candidates(_task(), _snapshot(), candidates, PolicyGates())

    assert result.selected is not None
    assert result.selected.model_id == "minimax-m2.7:cloud"


def test_retry_escalation_latency_and_quota_change_ranking() -> None:
    reliable = _candidate(
        "kimi-k2.7-code:cloud",
        global_success_millis=800,
        expected_latency_ms=2_000,
        retry_rate_millis=50,
        escalation_rate_millis=20,
    )
    expensive_failure = _candidate(
        "minimax-m2.7:cloud",
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


def test_low_risk_isolated_code_uses_role_and_usage_ranking() -> None:
    candidates = _all_candidates()

    first = rank_candidates(
        _task(category=TaskCategory.ISOLATED_CODE),
        _snapshot(),
        candidates,
        PolicyGates(),
    )
    second = rank_candidates(
        _task(category=TaskCategory.ISOLATED_CODE),
        _snapshot(),
        tuple(reversed(candidates)),
        PolicyGates(),
    )

    assert first == second
    assert first.selected is not None
    assert first.selected.model_id == "kimi-k2.7-code:cloud"
    assert [item.model_id for item in first.ranked[:3]] == [
        "kimi-k2.7-code:cloud",
        "minimax-m2.7:cloud",
        "nemotron-3-super:cloud",
    ]
    assert [item.score for item in first.ranked[:3]] == [727, 707, 667]
    assert first.policy_version == "2"
    assert dict(first.selected.components)["role_fit_bonus"] == 100
    assert dict(first.selected.components)["usage_penalty"] == 40


def test_medium_usage_beats_high_usage_when_role_and_evidence_match(monkeypatch) -> None:
    high_id = "kimi-k2.7-code:cloud"
    medium_id = "minimax-m2.7:cloud"
    medium = replace(
        BUNDLED_PROFILES[medium_id],
        roles=(ModelRole.CODING_PRIMARY, ModelRole.CODING),
        usage_class=UsageClass.MEDIUM,
    )
    replacement = MappingProxyType({**BUNDLED_PROFILES, medium_id: medium})
    monkeypatch.setattr(policy_module, "BUNDLED_PROFILES", replacement)
    monkeypatch.setattr(registry_module, "BUNDLED_PROFILES", replacement)

    result = rank_candidates(
        _task(category=TaskCategory.ISOLATED_CODE),
        _snapshot(),
        (_candidate(high_id), _candidate(medium_id)),
        PolicyGates(),
    )

    assert [item.model_id for item in result.ranked] == [medium_id, high_id]


def test_minimax_m3_is_sole_candidate_for_context_above_262144() -> None:
    result = rank_candidates(
        _task(category=TaskCategory.ARCHITECTURE),
        _snapshot(),
        _all_candidates(),
        PolicyGates(context_tokens=300_000, budget_tokens=600_000),
    )

    assert result.selected is not None
    assert [item.model_id for item in result.ranked] == ["minimax-m3:cloud"]


def test_high_risk_architecture_hands_off_when_all_profiles_are_provisional() -> None:
    result = rank_candidates(
        _task(RiskTier.HIGH, TaskCategory.ARCHITECTURE),
        _snapshot(),
        _all_candidates(),
        PolicyGates(context_tokens=100_000, budget_tokens=600_000),
    )

    assert result.selected is None
    assert result.manual_handoff is True
    assert result.reasons == ("risk_ineligible",)


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
