"""Deterministic routing eligibility, scoring, and confidence tests."""
from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest

import graphite.routing.policy as policy_module
import graphite.routing.registry as registry_module
from graphite.routing.contracts import (
    CapabilityProfile,
    CapabilitySnapshot,
    CliIdentity,
    Effort,
    ModelProfile,
    PermissionMode,
    ProviderId,
    RiskTier,
    TaskCategory,
    TaskProfile,
)
from graphite.routing.policy import (
    CandidateMetrics,
    CliCandidateMetrics,
    CliPolicyGates,
    CliLearnedPolicy,
    PolicyGates,
    rank_candidates,
    rank_cli_candidates,
    promotion_eligibility,
    compare_cli_policy_candidate,
    sign_cli_policy,
    wilson_lower_bound,
)
from graphite.routing.registry import (
    BUNDLED_PROFILES,
    ModelRole,
    RegistryProfile,
    RegistrySnapshot,
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


def _cli_snapshot(
    provider: ProviderId = ProviderId.CLAUDE_CODE,
    requested_model: str = "sonnet",
    effective_model: str = "claude-sonnet-5",
    risk_ceiling: RiskTier = RiskTier.MEDIUM,
    permission_mode: PermissionMode = PermissionMode.WORKSPACE_WRITE,
    expires_at: int = 200,
) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        1,
        100,
        expires_at,
        CliIdentity(provider, "a" * 64, "2.1.208", "1.0.0"),
        CapabilityProfile(
            provider,
            requested_model,
            effective_model,
            "2026-07-18.1",
            ("code", "reasoning", "architecture"),
            200_000,
            (Effort.LOW, Effort.MEDIUM, Effort.HIGH),
            risk_ceiling,
            permission_mode,
        ),
    )


def _cli_candidate(snapshot: CapabilitySnapshot, **changes) -> CliCandidateMetrics:
    values = {
        "capability_snapshot_digest": snapshot.digest,
        "effort": Effort.HIGH,
        "repository_success_millis": None,
        "global_success_millis": 750,
        "sample_count": 0,
        "expected_input_tokens": 4_000,
        "expected_output_tokens": 1_000,
        "expected_latency_ms": 3_000,
        "quota_scarcity_millis": 0,
    }
    values.update(changes)
    return CliCandidateMetrics(**values)


def _cli_gates(snapshot: CapabilitySnapshot, **changes) -> CliPolicyGates:
    values = {"current_cli_identities": (snapshot.identity,)}
    values.update(changes)
    return CliPolicyGates(**values)


def test_cli_ranking_applies_identity_auth_expiry_risk_context_and_permission_gates() -> None:
    snapshot = _cli_snapshot()
    cases = (
        (_cli_gates(snapshot, authenticated_providers=()), "provider_unauthenticated"),
        (_cli_gates(snapshot, now=201), "capability_snapshot_expired"),
        (_cli_gates(snapshot, permission_mode=PermissionMode.READ_ONLY), "permission_mismatch"),
        (_cli_gates(snapshot, context_tokens=199_001, budget_tokens=300_000), "context_exceeded"),
        (
            _cli_gates(
                snapshot,
                current_cli_identities=(
                    replace(snapshot.identity, cli_version="2.1.209"),
                ),
            ),
            "cli_identity_changed",
        ),
    )
    for gates, reason in cases:
        result = rank_cli_candidates(_task(), (snapshot,), (_cli_candidate(snapshot),), gates)
        assert result.selected is None
        assert reason in result.reasons
    high = rank_cli_candidates(
        _task(RiskTier.HIGH),
        (snapshot,),
        (_cli_candidate(snapshot),),
        _cli_gates(snapshot),
    )
    assert high.selected is None
    assert "risk_ineligible" in high.reasons


def test_cli_ranking_is_deterministic_and_ignores_undersampled_repository_signal() -> None:
    claude = _cli_snapshot()
    codex = _cli_snapshot(
        ProviderId.CODEX,
        "gpt-tested-codex",
        "gpt-tested-codex-2026-07-18",
    )
    candidates = (
        _cli_candidate(claude, repository_success_millis=100, global_success_millis=700),
        _cli_candidate(codex, repository_success_millis=1_000, global_success_millis=800),
    )

    gates = CliPolicyGates(current_cli_identities=(claude.identity, codex.identity))
    first = rank_cli_candidates(_task(), (claude, codex), candidates, gates)
    second = rank_cli_candidates(
        _task(), tuple(reversed((claude, codex))), tuple(reversed(candidates)), gates
    )

    assert first == second
    assert first.selected is not None
    assert first.selected.provider is ProviderId.CODEX


def test_cli_ranking_penalizes_missing_confidence_recency_and_unknown_cost() -> None:
    snapshot = _cli_snapshot()
    candidate = _cli_candidate(snapshot)
    result = rank_cli_candidates(_task(), (snapshot,), (candidate,), _cli_gates(snapshot))
    assert result.selected is not None
    components = dict(result.selected.components)
    assert components["confidence_penalty"] == 150
    assert components["recency_penalty"] == 50
    assert components["cost_unknown_penalty"] == 25
    with pytest.raises(ValueError, match="cost_status_invalid"):
        rank_cli_candidates(
            _task(), (snapshot,), (_cli_candidate(snapshot, cost_status="zero"),),
            _cli_gates(snapshot),
        )


def test_learned_policy_can_only_tune_scoring_and_never_grants_autonomy() -> None:
    candidate = CliLearnedPolicy(
        "candidate-4", "candidate-3",
        (("reliability", 900), ("latency", 100), ("confidence", 200)),
    )
    payload = candidate.to_payload()
    assert payload["autonomy"] is False
    assert payload["allowed_providers"] == ["claude-code", "codex"]
    assert payload["permission_ceiling"] == "workspace-write"
    assert payload["risk_ceilings"] == {"claude-code": "medium", "codex": "medium"}
    assert len(sign_cli_policy(candidate, "a" * 64, b"k" * 32)) == 64
    insufficient = compare_cli_policy_candidate(
        candidate,
        evidence_hash="a" * 64,
        sample_count=19,
        baseline_score_millis=700,
        candidate_score_millis=800,
    )
    sufficient = compare_cli_policy_candidate(
        candidate,
        evidence_hash="a" * 64,
        sample_count=20,
        baseline_score_millis=700,
        candidate_score_millis=800,
    )
    assert insufficient.promotion_eligible is False
    assert insufficient.reason == "sample_size_insufficient"
    assert sufficient.promotion_eligible is True


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
    assert [item.model_id for item in first.ranked] == [
        "kimi-k2.7-code:cloud",
        "minimax-m2.7:cloud",
    ]
    assert [item.score for item in first.ranked] == [727, 707]
    assert first.policy_version == "2"
    assert dict(first.selected.components)["role_fit_bonus"] == 100
    assert dict(first.selected.components)["usage_penalty"] == 40


def test_capability_ineligible_sole_candidate_cannot_be_selected() -> None:
    result = rank_candidates(
        _task(category=TaskCategory.ISOLATED_CODE),
        _snapshot(),
        (_candidate("nemotron-3-super:cloud"),),
        PolicyGates(),
    )

    assert result.selected is None
    assert result.manual_handoff is True
    assert result.reasons == ("capability_missing",)


def test_refactor_keeps_reasoning_review_model_as_deterministic_alternative() -> None:
    candidates = _all_candidates()

    first = rank_candidates(
        _task(category=TaskCategory.REFACTOR),
        _snapshot(),
        candidates,
        PolicyGates(),
    )
    second = rank_candidates(
        _task(category=TaskCategory.REFACTOR),
        _snapshot(),
        tuple(reversed(candidates)),
        PolicyGates(),
    )

    assert first == second
    assert [item.model_id for item in first.ranked] == [
        "kimi-k2.7-code:cloud",
        "minimax-m2.7:cloud",
        "nemotron-3-super:cloud",
        "minimax-m3:cloud",
    ]


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
    ("rejected", "task", "snapshot", "error"),
    [
        (
            _candidate("unknown:cloud", expected_latency_ms=-1),
            _task(),
            _snapshot(),
            "expected_latency_invalid",
        ),
        (
            _candidate("minimax-m2.7:cloud", retry_rate_millis=float("nan")),
            _task(),
            parse_inventory({"models": []}, refreshed_at=100, ttl_seconds=300),
            "retry_rate_invalid",
        ),
        (
            _candidate("nemotron-3-super:cloud", escalation_rate_millis=-1),
            _task(category=TaskCategory.ISOLATED_CODE),
            _snapshot(),
            "escalation_rate_invalid",
        ),
        (
            _candidate("kimi-k2.7-code:cloud", quota_scarcity_millis=-1),
            _task(RiskTier.HIGH),
            _snapshot(),
            "quota_scarcity_invalid",
        ),
    ],
)
def test_rejected_candidate_evidence_remains_fail_closed_in_mixed_pool(
    rejected: CandidateMetrics,
    task: TaskProfile,
    snapshot: RegistrySnapshot,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=f"^{error}$"):
        rank_candidates(
            task,
            snapshot,
            (_candidate(), rejected),
            PolicyGates(),
        )


@pytest.mark.parametrize(
    ("category", "role", "capability", "expected_bonus"),
    [
        (TaskCategory.DOCUMENTATION, ModelRole.CODING_PRIMARY, "code", 100),
        (TaskCategory.DOCUMENTATION, ModelRole.CODING, "code", 40),
        (TaskCategory.ISOLATED_CODE, ModelRole.CODING_PRIMARY, "code", 100),
        (TaskCategory.ISOLATED_CODE, ModelRole.CODING, "code", 40),
        (TaskCategory.FEATURE, ModelRole.CODING_PRIMARY, "code", 100),
        (TaskCategory.FEATURE, ModelRole.CODING, "code", 40),
        (TaskCategory.FEATURE, ModelRole.AGENTIC, "code", 20),
        (TaskCategory.REFACTOR, ModelRole.CODING_PRIMARY, "code", 80),
        (TaskCategory.REFACTOR, ModelRole.CODING, "code", 40),
        (TaskCategory.REFACTOR, ModelRole.REASONING, "reasoning", 20),
        (TaskCategory.ARCHITECTURE, ModelRole.LONG_CONTEXT, "architecture", 80),
        (TaskCategory.ARCHITECTURE, ModelRole.REASONING, "architecture", 40),
    ],
)
def test_role_bonus_category_vectors_are_exact(
    monkeypatch,
    category: TaskCategory,
    role: ModelRole,
    capability: str,
    expected_bonus: int,
) -> None:
    model_id = "test-model:cloud"
    profile = RegistryProfile(
        profile=ModelProfile(
            model_id=model_id,
            profile_version="1",
            capabilities=(capability,),
            context_window_tokens=128_000,
            supported_efforts=(Effort.DEFAULT,),
            provisional=True,
        ),
        effort_payloads={Effort.DEFAULT: {}},
        evidence_url="https://ollama.com/library/test-model",
        evidence_accessed="2026-07-14",
        roles=(role,),
        usage_class=UsageClass.MEDIUM,
    )
    replacement = MappingProxyType({model_id: profile})
    monkeypatch.setattr(policy_module, "BUNDLED_PROFILES", replacement)
    monkeypatch.setattr(registry_module, "BUNDLED_PROFILES", replacement)
    snapshot = parse_inventory(
        {
            "models": [
                {
                    "name": model_id,
                    "model": model_id,
                    "digest": "e" * 64,
                    "details": {"context_length": 128_000},
                    "capabilities": [capability],
                }
            ]
        },
        refreshed_at=100,
        ttl_seconds=300,
    )

    result = rank_candidates(
        _task(category=category),
        snapshot,
        (_candidate(model_id),),
        PolicyGates(),
    )

    assert result.selected is not None
    assert dict(result.selected.components)["role_fit_bonus"] == expected_bonus
    assert result.selected.score == 667 + expected_bonus


def test_score_is_clamped_to_floor_and_ceiling() -> None:
    ceiling = rank_candidates(
        _task(),
        _snapshot(),
        (
            _candidate(
                global_success_millis=1_000,
                expected_input_tokens=0,
                expected_output_tokens=0,
                expected_latency_ms=0,
                retry_rate_millis=0,
                escalation_rate_millis=0,
                quota_scarcity_millis=0,
            ),
        ),
        PolicyGates(),
    )
    floor = rank_candidates(
        _task(),
        _snapshot(),
        (
            _candidate(
                global_success_millis=0,
                expected_input_tokens=20_000,
                expected_output_tokens=10_000,
                expected_latency_ms=200_000,
                retry_rate_millis=1_000,
                escalation_rate_millis=1_000,
                quota_scarcity_millis=1_000,
            ),
        ),
        PolicyGates(budget_tokens=100_000),
    )

    assert ceiling.selected is not None
    assert floor.selected is not None
    assert ceiling.selected.score == 1_000
    assert floor.selected.score == 0


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
