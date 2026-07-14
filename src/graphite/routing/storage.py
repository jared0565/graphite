"""SQLite evidence storage with repository and aggregate trust boundaries."""
from __future__ import annotations

import os
import hashlib
import json
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .contracts import Effort, ExecutionOutcome, ExecutionReceipt, RiskTier, TaskCategory

SCHEMA_VERSION: Final = "2"
BUSY_TIMEOUT_MS: Final = 2_000
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}(?::cloud)?$")
_SNAPSHOT_MODEL_ID = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,127}(?::[a-z0-9][a-z0-9._-]{0,63})?$"
)
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_VERSION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_TABLES = frozenset(
    {
        "tasks",
        "decisions",
        "approvals",
        "executions",
        "outcomes",
        "shadow_comparisons",
        "policy_versions",
        "budget_ledger",
        "confidence_stats",
        "registry_snapshots",
        "execution_evidence",
        "execution_attempts",
        "execution_receipts",
        "staged_execution_receipts",
        "incident_reviews",
        "blind_comparisons",
    }
)


class StorageError(RuntimeError):
    """A stable, path-free persistence failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _is_reparse_point(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _selected_root(root: Path) -> Path:
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise StorageError("repository_root_invalid") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
    ):
        raise StorageError("repository_root_invalid")
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise StorageError("repository_root_invalid") from exc


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(code)
    return value


def _nonnegative_integer(value: object, code: str, maximum: int = 10**12) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ValueError(code)
    return value


def _translate_database_error(exc: sqlite3.Error) -> StorageError:
    message = str(exc).casefold()
    if "locked" in message or "busy" in message:
        return StorageError("storage_locked")
    if "database disk image is malformed" in message or "not a database" in message:
        return StorageError("storage_corrupt")
    return StorageError("storage_unavailable")


def _secure_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_reparse_point(metadata)
        ):
            raise StorageError("storage_path_invalid")
        if os.name != "nt":
            path.chmod(0o700)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError("storage_unavailable") from exc


def _secure_repository_directory(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise StorageError("storage_path_invalid") from exc
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            metadata = None
        except OSError as exc:
            raise StorageError("storage_unavailable") from exc
        if metadata is not None:
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse_point(metadata)
            ):
                raise StorageError("storage_path_invalid")
        else:
            try:
                current.mkdir(mode=0o700)
            except OSError as exc:
                raise StorageError("storage_unavailable") from exc
        if os.name != "nt":
            try:
                current.chmod(0o700)
            except OSError as exc:
                raise StorageError("storage_unavailable") from exc


def _validate_database_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise StorageError("storage_unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
    ):
        raise StorageError("storage_path_invalid")


def _secure_file(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        path.chmod(0o600)
    except OSError as exc:
        raise StorageError("storage_unavailable") from exc


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        risk TEXT NOT NULL,
        objective_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS decisions (
        decision_id TEXT PRIMARY KEY,
        task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE,
        model_id TEXT,
        effort TEXT,
        policy_version TEXT NOT NULL,
        evidence_version TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS approvals (
        approval_id TEXT PRIMARY KEY,
        task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE,
        decision_id TEXT REFERENCES decisions(decision_id) ON DELETE CASCADE,
        nonce_hash TEXT NOT NULL UNIQUE,
        manifest_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        reserved_tokens INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS executions (
        execution_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
        decision_id TEXT REFERENCES decisions(decision_id) ON DELETE SET NULL,
        approval_id TEXT REFERENCES approvals(approval_id) ON DELETE SET NULL,
        model_id TEXT NOT NULL,
        effort TEXT NOT NULL,
        status TEXT NOT NULL,
        reserved_tokens INTEGER NOT NULL,
        actual_input_tokens INTEGER,
        actual_output_tokens INTEGER,
        created_at INTEGER NOT NULL,
        completed_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS outcomes (
        outcome_id TEXT PRIMARY KEY,
        execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
        provenance TEXT NOT NULL,
        success INTEGER NOT NULL CHECK (success IN (0, 1)),
        severe_failure INTEGER NOT NULL CHECK (severe_failure IN (0, 1)),
        recorded_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS shadow_comparisons (
        comparison_id TEXT PRIMARY KEY,
        primary_execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
        shadow_execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
        verdict TEXT,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS policy_versions (
        policy_version TEXT PRIMARY KEY,
        policy_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS budget_ledger (
        entry_id TEXT PRIMARY KEY,
        approval_id TEXT REFERENCES approvals(approval_id) ON DELETE SET NULL,
        execution_id TEXT REFERENCES executions(execution_id) ON DELETE SET NULL,
        entry_type TEXT NOT NULL,
        token_amount INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS confidence_stats (
        model_id TEXT NOT NULL,
        effort TEXT NOT NULL,
        category TEXT NOT NULL,
        risk TEXT NOT NULL,
        sample_count INTEGER NOT NULL,
        success_count INTEGER NOT NULL,
        severe_failure_count INTEGER NOT NULL,
        PRIMARY KEY (model_id, effort, category, risk)
    )""",
    "CREATE INDEX IF NOT EXISTS outcomes_recorded_at_idx ON outcomes(recorded_at)",
    "CREATE INDEX IF NOT EXISTS executions_task_idx ON executions(task_id)",
    """CREATE TABLE IF NOT EXISTS execution_evidence (
        execution_id TEXT PRIMARY KEY REFERENCES executions(execution_id) ON DELETE CASCADE,
        task_id TEXT NOT NULL,
        decision_id TEXT NOT NULL,
        graph_fingerprint TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS execution_attempts (
        attempt_id TEXT PRIMARY KEY,
        approval_id TEXT NOT NULL UNIQUE REFERENCES approvals(approval_id) ON DELETE RESTRICT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
        decision_id TEXT NOT NULL REFERENCES decisions(decision_id) ON DELETE RESTRICT,
        manifest_hash TEXT NOT NULL,
        graph_fingerprint TEXT NOT NULL,
        model_id TEXT NOT NULL,
        effort TEXT NOT NULL,
        reserved_tokens INTEGER NOT NULL,
        max_input_tokens INTEGER NOT NULL,
        max_output_tokens INTEGER NOT NULL,
        expected_prompt_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'failed', 'persistence_failed')),
        failure_reason TEXT,
        execution_id TEXT UNIQUE REFERENCES executions(execution_id) ON DELETE RESTRICT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS staged_execution_receipts (
        attempt_id TEXT PRIMARY KEY REFERENCES execution_attempts(attempt_id) ON DELETE CASCADE,
        execution_id TEXT NOT NULL UNIQUE,
        approval_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        effort TEXT NOT NULL,
        outcome TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        latency_ms INTEGER NOT NULL,
        prompt_hash TEXT NOT NULL,
        response_hash TEXT NOT NULL,
        failure_reason TEXT,
        staged_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS execution_receipts (
        execution_id TEXT PRIMARY KEY REFERENCES executions(execution_id) ON DELETE CASCADE,
        approval_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        effort TEXT NOT NULL,
        outcome TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        latency_ms INTEGER NOT NULL,
        prompt_hash TEXT NOT NULL,
        response_hash TEXT NOT NULL,
        failure_reason TEXT,
        completed_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS incident_reviews (
        review_id INTEGER PRIMARY KEY AUTOINCREMENT,
        execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
        reviewed_at INTEGER NOT NULL,
        UNIQUE(execution_id, reviewed_at)
    )""",
    """CREATE TABLE IF NOT EXISTS blind_comparisons (
        comparison_id TEXT PRIMARY KEY,
        primary_execution_id TEXT NOT NULL,
        shadow_execution_id TEXT NOT NULL,
        label_a_hash TEXT NOT NULL,
        label_b_hash TEXT NOT NULL,
        label_a_is_shadow INTEGER NOT NULL CHECK (label_a_is_shadow IN (0, 1)),
        verdict TEXT,
        recorded_at INTEGER,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS registry_snapshots (
        snapshot_id INTEGER PRIMARY KEY CHECK (snapshot_id = 1),
        payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        refreshed_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
    )""",
)


class RepositoryStore:
    """Detailed evidence that never leaves the selected repository by default."""

    def __init__(self, repository_root: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> None:
        self.root = _selected_root(repository_root)
        if isinstance(busy_timeout_ms, bool) or not 100 <= busy_timeout_ms <= 30_000:
            raise ValueError("busy_timeout_invalid")
        self.busy_timeout_ms = busy_timeout_ms
        self.path = self.root / ".graphite" / "routing" / "events.sqlite3"

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
            existing_version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if existing_version is not None and existing_version[0] not in {"1", SCHEMA_VERSION}:
                raise StorageError("storage_schema_unsupported")
            for statement in _SCHEMA[1:]:
                connection.execute(statement)
            if existing_version is not None and existing_version[0] == "1":
                attempt_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(execution_attempts)")
                }
                for name, declaration in (
                    ("max_input_tokens", "INTEGER"),
                    ("max_output_tokens", "INTEGER"),
                    ("expected_prompt_hash", "TEXT"),
                ):
                    if name not in attempt_columns:
                        connection.execute(
                            f"ALTER TABLE execution_attempts ADD COLUMN {name} {declaration}"
                        )
            connection.execute(
                """INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (SCHEMA_VERSION,),
            )
            connection.commit()
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise StorageError("storage_corrupt")
        except StorageError:
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

    def integrity_check(self) -> str:
        try:
            with self._connect() as connection:
                row = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
        if row is None or row[0] != "ok":
            raise StorageError("storage_corrupt")
        return "ok"

    def pragma_state(self) -> dict[str, int | str]:
        with self._connect() as connection:
            foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
            busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
        return {
            "foreign_keys": foreign_keys,
            "journal_mode": journal_mode,
            "busy_timeout": busy_timeout,
        }

    def record_task(
        self,
        task_id: str,
        category: str,
        risk: str,
        objective_hash: str,
        created_at: int,
    ) -> bool:
        task_id = _identifier(task_id, "task_id_invalid")
        category = TaskCategory(category).value
        risk = RiskTier(risk).value
        if not _HEX_64.fullmatch(objective_hash):
            raise ValueError("objective_hash_invalid")
        created_at = _nonnegative_integer(created_at, "created_at_invalid")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO tasks(task_id, category, risk, objective_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (task_id, category, risk, objective_hash, created_at),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def record_decision(
        self,
        decision_id: str,
        task_id: str,
        model_id: str | None,
        effort: str | None,
        policy_version: str,
        evidence_version: str,
        created_at: int,
    ) -> bool:
        values = (
            _identifier(decision_id, "decision_id_invalid"),
            _identifier(task_id, "task_id_invalid"),
            None if model_id is None else _identifier(model_id, "model_id_invalid"),
            None if effort is None else Effort(effort).value,
            _identifier(policy_version, "policy_version_invalid"),
            _identifier(evidence_version, "evidence_version_invalid"),
            _nonnegative_integer(created_at, "created_at_invalid"),
        )
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO decisions(
                        decision_id, task_id, model_id, effort, policy_version,
                        evidence_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def insert_execution(
        self,
        *,
        execution_id: str,
        idempotency_key: str,
        task_id: str | None,
        decision_id: str | None,
        approval_id: str | None,
        model_id: str,
        effort: str,
        status: str,
        reserved_tokens: int,
        created_at: int,
    ) -> bool:
        values = {
            "execution_id": _identifier(execution_id, "execution_id_invalid"),
            "idempotency_key": _identifier(idempotency_key, "idempotency_key_invalid"),
            "task_id": None if task_id is None else _identifier(task_id, "task_id_invalid"),
            "decision_id": None if decision_id is None else _identifier(decision_id, "decision_id_invalid"),
            "approval_id": None if approval_id is None else _identifier(approval_id, "approval_id_invalid"),
            "model_id": _identifier(model_id, "model_id_invalid"),
            "effort": Effort(effort).value,
            "status": _identifier(status, "status_invalid"),
            "reserved_tokens": _nonnegative_integer(reserved_tokens, "reserved_tokens_invalid"),
            "created_at": _nonnegative_integer(created_at, "created_at_invalid"),
        }
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT execution_id FROM executions WHERE idempotency_key = ?",
                (values["idempotency_key"],),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return False
            connection.execute(
                """INSERT INTO executions(
                    execution_id, idempotency_key, task_id, decision_id, approval_id,
                    model_id, effort, status, reserved_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(values.values()),
            )
            connection.commit()
            return True
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise _translate_database_error(exc) from exc
        finally:
            if connection is not None:
                connection.close()

    def record_outcome(
        self,
        outcome_id: str,
        execution_id: str,
        provenance: str,
        success: bool,
        severe_failure: bool,
        recorded_at: int,
    ) -> bool:
        outcome_id = _identifier(outcome_id, "outcome_id_invalid")
        execution_id = _identifier(execution_id, "execution_id_invalid")
        if provenance not in {"machine_verified", "ci_imported", "human", "pairwise", "reversion", "ambiguous"}:
            raise ValueError("provenance_invalid")
        if not isinstance(success, bool) or not isinstance(severe_failure, bool):
            raise ValueError("outcome_invalid")
        recorded_at = _nonnegative_integer(recorded_at, "recorded_at_invalid")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO outcomes(outcome_id, execution_id, provenance, success, severe_failure, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (outcome_id, execution_id, provenance, int(success), int(severe_failure), recorded_at),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def link_execution_evidence(
        self,
        execution_id: str,
        task_id: str,
        decision_id: str,
        graph_fingerprint: str,
    ) -> bool:
        values = (
            _identifier(execution_id, "execution_id_invalid"),
            _identifier(task_id, "task_id_invalid"),
            _identifier(decision_id, "decision_id_invalid"),
            graph_fingerprint,
        )
        if not _HEX_64.fullmatch(graph_fingerprint):
            raise ValueError("graph_fingerprint_invalid")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO execution_evidence(
                        execution_id, task_id, decision_id, graph_fingerprint
                    ) VALUES (?, ?, ?, ?)""",
                    values,
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def execution_evidence(self, execution_id: str) -> dict[str, str] | None:
        execution_id = _identifier(execution_id, "execution_id_invalid")
        with self._connect() as connection:
            row = connection.execute(
                """SELECT ee.task_id, ee.decision_id, ee.graph_fingerprint
                FROM execution_evidence ee
                JOIN executions e ON e.execution_id = ee.execution_id
                WHERE ee.execution_id = ?
                  AND e.task_id = ee.task_id AND e.decision_id = ee.decision_id""",
                (execution_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def create_execution_attempt(
        self,
        *,
        attempt_id: str,
        approval_id: str,
        task_id: str,
        decision_id: str,
        manifest_hash: str,
        graph_fingerprint: str,
        model_id: str,
        effort: str,
        reserved_tokens: int,
        max_input_tokens: int,
        max_output_tokens: int,
        expected_prompt_hash: str,
        created_at: int,
    ) -> bool:
        values = (
            _identifier(attempt_id, "attempt_id_invalid"),
            _identifier(approval_id, "approval_id_invalid"),
            _identifier(task_id, "task_id_invalid"),
            _identifier(decision_id, "decision_id_invalid"),
            manifest_hash,
            graph_fingerprint,
            _identifier(model_id, "model_id_invalid"),
            Effort(effort).value,
            _nonnegative_integer(reserved_tokens, "reserved_tokens_invalid"),
            _nonnegative_integer(max_input_tokens, "max_input_tokens_invalid"),
            _nonnegative_integer(max_output_tokens, "max_output_tokens_invalid"),
            expected_prompt_hash,
            _nonnegative_integer(created_at, "created_at_invalid"),
        )
        if not _HEX_64.fullmatch(manifest_hash):
            raise ValueError("manifest_hash_invalid")
        if not _HEX_64.fullmatch(graph_fingerprint):
            raise ValueError("graph_fingerprint_invalid")
        if not _HEX_64.fullmatch(expected_prompt_hash):
            raise ValueError("prompt_hash_invalid")
        if values[9] < 1 or values[10] < 1 or values[9] + values[10] != values[8]:
            raise ValueError("token_bounds_invalid")
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT attempt_id, task_id, decision_id, manifest_hash, "
                "graph_fingerprint, model_id, effort, reserved_tokens, max_input_tokens, "
                "max_output_tokens, expected_prompt_hash "
                "FROM execution_attempts WHERE approval_id = ?",
                (values[1],),
            ).fetchone()
            if existing is not None:
                expected = (values[0], *values[2:12])
                if tuple(existing) != expected:
                    raise StorageError("execution_attempt_conflict")
                connection.rollback()
                return False
            approval = connection.execute(
                "SELECT manifest_hash, reserved_tokens, status FROM approvals "
                "WHERE approval_id = ?",
                (values[1],),
            ).fetchone()
            decision = connection.execute(
                "SELECT task_id, model_id, effort FROM decisions WHERE decision_id = ?",
                (values[3],),
            ).fetchone()
            if (
                approval is None
                or tuple(approval) != (values[4], values[8], "issued")
                or decision is None
                or tuple(decision) != (values[2], values[6], values[7])
            ):
                raise StorageError("execution_attempt_conflict")
            connection.execute(
                """INSERT INTO execution_attempts(
                    attempt_id, approval_id, task_id, decision_id, manifest_hash,
                    graph_fingerprint, model_id, effort, reserved_tokens,
                    max_input_tokens, max_output_tokens, expected_prompt_hash, status,
                    failure_reason, execution_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, ?)""",
                (*values[:12], values[12], values[12]),
            )
            connection.commit()
            return True
        except StorageError:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise StorageError("execution_attempt_conflict") from exc
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise _translate_database_error(exc) from exc
        finally:
            if connection is not None:
                connection.close()

    def mark_execution_attempt_failed(
        self,
        attempt_id: str,
        reason: str,
        *,
        persistence_failed: bool = False,
        updated_at: int,
    ) -> bool:
        attempt_id = _identifier(attempt_id, "attempt_id_invalid")
        reason = _identifier(reason, "failure_reason_invalid")
        updated_at = _nonnegative_integer(updated_at, "updated_at_invalid")
        status = "persistence_failed" if persistence_failed else "failed"
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """UPDATE execution_attempts
                    SET status = ?, failure_reason = ?, updated_at = ?
                    WHERE attempt_id = ? AND status IN ('pending', 'persistence_failed')""",
                    (status, reason, updated_at, attempt_id),
                )
                if cursor.rowcount == 1:
                    return True
                row = connection.execute(
                    "SELECT status, failure_reason FROM execution_attempts WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
                if row is None:
                    raise StorageError("execution_attempt_missing")
                if tuple(row) == (status, reason):
                    return False
                raise StorageError("execution_attempt_conflict")
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    @staticmethod
    def _validate_receipt_shape(receipt: ExecutionReceipt) -> None:
        if not isinstance(receipt, ExecutionReceipt):
            raise ValueError("execution_receipt_invalid")
        if (
            receipt.outcome is not ExecutionOutcome.SUCCEEDED
            or receipt.input_tokens is None
            or receipt.output_tokens is None
            or receipt.response_hash is None
            or receipt.failure_reason is not None
        ):
            raise ValueError("execution_receipt_invalid")

    @staticmethod
    def _receipt_tuple(receipt: ExecutionReceipt) -> tuple[Any, ...]:
        return (
            receipt.approval_id, receipt.model_id, receipt.effort.value,
            receipt.outcome.value, receipt.input_tokens, receipt.output_tokens,
            receipt.latency_ms, receipt.prompt_hash, receipt.response_hash, None,
        )

    @staticmethod
    def _receipt_from_row(row: sqlite3.Row) -> ExecutionReceipt:
        return ExecutionReceipt(
            execution_id=str(row["execution_id"]),
            approval_id=str(row["approval_id"]),
            model_id=str(row["model_id"]),
            effort=Effort(str(row["effort"])),
            outcome=ExecutionOutcome(str(row["outcome"])),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            latency_ms=int(row["latency_ms"]),
            prompt_hash=str(row["prompt_hash"]),
            response_hash=str(row["response_hash"]),
            failure_reason=None,
        )

    @staticmethod
    def _validate_attempt_binding(attempt: sqlite3.Row, receipt: ExecutionReceipt) -> None:
        if (
            attempt["approval_id"] != receipt.approval_id
            or attempt["model_id"] != receipt.model_id
            or attempt["effort"] != receipt.effort.value
            or attempt["max_input_tokens"] is None
            or attempt["max_output_tokens"] is None
            or attempt["expected_prompt_hash"] is None
            or receipt.input_tokens > attempt["max_input_tokens"]
            or receipt.output_tokens > attempt["max_output_tokens"]
            or receipt.input_tokens + receipt.output_tokens > attempt["reserved_tokens"]
            or receipt.prompt_hash != attempt["expected_prompt_hash"]
        ):
            raise StorageError("execution_attempt_conflict")

    @staticmethod
    def _assert_approval_consumed(
        connection: sqlite3.Connection, attempt: sqlite3.Row
    ) -> None:
        approval = connection.execute(
            "SELECT status, manifest_hash, reserved_tokens FROM approvals WHERE approval_id = ?",
            (attempt["approval_id"],),
        ).fetchone()
        decision = connection.execute(
            "SELECT task_id, model_id, effort FROM decisions WHERE decision_id = ?",
            (attempt["decision_id"],),
        ).fetchone()
        if approval is None or approval["status"] != "consumed":
            raise StorageError("approval_not_consumed")
        if (
            approval["manifest_hash"] != attempt["manifest_hash"]
            or approval["reserved_tokens"] != attempt["reserved_tokens"]
            or decision is None
            or tuple(decision)
            != (attempt["task_id"], attempt["model_id"], attempt["effort"])
        ):
            raise StorageError("execution_attempt_conflict")

    @staticmethod
    def _finalize_rows(
        connection: sqlite3.Connection,
        attempt: sqlite3.Row,
        receipt: ExecutionReceipt,
        completed_at: int,
    ) -> None:
        connection.execute(
            """INSERT INTO executions(
                execution_id, idempotency_key, task_id, decision_id, approval_id,
                model_id, effort, status, reserved_tokens, actual_input_tokens,
                actual_output_tokens, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt.execution_id, attempt["attempt_id"], attempt["task_id"],
                attempt["decision_id"], receipt.approval_id, receipt.model_id,
                receipt.effort.value, receipt.outcome.value, attempt["reserved_tokens"],
                receipt.input_tokens, receipt.output_tokens, attempt["created_at"],
                completed_at,
            ),
        )
        connection.execute(
            """INSERT INTO execution_receipts(
                execution_id, approval_id, model_id, effort, outcome, input_tokens,
                output_tokens, latency_ms, prompt_hash, response_hash, failure_reason,
                completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            (
                receipt.execution_id,
                *RepositoryStore._receipt_tuple(receipt)[:-1],
                completed_at,
            ),
        )
        connection.execute(
            """INSERT INTO execution_evidence(
                execution_id, task_id, decision_id, graph_fingerprint
            ) VALUES (?, ?, ?, ?)""",
            (
                receipt.execution_id, attempt["task_id"], attempt["decision_id"],
                attempt["graph_fingerprint"],
            ),
        )
        connection.execute(
            """UPDATE execution_attempts
            SET status = 'completed', failure_reason = NULL, execution_id = ?, updated_at = ?
            WHERE attempt_id = ?""",
            (receipt.execution_id, completed_at, attempt["attempt_id"]),
        )
        connection.execute(
            "DELETE FROM staged_execution_receipts WHERE attempt_id = ?",
            (attempt["attempt_id"],),
        )

    def finalize_execution_attempt(
        self,
        *,
        attempt_id: str,
        receipt: ExecutionReceipt,
        completed_at: int,
    ) -> bool:
        attempt_id = _identifier(attempt_id, "attempt_id_invalid")
        self._validate_receipt_shape(receipt)
        completed_at = _nonnegative_integer(completed_at, "completed_at_invalid")
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise StorageError("execution_attempt_missing")
            self._assert_approval_consumed(connection, attempt)
            if attempt["status"] == "completed":
                stored = connection.execute(
                    "SELECT approval_id, model_id, effort, outcome, input_tokens, "
                    "output_tokens, latency_ms, prompt_hash, response_hash, failure_reason "
                    "FROM execution_receipts WHERE execution_id = ?",
                    (receipt.execution_id,),
                ).fetchone()
                expected = self._receipt_tuple(receipt)
                if attempt["execution_id"] != receipt.execution_id or tuple(stored or ()) != expected:
                    raise StorageError("execution_attempt_conflict")
                connection.rollback()
                return False
            if attempt["status"] not in {"pending", "persistence_failed"}:
                raise StorageError("execution_attempt_conflict")
            self._validate_attempt_binding(attempt, receipt)
            staged = connection.execute(
                "SELECT * FROM staged_execution_receipts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if (
                staged is not None
                and self._receipt_tuple(self._receipt_from_row(staged))
                != self._receipt_tuple(receipt)
            ):
                raise StorageError("execution_attempt_conflict")
            self._finalize_rows(connection, attempt, receipt, completed_at)
            connection.commit()
            return True
        except StorageError:
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

    def stage_execution_receipt(
        self,
        *,
        attempt_id: str,
        receipt: ExecutionReceipt,
        staged_at: int,
    ) -> bool:
        attempt_id = _identifier(attempt_id, "attempt_id_invalid")
        self._validate_receipt_shape(receipt)
        staged_at = _nonnegative_integer(staged_at, "staged_at_invalid")
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise StorageError("execution_attempt_missing")
            self._assert_approval_consumed(connection, attempt)
            if attempt["status"] not in {"pending", "persistence_failed"}:
                raise StorageError("execution_attempt_conflict")
            self._validate_attempt_binding(attempt, receipt)
            existing = connection.execute(
                "SELECT * FROM staged_execution_receipts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                if self._receipt_tuple(self._receipt_from_row(existing)) != self._receipt_tuple(receipt):
                    raise StorageError("execution_attempt_conflict")
                connection.rollback()
                return False
            connection.execute(
                """INSERT INTO staged_execution_receipts(
                    attempt_id, execution_id, approval_id, model_id, effort, outcome,
                    input_tokens, output_tokens, latency_ms, prompt_hash, response_hash,
                    failure_reason, staged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (attempt_id, receipt.execution_id, *self._receipt_tuple(receipt)[:-1], staged_at),
            )
            connection.execute(
                """UPDATE execution_attempts SET status = 'persistence_failed',
                failure_reason = 'execution_persistence_failed', updated_at = ?
                WHERE attempt_id = ?""",
                (staged_at, attempt_id),
            )
            connection.commit()
            return True
        except StorageError:
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

    def recoverable_attempt_ids(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT a.attempt_id FROM execution_attempts a
                JOIN staged_execution_receipts s ON s.attempt_id = a.attempt_id
                WHERE a.status = 'persistence_failed' ORDER BY a.attempt_id"""
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def reconcile_execution_attempt(
        self, attempt_id: str, *, completed_at: int
    ) -> ExecutionReceipt:
        attempt_id = _identifier(attempt_id, "attempt_id_invalid")
        completed_at = _nonnegative_integer(completed_at, "completed_at_invalid")
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise StorageError("execution_attempt_missing")
            self._assert_approval_consumed(connection, attempt)
            if attempt["status"] == "completed":
                row = connection.execute(
                    "SELECT * FROM execution_receipts WHERE execution_id = ?",
                    (attempt["execution_id"],),
                ).fetchone()
                evidence = connection.execute(
                    "SELECT 1 FROM execution_evidence WHERE execution_id = ?",
                    (attempt["execution_id"],),
                ).fetchone()
                if row is None or evidence is None:
                    raise StorageError("execution_attempt_conflict")
                receipt = self._receipt_from_row(row)
                self._validate_attempt_binding(attempt, receipt)
                connection.rollback()
                return receipt
            if attempt["status"] != "persistence_failed":
                raise StorageError("execution_attempt_conflict")
            staged = connection.execute(
                "SELECT * FROM staged_execution_receipts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if staged is None:
                raise StorageError("execution_attempt_conflict")
            receipt = self._receipt_from_row(staged)
            self._validate_attempt_binding(attempt, receipt)
            self._finalize_rows(connection, attempt, receipt, completed_at)
            connection.commit()
            return receipt
        except StorageError:
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

    def execution_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        attempt_id = _identifier(attempt_id, "attempt_id_invalid")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def outcome_evidence_rows(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT o.outcome_id, o.execution_id, o.provenance, o.success,
                    o.severe_failure, o.recorded_at, e.model_id, e.effort,
                    t.category, t.risk
                FROM outcomes o
                JOIN executions e ON e.execution_id = o.execution_id
                JOIN tasks t ON t.task_id = e.task_id
                ORDER BY o.recorded_at, o.outcome_id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def close_incident(self, execution_id: str, reviewed_at: int) -> bool:
        execution_id = _identifier(execution_id, "execution_id_invalid")
        reviewed_at = _nonnegative_integer(reviewed_at, "reviewed_at_invalid")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO incident_reviews(execution_id, reviewed_at) VALUES (?, ?)",
                    (execution_id, reviewed_at),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def latest_incident_review(self) -> int | None:
        with self._connect() as connection:
            row = connection.execute("SELECT MAX(reviewed_at) FROM incident_reviews").fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def reserve_shadow_budget(
        self,
        entry_id: str,
        token_amount: int,
        quota: int,
        now: int,
    ) -> bool:
        entry_id = _identifier(entry_id, "entry_id_invalid")
        token_amount = _nonnegative_integer(token_amount, "token_amount_invalid")
        quota = _nonnegative_integer(quota, "quota_invalid")
        now = _nonnegative_integer(now, "created_at_invalid")
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            used = int(connection.execute(
                """SELECT COALESCE(SUM(token_amount), 0) FROM budget_ledger
                WHERE entry_type = 'shadow_reservation' AND created_at >= ?""",
                (max(0, now - 86_400),),
            ).fetchone()[0])
            if token_amount > quota or used + token_amount > quota:
                connection.rollback()
                return False
            connection.execute(
                """INSERT INTO budget_ledger(
                    entry_id, approval_id, execution_id, entry_type, token_amount, created_at
                ) VALUES (?, NULL, NULL, 'shadow_reservation', ?, ?)""",
                (entry_id, token_amount, now),
            )
            connection.commit()
            return True
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise _translate_database_error(exc) from exc
        finally:
            if connection is not None:
                connection.close()

    def create_blind_comparison(
        self,
        *,
        comparison_id: str,
        primary_execution_id: str,
        shadow_execution_id: str,
        label_a_hash: str,
        label_b_hash: str,
        label_a_is_shadow: bool,
        created_at: int,
    ) -> None:
        for value, code in (
            (comparison_id, "comparison_id_invalid"),
            (primary_execution_id, "execution_id_invalid"),
            (shadow_execution_id, "execution_id_invalid"),
        ):
            _identifier(value, code)
        if not _HEX_64.fullmatch(label_a_hash) or not _HEX_64.fullmatch(label_b_hash):
            raise ValueError("response_hash_invalid")
        if not isinstance(label_a_is_shadow, bool):
            raise ValueError("comparison_invalid")
        created_at = _nonnegative_integer(created_at, "created_at_invalid")
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO blind_comparisons(
                        comparison_id, primary_execution_id, shadow_execution_id,
                        label_a_hash, label_b_hash, label_a_is_shadow, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (comparison_id, primary_execution_id, shadow_execution_id,
                     label_a_hash, label_b_hash, int(label_a_is_shadow), created_at),
                )
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def record_blind_verdict(self, comparison_id: str, verdict: str, recorded_at: int) -> bool:
        comparison_id = _identifier(comparison_id, "comparison_id_invalid")
        if verdict not in {"a", "b", "tie", "reject_both"}:
            raise ValueError("pairwise_verdict_invalid")
        recorded_at = _nonnegative_integer(recorded_at, "recorded_at_invalid")
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE blind_comparisons SET verdict = ?, recorded_at = ?
                WHERE comparison_id = ? AND verdict IS NULL""",
                (verdict, recorded_at, comparison_id),
            )
        return cursor.rowcount == 1

    def blind_comparison(self, comparison_id: str) -> dict[str, Any] | None:
        comparison_id = _identifier(comparison_id, "comparison_id_invalid")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM blind_comparisons WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def purge_outcomes_before(self, cutoff: int) -> int:
        cutoff = _nonnegative_integer(cutoff, "cutoff_invalid")
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "DELETE FROM outcomes WHERE recorded_at < ?",
                (cutoff,),
            ).rowcount
            connection.execute("DELETE FROM confidence_stats")
            connection.execute(
                """INSERT INTO confidence_stats(
                    model_id, effort, category, risk, sample_count,
                    success_count, severe_failure_count
                )
                SELECT e.model_id, e.effort, t.category, t.risk,
                    COUNT(*), SUM(o.success), SUM(o.severe_failure)
                FROM outcomes o
                JOIN executions e ON e.execution_id = o.execution_id
                JOIN tasks t ON t.task_id = e.task_id
                WHERE o.provenance IN ('machine_verified', 'ci_imported')
                GROUP BY e.model_id, e.effort, t.category, t.risk"""
            )
            connection.commit()
            return int(deleted)
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise _translate_database_error(exc) from exc
        finally:
            if connection is not None:
                connection.close()

    def confidence_rows(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT model_id, effort, category, risk, sample_count,
                    success_count, severe_failure_count
                FROM confidence_stats
                ORDER BY model_id, effort, category, risk"""
            ).fetchall()
        return [dict(row) for row in rows]

    def row_count(self, table: str) -> int:
        if table not in _TABLES:
            raise ValueError("table_invalid")
        with self._connect() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def save_registry_snapshot(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "refreshed_at",
            "expires_at",
            "models",
        }:
            raise ValueError("registry_snapshot_invalid")
        refreshed = _nonnegative_integer(payload["refreshed_at"], "refreshed_at_invalid")
        expires = _nonnegative_integer(payload["expires_at"], "expires_at_invalid")
        if expires <= refreshed:
            raise ValueError("expires_at_invalid")
        models = payload["models"]
        if not isinstance(models, list) or len(models) > 128:
            raise ValueError("registry_models_invalid")
        for model in models:
            if not isinstance(model, dict) or set(model) != {
                "model_id",
                "digest",
                "context_window_tokens",
                "capabilities",
            }:
                raise ValueError("registry_model_invalid")
            model_id = model["model_id"]
            digest = model["digest"]
            context = model["context_window_tokens"]
            capabilities = model["capabilities"]
            if not isinstance(model_id, str) or not _SNAPSHOT_MODEL_ID.fullmatch(model_id):
                raise ValueError("registry_model_invalid")
            if not isinstance(digest, str) or not _HEX_64.fullmatch(digest):
                raise ValueError("registry_model_invalid")
            _nonnegative_integer(context, "registry_model_invalid", 4_194_304)
            if (
                not isinstance(capabilities, list)
                or len(capabilities) > 16
                or any(
                    not isinstance(item, str) or not _CAPABILITY.fullmatch(item)
                    for item in capabilities
                )
            ):
                raise ValueError("registry_model_invalid")
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("registry_snapshot_too_large")
        payload_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO registry_snapshots(
                        snapshot_id, payload_json, payload_hash, refreshed_at, expires_at
                    ) VALUES (1, ?, ?, ?, ?)
                    ON CONFLICT(snapshot_id) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        payload_hash = excluded.payload_hash,
                        refreshed_at = excluded.refreshed_at,
                        expires_at = excluded.expires_at""",
                    (serialized, payload_hash, refreshed, expires),
                )
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def load_registry_snapshot(self) -> dict[str, Any] | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload_json, payload_hash FROM registry_snapshots WHERE snapshot_id = 1"
                ).fetchone()
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
        if row is None:
            return None
        payload_text = str(row["payload_json"])
        if hashlib.sha256(payload_text.encode("utf-8")).hexdigest() != row["payload_hash"]:
            raise StorageError("storage_corrupt")
        try:
            payload = json.loads(payload_text)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise StorageError("storage_corrupt") from exc
        if not isinstance(payload, dict):
            raise StorageError("storage_corrupt")
        return payload

    def save_approval_record(
        self,
        *,
        approval_id: str,
        task_id: str | None,
        decision_id: str | None,
        nonce_hash: str,
        manifest_hash: str,
        expires_at: int,
        reserved_tokens: int,
    ) -> None:
        approval_id = _identifier(approval_id, "approval_id_invalid")
        if task_id is not None:
            task_id = _identifier(task_id, "task_id_invalid")
        if decision_id is not None:
            decision_id = _identifier(decision_id, "decision_id_invalid")
        if not _HEX_64.fullmatch(nonce_hash) or not _HEX_64.fullmatch(manifest_hash):
            raise ValueError("approval_hash_invalid")
        expires_at = _nonnegative_integer(expires_at, "expires_at_invalid")
        reserved_tokens = _nonnegative_integer(reserved_tokens, "reserved_tokens_invalid")
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT nonce_hash, manifest_hash, expires_at, reserved_tokens FROM approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != (nonce_hash, manifest_hash, expires_at, reserved_tokens):
                        raise StorageError("approval_manifest_changed")
                    return
                connection.execute(
                    """INSERT INTO approvals(
                        approval_id, task_id, decision_id, nonce_hash, manifest_hash,
                        status, expires_at, reserved_tokens
                    ) VALUES (?, ?, ?, ?, ?, 'issued', ?, ?)""",
                    (
                        approval_id,
                        task_id,
                        decision_id,
                        nonce_hash,
                        manifest_hash,
                        expires_at,
                        reserved_tokens,
                    ),
                )
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def consume_approval_record(
        self,
        *,
        approval_id: str,
        nonce_hash: str,
        manifest_hash: str,
        now: int,
        token_amount: int,
        repository_quota: int,
    ) -> None:
        approval_id = _identifier(approval_id, "approval_id_invalid")
        now = _nonnegative_integer(now, "now_invalid")
        token_amount = _nonnegative_integer(token_amount, "token_amount_invalid")
        repository_quota = _nonnegative_integer(repository_quota, "repository_quota_invalid")
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT nonce_hash, manifest_hash, status, expires_at, reserved_tokens
                FROM approvals WHERE approval_id = ?""",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise StorageError("approval_missing")
            if row["nonce_hash"] != nonce_hash or row["manifest_hash"] != manifest_hash:
                raise StorageError("approval_manifest_changed")
            if row["status"] != "issued":
                raise StorageError("approval_reused")
            if now >= int(row["expires_at"]):
                raise StorageError("approval_expired")
            if int(row["reserved_tokens"]) != token_amount:
                raise StorageError("approval_manifest_changed")
            current = int(
                connection.execute(
                    "SELECT COALESCE(SUM(token_amount), 0) FROM budget_ledger WHERE entry_type = 'reservation'"
                ).fetchone()[0]
            )
            if token_amount > repository_quota or current + token_amount > repository_quota:
                raise StorageError("budget_exhausted")
            connection.execute(
                "UPDATE approvals SET status = 'consumed' WHERE approval_id = ? AND status = 'issued'",
                (approval_id,),
            )
            connection.execute(
                """INSERT INTO budget_ledger(
                    entry_id, approval_id, execution_id, entry_type, token_amount, created_at
                ) VALUES (?, ?, NULL, 'reservation', ?, ?)""",
                (f"reserve:{approval_id}", approval_id, token_amount, now),
            )
            connection.commit()
        except StorageError:
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

    def approval_status(self, approval_id: str) -> str | None:
        approval_id = _identifier(approval_id, "approval_id_invalid")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return None if row is None else str(row[0])

    def reserved_token_total(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COALESCE(SUM(token_amount), 0) FROM budget_ledger WHERE entry_type = 'reservation'"
                ).fetchone()[0]
            )


@dataclass(frozen=True)
class AggregateRecord:
    model_id: str
    effort: str
    category: str
    risk: str
    outcome: str
    input_bucket: int
    output_bucket: int
    latency_bucket: int
    policy_version: str
    recorded_day: int

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not _MODEL_ID.fullmatch(self.model_id):
            raise ValueError("model_id_invalid")
        object.__setattr__(self, "effort", Effort(self.effort).value)
        object.__setattr__(self, "category", TaskCategory(self.category).value)
        object.__setattr__(self, "risk", RiskTier(self.risk).value)
        object.__setattr__(self, "outcome", ExecutionOutcome(self.outcome).value)
        for name in ("input_bucket", "output_bucket", "latency_bucket"):
            object.__setattr__(self, name, _nonnegative_integer(getattr(self, name), f"{name}_invalid", 20))
        if not isinstance(self.policy_version, str) or not _VERSION.fullmatch(self.policy_version):
            raise ValueError("policy_version_invalid")
        object.__setattr__(self, "recorded_day", _nonnegative_integer(self.recorded_day, "recorded_day_invalid", 1_000_000))

    def to_dict(self) -> dict[str, str | int]:
        return {
            "model_id": self.model_id,
            "effort": self.effort,
            "category": self.category,
            "risk": self.risk,
            "outcome": self.outcome,
            "input_bucket": self.input_bucket,
            "output_bucket": self.output_bucket,
            "latency_bucket": self.latency_bucket,
            "policy_version": self.policy_version,
            "recorded_day": self.recorded_day,
        }


def _default_machine_state_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "Graphite" / "routing"
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "state") / "graphite" / "routing"


class AggregateStore:
    """Opt-in machine-wide store containing sanitized typed aggregates only."""

    def __init__(
        self,
        repository_root: Path,
        *,
        opt_in: bool,
        state_dir: Path | None = None,
    ) -> None:
        if not isinstance(opt_in, bool):
            raise ValueError("aggregate_opt_in_invalid")
        self.root = _selected_root(repository_root)
        self.opt_in = opt_in
        selected_state = (state_dir or _default_machine_state_dir()).resolve(strict=False)
        try:
            selected_state.relative_to(self.root)
        except ValueError:
            pass
        else:
            raise StorageError("aggregate_path_invalid")
        self.path = selected_state / "aggregate.sqlite3"

    def _initialize(self) -> None:
        _secure_directory(self.path.parent)
        try:
            with sqlite3.connect(self.path, timeout=2.0) as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA busy_timeout = 2000")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS aggregate_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        model_id TEXT NOT NULL,
                        effort TEXT NOT NULL,
                        category TEXT NOT NULL,
                        risk TEXT NOT NULL,
                        outcome TEXT NOT NULL,
                        input_bucket INTEGER NOT NULL,
                        output_bucket INTEGER NOT NULL,
                        latency_bucket INTEGER NOT NULL,
                        policy_version TEXT NOT NULL,
                        recorded_day INTEGER NOT NULL
                    )"""
                )
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
        _secure_file(self.path)

    def write(self, record: AggregateRecord) -> bool:
        if not isinstance(record, AggregateRecord):
            raise ValueError("aggregate_record_invalid")
        if not self.opt_in:
            return False
        self._initialize()
        values = record.to_dict()
        try:
            with sqlite3.connect(self.path, timeout=2.0) as connection:
                connection.execute("PRAGMA busy_timeout = 2000")
                connection.execute(
                    """INSERT INTO aggregate_events(
                        model_id, effort, category, risk, outcome,
                        input_bucket, output_bucket, latency_bucket,
                        policy_version, recorded_day
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    tuple(values.values()),
                )
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
        return True

    def row_count(self) -> int:
        if not self.opt_in or not self.path.exists():
            return 0
        try:
            with sqlite3.connect(self.path, timeout=2.0) as connection:
                return int(connection.execute("SELECT COUNT(*) FROM aggregate_events").fetchone()[0])
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
