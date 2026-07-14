"""Repository-local routing evidence and sanitized aggregate storage tests."""
from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from graphite.routing.contracts import Effort, ExecutionOutcome, ExecutionReceipt
from graphite.routing.storage import (
    AggregateRecord,
    AggregateStore,
    RepositoryStore,
    StorageError,
)


def test_repository_database_is_fixed_under_selected_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    store = RepositoryStore(root)
    store.initialize()

    assert store.path == root / ".graphite" / "routing" / "events.sqlite3"
    assert store.path.is_file()
    assert store.integrity_check() == "ok"


def test_machine_aggregate_cannot_be_redirected_into_repository(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    with pytest.raises(StorageError, match="aggregate_path_invalid"):
        AggregateStore(root, opt_in=True, state_dir=root / ".graphite" / "machine")


def test_schema_migration_is_idempotent_and_enables_safety_pragmas(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)

    store.initialize()
    store.initialize()

    with sqlite3.connect(store.path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        attempt_columns = {
            row[1]: row[3]
            for row in connection.execute("PRAGMA table_info(execution_attempts)")
        }
    assert {
        "tasks",
        "decisions",
        "approvals",
        "executions",
        "outcomes",
        "shadow_comparisons",
        "policy_versions",
        "budget_ledger",
        "confidence_stats",
        "execution_attempts",
        "execution_receipts",
    } <= tables
    assert version == "2"
    assert attempt_columns["max_input_tokens"] == 1
    assert attempt_columns["max_output_tokens"] == 1
    assert attempt_columns["expected_prompt_hash"] == 1
    assert store.pragma_state() == {
        "foreign_keys": 1,
        "journal_mode": "wal",
        "busy_timeout": 2_000,
    }


def test_execution_finalization_is_atomic_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    store.record_task("task-1", "isolated_code", "low", "a" * 64, 10)
    store.record_decision(
        "decision-1", "task-1", "kimi-k2.7-code:cloud", "default", "2", "1", 10
    )
    store.save_approval_record(
        approval_id="approval-1", task_id="task-1", decision_id="decision-1",
        nonce_hash="b" * 64, manifest_hash="c" * 64, expires_at=100,
        reserved_tokens=10,
    )
    store.create_execution_attempt(
        attempt_id="attempt-1", approval_id="approval-1", task_id="task-1",
        decision_id="decision-1", manifest_hash="c" * 64,
        graph_fingerprint="d" * 64, model_id="kimi-k2.7-code:cloud",
        effort="default", reserved_tokens=10, max_input_tokens=8,
        max_output_tokens=2, expected_prompt_hash="e" * 64, created_at=11,
    )
    receipt = ExecutionReceipt(
        "exec-1", "approval-1", "kimi-k2.7-code:cloud", Effort.DEFAULT,
        ExecutionOutcome.SUCCEEDED, 6, 2, 20, "e" * 64, "f" * 64, None,
    )
    with pytest.raises(StorageError, match="^approval_not_consumed$"):
        store.finalize_execution_attempt(
            attempt_id="attempt-1", receipt=receipt, completed_at=12
        )
    assert store.row_count("executions") == 0
    assert store.row_count("execution_receipts") == 0
    assert store.row_count("execution_evidence") == 0
    assert store.approval_status("approval-1") == "issued"
    store.consume_approval_record(
        approval_id="approval-1", nonce_hash="b" * 64, manifest_hash="c" * 64,
        now=12, token_amount=10, repository_quota=100,
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_evidence BEFORE INSERT ON execution_evidence "
            "BEGIN SELECT RAISE(ABORT, 'fault injection'); END"
        )

    with pytest.raises(StorageError, match="storage_unavailable"):
        store.finalize_execution_attempt(
            attempt_id="attempt-1", receipt=receipt, completed_at=12
        )
    assert store.row_count("executions") == 0
    assert store.row_count("execution_receipts") == 0
    assert store.row_count("execution_evidence") == 0
    assert store.execution_attempt("attempt-1")["status"] == "pending"

    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER fail_evidence")
    assert store.finalize_execution_attempt(
        attempt_id="attempt-1", receipt=receipt, completed_at=12
    ) is True
    assert store.finalize_execution_attempt(
        attempt_id="attempt-1", receipt=receipt, completed_at=12
    ) is False
    assert store.row_count("executions") == 1
    assert store.row_count("execution_receipts") == 1
    assert store.row_count("execution_evidence") == 1
    assert store.execution_attempt("attempt-1")["status"] == "completed"
    assert store.approval_status("approval-1") == "consumed"


def test_v1_database_migrates_to_v2_without_losing_rows(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".graphite" / "routing" / "events.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES ('schema_version', '1');
            CREATE TABLE tasks (task_id TEXT PRIMARY KEY, category TEXT NOT NULL,
                risk TEXT NOT NULL, objective_hash TEXT NOT NULL, created_at INTEGER NOT NULL);
            CREATE TABLE decisions (decision_id TEXT PRIMARY KEY,
                task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE, model_id TEXT,
                effort TEXT, policy_version TEXT NOT NULL, evidence_version TEXT NOT NULL,
                created_at INTEGER NOT NULL);
            CREATE TABLE approvals (approval_id TEXT PRIMARY KEY,
                task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE,
                decision_id TEXT REFERENCES decisions(decision_id) ON DELETE CASCADE,
                nonce_hash TEXT NOT NULL UNIQUE, manifest_hash TEXT NOT NULL,
                status TEXT NOT NULL, expires_at INTEGER NOT NULL, reserved_tokens INTEGER NOT NULL);
            CREATE TABLE executions (execution_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
                decision_id TEXT REFERENCES decisions(decision_id) ON DELETE SET NULL,
                approval_id TEXT REFERENCES approvals(approval_id) ON DELETE SET NULL,
                model_id TEXT NOT NULL, effort TEXT NOT NULL, status TEXT NOT NULL,
                reserved_tokens INTEGER NOT NULL, actual_input_tokens INTEGER,
                actual_output_tokens INTEGER, created_at INTEGER NOT NULL, completed_at INTEGER);
            CREATE TABLE outcomes (outcome_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
                provenance TEXT NOT NULL, success INTEGER NOT NULL CHECK(success IN (0,1)),
                severe_failure INTEGER NOT NULL CHECK(severe_failure IN (0,1)),
                recorded_at INTEGER NOT NULL);
            CREATE TABLE shadow_comparisons (comparison_id TEXT PRIMARY KEY,
                primary_execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
                shadow_execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
                verdict TEXT, created_at INTEGER NOT NULL);
            CREATE TABLE policy_versions (policy_version TEXT PRIMARY KEY,
                policy_hash TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL);
            CREATE TABLE budget_ledger (entry_id TEXT PRIMARY KEY,
                approval_id TEXT REFERENCES approvals(approval_id) ON DELETE SET NULL,
                execution_id TEXT REFERENCES executions(execution_id) ON DELETE SET NULL,
                entry_type TEXT NOT NULL, token_amount INTEGER NOT NULL, created_at INTEGER NOT NULL);
            CREATE TABLE confidence_stats (model_id TEXT NOT NULL, effort TEXT NOT NULL,
                category TEXT NOT NULL, risk TEXT NOT NULL, sample_count INTEGER NOT NULL,
                success_count INTEGER NOT NULL, severe_failure_count INTEGER NOT NULL,
                PRIMARY KEY(model_id, effort, category, risk));
            CREATE INDEX outcomes_recorded_at_idx ON outcomes(recorded_at);
            CREATE INDEX executions_task_idx ON executions(task_id);
            CREATE TABLE execution_evidence (
                execution_id TEXT PRIMARY KEY REFERENCES executions(execution_id) ON DELETE CASCADE,
                task_id TEXT NOT NULL, decision_id TEXT NOT NULL, graph_fingerprint TEXT NOT NULL);
            CREATE TABLE execution_attempts (attempt_id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL UNIQUE REFERENCES approvals(approval_id) ON DELETE RESTRICT,
                task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
                decision_id TEXT NOT NULL REFERENCES decisions(decision_id) ON DELETE RESTRICT,
                manifest_hash TEXT NOT NULL, graph_fingerprint TEXT NOT NULL, model_id TEXT NOT NULL,
                effort TEXT NOT NULL, reserved_tokens INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','completed','failed','persistence_failed')),
                failure_reason TEXT, execution_id TEXT UNIQUE REFERENCES executions(execution_id) ON DELETE RESTRICT,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE execution_receipts (
                execution_id TEXT PRIMARY KEY REFERENCES executions(execution_id) ON DELETE CASCADE,
                approval_id TEXT NOT NULL, model_id TEXT NOT NULL, effort TEXT NOT NULL,
                outcome TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL, prompt_hash TEXT NOT NULL, response_hash TEXT NOT NULL,
                failure_reason TEXT, completed_at INTEGER NOT NULL);
            CREATE TABLE incident_reviews (review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
                reviewed_at INTEGER NOT NULL, UNIQUE(execution_id, reviewed_at));
            CREATE TABLE blind_comparisons (comparison_id TEXT PRIMARY KEY,
                primary_execution_id TEXT NOT NULL, shadow_execution_id TEXT NOT NULL,
                label_a_hash TEXT NOT NULL, label_b_hash TEXT NOT NULL,
                label_a_is_shadow INTEGER NOT NULL CHECK(label_a_is_shadow IN (0,1)),
                verdict TEXT, recorded_at INTEGER, created_at INTEGER NOT NULL);
            CREATE TABLE registry_snapshots (snapshot_id INTEGER PRIMARY KEY CHECK(snapshot_id=1),
                payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
                refreshed_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
            """
        )
        for suffix in ("pending", "recover", "completed"):
            connection.execute(
                "INSERT INTO tasks VALUES (?, 'isolated_code', 'low', ?, 7)",
                (f"task-{suffix}", "a" * 64),
            )
            connection.execute(
                "INSERT INTO decisions VALUES (?, ?, 'kimi-k2.7-code:cloud', "
                "'default', '2', '1', 8)",
                (f"decision-{suffix}", f"task-{suffix}"),
            )
            connection.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, 999, 10)",
                (
                    f"approval-{suffix}", f"task-{suffix}", f"decision-{suffix}",
                    (suffix[0] * 64), (suffix[-1] * 64),
                    "issued" if suffix == "pending" else "consumed",
                ),
            )
        connection.execute(
            "INSERT INTO executions VALUES ('exec-completed','attempt-completed','task-completed',"
            "'decision-completed','approval-completed','kimi-k2.7-code:cloud','default',"
            "'succeeded',10,6,2,9,10)"
        )
        connection.executemany(
            "INSERT INTO execution_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("attempt-pending", "approval-pending", "task-pending", "decision-pending",
                 "g" * 64, "h" * 64, "kimi-k2.7-code:cloud", "default", 10,
                 "pending", None, None, 9, 9),
                ("attempt-recover", "approval-recover", "task-recover", "decision-recover",
                 "r" * 64, "s" * 64, "kimi-k2.7-code:cloud", "default", 10,
                 "persistence_failed", "execution_persistence_failed", None, 9, 10),
                ("attempt-completed", "approval-completed", "task-completed", "decision-completed",
                 "d" * 64, "e" * 64, "kimi-k2.7-code:cloud", "default", 10,
                 "completed", None, "exec-completed", 9, 10),
            ],
        )
        connection.execute(
            "INSERT INTO execution_receipts VALUES ('exec-completed','approval-completed',"
            "'kimi-k2.7-code:cloud','default','succeeded',6,2,15,?,?,NULL,10)",
            ("f" * 64, "1" * 64),
        )
        connection.execute(
            "INSERT INTO execution_evidence VALUES ('exec-completed','task-completed',"
            "'decision-completed',?)", ("e" * 64,),
        )
        connection.execute(
            "INSERT INTO outcomes VALUES ('outcome-1','exec-completed','human',1,0,11)"
        )
        connection.execute(
            "INSERT INTO budget_ledger VALUES ('reserve:approval-completed',"
            "'approval-completed','exec-completed','reservation',10,9)"
        )
        connection.execute(
            "INSERT INTO incident_reviews(execution_id,reviewed_at) VALUES ('exec-completed',12)"
        )
        connection.execute(
            "INSERT INTO policy_versions VALUES ('2',?,'active',6)", ("2" * 64,)
        )
        connection.execute(
            "INSERT INTO confidence_stats VALUES ('kimi-k2.7-code:cloud','default',"
            "'isolated_code','low',1,1,0)"
        )
        before = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in (
                "tasks", "decisions", "approvals", "executions", "execution_receipts",
                "execution_evidence", "outcomes", "budget_ledger", "incident_reviews",
                "policy_versions", "confidence_stats",
            )
        }
        before_attempts = connection.execute(
            "SELECT attempt_id,approval_id,task_id,decision_id,manifest_hash,"
            "graph_fingerprint,model_id,effort,reserved_tokens,status,failure_reason,"
            "execution_id,created_at,updated_at FROM execution_attempts ORDER BY attempt_id"
        ).fetchall()

    store = RepositoryStore(root)
    store.initialize()
    store.initialize()

    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        after = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in before
        }
        attempts = connection.execute(
            "SELECT attempt_id,status,failure_reason,execution_id,max_input_tokens,"
            "max_output_tokens,expected_prompt_hash FROM execution_attempts ORDER BY attempt_id"
        ).fetchall()
        preserved_attempt_fields = connection.execute(
            "SELECT attempt_id,approval_id,task_id,decision_id,manifest_hash,"
            "graph_fingerprint,model_id,effort,reserved_tokens,status,failure_reason,"
            "execution_id,created_at,updated_at FROM execution_attempts ORDER BY attempt_id"
        ).fetchall()
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(execution_attempts)")
        }
        staged_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='staged_execution_receipts'"
        ).fetchone()
        indexes = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    assert version == "2"
    assert after == before
    expected_attempt_fields = []
    for legacy in before_attempts:
        expected = list(legacy)
        if expected[9] in {"pending", "persistence_failed"}:
            expected[9] = "legacy_unrecoverable"
            expected[10] = "legacy_attempt_bindings_missing"
        expected_attempt_fields.append(tuple(expected))
    assert preserved_attempt_fields == expected_attempt_fields
    assert attempts == [
        ("attempt-completed", "completed", None, "exec-completed", None, None, None),
        ("attempt-pending", "legacy_unrecoverable", "legacy_attempt_bindings_missing", None, None, None, None),
        ("attempt-recover", "legacy_unrecoverable", "legacy_attempt_bindings_missing", None, None, None, None),
    ]
    assert {"max_input_tokens", "max_output_tokens", "expected_prompt_hash"} <= columns
    assert staged_exists == (1,)
    assert {"outcomes_recorded_at_idx", "executions_task_idx"} <= indexes
    assert foreign_key_errors == []
    assert integrity == "ok"
    assert store.recoverable_attempt_ids() == ()
    for attempt_id in ("attempt-pending", "attempt-recover"):
        with pytest.raises(StorageError, match="^legacy_attempt_bindings_missing$"):
            store.reconcile_execution_attempt(attempt_id, completed_at=20)


def test_attempt_creation_is_transactionally_idempotent_under_concurrency(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    store.record_task("task-1", "isolated_code", "low", "a" * 64, 10)
    store.record_decision(
        "decision-1", "task-1", "kimi-k2.7-code:cloud", "default", "2", "1", 10
    )
    store.save_approval_record(
        approval_id="approval-1", task_id="task-1", decision_id="decision-1",
        nonce_hash="b" * 64, manifest_hash="c" * 64, expires_at=100,
        reserved_tokens=10,
    )
    values = {
        "attempt_id": "attempt-1", "approval_id": "approval-1", "task_id": "task-1",
        "decision_id": "decision-1", "manifest_hash": "c" * 64,
        "graph_fingerprint": "d" * 64, "model_id": "kimi-k2.7-code:cloud",
        "effort": "default", "reserved_tokens": 10, "max_input_tokens": 8,
        "max_output_tokens": 2, "expected_prompt_hash": "e" * 64, "created_at": 11,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: store.create_execution_attempt(**values), range(2)))
    assert sorted(results) == [False, True]
    assert store.row_count("execution_attempts") == 1


def test_duplicate_idempotency_key_cannot_create_duplicate_execution(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()

    first = store.insert_execution(
        execution_id="execution-1",
        idempotency_key="request-1",
        task_id=None,
        decision_id=None,
        approval_id=None,
        model_id="kimi-k2.7-code:cloud",
        effort="default",
        status="reserved",
        reserved_tokens=1_000,
        created_at=1_700_000_000,
    )
    second = store.insert_execution(
        execution_id="execution-2",
        idempotency_key="request-1",
        task_id=None,
        decision_id=None,
        approval_id=None,
        model_id="kimi-k2.7-code:cloud",
        effort="default",
        status="reserved",
        reserved_tokens=1_000,
        created_at=1_700_000_001,
    )

    assert first is True
    assert second is False
    assert store.row_count("executions") == 1


def test_corrupt_database_fails_closed_without_deleting_it(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".graphite" / "routing" / "events.sqlite3"
    path.parent.mkdir(parents=True)
    original = b"not a sqlite database - private"
    path.write_bytes(original)

    with pytest.raises(StorageError, match="storage_corrupt"):
        RepositoryStore(root).initialize()

    assert path.read_bytes() == original


def test_repository_store_rejects_symlinked_state_directory(tmp_path: Path) -> None:
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / ".graphite").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")

    with pytest.raises(StorageError, match="storage_path_invalid"):
        RepositoryStore(root).initialize()

    assert not (outside / "routing" / "events.sqlite3").exists()


def test_repository_store_refuses_unknown_future_schema(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'"
        )

    with pytest.raises(StorageError, match="storage_schema_unsupported"):
        store.initialize()

    with sqlite3.connect(store.path) as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert version == "999"


def test_aggregate_opt_out_creates_no_machine_file_or_directory(tmp_path: Path) -> None:
    root = tmp_path / "project"
    state = tmp_path / "machine-state"
    root.mkdir()
    store = AggregateStore(root, opt_in=False, state_dir=state)

    written = store.write(
        AggregateRecord(
            model_id="kimi-k2.7-code:cloud",
            effort="default",
            category="feature",
            risk="low",
            outcome="succeeded",
            input_bucket=4,
            output_bucket=2,
            latency_bucket=3,
            policy_version="1",
            recorded_day=20_000,
        )
    )

    assert written is False
    assert not state.exists()


def test_aggregate_accepts_only_typed_sanitized_fields(tmp_path: Path) -> None:
    root = tmp_path / "project"
    state = tmp_path / "machine-state"
    root.mkdir()
    store = AggregateStore(root, opt_in=True, state_dir=state)
    record = AggregateRecord(
        model_id="kimi-k2.7-code:cloud",
        effort="default",
        category="feature",
        risk="low",
        outcome="succeeded",
        input_bucket=4,
        output_bucket=2,
        latency_bucket=3,
        policy_version="1",
        recorded_day=20_000,
    )

    assert store.write(record) is True
    assert store.row_count() == 1

    for field, value in (
        ("model_id", "C:/private/repository/model"),
        ("category", "my-secret-project"),
        ("policy_version", "user@example.test"),
    ):
        values = record.to_dict()
        values[field] = value
        with pytest.raises(ValueError):
            AggregateRecord(**values)


def test_retention_deletes_expired_outcomes_and_rebuilds_confidence(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    store.record_task("task-1", "feature", "low", "a" * 64, 1_700_000_000)
    store.insert_execution(
        execution_id="execution-old",
        idempotency_key="request-old",
        task_id="task-1",
        decision_id=None,
        approval_id=None,
        model_id="kimi-k2.7-code:cloud",
        effort="default",
        status="completed",
        reserved_tokens=1_000,
        created_at=1_700_000_001,
    )
    store.insert_execution(
        execution_id="execution-new",
        idempotency_key="request-new",
        task_id="task-1",
        decision_id=None,
        approval_id=None,
        model_id="kimi-k2.7-code:cloud",
        effort="default",
        status="completed",
        reserved_tokens=1_000,
        created_at=1_700_000_101,
    )
    store.record_outcome("outcome-old", "execution-old", "machine_verified", True, False, 100)
    store.record_outcome("outcome-new", "execution-new", "machine_verified", True, False, 300)

    assert store.purge_outcomes_before(200) == 1
    stats = store.confidence_rows()

    assert store.row_count("outcomes") == 1
    assert stats == [
        {
            "model_id": "kimi-k2.7-code:cloud",
            "effort": "default",
            "category": "feature",
            "risk": "low",
            "sample_count": 1,
            "success_count": 1,
            "severe_failure_count": 0,
        }
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not authoritative on Windows")
def test_storage_permissions_are_current_user_only_where_supported(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()

    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600
