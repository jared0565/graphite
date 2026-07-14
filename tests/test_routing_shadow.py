"""Bounded, separately approved shadow-evaluation tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.routing.contracts import ApprovalManifest, Effort, RiskTier, TaskCategory
from graphite.routing.settings import RoutingSettings
from graphite.routing.shadow import (
    ShadowCandidate,
    create_blinded_comparison,
    make_shadow_manifest,
    plan_shadow,
    record_pairwise_verdict,
    reveal_comparison,
)
from graphite.routing.storage import RepositoryStore


def _store(tmp_path: Path) -> RepositoryStore:
    root = tmp_path / "repo"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    return store


def _settings(rate: int = 10, quota: int = 10_000) -> RoutingSettings:
    return RoutingSettings(shadow_rate_percent=rate, shadow_quota_tokens=quota)


def test_shadow_is_disabled_by_default(tmp_path: Path) -> None:
    assert plan_shadow(
        store=_store(tmp_path), primary_model="kimi-k2.7-code:cloud",
        primary_effort=Effort.DEFAULT,
        alternatives=(ShadowCandidate("kimi-k2.6:cloud", Effort.DEFAULT, 100, True),),
        risk=RiskTier.LOW, category=TaskCategory.FEATURE,
        settings=RoutingSettings(), now=10, randbelow=lambda _: 0,
    ) is None


@pytest.mark.parametrize(
    ("risk", "category"),
    [
        (RiskTier.HIGH, TaskCategory.FEATURE),
        (RiskTier.LOW, TaskCategory.AUTHENTICATION),
        (RiskTier.MEDIUM, TaskCategory.TENANT_ISOLATION),
        (RiskTier.LOW, TaskCategory.FINANCIAL),
    ],
)
def test_shadow_never_runs_for_high_or_sensitive_work(
    tmp_path: Path, risk: RiskTier, category: TaskCategory
) -> None:
    assert plan_shadow(
        store=_store(tmp_path), primary_model="kimi-k2.7-code:cloud",
        primary_effort=Effort.DEFAULT,
        alternatives=(ShadowCandidate("kimi-k2.6:cloud", Effort.DEFAULT, 100, True),),
        risk=risk, category=category, settings=_settings(), now=10,
        randbelow=lambda _: 0,
    ) is None


def test_shadow_requires_material_independently_eligible_alternative(tmp_path: Path) -> None:
    candidates = (
        ShadowCandidate("kimi-k2.7-code:cloud", Effort.DEFAULT, 100, True),
        ShadowCandidate("kimi-k2.6:cloud", Effort.DEFAULT, 100, False),
    )
    assert plan_shadow(
        store=_store(tmp_path), primary_model="kimi-k2.7-code:cloud",
        primary_effort=Effort.DEFAULT, alternatives=candidates, risk=RiskTier.LOW,
        category=TaskCategory.FEATURE, settings=_settings(), now=10,
        randbelow=lambda _: 0,
    ) is None


def test_shadow_rate_and_transactional_budget_are_both_enforced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate = ShadowCandidate("kimi-k2.6:cloud", Effort.DEFAULT, 60, True)
    assert plan_shadow(
        store=store, primary_model="kimi-k2.7-code:cloud",
        primary_effort=Effort.DEFAULT, alternatives=(candidate,), risk=RiskTier.LOW,
        category=TaskCategory.FEATURE, settings=_settings(rate=10, quota=100), now=10,
        randbelow=lambda _: 9,
    ) is not None
    assert plan_shadow(
        store=store, primary_model="kimi-k2.7-code:cloud",
        primary_effort=Effort.DEFAULT, alternatives=(candidate,), risk=RiskTier.LOW,
        category=TaskCategory.FEATURE, settings=_settings(rate=10, quota=100), now=11,
        randbelow=lambda _: 9,
    ) is None
    assert plan_shadow(
        store=store, primary_model="kimi-k2.7-code:cloud",
        primary_effort=Effort.DEFAULT, alternatives=(candidate,), risk=RiskTier.LOW,
        category=TaskCategory.FEATURE, settings=_settings(rate=10, quota=100), now=12,
        randbelow=lambda _: 10,
    ) is None


def _manifest() -> ApprovalManifest:
    return ApprovalManifest(
        approval_id="primary", task_id="task-1", decision_id="decision-1",
        graph_fingerprint="a" * 64, context_manifest_hash="b" * 64,
        model_id="kimi-k2.7-code:cloud", effort=Effort.DEFAULT,
        max_input_tokens=100, max_output_tokens=20, policy_version="1",
        issued_at=10, expires_at=100, nonce="primary-nonce",
    )


def test_shadow_manifest_has_independent_identity_nonce_and_quota() -> None:
    shadow = make_shadow_manifest(
        _manifest(), ShadowCandidate("kimi-k2.6:cloud", Effort.DEFAULT, 50, True),
        approval_id="shadow", issued_at=20, expires_at=80, nonce="shadow-nonce",
    )
    assert shadow.approval_id != "primary"
    assert shadow.nonce != "primary-nonce"
    assert shadow.model_id == "kimi-k2.6:cloud"
    assert shadow.max_input_tokens + shadow.max_output_tokens == 50


def test_pairwise_labels_hide_models_until_verdict_is_persisted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    comparison = create_blinded_comparison(
        store, comparison_id="comparison-1", primary_execution_id="exec-primary",
        shadow_execution_id="exec-shadow", primary_hash="a" * 64,
        shadow_hash="b" * 64, created_at=10, swap=True,
    )
    assert set(comparison.to_dict()) == {"comparison_id", "label_a_hash", "label_b_hash"}
    assert "model" not in str(comparison.to_dict())
    with pytest.raises(ValueError, match="pairwise_verdict_missing"):
        reveal_comparison(store, "comparison-1")
    record_pairwise_verdict(store, "comparison-1", "a", recorded_at=11)
    revealed = reveal_comparison(store, "comparison-1")
    assert revealed["winner_execution_id"] == "exec-shadow"
    assert revealed["provenance"] == "pairwise"
    assert revealed["autonomy_admissible"] is False
