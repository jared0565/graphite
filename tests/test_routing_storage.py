"""Repository-local routing evidence and sanitized aggregate storage tests."""
from __future__ import annotations

import os
import sqlite3
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
    assert version == "1"
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
        effort="default", reserved_tokens=10, created_at=11,
    )
    receipt = ExecutionReceipt(
        "exec-1", "approval-1", "kimi-k2.7-code:cloud", Effort.DEFAULT,
        ExecutionOutcome.SUCCEEDED, 6, 2, 20, "e" * 64, "f" * 64, None,
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
