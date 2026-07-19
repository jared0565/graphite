"""Verified Claude/Codex capability profile tests."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from graphite.routing.contracts import (
    CliIdentity,
    Effort,
    PermissionMode,
    ProviderId,
    RiskTier,
)
from graphite.routing.profiles import (
    BUNDLED_REQUESTED_PROFILES,
    EditSmokeEvidence,
    ProfileError,
    VerificationEvidence,
    create_capability_snapshot,
    load_verified_capability_snapshots,
    operator_codex_profile,
    save_capability_snapshot,
    verify_and_save_approved_edit_profile,
    verify_and_save_approved_profile,
    verify_approved_profile,
)
from graphite.routing.storage import RepositoryStore, StorageError


def _identity(provider: ProviderId = ProviderId.CLAUDE_CODE) -> CliIdentity:
    return CliIdentity(provider, "a" * 64, "2.1.208", "1.0.0")


def test_bundled_requests_are_only_documented_claude_aliases() -> None:
    assert set(BUNDLED_REQUESTED_PROFILES) == {
        "claude-code/fable",
        "claude-code/opus",
        "claude-code/sonnet",
    }
    assert {item.provider for item in BUNDLED_REQUESTED_PROFILES.values()} == {
        ProviderId.CLAUDE_CODE
    }
    assert all(item.provisional for item in BUNDLED_REQUESTED_PROFILES.values())


def test_codex_profile_requires_explicit_official_operator_evidence() -> None:
    profile = operator_codex_profile(
        model_id="gpt-tested-codex",
        supported_efforts=(Effort.MEDIUM, Effort.HIGH),
        evidence_url="https://developers.openai.com/codex/models",
        evidence_accessed="2026-07-18",
    )

    assert profile.provider is ProviderId.CODEX
    assert profile.requested_model == "gpt-tested-codex"
    with pytest.raises(ProfileError, match="^profile_evidence_invalid$"):
        operator_codex_profile(
            model_id="gpt-guessed",
            supported_efforts=(Effort.HIGH,),
            evidence_url="https://example.invalid/models",
            evidence_accessed="2026-07-18",
        )


def test_snapshot_creation_binds_verified_effective_identity_and_bounds() -> None:
    requested = BUNDLED_REQUESTED_PROFILES["claude-code/sonnet"]
    snapshot = create_capability_snapshot(
        requested=requested,
        identity=_identity(),
        effective_model="claude-sonnet-5",
        effort=Effort.HIGH,
        capabilities=("reasoning", "code"),
        context_window_tokens=200_000,
        risk_ceiling=RiskTier.MEDIUM,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        verified_at=1_700_000_000,
        ttl_seconds=3_600,
    )

    assert snapshot.profile.requested_model == "sonnet"
    assert snapshot.profile.effective_model == "claude-sonnet-5"
    assert snapshot.profile.capabilities == ("code", "reasoning")
    assert snapshot.expires_at == 1_700_003_600
    with pytest.raises(ProfileError, match="^profile_effort_unsupported$"):
        create_capability_snapshot(
            requested=requested,
            identity=_identity(),
            effective_model="claude-sonnet-5",
            effort=Effort.DEFAULT,
            capabilities=("code",),
            context_window_tokens=200_000,
            risk_ceiling=RiskTier.LOW,
            permission_mode=PermissionMode.READ_ONLY,
            verified_at=1_700_000_000,
            ttl_seconds=3_600,
        )


def test_approved_verification_invokes_exactly_one_read_only_adapter_call() -> None:
    requested = BUNDLED_REQUESTED_PROFILES["claude-code/sonnet"]
    calls: list[tuple[str, Effort]] = []

    def verifier(profile, effort: Effort) -> VerificationEvidence:
        calls.append((profile.requested_model, effort))
        return VerificationEvidence(
            "claude-sonnet-5",
            ("code", "reasoning"),
            200_000,
            RiskTier.MEDIUM,
            1_024,
            256,
        )

    snapshot = verify_approved_profile(
        requested=requested,
        identity=_identity(),
        effort=Effort.HIGH,
        verified_at=1_700_000_000,
        ttl_seconds=3_600,
        approval_granted=True,
        max_input_tokens=32_768,
        max_output_tokens=4_096,
        verifier=verifier,
    )

    assert calls == [("sonnet", Effort.HIGH)]
    assert snapshot.profile.permission_mode is PermissionMode.READ_ONLY
    with pytest.raises(ProfileError, match="^profile_verification_approval_required$"):
        verify_approved_profile(
            requested=requested,
            identity=_identity(),
            effort=Effort.HIGH,
            verified_at=1_700_000_000,
            ttl_seconds=3_600,
            approval_granted=False,
            max_input_tokens=32_768,
            max_output_tokens=4_096,
            verifier=lambda *_: (_ for _ in ()).throw(AssertionError("must not call")),
        )


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    ((32_769, 1), (1, 4_097)),
)
def test_over_budget_verification_never_persists_authority(
    tmp_path: Path,
    input_tokens: int,
    output_tokens: int,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()

    with pytest.raises(ProfileError, match="^profile_verification_budget_exceeded$"):
        verify_and_save_approved_profile(
            store,
            requested=BUNDLED_REQUESTED_PROFILES["claude-code/sonnet"],
            identity=_identity(),
            effort=Effort.HIGH,
            verified_at=1_700_000_000,
            ttl_seconds=3_600,
            approval_granted=True,
            max_input_tokens=32_768,
            max_output_tokens=4_096,
            verifier=lambda *_: VerificationEvidence(
                "claude-sonnet-5",
                ("code", "reasoning"),
                200_000,
                RiskTier.MEDIUM,
                input_tokens,
                output_tokens,
            ),
        )

    assert load_verified_capability_snapshots(store, now=1_700_000_001) == ()


def test_verify_and_save_persists_only_validated_authority(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()

    snapshot = verify_and_save_approved_profile(
        store,
        requested=BUNDLED_REQUESTED_PROFILES["claude-code/sonnet"],
        identity=_identity(),
        effort=Effort.HIGH,
        verified_at=1_700_000_000,
        ttl_seconds=3_600,
        approval_granted=True,
        max_input_tokens=32_768,
        max_output_tokens=4_096,
        verifier=lambda *_: VerificationEvidence(
            "claude-sonnet-5",
            ("code", "reasoning"),
            200_000,
            RiskTier.MEDIUM,
            1_024,
            256,
        ),
    )

    assert load_verified_capability_snapshots(store, now=1_700_000_001) == (snapshot,)


def test_lifecycle_bound_snapshot_is_eligible_only_for_exact_active_identity(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    lifecycle_digest = "d" * 64

    snapshot = verify_and_save_approved_profile(
        store,
        requested=BUNDLED_REQUESTED_PROFILES["claude-code/sonnet"],
        identity=_identity(),
        effort=Effort.HIGH,
        verified_at=1_700_000_000,
        ttl_seconds=3_600,
        approval_granted=True,
        max_input_tokens=32_768,
        max_output_tokens=4_096,
        verifier=lambda *_: VerificationEvidence(
            "claude-sonnet-5", ("code", "reasoning"), 200_000,
            RiskTier.MEDIUM, 1_024, 256,
        ),
        lifecycle_identity_digest=lifecycle_digest,
    )

    assert store.lifecycle_identity_binding(
        authority_kind="capability_snapshot", authority_id=snapshot.digest
    ) == lifecycle_digest
    assert load_verified_capability_snapshots(
        store, now=1_700_000_001,
        active_lifecycle_identity_digests=frozenset({lifecycle_digest}),
    ) == (snapshot,)
    assert load_verified_capability_snapshots(
        store, now=1_700_000_001,
        active_lifecycle_identity_digests=frozenset({"e" * 64}),
    ) == ()


def _bound_read_only_snapshot(
    store: RepositoryStore,
    *,
    lifecycle_digest: str = "d" * 64,
):
    return verify_and_save_approved_profile(
        store,
        requested=BUNDLED_REQUESTED_PROFILES["claude-code/sonnet"],
        identity=_identity(),
        effort=Effort.HIGH,
        verified_at=1_700_000_000,
        ttl_seconds=3_600,
        approval_granted=True,
        max_input_tokens=32_768,
        max_output_tokens=4_096,
        verifier=lambda *_: VerificationEvidence(
            "claude-sonnet-5", ("code", "reasoning"), 200_000,
            RiskTier.MEDIUM, 1_024, 256,
        ),
        lifecycle_identity_digest=lifecycle_digest,
    )


def _edit_evidence(**changes: object) -> EditSmokeEvidence:
    values: dict[str, object] = {
        "effective_model": "claude-sonnet-5",
        "input_tokens": 2_048,
        "output_tokens": 512,
        "duration_ms": 1_250,
        "diff_sha256": "e" * 64,
        "changed_file_count": 1,
        "changed_byte_count": 32,
        "validation_outcome": "passed",
    }
    values.update(changes)
    return EditSmokeEvidence(**values)


def test_approved_edit_smoke_atomically_promotes_bound_workspace_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    lifecycle_digest = "d" * 64
    source = _bound_read_only_snapshot(store, lifecycle_digest=lifecycle_digest)
    calls: list[str] = []

    promoted = verify_and_save_approved_edit_profile(
        store,
        verified_snapshot=source,
        verified_at=1_700_000_100,
        ttl_seconds=3_600,
        approval_granted=True,
        max_input_tokens=32_768,
        max_output_tokens=4_096,
        max_changed_files=1,
        max_changed_bytes=1_024,
        lifecycle_identity_digest=lifecycle_digest,
        verifier=lambda snapshot: (
            calls.append(snapshot.digest) or _edit_evidence()
        ),
    )

    assert calls == [source.digest]
    assert promoted.profile.permission_mode is PermissionMode.WORKSPACE_WRITE
    assert promoted.identity == source.identity
    assert promoted.profile.effective_model == source.profile.effective_model
    assert store.lifecycle_identity_binding(
        authority_kind="capability_snapshot", authority_id=promoted.digest
    ) == lifecycle_digest
    telemetry = store.cli_telemetry_records()
    assert len(telemetry) == 1
    assert telemetry[0]["capability_snapshot_digest"] == promoted.digest
    assert telemetry[0]["validation_outcome"] == "passed"
    assert telemetry[0]["changed_file_count"] == 1
    assert telemetry[0]["diff_sha256"] == "e" * 64
    assert telemetry[0]["human_verdict"] is None


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        ({"input_tokens": 32_769}, "profile_edit_smoke_budget_exceeded"),
        ({"output_tokens": 4_097}, "profile_edit_smoke_budget_exceeded"),
        ({"changed_file_count": 0}, "profile_edit_smoke_diff_invalid"),
        ({"changed_file_count": 2}, "profile_edit_smoke_diff_exceeded"),
        ({"changed_byte_count": 1_025}, "profile_edit_smoke_diff_exceeded"),
        ({"validation_outcome": "failed"}, "profile_edit_smoke_validation_failed"),
        ({"effective_model": "claude-opus-5"}, "profile_edit_smoke_model_mismatch"),
    ),
)
def test_failed_edit_smoke_never_persists_workspace_authority(
    tmp_path: Path,
    changes: dict[str, object],
    code: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    source = _bound_read_only_snapshot(store)

    with pytest.raises(ProfileError, match=f"^{code}$"):
        verify_and_save_approved_edit_profile(
            store,
            verified_snapshot=source,
            verified_at=1_700_000_100,
            ttl_seconds=3_600,
            approval_granted=True,
            max_input_tokens=32_768,
            max_output_tokens=4_096,
            max_changed_files=1,
            max_changed_bytes=1_024,
            lifecycle_identity_digest="d" * 64,
            verifier=lambda _snapshot: _edit_evidence(**changes),
        )

    snapshots = load_verified_capability_snapshots(store, now=1_700_000_101)
    assert snapshots == (source,)
    assert store.cli_telemetry_records() == ()


def test_edit_smoke_requires_approval_and_current_bound_read_only_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    source = _bound_read_only_snapshot(store)
    calls = 0

    def verifier(_snapshot):
        nonlocal calls
        calls += 1
        return _edit_evidence()

    with pytest.raises(ProfileError, match="^profile_edit_smoke_approval_required$"):
        verify_and_save_approved_edit_profile(
            store,
            verified_snapshot=source,
            verified_at=1_700_000_100,
            ttl_seconds=3_600,
            approval_granted=False,
            max_input_tokens=32_768,
            max_output_tokens=4_096,
            max_changed_files=1,
            max_changed_bytes=1_024,
            lifecycle_identity_digest="d" * 64,
            verifier=verifier,
        )
    assert calls == 0

    with pytest.raises(ProfileError, match="^profile_edit_smoke_source_expired$"):
        verify_and_save_approved_edit_profile(
            store,
            verified_snapshot=source,
            verified_at=source.expires_at,
            ttl_seconds=3_600,
            approval_granted=True,
            max_input_tokens=32_768,
            max_output_tokens=4_096,
            max_changed_files=1,
            max_changed_bytes=1_024,
            lifecycle_identity_digest="d" * 64,
            verifier=verifier,
        )
    assert calls == 0


def test_edit_smoke_binding_failure_is_atomic(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    source = _bound_read_only_snapshot(store)

    with pytest.raises(ProfileError, match="^profile_edit_smoke_source_unbound$"):
        verify_and_save_approved_edit_profile(
            store,
            verified_snapshot=source,
            verified_at=1_700_000_100,
            ttl_seconds=3_600,
            approval_granted=True,
            max_input_tokens=32_768,
            max_output_tokens=4_096,
            max_changed_files=1,
            max_changed_bytes=1_024,
            lifecycle_identity_digest="f" * 64,
            verifier=lambda _snapshot: _edit_evidence(),
        )

    snapshots = load_verified_capability_snapshots(store, now=1_700_000_101)
    assert snapshots == (source,)
    assert store.cli_telemetry_records() == ()


def test_edit_smoke_audit_failure_rolls_back_promoted_snapshot_and_binding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    source = _bound_read_only_snapshot(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """CREATE TRIGGER reject_edit_smoke_audit
            BEFORE INSERT ON cli_telemetry_events BEGIN
                SELECT RAISE(ABORT, 'synthetic_audit_failure');
            END"""
        )

    with pytest.raises(
        ProfileError,
        match="^profile_edit_smoke_persistence_failed$",
    ):
        verify_and_save_approved_edit_profile(
            store,
            verified_snapshot=source,
            verified_at=1_700_000_100,
            ttl_seconds=3_600,
            approval_granted=True,
            max_input_tokens=32_768,
            max_output_tokens=4_096,
            max_changed_files=1,
            max_changed_bytes=1_024,
            lifecycle_identity_digest="d" * 64,
            verifier=lambda _snapshot: _edit_evidence(),
        )

    snapshots = load_verified_capability_snapshots(store, now=1_700_000_101)
    assert snapshots == (source,)
    assert store.cli_telemetry_records() == ()


def test_snapshot_persistence_is_canonical_bounded_and_expiry_aware(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    snapshot = create_capability_snapshot(
        requested=BUNDLED_REQUESTED_PROFILES["claude-code/sonnet"],
        identity=_identity(),
        effective_model="claude-sonnet-5",
        effort=Effort.HIGH,
        capabilities=("code", "reasoning"),
        context_window_tokens=200_000,
        risk_ceiling=RiskTier.MEDIUM,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        verified_at=1_700_000_000,
        ttl_seconds=3_600,
    )

    assert save_capability_snapshot(store, snapshot) is True
    assert save_capability_snapshot(store, snapshot) is False
    assert load_verified_capability_snapshots(store, now=1_700_000_001) == (snapshot,)
    assert load_verified_capability_snapshots(store, now=snapshot.expires_at) == ()

    with sqlite3.connect(store.path) as connection:
        payload = json.dumps(snapshot.to_dict(), sort_keys=True, separators=(",", ":"))
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TRIGGER capability_snapshot_update_guard")
        connection.execute(
            "UPDATE capability_snapshots SET payload_json=? "
            "WHERE capability_snapshot_digest=?",
            (payload.replace("claude-sonnet-5", "tampered-model"), snapshot.digest),
        )
    with pytest.raises(StorageError, match="^storage_corrupt$"):
        load_verified_capability_snapshots(store, now=1_700_000_001)
