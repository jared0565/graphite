"""Isolated SQLite authority storage for provider lifecycle observations."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .lifecycle import (
    LifecycleReasonCode,
    ProviderLifecycleEvent,
    ProviderLifecycleState,
    ProviderRuntimeIdentity,
)
from .storage import (
    BUSY_TIMEOUT_MS,
    _secure_file,
    _secure_repository_directory,
    _selected_root,
    _validate_database_file,
)

LIFECYCLE_SCHEMA_VERSION: Final = "1"
MAX_EVENT_PAGE_SIZE: Final = 100
MAX_INVALIDATION_TARGETS: Final = 128

_PROVIDER_VALUES = "'claude-code','codex','ollama','openrouter'"
_RUNTIME_VALUES = "'local-cli','local-http','remote-https'"
_STATE_VALUES = (
    "'discovered','compatible','verification_required','active','incompatible','unavailable'"
)
_HEX_64_GLOB = "length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


class LifecycleStorageError(RuntimeError):
    """A stable, path-free lifecycle persistence failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _translate_database_error(exc: sqlite3.Error) -> LifecycleStorageError:
    message = str(exc).casefold()
    if "locked" in message or "busy" in message:
        return LifecycleStorageError("lifecycle_storage_locked")
    if (
        "database disk image is malformed" in message
        or "not a database" in message
        or "file is encrypted" in message
    ):
        return LifecycleStorageError("lifecycle_storage_corrupt")
    return LifecycleStorageError("lifecycle_storage_unavailable")


def _digest(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(code)
    return value


def _identifier(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(not (character.isascii() and (character.isalnum() or character in "._:-")) for character in value)
    ):
        raise ValueError(code)
    return value


def _timestamp(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**12:
        raise ValueError(code)
    return value


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _payload_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _identity_from_json(payload: str) -> ProviderRuntimeIdentity:
    try:
        value = json.loads(payload)
        if not isinstance(value, dict) or set(value) != set(ProviderRuntimeIdentity._public_fields):
            raise ValueError
        return ProviderRuntimeIdentity(
            provider=value["provider"],
            runtime_kind=value["runtime_kind"],
            version=value["version"],
            runtime_digest=value["runtime_digest"],
            model_identity_digest=value["model_identity_digest"],
            routing_policy_digest=value["routing_policy_digest"],
            capabilities=tuple(value["capabilities"]),
            policy_version=value["policy_version"],
            observed_at=value["observed_at"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LifecycleStorageError("lifecycle_storage_corrupt") from exc


def _event_from_json(payload: str) -> ProviderLifecycleEvent:
    try:
        value = json.loads(payload)
        if not isinstance(value, dict) or set(value) != set(ProviderLifecycleEvent._public_fields):
            raise ValueError
        return ProviderLifecycleEvent(**value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LifecycleStorageError("lifecycle_storage_corrupt") from exc


@dataclass(frozen=True, slots=True)
class CurrentLifecycleObservation:
    boundary_digest: str
    identity: ProviderRuntimeIdentity | None
    state: ProviderLifecycleState
    updated_at: int


@dataclass(frozen=True, slots=True)
class LifecycleInvalidationRecord:
    invalidation_id: str
    boundary_digest: str
    previous_identity_digest: str
    current_identity_digest: str
    target_kind: str
    target_id: str
    reason: LifecycleReasonCode
    invalidated_at: int


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    f"""CREATE TABLE IF NOT EXISTS current_observations (
        boundary_digest TEXT PRIMARY KEY CHECK({_HEX_64_GLOB.format(column='boundary_digest')}),
        provider TEXT NOT NULL CHECK(provider IN ({_PROVIDER_VALUES})),
        runtime_kind TEXT NOT NULL CHECK(runtime_kind IN ({_RUNTIME_VALUES})),
        identity_digest TEXT CHECK(
            identity_digest IS NULL OR ({_HEX_64_GLOB.format(column='identity_digest')})
        ),
        state TEXT NOT NULL CHECK(state IN ({_STATE_VALUES})),
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
    )""",
    f"""CREATE TABLE IF NOT EXISTS lifecycle_events (
        event_id TEXT PRIMARY KEY,
        boundary_digest TEXT NOT NULL CHECK({_HEX_64_GLOB.format(column='boundary_digest')}),
        provider TEXT NOT NULL CHECK(provider IN ({_PROVIDER_VALUES})),
        runtime_kind TEXT NOT NULL CHECK(runtime_kind IN ({_RUNTIME_VALUES})),
        previous_identity_digest TEXT CHECK(
            previous_identity_digest IS NULL OR ({_HEX_64_GLOB.format(column='previous_identity_digest')})
        ),
        current_identity_digest TEXT CHECK(
            current_identity_digest IS NULL OR ({_HEX_64_GLOB.format(column='current_identity_digest')})
        ),
        previous_state TEXT CHECK(previous_state IS NULL OR previous_state IN ({_STATE_VALUES})),
        current_state TEXT NOT NULL CHECK(current_state IN ({_STATE_VALUES})),
        reason TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        occurred_at INTEGER NOT NULL CHECK(occurred_at >= 0),
        payload_json TEXT NOT NULL CHECK(length(payload_json) <= 8192),
        payload_hash TEXT NOT NULL CHECK({_HEX_64_GLOB.format(column='payload_hash')}),
        CHECK(
            (provider IN ('claude-code','codex') AND runtime_kind='local-cli')
            OR (provider='ollama' AND runtime_kind='local-http')
            OR (provider='openrouter' AND runtime_kind='remote-https')
        )
    )""",
    "CREATE INDEX IF NOT EXISTS lifecycle_events_boundary_time_idx ON lifecycle_events(boundary_digest, occurred_at, event_id)",
    f"""CREATE TABLE IF NOT EXISTS authority_invalidations (
        invalidation_id TEXT PRIMARY KEY CHECK({_HEX_64_GLOB.format(column='invalidation_id')}),
        boundary_digest TEXT NOT NULL CHECK({_HEX_64_GLOB.format(column='boundary_digest')}),
        previous_identity_digest TEXT NOT NULL CHECK({_HEX_64_GLOB.format(column='previous_identity_digest')}),
        current_identity_digest TEXT NOT NULL CHECK({_HEX_64_GLOB.format(column='current_identity_digest')}),
        target_kind TEXT NOT NULL CHECK(target_kind IN ('capability_snapshot','approval')),
        target_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        invalidated_at INTEGER NOT NULL CHECK(invalidated_at >= 0)
    )""",
    "CREATE INDEX IF NOT EXISTS authority_invalidations_boundary_time_idx ON authority_invalidations(boundary_digest, invalidated_at, invalidation_id)",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_event_update_guard
    BEFORE UPDATE ON lifecycle_events BEGIN
        SELECT RAISE(ABORT, 'lifecycle_event_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_event_delete_guard
    BEFORE DELETE ON lifecycle_events BEGIN
        SELECT RAISE(ABORT, 'lifecycle_event_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_invalidation_update_guard
    BEFORE UPDATE ON authority_invalidations BEGIN
        SELECT RAISE(ABORT, 'lifecycle_invalidation_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_invalidation_delete_guard
    BEFORE DELETE ON authority_invalidations BEGIN
        SELECT RAISE(ABORT, 'lifecycle_invalidation_immutable');
    END""",
)


class LifecycleStore:
    """Repository-scoped provider lifecycle authority and sanitized evidence."""

    def __init__(self, repository_root: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> None:
        self.root = _selected_root(repository_root)
        if isinstance(busy_timeout_ms, bool) or not 100 <= busy_timeout_ms <= 30_000:
            raise ValueError("busy_timeout_invalid")
        self.busy_timeout_ms = busy_timeout_ms
        self.path = self.root / ".graphite" / "routing" / "provider-lifecycle.sqlite3"

    @property
    def backup_path(self) -> Path:
        return self.path.parent / "backups" / "provider-lifecycle-schema-v1.sqlite3"

    @property
    def backup_marker_path(self) -> Path:
        return self.path.parent / "backups" / "provider-lifecycle-schema-v1.sha256.json"

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1_000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            return connection
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def initialize(self) -> None:
        _secure_repository_directory(self.root, self.path.parent)
        _validate_database_file(self.path)
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(_SCHEMA[0])
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if version is not None and version[0] != LIFECYCLE_SCHEMA_VERSION:
                raise LifecycleStorageError("lifecycle_schema_unsupported")
            if version is not None:
                self._validate_schema(connection)
            for statement in _SCHEMA[1:]:
                connection.execute(statement)
            connection.execute(
                """INSERT INTO schema_meta(key,value) VALUES('schema_version',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (LIFECYCLE_SCHEMA_VERSION,),
            )
            connection.commit()
            self._validate_integrity(connection)
        except LifecycleStorageError:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise _translate_database_error(exc) from exc
        finally:
            if connection is not None:
                connection.close()
        _validate_database_file(self.path)
        _secure_file(self.path)

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        expected = {
            "current_observations": {
                "boundary_digest", "provider", "runtime_kind", "identity_digest",
                "state", "policy_version", "identity_json", "observed_at", "updated_at",
            },
            "lifecycle_events": {
                "event_id", "boundary_digest", "provider", "runtime_kind",
                "previous_identity_digest", "current_identity_digest", "previous_state",
                "current_state", "reason", "policy_version", "occurred_at",
                "payload_json", "payload_hash",
            },
            "authority_invalidations": {
                "invalidation_id", "boundary_digest", "previous_identity_digest",
                "current_identity_digest", "target_kind", "target_id", "reason",
                "invalidated_at",
            },
        }
        actual = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not set(expected) <= actual:
            raise LifecycleStorageError("lifecycle_rollback_required")
        for table, required_columns in expected.items():
            columns = {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if not required_columns <= columns:
                raise LifecycleStorageError("lifecycle_rollback_required")

    @staticmethod
    def _validate_integrity(connection: sqlite3.Connection) -> None:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if integrity is None or integrity[0] != "ok" or foreign_keys:
            raise LifecycleStorageError("lifecycle_storage_corrupt")

    def integrity_check(self) -> str:
        try:
            with self._connect() as connection:
                self._validate_integrity(connection)
        except LifecycleStorageError:
            raise
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
        return "ok"

    def pragma_state(self) -> dict[str, int | str]:
        try:
            with self._connect() as connection:
                return {
                    "foreign_keys": int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                    "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold(),
                    "busy_timeout": int(connection.execute("PRAGMA busy_timeout").fetchone()[0]),
                }
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def record_transition(
        self,
        boundary_digest: str,
        identity: ProviderRuntimeIdentity | None,
        event: ProviderLifecycleEvent,
    ) -> bool:
        boundary = _digest(boundary_digest, "lifecycle_boundary_invalid")
        if identity is not None and not isinstance(identity, ProviderRuntimeIdentity):
            raise ValueError("lifecycle_record_invalid")
        if not isinstance(event, ProviderLifecycleEvent):
            raise ValueError("lifecycle_record_invalid")
        event_payload = _canonical_json(event.to_dict())
        event_hash = _payload_hash(event_payload)
        identity_payload = None if identity is None else _canonical_json(identity.to_dict())
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            existing_event = connection.execute(
                "SELECT boundary_digest,payload_hash FROM lifecycle_events WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
            if existing_event is not None:
                if existing_event["boundary_digest"] != boundary or existing_event["payload_hash"] != event_hash:
                    raise LifecycleStorageError("lifecycle_event_changed")
                connection.rollback()
                return False
            if identity is None:
                if (
                    event.current_state is not ProviderLifecycleState.UNAVAILABLE
                    or event.current_identity_digest is not None
                ):
                    raise LifecycleStorageError("lifecycle_identity_mismatch")
            else:
                if (
                    event.provider is not identity.provider
                    or event.runtime_kind is not identity.runtime_kind
                    or event.current_identity_digest != identity.digest
                ):
                    raise LifecycleStorageError("lifecycle_identity_mismatch")
                if event.policy_version != identity.policy_version:
                    raise LifecycleStorageError("lifecycle_policy_mismatch")
                if event.occurred_at != identity.observed_at:
                    raise LifecycleStorageError("lifecycle_observation_time_mismatch")
            current = connection.execute(
                """SELECT provider,runtime_kind,identity_digest,state
                FROM current_observations WHERE boundary_digest=?""",
                (boundary,),
            ).fetchone()
            if current is None:
                if event.previous_identity_digest is not None or event.previous_state is not None:
                    raise LifecycleStorageError("lifecycle_transition_stale")
            elif (
                current["provider"] != event.provider.value
                or current["runtime_kind"] != event.runtime_kind.value
                or current["identity_digest"] != event.previous_identity_digest
                or current["state"] != event.previous_state.value
            ):
                raise LifecycleStorageError("lifecycle_transition_stale")
            connection.execute(
                """INSERT INTO lifecycle_events(
                    event_id,boundary_digest,provider,runtime_kind,
                    previous_identity_digest,current_identity_digest,
                    previous_state,current_state,reason,policy_version,
                    occurred_at,payload_json,payload_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.event_id,
                    boundary,
                    event.provider.value,
                    event.runtime_kind.value,
                    event.previous_identity_digest,
                    event.current_identity_digest,
                    None if event.previous_state is None else event.previous_state.value,
                    event.current_state.value,
                    event.reason.value,
                    event.policy_version,
                    event.occurred_at,
                    event_payload,
                    event_hash,
                ),
            )
            connection.execute(
                """INSERT INTO current_observations(
                    boundary_digest,provider,runtime_kind,identity_digest,state,
                    policy_version,identity_json,observed_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(boundary_digest) DO UPDATE SET
                    provider=excluded.provider,
                    runtime_kind=excluded.runtime_kind,
                    identity_digest=excluded.identity_digest,
                    state=excluded.state,
                    policy_version=excluded.policy_version,
                    identity_json=excluded.identity_json,
                    observed_at=excluded.observed_at,
                    updated_at=excluded.updated_at""",
                (
                    boundary,
                    event.provider.value,
                    event.runtime_kind.value,
                    None if identity is None else identity.digest,
                    event.current_state.value,
                    event.policy_version,
                    identity_payload,
                    event.occurred_at if identity is None else identity.observed_at,
                    event.occurred_at,
                ),
            )
            connection.commit()
            return True
        except LifecycleStorageError:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise _translate_database_error(exc) from exc
        finally:
            if connection is not None:
                connection.close()

    def current_observation(
        self, boundary_digest: str
    ) -> CurrentLifecycleObservation | None:
        boundary = _digest(boundary_digest, "lifecycle_boundary_invalid")
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """SELECT identity_json,state,updated_at FROM current_observations
                    WHERE boundary_digest=?""",
                    (boundary,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
        if row is None:
            return None
        try:
            state = ProviderLifecycleState(row["state"])
        except ValueError as exc:
            raise LifecycleStorageError("lifecycle_storage_corrupt") from exc
        identity_payload = row["identity_json"]
        identity = None if identity_payload is None else _identity_from_json(identity_payload)
        return CurrentLifecycleObservation(boundary, identity, state, int(row["updated_at"]))

    def events(
        self, boundary_digest: str, *, limit: int = MAX_EVENT_PAGE_SIZE
    ) -> tuple[ProviderLifecycleEvent, ...]:
        boundary = _digest(boundary_digest, "lifecycle_boundary_invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_EVENT_PAGE_SIZE:
            raise ValueError("events_limit_invalid")
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """SELECT payload_json FROM lifecycle_events
                    WHERE boundary_digest=? ORDER BY occurred_at DESC,event_id DESC LIMIT ?""",
                    (boundary, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
        return tuple(reversed([_event_from_json(row["payload_json"]) for row in rows]))

    def record_invalidations(
        self,
        *,
        boundary_digest: str,
        previous_identity_digest: str,
        current_identity_digest: str,
        capability_snapshot_digests: tuple[str, ...],
        approval_ids: tuple[str, ...],
        reason: LifecycleReasonCode,
        invalidated_at: int,
    ) -> int:
        boundary = _digest(boundary_digest, "lifecycle_boundary_invalid")
        previous = _digest(previous_identity_digest, "previous_identity_digest_invalid")
        current = _digest(current_identity_digest, "current_identity_digest_invalid")
        try:
            normalized_reason = LifecycleReasonCode(reason)
        except (TypeError, ValueError) as exc:
            raise ValueError("lifecycle_reason_invalid") from exc
        observed = _timestamp(invalidated_at, "invalidated_at_invalid")
        if not isinstance(capability_snapshot_digests, (tuple, list)) or not isinstance(
            approval_ids, (tuple, list)
        ):
            raise ValueError("invalidation_targets_invalid")
        if len(capability_snapshot_digests) + len(approval_ids) > MAX_INVALIDATION_TARGETS:
            raise ValueError("invalidation_targets_invalid")
        targets = [
            ("capability_snapshot", _digest(value, "capability_snapshot_digest_invalid"))
            for value in capability_snapshot_digests
        ]
        targets.extend(("approval", _identifier(value, "approval_id_invalid")) for value in approval_ids)
        if len(set(targets)) != len(targets):
            raise ValueError("invalidation_targets_invalid")
        inserted = 0
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            for target_kind, target_id in sorted(targets):
                payload = {
                    "boundary_digest": boundary,
                    "previous_identity_digest": previous,
                    "current_identity_digest": current,
                    "target_kind": target_kind,
                    "target_id": target_id,
                    "reason": normalized_reason.value,
                    "invalidated_at": observed,
                }
                invalidation_id = _payload_hash(_canonical_json(payload))
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO authority_invalidations(
                        invalidation_id,boundary_digest,previous_identity_digest,
                        current_identity_digest,target_kind,target_id,reason,invalidated_at
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        invalidation_id,
                        boundary,
                        previous,
                        current,
                        target_kind,
                        target_id,
                        normalized_reason.value,
                        observed,
                    ),
                )
                inserted += cursor.rowcount
            connection.commit()
            return inserted
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise _translate_database_error(exc) from exc
        finally:
            if connection is not None:
                connection.close()

    def invalidations(
        self, boundary_digest: str, *, limit: int = MAX_EVENT_PAGE_SIZE
    ) -> tuple[LifecycleInvalidationRecord, ...]:
        boundary = _digest(boundary_digest, "lifecycle_boundary_invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_EVENT_PAGE_SIZE:
            raise ValueError("invalidations_limit_invalid")
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """SELECT * FROM authority_invalidations WHERE boundary_digest=?
                    ORDER BY invalidated_at,invalidation_id LIMIT ?""",
                    (boundary, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
        try:
            return tuple(
                LifecycleInvalidationRecord(
                    invalidation_id=row["invalidation_id"],
                    boundary_digest=row["boundary_digest"],
                    previous_identity_digest=row["previous_identity_digest"],
                    current_identity_digest=row["current_identity_digest"],
                    target_kind=row["target_kind"],
                    target_id=row["target_id"],
                    reason=LifecycleReasonCode(row["reason"]),
                    invalidated_at=int(row["invalidated_at"]),
                )
                for row in rows
            )
        except (TypeError, ValueError) as exc:
            raise LifecycleStorageError("lifecycle_storage_corrupt") from exc

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            raise LifecycleStorageError("lifecycle_backup_failed") from exc
        return digest.hexdigest()

    @staticmethod
    def _atomic_private_write(path: Path, payload: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise LifecycleStorageError("lifecycle_backup_failed")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, path)
            _secure_file(path)
        except LifecycleStorageError:
            raise
        except OSError as exc:
            raise LifecycleStorageError("lifecycle_backup_failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def create_verified_backup(self) -> tuple[Path, Path]:
        backup = self.backup_path
        marker = self.backup_marker_path
        _secure_repository_directory(self.root, backup.parent)
        temporary = backup.with_name(f".{backup.name}.{secrets.token_hex(8)}.tmp")
        try:
            _validate_database_file(backup)
            with closing(
                sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1_000)
            ) as source:
                with closing(sqlite3.connect(temporary)) as destination:
                    source.backup(destination)
                    integrity = destination.execute("PRAGMA integrity_check").fetchone()
                    version = destination.execute(
                        "SELECT value FROM schema_meta WHERE key='schema_version'"
                    ).fetchone()
                    if integrity != ("ok",) or version != (LIFECYCLE_SCHEMA_VERSION,):
                        raise LifecycleStorageError("lifecycle_backup_failed")
            _secure_file(temporary)
            os.replace(temporary, backup)
            _secure_file(backup)
            evidence = _canonical_json(
                {
                    "backup_sha256": self._file_sha256(backup),
                    "schema_version": LIFECYCLE_SCHEMA_VERSION,
                }
            ).encode("utf-8")
            self._atomic_private_write(marker, evidence)
            return backup, marker
        except LifecycleStorageError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise LifecycleStorageError("lifecycle_backup_failed") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
