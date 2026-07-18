"""Deterministic, separately authorized cross-provider review policy."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, replace

from .contracts import (
    CapabilitySnapshot,
    CliApprovalManifest,
    PermissionMode,
    ProviderId,
    RiskTier,
)
from .storage import RepositoryStore


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    snapshot: CapabilitySnapshot
    reserved_tokens: int
    independently_eligible: bool

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, CapabilitySnapshot):
            raise ValueError("review_snapshot_invalid")
        if self.snapshot.profile.permission_mode is not PermissionMode.READ_ONLY:
            raise ValueError("review_permission_invalid")
        if (
            isinstance(self.reserved_tokens, bool)
            or not isinstance(self.reserved_tokens, int)
            or not 2 <= self.reserved_tokens <= 294_912
        ):
            raise ValueError("reserved_tokens_invalid")
        if not isinstance(self.independently_eligible, bool):
            raise ValueError("eligibility_invalid")


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    candidate: ReviewCandidate
    primary_diff_hash: str
    authority: str = "separate_single_use_approval_required"


@dataclass(frozen=True, slots=True)
class BlindedComparison:
    comparison_id: str
    label_a_hash: str
    label_b_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "comparison_id": self.comparison_id,
            "label_a_hash": self.label_a_hash,
            "label_b_hash": self.label_b_hash,
        }


def plan_cross_provider_review(
    *,
    primary_provider: ProviderId,
    primary_diff_hash: str,
    candidates: tuple[ReviewCandidate, ...],
    risk: RiskTier,
) -> ReviewPlan | None:
    """Select one other-provider reviewer only for high-risk primary work."""
    provider = ProviderId(primary_provider)
    if RiskTier(risk) is not RiskTier.HIGH:
        return None
    if (
        not isinstance(primary_diff_hash, str)
        or len(primary_diff_hash) != 64
        or any(character not in "0123456789abcdef" for character in primary_diff_hash)
    ):
        raise ValueError("primary_diff_hash_invalid")
    eligible = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.independently_eligible
            and candidate.snapshot.profile.provider is not provider
        ),
        key=lambda item: (
            item.reserved_tokens,
            item.snapshot.profile.provider.value,
            item.snapshot.profile.requested_model,
            item.snapshot.profile.supported_efforts[0].value,
            item.snapshot.digest,
        ),
    )
    return None if not eligible else ReviewPlan(eligible[0], primary_diff_hash)


def make_review_manifest(
    primary: CliApprovalManifest,
    plan: ReviewPlan,
    *,
    approval_id: str,
    decision_id: str,
    worktree_id: str,
    issued_at: int,
    expires_at: int,
    nonce: str,
) -> CliApprovalManifest:
    """Derive a separately signable read-only manifest without primary authority reuse."""
    snapshot = plan.candidate.snapshot
    if (
        approval_id == primary.approval_id
        or decision_id == primary.decision_id
        or worktree_id == primary.worktree_id
        or nonce == primary.nonce
        or snapshot.profile.provider is primary.provider
    ):
        raise ValueError("review_approval_not_independent")
    output = min(primary.max_output_tokens, plan.candidate.reserved_tokens - 1)
    input_tokens = plan.candidate.reserved_tokens - output
    return replace(
        primary,
        approval_id=approval_id,
        decision_id=decision_id,
        provider=snapshot.profile.provider,
        requested_model=snapshot.profile.requested_model,
        effective_model=snapshot.profile.effective_model,
        effort=snapshot.profile.supported_efforts[0],
        cli_executable_sha256=snapshot.identity.executable_sha256,
        cli_version=snapshot.identity.cli_version,
        adapter_protocol_version=snapshot.identity.adapter_protocol_version,
        capability_snapshot_digest=snapshot.digest,
        context_manifest_hash=plan.primary_diff_hash,
        worktree_id=worktree_id,
        permission_mode=PermissionMode.READ_ONLY,
        max_input_tokens=input_tokens,
        max_output_tokens=output,
        policy_version="review-1",
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
    )


def create_blinded_comparison(
    store: RepositoryStore,
    *,
    comparison_id: str,
    primary_execution_id: str,
    shadow_execution_id: str,
    primary_hash: str,
    shadow_hash: str,
    created_at: int,
    swap: bool | None = None,
) -> BlindedComparison:
    label_a_is_shadow = bool(secrets.randbelow(2)) if swap is None else bool(swap)
    label_a_hash, label_b_hash = (
        (shadow_hash, primary_hash)
        if label_a_is_shadow
        else (primary_hash, shadow_hash)
    )
    store.create_blind_comparison(
        comparison_id=comparison_id,
        primary_execution_id=primary_execution_id,
        shadow_execution_id=shadow_execution_id,
        label_a_hash=label_a_hash,
        label_b_hash=label_b_hash,
        label_a_is_shadow=label_a_is_shadow,
        created_at=created_at,
    )
    return BlindedComparison(comparison_id, label_a_hash, label_b_hash)


def record_pairwise_verdict(
    store: RepositoryStore, comparison_id: str, verdict: str, *, recorded_at: int
) -> None:
    if not store.record_blind_verdict(comparison_id, verdict, recorded_at):
        raise ValueError("pairwise_verdict_invalid")


def reveal_comparison(store: RepositoryStore, comparison_id: str) -> dict[str, object]:
    row = store.blind_comparison(comparison_id)
    if row is None:
        raise ValueError("pairwise_comparison_missing")
    verdict = row["verdict"]
    if verdict is None:
        raise ValueError("pairwise_verdict_missing")
    winner: str | None = None
    if verdict in {"a", "b"}:
        a_is_shadow = bool(row["label_a_is_shadow"])
        winner_is_shadow = a_is_shadow if verdict == "a" else not a_is_shadow
        winner = str(
            row["shadow_execution_id"]
            if winner_is_shadow
            else row["primary_execution_id"]
        )
    return {
        "comparison_id": comparison_id,
        "verdict": verdict,
        "winner_execution_id": winner,
        "primary_execution_id": row["primary_execution_id"],
        "shadow_execution_id": row["shadow_execution_id"],
        "provenance": "pairwise",
        "autonomy_admissible": False,
    }
