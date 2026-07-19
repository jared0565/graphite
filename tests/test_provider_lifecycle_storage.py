"""Isolated provider lifecycle persistence tests."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from graphite.routing.lifecycle import (
    LifecycleProviderId,
    LifecycleReasonCode,
    ProviderLifecycleEvent,
    ProviderLifecycleState,
    ProviderRuntimeIdentity,
    RuntimeKind,
)
from graphite.routing.lifecycle_storage import LifecycleStore, LifecycleStorageError


def _identity(**changes: object) -> ProviderRuntimeIdentity:
    values: dict[str, object] = {
        "provider": LifecycleProviderId.CLAUDE_CODE,
        "runtime_kind": RuntimeKind.LOCAL_CLI,
        "version": "2.1.215",
        "runtime_digest": "a" * 64,
        "model_identity_digest": None,
        "routing_policy_digest": None,
        "capabilities": ("auth-status-json", "structured-output"),
        "policy_version": "1.0.0",
        "observed_at": 1_721_347_200,
    }
    values.update(changes)
    return ProviderRuntimeIdentity(**values)


def _event(identity: ProviderRuntimeIdentity, **changes: object) -> ProviderLifecycleEvent:
    values: dict[str, object] = {
        "event_id": "event-1",
        "provider": identity.provider,
        "runtime_kind": identity.runtime_kind,
        "previous_identity_digest": None,
        "current_identity_digest": identity.digest,
        "previous_state": None,
        "current_state": ProviderLifecycleState.DISCOVERED,
        "reason": LifecycleReasonCode.DISCOVERED,
        "policy_version": identity.policy_version,
        "occurred_at": identity.observed_at,
    }
    values.update(changes)
    return ProviderLifecycleEvent(**values)


def _store(tmp_path: Path, **changes: object) -> LifecycleStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {"repository_root": tmp_path}
    values.update(changes)
    store = LifecycleStore(**values)
    store.initialize()
    return store


def test_lifecycle_store_creates_isolated_schema_with_safety_pragmas(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.path == tmp_path / ".graphite" / "routing" / "provider-lifecycle.sqlite3"
    assert store.integrity_check() == "ok"
    assert store.pragma_state() == {
        "foreign_keys": 1,
        "journal_mode": "wal",
        "busy_timeout": 2_000,
    }
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("1",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"current_observations", "lifecycle_events", "authority_invalidations"} <= tables


def test_schema_v1_fixture_reopens_idempotently(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".graphite" / "routing" / "provider-lifecycle.sqlite3"
    path.parent.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "provider_lifecycle_schema_v1.sql"
    with sqlite3.connect(path) as connection:
        connection.executescript(fixture.read_text(encoding="utf-8"))

    store = LifecycleStore(root)
    store.initialize()
    store.initialize()

    assert store.integrity_check() == "ok"
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("1",)


def test_record_transition_atomically_updates_current_and_appends_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    identity = _identity()
    boundary = "b" * 64

    assert store.record_transition(boundary, identity, _event(identity)) is True
    current = store.current_observation(boundary)
    events = store.events(boundary)

    assert current is not None
    assert current.identity == identity
    assert current.state is ProviderLifecycleState.DISCOVERED
    assert events == (_event(identity),)

    changed = replace(identity, runtime_digest="c" * 64, observed_at=identity.observed_at + 60)
    transition = _event(
        changed,
        event_id="event-2",
        previous_identity_digest=identity.digest,
        previous_state=ProviderLifecycleState.DISCOVERED,
        current_state=ProviderLifecycleState.VERIFICATION_REQUIRED,
        reason=LifecycleReasonCode.HASH_CHANGED,
        occurred_at=changed.observed_at,
    )
    assert store.record_transition(boundary, changed, transition) is True
    assert store.current_observation(boundary).identity == changed
    assert store.events(boundary) == (_event(identity), transition)


def test_unavailable_provider_can_be_recorded_before_identity_discovery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    boundary = "b" * 64
    unavailable = ProviderLifecycleEvent(
        event_id="event-unavailable",
        provider=LifecycleProviderId.CLAUDE_CODE,
        runtime_kind=RuntimeKind.LOCAL_CLI,
        previous_identity_digest=None,
        current_identity_digest=None,
        previous_state=None,
        current_state=ProviderLifecycleState.UNAVAILABLE,
        reason=LifecycleReasonCode.RUNTIME_MISSING,
        policy_version="1.0.0",
        occurred_at=1_721_347_100,
    )
    assert store.record_transition(boundary, None, unavailable) is True
    current = store.current_observation(boundary)
    assert current is not None
    assert current.identity is None
    assert current.state is ProviderLifecycleState.UNAVAILABLE

    identity = _identity()
    discovered = _event(
        identity,
        event_id="event-discovered",
        previous_identity_digest=None,
        previous_state=ProviderLifecycleState.UNAVAILABLE,
    )
    assert store.record_transition(boundary, identity, discovered) is True
    assert store.current_observation(boundary).identity == identity


def test_transition_write_is_idempotent_but_rejects_event_reuse(tmp_path: Path) -> None:
    store = _store(tmp_path)
    identity = _identity()
    event = _event(identity)
    boundary = "b" * 64

    assert store.record_transition(boundary, identity, event) is True
    assert store.record_transition(boundary, identity, event) is False
    with pytest.raises(LifecycleStorageError, match="^lifecycle_event_changed$"):
        store.record_transition(
            boundary,
            identity,
            replace(event, current_identity_digest="c" * 64),
        )
    with pytest.raises(LifecycleStorageError, match="^lifecycle_event_changed$"):
        store.record_transition("c" * 64, identity, event)


@pytest.mark.parametrize(
    ("boundary", "event_changes", "code"),
    [
        ("B" * 64, {}, "lifecycle_boundary_invalid"),
        ("b" * 64, {"current_identity_digest": "c" * 64}, "lifecycle_identity_mismatch"),
        ("b" * 64, {"policy_version": "1.0.1"}, "lifecycle_policy_mismatch"),
    ],
)
def test_transition_rejects_malformed_or_mismatched_authority(
    tmp_path: Path,
    boundary: str,
    event_changes: dict[str, object],
    code: str,
) -> None:
    store = _store(tmp_path)
    identity = _identity()
    with pytest.raises((ValueError, LifecycleStorageError), match=f"^{code}$"):
        store.record_transition(boundary, identity, _event(identity, **event_changes))


def test_transition_rejects_stale_previous_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    identity = _identity()
    boundary = "b" * 64
    store.record_transition(boundary, identity, _event(identity))
    changed = replace(identity, runtime_digest="c" * 64, observed_at=identity.observed_at + 60)

    with pytest.raises(LifecycleStorageError, match="^lifecycle_transition_stale$"):
        store.record_transition(
            boundary,
            changed,
            _event(
                changed,
                event_id="event-2",
                previous_identity_digest="d" * 64,
                previous_state=ProviderLifecycleState.DISCOVERED,
                current_state=ProviderLifecycleState.VERIFICATION_REQUIRED,
                reason=LifecycleReasonCode.HASH_CHANGED,
            ),
        )


def test_lifecycle_events_are_database_immutable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    identity = _identity()
    store.record_transition("b" * 64, identity, _event(identity))

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="lifecycle_event_immutable"):
            connection.execute("UPDATE lifecycle_events SET reason='probe_failed'")
        with pytest.raises(sqlite3.IntegrityError, match="lifecycle_event_immutable"):
            connection.execute("DELETE FROM lifecycle_events")


def test_events_are_bounded_and_ordered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    identity = _identity()
    boundary = "b" * 64
    store.record_transition(boundary, identity, _event(identity))
    state = ProviderLifecycleState.DISCOVERED
    for index in range(2, 8):
        next_state = (
            ProviderLifecycleState.UNAVAILABLE
            if state is ProviderLifecycleState.DISCOVERED
            else ProviderLifecycleState.DISCOVERED
        )
        reason = (
            LifecycleReasonCode.RUNTIME_MISSING
            if next_state is ProviderLifecycleState.UNAVAILABLE
            else LifecycleReasonCode.DISCOVERED
        )
        changed = replace(identity, observed_at=identity.observed_at + index)
        event = _event(
            changed,
            event_id=f"event-{index}",
            previous_identity_digest=identity.digest,
            previous_state=state,
            current_state=next_state,
            reason=reason,
            occurred_at=changed.observed_at,
        )
        store.record_transition(boundary, changed, event)
        state = next_state

    assert [event.event_id for event in store.events(boundary, limit=3)] == [
        "event-5",
        "event-6",
        "event-7",
    ]
    with pytest.raises(ValueError, match="events_limit_invalid"):
        store.events(boundary, limit=101)


def test_invalidation_evidence_is_append_only_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    recorded = store.record_invalidations(
        boundary_digest="b" * 64,
        previous_identity_digest="a" * 64,
        current_identity_digest="c" * 64,
        capability_snapshot_digests=("d" * 64, "e" * 64),
        approval_ids=("approval-1",),
        reason=LifecycleReasonCode.PATCH_CHANGED,
        invalidated_at=1_721_347_260,
    )

    assert recorded == 3
    assert store.record_invalidations(
        boundary_digest="b" * 64,
        previous_identity_digest="a" * 64,
        current_identity_digest="c" * 64,
        capability_snapshot_digests=("d" * 64, "e" * 64),
        approval_ids=("approval-1",),
        reason=LifecycleReasonCode.PATCH_CHANGED,
        invalidated_at=1_721_347_260,
    ) == 0
    invalidations = store.invalidations("b" * 64)
    assert {(item.target_kind, item.target_id) for item in invalidations} == {
        ("capability_snapshot", "d" * 64),
        ("capability_snapshot", "e" * 64),
        ("approval", "approval-1"),
    }
    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="lifecycle_invalidation_immutable"):
            connection.execute("DELETE FROM authority_invalidations")


def test_verified_backup_has_external_digest_marker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    identity = _identity()
    store.record_transition("b" * 64, identity, _event(identity))

    backup, marker = store.create_verified_backup()
    evidence = json.loads(marker.read_text(encoding="utf-8"))

    assert evidence == {
        "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        "schema_version": "1",
    }
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_unknown_schema_and_partial_schema_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE schema_meta SET value='999' WHERE key='schema_version'")
    with pytest.raises(LifecycleStorageError, match="^lifecycle_schema_unsupported$"):
        LifecycleStore(tmp_path).initialize()

    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE schema_meta SET value='1' WHERE key='schema_version'")
        connection.execute("DROP TABLE current_observations")
    with pytest.raises(LifecycleStorageError, match="^lifecycle_rollback_required$"):
        LifecycleStore(tmp_path).initialize()


def test_concurrent_writer_and_invalid_database_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path, busy_timeout_ms=100)
    identity = _identity()
    blocker = sqlite3.connect(store.path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(LifecycleStorageError, match="^lifecycle_storage_locked$"):
            store.record_transition("b" * 64, identity, _event(identity))
    finally:
        blocker.rollback()
        blocker.close()

    other = tmp_path / "other"
    other.mkdir()
    corrupt = LifecycleStore(other)
    corrupt.path.parent.mkdir(parents=True)
    corrupt.path.write_bytes(b"not a sqlite database")
    with pytest.raises(LifecycleStorageError, match="^lifecycle_storage_corrupt$"):
        corrupt.initialize()
