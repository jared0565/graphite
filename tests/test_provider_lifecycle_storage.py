"""Isolated provider lifecycle persistence tests."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
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
from graphite.routing.lifecycle_storage import (
    LIFECYCLE_SCHEMA_VERSION,
    LifecycleStore,
    LifecycleStorageError,
)


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
    with closing(sqlite3.connect(store.path)) as connection, connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("2",)
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
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(fixture.read_text(encoding="utf-8"))

    store = LifecycleStore(root)
    # First initialize() migrates the v1 fixture to v2; the second is a no-op.
    store.initialize()
    store.initialize()

    assert store.integrity_check() == "ok"
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("2",)


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

    with closing(sqlite3.connect(store.path)) as connection, connection:
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
    with closing(sqlite3.connect(store.path)) as connection, connection:
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
        "schema_version": "2",
    }
    with closing(sqlite3.connect(backup)) as connection, connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_unknown_schema_and_partial_schema_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("UPDATE schema_meta SET value='999' WHERE key='schema_version'")
    with pytest.raises(LifecycleStorageError, match="^lifecycle_schema_unsupported$"):
        LifecycleStore(tmp_path).initialize()

    with closing(sqlite3.connect(store.path)) as connection, connection:
        # Keep the DB at the current version so the migration early-returns and
        # the dropped table surfaces as lifecycle_rollback_required (not a
        # rebuild attempt against a missing table).
        connection.execute("UPDATE schema_meta SET value='2' WHERE key='schema_version'")
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


# --- z.ai schema v1 -> v2 rebuild migration -------------------------------

_V1_CURRENT_OBSERVATIONS_DDL = """CREATE TABLE current_observations (
    boundary_digest TEXT PRIMARY KEY CHECK(length(boundary_digest) = 64 AND boundary_digest NOT GLOB '*[^0-9a-f]*'),
    provider TEXT NOT NULL CHECK(provider IN ('claude-code','codex','ollama','openrouter')),
    runtime_kind TEXT NOT NULL CHECK(runtime_kind IN ('local-cli','local-http','remote-https')),
    identity_digest TEXT CHECK(identity_digest IS NULL OR (length(identity_digest) = 64 AND identity_digest NOT GLOB '*[^0-9a-f]*')),
    state TEXT NOT NULL CHECK(state IN ('discovered','compatible','verification_required','active','incompatible','unavailable')),
    policy_version TEXT NOT NULL,
    identity_json TEXT CHECK(identity_json IS NULL OR length(identity_json) <= 8192),
    observed_at INTEGER NOT NULL CHECK(observed_at >= 0),
    updated_at INTEGER NOT NULL CHECK(updated_at >= observed_at),
    CHECK(
        (provider IN ('claude-code','codex') AND runtime_kind='local-cli')
        OR (provider='ollama' AND runtime_kind='local-http')
        OR (provider='openrouter' AND runtime_kind='remote-https')
    ),
    CHECK(state='unavailable' OR (identity_digest IS NOT NULL AND identity_json IS NOT NULL))
)"""

_V1_LIFECYCLE_EVENTS_DDL = """CREATE TABLE lifecycle_events (
    event_id TEXT PRIMARY KEY,
    boundary_digest TEXT NOT NULL CHECK(length(boundary_digest) = 64 AND boundary_digest NOT GLOB '*[^0-9a-f]*'),
    provider TEXT NOT NULL CHECK(provider IN ('claude-code','codex','ollama','openrouter')),
    runtime_kind TEXT NOT NULL CHECK(runtime_kind IN ('local-cli','local-http','remote-https')),
    previous_identity_digest TEXT CHECK(previous_identity_digest IS NULL OR (length(previous_identity_digest) = 64 AND previous_identity_digest NOT GLOB '*[^0-9a-f]*')),
    current_identity_digest TEXT CHECK(current_identity_digest IS NULL OR (length(current_identity_digest) = 64 AND current_identity_digest NOT GLOB '*[^0-9a-f]*')),
    previous_state TEXT CHECK(previous_state IS NULL OR previous_state IN ('discovered','compatible','verification_required','active','incompatible','unavailable')),
    current_state TEXT NOT NULL CHECK(current_state IN ('discovered','compatible','verification_required','active','incompatible','unavailable')),
    reason TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    occurred_at INTEGER NOT NULL CHECK(occurred_at >= 0),
    payload_json TEXT NOT NULL CHECK(length(payload_json) <= 8192),
    payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK(
        (provider IN ('claude-code','codex') AND runtime_kind='local-cli')
        OR (provider='ollama' AND runtime_kind='local-http')
        OR (provider='openrouter' AND runtime_kind='remote-https')
    )
)"""

_V1_AUTHORITY_INVALIDATIONS_DDL = """CREATE TABLE authority_invalidations (
    invalidation_id TEXT PRIMARY KEY CHECK(length(invalidation_id) = 64 AND invalidation_id NOT GLOB '*[^0-9a-f]*'),
    boundary_digest TEXT NOT NULL CHECK(length(boundary_digest) = 64 AND boundary_digest NOT GLOB '*[^0-9a-f]*'),
    previous_identity_digest TEXT NOT NULL CHECK(length(previous_identity_digest) = 64 AND previous_identity_digest NOT GLOB '*[^0-9a-f]*'),
    current_identity_digest TEXT NOT NULL CHECK(length(current_identity_digest) = 64 AND current_identity_digest NOT GLOB '*[^0-9a-f]*'),
    target_kind TEXT NOT NULL CHECK(target_kind IN ('capability_snapshot','approval')),
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    invalidated_at INTEGER NOT NULL CHECK(invalidated_at >= 0)
)"""

_OBSERVATION_COLUMNS = (
    "boundary_digest,provider,runtime_kind,identity_digest,state,"
    "policy_version,identity_json,observed_at,updated_at"
)


def _build_v1_lifecycle_db(path: Path) -> None:
    """Materialize a genuine schema-v1 lifecycle DB with the old openrouter-only CHECKs."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO schema_meta(key,value) VALUES('schema_version','1')")
        connection.execute(_V1_CURRENT_OBSERVATIONS_DDL)
        connection.execute(_V1_LIFECYCLE_EVENTS_DDL)
        connection.execute(_V1_AUTHORITY_INVALIDATIONS_DDL)
        connection.commit()
    finally:
        connection.close()


def _insert_observation(
    connection: sqlite3.Connection, boundary: str, provider: str
) -> None:
    connection.execute(
        f"INSERT INTO current_observations({_OBSERVATION_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?)",
        (boundary, provider, "remote-https", None, "unavailable", "1.0.0", None, 100, 100),
    )


def test_fresh_lifecycle_store_initializes_at_v2_admitting_zai(tmp_path: Path) -> None:
    assert LIFECYCLE_SCHEMA_VERSION == "2"
    store = _store(tmp_path)

    with closing(sqlite3.connect(store.path)) as connection, connection:
        for table in ("current_observations", "lifecycle_events"):
            ddl = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            assert "'zai'" in ddl, table
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("2",)
        triggers = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        ]
    assert len(triggers) >= 4

    # A fresh install accepts a zai remote-https observation directly.
    store.record_transition(
        "c" * 64,
        None,
        ProviderLifecycleEvent(
            event_id="event-zai",
            provider=LifecycleProviderId.ZAI,
            runtime_kind=RuntimeKind.REMOTE_HTTPS,
            previous_identity_digest=None,
            current_identity_digest=None,
            previous_state=None,
            current_state=ProviderLifecycleState.UNAVAILABLE,
            reason=LifecycleReasonCode.RUNTIME_MISSING,
            policy_version="1.0.0",
            occurred_at=100,
        ),
    )
    observation = store.current_observation("c" * 64)
    assert observation is not None
    assert observation.provider is LifecycleProviderId.ZAI


def test_existing_v1_lifecycle_db_rebuilds_to_v2_and_admits_zai(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / ".graphite" / "routing").mkdir(parents=True)
    db = root / ".graphite" / "routing" / "provider-lifecycle.sqlite3"
    _build_v1_lifecycle_db(db)

    openrouter_boundary = "a" * 64
    zai_boundary = "b" * 64
    with closing(sqlite3.connect(db)) as seed, seed:
        pre_ddl = seed.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='current_observations'"
        ).fetchone()[0]
        assert "'zai'" not in pre_ddl  # genuine v1: the old CHECK omits zai
        with pytest.raises(sqlite3.IntegrityError):  # ...and rejects it outright
            _insert_observation(seed, zai_boundary, "zai")
        _insert_observation(seed, openrouter_boundary, "openrouter")
        seed.commit()

    LifecycleStore(root).initialize()  # triggers the hand-written v1->v2 rebuild

    with closing(sqlite3.connect(db)) as con, con:
        assert con.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("2",)
        for table in ("current_observations", "lifecycle_events"):
            post_ddl = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            assert "'zai'" in post_ddl, table  # rebuild happened (not early-return)
        # The openrouter observation survived the rebuild unchanged.
        assert con.execute("SELECT count(*) FROM current_observations").fetchone()[0] == 1
        assert con.execute(
            "SELECT provider,runtime_kind,state FROM current_observations WHERE boundary_digest=?",
            (openrouter_boundary,),
        ).fetchone() == ("openrouter", "remote-https", "unavailable")
        # A zai remote-https observation is now insertable (was rejected pre-migration).
        _insert_observation(con, zai_boundary, "zai")
        con.commit()
        assert con.execute(
            "SELECT count(*) FROM current_observations WHERE provider='zai'"
        ).fetchone()[0] == 1
        triggers = [
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        ]
    assert len(triggers) >= 4
