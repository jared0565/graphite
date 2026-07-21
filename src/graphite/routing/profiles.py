"""Requested CLI profiles and verified, short-lived capability snapshots."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from types import MappingProxyType
from collections.abc import Callable
from typing import Final, Mapping
from urllib.parse import urlparse

from .contracts import (
    CapabilityProfile,
    CapabilitySnapshot,
    CliIdentity,
    EvidenceProvenance,
    Effort,
    PermissionMode,
    ProviderId,
    RiskTier,
    TaskCategory,
)
from .storage import RepositoryStore, StorageError

MAX_VERIFIED_SNAPSHOTS: Final = 64
MIN_SNAPSHOT_TTL_SECONDS: Final = 60
MAX_SNAPSHOT_TTL_SECONDS: Final = 86_400
MAX_VERIFICATION_INPUT_TOKENS: Final = 262_144
MAX_VERIFICATION_OUTPUT_TOKENS: Final = 32_768
_EVIDENCE_HOSTS: Final = {
    ProviderId.CLAUDE_CODE: "platform.claude.com",
    ProviderId.CODEX: "developers.openai.com",
    ProviderId.OPENROUTER: "openrouter.ai",
    ProviderId.ZAI: "docs.z.ai",
}


class ProfileError(RuntimeError):
    """Stable requested-profile or verification failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RequestedProfile:
    provider: ProviderId
    requested_model: str
    supported_efforts: tuple[Effort, ...]
    evidence_url: str
    evidence_accessed: str
    provisional: bool = True

    def __post_init__(self) -> None:
        try:
            provider = ProviderId(self.provider)
        except (TypeError, ValueError) as exc:
            raise ProfileError("profile_provider_invalid") from exc
        if (
            not isinstance(self.requested_model, str)
            or not self.requested_model
            or any(character.isspace() for character in self.requested_model)
            or len(self.requested_model) > 128
        ):
            raise ProfileError("profile_model_invalid")
        try:
            efforts = tuple(Effort(item) for item in self.supported_efforts)
        except (TypeError, ValueError) as exc:
            raise ProfileError("profile_effort_invalid") from exc
        if not efforts or len(set(efforts)) != len(efforts):
            raise ProfileError("profile_effort_invalid")
        parsed = urlparse(self.evidence_url)
        allowed_host = _EVIDENCE_HOSTS[provider]
        if parsed.scheme != "https" or parsed.hostname != allowed_host or parsed.username:
            raise ProfileError("profile_evidence_invalid")
        try:
            accessed = date.fromisoformat(self.evidence_accessed)
        except (TypeError, ValueError) as exc:
            raise ProfileError("profile_evidence_invalid") from exc
        if accessed.isoformat() != self.evidence_accessed or not isinstance(self.provisional, bool):
            raise ProfileError("profile_evidence_invalid")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "supported_efforts", efforts)


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    effective_model: str
    capabilities: tuple[str, ...]
    context_window_tokens: int
    risk_ceiling: RiskTier
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        for value in (self.input_tokens, self.output_tokens):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProfileError("profile_verification_usage_invalid")


@dataclass(frozen=True, slots=True)
class EditSmokeEvidence:
    """Sanitized evidence from one approved, isolated edit smoke."""

    effective_model: str
    input_tokens: int
    output_tokens: int
    duration_ms: int
    diff_sha256: str
    changed_file_count: int
    changed_byte_count: int
    validation_outcome: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.effective_model, str)
            or not self.effective_model
            or len(self.effective_model) > 128
            or any(character.isspace() for character in self.effective_model)
            or not isinstance(self.diff_sha256, str)
            or len(self.diff_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.diff_sha256)
            or not isinstance(self.validation_outcome, str)
        ):
            raise ProfileError("profile_edit_smoke_evidence_invalid")
        for value, maximum in (
            (self.input_tokens, MAX_VERIFICATION_INPUT_TOKENS),
            (self.output_tokens, MAX_VERIFICATION_OUTPUT_TOKENS),
            (self.duration_ms, 86_400_000),
            (self.changed_file_count, 100_000),
            (self.changed_byte_count, 10**12),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= maximum
            ):
                raise ProfileError("profile_edit_smoke_evidence_invalid")


_CLAUDE_EFFORTS = (Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH, Effort.MAX)
_CLAUDE_EVIDENCE = "https://platform.claude.com/docs/en/build-with-claude/effort"
BUNDLED_REQUESTED_PROFILES: Final[Mapping[str, RequestedProfile]] = MappingProxyType(
    {
        f"claude-code/{model}": RequestedProfile(
            ProviderId.CLAUDE_CODE,
            model,
            _CLAUDE_EFFORTS,
            _CLAUDE_EVIDENCE,
            "2026-07-18",
        )
        for model in ("fable", "opus", "sonnet")
    }
)


def operator_codex_profile(
    *,
    model_id: str,
    supported_efforts: tuple[Effort, ...],
    evidence_url: str,
    evidence_accessed: str,
) -> RequestedProfile:
    """Create an operator-selected Codex request without claiming subscription access."""
    return RequestedProfile(
        ProviderId.CODEX,
        model_id,
        supported_efforts,
        evidence_url,
        evidence_accessed,
    )


def operator_openrouter_profile(
    *,
    model_id: str,
    supported_efforts: tuple[Effort, ...],
    evidence_url: str,
    evidence_accessed: str,
) -> RequestedProfile:
    """Create an operator-selected OpenRouter request without claiming key validity."""
    return RequestedProfile(
        ProviderId.OPENROUTER,
        model_id,
        supported_efforts,
        evidence_url,
        evidence_accessed,
    )


def operator_zai_profile(
    *,
    model_id: str,
    supported_efforts: tuple[Effort, ...],
    evidence_url: str,
    evidence_accessed: str,
) -> RequestedProfile:
    """Create an operator-selected z.ai request without claiming key validity."""
    return RequestedProfile(
        ProviderId.ZAI,
        model_id,
        supported_efforts,
        evidence_url,
        evidence_accessed,
    )


def create_capability_snapshot(
    *,
    requested: RequestedProfile,
    identity: CliIdentity,
    effective_model: str,
    effort: Effort,
    capabilities: tuple[str, ...],
    context_window_tokens: int,
    risk_ceiling: RiskTier,
    permission_mode: PermissionMode,
    verified_at: int,
    ttl_seconds: int,
) -> CapabilitySnapshot:
    """Construct evidence only after an adapter verified one approved no-edit call."""
    if not isinstance(requested, RequestedProfile) or not isinstance(identity, CliIdentity):
        raise ProfileError("profile_identity_invalid")
    if identity.provider is not requested.provider:
        raise ProfileError("profile_identity_invalid")
    try:
        normalized_effort = Effort(effort)
    except (TypeError, ValueError) as exc:
        raise ProfileError("profile_effort_unsupported") from exc
    if normalized_effort not in requested.supported_efforts:
        raise ProfileError("profile_effort_unsupported")
    if (
        isinstance(verified_at, bool)
        or not isinstance(verified_at, int)
        or verified_at < 0
        or isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not MIN_SNAPSHOT_TTL_SECONDS <= ttl_seconds <= MAX_SNAPSHOT_TTL_SECONDS
    ):
        raise ProfileError("profile_time_invalid")
    try:
        profile = CapabilityProfile(
            provider=requested.provider,
            requested_model=requested.requested_model,
            effective_model=effective_model,
            profile_version=f"{requested.evidence_accessed}.1",
            capabilities=capabilities,
            context_window_tokens=context_window_tokens,
            supported_efforts=(normalized_effort,),
            risk_ceiling=risk_ceiling,
            permission_mode=permission_mode,
        )
        return CapabilitySnapshot(
            1,
            verified_at,
            verified_at + ttl_seconds,
            identity,
            profile,
        )
    except ValueError as exc:
        raise ProfileError("profile_verification_invalid") from exc


def verify_approved_profile(
    *,
    requested: RequestedProfile,
    identity: CliIdentity,
    effort: Effort,
    verified_at: int,
    ttl_seconds: int,
    approval_granted: bool,
    max_input_tokens: int,
    max_output_tokens: int,
    verifier: Callable[[RequestedProfile, Effort], VerificationEvidence],
) -> CapabilitySnapshot:
    """Invoke one approved read-only adapter verification and bind its evidence."""
    if approval_granted is not True:
        raise ProfileError("profile_verification_approval_required")
    limits = (
        (max_input_tokens, MAX_VERIFICATION_INPUT_TOKENS),
        (max_output_tokens, MAX_VERIFICATION_OUTPUT_TOKENS),
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
        for value, maximum in limits
    ):
        raise ProfileError("profile_verification_budget_invalid")
    try:
        evidence = verifier(requested, Effort(effort))
    except ProfileError:
        raise
    except Exception:
        raise ProfileError("profile_verification_failed") from None
    if not isinstance(evidence, VerificationEvidence):
        raise ProfileError("profile_verification_invalid")
    if (
        evidence.input_tokens > max_input_tokens
        or evidence.output_tokens > max_output_tokens
        or evidence.input_tokens + evidence.output_tokens
        > max_input_tokens + max_output_tokens
    ):
        raise ProfileError("profile_verification_budget_exceeded")
    return create_capability_snapshot(
        requested=requested,
        identity=identity,
        effective_model=evidence.effective_model,
        effort=effort,
        capabilities=evidence.capabilities,
        context_window_tokens=evidence.context_window_tokens,
        risk_ceiling=evidence.risk_ceiling,
        permission_mode=PermissionMode.READ_ONLY,
        verified_at=verified_at,
        ttl_seconds=ttl_seconds,
    )


def verify_and_save_approved_profile(
    store: RepositoryStore,
    *,
    requested: RequestedProfile,
    identity: CliIdentity,
    effort: Effort,
    verified_at: int,
    ttl_seconds: int,
    approval_granted: bool,
    max_input_tokens: int,
    max_output_tokens: int,
    verifier: Callable[[RequestedProfile, Effort], VerificationEvidence],
    lifecycle_identity_digest: str | None = None,
) -> CapabilitySnapshot:
    """Persist authority only after identity, evidence, and usage validation pass."""
    snapshot = verify_approved_profile(
        requested=requested,
        identity=identity,
        effort=effort,
        verified_at=verified_at,
        ttl_seconds=ttl_seconds,
        approval_granted=approval_granted,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        verifier=verifier,
    )
    save_capability_snapshot(store, snapshot)
    if lifecycle_identity_digest is not None:
        try:
            store.save_lifecycle_snapshot_binding(
                capability_snapshot_digest=snapshot.digest,
                lifecycle_identity_digest=lifecycle_identity_digest,
                bound_at=verified_at,
            )
        except (StorageError, ValueError):
            raise ProfileError("profile_lifecycle_binding_failed") from None
    return snapshot


def verify_and_save_approved_edit_profile(
    store: RepositoryStore,
    *,
    verified_snapshot: CapabilitySnapshot,
    verified_at: int,
    ttl_seconds: int,
    approval_granted: bool,
    max_input_tokens: int,
    max_output_tokens: int,
    max_changed_files: int,
    max_changed_bytes: int,
    lifecycle_identity_digest: str,
    verifier: Callable[[CapabilitySnapshot], EditSmokeEvidence],
) -> CapabilitySnapshot:
    """Promote write authority only after one approved, validated edit smoke."""
    if approval_granted is not True:
        raise ProfileError("profile_edit_smoke_approval_required")
    if (
        not isinstance(store, RepositoryStore)
        or not isinstance(verified_snapshot, CapabilitySnapshot)
        or verified_snapshot.profile.permission_mode is not PermissionMode.READ_ONLY
    ):
        raise ProfileError("profile_edit_smoke_source_invalid")
    if (
        isinstance(verified_at, bool)
        or not isinstance(verified_at, int)
        or verified_at < verified_snapshot.verified_at
        or isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not MIN_SNAPSHOT_TTL_SECONDS <= ttl_seconds <= MAX_SNAPSHOT_TTL_SECONDS
    ):
        raise ProfileError("profile_time_invalid")
    if verified_snapshot.expires_at <= verified_at:
        raise ProfileError("profile_edit_smoke_source_expired")
    for value, maximum in (
        (max_input_tokens, MAX_VERIFICATION_INPUT_TOKENS),
        (max_output_tokens, MAX_VERIFICATION_OUTPUT_TOKENS),
        (max_changed_files, 64),
        (max_changed_bytes, 16 * 1024 * 1024),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise ProfileError("profile_edit_smoke_budget_invalid")
    try:
        source_binding = store.lifecycle_identity_binding(
            authority_kind="capability_snapshot",
            authority_id=verified_snapshot.digest,
        )
    except (StorageError, ValueError):
        raise ProfileError("profile_edit_smoke_source_unbound") from None
    if source_binding != lifecycle_identity_digest:
        raise ProfileError("profile_edit_smoke_source_unbound")
    try:
        evidence = verifier(verified_snapshot)
    except ProfileError:
        raise
    except Exception:
        raise ProfileError("profile_edit_smoke_failed") from None
    if not isinstance(evidence, EditSmokeEvidence):
        raise ProfileError("profile_edit_smoke_evidence_invalid")
    if evidence.effective_model != verified_snapshot.profile.effective_model:
        raise ProfileError("profile_edit_smoke_model_mismatch")
    if (
        evidence.input_tokens > max_input_tokens
        or evidence.output_tokens > max_output_tokens
        or evidence.input_tokens + evidence.output_tokens
        > max_input_tokens + max_output_tokens
    ):
        raise ProfileError("profile_edit_smoke_budget_exceeded")
    if evidence.changed_file_count < 1 or evidence.changed_byte_count < 1:
        raise ProfileError("profile_edit_smoke_diff_invalid")
    if (
        evidence.changed_file_count > max_changed_files
        or evidence.changed_byte_count > max_changed_bytes
    ):
        raise ProfileError("profile_edit_smoke_diff_exceeded")
    if evidence.validation_outcome != "passed":
        raise ProfileError("profile_edit_smoke_validation_failed")
    try:
        promoted_profile = replace(
            verified_snapshot.profile,
            permission_mode=PermissionMode.WORKSPACE_WRITE,
        )
        promoted = CapabilitySnapshot(
            verified_snapshot.schema_version,
            verified_at,
            verified_at + ttl_seconds,
            verified_snapshot.identity,
            promoted_profile,
        )
        telemetry = {
            "provider": promoted.profile.provider.value,
            "requested_model": promoted.profile.requested_model,
            "effective_model": promoted.profile.effective_model,
            "effort": promoted.profile.supported_efforts[0].value,
            "capability_snapshot_digest": promoted.digest,
            "category": TaskCategory.ISOLATED_CODE.value,
            "risk": RiskTier.LOW.value,
            "latency_ms": evidence.duration_ms,
            "input_tokens": evidence.input_tokens,
            "output_tokens": evidence.output_tokens,
            "cost_status": "unknown",
            "changed_file_count": evidence.changed_file_count,
            "changed_byte_count": evidence.changed_byte_count,
            "validation_outcome": evidence.validation_outcome,
            "diff_sha256": evidence.diff_sha256,
            "review_defect_classes": [],
            "rework_count": 0,
            "human_verdict": None,
            "provenance": EvidenceProvenance.MACHINE_VERIFIED.value,
            "observed_at": verified_at,
        }
        store.promote_bound_capability_snapshot_record(
            source_snapshot_digest=verified_snapshot.digest,
            snapshot=promoted,
            lifecycle_identity_digest=lifecycle_identity_digest,
            bound_at=verified_at,
            telemetry_payload=telemetry,
        )
    except ProfileError:
        raise
    except StorageError as exc:
        code = (
            "profile_edit_smoke_source_unbound"
            if exc.args == ("capability_promotion_source_invalid",)
            else "profile_edit_smoke_persistence_failed"
        )
        raise ProfileError(code) from None
    except (TypeError, ValueError):
        raise ProfileError("profile_edit_smoke_persistence_failed") from None
    return promoted


def _snapshot_from_dict(value: object) -> CapabilitySnapshot:
    try:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "verified_at",
            "expires_at",
            "identity",
            "profile",
        }:
            raise ValueError
        identity_value = value["identity"]
        profile_value = value["profile"]
        if not isinstance(identity_value, dict) or not isinstance(profile_value, dict):
            raise ValueError
        identity = CliIdentity(**identity_value)
        profile = CapabilityProfile(**profile_value)
        return CapabilitySnapshot(
            value["schema_version"],
            value["verified_at"],
            value["expires_at"],
            identity,
            profile,
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise StorageError("storage_corrupt") from exc


def save_capability_snapshot(store: RepositoryStore, snapshot: CapabilitySnapshot) -> bool:
    if not isinstance(snapshot, CapabilitySnapshot):
        raise ProfileError("profile_snapshot_invalid")
    return store.save_capability_snapshot_record(snapshot)


def load_verified_capability_snapshots(
    store: RepositoryStore,
    *,
    now: int,
    active_lifecycle_identity_digests: frozenset[str] | None = None,
) -> tuple[CapabilitySnapshot, ...]:
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise ProfileError("profile_time_invalid")
    snapshots: list[CapabilitySnapshot] = []
    for row in store.capability_snapshot_records(limit=MAX_VERIFIED_SNAPSHOTS):
        try:
            snapshot = _snapshot_from_dict(row["payload"])
        except StorageError:
            raise
        if snapshot.digest != row["digest"]:
            raise StorageError("storage_corrupt")
        lifecycle_eligible = True
        if active_lifecycle_identity_digests is not None:
            try:
                binding = store.lifecycle_identity_binding(
                    authority_kind="capability_snapshot", authority_id=snapshot.digest
                )
            except (StorageError, ValueError):
                raise ProfileError("profile_lifecycle_binding_failed") from None
            lifecycle_eligible = binding in active_lifecycle_identity_digests
        if snapshot.expires_at > now and lifecycle_eligible:
            snapshots.append(snapshot)
    return tuple(sorted(snapshots, key=lambda item: item.digest))
