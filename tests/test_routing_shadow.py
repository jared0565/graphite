"""Separate cross-provider review authority tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.routing.contracts import (
    CapabilityProfile,
    CapabilitySnapshot,
    CliApprovalManifest,
    CliIdentity,
    Effort,
    PermissionMode,
    ProviderId,
    RiskTier,
)
from graphite.routing.shadow import (
    ReviewCandidate,
    create_blinded_comparison,
    make_review_manifest,
    plan_cross_provider_review,
    record_pairwise_verdict,
    reveal_comparison,
)
from graphite.routing.storage import RepositoryStore


def _snapshot(provider: ProviderId) -> CapabilitySnapshot:
    model = "sonnet" if provider is ProviderId.CLAUDE_CODE else "gpt-5.6-codex"
    effective = "claude-sonnet-4-6" if provider is ProviderId.CLAUDE_CODE else model
    version = "2.1.208" if provider is ProviderId.CLAUDE_CODE else "0.144.1"
    return CapabilitySnapshot(
        1,
        10,
        1_000,
        CliIdentity(provider, ("a" if provider is ProviderId.CLAUDE_CODE else "b") * 64, version, "1.0.0"),
        CapabilityProfile(
            provider,
            model,
            effective,
            "2026-07-18.1",
            ("code", "reasoning"),
            200_000,
            (Effort.HIGH,),
            RiskTier.HIGH,
            PermissionMode.READ_ONLY,
        ),
    )


def _primary() -> CliApprovalManifest:
    return CliApprovalManifest(
        approval_id="primary-approval",
        task_id="task-1",
        decision_id="primary-decision",
        provider=ProviderId.CODEX,
        requested_model="gpt-5.6-codex",
        effective_model="gpt-5.6-codex",
        effort=Effort.XHIGH,
        cli_executable_sha256="c" * 64,
        cli_version="0.144.1",
        adapter_protocol_version="1.0.0",
        capability_snapshot_digest="d" * 64,
        graph_fingerprint="e" * 64,
        context_manifest_hash="f" * 64,
        repository_commit="1" * 40,
        worktree_id="primary-worktree",
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        max_input_tokens=100,
        max_output_tokens=20,
        policy_version="3",
        issued_at=10,
        expires_at=100,
        nonce="primary-nonce",
    )


def test_review_is_only_planned_for_high_risk_other_provider() -> None:
    claude = ReviewCandidate(_snapshot(ProviderId.CLAUDE_CODE), 80, True)
    codex = ReviewCandidate(_snapshot(ProviderId.CODEX), 60, True)
    assert plan_cross_provider_review(
        primary_provider=ProviderId.CODEX,
        primary_diff_hash="1" * 64,
        candidates=(codex, claude),
        risk=RiskTier.MEDIUM,
    ) is None
    plan = plan_cross_provider_review(
        primary_provider=ProviderId.CODEX,
        primary_diff_hash="1" * 64,
        candidates=(codex, claude),
        risk=RiskTier.HIGH,
    )
    assert plan is not None
    assert plan.candidate.snapshot.profile.provider is ProviderId.CLAUDE_CODE
    assert plan.authority == "separate_single_use_approval_required"


def test_review_manifest_cannot_reuse_primary_authority() -> None:
    plan = plan_cross_provider_review(
        primary_provider=ProviderId.CODEX,
        primary_diff_hash="1" * 64,
        candidates=(ReviewCandidate(_snapshot(ProviderId.CLAUDE_CODE), 80, True),),
        risk=RiskTier.HIGH,
    )
    assert plan is not None
    review = make_review_manifest(
        _primary(),
        plan,
        approval_id="review-approval",
        decision_id="review-decision",
        worktree_id="review-worktree",
        issued_at=20,
        expires_at=80,
        nonce="review-nonce",
    )
    assert review.provider is ProviderId.CLAUDE_CODE
    assert review.permission_mode is PermissionMode.READ_ONLY
    assert review.context_manifest_hash == "1" * 64
    assert review.max_input_tokens + review.max_output_tokens == 80
    for field in ("approval_id", "decision_id", "worktree_id", "nonce"):
        assert getattr(review, field) != getattr(_primary(), field)
    with pytest.raises(ValueError, match="review_approval_not_independent"):
        make_review_manifest(
            _primary(),
            plan,
            approval_id="primary-approval",
            decision_id="review-decision",
            worktree_id="review-worktree",
            issued_at=20,
            expires_at=80,
            nonce="review-nonce",
        )


def test_pairwise_review_labels_remain_blinded_until_verdict(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    comparison = create_blinded_comparison(
        store,
        comparison_id="comparison-1",
        primary_execution_id="exec-primary",
        shadow_execution_id="exec-review",
        primary_hash="a" * 64,
        shadow_hash="b" * 64,
        created_at=10,
        swap=True,
    )
    assert "model" not in str(comparison.to_dict())
    with pytest.raises(ValueError, match="pairwise_verdict_missing"):
        reveal_comparison(store, "comparison-1")
    record_pairwise_verdict(store, "comparison-1", "a", recorded_at=11)
    revealed = reveal_comparison(store, "comparison-1")
    assert revealed["winner_execution_id"] == "exec-review"
    assert revealed["autonomy_admissible"] is False
