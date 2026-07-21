"""Repository-local routing evidence and sanitized aggregate storage tests."""
from __future__ import annotations

import os
import gc
import hashlib
import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from graphite.routing.contracts import Effort, ExecutionOutcome, ExecutionReceipt
from graphite.routing.policy import CliLearnedPolicy, sign_cli_policy
from graphite.routing.storage import (
    AggregateRecord,
    AggregateStore,
    RepositoryStore,
    StorageError,
)


def test_signed_cli_policy_requires_human_promotion_and_preserves_rollback_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    key = b"policy-signing-key-material-32!!"

    first = CliLearnedPolicy("learned-1", None, (("reliability", 900),))
    first_signature = sign_cli_policy(first, "a" * 64, key)
    assert store.record_cli_policy_candidate(
        policy_version=first.policy_version,
        parent_version=first.parent_version,
        payload=first.to_payload(),
        evidence_hash="a" * 64,
        signature=first_signature,
        signing_key=key,
        created_at=10,
    ) is True
    assert store.active_cli_policy_version() is None
    with pytest.raises(ValueError, match="human_authority_required"):
        store.activate_cli_policy(
            "learned-1", action="promote", authority_granted=False, created_at=11
        )
    assert store.activate_cli_policy(
        "learned-1", action="promote", authority_granted=True, created_at=11
    ) is True

    second = CliLearnedPolicy("learned-2", "learned-1", (("reliability", 850),))
    store.record_cli_policy_candidate(
        policy_version=second.policy_version,
        parent_version=second.parent_version,
        payload=second.to_payload(),
        evidence_hash="b" * 64,
        signature=sign_cli_policy(second, "b" * 64, key),
        signing_key=key,
        created_at=12,
    )
    store.activate_cli_policy(
        "learned-2", action="promote", authority_granted=True, created_at=13
    )
    assert store.activate_cli_policy(
        "learned-1", action="rollback", authority_granted=True, created_at=14
    ) is True
    assert store.active_cli_policy_version() == "learned-1"
    assert store.row_count("cli_policy_versions") == 2
    assert [event["action"] for event in store.cli_policy_event_records()] == [
        "candidate", "promote", "candidate", "promote", "rollback"
    ]

    with pytest.raises(ValueError, match="policy_signature_invalid"):
        store.record_cli_policy_candidate(
            policy_version="tampered",
            parent_version="learned-1",
            payload=second.to_payload(),
            evidence_hash="b" * 64,
            signature="0" * 64,
            signing_key=key,
            created_at=15,
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
        staged_columns = {
            row[1]: row[3]
            for row in connection.execute("PRAGMA table_info(staged_execution_receipts)")
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
    assert version == "7"
    assert attempt_columns["max_input_tokens"] == 1
    assert attempt_columns["max_output_tokens"] == 1
    assert attempt_columns["expected_prompt_hash"] == 1
    assert attempt_columns["inventory_digest"] == 1
    assert staged_columns["inventory_digest"] == 1
    assert store.pragma_state() == {
        "foreign_keys": 1,
        "journal_mode": "wal",
        "busy_timeout": 2_000,
    }


def _downgrade_fixture_to_v3(store: RepositoryStore) -> None:
    """Turn a fresh v5 test database into a representative committed v3 database."""
    with sqlite3.connect(store.path) as connection:
        for table in (
            "lifecycle_attempt_bindings",
            "lifecycle_approval_bindings",
            "lifecycle_snapshot_bindings",
            "cli_policy_events",
            "cli_policy_versions",
            "cli_telemetry_events",
            "review_links",
            "validation_results",
            "cli_execution_attempts",
            "task_worktrees",
            "capability_snapshots",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute(
            "UPDATE schema_meta SET value='3' WHERE key='schema_version'"
        )


def _downgrade_fixture_to_v4(store: RepositoryStore) -> None:
    """Turn a fresh v5 test database into a representative committed v4 database."""
    with sqlite3.connect(store.path) as connection:
        for table in (
            "lifecycle_attempt_bindings",
            "lifecycle_approval_bindings",
            "lifecycle_snapshot_bindings",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute("UPDATE schema_meta SET value='4' WHERE key='schema_version'")


def test_schema_v3_compatibility_fixture_reconstructs_digest_contract(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    fixture_root = Path(__file__).parent / "fixtures"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            (fixture_root / "routing_schema_v2_ca77600.sql").read_text(encoding="utf-8")
        )
        connection.executescript(
            (fixture_root / "routing_schema_v3_94eb333.sql").read_text(encoding="utf-8")
        )
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        attempt_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(execution_attempts)")
        }
        staged_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(staged_execution_receipts)")
        }
    assert version == ("3",)
    assert "inventory_digest" in attempt_columns
    assert "inventory_digest" in staged_columns


def test_v3_to_current_creates_verified_backup_and_quarantines_live_ollama_attempts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    _downgrade_fixture_to_v3(store)
    store.record_task("task-legacy", "isolated_code", "low", "1" * 64, 1)
    store.record_decision(
        "decision-legacy", "task-legacy", "kimi-k2.7-code:cloud", "default", "1", "1", 1
    )
    store.save_approval_record(
        approval_id="approval-legacy",
        task_id="task-legacy",
        decision_id="decision-legacy",
        nonce_hash="2" * 64,
        manifest_hash="3" * 64,
        expires_at=99,
        reserved_tokens=10,
    )
    store.create_execution_attempt(
        attempt_id="attempt-legacy",
        approval_id="approval-legacy",
        task_id="task-legacy",
        decision_id="decision-legacy",
        manifest_hash="3" * 64,
        graph_fingerprint="4" * 64,
        model_id="kimi-k2.7-code:cloud",
        effort="default",
        reserved_tokens=10,
        max_input_tokens=8,
        max_output_tokens=2,
        expected_prompt_hash="5" * 64,
        inventory_digest="6" * 64,
        created_at=2,
    )

    store.initialize()

    backup = store.path.parent / "backups" / "events-schema-v3.sqlite3"
    marker = store.path.parent / "backups" / "events-schema-v3.sha256.json"
    assert backup.is_file()
    assert marker.is_file()
    evidence = json.loads(marker.read_text(encoding="utf-8"))
    assert evidence == {
        "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        "schema_version": "3",
    }
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("3",)
        assert connection.execute(
            "SELECT status FROM execution_attempts WHERE attempt_id='attempt-legacy'"
        ).fetchone() == ("pending",)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("7",)
        assert connection.execute(
            "SELECT status,failure_reason FROM execution_attempts "
            "WHERE attempt_id='attempt-legacy'"
        ).fetchone() == ("legacy_unrecoverable", "legacy_provider_retired")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "capability_snapshots",
        "task_worktrees",
        "cli_execution_attempts",
        "validation_results",
        "review_links",
    } <= tables
    assert store.recoverable_attempts().attempt_ids == ()
    with pytest.raises(StorageError, match="^legacy_provider_retired$"):
        store.reconcile_execution_attempt("attempt-legacy", completed_at=3)


def test_v3_v4_rollback_drill_restores_verified_backup_for_old_reader(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    _downgrade_fixture_to_v3(store)
    store.record_task("task-history", "documentation", "low", "a" * 64, 1)

    store.initialize()
    backup = store.schema_v3_backup_path
    marker = json.loads(store.schema_v3_backup_marker_path.read_text(encoding="utf-8"))
    assert marker["backup_sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    # All connections above are closed: this is the fixture's stop-writers boundary.
    preserved_v5 = store.path.with_name("events-schema-v5-preserved.sqlite3")
    shutil.copy2(store.path, preserved_v5)
    restore_stage = store.path.with_name("events-restore-stage.sqlite3")
    shutil.copy2(backup, restore_stage)
    live_path = store.path
    del store
    gc.collect()
    live_path.unlink()
    os.replace(restore_stage, live_path)

    # Simulate the old read-only schema consumer; do not initialize with v4 code.
    with sqlite3.connect(live_path) as old_reader:
        assert old_reader.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert old_reader.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("3",)
        assert old_reader.execute(
            "SELECT category,risk FROM tasks WHERE task_id='task-history'"
        ).fetchone() == ("documentation", "low")
        assert old_reader.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='cli_execution_attempts'"
        ).fetchone() is None
    with sqlite3.connect(preserved_v5) as v5_reader:
        assert v5_reader.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("7",)


def test_v3_migration_fails_closed_while_another_writer_holds_lock(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root, busy_timeout_ms=100)
    store.initialize()
    _downgrade_fixture_to_v3(store)

    blocker = sqlite3.connect(store.path, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(StorageError, match="^storage_locked$"):
            store.initialize()
    finally:
        blocker.rollback()
        blocker.close()

    assert not (store.path.parent / "backups" / "events-schema-v3.sqlite3").exists()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("3",)


def test_v3_migration_quarantines_malformed_partial_schema(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    _downgrade_fixture_to_v3(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TABLE staged_execution_receipts")
        connection.execute("DROP TABLE execution_attempts")
        connection.execute(
            "CREATE TABLE execution_attempts(attempt_id TEXT PRIMARY KEY, status TEXT)"
        )

    with pytest.raises(StorageError, match="^storage_migration_quarantined$"):
        store.initialize()

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("3",)


def test_v3_migration_requires_external_backup_digest_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    _downgrade_fixture_to_v3(store)

    def fail_marker(path: Path, payload: bytes) -> None:
        raise StorageError("storage_backup_failed")

    monkeypatch.setattr(RepositoryStore, "_atomic_private_write", staticmethod(fail_marker))
    with pytest.raises(StorageError, match="^storage_backup_failed$"):
        store.initialize()

    assert not store.schema_v3_backup_marker_path.exists()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("3",)


def test_v4_to_current_creates_verified_backup_and_leaves_history_unbound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    _downgrade_fixture_to_v4(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO capability_snapshots VALUES "
            "(?,'claude-code','sonnet','claude-sonnet-5','high',?,'2.1.215',"
            "'1.0.0','workspace-write','{}',1,99)",
            ("4" * 64, "5" * 64),
        )

    store.initialize()

    assert store.schema_v4_backup_path.is_file()
    marker = json.loads(store.schema_v4_backup_marker_path.read_text(encoding="utf-8"))
    assert marker == {
        "backup_sha256": hashlib.sha256(store.schema_v4_backup_path.read_bytes()).hexdigest(),
        "schema_version": "4",
    }
    with sqlite3.connect(store.schema_v4_backup_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("4",)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='lifecycle_snapshot_bindings'"
        ).fetchone() is None
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("7",)
    assert store.lifecycle_identity_binding(
        authority_kind="capability_snapshot", authority_id="4" * 64
    ) is None


def test_committed_v4_fixture_migrates_to_current_without_granting_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    path = root / ".graphite" / "routing" / "events.sqlite3"
    path.parent.mkdir(parents=True)
    fixtures = Path(__file__).parent / "fixtures"
    with sqlite3.connect(path) as connection:
        for name in (
            "routing_schema_v2_ca77600.sql",
            "routing_schema_v3_94eb333.sql",
            "routing_schema_v4_lifecycle_migration.sql",
        ):
            connection.executescript((fixtures / name).read_text(encoding="utf-8"))

    store = RepositoryStore(root)
    store.initialize()

    assert store.integrity_check() == "ok"
    assert store.row_count("lifecycle_snapshot_bindings") == 0
    assert store.row_count("lifecycle_approval_bindings") == 0
    assert store.row_count("lifecycle_attempt_bindings") == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("7",)


def test_v4_v5_rollback_drill_restores_verified_v4_backup(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    _downgrade_fixture_to_v4(store)
    store.record_task("task-history", "documentation", "low", "a" * 64, 1)

    store.initialize()
    backup = store.schema_v4_backup_path
    marker = json.loads(store.schema_v4_backup_marker_path.read_text(encoding="utf-8"))
    assert marker["backup_sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()
    restore_stage = store.path.with_name("events-v4-restore-stage.sqlite3")
    shutil.copy2(backup, restore_stage)
    live_path = store.path
    del store
    gc.collect()
    live_path.unlink()
    os.replace(restore_stage, live_path)

    with sqlite3.connect(live_path) as reader:
        assert reader.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert reader.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("4",)
        assert reader.execute(
            "SELECT category,risk FROM tasks WHERE task_id='task-history'"
        ).fetchone() == ("documentation", "low")
        assert reader.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='lifecycle_snapshot_bindings'"
        ).fetchone() is None


def test_v4_migration_refuses_concurrent_writer(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root, busy_timeout_ms=100)
    store.initialize()
    _downgrade_fixture_to_v4(store)
    blocker = sqlite3.connect(store.path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StorageError, match="^storage_locked$"):
            store.initialize()
    finally:
        blocker.rollback()
        blocker.close()

    assert not store.schema_v4_backup_path.exists()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("4",)


def test_partial_v4_lifecycle_cutover_requires_rollback(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    _downgrade_fixture_to_v4(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TABLE lifecycle_snapshot_bindings("
            "capability_snapshot_digest TEXT PRIMARY KEY)"
        )

    with pytest.raises(StorageError, match="^storage_rollback_required$"):
        store.initialize()

    assert not store.schema_v4_backup_path.exists()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("4",)


def test_lifecycle_authority_bindings_are_exact_immutable_and_ordered(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO tasks VALUES ('task-cli','isolated_code','low',?,1)",
            ("1" * 64,),
        )
        connection.execute(
            "INSERT INTO decisions VALUES "
            "('decision-cli','task-cli','sonnet','high','1','1',1)"
        )
        connection.execute(
            "INSERT INTO approvals VALUES "
            "('approval-cli','task-cli','decision-cli',?,?,'issued',99,10)",
            ("2" * 64, "3" * 64),
        )
        connection.execute(
            "INSERT INTO capability_snapshots VALUES "
            "(?,'claude-code','sonnet','claude-sonnet-5','high',?,'2.1.215',"
            "'1.0.0','workspace-write','{}',1,99)",
            ("4" * 64, "5" * 64),
        )
        connection.execute(
            "INSERT INTO task_worktrees VALUES "
            "('worktree-cli','task-cli',?,?, 'prepared',1,1)",
            ("6" * 40, "7" * 64),
        )
        connection.execute(
            """INSERT INTO cli_execution_attempts(
                attempt_id,approval_id,task_id,decision_id,worktree_id,provider,
                requested_model,effective_model,effort,cli_version,executable_sha256,
                capability_snapshot_digest,manifest_hash,expected_prompt_hash,
                reserved_tokens,status,failure_reason,execution_id,created_at,updated_at
            ) VALUES (
                'attempt-cli','approval-cli','task-cli','decision-cli','worktree-cli',
                'claude-code','sonnet','claude-sonnet-5','high','2.1.215',?,?,?, ?,
                10,'pending',NULL,NULL,1,1
            )""",
            ("5" * 64, "4" * 64, "3" * 64, "8" * 64),
        )

    assert store.save_lifecycle_snapshot_binding(
        capability_snapshot_digest="4" * 64,
        lifecycle_identity_digest="9" * 64,
        bound_at=2,
    ) is True
    assert store.save_lifecycle_snapshot_binding(
        capability_snapshot_digest="4" * 64,
        lifecycle_identity_digest="9" * 64,
        bound_at=2,
    ) is False
    assert store.save_lifecycle_approval_binding(
        approval_id="approval-cli",
        capability_snapshot_digest="4" * 64,
        lifecycle_identity_digest="9" * 64,
        bound_at=3,
    ) is True
    assert store.save_lifecycle_attempt_binding(
        attempt_id="attempt-cli", lifecycle_identity_digest="9" * 64, bound_at=4
    ) is True
    assert store.lifecycle_identity_binding(
        authority_kind="capability_snapshot", authority_id="4" * 64
    ) == "9" * 64
    assert store.lifecycle_identity_binding(
        authority_kind="approval", authority_id="approval-cli"
    ) == "9" * 64
    assert store.lifecycle_identity_binding(
        authority_kind="attempt", authority_id="attempt-cli"
    ) == "9" * 64
    with pytest.raises(StorageError, match="^lifecycle_binding_changed$"):
        store.save_lifecycle_snapshot_binding(
            capability_snapshot_digest="4" * 64,
            lifecycle_identity_digest="a" * 64,
            bound_at=2,
        )
    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="lifecycle_approval_binding_immutable"):
            connection.execute(
                "UPDATE lifecycle_approval_bindings SET lifecycle_identity_digest=?",
                ("a" * 64,),
            )


def test_v4_cli_authority_identity_is_constrained_and_immutable(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO tasks VALUES ('task-cli','isolated_code','low',?,1)",
            ("1" * 64,),
        )
        connection.execute(
            "INSERT INTO decisions VALUES "
            "('decision-cli','task-cli','sonnet','high','1','1',1)"
        )
        connection.execute(
            "INSERT INTO approvals VALUES "
            "('approval-cli','task-cli','decision-cli',?,?,'issued',99,10)",
            ("2" * 64, "3" * 64),
        )
        connection.execute(
            "INSERT INTO capability_snapshots VALUES "
            "(?,'claude-code','sonnet','claude-sonnet-5','high',?,'2.1.208',"
            "'1.0.0','workspace-write','{}',1,99)",
            ("4" * 64, "5" * 64),
        )
        connection.execute(
            "INSERT INTO task_worktrees VALUES "
            "('worktree-cli','task-cli',?,?, 'prepared',1,1)",
            ("6" * 40, "7" * 64),
        )
        connection.execute(
            """INSERT INTO cli_execution_attempts(
                attempt_id,approval_id,task_id,decision_id,worktree_id,provider,
                requested_model,effective_model,effort,cli_version,executable_sha256,
                capability_snapshot_digest,manifest_hash,expected_prompt_hash,
                reserved_tokens,status,failure_reason,execution_id,created_at,updated_at
            ) VALUES (
                'attempt-cli','approval-cli','task-cli','decision-cli','worktree-cli',
                'claude-code','sonnet','claude-sonnet-5','high','2.1.208',?,?,?, ?,
                10,'pending',NULL,NULL,1,1
            )""",
            ("5" * 64, "4" * 64, "3" * 64, "8" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE cli_execution_attempts SET provider='codex' "
                "WHERE attempt_id='attempt-cli'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE capability_snapshots SET effective_model='changed' "
                "WHERE capability_snapshot_digest=?",
                ("4" * 64,),
            )
        connection.execute(
            "UPDATE cli_execution_attempts SET status='failed', "
            "failure_reason='provider_unavailable',updated_at=2 "
            "WHERE attempt_id='attempt-cli'"
        )
        assert connection.execute(
            "SELECT status FROM cli_execution_attempts WHERE attempt_id='attempt-cli'"
        ).fetchone() == ("failed",)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE cli_execution_attempts SET status='pending' "
                "WHERE attempt_id='attempt-cli'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM cli_execution_attempts WHERE attempt_id='attempt-cli'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM capability_snapshots WHERE capability_snapshot_digest=?",
                ("4" * 64,),
            )


def test_partial_core_schema_requires_rollback_or_forward_repair(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TABLE review_links")

    with pytest.raises(StorageError, match="^storage_rollback_required$"):
        store.initialize()

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("7",)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_links'"
        ).fetchone() is None


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
        max_output_tokens=2, expected_prompt_hash="e" * 64,
        inventory_digest="1" * 64, created_at=11,
    )
    receipt = ExecutionReceipt(
        "exec-1", "approval-1", "kimi-k2.7-code:cloud", Effort.DEFAULT,
        ExecutionOutcome.SUCCEEDED, 6, 2, 20, "e" * 64, "f" * 64, None,
    )
    with pytest.raises(StorageError, match="^approval_not_consumed$"):
        store.finalize_execution_attempt(
            attempt_id="attempt-1", receipt=receipt,
            inventory_digest="1" * 64, completed_at=12
        )
    assert store.row_count("executions") == 0
    assert store.row_count("execution_receipts") == 0
    assert store.row_count("execution_evidence") == 0
    assert store.approval_status("approval-1") == "issued"
    store.consume_approval_record(
        approval_id="approval-1", nonce_hash="b" * 64, manifest_hash="c" * 64,
        now=12, token_amount=10, repository_quota=100,
    )
    with pytest.raises(StorageError, match="^execution_attempt_conflict$"):
        store.finalize_execution_attempt(
            attempt_id="attempt-1", receipt=receipt,
            inventory_digest="2" * 64, completed_at=12,
        )
    with pytest.raises(StorageError, match="^execution_attempt_conflict$"):
        store.stage_execution_receipt(
            attempt_id="attempt-1", receipt=receipt,
            inventory_digest="2" * 64, staged_at=12,
        )
    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE execution_attempts SET inventory_digest = ? WHERE attempt_id = ?",
                ("A" * 64, "attempt-1"),
            )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_evidence BEFORE INSERT ON execution_evidence "
            "BEGIN SELECT RAISE(ABORT, 'fault injection'); END"
        )

    with pytest.raises(StorageError, match="storage_unavailable"):
        store.finalize_execution_attempt(
            attempt_id="attempt-1", receipt=receipt,
            inventory_digest="1" * 64, completed_at=12
        )
    assert store.row_count("executions") == 0
    assert store.row_count("execution_receipts") == 0
    assert store.row_count("execution_evidence") == 0
    assert store.execution_attempt("attempt-1")["status"] == "pending"

    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER fail_evidence")
    assert store.finalize_execution_attempt(
        attempt_id="attempt-1", receipt=receipt,
        inventory_digest="1" * 64, completed_at=12
    ) is True
    assert store.finalize_execution_attempt(
        attempt_id="attempt-1", receipt=receipt,
        inventory_digest="1" * 64, completed_at=12
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
            "max_output_tokens,expected_prompt_hash,inventory_digest "
            "FROM execution_attempts ORDER BY attempt_id"
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
    assert version == "7"
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
        ("attempt-completed", "completed", None, "exec-completed", None, None, None, None),
        ("attempt-pending", "legacy_unrecoverable", "legacy_attempt_bindings_missing", None, None, None, None, None),
        ("attempt-recover", "legacy_unrecoverable", "legacy_attempt_bindings_missing", None, None, None, None, None),
    ]
    assert {
        "max_input_tokens", "max_output_tokens", "expected_prompt_hash", "inventory_digest"
    } <= columns
    assert staged_exists == (1,)
    assert {"outcomes_recorded_at_idx", "executions_task_idx"} <= indexes
    assert foreign_key_errors == []
    assert integrity == "ok"
    assert store.recoverable_attempts().attempt_ids == ()
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
        "max_output_tokens": 2, "expected_prompt_hash": "e" * 64,
        "inventory_digest": "1" * 64, "created_at": 11,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: store.create_execution_attempt(**values), range(2)))
    assert sorted(results) == [False, True]
    assert store.row_count("execution_attempts") == 1


def test_v2_digestless_recovery_is_quarantined_without_fabrication(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".graphite" / "routing" / "events.sqlite3"
    path.parent.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "routing_schema_v2_ca77600.sql"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(fixture.read_text(encoding="utf-8"))
        for suffix in ("pending", "recover", "completed"):
            connection.execute(
                "INSERT INTO tasks VALUES (?, 'isolated_code', 'low', ?, 1)",
                (f"task-{suffix}", suffix[0] * 64),
            )
            connection.execute(
                "INSERT INTO decisions VALUES (?, ?, 'model:cloud', 'default', '1', '1', 1)",
                (f"decision-{suffix}", f"task-{suffix}"),
            )
            connection.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, 'consumed', 99, 10)",
                (f"approval-{suffix}", f"task-{suffix}", f"decision-{suffix}",
                 suffix[0] * 64, suffix[-1] * 64),
            )
        connection.execute(
            "INSERT INTO executions VALUES ('exec-completed','attempt-completed','task-completed',"
            "'decision-completed','approval-completed','model:cloud','default','succeeded',"
            "10,6,2,1,2)"
        )
        connection.execute(
            "INSERT INTO executions VALUES ('exec-shadow','shadow-key','task-completed',"
            "'decision-completed','approval-completed','model:cloud','default','succeeded',"
            "10,5,2,1,2)"
        )
        attempts = [
            ("attempt-pending", "pending", None, None),
            ("attempt-recover", "persistence_failed", "execution_persistence_failed", None),
            ("attempt-completed", "completed", None, "exec-completed"),
        ]
        for attempt_id, status, reason, execution_id in attempts:
            suffix = attempt_id.removeprefix("attempt-")
            connection.execute(
                "INSERT INTO execution_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, f"approval-{suffix}", f"task-{suffix}", f"decision-{suffix}",
                 suffix[-1] * 64, "b" * 64, "model:cloud", "default", 10, 8, 2,
                 "c" * 64, status, reason, execution_id, 1, 2),
            )
        connection.execute(
            "INSERT INTO staged_execution_receipts VALUES "
            "('attempt-recover','exec-recover','approval-recover','model:cloud','default',"
            "'succeeded',6,2,5,?,?,NULL,2)", ("c" * 64, "d" * 64),
        )
        connection.execute(
            "INSERT INTO execution_receipts VALUES "
            "('exec-completed','approval-completed','model:cloud','default','succeeded',"
            "6,2,5,?,?,NULL,2)", ("c" * 64, "e" * 64),
        )
        connection.execute(
            "INSERT INTO execution_evidence VALUES "
            "('exec-completed','task-completed','decision-completed',?)", ("b" * 64,),
        )
        connection.execute(
            "INSERT INTO outcomes VALUES "
            "('outcome-1','exec-completed','human',1,0,3)"
        )
        connection.execute(
            "INSERT INTO shadow_comparisons VALUES "
            "('shadow-1','exec-completed','exec-shadow','primary',3)"
        )
        connection.execute(
            "INSERT INTO policy_versions VALUES ('1',?,'active',1)", ("e" * 64,)
        )
        connection.execute(
            "INSERT INTO budget_ledger VALUES "
            "('reserve-1','approval-completed','exec-completed','reservation',10,1)"
        )
        connection.execute(
            "INSERT INTO confidence_stats VALUES "
            "('model:cloud','default','isolated_code','low',4,3,1)"
        )
        connection.execute(
            "INSERT INTO incident_reviews(execution_id,reviewed_at) "
            "VALUES ('exec-completed',4)"
        )
        connection.execute(
            "INSERT INTO blind_comparisons VALUES "
            "('blind-1','exec-completed','exec-shadow',?,?,1,'a',4,3)",
            ("f" * 64, "0" * 64),
        )
        connection.execute(
            "INSERT INTO registry_snapshots VALUES (1,'{}',?,1,99)", ("9" * 64,)
        )
        preserved_tables = (
            "tasks", "decisions", "approvals", "executions", "outcomes",
            "shadow_comparisons", "policy_versions", "budget_ledger",
            "confidence_stats", "execution_evidence", "execution_receipts",
            "incident_reviews", "blind_comparisons", "registry_snapshots",
        )
        before = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in preserved_tables
        }
        before_attempts = connection.execute(
            "SELECT attempt_id,approval_id,task_id,decision_id,manifest_hash,"
            "graph_fingerprint,model_id,effort,reserved_tokens,max_input_tokens,"
            "max_output_tokens,expected_prompt_hash,status,failure_reason,execution_id,"
            "created_at,updated_at FROM execution_attempts ORDER BY attempt_id"
        ).fetchall()
        before_staged = connection.execute(
            "SELECT * FROM staged_execution_receipts ORDER BY attempt_id"
        ).fetchall()
        before_indexes = connection.execute(
            "SELECT name,sql FROM sqlite_master "
            "WHERE type='index' AND sql IS NOT NULL ORDER BY name"
        ).fetchall()

    store = RepositoryStore(root)
    store.initialize()
    store.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("7",)
        rows = connection.execute(
            "SELECT attempt_id,status,failure_reason,inventory_digest "
            "FROM execution_attempts ORDER BY attempt_id"
        ).fetchall()
        after = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in preserved_tables
        }
        after_attempts = connection.execute(
            "SELECT attempt_id,approval_id,task_id,decision_id,manifest_hash,"
            "graph_fingerprint,model_id,effort,reserved_tokens,max_input_tokens,"
            "max_output_tokens,expected_prompt_hash,status,failure_reason,execution_id,"
            "created_at,updated_at FROM execution_attempts ORDER BY attempt_id"
        ).fetchall()
        after_staged = connection.execute(
            "SELECT attempt_id,execution_id,approval_id,model_id,effort,outcome,"
            "input_tokens,output_tokens,latency_ms,prompt_hash,response_hash,"
            "failure_reason,staged_at FROM staged_execution_receipts ORDER BY attempt_id"
        ).fetchall()
        after_indexes = connection.execute(
            "SELECT name,sql FROM sqlite_master "
            "WHERE type='index' AND sql IS NOT NULL ORDER BY name"
        ).fetchall()
        assert rows == [
            ("attempt-completed", "completed", None, None),
            ("attempt-pending", "legacy_unrecoverable", "legacy_attempt_digest_missing", None),
            ("attempt-recover", "legacy_unrecoverable", "legacy_attempt_digest_missing", None),
        ]
        assert connection.execute(
            "SELECT execution_id, inventory_digest FROM staged_execution_receipts"
        ).fetchone() == ("exec-recover", None)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        canonical_columns = {
            "tasks": [row[1] for row in connection.execute("PRAGMA table_info(tasks)")],
            "decisions": [
                row[1] for row in connection.execute("PRAGMA table_info(decisions)")
            ],
        }
    assert canonical_columns["tasks"] == [
        "task_id", "category", "risk", "objective_hash", "created_at"
    ]
    assert canonical_columns["decisions"] == [
        "decision_id", "task_id", "model_id", "effort", "policy_version",
        "evidence_version", "created_at",
    ]
    assert after == before
    expected_attempts = []
    for row in before_attempts:
        expected = list(row)
        if expected[12] in {"pending", "persistence_failed"}:
            expected[12] = "legacy_unrecoverable"
            expected[13] = "legacy_attempt_digest_missing"
        expected_attempts.append(tuple(expected))
    assert after_attempts == expected_attempts
    assert after_staged == before_staged
    assert after_indexes == before_indexes
    assert store.recoverable_attempts().attempt_ids == ()
    assert store.execution_attempt("attempt-completed")["execution_id"] == "exec-completed"
    for attempt_id in ("attempt-pending", "attempt-recover"):
        with pytest.raises(StorageError, match="^legacy_attempt_digest_missing$"):
            store.reconcile_execution_attempt(attempt_id, completed_at=3)

    store.record_task("task-new", "isolated_code", "low", "1" * 64, 5)
    store.record_decision(
        "decision-new", "task-new", "model:cloud", "default", "1", "1", 5
    )
    store.save_approval_record(
        approval_id="approval-new", task_id="task-new", decision_id="decision-new",
        nonce_hash="2" * 64, manifest_hash="3" * 64, expires_at=99,
        reserved_tokens=10,
    )
    store.create_execution_attempt(
        attempt_id="attempt-new", approval_id="approval-new", task_id="task-new",
        decision_id="decision-new", manifest_hash="3" * 64,
        graph_fingerprint="1" * 64, model_id="model:cloud", effort="default",
        reserved_tokens=10, max_input_tokens=8, max_output_tokens=2,
        expected_prompt_hash="4" * 64, inventory_digest="5" * 64, created_at=5,
    )
    store.save_approval_record(
        approval_id="approval-insert", task_id="task-new", decision_id="decision-new",
        nonce_hash="6" * 64, manifest_hash="7" * 64, expires_at=99,
        reserved_tokens=10,
    )
    with sqlite3.connect(path) as connection:
        for statement, parameters in (
            (
                "UPDATE execution_attempts SET status='pending' WHERE attempt_id=?",
                ("attempt-completed",),
            ),
            (
                "UPDATE execution_attempts SET inventory_digest=? WHERE attempt_id=?",
                ("6" * 64, "attempt-completed"),
            ),
            (
                "UPDATE execution_attempts SET inventory_digest=? WHERE attempt_id=?",
                ("A" * 64, "attempt-new"),
            ),
            (
                "UPDATE staged_execution_receipts SET inventory_digest=? WHERE attempt_id=?",
                ("6" * 64, "attempt-recover"),
            ),
            (
                "DELETE FROM staged_execution_receipts WHERE attempt_id=?",
                ("attempt-recover",),
            ),
            (
                "DELETE FROM execution_attempts WHERE attempt_id=?",
                ("attempt-completed",),
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, parameters)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO execution_attempts(attempt_id,approval_id,task_id,decision_id,"
                "manifest_hash,graph_fingerprint,model_id,effort,reserved_tokens,"
                "max_input_tokens,max_output_tokens,expected_prompt_hash,status,"
                "failure_reason,execution_id,created_at,updated_at,inventory_digest) VALUES "
                "('attempt-insert','approval-insert','task-new','decision-new',?,?,?,?,"
                "10,8,2,?,'pending',NULL,NULL,6,6,NULL)",
                ("7" * 64, "1" * 64, "model:cloud", "default", "4" * 64),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO staged_execution_receipts(attempt_id,execution_id,approval_id,"
                "model_id,effort,outcome,input_tokens,output_tokens,latency_ms,prompt_hash,"
                "response_hash,failure_reason,staged_at,inventory_digest) VALUES "
                "('attempt-new','exec-new','approval-new','model:cloud','default',"
                "'succeeded',6,2,5,?,?,NULL,6,NULL)",
                ("4" * 64, "7" * 64),
            )
        connection.execute(
            "INSERT INTO staged_execution_receipts(attempt_id,execution_id,approval_id,"
            "model_id,effort,outcome,input_tokens,output_tokens,latency_ms,prompt_hash,"
            "response_hash,failure_reason,staged_at,inventory_digest) VALUES "
            "('attempt-new','exec-new','approval-new','model:cloud','default',"
            "'succeeded',6,2,5,?,?,NULL,6,?)",
            ("4" * 64, "7" * 64, "5" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE staged_execution_receipts SET inventory_digest=? "
                "WHERE attempt_id='attempt-new'", ("A" * 64,),
            )
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_recoverable_enumeration_is_validated_bounded_and_paginated(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for index in range(105):
            suffix = f"{index:03d}"
            connection.execute(
                "INSERT INTO tasks VALUES (?, 'isolated_code', 'low', ?, 1)",
                (f"task-{suffix}", "1" * 64),
            )
            connection.execute(
                "INSERT INTO decisions VALUES (?, ?, 'model:cloud', 'default', '1', '1', 1)",
                (f"decision-{suffix}", f"task-{suffix}"),
            )
            connection.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, 'consumed', 99, 10)",
                (f"approval-{suffix}", f"task-{suffix}", f"decision-{suffix}",
                 f"{index:064x}", f"{index + 200:064x}"),
            )
            connection.execute(
                "INSERT INTO execution_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "'persistence_failed','execution_persistence_failed',NULL,1,1)",
                (f"attempt-{suffix}", f"approval-{suffix}", f"task-{suffix}",
                 f"decision-{suffix}", f"{index + 200:064x}", "2" * 64,
                 "model:cloud", "default", 10, 8, 2, "3" * 64, "4" * 64),
            )
            connection.execute(
                "INSERT INTO staged_execution_receipts VALUES (?,?,?,?,?,'succeeded',"
                "6,2,5,?,?,?,NULL,1)",
                (f"attempt-{suffix}", f"exec-{suffix}", f"approval-{suffix}",
                 "model:cloud", "default", "3" * 64, "5" * 64, "4" * 64),
            )

    collected: list[str] = []
    cursor: str | None = None
    while True:
        page = store.recoverable_attempts(limit=17, after=cursor)
        assert len(page.attempt_ids) <= 17
        collected.extend(page.attempt_ids)
        if not page.has_more:
            assert page.next_cursor is None
            break
        assert page.next_cursor == page.attempt_ids[-1]
        cursor = page.next_cursor
    assert collected == [f"attempt-{index:03d}" for index in range(105)]
    assert len(collected) == len(set(collected))
    assert len(store.recoverable_attempts().attempt_ids) == 50
    for invalid in (0, 101, True):
        with pytest.raises(ValueError, match="^recovery_limit_invalid$"):
            store.recoverable_attempts(limit=invalid)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="^recovery_cursor_invalid$"):
        store.recoverable_attempts(after="../PRIVATE")


@pytest.mark.parametrize(
    "corrupt_id",
    ["C:\\PRIVATE\\secret\n\x1b", "a" * 257],
)
def test_recoverable_enumeration_never_reflects_corrupt_attempt_id(
    tmp_path: Path, corrupt_id: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO execution_attempts VALUES (?, 'approval-x', 'task-x', 'decision-x',"
            "?,?,?,?,?,?,?,?,?,'persistence_failed',NULL,NULL,1,1)",
            (corrupt_id, "1" * 64, "2" * 64, "model:cloud", "default", 10, 8, 2,
             "3" * 64, "4" * 64),
        )
        connection.execute(
            "INSERT INTO staged_execution_receipts VALUES (?, 'exec-x', 'approval-x',"
            "'model:cloud','default','succeeded',6,2,5,?,?,?,NULL,1)",
            (corrupt_id, "3" * 64, "5" * 64, "4" * 64),
        )
    with pytest.raises(StorageError, match="^storage_corrupt$") as caught:
        store.recoverable_attempts()
    assert corrupt_id not in str(caught.value)


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


def _narrow_provider_checks(path: Path, *, stamp_version: str) -> None:
    """Rewrite the two provider CHECKs back to the legacy two-provider form."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA writable_schema=ON")
        for table in ("capability_snapshots", "cli_execution_attempts"):
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            assert row is not None and "'openrouter'" in row[0]
            narrowed = row[0].replace(
                "provider IN ('claude-code', 'codex', 'openrouter', 'zai')",
                "provider IN ('claude-code', 'codex')",
            )
            connection.execute(
                "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
                (narrowed, table),
            )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (stamp_version,),
        )
        connection.commit()
    finally:
        connection.close()


def _revert_provider_checks_to_v6(path: Path, *, stamp_version: str) -> None:
    """Rewrite the two provider CHECKs back to the openrouter-inclusive v6 form.

    Unlike ``_narrow_provider_checks`` (which strips the CHECK down to the
    fail-closed claude/codex form), this reconstructs a *valid* pre-zai v6
    store: it removes only the ``, 'zai'`` widening so the CHECK reads
    ``provider IN ('claude-code', 'codex', 'openrouter')`` and stamps the
    supplied schema version. This is what a genuine v6 store looks like before
    the v6->v7 rebuild admits 'zai'.
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA writable_schema=ON")
        for table in ("capability_snapshots", "cli_execution_attempts"):
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            assert row is not None and "'zai'" in row[0]
            reverted = row[0].replace(
                "provider IN ('claude-code', 'codex', 'openrouter', 'zai')",
                "provider IN ('claude-code', 'codex', 'openrouter')",
            )
            connection.execute(
                "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
                (reverted, table),
            )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (stamp_version,),
        )
        connection.commit()
    finally:
        connection.close()


def _seed_claude_snapshot(root: Path) -> str:
    from graphite.routing.contracts import (
        CliIdentity,
        PermissionMode,
        ProviderId,
        RiskTier,
    )
    from graphite.routing.profiles import (
        BUNDLED_REQUESTED_PROFILES,
        create_capability_snapshot,
        save_capability_snapshot,
    )

    store = RepositoryStore(root)
    snapshot = create_capability_snapshot(
        requested=BUNDLED_REQUESTED_PROFILES["claude-code/sonnet"],
        identity=CliIdentity(ProviderId.CLAUDE_CODE, "a" * 64, "2.1.208", "1.0.0"),
        effective_model="claude-sonnet-5",
        effort=Effort.HIGH,
        capabilities=("code", "reasoning"),
        context_window_tokens=200_000,
        risk_ceiling=RiskTier.MEDIUM,
        permission_mode=PermissionMode.READ_ONLY,
        verified_at=1_700_000_000,
        ttl_seconds=86_400,
    )
    assert save_capability_snapshot(store, snapshot) is True
    return snapshot.digest


def _save_openrouter_snapshot(root: Path) -> bool:
    from graphite.routing.contracts import (
        CliIdentity,
        Effort as _Effort,
        PermissionMode,
        ProviderId,
        RiskTier,
    )
    from graphite.routing.profiles import (
        create_capability_snapshot,
        operator_openrouter_profile,
        save_capability_snapshot,
    )

    store = RepositoryStore(root)
    snapshot = create_capability_snapshot(
        requested=operator_openrouter_profile(
            model_id="moonshotai/kimi-k3",
            supported_efforts=(_Effort.HIGH,),
            evidence_url="https://openrouter.ai/moonshotai/kimi-k3",
            evidence_accessed="2026-07-20",
        ),
        identity=CliIdentity(ProviderId.OPENROUTER, "b" * 64, "1.0.0", "1.0.0"),
        effective_model="moonshotai/kimi-k3",
        effort=_Effort.HIGH,
        capabilities=("code",),
        context_window_tokens=262_144,
        risk_ceiling=RiskTier.HIGH,
        permission_mode=PermissionMode.READ_ONLY,
        verified_at=1_700_000_100,
        ttl_seconds=86_400,
    )
    return save_capability_snapshot(store, snapshot)


def test_v5_store_with_legacy_provider_check_migrates_preserving_rows(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    claude_digest = _seed_claude_snapshot(root)
    _narrow_provider_checks(store.path, stamp_version="5")
    narrowed = sqlite3.connect(store.path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            narrowed.execute(
                "INSERT INTO capability_snapshots VALUES"
                " (?, 'openrouter', 'm/m', 'm/m', 'high', ?,"
                " '1.0.0', '1.0.0', 'read-only', '{}', 1, 2)",
                ("f" * 64, "b" * 64),
            )
    finally:
        narrowed.close()

    RepositoryStore(root).initialize()

    connection = sqlite3.connect(store.path)
    try:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert version == "7"
        for table in ("capability_snapshots", "cli_execution_attempts"):
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()[0]
            assert "'openrouter'" in sql
            assert "'zai'" in sql
        preserved = connection.execute(
            "SELECT capability_snapshot_digest FROM capability_snapshots"
        ).fetchall()
        assert preserved == [(claude_digest,)]
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert "capability_snapshot_update_guard" in triggers
        assert "cli_attempt_identity_update_guard" in triggers
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        connection.close()
    backup = store.path.parent / "backups" / "events-schema-v5-pre-v6.sqlite3"
    assert backup.exists()
    assert _save_openrouter_snapshot(root) is True


def test_migrated_store_reinitializes_idempotently(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    _seed_claude_snapshot(root)
    _narrow_provider_checks(store.path, stamp_version="5")
    RepositoryStore(root).initialize()
    RepositoryStore(root).initialize()
    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("7",)
        assert connection.execute(
            "SELECT COUNT(*) FROM capability_snapshots"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_v6_stamped_store_with_narrow_check_fails_closed(tmp_path: Path) -> None:
    # NB: the "v6" in the name is historical; this stamps the CURRENT schema
    # version ("7") with a narrowed provider CHECK to assert that opening a
    # current-version store whose DDL lost 'openrouter'/'zai' fails closed
    # (_validate_v6_schema, the current-version validator, raises).
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    _narrow_provider_checks(store.path, stamp_version="7")
    with pytest.raises(StorageError, match="^storage_rollback_required$"):
        RepositoryStore(root).initialize()


def test_v6_store_migrates_to_v7_and_admits_zai(tmp_path):
    import sqlite3
    from graphite.routing.storage import RepositoryStore, SCHEMA_VERSION
    root = tmp_path / "repo"
    (root / ".graphite" / "routing").mkdir(parents=True)
    store = RepositoryStore(root)          # fresh -> should be at SCHEMA_VERSION
    store.initialize()
    assert SCHEMA_VERSION == "7"
    db = root / ".graphite" / "routing" / "events.sqlite3"
    con = sqlite3.connect(db)
    try:
        sv = con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        assert sv == "7"
        ddl = con.execute(
            "SELECT sql FROM sqlite_master WHERE name='capability_snapshots'").fetchone()[0]
        assert "'zai'" in ddl
        ddl2 = con.execute(
            "SELECT sql FROM sqlite_master WHERE name='cli_execution_attempts'").fetchone()[0]
        assert "'zai'" in ddl2
    finally:
        con.close()


def test_v6_store_with_rows_rebuilds_to_v7_preserving_rows_and_admitting_zai(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()  # fresh -> current v7; both DDLs already carry 'zai'

    # Seed a claude-code snapshot row (valid under BOTH the old v6 CHECK and the
    # widened v7 CHECK) so the rebuild has a real row to carry through.
    claude_digest = _seed_claude_snapshot(root)
    seeding = sqlite3.connect(store.path)
    try:
        # Fail loudly if the seed silently didn't land, so a later "row
        # survived" assertion can never masquerade over a missing seed.
        assert seeding.execute(
            "SELECT COUNT(*) FROM capability_snapshots "
            "WHERE capability_snapshot_digest=?",
            (claude_digest,),
        ).fetchone() == (1,)
    finally:
        seeding.close()

    # Reconstruct a genuine v6 store: revert both provider CHECKs to the
    # openrouter-inclusive pre-zai form and stamp schema_version='6'.
    _revert_provider_checks_to_v6(store.path, stamp_version="6")

    # Precondition (anti-false-green): at v6, both DDLs admit 'openrouter' but
    # NOT 'zai'. This is what makes the post-condition meaningful -- the only
    # thing that can reintroduce 'zai' is the rebuild recreating the tables.
    precondition = sqlite3.connect(store.path)
    try:
        assert precondition.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("6",)
        for table in ("capability_snapshots", "cli_execution_attempts"):
            sql = precondition.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()[0]
            assert "'openrouter'" in sql
            assert "'zai'" not in sql
    finally:
        precondition.close()

    # Re-initialize a fresh handle -> drives _migrate_v6_to_v7's rebuild body
    # (CREATE-copy / DROP / recreate-widened / INSERT / COUNT-equality).
    RepositoryStore(root).initialize()

    # Proof the rebuild body actually ran (NOT an early-return): the pre-v7
    # backup is created only after all four guards pass (path exists,
    # version=='6', tables present, not-already-widened), immediately before
    # the CREATE-copy/DROP/INSERT loop.
    backup = store.path.parent / "backups" / "events-schema-v6-pre-v7.sqlite3"
    assert backup.exists()

    verify = sqlite3.connect(store.path)
    try:
        assert verify.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone() == ("7",)
        # Seeded row survived copy->drop->recreate->insert (row preservation).
        assert verify.execute(
            "SELECT capability_snapshot_digest FROM capability_snapshots "
            "WHERE capability_snapshot_digest=?",
            (claude_digest,),
        ).fetchone() == (claude_digest,)
        # Both tables were recreated from the widened DDL -> now admit 'zai'.
        for table in ("capability_snapshots", "cli_execution_attempts"):
            sql = verify.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()[0]
            assert "'openrouter'" in sql
            assert "'zai'" in sql
        assert verify.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        verify.close()
