"""Deterministic eligibility, ranking, and promotion-confidence policy."""
from __future__ import annotations

import math
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Final

from .contracts import (
    CapabilitySnapshot,
    CliIdentity,
    Effort,
    PermissionMode,
    ProviderId,
    RiskTier,
    TaskCategory,
    TaskProfile,
)
from .registry import (
    BUNDLED_PROFILES,
    ModelRole,
    RegistrySnapshot,
    UsageClass,
    lifecycle_is_eligible,
    profile_is_eligible,
)

POLICY_VERSION: Final = "2"
EVIDENCE_VERSION: Final = "1"
_Z_95: Final = 1.959963984540054

_ROLE_BONUSES: Final = {
    TaskCategory.DOCUMENTATION: {
        ModelRole.CODING_PRIMARY: 100,
        ModelRole.CODING: 40,
    },
    TaskCategory.ISOLATED_CODE: {
        ModelRole.CODING_PRIMARY: 100,
        ModelRole.CODING: 40,
    },
    TaskCategory.FEATURE: {
        ModelRole.CODING_PRIMARY: 100,
        ModelRole.CODING: 40,
        ModelRole.AGENTIC: 20,
    },
    TaskCategory.REFACTOR: {
        ModelRole.CODING_PRIMARY: 80,
        ModelRole.CODING: 40,
        ModelRole.REASONING: 20,
    },
    TaskCategory.ARCHITECTURE: {
        ModelRole.LONG_CONTEXT: 80,
        ModelRole.REASONING: 40,
    },
}
_USAGE_PENALTIES: Final = {
    UsageClass.MEDIUM: 0,
    UsageClass.HIGH: 40,
}
_CODE_CAPABILITIES: Final = frozenset({"code"})
_REFACTOR_CAPABILITIES: Final = frozenset({"code", "reasoning"})
_ARCHITECTURE_CAPABILITIES: Final = frozenset({"architecture"})
CLI_MINIMUM_LEARNING_SAMPLES: Final = 20
CLI_RECENCY_HALF_LIFE_DAYS: Final = 30
_LEARNED_PROVIDERS: Final = ("claude-code", "codex")
_LEARNED_RISK_CEILINGS: Final = {"claude-code": "medium", "codex": "medium"}
_LEARNED_PERMISSION_CEILING: Final = "workspace-write"


@dataclass(frozen=True)
class CandidateMetrics:
    model_id: str
    effort: Effort
    repository_success_millis: int | None
    global_success_millis: int
    expected_input_tokens: int
    expected_output_tokens: int
    expected_latency_ms: int
    retry_rate_millis: int
    escalation_rate_millis: int
    quota_scarcity_millis: int


@dataclass(frozen=True)
class _ValidatedCandidateMetrics:
    reliability: int
    input_tokens: int
    output_tokens: int
    latency: int
    retry: int
    escalation: int
    scarcity: int


@dataclass(frozen=True)
class PolicyGates:
    graph_valid: bool = True
    graph_fresh: bool = True
    registry_fresh: bool = True
    data_policy_allowed: bool = True
    storage_available: bool = True
    task_evaluated: bool = False
    context_tokens: int = 1_000
    budget_tokens: int = 10_000
    current_date: str = "2026-07-14"


@dataclass(frozen=True)
class ScoredCandidate:
    model_id: str
    effort: Effort
    score: int
    components: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class PolicyResult:
    selected: ScoredCandidate | None
    ranked: tuple[ScoredCandidate, ...]
    manual_handoff: bool
    recommended_channels: tuple[str, ...]
    reasons: tuple[str, ...]
    policy_version: str = POLICY_VERSION
    evidence_version: str = EVIDENCE_VERSION


@dataclass(frozen=True)
class PromotionEligibility:
    eligible: bool
    lower_bound: float
    reason: str
    required_samples: int | None
    required_lower_bound: float | None


@dataclass(frozen=True)
class CliCandidateMetrics:
    capability_snapshot_digest: str
    effort: Effort
    repository_success_millis: int | None
    global_success_millis: int
    sample_count: int
    expected_input_tokens: int
    expected_output_tokens: int
    expected_latency_ms: int
    quota_scarcity_millis: int
    success_count: int | None = None
    latest_observed_at: int | None = None
    cost_status: str = "unknown"


@dataclass(frozen=True)
class CliPolicyGates:
    graph_valid: bool = True
    graph_fresh: bool = True
    data_policy_allowed: bool = True
    storage_available: bool = True
    authenticated_providers: tuple[ProviderId, ...] = (
        ProviderId.CLAUDE_CODE,
        ProviderId.CODEX,
    )
    current_cli_identities: tuple[CliIdentity, ...] = ()
    permission_mode: PermissionMode = PermissionMode.WORKSPACE_WRITE
    context_tokens: int = 1_000
    budget_tokens: int = 10_000
    now: int = 150


@dataclass(frozen=True)
class ScoredCliCandidate:
    provider: ProviderId
    requested_model: str
    effective_model: str
    effort: Effort
    capability_snapshot_digest: str
    score: int
    components: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class CliPolicyResult:
    selected: ScoredCliCandidate | None
    ranked: tuple[ScoredCliCandidate, ...]
    manual_handoff: bool
    reasons: tuple[str, ...]
    policy_version: str = "3"


@dataclass(frozen=True)
class CliLearnedPolicy:
    policy_version: str
    parent_version: str | None
    scoring_weights: tuple[tuple[str, int], ...]
    minimum_samples: int = CLI_MINIMUM_LEARNING_SAMPLES
    recency_half_life_days: int = CLI_RECENCY_HALF_LIFE_DAYS
    source_policy_version: str = "3"

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not self.policy_version:
            raise ValueError("policy_version_invalid")
        if self.parent_version is not None and (
            not isinstance(self.parent_version, str) or not self.parent_version
        ):
            raise ValueError("parent_policy_version_invalid")
        if (
            not isinstance(self.scoring_weights, tuple)
            or not self.scoring_weights
            or len({name for name, _ in self.scoring_weights}) != len(self.scoring_weights)
        ):
            raise ValueError("policy_weights_invalid")
        allowed = {"reliability", "tokens", "latency", "scarcity", "effort", "confidence", "recency"}
        for name, value in self.scoring_weights:
            if name not in allowed:
                raise ValueError("policy_weights_invalid")
            _bounded_metric(value, "policy_weights_invalid", 1_000)
        if (
            isinstance(self.minimum_samples, bool)
            or not isinstance(self.minimum_samples, int)
            or not 20 <= self.minimum_samples <= 10_000
        ):
            raise ValueError("policy_minimum_samples_invalid")
        if (
            isinstance(self.recency_half_life_days, bool)
            or not isinstance(self.recency_half_life_days, int)
            or not 7 <= self.recency_half_life_days <= 365
        ):
            raise ValueError("policy_recency_invalid")

    def to_payload(self) -> dict[str, object]:
        """Return fixed guardrails plus tunable ranking weights only."""
        return {
            "schema_version": 1,
            "autonomy": False,
            "allowed_providers": list(_LEARNED_PROVIDERS),
            "permission_ceiling": _LEARNED_PERMISSION_CEILING,
            "risk_ceilings": dict(_LEARNED_RISK_CEILINGS),
            "scoring_weights": dict(sorted(self.scoring_weights)),
            "minimum_samples": self.minimum_samples,
            "recency_half_life_days": self.recency_half_life_days,
            "source_policy_version": self.source_policy_version,
        }


@dataclass(frozen=True)
class PolicyComparisonEvidence:
    baseline_version: str
    candidate_version: str
    evidence_hash: str
    sample_count: int
    score_delta_millis: int
    promotion_eligible: bool
    reason: str


def cli_policy_payload_hash(candidate: CliLearnedPolicy) -> str:
    payload = json.dumps(
        candidate.to_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sign_cli_policy(candidate: CliLearnedPolicy, evidence_hash: str, signing_key: bytes) -> str:
    if not isinstance(signing_key, bytes) or len(signing_key) < 32:
        raise ValueError("policy_signing_key_invalid")
    if not isinstance(evidence_hash, str) or len(evidence_hash) != 64 or any(
        character not in "0123456789abcdef" for character in evidence_hash
    ):
        raise ValueError("evidence_hash_invalid")
    signed = json.dumps(
        {
            "evidence_hash": evidence_hash,
            "parent_version": candidate.parent_version,
            "payload_hash": cli_policy_payload_hash(candidate),
            "policy_version": candidate.policy_version,
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return hmac.new(signing_key, signed, hashlib.sha256).hexdigest()


def compare_cli_policy_candidate(
    candidate: CliLearnedPolicy,
    *,
    evidence_hash: str,
    sample_count: int,
    baseline_score_millis: int,
    candidate_score_millis: int,
) -> PolicyComparisonEvidence:
    sample_count = _bounded_metric(sample_count, "sample_count_invalid")
    baseline = _bounded_metric(baseline_score_millis, "baseline_score_invalid", 1_000)
    proposed = _bounded_metric(candidate_score_millis, "candidate_score_invalid", 1_000)
    if len(evidence_hash) != 64 or any(c not in "0123456789abcdef" for c in evidence_hash):
        raise ValueError("evidence_hash_invalid")
    enough = sample_count >= candidate.minimum_samples
    improved = proposed > baseline
    return PolicyComparisonEvidence(
        candidate.source_policy_version,
        candidate.policy_version,
        evidence_hash,
        sample_count,
        proposed - baseline,
        enough and improved,
        "candidate_improves_with_sufficient_evidence"
        if enough and improved
        else "sample_size_insufficient" if not enough else "candidate_not_better",
    )


def _global_failures(gates: PolicyGates) -> list[str]:
    failures: list[str] = []
    for allowed, reason in (
        (gates.graph_valid, "graph_invalid"),
        (gates.graph_fresh, "graph_stale"),
        (gates.registry_fresh, "registry_stale"),
        (gates.data_policy_allowed, "data_policy_blocked"),
        (gates.storage_available, "storage_unavailable"),
    ):
        if not isinstance(allowed, bool) or not allowed:
            failures.append(reason)
    if (
        isinstance(gates.context_tokens, bool)
        or not isinstance(gates.context_tokens, int)
        or gates.context_tokens < 0
    ):
        failures.append("context_invalid")
    if (
        isinstance(gates.budget_tokens, bool)
        or not isinstance(gates.budget_tokens, int)
        or gates.budget_tokens < 0
    ):
        failures.append("budget_invalid")
    return failures


def _bounded_metric(value: object, code: str, maximum: int = 10**9) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(code)
    if value < 0 or value > maximum or int(value) != value:
        raise ValueError(code)
    return int(value)


def _validate_candidate_metrics(candidate: CandidateMetrics) -> _ValidatedCandidateMetrics:
    global_success = _bounded_metric(
        candidate.global_success_millis,
        "global_success_invalid",
        1_000,
    )
    repository_success = candidate.repository_success_millis
    reliability = (
        global_success
        if repository_success is None
        else _bounded_metric(repository_success, "repository_success_invalid", 1_000)
    )
    return _ValidatedCandidateMetrics(
        reliability=reliability,
        input_tokens=_bounded_metric(
            candidate.expected_input_tokens,
            "expected_input_tokens_invalid",
        ),
        output_tokens=_bounded_metric(
            candidate.expected_output_tokens,
            "expected_output_tokens_invalid",
        ),
        latency=_bounded_metric(candidate.expected_latency_ms, "expected_latency_invalid"),
        retry=_bounded_metric(candidate.retry_rate_millis, "retry_rate_invalid", 1_000),
        escalation=_bounded_metric(
            candidate.escalation_rate_millis,
            "escalation_rate_invalid",
            1_000,
        ),
        scarcity=_bounded_metric(
            candidate.quota_scarcity_millis,
            "quota_scarcity_invalid",
            1_000,
        ),
    )


def _accepted_capabilities(category: TaskCategory) -> frozenset[str]:
    if category is TaskCategory.ARCHITECTURE:
        return _ARCHITECTURE_CAPABILITIES
    if category is TaskCategory.REFACTOR:
        return _REFACTOR_CAPABILITIES
    return _CODE_CAPABILITIES


def _candidate_failure(
    task: TaskProfile,
    snapshot: RegistrySnapshot,
    candidate: CandidateMetrics,
    gates: PolicyGates,
) -> str | None:
    entry = BUNDLED_PROFILES.get(candidate.model_id)
    if entry is None:
        return "model_profile_missing"
    try:
        effort = Effort(candidate.effort)
    except (TypeError, ValueError):
        return "effort_unsupported"
    if effort not in entry.profile.supported_efforts:
        return "effort_unsupported"
    if not profile_is_eligible(candidate.model_id, task.risk):
        return "risk_ineligible"
    if not lifecycle_is_eligible(entry.retirement_date, gates.current_date):
        return "model_retiring"
    inventory_model = next(
        (model for model in snapshot.models if model.model_id == candidate.model_id),
        None,
    )
    if inventory_model is None:
        return "model_unavailable"
    expected_input = _bounded_metric(candidate.expected_input_tokens, "expected_input_tokens_invalid")
    expected_output = _bounded_metric(candidate.expected_output_tokens, "expected_output_tokens_invalid")
    context_limit = min(
        entry.profile.context_window_tokens,
        inventory_model.context_window_tokens or entry.profile.context_window_tokens,
    )
    if max(gates.context_tokens, expected_input) + expected_output > context_limit:
        return "context_exceeded"
    if expected_input + expected_output > gates.budget_tokens:
        return "budget_exceeded"
    if not gates.task_evaluated and not entry.profile.provisional:
        return "task_not_evaluated"
    accepted_capabilities = _accepted_capabilities(task.category)
    if accepted_capabilities.isdisjoint(entry.profile.capabilities):
        return "capability_missing"
    return None


def _score(
    candidate: CandidateMetrics,
    task: TaskProfile,
    metrics: _ValidatedCandidateMetrics,
) -> ScoredCandidate:
    entry = BUNDLED_PROFILES[candidate.model_id]
    token_penalty = min(250, (metrics.input_tokens + metrics.output_tokens) // 100)
    latency_penalty = min(200, metrics.latency // 1_000)
    retry_penalty = metrics.retry // 5
    escalation_penalty = metrics.escalation // 5
    scarcity_penalty = metrics.scarcity // 10
    role_bonuses = _ROLE_BONUSES.get(task.category, {})
    role_fit_bonus = max((role_bonuses.get(role, 0) for role in entry.roles), default=0)
    usage_penalty = _USAGE_PENALTIES[entry.usage_class]
    score = max(
        0,
        min(
            1_000,
            metrics.reliability
            + role_fit_bonus
            - usage_penalty
            - token_penalty
            - latency_penalty
            - retry_penalty
            - escalation_penalty
            - scarcity_penalty,
        ),
    )
    return ScoredCandidate(
        candidate.model_id,
        Effort(candidate.effort),
        score,
        (
            ("reliability", metrics.reliability),
            ("role_fit_bonus", role_fit_bonus),
            ("usage_penalty", usage_penalty),
            ("token_penalty", token_penalty),
            ("latency_penalty", latency_penalty),
            ("retry_penalty", retry_penalty),
            ("escalation_penalty", escalation_penalty),
            ("quota_scarcity_penalty", scarcity_penalty),
        ),
    )


def rank_candidates(
    task: TaskProfile,
    snapshot: RegistrySnapshot,
    candidates: tuple[CandidateMetrics, ...],
    gates: PolicyGates,
) -> PolicyResult:
    """Apply hard gates then rank eligible candidates deterministically."""
    global_failures = _global_failures(gates)
    if global_failures:
        return PolicyResult(
            None,
            (),
            True,
            ("claude_code", "codex"),
            tuple(global_failures),
        )
    scored: list[ScoredCandidate] = []
    rejected: list[str] = []
    for candidate in candidates:
        metrics = _validate_candidate_metrics(candidate)
        failure = _candidate_failure(task, snapshot, candidate, gates)
        if failure is not None:
            rejected.append(failure)
            continue
        scored.append(_score(candidate, task, metrics))
    ranked = tuple(
        sorted(scored, key=lambda item: (-item.score, item.model_id, item.effort.value))
    )
    if not ranked:
        return PolicyResult(
            None,
            (),
            True,
            ("claude_code", "codex"),
            tuple(sorted(set(rejected))) or ("no_candidates",),
        )
    return PolicyResult(
        ranked[0],
        ranked,
        False,
        (),
        ("eligible_ranked",),
    )


_RISK_ORDER: Final = {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2}
_EFFORT_PENALTY: Final = {
    Effort.DEFAULT: 30,
    Effort.LOW: 0,
    Effort.MEDIUM: 10,
    Effort.HIGH: 30,
    Effort.XHIGH: 60,
    Effort.MAX: 80,
}


def _cli_global_failures(gates: CliPolicyGates) -> list[str]:
    failures: list[str] = []
    for allowed, reason in (
        (gates.graph_valid, "graph_invalid"),
        (gates.graph_fresh, "graph_stale"),
        (gates.data_policy_allowed, "data_policy_blocked"),
        (gates.storage_available, "storage_unavailable"),
    ):
        if not isinstance(allowed, bool) or not allowed:
            failures.append(reason)
    try:
        providers = tuple(ProviderId(item) for item in gates.authenticated_providers)
        PermissionMode(gates.permission_mode)
    except (TypeError, ValueError):
        failures.append("policy_gate_invalid")
        providers = ()
    if len(set(providers)) != len(providers):
        failures.append("policy_gate_invalid")
    if (
        not isinstance(gates.current_cli_identities, tuple)
        or any(not isinstance(item, CliIdentity) for item in gates.current_cli_identities)
        or len({item.provider for item in gates.current_cli_identities})
        != len(gates.current_cli_identities)
    ):
        failures.append("policy_gate_invalid")
    for value, reason in (
        (gates.context_tokens, "context_invalid"),
        (gates.budget_tokens, "budget_invalid"),
        (gates.now, "policy_time_invalid"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            failures.append(reason)
    if failures:
        return failures
    return failures


def rank_cli_candidates(
    task: TaskProfile,
    snapshots: tuple[CapabilitySnapshot, ...],
    candidates: tuple[CliCandidateMetrics, ...],
    gates: CliPolicyGates,
) -> CliPolicyResult:
    """Apply provider-neutral hard gates before deterministic CLI profile scoring."""
    failures = _cli_global_failures(gates)
    if failures:
        return CliPolicyResult(None, (), True, tuple(sorted(set(failures))))
    snapshot_by_digest: dict[str, CapabilitySnapshot] = {}
    for entry in snapshots:
        if not isinstance(entry, CapabilitySnapshot) or entry.digest in snapshot_by_digest:
            raise ValueError("capability_snapshot_invalid")
        snapshot_by_digest[entry.digest] = entry
    authenticated = {ProviderId(item) for item in gates.authenticated_providers}
    current_identities = {item.provider: item for item in gates.current_cli_identities}
    required_permission = PermissionMode(gates.permission_mode)
    scored: list[ScoredCliCandidate] = []
    rejected: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, CliCandidateMetrics):
            raise ValueError("candidate_invalid")
        snapshot = snapshot_by_digest.get(candidate.capability_snapshot_digest)
        if snapshot is None:
            rejected.append("capability_snapshot_missing")
            continue
        profile = snapshot.profile
        if snapshot.expires_at <= gates.now:
            rejected.append("capability_snapshot_expired")
            continue
        if profile.provider not in authenticated:
            rejected.append("provider_unauthenticated")
            continue
        if current_identities.get(profile.provider) != snapshot.identity:
            rejected.append("cli_identity_changed")
            continue
        if profile.permission_mode is not required_permission:
            rejected.append("permission_mismatch")
            continue
        try:
            effort = Effort(candidate.effort)
        except (TypeError, ValueError):
            rejected.append("effort_unsupported")
            continue
        if effort not in profile.supported_efforts:
            rejected.append("effort_unsupported")
            continue
        if _RISK_ORDER[task.risk] > _RISK_ORDER[profile.risk_ceiling]:
            rejected.append("risk_ineligible")
            continue
        input_tokens = _bounded_metric(
            candidate.expected_input_tokens, "expected_input_tokens_invalid"
        )
        output_tokens = _bounded_metric(
            candidate.expected_output_tokens, "expected_output_tokens_invalid"
        )
        if max(gates.context_tokens, input_tokens) + output_tokens > profile.context_window_tokens:
            rejected.append("context_exceeded")
            continue
        if input_tokens + output_tokens > gates.budget_tokens:
            rejected.append("budget_exceeded")
            continue
        if _accepted_capabilities(task.category).isdisjoint(profile.capabilities):
            rejected.append("capability_missing")
            continue
        sample_count = _bounded_metric(candidate.sample_count, "sample_count_invalid")
        if candidate.cost_status != "unknown":
            raise ValueError("cost_status_invalid")
        global_success = _bounded_metric(
            candidate.global_success_millis, "global_success_invalid", 1_000
        )
        repository_success = candidate.repository_success_millis
        reliability = global_success
        if sample_count >= 5 and repository_success is not None:
            reliability = _bounded_metric(
                repository_success, "repository_success_invalid", 1_000
            )
        if candidate.success_count is None:
            confidence_penalty = 150
        else:
            successes = _bounded_metric(candidate.success_count, "success_count_invalid")
            if successes > sample_count:
                raise ValueError("success_count_invalid")
            confidence_penalty = (
                150
                if sample_count < CLI_MINIMUM_LEARNING_SAMPLES
                else min(200, round((1 - wilson_lower_bound(successes, sample_count)) * 200))
            )
        if candidate.latest_observed_at is None:
            recency_penalty = 50
        else:
            latest = _bounded_metric(candidate.latest_observed_at, "latest_observed_at_invalid")
            if latest > gates.now:
                raise ValueError("latest_observed_at_invalid")
            age_days = (gates.now - latest) // 86_400
            recency_penalty = min(100, age_days * 2)
        latency = _bounded_metric(candidate.expected_latency_ms, "expected_latency_invalid")
        scarcity = _bounded_metric(
            candidate.quota_scarcity_millis, "quota_scarcity_invalid", 1_000
        )
        token_penalty = min(250, (input_tokens + output_tokens) // 100)
        latency_penalty = min(200, latency // 1_000)
        scarcity_penalty = scarcity // 10
        effort_penalty = _EFFORT_PENALTY[effort]
        cost_unknown_penalty = 25
        score = max(
            0,
            min(
                1_000,
                reliability
                - token_penalty
                - latency_penalty
                - scarcity_penalty
                - effort_penalty
                - confidence_penalty
                - recency_penalty
                - cost_unknown_penalty,
            ),
        )
        scored.append(
            ScoredCliCandidate(
                profile.provider,
                profile.requested_model,
                profile.effective_model,
                effort,
                snapshot.digest,
                score,
                (
                    ("reliability", reliability),
                    ("token_penalty", token_penalty),
                    ("latency_penalty", latency_penalty),
                    ("quota_scarcity_penalty", scarcity_penalty),
                    ("effort_penalty", effort_penalty),
                    ("confidence_penalty", confidence_penalty),
                    ("recency_penalty", recency_penalty),
                    ("cost_unknown_penalty", cost_unknown_penalty),
                ),
            )
        )
    ranked = tuple(
        sorted(
            scored,
            key=lambda item: (
                -item.score,
                item.provider.value,
                item.effective_model,
                item.effort.value,
                item.capability_snapshot_digest,
            ),
        )
    )
    if not ranked:
        return CliPolicyResult(
            None,
            (),
            True,
            tuple(sorted(set(rejected))) or ("no_candidates",),
        )
    return CliPolicyResult(ranked[0], ranked, False, ("eligible_ranked",))


def wilson_lower_bound(successes: int, total: int) -> float:
    """Return the two-sided 95% Wilson score lower bound."""
    if (
        isinstance(successes, bool)
        or isinstance(total, bool)
        or not isinstance(successes, int)
        or not isinstance(total, int)
        or successes < 0
        or total < 0
        or successes > total
    ):
        raise ValueError("confidence_counts_invalid")
    if total == 0:
        return 0.0
    proportion = successes / total
    z_squared = _Z_95 * _Z_95
    denominator = 1 + z_squared / total
    center = proportion + z_squared / (2 * total)
    margin = _Z_95 * math.sqrt(
        (proportion * (1 - proportion) + z_squared / (4 * total)) / total
    )
    return max(0.0, (center - margin) / denominator)


def promotion_eligibility(
    risk: RiskTier | str,
    *,
    successes: int,
    total: int,
    severe_failures: int,
) -> PromotionEligibility:
    """Evaluate evidence only; this function never grants execution authority."""
    normalized = RiskTier(risk)
    lower = wilson_lower_bound(successes, total)
    if (
        isinstance(severe_failures, bool)
        or not isinstance(severe_failures, int)
        or severe_failures < 0
        or severe_failures > total
    ):
        raise ValueError("severe_failure_count_invalid")
    if normalized is RiskTier.HIGH:
        return PromotionEligibility(
            False,
            lower,
            "high_risk_permanent_gate",
            None,
            None,
        )
    required_samples, required_lower = (
        (50, 0.90) if normalized is RiskTier.LOW else (100, 0.95)
    )
    if severe_failures:
        return PromotionEligibility(
            False,
            lower,
            "severe_failure_open",
            required_samples,
            required_lower,
        )
    if total < required_samples:
        return PromotionEligibility(
            False,
            lower,
            "sample_size_insufficient",
            required_samples,
            required_lower,
        )
    if lower < required_lower:
        return PromotionEligibility(
            False,
            lower,
            "confidence_insufficient",
            required_samples,
            required_lower,
        )
    return PromotionEligibility(
        True,
        lower,
        "evidence_threshold_met",
        required_samples,
        required_lower,
    )
