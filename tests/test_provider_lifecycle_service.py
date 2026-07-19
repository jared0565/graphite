from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from graphite.routing.contracts import CliIdentity, Effort, PermissionMode, ProviderId, RiskTier
from graphite.routing.lifecycle import (
    IdentityChange,
    LifecycleReasonCode,
    LifecycleProviderId,
    ProviderCompatibilityPolicy,
    ProviderLifecycleState,
    ProviderRuntimeIdentity,
    RuntimeKind,
)
from graphite.routing.lifecycle_service import (
    LifecycleServiceError,
    ProviderLifecycleService,
    VerificationManifest,
)
from graphite.routing.lifecycle_storage import LifecycleStore
from graphite.routing.profiles import BUNDLED_REQUESTED_PROFILES, create_capability_snapshot, save_capability_snapshot
from graphite.routing.storage import RepositoryStore


def _identity(provider: LifecycleProviderId = LifecycleProviderId.CLAUDE_CODE, **changes: object) -> ProviderRuntimeIdentity:
    values: dict[str, object] = {
        "provider": provider,
        "runtime_kind": RuntimeKind.LOCAL_CLI,
        "version": "2.1.214" if provider is LifecycleProviderId.CLAUDE_CODE else "0.144.1",
        "runtime_digest": "a" * 64,
        "model_identity_digest": None,
        "routing_policy_digest": None,
        "capabilities": ("credential_health", "structured_output", "version"),
        "policy_version": "1.0.0",
        "observed_at": 100,
    }
    values.update(changes)
    return ProviderRuntimeIdentity(**values)


def _policy(provider: LifecycleProviderId = LifecycleProviderId.CLAUDE_CODE) -> ProviderCompatibilityPolicy:
    return ProviderCompatibilityPolicy(provider, RuntimeKind.LOCAL_CLI, "1.0.0", "0.1.0", "3.0.0", ("credential_health", "structured_output"))


def _service(tmp_path: Path) -> tuple[ProviderLifecycleService, LifecycleStore, RepositoryStore]:
    root = tmp_path / "project"
    root.mkdir()
    lifecycle = LifecycleStore(root)
    lifecycle.initialize()
    routing = RepositoryStore(root)
    routing.initialize()
    return ProviderLifecycleService(lifecycle, routing), lifecycle, routing


def _snapshot(routing: RepositoryStore, lifecycle_digest: str):
    snapshot = create_capability_snapshot(
        requested=BUNDLED_REQUESTED_PROFILES["claude-code/sonnet"],
        identity=CliIdentity(ProviderId.CLAUDE_CODE, "a" * 64, "2.1.214", "1.0.0"),
        effective_model="claude-sonnet-5",
        effort=Effort.HIGH,
        capabilities=("code", "reasoning"),
        context_window_tokens=200_000,
        risk_ceiling=RiskTier.MEDIUM,
        permission_mode=PermissionMode.READ_ONLY,
        verified_at=101,
        ttl_seconds=3_600,
    )
    save_capability_snapshot(routing, snapshot)
    routing.save_lifecycle_snapshot_binding(capability_snapshot_digest=snapshot.digest, lifecycle_identity_digest=lifecycle_digest, bound_at=101)
    return snapshot


def test_first_discovery_stops_at_verification_required_and_activation_is_explicit(tmp_path: Path) -> None:
    service, lifecycle, routing = _service(tmp_path)
    identity = _identity()
    boundary = "b" * 64

    result = service.observe(boundary_digest=boundary, identity=identity, policy=_policy())

    assert result.state is ProviderLifecycleState.VERIFICATION_REQUIRED
    assert [event.current_state for event in lifecycle.events(boundary)] == [ProviderLifecycleState.DISCOVERED, ProviderLifecycleState.VERIFICATION_REQUIRED]
    snapshot = _snapshot(routing, identity.digest)
    activated = service.activate(boundary_digest=boundary, lifecycle_identity_digest=identity.digest, capability_snapshot_digest=snapshot.digest, activated_at=102)
    assert activated.state is ProviderLifecycleState.ACTIVE
    assert lifecycle.current_observation(boundary).state is ProviderLifecycleState.ACTIVE


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"runtime_digest": "c" * 64, "observed_at": 200}, IdentityChange.HASH_ONLY),
        ({"version": "2.1.215", "observed_at": 200}, IdentityChange.PATCH),
        ({"version": "2.2.0", "observed_at": 200}, IdentityChange.MINOR),
        ({"version": "3.0.0", "observed_at": 200}, IdentityChange.MAJOR),
        ({"capabilities": ("credential_health", "structured_output", "tools", "version"), "observed_at": 200}, IdentityChange.CAPABILITY),
        ({"policy_version": "1.0.1", "observed_at": 200}, IdentityChange.POLICY),
    ],
)
def test_observation_classifies_each_cli_drift_without_reactivation(tmp_path: Path, changes: dict[str, object], expected: IdentityChange) -> None:
    service, _lifecycle, routing = _service(tmp_path)
    boundary = "b" * 64
    identity = _identity()
    service.observe(boundary_digest=boundary, identity=identity, policy=_policy())
    snapshot = _snapshot(routing, identity.digest)
    service.activate(boundary_digest=boundary, lifecycle_identity_digest=identity.digest, capability_snapshot_digest=snapshot.digest, activated_at=102)

    result = service.observe(boundary_digest=boundary, identity=replace(identity, **changes), policy=replace(_policy(), policy_version=changes.get("policy_version", "1.0.0")))

    assert result.change is expected
    assert result.state is not ProviderLifecycleState.ACTIVE


def test_drift_invalidates_only_matching_snapshot_and_pending_approval(tmp_path: Path) -> None:
    service, lifecycle, routing = _service(tmp_path)
    boundary = "b" * 64
    other_boundary = "c" * 64
    identity = _identity()
    other = _identity(LifecycleProviderId.CODEX)
    service.observe(boundary_digest=boundary, identity=identity, policy=_policy())
    service.observe(boundary_digest=other_boundary, identity=other, policy=_policy(LifecycleProviderId.CODEX))
    snapshot = _snapshot(routing, identity.digest)
    routing.save_approval_record(approval_id="approval-old", task_id=None, decision_id=None, nonce_hash="1" * 64, manifest_hash="2" * 64, expires_at=999, reserved_tokens=1)
    routing.save_lifecycle_approval_binding(approval_id="approval-old", capability_snapshot_digest=snapshot.digest, lifecycle_identity_digest=identity.digest, bound_at=103)

    result = service.observe(boundary_digest=boundary, identity=replace(identity, runtime_digest="d" * 64, observed_at=200), policy=_policy())

    assert result.invalidated_snapshots == (snapshot.digest,)
    assert result.invalidated_approvals == ("approval-old",)
    assert routing.approval_status("approval-old") == "invalidated"
    assert lifecycle.current_observation(other_boundary).identity == other


def test_lazy_execution_gate_requires_active_exact_live_and_all_bindings(tmp_path: Path) -> None:
    service, _lifecycle, routing = _service(tmp_path)
    boundary = "b" * 64
    identity = _identity()
    service.observe(boundary_digest=boundary, identity=identity, policy=_policy())
    snapshot = _snapshot(routing, identity.digest)
    service.activate(boundary_digest=boundary, lifecycle_identity_digest=identity.digest, capability_snapshot_digest=snapshot.digest, activated_at=102)
    routing.save_approval_record(approval_id="approval-current", task_id=None, decision_id=None, nonce_hash="3" * 64, manifest_hash="4" * 64, expires_at=999, reserved_tokens=1)
    routing.save_lifecycle_approval_binding(approval_id="approval-current", capability_snapshot_digest=snapshot.digest, lifecycle_identity_digest=identity.digest, bound_at=103)

    authority = service.require_execution_authority(boundary_digest=boundary, lifecycle_identity_digest=identity.digest, capability_snapshot_digest=snapshot.digest, approval_id="approval-current", live_identity=identity)
    assert authority.lifecycle_identity_digest == identity.digest
    with pytest.raises(LifecycleServiceError, match="^lifecycle_identity_changed$"):
        service.require_execution_authority(boundary_digest=boundary, lifecycle_identity_digest=identity.digest, capability_snapshot_digest=snapshot.digest, approval_id="approval-current", live_identity=replace(identity, runtime_digest="e" * 64, observed_at=104))


def test_provider_failure_is_isolated_and_revokes_only_its_active_authority(tmp_path: Path) -> None:
    service, lifecycle, routing = _service(tmp_path)
    boundary = "b" * 64
    other_boundary = "c" * 64
    identity = _identity()
    other = _identity(LifecycleProviderId.CODEX)
    service.observe(boundary_digest=boundary, identity=identity, policy=_policy())
    service.observe(boundary_digest=other_boundary, identity=other, policy=_policy(LifecycleProviderId.CODEX))
    snapshot = _snapshot(routing, identity.digest)
    service.activate(boundary_digest=boundary, lifecycle_identity_digest=identity.digest, capability_snapshot_digest=snapshot.digest, activated_at=102)

    unavailable = service.mark_unavailable(
        boundary_digest=boundary,
        provider=identity.provider,
        runtime_kind=identity.runtime_kind,
        policy_version=identity.policy_version,
        observed_at=200,
        reason=LifecycleReasonCode.RUNTIME_MISSING,
    )

    assert unavailable.state is ProviderLifecycleState.UNAVAILABLE
    assert unavailable.identity == identity
    assert lifecycle.current_observation(other_boundary).state is ProviderLifecycleState.VERIFICATION_REQUIRED


def test_manifest_preparation_is_pure_and_contains_no_prompt_or_credentials(tmp_path: Path) -> None:
    service, _lifecycle, _routing = _service(tmp_path)
    identity = _identity()
    service.observe(boundary_digest="b" * 64, identity=identity, policy=_policy())
    manifest = service.prepare_verification_manifest(
        boundary_digest="b" * 64,
        requested_model="sonnet",
        expected_effective_model="claude-sonnet-5",
        effort=Effort.HIGH,
        max_input_tokens=32_768,
        max_output_tokens=4_096,
        timeout_seconds=120,
        expires_at=500,
        fixture_repository_commit="1" * 40,
        graph_fingerprint="2" * 64,
        prompt_contract_hash="3" * 64,
        response_contract_hash="4" * 64,
    )
    assert isinstance(manifest, VerificationManifest)
    serialized = repr(manifest.to_dict())
    assert identity.digest in serialized
    assert "prompt_body" not in serialized.casefold()
    assert "source" not in serialized.casefold()
    assert "credential" not in serialized.casefold()
    assert manifest.max_attempts == 1
    assert manifest.fallback_enabled is False
    assert manifest.no_resume is manifest.no_substitution is True
