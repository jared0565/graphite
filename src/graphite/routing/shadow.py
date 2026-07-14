"""Explicitly budgeted shadow selection and blinded pairwise evidence."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, replace
from typing import Callable, Final

from .contracts import ApprovalManifest, Effort, RiskTier, TaskCategory
from .settings import RoutingSettings
from .storage import RepositoryStore

_SENSITIVE_CATEGORIES: Final = frozenset(
    {
        TaskCategory.AUTHENTICATION,
        TaskCategory.AUTHORIZATION,
        TaskCategory.TENANT_ISOLATION,
        TaskCategory.MIGRATION,
        TaskCategory.DEPLOYMENT,
        TaskCategory.INFRASTRUCTURE,
        TaskCategory.CONCURRENCY,
        TaskCategory.FINANCIAL,
        TaskCategory.LEGAL,
    }
)


@dataclass(frozen=True)
class ShadowCandidate:
    model_id: str
    effort: Effort
    reserved_tokens: int
    independently_eligible: bool

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id or any(
            character.isspace() for character in self.model_id
        ):
            raise ValueError("model_id_invalid")
        object.__setattr__(self, "effort", Effort(self.effort))
        if (
            isinstance(self.reserved_tokens, bool)
            or not isinstance(self.reserved_tokens, int)
            or not 2 <= self.reserved_tokens <= 294_912
        ):
            raise ValueError("reserved_tokens_invalid")
        if not isinstance(self.independently_eligible, bool):
            raise ValueError("eligibility_invalid")


@dataclass(frozen=True)
class ShadowPlan:
    candidate: ShadowCandidate
    budget_entry_id: str
    incremental_tokens: int


@dataclass(frozen=True)
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


def plan_shadow(
    *,
    store: RepositoryStore,
    primary_model: str,
    primary_effort: Effort,
    alternatives: tuple[ShadowCandidate, ...],
    risk: RiskTier,
    category: TaskCategory,
    settings: RoutingSettings,
    now: int,
    randbelow: Callable[[int], int] = secrets.randbelow,
) -> ShadowPlan | None:
    """Select at most one independently eligible, materially different shadow."""
    rate = settings.shadow_rate_percent
    if rate == 0 or RiskTier(risk) is RiskTier.HIGH or TaskCategory(category) in _SENSITIVE_CATEGORIES:
        return None
    draw = randbelow(100)
    if isinstance(draw, bool) or not isinstance(draw, int) or not 0 <= draw < 100:
        raise ValueError("shadow_random_invalid")
    if draw >= rate:
        return None
    normalized_effort = Effort(primary_effort)
    eligible = sorted(
        (
            candidate for candidate in alternatives
            if candidate.independently_eligible
            and (candidate.model_id != primary_model or candidate.effort is not normalized_effort)
        ),
        key=lambda item: (item.reserved_tokens, item.model_id, item.effort.value),
    )
    if not eligible:
        return None
    candidate = eligible[0]
    entry_id = f"shadow:{secrets.token_hex(16)}"
    if not store.reserve_shadow_budget(
        entry_id, candidate.reserved_tokens, settings.shadow_quota_tokens, now
    ):
        return None
    return ShadowPlan(candidate, entry_id, candidate.reserved_tokens)


def make_shadow_manifest(
    primary: ApprovalManifest,
    candidate: ShadowCandidate,
    *,
    approval_id: str,
    issued_at: int,
    expires_at: int,
    nonce: str,
) -> ApprovalManifest:
    """Create a separately signable approval; never reuse primary identity or quota."""
    if approval_id == primary.approval_id or nonce == primary.nonce:
        raise ValueError("shadow_approval_not_independent")
    output = min(primary.max_output_tokens, candidate.reserved_tokens - 1)
    input_tokens = candidate.reserved_tokens - output
    return replace(
        primary,
        approval_id=approval_id,
        model_id=candidate.model_id,
        effort=candidate.effort,
        max_input_tokens=input_tokens,
        max_output_tokens=output,
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
        (shadow_hash, primary_hash) if label_a_is_shadow else (primary_hash, shadow_hash)
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
    store: RepositoryStore,
    comparison_id: str,
    verdict: str,
    *,
    recorded_at: int,
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
            row["shadow_execution_id"] if winner_is_shadow else row["primary_execution_id"]
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
