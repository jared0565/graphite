"""Requested CLI profiles and verified, short-lived capability snapshots."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from collections.abc import Callable
from typing import Final, Mapping
from urllib.parse import urlparse

from .contracts import (
    CapabilityProfile,
    CapabilitySnapshot,
    CliIdentity,
    Effort,
    PermissionMode,
    ProviderId,
    RiskTier,
)
from .storage import RepositoryStore, StorageError

MAX_VERIFIED_SNAPSHOTS: Final = 64
MIN_SNAPSHOT_TTL_SECONDS: Final = 60
MAX_SNAPSHOT_TTL_SECONDS: Final = 86_400
MAX_VERIFICATION_INPUT_TOKENS: Final = 262_144
MAX_VERIFICATION_OUTPUT_TOKENS: Final = 32_768


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
        allowed_host = (
            "platform.claude.com"
            if provider is ProviderId.CLAUDE_CODE
            else "developers.openai.com"
        )
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
    return snapshot


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
        if snapshot.expires_at > now:
            snapshots.append(snapshot)
    return tuple(sorted(snapshots, key=lambda item: item.digest))
