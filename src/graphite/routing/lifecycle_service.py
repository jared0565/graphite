"""Fail-closed coordination for provider lifecycle execution authority."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import ClassVar

from .contracts import Effort, PublicRecord
from .lifecycle import (
    IdentityChange,
    LifecycleReasonCode,
    ProviderCompatibilityAssessment,
    ProviderCompatibilityPolicy,
    ProviderLifecycleEvent,
    ProviderLifecycleState,
    ProviderRuntimeIdentity,
    assess_identity_change,
)
from .lifecycle_storage import CurrentLifecycleObservation, LifecycleStore, LifecycleStorageError
from .storage import RepositoryStore, StorageError
from .route_pool import ApprovedRouteCandidate, RouteAuthority


class LifecycleServiceError(RuntimeError):
    """Stable lifecycle authority failure without provider diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LifecycleObservationResult(PublicRecord):
    lifecycle_identity_digest: str
    change: IdentityChange
    state: ProviderLifecycleState
    reason: LifecycleReasonCode
    invalidated_snapshots: tuple[str, ...]
    invalidated_approvals: tuple[str, ...]
    carried_snapshots: tuple[str, ...]

    _public_fields: ClassVar[tuple[str, ...]] = (
        "lifecycle_identity_digest", "change", "state", "reason",
        "invalidated_snapshots", "invalidated_approvals", "carried_snapshots",
    )


@dataclass(frozen=True, slots=True)
class ExecutionLifecycleAuthority(PublicRecord):
    lifecycle_identity_digest: str
    capability_snapshot_digest: str
    approval_id: str
    state: ProviderLifecycleState

    _public_fields: ClassVar[tuple[str, ...]] = (
        "lifecycle_identity_digest", "capability_snapshot_digest", "approval_id", "state"
    )


@dataclass(frozen=True, slots=True)
class VerificationManifest(PublicRecord):
    provider: str
    runtime_kind: str
    lifecycle_identity_digest: str
    runtime_version: str
    runtime_digest: str
    model_identity_digest: str | None
    routing_policy_digest: str | None
    policy_version: str
    requested_model: str
    expected_effective_model: str
    effort: str
    permission_mode: str
    max_input_tokens: int
    max_output_tokens: int
    timeout_seconds: int
    expires_at: int
    fixture_repository_commit: str
    graph_fingerprint: str
    prompt_contract_hash: str
    response_contract_hash: str
    max_cost_microunits: int | None
    max_attempts: int
    fallback_enabled: bool
    no_resume: bool
    no_substitution: bool
    evidence_fields: tuple[str, ...]

    _public_fields: ClassVar[tuple[str, ...]] = (
        "provider", "runtime_kind", "lifecycle_identity_digest", "runtime_version",
        "runtime_digest", "model_identity_digest", "routing_policy_digest",
        "policy_version", "requested_model", "expected_effective_model", "effort",
        "permission_mode", "max_input_tokens", "max_output_tokens", "timeout_seconds",
        "expires_at",
        "fixture_repository_commit", "graph_fingerprint", "prompt_contract_hash",
        "response_contract_hash", "max_cost_microunits", "max_attempts",
        "fallback_enabled", "no_resume", "no_substitution", "evidence_fields",
    )

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_verification_manifest(
    current: CurrentLifecycleObservation,
    *,
    requested_model: str,
    expected_effective_model: str,
    effort: Effort,
    max_input_tokens: int,
    max_output_tokens: int,
    timeout_seconds: int,
    expires_at: int,
    fixture_repository_commit: str,
    graph_fingerprint: str,
    prompt_contract_hash: str,
    response_contract_hash: str,
    max_cost_microunits: int | None = None,
) -> VerificationManifest:
    """Build a bounded manifest from one already-read exact lifecycle observation."""
    try:
        normalized_effort = Effort(effort)
    except (TypeError, ValueError):
        raise LifecycleServiceError("verification_manifest_invalid") from None
    values = (max_input_tokens, max_output_tokens, timeout_seconds, expires_at)
    digests = (graph_fingerprint, prompt_contract_hash, response_contract_hash)
    models = (requested_model, expected_effective_model)
    safe_models = all(
        isinstance(value, str)
        and 0 < len(value) <= 256
        and all(
            character.isascii() and (character.isalnum() or character in "._:-")
            for character in value
        )
        for value in models
    )
    commit_valid = (
        isinstance(fixture_repository_commit, str)
        and len(fixture_repository_commit) in {40, 64}
        and all(character in "0123456789abcdef" for character in fixture_repository_commit)
    )
    if (
        not isinstance(current, CurrentLifecycleObservation)
        or current.identity is None
        or current.state is not ProviderLifecycleState.VERIFICATION_REQUIRED
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in values
        )
        or not safe_models
        or not commit_valid
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in digests
        )
        or max_cost_microunits is not None
        and (
            isinstance(max_cost_microunits, bool)
            or not isinstance(max_cost_microunits, int)
            or max_cost_microunits <= 0
        )
        or expires_at <= current.updated_at
    ):
        raise LifecycleServiceError("verification_manifest_invalid")
    identity = current.identity
    return VerificationManifest(
        identity.provider.value, identity.runtime_kind.value, identity.digest,
        identity.version, identity.runtime_digest, identity.model_identity_digest,
        identity.routing_policy_digest, identity.policy_version, requested_model,
        expected_effective_model, normalized_effort.value, "read-only",
        max_input_tokens, max_output_tokens, timeout_seconds, expires_at,
        fixture_repository_commit, graph_fingerprint, prompt_contract_hash,
        response_contract_hash, max_cost_microunits, 1, False, True, True,
        (
            "duration_ms", "effective_model", "exit_classification",
            "input_tokens", "output_tokens", "stderr_sha256", "stdout_sha256",
        ),
    )


def _event(
    boundary_digest: str,
    identity: ProviderRuntimeIdentity,
    previous: CurrentLifecycleObservation | None,
    state: ProviderLifecycleState,
    reason: LifecycleReasonCode,
    occurred_at: int,
) -> ProviderLifecycleEvent:
    payload = {
        "boundary": boundary_digest,
        "identity": identity.digest,
        "previous_state": None if previous is None else previous.state.value,
        "state": state.value,
        "reason": reason.value,
        "occurred_at": occurred_at,
    }
    event_id = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return ProviderLifecycleEvent(
        event_id, identity.provider, identity.runtime_kind,
        None if previous is None or previous.identity is None else previous.identity.digest,
        identity.digest,
        None if previous is None else previous.state,
        state, reason, identity.policy_version, occurred_at,
    )


class ProviderLifecycleService:
    """Coordinates isolated observations with routing authority bindings."""

    def __init__(self, lifecycle_store: LifecycleStore, routing_store: RepositoryStore) -> None:
        if not isinstance(lifecycle_store, LifecycleStore) or not isinstance(routing_store, RepositoryStore):
            raise LifecycleServiceError("lifecycle_service_invalid")
        self.lifecycle_store = lifecycle_store
        self.routing_store = routing_store

    def active_identity_digest(self, boundary_digest: str) -> str:
        try:
            current = self.lifecycle_store.current_observation(boundary_digest)
        except (LifecycleStorageError, ValueError):
            raise LifecycleServiceError("lifecycle_authority_unavailable") from None
        if current is None or current.identity is None or current.state is not ProviderLifecycleState.ACTIVE:
            raise LifecycleServiceError("lifecycle_inactive")
        return current.identity.digest

    def route_authority(
        self, boundary_digest: str, candidate: ApprovedRouteCandidate
    ) -> RouteAuthority:
        if not isinstance(candidate, ApprovedRouteCandidate):
            raise LifecycleServiceError("lifecycle_authority_invalid")
        try:
            current = self.lifecycle_store.current_observation(boundary_digest)
            snapshot_binding = self.routing_store.lifecycle_identity_binding(
                authority_kind="capability_snapshot",
                authority_id=candidate.capability_snapshot_digest,
            )
        except (LifecycleStorageError, StorageError, ValueError):
            raise LifecycleServiceError("lifecycle_authority_unavailable") from None
        if current is None or current.identity is None:
            raise LifecycleServiceError("lifecycle_inactive")
        if snapshot_binding != current.identity.digest:
            raise LifecycleServiceError("lifecycle_binding_missing")
        return RouteAuthority(
            candidate.candidate_id,
            current.identity.provider,
            current.identity.runtime_kind,
            current.identity.digest,
            candidate.capability_snapshot_digest,
            current.state,
        )

    def require_snapshot_authority(
        self, *, boundary_digest: str, lifecycle_identity_digest: str,
        capability_snapshot_digest: str, live_identity: ProviderRuntimeIdentity,
    ) -> None:
        try:
            current = self.lifecycle_store.current_observation(boundary_digest)
            binding = self.routing_store.lifecycle_identity_binding(
                authority_kind="capability_snapshot",
                authority_id=capability_snapshot_digest,
            )
        except (LifecycleStorageError, StorageError, ValueError):
            raise LifecycleServiceError("lifecycle_authority_unavailable") from None
        if current is None or current.identity is None or current.state is not ProviderLifecycleState.ACTIVE:
            raise LifecycleServiceError("lifecycle_inactive")
        if not isinstance(live_identity, ProviderRuntimeIdentity) or current.identity.digest != lifecycle_identity_digest or live_identity.digest != lifecycle_identity_digest:
            raise LifecycleServiceError("lifecycle_identity_changed")
        if binding != lifecycle_identity_digest:
            raise LifecycleServiceError("lifecycle_binding_missing")

    def _record(
        self,
        boundary_digest: str,
        identity: ProviderRuntimeIdentity,
        previous: CurrentLifecycleObservation | None,
        state: ProviderLifecycleState,
        reason: LifecycleReasonCode,
        occurred_at: int,
    ) -> CurrentLifecycleObservation:
        try:
            self.lifecycle_store.record_transition(
                boundary_digest,
                identity,
                _event(boundary_digest, identity, previous, state, reason, occurred_at),
            )
            current = self.lifecycle_store.current_observation(boundary_digest)
        except (LifecycleStorageError, ValueError):
            raise LifecycleServiceError("lifecycle_persistence_failed") from None
        if current is None:
            raise LifecycleServiceError("lifecycle_persistence_failed")
        return current

    def _carry_forward(
        self,
        boundary_digest: str,
        identity: ProviderRuntimeIdentity,
        previous: CurrentLifecycleObservation,
        assessment: ProviderCompatibilityAssessment,
    ) -> LifecycleObservationResult:
        if previous.identity is None:
            raise LifecycleServiceError("lifecycle_observation_invalid")
        event = _event(
            boundary_digest,
            identity,
            previous,
            ProviderLifecycleState.ACTIVE,
            LifecycleReasonCode.PATCH_CARRIED_FORWARD,
            identity.observed_at,
        )
        try:
            carried = self.routing_store.carry_forward_snapshot_bindings(
                previous_identity_digest=previous.identity.digest,
                new_identity_digest=identity.digest,
                lifecycle_event_id=event.event_id,
                carried_at=identity.observed_at,
            )
            invalidated_approvals = self.routing_store.invalidate_lifecycle_approvals(
                previous.identity.digest
            )
            self.lifecycle_store.record_invalidations(
                boundary_digest=boundary_digest,
                previous_identity_digest=previous.identity.digest,
                current_identity_digest=identity.digest,
                capability_snapshot_digests=(),
                approval_ids=invalidated_approvals,
                reason=LifecycleReasonCode.PATCH_CARRIED_FORWARD,
                invalidated_at=identity.observed_at,
            )
        except (StorageError, LifecycleStorageError, ValueError):
            raise LifecycleServiceError("lifecycle_carry_forward_failed") from None
        try:
            self.lifecycle_store.record_transition(boundary_digest, identity, event)
            current = self.lifecycle_store.current_observation(boundary_digest)
        except (LifecycleStorageError, ValueError):
            raise LifecycleServiceError("lifecycle_persistence_failed") from None
        if current is None:
            raise LifecycleServiceError("lifecycle_persistence_failed")
        return LifecycleObservationResult(
            identity.digest,
            assessment.change,
            current.state,
            assessment.reason,
            (),
            invalidated_approvals,
            carried,
        )

    def observe(
        self,
        *, boundary_digest: str, identity: ProviderRuntimeIdentity,
        policy: ProviderCompatibilityPolicy, standard_probe_passed: bool = True,
        expanded_probe_passed: bool = True,
    ) -> LifecycleObservationResult:
        if not isinstance(identity, ProviderRuntimeIdentity) or not isinstance(policy, ProviderCompatibilityPolicy):
            raise LifecycleServiceError("lifecycle_observation_invalid")
        try:
            previous = self.lifecycle_store.current_observation(boundary_digest)
        except (LifecycleStorageError, ValueError):
            raise LifecycleServiceError("lifecycle_persistence_failed") from None
        if previous is not None and previous.identity is not None and (
            previous.identity.provider is not identity.provider
            or previous.identity.runtime_kind is not identity.runtime_kind
        ):
            raise LifecycleServiceError("lifecycle_boundary_changed")

        comparison_identity = None if previous is None else previous.identity
        if previous is None:
            if not standard_probe_passed:
                state = ProviderLifecycleState.INCOMPATIBLE
                reason = LifecycleReasonCode.PROBE_FAILED
                current = self._record(boundary_digest, identity, None, state, reason, identity.observed_at)
                assessment = assess_identity_change(None, identity, policy)
                change = assessment.change
            else:
                assessment = assess_identity_change(None, identity, policy)
                current = self._record(boundary_digest, identity, None, assessment.state, assessment.reason, identity.observed_at)
                change = assessment.change
        elif previous.state is ProviderLifecycleState.UNAVAILABLE:
            current = self._record(boundary_digest, identity, previous, ProviderLifecycleState.DISCOVERED, LifecycleReasonCode.DISCOVERED, identity.observed_at)
            assessment = assess_identity_change(None, identity, policy)
            change = assessment.change
        else:
            assessment = assess_identity_change(
                comparison_identity, identity, policy,
                standard_probe_passed=standard_probe_passed,
                expanded_probe_passed=expanded_probe_passed,
                existing_state=previous.state,
            )
            reason = assessment.reason
            if previous.state is ProviderLifecycleState.INCOMPATIBLE and assessment.state is ProviderLifecycleState.VERIFICATION_REQUIRED:
                reason = LifecycleReasonCode.POLICY_PROMOTED
            if reason is LifecycleReasonCode.PATCH_CARRIED_FORWARD:
                return self._carry_forward(boundary_digest, identity, previous, assessment)
            current = self._record(boundary_digest, identity, previous, assessment.state, reason, identity.observed_at)
            change = assessment.change

        if current.state is ProviderLifecycleState.DISCOVERED and policy.supports(identity) and standard_probe_passed:
            current = self._record(boundary_digest, identity, current, ProviderLifecycleState.VERIFICATION_REQUIRED, LifecycleReasonCode.COMPATIBILITY_CONFIRMED, identity.observed_at)

        invalidated_snapshots: tuple[str, ...] = ()
        invalidated_approvals: tuple[str, ...] = ()
        if previous is not None and previous.identity is not None and (
            previous.identity.digest != identity.digest
            or previous.state is ProviderLifecycleState.ACTIVE and current.state is not ProviderLifecycleState.ACTIVE
        ):
            try:
                invalidated_snapshots, expected_approvals = self.routing_store.lifecycle_authority_targets(previous.identity.digest)
                invalidated_approvals = self.routing_store.invalidate_lifecycle_approvals(previous.identity.digest)
                if invalidated_approvals != expected_approvals:
                    raise StorageError("lifecycle_invalidation_changed")
                self.lifecycle_store.record_invalidations(
                    boundary_digest=boundary_digest,
                    previous_identity_digest=previous.identity.digest,
                    current_identity_digest=identity.digest,
                    capability_snapshot_digests=invalidated_snapshots,
                    approval_ids=invalidated_approvals,
                    reason=assessment.reason,
                    invalidated_at=identity.observed_at,
                )
            except (StorageError, LifecycleStorageError, ValueError):
                raise LifecycleServiceError("lifecycle_invalidation_failed") from None
        return LifecycleObservationResult(identity.digest, change, current.state, assessment.reason, invalidated_snapshots, invalidated_approvals, ())

    def mark_unavailable(
        self,
        *,
        boundary_digest: str,
        provider: object,
        runtime_kind: object,
        policy_version: str,
        observed_at: int,
        reason: LifecycleReasonCode,
    ) -> CurrentLifecycleObservation:
        if reason not in {
            LifecycleReasonCode.RUNTIME_MISSING,
            LifecycleReasonCode.CREDENTIAL_UNHEALTHY,
        }:
            raise LifecycleServiceError("lifecycle_observation_invalid")
        try:
            current = self.lifecycle_store.current_observation(boundary_digest)
            if current is not None and current.identity is not None:
                identity = current.identity
                if identity.provider != provider or identity.runtime_kind != runtime_kind or identity.policy_version != policy_version:
                    raise LifecycleServiceError("lifecycle_boundary_changed")
                current_digest = identity.digest
            else:
                identity = None
                current_digest = None
            repeated = current is not None and current.state is ProviderLifecycleState.UNAVAILABLE
            event = ProviderLifecycleEvent(
                hashlib.sha256(f"{boundary_digest}:{observed_at}:unavailable".encode()).hexdigest(),
                provider,
                runtime_kind,
                None if current is None or current.identity is None else current.identity.digest,
                current_digest,
                None if current is None else current.state,
                ProviderLifecycleState.UNAVAILABLE,
                LifecycleReasonCode.IDENTITY_UNCHANGED if repeated else reason,
                policy_version,
                observed_at,
            )
            self.lifecycle_store.record_transition(boundary_digest, identity, event)
            updated = self.lifecycle_store.current_observation(boundary_digest)
            if current is not None and current.identity is not None and current.state is ProviderLifecycleState.ACTIVE:
                snapshots, approvals = self.routing_store.lifecycle_authority_targets(current.identity.digest)
                invalidated = self.routing_store.invalidate_lifecycle_approvals(current.identity.digest)
                if approvals != invalidated:
                    raise StorageError("lifecycle_invalidation_changed")
                self.lifecycle_store.record_invalidations(
                    boundary_digest=boundary_digest,
                    previous_identity_digest=current.identity.digest,
                    current_identity_digest=current.identity.digest,
                    capability_snapshot_digests=snapshots,
                    approval_ids=invalidated,
                    reason=reason,
                    invalidated_at=observed_at,
                )
        except LifecycleServiceError:
            raise
        except (LifecycleStorageError, StorageError, TypeError, ValueError):
            raise LifecycleServiceError("lifecycle_persistence_failed") from None
        if updated is None:
            raise LifecycleServiceError("lifecycle_persistence_failed")
        return updated

    def activate(
        self, *, boundary_digest: str, lifecycle_identity_digest: str,
        capability_snapshot_digest: str, activated_at: int,
    ) -> CurrentLifecycleObservation:
        try:
            current = self.lifecycle_store.current_observation(boundary_digest)
            binding = self.routing_store.lifecycle_identity_binding(authority_kind="capability_snapshot", authority_id=capability_snapshot_digest)
            records = self.routing_store.capability_snapshot_records(limit=64)
        except (LifecycleStorageError, StorageError, ValueError):
            raise LifecycleServiceError("lifecycle_authority_unavailable") from None
        if current is None or current.identity is None or current.state is not ProviderLifecycleState.VERIFICATION_REQUIRED:
            raise LifecycleServiceError("lifecycle_verification_required")
        if current.identity.digest != lifecycle_identity_digest or binding != lifecycle_identity_digest:
            raise LifecycleServiceError("lifecycle_identity_changed")
        matching = [row for row in records if row["digest"] == capability_snapshot_digest]
        if len(matching) != 1 or matching[0]["payload"].get("expires_at", 0) <= activated_at:
            raise LifecycleServiceError("lifecycle_snapshot_invalid")
        return self._record(boundary_digest, current.identity, current, ProviderLifecycleState.ACTIVE, LifecycleReasonCode.VERIFICATION_ACCEPTED, activated_at)

    def require_execution_authority(
        self, *, boundary_digest: str, lifecycle_identity_digest: str,
        capability_snapshot_digest: str, approval_id: str,
        live_identity: ProviderRuntimeIdentity,
    ) -> ExecutionLifecycleAuthority:
        try:
            current = self.lifecycle_store.current_observation(boundary_digest)
            snapshot_binding = self.routing_store.lifecycle_identity_binding(authority_kind="capability_snapshot", authority_id=capability_snapshot_digest)
            approval_binding = self.routing_store.lifecycle_approval_binding_details(approval_id)
        except (LifecycleStorageError, StorageError, ValueError):
            raise LifecycleServiceError("lifecycle_authority_unavailable") from None
        if current is None or current.identity is None or current.state is not ProviderLifecycleState.ACTIVE:
            raise LifecycleServiceError("lifecycle_inactive")
        if not isinstance(live_identity, ProviderRuntimeIdentity) or current.identity.digest != lifecycle_identity_digest or live_identity.digest != lifecycle_identity_digest:
            raise LifecycleServiceError("lifecycle_identity_changed")
        if snapshot_binding != lifecycle_identity_digest or approval_binding != (capability_snapshot_digest, lifecycle_identity_digest):
            raise LifecycleServiceError("lifecycle_binding_missing")
        return ExecutionLifecycleAuthority(lifecycle_identity_digest, capability_snapshot_digest, approval_id, current.state)

    def prepare_verification_manifest(
        self, *, boundary_digest: str, requested_model: str,
        expected_effective_model: str, effort: Effort, max_input_tokens: int,
        max_output_tokens: int, timeout_seconds: int, expires_at: int,
        fixture_repository_commit: str, graph_fingerprint: str,
        prompt_contract_hash: str, response_contract_hash: str,
        max_cost_microunits: int | None = None,
    ) -> VerificationManifest:
        try:
            current = self.lifecycle_store.current_observation(boundary_digest)
        except (LifecycleStorageError, ValueError):
            raise LifecycleServiceError("verification_manifest_invalid") from None
        if current is None:
            raise LifecycleServiceError("verification_manifest_invalid")
        return build_verification_manifest(
            current,
            requested_model=requested_model,
            expected_effective_model=expected_effective_model,
            effort=effort,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
            expires_at=expires_at,
            fixture_repository_commit=fixture_repository_commit,
            graph_fingerprint=graph_fingerprint,
            prompt_contract_hash=prompt_contract_hash,
            response_contract_hash=response_contract_hash,
            max_cost_microunits=max_cost_microunits,
        )
