"""SQLite evidence storage with repository and aggregate trust boundaries."""
from __future__ import annotations

import os
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import stat
from contextlib import closing, contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .contracts import (
    CapabilitySnapshot,
    EvidenceProvenance,
    Effort,
    ExecutionOutcome,
    ExecutionReceipt,
    PermissionMode,
    ProviderId,
    RiskTier,
    TaskCategory,
)

SCHEMA_VERSION: Final = "8"
BUSY_TIMEOUT_MS: Final = 2_000
DEFAULT_RECOVERY_PAGE_SIZE: Final = 50
MAX_RECOVERY_PAGE_SIZE: Final = 100
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MODEL_IDENTIFIER = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}(/[A-Za-z0-9][A-Za-z0-9._:+-]{0,127})?$"
)
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}(?::cloud)?$")
_SNAPSHOT_MODEL_ID = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,127}(?::[a-z0-9][a-z0-9._-]{0,63})?$"
)
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_VERSION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SEMANTIC_VERSION = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$"
)
_CLI_TELEMETRY_KEYS: Final = frozenset(
    {
        "provider", "requested_model", "effective_model", "effort",
        "capability_snapshot_digest", "category", "risk", "latency_ms",
        "input_tokens", "output_tokens", "cost_status", "changed_file_count",
        "changed_byte_count", "validation_outcome", "review_defect_classes",
        "rework_count", "human_verdict", "provenance", "observed_at",
        "diff_sha256",
    }
)
_CLI_POLICY_KEYS: Final = frozenset(
    {
        "schema_version", "autonomy", "allowed_providers", "permission_ceiling",
        "risk_ceilings", "scoring_weights", "minimum_samples",
        "recency_half_life_days", "source_policy_version",
    }
)
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
        "capability_snapshots",
        "task_worktrees",
        "cli_execution_attempts",
        "validation_results",
        "review_links",
        "cli_telemetry_events",
        "cli_policy_versions",
        "cli_policy_events",
        "lifecycle_snapshot_bindings",
        "lifecycle_approval_bindings",
        "lifecycle_attempt_bindings",
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


def _model_identifier(value: object, code: str) -> str:
    """Model names additionally permit one OpenRouter-style vendor/model slash."""
    if not isinstance(value, str) or not _MODEL_IDENTIFIER.fullmatch(value):
        raise ValueError(code)
    return value


def _nonnegative_integer(value: object, code: str, maximum: int = 10**12) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ValueError(code)
    return value


def _positive_integer(value: object, code: str, maximum: int = 10**12) -> int:
    normalized = _nonnegative_integer(value, code, maximum)
    if normalized == 0:
        raise ValueError(code)
    return normalized


def _semantic_version(value: object, code: str) -> str:
    if not isinstance(value, str) or _SEMANTIC_VERSION.fullmatch(value) is None:
        raise ValueError(code)
    return value


def _translate_database_error(exc: sqlite3.Error) -> StorageError:
    message = str(exc).casefold()
    for code in (
        "lifecycle_snapshot_binding_missing",
        "lifecycle_approval_binding_missing",
        "lifecycle_snapshot_binding_immutable",
        "lifecycle_approval_binding_immutable",
        "lifecycle_attempt_binding_immutable",
        "lifecycle_binding_carry_immutable",
    ):
        if code in message:
            return StorageError(code)
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


_CAPABILITY_SNAPSHOTS_DDL: Final = """CREATE TABLE IF NOT EXISTS capability_snapshots (
        capability_snapshot_digest TEXT PRIMARY KEY CHECK(
            length(capability_snapshot_digest) = 64
            AND capability_snapshot_digest NOT GLOB '*[^0-9a-f]*'
        ),
        provider TEXT NOT NULL CHECK(provider IN ('claude-code', 'codex', 'openrouter', 'zai')),
        requested_model TEXT NOT NULL,
        effective_model TEXT NOT NULL,
        effort TEXT NOT NULL CHECK(effort IN ('default', 'low', 'medium', 'high', 'xhigh', 'max')),
        executable_sha256 TEXT NOT NULL CHECK(
            length(executable_sha256) = 64
            AND executable_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        cli_version TEXT NOT NULL,
        adapter_protocol_version TEXT NOT NULL,
        permission_mode TEXT NOT NULL CHECK(permission_mode IN ('read-only', 'workspace-write')),
        payload_json TEXT NOT NULL,
        verified_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL CHECK(expires_at > verified_at)
    )"""
_CAPABILITY_SNAPSHOTS_COLUMNS: Final = (
    "capability_snapshot_digest, provider, requested_model, effective_model, "
    "effort, executable_sha256, cli_version, adapter_protocol_version, "
    "permission_mode, payload_json, verified_at, expires_at"
)
_CLI_EXECUTION_ATTEMPTS_DDL: Final = """CREATE TABLE IF NOT EXISTS cli_execution_attempts (
        attempt_id TEXT PRIMARY KEY,
        approval_id TEXT NOT NULL UNIQUE REFERENCES approvals(approval_id) ON DELETE RESTRICT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
        decision_id TEXT NOT NULL REFERENCES decisions(decision_id) ON DELETE RESTRICT,
        worktree_id TEXT NOT NULL REFERENCES task_worktrees(worktree_id) ON DELETE RESTRICT,
        provider TEXT NOT NULL CHECK(provider IN ('claude-code', 'codex', 'openrouter', 'zai')),
        requested_model TEXT NOT NULL,
        effective_model TEXT NOT NULL,
        effort TEXT NOT NULL CHECK(effort IN ('default', 'low', 'medium', 'high', 'xhigh', 'max')),
        cli_version TEXT NOT NULL,
        executable_sha256 TEXT NOT NULL CHECK(
            length(executable_sha256) = 64
            AND executable_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        capability_snapshot_digest TEXT NOT NULL REFERENCES capability_snapshots(capability_snapshot_digest) ON DELETE RESTRICT,
        manifest_hash TEXT NOT NULL CHECK(
            length(manifest_hash) = 64 AND manifest_hash NOT GLOB '*[^0-9a-f]*'
        ),
        expected_prompt_hash TEXT NOT NULL CHECK(
            length(expected_prompt_hash) = 64 AND expected_prompt_hash NOT GLOB '*[^0-9a-f]*'
        ),
        reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens > 0),
        status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'failed', 'persistence_failed', 'quarantined')),
        failure_reason TEXT,
        execution_id TEXT UNIQUE REFERENCES executions(execution_id) ON DELETE RESTRICT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )"""
_CLI_EXECUTION_ATTEMPTS_COLUMNS: Final = (
    "attempt_id, approval_id, task_id, decision_id, worktree_id, provider, "
    "requested_model, effective_model, effort, cli_version, executable_sha256, "
    "capability_snapshot_digest, manifest_hash, expected_prompt_hash, "
    "reserved_tokens, status, failure_reason, execution_id, created_at, updated_at"
)
_PRE_V6_WIDENED_TABLES: Final = {
    "capability_snapshots": (_CAPABILITY_SNAPSHOTS_DDL, _CAPABILITY_SNAPSHOTS_COLUMNS),
    "cli_execution_attempts": (_CLI_EXECUTION_ATTEMPTS_DDL, _CLI_EXECUTION_ATTEMPTS_COLUMNS),
}

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
        inventory_digest TEXT NOT NULL CHECK(
            length(inventory_digest) = 64
            AND inventory_digest NOT GLOB '*[^0-9a-f]*'
        ),
        status TEXT NOT NULL CHECK(status IN (
            'pending', 'completed', 'failed', 'persistence_failed', 'legacy_unrecoverable'
        )),
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
        inventory_digest TEXT NOT NULL CHECK(
            length(inventory_digest) = 64
            AND inventory_digest NOT GLOB '*[^0-9a-f]*'
        ),
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
    _CAPABILITY_SNAPSHOTS_DDL,
    """CREATE TABLE IF NOT EXISTS task_worktrees (
        worktree_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
        baseline_commit TEXT NOT NULL CHECK(
            (length(baseline_commit) = 40 OR length(baseline_commit) = 64)
            AND baseline_commit NOT GLOB '*[^0-9a-f]*'
        ),
        canonical_root_hash TEXT NOT NULL CHECK(
            length(canonical_root_hash) = 64
            AND canonical_root_hash NOT GLOB '*[^0-9a-f]*'
        ),
        status TEXT NOT NULL CHECK(status IN ('prepared', 'executed', 'accepted', 'rejected', 'quarantined', 'cleaned')),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )""",
    _CLI_EXECUTION_ATTEMPTS_DDL,
    """CREATE TABLE IF NOT EXISTS validation_results (
        attempt_id TEXT PRIMARY KEY REFERENCES cli_execution_attempts(attempt_id) ON DELETE RESTRICT,
        diff_hash TEXT NOT NULL CHECK(
            length(diff_hash) = 64 AND diff_hash NOT GLOB '*[^0-9a-f]*'
        ),
        changed_file_count INTEGER NOT NULL CHECK(changed_file_count >= 0),
        changed_byte_count INTEGER NOT NULL CHECK(changed_byte_count >= 0),
        outcome TEXT NOT NULL CHECK(outcome IN ('passed', 'failed', 'blocked', 'not_run')),
        recorded_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS review_links (
        review_attempt_id TEXT PRIMARY KEY REFERENCES cli_execution_attempts(attempt_id) ON DELETE RESTRICT,
        primary_attempt_id TEXT NOT NULL REFERENCES cli_execution_attempts(attempt_id) ON DELETE RESTRICT,
        primary_diff_hash TEXT NOT NULL CHECK(
            length(primary_diff_hash) = 64 AND primary_diff_hash NOT GLOB '*[^0-9a-f]*'
        ),
        created_at INTEGER NOT NULL,
        CHECK(review_attempt_id != primary_attempt_id)
    )""",
    """CREATE TABLE IF NOT EXISTS cli_telemetry_events (
        event_hash TEXT PRIMARY KEY CHECK(
            length(event_hash) = 64 AND event_hash NOT GLOB '*[^0-9a-f]*'
        ),
        payload_json TEXT NOT NULL CHECK(length(payload_json) <= 8192),
        observed_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cli_policy_versions (
        policy_version TEXT PRIMARY KEY,
        parent_version TEXT REFERENCES cli_policy_versions(policy_version) ON DELETE RESTRICT,
        payload_json TEXT NOT NULL CHECK(length(payload_json) <= 16384),
        payload_hash TEXT NOT NULL CHECK(
            length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'
        ),
        signature TEXT NOT NULL CHECK(
            length(signature) = 64 AND signature NOT GLOB '*[^0-9a-f]*'
        ),
        evidence_hash TEXT NOT NULL CHECK(
            length(evidence_hash) = 64 AND evidence_hash NOT GLOB '*[^0-9a-f]*'
        ),
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cli_policy_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        policy_version TEXT NOT NULL REFERENCES cli_policy_versions(policy_version) ON DELETE RESTRICT,
        action TEXT NOT NULL CHECK(action IN ('candidate', 'promote', 'rollback')),
        prior_version TEXT REFERENCES cli_policy_versions(policy_version) ON DELETE RESTRICT,
        actor TEXT NOT NULL CHECK(actor = 'human'),
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS lifecycle_snapshot_bindings (
        capability_snapshot_digest TEXT PRIMARY KEY
            REFERENCES capability_snapshots(capability_snapshot_digest) ON DELETE RESTRICT,
        lifecycle_identity_digest TEXT NOT NULL CHECK(
            length(lifecycle_identity_digest) = 64
            AND lifecycle_identity_digest NOT GLOB '*[^0-9a-f]*'
        ),
        bound_at INTEGER NOT NULL CHECK(bound_at >= 0)
    )""",
    """CREATE TABLE IF NOT EXISTS lifecycle_approval_bindings (
        approval_id TEXT PRIMARY KEY REFERENCES approvals(approval_id) ON DELETE RESTRICT,
        capability_snapshot_digest TEXT NOT NULL
            REFERENCES capability_snapshots(capability_snapshot_digest) ON DELETE RESTRICT,
        lifecycle_identity_digest TEXT NOT NULL CHECK(
            length(lifecycle_identity_digest) = 64
            AND lifecycle_identity_digest NOT GLOB '*[^0-9a-f]*'
        ),
        bound_at INTEGER NOT NULL CHECK(bound_at >= 0)
    )""",
    """CREATE TABLE IF NOT EXISTS lifecycle_attempt_bindings (
        attempt_id TEXT PRIMARY KEY
            REFERENCES cli_execution_attempts(attempt_id) ON DELETE RESTRICT,
        lifecycle_identity_digest TEXT NOT NULL CHECK(
            length(lifecycle_identity_digest) = 64
            AND lifecycle_identity_digest NOT GLOB '*[^0-9a-f]*'
        ),
        bound_at INTEGER NOT NULL CHECK(bound_at >= 0)
    )""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_snapshot_binding_update_guard
    BEFORE UPDATE ON lifecycle_snapshot_bindings BEGIN
        SELECT RAISE(ABORT, 'lifecycle_snapshot_binding_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_snapshot_binding_delete_guard
    BEFORE DELETE ON lifecycle_snapshot_bindings BEGIN
        SELECT RAISE(ABORT, 'lifecycle_snapshot_binding_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_approval_binding_update_guard
    BEFORE UPDATE ON lifecycle_approval_bindings BEGIN
        SELECT RAISE(ABORT, 'lifecycle_approval_binding_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_approval_binding_delete_guard
    BEFORE DELETE ON lifecycle_approval_bindings BEGIN
        SELECT RAISE(ABORT, 'lifecycle_approval_binding_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_attempt_binding_update_guard
    BEFORE UPDATE ON lifecycle_attempt_bindings BEGIN
        SELECT RAISE(ABORT, 'lifecycle_attempt_binding_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_attempt_binding_insert_guard
    BEFORE INSERT ON lifecycle_attempt_bindings
    WHEN NOT EXISTS (
        SELECT 1
        FROM cli_execution_attempts AS attempt
        JOIN lifecycle_approval_bindings AS binding
          ON binding.approval_id = attempt.approval_id
        WHERE attempt.attempt_id = NEW.attempt_id
          AND binding.lifecycle_identity_digest = NEW.lifecycle_identity_digest
    )
    BEGIN
        SELECT RAISE(ABORT, 'lifecycle_approval_binding_missing');
    END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_attempt_binding_delete_guard
    BEFORE DELETE ON lifecycle_attempt_bindings BEGIN
        SELECT RAISE(ABORT, 'lifecycle_attempt_binding_immutable');
    END""",
    """CREATE TABLE IF NOT EXISTS lifecycle_binding_carries (
        carry_id INTEGER PRIMARY KEY AUTOINCREMENT,
        capability_snapshot_digest TEXT NOT NULL
            REFERENCES capability_snapshots(capability_snapshot_digest) ON DELETE RESTRICT,
        previous_identity_digest TEXT NOT NULL CHECK(
            length(previous_identity_digest) = 64
            AND previous_identity_digest NOT GLOB '*[^0-9a-f]*'
        ),
        new_identity_digest TEXT NOT NULL CHECK(
            length(new_identity_digest) = 64
            AND new_identity_digest NOT GLOB '*[^0-9a-f]*'
        ),
        lifecycle_event_id TEXT NOT NULL CHECK(
            length(lifecycle_event_id) = 64
            AND lifecycle_event_id NOT GLOB '*[^0-9a-f]*'
        ),
        carried_at INTEGER NOT NULL CHECK(carried_at >= 0)
    )""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_binding_carry_update_guard
    BEFORE UPDATE ON lifecycle_binding_carries BEGIN
        SELECT RAISE(ABORT, 'lifecycle_binding_carry_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_binding_carry_delete_guard
    BEFORE DELETE ON lifecycle_binding_carries BEGIN
        SELECT RAISE(ABORT, 'lifecycle_binding_carry_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS lifecycle_approval_binding_insert_guard_v8
    BEFORE INSERT ON lifecycle_approval_bindings
    WHEN NOT EXISTS (
        SELECT 1 FROM lifecycle_snapshot_bindings AS binding
        WHERE binding.capability_snapshot_digest = NEW.capability_snapshot_digest
          AND binding.lifecycle_identity_digest = NEW.lifecycle_identity_digest
    ) AND NOT EXISTS (
        SELECT 1 FROM lifecycle_binding_carries AS carry
        WHERE carry.capability_snapshot_digest = NEW.capability_snapshot_digest
          AND carry.new_identity_digest = NEW.lifecycle_identity_digest
    )
    BEGIN
        SELECT RAISE(ABORT, 'lifecycle_snapshot_binding_missing');
    END""",
    """CREATE TRIGGER IF NOT EXISTS cli_attempt_identity_update_guard
    BEFORE UPDATE OF approval_id, task_id, decision_id, worktree_id, provider,
        requested_model, effective_model, effort, cli_version, executable_sha256,
        capability_snapshot_digest, manifest_hash, expected_prompt_hash, reserved_tokens
    ON cli_execution_attempts
    BEGIN
        SELECT RAISE(ABORT, 'cli_attempt_identity_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS capability_snapshot_update_guard
    BEFORE UPDATE ON capability_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'capability_snapshot_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS capability_snapshot_delete_guard
    BEFORE DELETE ON capability_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'capability_snapshot_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS cli_attempt_status_transition_guard
    BEFORE UPDATE OF status ON cli_execution_attempts
    WHEN NOT (
        (OLD.status = 'pending' AND NEW.status IN (
            'completed', 'failed', 'persistence_failed', 'quarantined'
        ))
        OR (OLD.status = 'persistence_failed' AND NEW.status IN (
            'completed', 'failed', 'quarantined'
        ))
        OR OLD.status = NEW.status
    )
    BEGIN
        SELECT RAISE(ABORT, 'cli_attempt_transition_invalid');
    END""",
    """CREATE TRIGGER IF NOT EXISTS cli_attempt_delete_guard
    BEFORE DELETE ON cli_execution_attempts
    BEGIN
        SELECT RAISE(ABORT, 'cli_attempt_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS worktree_identity_update_guard
    BEFORE UPDATE OF worktree_id, task_id, baseline_commit, canonical_root_hash, created_at
    ON task_worktrees
    BEGIN
        SELECT RAISE(ABORT, 'worktree_identity_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS worktree_status_transition_guard
    BEFORE UPDATE OF status ON task_worktrees
    WHEN NOT (
        (OLD.status = 'prepared' AND NEW.status IN ('executed', 'quarantined'))
        OR (OLD.status = 'executed' AND NEW.status IN ('accepted', 'rejected', 'quarantined'))
        OR (OLD.status IN ('accepted', 'rejected', 'quarantined') AND NEW.status = 'cleaned')
        OR OLD.status = NEW.status
    )
    BEGIN
        SELECT RAISE(ABORT, 'worktree_transition_invalid');
    END""",
    """CREATE TRIGGER IF NOT EXISTS worktree_delete_guard
    BEFORE DELETE ON task_worktrees
    BEGIN
        SELECT RAISE(ABORT, 'worktree_evidence_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS validation_result_update_guard
    BEFORE UPDATE ON validation_results
    BEGIN
        SELECT RAISE(ABORT, 'validation_result_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS validation_result_delete_guard
    BEFORE DELETE ON validation_results
    BEGIN
        SELECT RAISE(ABORT, 'validation_result_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS review_link_update_guard
    BEFORE UPDATE ON review_links
    BEGIN
        SELECT RAISE(ABORT, 'review_link_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS review_link_delete_guard
    BEFORE DELETE ON review_links
    BEGIN
        SELECT RAISE(ABORT, 'review_link_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS cli_telemetry_update_guard
    BEFORE UPDATE ON cli_telemetry_events
    BEGIN
        SELECT RAISE(ABORT, 'cli_telemetry_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS cli_telemetry_delete_guard
    BEFORE DELETE ON cli_telemetry_events
    BEGIN
        SELECT RAISE(ABORT, 'cli_telemetry_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS cli_policy_version_update_guard
    BEFORE UPDATE ON cli_policy_versions
    BEGIN
        SELECT RAISE(ABORT, 'cli_policy_version_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS cli_policy_version_delete_guard
    BEFORE DELETE ON cli_policy_versions
    BEGIN
        SELECT RAISE(ABORT, 'cli_policy_version_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS cli_policy_event_update_guard
    BEFORE UPDATE ON cli_policy_events
    BEGIN
        SELECT RAISE(ABORT, 'cli_policy_event_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS cli_policy_event_delete_guard
    BEFORE DELETE ON cli_policy_events
    BEGIN
        SELECT RAISE(ABORT, 'cli_policy_event_immutable');
    END""",
    """CREATE TRIGGER IF NOT EXISTS execution_attempt_digest_insert_guard
    BEFORE INSERT ON execution_attempts
    WHEN NEW.inventory_digest IS NULL
      OR length(NEW.inventory_digest) != 64
      OR NEW.inventory_digest GLOB '*[^0-9a-f]*'
    BEGIN
        SELECT RAISE(ABORT, 'inventory_digest_invalid');
    END""",
    """CREATE TRIGGER IF NOT EXISTS execution_attempt_digest_update_guard
    BEFORE UPDATE ON execution_attempts
    WHEN OLD.inventory_digest IS NULL
      OR NEW.inventory_digest IS NULL
      OR length(NEW.inventory_digest) != 64
      OR NEW.inventory_digest GLOB '*[^0-9a-f]*'
    BEGIN
        SELECT RAISE(ABORT, 'inventory_digest_invalid');
    END""",
    """CREATE TRIGGER IF NOT EXISTS execution_attempt_digest_delete_guard
    BEFORE DELETE ON execution_attempts
    WHEN OLD.inventory_digest IS NULL
    BEGIN
        SELECT RAISE(ABORT, 'inventory_digest_invalid');
    END""",
    """CREATE TRIGGER IF NOT EXISTS staged_receipt_digest_insert_guard
    BEFORE INSERT ON staged_execution_receipts
    WHEN NEW.inventory_digest IS NULL
      OR length(NEW.inventory_digest) != 64
      OR NEW.inventory_digest GLOB '*[^0-9a-f]*'
    BEGIN
        SELECT RAISE(ABORT, 'inventory_digest_invalid');
    END""",
    """CREATE TRIGGER IF NOT EXISTS staged_receipt_digest_update_guard
    BEFORE UPDATE ON staged_execution_receipts
    WHEN OLD.inventory_digest IS NULL
      OR NEW.inventory_digest IS NULL
      OR length(NEW.inventory_digest) != 64
      OR NEW.inventory_digest GLOB '*[^0-9a-f]*'
    BEGIN
        SELECT RAISE(ABORT, 'inventory_digest_invalid');
    END""",
    """CREATE TRIGGER IF NOT EXISTS staged_receipt_digest_delete_guard
    BEFORE DELETE ON staged_execution_receipts
    WHEN OLD.inventory_digest IS NULL
    BEGIN
        SELECT RAISE(ABORT, 'inventory_digest_invalid');
    END""",
)


@dataclass(frozen=True, slots=True)
class RecoverableAttemptPage:
    """Bounded public recovery enumeration with an opaque validated cursor."""

    attempt_ids: tuple[str, ...]
    next_cursor: str | None
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": [
                {"attempt_id": attempt_id, "status": "recoverable"}
                for attempt_id in self.attempt_ids
            ],
            "has_more": self.has_more,
            "next_cursor": self.next_cursor,
        }


class RepositoryStore:
    """Detailed evidence that never leaves the selected repository by default."""

    def __init__(self, repository_root: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> None:
        self.root = _selected_root(repository_root)
        if isinstance(busy_timeout_ms, bool) or not 100 <= busy_timeout_ms <= 30_000:
            raise ValueError("busy_timeout_invalid")
        self.busy_timeout_ms = busy_timeout_ms
        self.path = self.root / ".graphite" / "routing" / "events.sqlite3"

    @property
    def schema_v3_backup_path(self) -> Path:
        return self.path.parent / "backups" / "events-schema-v3.sqlite3"

    @property
    def schema_v3_backup_marker_path(self) -> Path:
        return self.path.parent / "backups" / "events-schema-v3.sha256.json"

    @property
    def schema_v4_backup_path(self) -> Path:
        return self.path.parent / "backups" / "events-schema-v4.sqlite3"

    @property
    def schema_v4_backup_marker_path(self) -> Path:
        return self.path.parent / "backups" / "events-schema-v4.sha256.json"

    def _pre_v6_backup_path(self, source_version: str) -> Path:
        return self.path.parent / "backups" / f"events-schema-v{source_version}-pre-v6.sqlite3"

    def _pre_v6_backup_marker_path(self, source_version: str) -> Path:
        return self.path.parent / "backups" / f"events-schema-v{source_version}-pre-v6.sha256.json"

    def _pre_v7_backup_path(self, source_version: str) -> Path:
        return self.path.parent / "backups" / f"events-schema-v{source_version}-pre-v7.sqlite3"

    def _pre_v7_backup_marker_path(self, source_version: str) -> Path:
        return self.path.parent / "backups" / f"events-schema-v{source_version}-pre-v7.sha256.json"

    def _pre_v8_backup_path(self, source_version: str) -> Path:
        return self.path.parent / "backups" / f"events-schema-v{source_version}-pre-v8.sqlite3"

    def _pre_v8_backup_marker_path(self, source_version: str) -> Path:
        return self.path.parent / "backups" / f"events-schema-v{source_version}-pre-v8.sha256.json"

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

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """End the transaction like `with connection:` does, then actually CLOSE it.

        `sqlite3.Connection.__exit__` commits or rolls back and leaves the handle
        OPEN -- a long-standing trap in that API. `with self._connection() as c:`
        therefore released nothing, and 51 sites across this package used it.
        Measured: 934 `ResourceWarning: unclosed database` in one suite run.

        Refcounting hides it: the local dies when the method returns and CPython
        finalizes immediately, which is why the suite was green. It stops hiding
        the moment anything else holds a frame alive -- a traceback, a debugger,
        a coverage tracer. Under `pytest --cov` the v3->v4 and v4->v5 rollback
        drills failed with `PermissionError: [WinError 32]`: Windows will not
        unlink a file with an open handle, and rollback REPLACES the live
        database. The recovery path was depending on collector timing.

        The methods that manage their own transaction keep using `_connect` with
        an explicit `try/finally: connection.close()` -- they were already
        correct, and wrapping them here would add a second commit they did not
        ask for.
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        _secure_repository_directory(self.root, self.path.parent)
        _validate_database_file(self.path)
        self._migrate_pre_v6_provider_widening()
        self._migrate_v6_to_v7()
        self._migrate_v7_to_v8()
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(_SCHEMA[0])
            existing_version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if existing_version is not None and existing_version[0] not in {
                "1", "2", "3", "4", "5", "6", "7", SCHEMA_VERSION
            }:
                raise StorageError("storage_schema_unsupported")
            if existing_version is not None and existing_version[0] == SCHEMA_VERSION:
                self._validate_v6_schema(connection)
            if existing_version is not None and existing_version[0] == "5":
                self._validate_v5_schema(connection)
            if existing_version is not None and existing_version[0] == "1":
                self._migrate_v1_attempts(connection)
            if existing_version is not None and existing_version[0] in {"1", "2"}:
                self._migrate_v2_digest(connection)
            if existing_version is not None and existing_version[0] == "3":
                self._create_schema_v3_backup()
                self._migrate_v3_cli_cutover(connection)
            if existing_version is not None and existing_version[0] == "4":
                self._validate_v4_schema(connection)
                self._create_schema_v4_backup()
            for statement in _SCHEMA[1:]:
                connection.execute(statement)
            # _migrate_v7_to_v8 only drops this superseded trigger when the
            # on-disk version is exactly "7" at the moment it runs. Stores
            # migrating from v6-or-earlier never pass through that check --
            # _migrate_v6_to_v7 (and the pre-v6 widening) rebuild tables
            # without stamping schema_version, so it stays at the origin
            # version until this transaction's INSERT below. Drop it here,
            # unconditionally and idempotently, so every origin converges on
            # only the v8 guard (which is a strict superset: same protection
            # plus carried-identity acceptance) instead of leaving the two
            # triggers to coexist and the old one to reject valid carries.
            connection.execute(
                "DROP TRIGGER IF EXISTS lifecycle_approval_binding_insert_guard"
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

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            raise StorageError("storage_backup_failed") from exc
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
                raise StorageError("storage_backup_failed")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, path)
            _secure_file(path)
        except StorageError:
            raise
        except OSError as exc:
            raise StorageError("storage_backup_failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _create_schema_v3_backup(self) -> None:
        self._create_schema_backup(
            source_version="3",
            backup=self.schema_v3_backup_path,
            marker=self.schema_v3_backup_marker_path,
        )

    def _create_schema_v4_backup(self) -> None:
        self._create_schema_backup(
            source_version="4",
            backup=self.schema_v4_backup_path,
            marker=self.schema_v4_backup_marker_path,
        )

    def _create_schema_backup(
        self, *, source_version: str, backup: Path, marker: Path
    ) -> None:
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
                    if integrity != ("ok",) or version != (source_version,):
                        raise StorageError("storage_backup_failed")
            _secure_file(temporary)
            os.replace(temporary, backup)
            _secure_file(backup)
            evidence = json.dumps(
                {
                    "backup_sha256": self._file_sha256(backup),
                    "schema_version": source_version,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self._atomic_private_write(marker, evidence)
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError("storage_backup_failed") from exc
        except OSError as exc:
            raise StorageError("storage_backup_failed") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _migrate_v3_cli_cutover(connection: sqlite3.Connection) -> None:
        required = {
            "attempt_id",
            "status",
            "failure_reason",
            "inventory_digest",
        }
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_attempts'"
        ).fetchone()
        if exists is None:
            raise StorageError("storage_migration_quarantined")
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(execution_attempts)")
        }
        if not required <= columns:
            raise StorageError("storage_migration_quarantined")
        connection.execute(
            """UPDATE execution_attempts
            SET status='legacy_unrecoverable', failure_reason='legacy_provider_retired'
            WHERE status IN ('pending', 'persistence_failed')"""
        )

    @staticmethod
    def _validate_v4_schema(connection: sqlite3.Connection) -> None:
        required_tables = {
            "capability_snapshots",
            "task_worktrees",
            "cli_execution_attempts",
            "validation_results",
            "review_links",
        }
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not required_tables <= existing_tables:
            raise StorageError("storage_rollback_required")
        cli_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(cli_execution_attempts)")
        }
        if not {
            "provider",
            "effective_model",
            "capability_snapshot_digest",
            "worktree_id",
            "status",
        } <= cli_columns:
            raise StorageError("storage_rollback_required")
        lifecycle_tables = {
            "lifecycle_snapshot_bindings",
            "lifecycle_approval_bindings",
            "lifecycle_attempt_bindings",
        }
        if lifecycle_tables & existing_tables:
            raise StorageError("storage_rollback_required")

    @staticmethod
    def _validate_v5_schema(connection: sqlite3.Connection) -> None:
        RepositoryStore._validate_v4_core_schema(connection)
        required_tables = {
            "lifecycle_snapshot_bindings",
            "lifecycle_approval_bindings",
            "lifecycle_attempt_bindings",
        }
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not required_tables <= existing_tables:
            raise StorageError("storage_rollback_required")

    @staticmethod
    def _validate_v6_schema(connection: sqlite3.Connection) -> None:
        RepositoryStore._validate_v5_schema(connection)
        for table in _PRE_V6_WIDENED_TABLES:
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if row is None or "'openrouter'" not in str(row[0]):
                raise StorageError("storage_rollback_required")

    def _migrate_pre_v6_provider_widening(self) -> None:
        """Rebuild the two provider-constrained tables to admit 'openrouter'.

        Runs on a dedicated connection before the main initialize transaction
        because both tables are foreign-key parents: the rebuild copies rows
        aside, recreates each table under its original name (so child
        REFERENCES clauses are never rewritten), and restores the rows, with
        foreign-key enforcement off for the duration and a full
        foreign_key_check before commit. Guard triggers dropped with the old
        tables are recreated by the normal schema pass that follows.
        """
        if not self.path.exists():
            return
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1_000,
                isolation_level=None,
            )
            try:
                version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
            except sqlite3.Error:
                return
            if version is None or version[0] not in {"4", "5"}:
                return
            table_sql: dict[str, str] = {}
            for table in _PRE_V6_WIDENED_TABLES:
                row = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if row is None:
                    return
                table_sql[table] = str(row[0])
            if all("'openrouter'" in sql for sql in table_sql.values()):
                return
            self._create_schema_backup(
                source_version=version[0],
                backup=self._pre_v6_backup_path(version[0]),
                marker=self._pre_v6_backup_marker_path(version[0]),
            )
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            for table, (ddl, columns) in _PRE_V6_WIDENED_TABLES.items():
                before = connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                connection.execute(
                    f"CREATE TABLE {table}_pre_v6_copy AS SELECT * FROM {table}"
                )
                connection.execute(f"DROP TABLE {table}")
                connection.execute(ddl)
                connection.execute(
                    f"INSERT INTO {table} ({columns}) "
                    f"SELECT {columns} FROM {table}_pre_v6_copy"
                )
                connection.execute(f"DROP TABLE {table}_pre_v6_copy")
                after = connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                if before != after:
                    raise StorageError("storage_migration_quarantined")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise StorageError("storage_migration_quarantined")
            connection.execute("COMMIT")
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise StorageError("storage_corrupt")
        except StorageError:
            if connection is not None and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise _translate_database_error(exc) from exc
        finally:
            if connection is not None:
                connection.close()

    def _migrate_v6_to_v7(self) -> None:
        """Rebuild provider-CHECK tables to admit 'zai' (schema v6->v7)."""
        if not self.path.exists():
            return
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1_000,
                isolation_level=None,
            )
            try:
                version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
            except sqlite3.Error:
                return
            if version is None or version[0] != "6":
                return
            table_sql: dict[str, str] = {}
            for table in _PRE_V6_WIDENED_TABLES:
                row = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if row is None:
                    return
                table_sql[table] = str(row[0])
            if all("'zai'" in sql for sql in table_sql.values()):
                return
            self._create_schema_backup(
                source_version=version[0],
                backup=self._pre_v7_backup_path(version[0]),
                marker=self._pre_v7_backup_marker_path(version[0]),
            )
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            for table, (ddl, columns) in _PRE_V6_WIDENED_TABLES.items():
                before = connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                connection.execute(
                    f"CREATE TABLE {table}_pre_v7_copy AS SELECT * FROM {table}"
                )
                connection.execute(f"DROP TABLE {table}")
                connection.execute(ddl)
                connection.execute(
                    f"INSERT INTO {table} ({columns}) "
                    f"SELECT {columns} FROM {table}_pre_v7_copy"
                )
                connection.execute(f"DROP TABLE {table}_pre_v7_copy")
                after = connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                if before != after:
                    raise StorageError("storage_migration_quarantined")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise StorageError("storage_migration_quarantined")
            connection.execute("COMMIT")
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise StorageError("storage_corrupt")
        except StorageError:
            if connection is not None and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise _translate_database_error(exc) from exc
        finally:
            if connection is not None:
                connection.close()

    def _migrate_v7_to_v8(self) -> None:
        """Add the carry table era: swap the approval insert guard (schema v7->v8)."""
        if not self.path.exists():
            return
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1_000,
                isolation_level=None,
            )
            try:
                version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
            except sqlite3.Error:
                return
            if version is None or version[0] != "7":
                return
            self._create_schema_backup(
                source_version=version[0],
                backup=self._pre_v8_backup_path(version[0]),
                marker=self._pre_v8_backup_marker_path(version[0]),
            )
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise _translate_database_error(exc) from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _validate_v4_core_schema(connection: sqlite3.Connection) -> None:
        required_tables = {
            "capability_snapshots",
            "task_worktrees",
            "cli_execution_attempts",
            "validation_results",
            "review_links",
        }
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not required_tables <= existing_tables:
            raise StorageError("storage_rollback_required")
        cli_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(cli_execution_attempts)")
        }
        if not {
            "provider",
            "effective_model",
            "capability_snapshot_digest",
            "worktree_id",
            "status",
        } <= cli_columns:
            raise StorageError("storage_rollback_required")

    @staticmethod
    def _migrate_v1_attempts(connection: sqlite3.Connection) -> None:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_attempts'"
        ).fetchone()
        if exists is None:
            return
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(execution_attempts)")
        }
        if {"max_input_tokens", "max_output_tokens", "expected_prompt_hash"} <= columns:
            return
        connection.execute("ALTER TABLE execution_attempts RENAME TO execution_attempts_v1")
        connection.execute(
            """CREATE TABLE execution_attempts (
                attempt_id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL UNIQUE REFERENCES approvals(approval_id) ON DELETE RESTRICT,
                task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
                decision_id TEXT NOT NULL REFERENCES decisions(decision_id) ON DELETE RESTRICT,
                manifest_hash TEXT NOT NULL,
                graph_fingerprint TEXT NOT NULL,
                model_id TEXT NOT NULL,
                effort TEXT NOT NULL,
                reserved_tokens INTEGER NOT NULL,
                max_input_tokens INTEGER,
                max_output_tokens INTEGER,
                expected_prompt_hash TEXT,
                status TEXT NOT NULL CHECK(status IN (
                    'pending', 'completed', 'failed', 'persistence_failed', 'legacy_unrecoverable'
                )),
                failure_reason TEXT,
                execution_id TEXT UNIQUE REFERENCES executions(execution_id) ON DELETE RESTRICT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                CHECK(
                    status IN ('completed', 'failed', 'legacy_unrecoverable')
                    OR (
                        max_input_tokens IS NOT NULL AND max_input_tokens > 0
                        AND max_output_tokens IS NOT NULL AND max_output_tokens > 0
                        AND expected_prompt_hash IS NOT NULL
                    )
                )
            )"""
        )
        connection.execute(
            """INSERT INTO execution_attempts(
                attempt_id, approval_id, task_id, decision_id, manifest_hash,
                graph_fingerprint, model_id, effort, reserved_tokens,
                max_input_tokens, max_output_tokens, expected_prompt_hash,
                status, failure_reason, execution_id, created_at, updated_at
            )
            SELECT attempt_id, approval_id, task_id, decision_id, manifest_hash,
                graph_fingerprint, model_id, effort, reserved_tokens,
                NULL, NULL, NULL,
                CASE WHEN status IN ('pending', 'persistence_failed')
                    THEN 'legacy_unrecoverable' ELSE status END,
                CASE WHEN status IN ('pending', 'persistence_failed')
                    THEN 'legacy_attempt_bindings_missing' ELSE failure_reason END,
                execution_id, created_at, updated_at
            FROM execution_attempts_v1"""
        )
        connection.execute("DROP TABLE execution_attempts_v1")

    @staticmethod
    def _migrate_v2_digest(connection: sqlite3.Connection) -> None:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_attempts'"
        ).fetchone()
        if exists is None:
            return
        attempt_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(execution_attempts)")
        }
        if "inventory_digest" not in attempt_columns:
            connection.execute("ALTER TABLE execution_attempts ADD COLUMN inventory_digest TEXT")
        connection.execute(
            """UPDATE execution_attempts
            SET status = 'legacy_unrecoverable',
                failure_reason = 'legacy_attempt_digest_missing'
            WHERE status IN ('pending', 'persistence_failed')
              AND inventory_digest IS NULL"""
        )
        staged_exists = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='staged_execution_receipts'"
        ).fetchone()
        if staged_exists is not None:
            staged_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(staged_execution_receipts)")
            }
            if "inventory_digest" not in staged_columns:
                connection.execute(
                    "ALTER TABLE staged_execution_receipts ADD COLUMN inventory_digest TEXT"
                )

    def integrity_check(self) -> str:
        try:
            with self._connection() as connection:
                row = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
        if row is None or row[0] != "ok":
            raise StorageError("storage_corrupt")
        return "ok"

    def pragma_state(self) -> dict[str, int | str]:
        with self._connection() as connection:
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
            with self._connection() as connection:
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
            with self._connection() as connection:
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
            with self._connection() as connection:
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
            with self._connection() as connection:
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
        with self._connection() as connection:
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
        inventory_digest: str,
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
            inventory_digest,
            _nonnegative_integer(created_at, "created_at_invalid"),
        )
        if not _HEX_64.fullmatch(manifest_hash):
            raise ValueError("manifest_hash_invalid")
        if not _HEX_64.fullmatch(graph_fingerprint):
            raise ValueError("graph_fingerprint_invalid")
        if not _HEX_64.fullmatch(expected_prompt_hash):
            raise ValueError("prompt_hash_invalid")
        if not isinstance(inventory_digest, str) or not _HEX_64.fullmatch(inventory_digest):
            raise ValueError("inventory_digest_invalid")
        if values[9] < 1 or values[10] < 1 or values[9] + values[10] != values[8]:
            raise ValueError("token_bounds_invalid")
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT attempt_id, task_id, decision_id, manifest_hash, "
                "graph_fingerprint, model_id, effort, reserved_tokens, max_input_tokens, "
                "max_output_tokens, expected_prompt_hash, inventory_digest "
                "FROM execution_attempts WHERE approval_id = ?",
                (values[1],),
            ).fetchone()
            if existing is not None:
                expected = (values[0], *values[2:13])
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
                    max_input_tokens, max_output_tokens, expected_prompt_hash,
                    inventory_digest, status,
                    failure_reason, execution_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'pending', NULL, NULL, ?, ?)""",
                (*values[:13], values[13], values[13]),
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
            with self._connection() as connection:
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
        inventory_digest: str,
        completed_at: int,
    ) -> bool:
        attempt_id = _identifier(attempt_id, "attempt_id_invalid")
        self._validate_receipt_shape(receipt)
        if not isinstance(inventory_digest, str) or not _HEX_64.fullmatch(inventory_digest):
            raise ValueError("inventory_digest_invalid")
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
            if attempt["status"] == "legacy_unrecoverable":
                raise StorageError(str(attempt["failure_reason"]))
            if attempt["inventory_digest"] != inventory_digest:
                raise StorageError("execution_attempt_conflict")
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
        inventory_digest: str,
        staged_at: int,
    ) -> bool:
        attempt_id = _identifier(attempt_id, "attempt_id_invalid")
        self._validate_receipt_shape(receipt)
        if not isinstance(inventory_digest, str) or not _HEX_64.fullmatch(inventory_digest):
            raise ValueError("inventory_digest_invalid")
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
            if attempt["status"] == "legacy_unrecoverable":
                raise StorageError(str(attempt["failure_reason"]))
            if attempt["inventory_digest"] != inventory_digest:
                raise StorageError("execution_attempt_conflict")
            self._assert_approval_consumed(connection, attempt)
            if attempt["status"] not in {"pending", "persistence_failed"}:
                raise StorageError("execution_attempt_conflict")
            self._validate_attempt_binding(attempt, receipt)
            existing = connection.execute(
                "SELECT * FROM staged_execution_receipts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["inventory_digest"] != inventory_digest
                    or self._receipt_tuple(self._receipt_from_row(existing))
                    != self._receipt_tuple(receipt)
                ):
                    raise StorageError("execution_attempt_conflict")
                connection.rollback()
                return False
            connection.execute(
                """INSERT INTO staged_execution_receipts(
                    attempt_id, execution_id, approval_id, model_id, effort, outcome,
                    input_tokens, output_tokens, latency_ms, prompt_hash, response_hash,
                    inventory_digest, failure_reason, staged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    attempt_id, receipt.execution_id,
                    *self._receipt_tuple(receipt)[:-1], inventory_digest, staged_at,
                ),
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

    def recoverable_attempts(
        self,
        *,
        limit: int = DEFAULT_RECOVERY_PAGE_SIZE,
        after: str | None = None,
    ) -> RecoverableAttemptPage:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RECOVERY_PAGE_SIZE
        ):
            raise ValueError("recovery_limit_invalid")
        if after is not None:
            after = _identifier(after, "recovery_cursor_invalid")
        query = """SELECT a.attempt_id FROM execution_attempts a
            JOIN staged_execution_receipts s ON s.attempt_id = a.attempt_id
            WHERE a.status = 'persistence_failed'"""
        parameters: list[Any] = []
        if after is not None:
            query += " AND a.attempt_id > ?"
            parameters.append(after)
        query += " ORDER BY a.attempt_id LIMIT ?"
        parameters.append(limit + 1)
        try:
            with self._connection() as connection:
                rows = connection.execute(query, parameters).fetchall()
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
        try:
            validated = tuple(_identifier(row[0], "storage_corrupt") for row in rows)
        except ValueError as exc:
            raise StorageError("storage_corrupt") from exc
        has_more = len(validated) > limit
        attempt_ids = validated[:limit]
        return RecoverableAttemptPage(
            attempt_ids=attempt_ids,
            next_cursor=attempt_ids[-1] if has_more else None,
            has_more=has_more,
        )

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
            if attempt["status"] == "legacy_unrecoverable":
                raise StorageError(str(attempt["failure_reason"]))
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
            if staged["inventory_digest"] != attempt["inventory_digest"]:
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
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def outcome_evidence_rows(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
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
            with self._connection() as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO incident_reviews(execution_id, reviewed_at) VALUES (?, ?)",
                    (execution_id, reviewed_at),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def latest_incident_review(self) -> int | None:
        with self._connection() as connection:
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
            with self._connection() as connection:
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
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE blind_comparisons SET verdict = ?, recorded_at = ?
                WHERE comparison_id = ? AND verdict IS NULL""",
                (verdict, recorded_at, comparison_id),
            )
        return cursor.rowcount == 1

    def blind_comparison(self, comparison_id: str) -> dict[str, Any] | None:
        comparison_id = _identifier(comparison_id, "comparison_id_invalid")
        with self._connection() as connection:
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
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT model_id, effort, category, risk, sample_count,
                    success_count, severe_failure_count
                FROM confidence_stats
                ORDER BY model_id, effort, category, risk"""
            ).fetchall()
        return [dict(row) for row in rows]

    def create_task_worktree_record(
        self,
        *,
        worktree_id: str,
        task_id: str,
        baseline_commit: str,
        canonical_root_hash: str,
        created_at: int,
    ) -> bool:
        values = (
            _identifier(worktree_id, "worktree_id_invalid"),
            _identifier(task_id, "task_id_invalid"),
            baseline_commit,
            canonical_root_hash,
            _nonnegative_integer(created_at, "created_at_invalid"),
        )
        if not _GIT_OBJECT.fullmatch(baseline_commit):
            raise ValueError("baseline_commit_invalid")
        if not _HEX_64.fullmatch(canonical_root_hash):
            raise ValueError("canonical_root_hash_invalid")
        try:
            with self._connection() as connection:
                existing = connection.execute(
                    "SELECT worktree_id,task_id,baseline_commit,canonical_root_hash,created_at "
                    "FROM task_worktrees WHERE worktree_id=?",
                    (values[0],),
                ).fetchone()
                expected = (*values,)
                if existing is not None:
                    if tuple(existing) != expected:
                        raise StorageError("worktree_conflict")
                    return False
                connection.execute(
                    "INSERT INTO task_worktrees(worktree_id,task_id,baseline_commit,"
                    "canonical_root_hash,status,created_at,updated_at) "
                    "VALUES (?,?,?,?,'prepared',?,?)",
                    (*values, values[-1]),
                )
                return True
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def transition_task_worktree(
        self, worktree_id: str, *, expected_status: str, new_status: str, updated_at: int
    ) -> bool:
        worktree_id = _identifier(worktree_id, "worktree_id_invalid")
        allowed = {"prepared", "executed", "accepted", "rejected", "quarantined", "cleaned"}
        if expected_status not in allowed or new_status not in allowed:
            raise ValueError("worktree_status_invalid")
        updated_at = _nonnegative_integer(updated_at, "updated_at_invalid")
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    "UPDATE task_worktrees SET status=?,updated_at=? "
                    "WHERE worktree_id=? AND status=?",
                    (new_status, updated_at, worktree_id, expected_status),
                )
                if cursor.rowcount == 1:
                    return True
                row = connection.execute(
                    "SELECT status FROM task_worktrees WHERE worktree_id=?", (worktree_id,)
                ).fetchone()
                if row is None:
                    raise StorageError("worktree_missing")
                if str(row[0]) == new_status:
                    return False
                raise StorageError("worktree_transition_invalid")
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def task_worktree_record(self, worktree_id: str) -> dict[str, Any] | None:
        worktree_id = _identifier(worktree_id, "worktree_id_invalid")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM task_worktrees WHERE worktree_id=?", (worktree_id,)
            ).fetchone()
        return None if row is None else dict(row)

    def latest_task_worktree_record(self, task_id: str) -> dict[str, Any] | None:
        task_id = _identifier(task_id, "task_id_invalid")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM task_worktrees WHERE task_id=? "
                "ORDER BY created_at DESC,worktree_id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def worktree_validation_record(self, worktree_id: str) -> dict[str, Any] | None:
        worktree_id = _identifier(worktree_id, "worktree_id_invalid")
        with self._connection() as connection:
            row = connection.execute(
                """SELECT v.*,a.attempt_id,a.execution_id,a.status AS attempt_status,
                    a.provider,a.task_id,a.decision_id,a.worktree_id,t.risk,t.category
                FROM cli_execution_attempts a
                JOIN validation_results v ON v.attempt_id=a.attempt_id
                JOIN tasks t ON t.task_id=a.task_id
                WHERE a.worktree_id=?
                ORDER BY a.created_at DESC,a.attempt_id DESC LIMIT 1""",
                (worktree_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def create_review_link_record(
        self,
        *,
        review_attempt_id: str,
        primary_attempt_id: str,
        primary_diff_hash: str,
        created_at: int,
    ) -> bool:
        values = (
            _identifier(review_attempt_id, "review_attempt_id_invalid"),
            _identifier(primary_attempt_id, "primary_attempt_id_invalid"),
            primary_diff_hash,
            _nonnegative_integer(created_at, "created_at_invalid"),
        )
        if values[0] == values[1]:
            raise ValueError("review_attempt_not_independent")
        if not _HEX_64.fullmatch(primary_diff_hash):
            raise ValueError("primary_diff_hash_invalid")
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO review_links VALUES (?,?,?,?)", values
                )
                if cursor.rowcount == 1:
                    return True
                row = connection.execute(
                    "SELECT review_attempt_id,primary_attempt_id,primary_diff_hash,created_at "
                    "FROM review_links WHERE review_attempt_id=?",
                    (values[0],),
                ).fetchone()
                if tuple(row or ()) == values:
                    return False
                raise StorageError("review_link_conflict")
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def create_cli_execution_attempt_record(
        self,
        *,
        attempt_id: str,
        approval_id: str,
        task_id: str,
        decision_id: str,
        worktree_id: str,
        provider: str,
        requested_model: str,
        effective_model: str,
        effort: str,
        cli_version: str,
        executable_sha256: str,
        capability_snapshot_digest: str,
        manifest_hash: str,
        expected_prompt_hash: str,
        reserved_tokens: int,
        created_at: int,
    ) -> bool:
        values = (
            _identifier(attempt_id, "attempt_id_invalid"),
            _identifier(approval_id, "approval_id_invalid"),
            _identifier(task_id, "task_id_invalid"),
            _identifier(decision_id, "decision_id_invalid"),
            _identifier(worktree_id, "worktree_id_invalid"),
            ProviderId(provider).value,
            _model_identifier(requested_model, "requested_model_invalid"),
            _model_identifier(effective_model, "effective_model_invalid"),
            Effort(effort).value,
            _semantic_version(cli_version, "cli_version_invalid"),
            executable_sha256,
            capability_snapshot_digest,
            manifest_hash,
            expected_prompt_hash,
            _positive_integer(reserved_tokens, "reserved_tokens_invalid"),
            _nonnegative_integer(created_at, "created_at_invalid"),
        )
        for value, code in (
            (executable_sha256, "executable_sha256_invalid"),
            (capability_snapshot_digest, "capability_snapshot_digest_invalid"),
            (manifest_hash, "manifest_hash_invalid"),
            (expected_prompt_hash, "prompt_hash_invalid"),
        ):
            if not _HEX_64.fullmatch(value):
                raise ValueError(code)
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO cli_execution_attempts(
                        attempt_id,approval_id,task_id,decision_id,worktree_id,provider,
                        requested_model,effective_model,effort,cli_version,executable_sha256,
                        capability_snapshot_digest,manifest_hash,expected_prompt_hash,
                        reserved_tokens,status,failure_reason,execution_id,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,?,?)""",
                    (*values, values[-1]),
                )
                if cursor.rowcount == 1:
                    return True
                raise StorageError("cli_attempt_conflict")
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def mark_cli_execution_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        failure_reason: str | None,
        execution_id: str | None = None,
        updated_at: int,
    ) -> bool:
        attempt_id = _identifier(attempt_id, "attempt_id_invalid")
        if status not in {"completed", "failed", "persistence_failed", "quarantined"}:
            raise ValueError("cli_attempt_status_invalid")
        if failure_reason is not None:
            failure_reason = _identifier(failure_reason, "failure_reason_invalid")
        if execution_id is not None:
            execution_id = _identifier(execution_id, "execution_id_invalid")
        updated_at = _nonnegative_integer(updated_at, "updated_at_invalid")
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE cli_execution_attempts SET status=?,failure_reason=?,execution_id=?,"
                "updated_at=? WHERE attempt_id=? AND status IN ('pending','persistence_failed')",
                (status, failure_reason, execution_id, updated_at, attempt_id),
            )
            if cursor.rowcount == 1:
                return True
            row = connection.execute(
                "SELECT status,failure_reason,execution_id FROM cli_execution_attempts "
                "WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise StorageError("cli_attempt_missing")
            if tuple(row) == (status, failure_reason, execution_id):
                return False
            raise StorageError("cli_attempt_transition_invalid")

    def record_validation_result(
        self,
        *,
        attempt_id: str,
        diff_hash: str,
        changed_file_count: int,
        changed_byte_count: int,
        outcome: str,
        recorded_at: int,
    ) -> bool:
        values = (
            _identifier(attempt_id, "attempt_id_invalid"),
            diff_hash,
            _nonnegative_integer(changed_file_count, "changed_file_count_invalid"),
            _nonnegative_integer(changed_byte_count, "changed_byte_count_invalid"),
            outcome,
            _nonnegative_integer(recorded_at, "recorded_at_invalid"),
        )
        if not _HEX_64.fullmatch(diff_hash):
            raise ValueError("diff_hash_invalid")
        if outcome not in {"passed", "failed", "blocked", "not_run"}:
            raise ValueError("validation_outcome_invalid")
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO validation_results VALUES (?,?,?,?,?,?)", values
                )
                if cursor.rowcount == 1:
                    return True
                row = connection.execute(
                    "SELECT attempt_id,diff_hash,changed_file_count,changed_byte_count,"
                    "outcome,recorded_at FROM validation_results WHERE attempt_id=?",
                    (values[0],),
                ).fetchone()
                if tuple(row or ()) == values:
                    return False
                raise StorageError("validation_result_conflict")
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def finalize_cli_execution(
        self,
        *,
        attempt_id: str,
        receipt: ExecutionReceipt,
        graph_fingerprint: str,
        completed_at: int,
    ) -> bool:
        attempt_id = _identifier(attempt_id, "attempt_id_invalid")
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
        if not _HEX_64.fullmatch(graph_fingerprint):
            raise ValueError("graph_fingerprint_invalid")
        completed_at = _nonnegative_integer(completed_at, "completed_at_invalid")
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                "SELECT * FROM cli_execution_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise StorageError("cli_attempt_missing")
            if attempt["status"] == "completed":
                if attempt["execution_id"] != receipt.execution_id:
                    raise StorageError("cli_attempt_conflict")
                connection.rollback()
                return False
            if attempt["status"] != "pending":
                raise StorageError("cli_attempt_transition_invalid")
            approval = connection.execute(
                "SELECT status,manifest_hash,reserved_tokens FROM approvals WHERE approval_id=?",
                (attempt["approval_id"],),
            ).fetchone()
            if (
                approval is None
                or approval["status"] != "consumed"
                or approval["manifest_hash"] != attempt["manifest_hash"]
                or approval["reserved_tokens"] != attempt["reserved_tokens"]
                or receipt.approval_id != attempt["approval_id"]
                or receipt.model_id != attempt["requested_model"]
                or receipt.effective_model != attempt["effective_model"]
                or receipt.effort.value != attempt["effort"]
                or receipt.cli_version != attempt["cli_version"]
                or receipt.prompt_hash != attempt["expected_prompt_hash"]
                or receipt.input_tokens + receipt.output_tokens > attempt["reserved_tokens"]
            ):
                raise StorageError("cli_attempt_conflict")
            connection.execute(
                """INSERT INTO executions(
                    execution_id,idempotency_key,task_id,decision_id,approval_id,
                    model_id,effort,status,reserved_tokens,actual_input_tokens,
                    actual_output_tokens,created_at,completed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    receipt.execution_id,
                    attempt_id,
                    attempt["task_id"],
                    attempt["decision_id"],
                    receipt.approval_id,
                    receipt.model_id,
                    receipt.effort.value,
                    receipt.outcome.value,
                    attempt["reserved_tokens"],
                    receipt.input_tokens,
                    receipt.output_tokens,
                    attempt["created_at"],
                    completed_at,
                ),
            )
            connection.execute(
                """INSERT INTO execution_receipts(
                    execution_id,approval_id,model_id,effort,outcome,input_tokens,
                    output_tokens,latency_ms,prompt_hash,response_hash,failure_reason,completed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                (
                    receipt.execution_id,
                    receipt.approval_id,
                    receipt.model_id,
                    receipt.effort.value,
                    receipt.outcome.value,
                    receipt.input_tokens,
                    receipt.output_tokens,
                    receipt.latency_ms,
                    receipt.prompt_hash,
                    receipt.response_hash,
                    completed_at,
                ),
            )
            connection.execute(
                "INSERT INTO execution_evidence VALUES (?,?,?,?)",
                (
                    receipt.execution_id,
                    attempt["task_id"],
                    attempt["decision_id"],
                    graph_fingerprint,
                ),
            )
            connection.execute(
                "UPDATE cli_execution_attempts SET status='completed',failure_reason=NULL,"
                "execution_id=?,updated_at=? WHERE attempt_id=?",
                (receipt.execution_id, completed_at, attempt_id),
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
            raise StorageError("cli_attempt_conflict") from exc
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise _translate_database_error(exc) from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _cli_telemetry_values(
        payload: dict[str, Any],
    ) -> tuple[str, str, int]:
        if not isinstance(payload, dict) or set(payload) != _CLI_TELEMETRY_KEYS:
            raise ValueError("cli_telemetry_payload_invalid")
        try:
            ProviderId(payload["provider"])
            Effort(payload["effort"])
            TaskCategory(payload["category"])
            RiskTier(payload["risk"])
            EvidenceProvenance(payload["provenance"])
            _model_identifier(payload["requested_model"], "cli_telemetry_payload_invalid")
            _model_identifier(payload["effective_model"], "cli_telemetry_payload_invalid")
        except (TypeError, ValueError) as exc:
            raise ValueError("cli_telemetry_payload_invalid") from exc
        if (
            not isinstance(payload["capability_snapshot_digest"], str)
            or _HEX_64.fullmatch(payload["capability_snapshot_digest"]) is None
            or payload["cost_status"] != "unknown"
            or payload["validation_outcome"] not in {"passed", "failed", "blocked", "not_run"}
            or payload["human_verdict"] not in {None, "accepted", "rejected", "rework"}
            or (
                payload["diff_sha256"] is not None
                and (
                    not isinstance(payload["diff_sha256"], str)
                    or _HEX_64.fullmatch(payload["diff_sha256"]) is None
                )
            )
        ):
            raise ValueError("cli_telemetry_payload_invalid")
        defects = payload["review_defect_classes"]
        allowed_defects = {
            "correctness", "security", "reliability", "maintainability",
            "performance", "test_coverage", "requirement_miss",
        }
        if (
            not isinstance(defects, list)
            or any(not isinstance(item, str) for item in defects)
            or len(defects) != len(set(defects))
            or not set(defects) <= allowed_defects
        ):
            raise ValueError("cli_telemetry_payload_invalid")
        for name, maximum, optional in (
            ("latency_ms", 86_400_000, True),
            ("input_tokens", 100_000_000, True),
            ("output_tokens", 100_000_000, True),
            ("changed_file_count", 100_000, False),
            ("changed_byte_count", 10**12, False),
            ("rework_count", 1_000, False),
            ("observed_at", 10**12, False),
        ):
            value = payload[name]
            if optional and value is None:
                continue
            _nonnegative_integer(value, "cli_telemetry_payload_invalid", maximum)
        try:
            canonical = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("cli_telemetry_payload_invalid") from exc
        if len(canonical.encode("utf-8")) > 8192:
            raise ValueError("cli_telemetry_payload_invalid")
        observed_at = _nonnegative_integer(payload["observed_at"], "observed_at_invalid")
        event_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return event_hash, canonical, observed_at

    def record_cli_telemetry(self, payload: dict[str, Any]) -> bool:
        """Persist one immutable, allowlisted telemetry event."""
        event_hash, canonical, observed_at = self._cli_telemetry_values(payload)
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO cli_telemetry_events VALUES (?,?,?)",
                    (event_hash, canonical, observed_at),
                )
                if cursor.rowcount == 1:
                    return True
                row = connection.execute(
                    "SELECT payload_json,observed_at FROM cli_telemetry_events "
                    "WHERE event_hash=?", (event_hash,),
                ).fetchone()
                if tuple(row or ()) == (canonical, observed_at):
                    return False
                raise StorageError("cli_telemetry_conflict")
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def promote_bound_capability_snapshot_record(
        self,
        *,
        source_snapshot_digest: str,
        snapshot: CapabilitySnapshot,
        lifecycle_identity_digest: str,
        bound_at: int,
        telemetry_payload: dict[str, Any],
    ) -> bool:
        """Atomically persist one validated edit-smoke promotion and its audit event."""
        if not _HEX_64.fullmatch(source_snapshot_digest):
            raise ValueError("source_snapshot_digest_invalid")
        if not isinstance(snapshot, CapabilitySnapshot) or (
            snapshot.profile.permission_mode is not PermissionMode.WORKSPACE_WRITE
        ):
            raise ValueError("capability_snapshot_invalid")
        if not _HEX_64.fullmatch(lifecycle_identity_digest):
            raise ValueError("lifecycle_identity_digest_invalid")
        bound_at = _nonnegative_integer(bound_at, "bound_at_invalid")
        event_hash, canonical, observed_at = self._cli_telemetry_values(
            telemetry_payload
        )
        if (
            telemetry_payload["capability_snapshot_digest"] != snapshot.digest
            or observed_at != bound_at
        ):
            raise ValueError("cli_telemetry_payload_invalid")
        payload_json = json.dumps(
            snapshot.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        if (
            len(snapshot.profile.supported_efforts) != 1
            or len(payload_json.encode("utf-8")) > 64 * 1024
        ):
            raise ValueError("capability_snapshot_invalid")
        values = (
            snapshot.digest,
            snapshot.profile.provider.value,
            snapshot.profile.requested_model,
            snapshot.profile.effective_model,
            snapshot.profile.supported_efforts[0].value,
            snapshot.identity.executable_sha256,
            snapshot.identity.cli_version,
            snapshot.identity.adapter_protocol_version,
            snapshot.profile.permission_mode.value,
            payload_json,
            snapshot.verified_at,
            snapshot.expires_at,
        )
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            source = connection.execute(
                "SELECT provider,requested_model,effective_model,effort,"
                "executable_sha256,cli_version,adapter_protocol_version,"
                "permission_mode,expires_at FROM capability_snapshots "
                "WHERE capability_snapshot_digest=?",
                (source_snapshot_digest,),
            ).fetchone()
            binding = connection.execute(
                "SELECT lifecycle_identity_digest FROM lifecycle_snapshot_bindings "
                "WHERE capability_snapshot_digest=?",
                (source_snapshot_digest,),
            ).fetchone()
            expected_source = (
                snapshot.profile.provider.value,
                snapshot.profile.requested_model,
                snapshot.profile.effective_model,
                snapshot.profile.supported_efforts[0].value,
                snapshot.identity.executable_sha256,
                snapshot.identity.cli_version,
                snapshot.identity.adapter_protocol_version,
            )
            if (
                source is None
                or tuple(source[:7]) != expected_source
                or source[7] != PermissionMode.READ_ONLY.value
                or int(source[8]) <= bound_at
                or binding is None
                or binding[0] != lifecycle_identity_digest
            ):
                raise StorageError("capability_promotion_source_invalid")
            existing = connection.execute(
                "SELECT capability_snapshot_digest,provider,requested_model,"
                "effective_model,effort,executable_sha256,cli_version,"
                "adapter_protocol_version,permission_mode,payload_json,verified_at,"
                "expires_at FROM capability_snapshots "
                "WHERE capability_snapshot_digest=?",
                (snapshot.digest,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise StorageError("storage_corrupt")
                existing_binding = connection.execute(
                    "SELECT lifecycle_identity_digest,bound_at "
                    "FROM lifecycle_snapshot_bindings "
                    "WHERE capability_snapshot_digest=?",
                    (snapshot.digest,),
                ).fetchone()
                existing_event = connection.execute(
                    "SELECT payload_json,observed_at FROM cli_telemetry_events "
                    "WHERE event_hash=?",
                    (event_hash,),
                ).fetchone()
                if tuple(existing_binding or ()) != (
                    lifecycle_identity_digest,
                    bound_at,
                ) or tuple(existing_event or ()) != (canonical, observed_at):
                    raise StorageError("capability_promotion_conflict")
                connection.rollback()
                return False
            connection.execute(
                """INSERT INTO capability_snapshots(
                    capability_snapshot_digest,provider,requested_model,effective_model,
                    effort,executable_sha256,cli_version,adapter_protocol_version,
                    permission_mode,payload_json,verified_at,expires_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
            self._save_lifecycle_binding(
                connection,
                table="lifecycle_snapshot_bindings",
                key_column="capability_snapshot_digest",
                key_value=snapshot.digest,
                lifecycle_identity_digest=lifecycle_identity_digest,
                bound_at=bound_at,
            )
            connection.execute(
                "INSERT INTO cli_telemetry_events VALUES (?,?,?)",
                (event_hash, canonical, observed_at),
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

    def cli_telemetry_records(self, *, limit: int = 1000) -> tuple[dict[str, Any], ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("cli_telemetry_limit_invalid")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM cli_telemetry_events "
                "ORDER BY observed_at DESC,event_hash DESC LIMIT ?", (limit,),
            ).fetchall()
        try:
            return tuple(json.loads(str(row[0])) for row in rows)
        except (TypeError, ValueError) as exc:
            raise StorageError("storage_corrupt") from exc

    @staticmethod
    def _policy_signature_payload(
        policy_version: str,
        parent_version: str | None,
        payload_hash: str,
        evidence_hash: str,
    ) -> bytes:
        return json.dumps(
            {
                "evidence_hash": evidence_hash,
                "parent_version": parent_version,
                "payload_hash": payload_hash,
                "policy_version": policy_version,
            },
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")

    def record_cli_policy_candidate(
        self,
        *,
        policy_version: str,
        parent_version: str | None,
        payload: dict[str, Any],
        evidence_hash: str,
        signature: str,
        signing_key: bytes,
        created_at: int,
    ) -> bool:
        """Record a signed candidate; recording never activates it."""
        policy_version = _identifier(policy_version, "policy_version_invalid")
        if parent_version is not None:
            parent_version = _identifier(parent_version, "parent_policy_version_invalid")
        if not isinstance(payload, dict) or set(payload) != _CLI_POLICY_KEYS:
            raise ValueError("cli_policy_payload_invalid")
        if (
            payload["schema_version"] != 1
            or payload["autonomy"] is not False
            or payload["allowed_providers"] != ["claude-code", "codex"]
            or payload["permission_ceiling"] != "workspace-write"
            or payload["risk_ceilings"] != {"claude-code": "medium", "codex": "medium"}
            or payload["source_policy_version"] != "3"
        ):
            raise ValueError("cli_policy_guardrail_invalid")
        weights = payload["scoring_weights"]
        if (
            not isinstance(weights, dict)
            or not weights
            or not set(weights) <= {
                "reliability", "tokens", "latency", "scarcity",
                "effort", "confidence", "recency",
            }
        ):
            raise ValueError("cli_policy_payload_invalid")
        for value in weights.values():
            _nonnegative_integer(value, "cli_policy_payload_invalid", 1_000)
        minimum_samples = _nonnegative_integer(
            payload["minimum_samples"], "cli_policy_payload_invalid", 10_000
        )
        half_life = _nonnegative_integer(
            payload["recency_half_life_days"], "cli_policy_payload_invalid", 365
        )
        if minimum_samples < 20 or half_life < 7:
            raise ValueError("cli_policy_payload_invalid")
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ValueError("policy_signing_key_invalid")
        if not isinstance(evidence_hash, str) or not _HEX_64.fullmatch(evidence_hash):
            raise ValueError("evidence_hash_invalid")
        if not isinstance(signature, str) or not _HEX_64.fullmatch(signature):
            raise ValueError("policy_signature_invalid")
        created_at = _nonnegative_integer(created_at, "created_at_invalid")
        try:
            canonical = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("cli_policy_payload_invalid") from exc
        if len(canonical.encode("utf-8")) > 16384:
            raise ValueError("cli_policy_payload_invalid")
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        expected = hmac.new(
            signing_key,
            self._policy_signature_payload(
                policy_version, parent_version, payload_hash, evidence_hash
            ),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("policy_signature_invalid")
        values = (
            policy_version, parent_version, canonical, payload_hash,
            signature, evidence_hash, created_at,
        )
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO cli_policy_versions VALUES (?,?,?,?,?,?,?)", values
                )
                if cursor.rowcount != 1:
                    row = connection.execute(
                        "SELECT policy_version,parent_version,payload_json,payload_hash,"
                        "signature,evidence_hash,created_at FROM cli_policy_versions "
                        "WHERE policy_version=?", (policy_version,),
                    ).fetchone()
                    if tuple(row or ()) == values:
                        connection.rollback()
                        return False
                    raise StorageError("cli_policy_version_conflict")
                connection.execute(
                    "INSERT INTO cli_policy_events(policy_version,action,prior_version,actor,created_at) "
                    "VALUES (?,'candidate',?,'human',?)",
                    (policy_version, parent_version, created_at),
                )
                connection.commit()
                return True
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    @staticmethod
    def _active_cli_policy(connection: sqlite3.Connection) -> str | None:
        row = connection.execute(
            "SELECT policy_version FROM cli_policy_events "
            "WHERE action IN ('promote','rollback') ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else str(row[0])

    def activate_cli_policy(
        self,
        policy_version: str,
        *,
        action: str,
        authority_granted: bool,
        created_at: int,
    ) -> bool:
        """Append a human promotion or rollback without rewriting evidence."""
        policy_version = _identifier(policy_version, "policy_version_invalid")
        if action not in {"promote", "rollback"}:
            raise ValueError("policy_action_invalid")
        if authority_granted is not True:
            raise ValueError("human_authority_required")
        created_at = _nonnegative_integer(created_at, "created_at_invalid")
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                exists = connection.execute(
                    "SELECT 1 FROM cli_policy_versions WHERE policy_version=?",
                    (policy_version,),
                ).fetchone()
                if exists is None:
                    raise StorageError("cli_policy_version_missing")
                prior = self._active_cli_policy(connection)
                if prior == policy_version:
                    connection.rollback()
                    return False
                if action == "rollback":
                    previously_active = connection.execute(
                        "SELECT 1 FROM cli_policy_events WHERE policy_version=? "
                        "AND action IN ('promote','rollback') LIMIT 1", (policy_version,),
                    ).fetchone()
                    if previously_active is None:
                        raise StorageError("cli_policy_rollback_target_invalid")
                connection.execute(
                    "INSERT INTO cli_policy_events(policy_version,action,prior_version,actor,created_at) "
                    "VALUES (?,?,?,'human',?)",
                    (policy_version, action, prior, created_at),
                )
                connection.commit()
                return True
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def active_cli_policy_version(self) -> str | None:
        with self._connection() as connection:
            return self._active_cli_policy(connection)

    def cli_policy_event_records(self) -> tuple[dict[str, Any], ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT policy_version,action,prior_version,actor,created_at "
                "FROM cli_policy_events ORDER BY event_id"
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def row_count(self, table: str) -> int:
        if table not in _TABLES:
            raise ValueError("table_invalid")
        with self._connection() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def save_capability_snapshot_record(self, snapshot: CapabilitySnapshot) -> bool:
        if not isinstance(snapshot, CapabilitySnapshot):
            raise ValueError("capability_snapshot_invalid")
        if len(snapshot.profile.supported_efforts) != 1:
            raise ValueError("capability_snapshot_invalid")
        payload_json = json.dumps(
            snapshot.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        if len(payload_json.encode("utf-8")) > 64 * 1024:
            raise ValueError("capability_snapshot_invalid")
        values = (
            snapshot.digest,
            snapshot.profile.provider.value,
            snapshot.profile.requested_model,
            snapshot.profile.effective_model,
            snapshot.profile.supported_efforts[0].value,
            snapshot.identity.executable_sha256,
            snapshot.identity.cli_version,
            snapshot.identity.adapter_protocol_version,
            snapshot.profile.permission_mode.value,
            payload_json,
            snapshot.verified_at,
            snapshot.expires_at,
        )
        try:
            with self._connection() as connection:
                existing = connection.execute(
                    "SELECT capability_snapshot_digest,provider,requested_model,"
                    "effective_model,effort,executable_sha256,cli_version,"
                    "adapter_protocol_version,permission_mode,payload_json,verified_at,expires_at "
                    "FROM capability_snapshots WHERE capability_snapshot_digest=?",
                    (snapshot.digest,),
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != values:
                        raise StorageError("storage_corrupt")
                    return False
                connection.execute(
                    """INSERT INTO capability_snapshots(
                        capability_snapshot_digest,provider,requested_model,effective_model,
                        effort,executable_sha256,cli_version,adapter_protocol_version,
                        permission_mode,payload_json,verified_at,expires_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )
                return True
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def capability_snapshot_records(self, *, limit: int) -> tuple[dict[str, Any], ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
            raise ValueError("capability_snapshot_limit_invalid")
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT capability_snapshot_digest,payload_json FROM capability_snapshots "
                    "ORDER BY capability_snapshot_digest LIMIT ?",
                    (limit + 1,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
        if len(rows) > limit:
            raise StorageError("capability_snapshot_limit")
        result: list[dict[str, Any]] = []
        for row in rows:
            payload_json = row["payload_json"]
            if not isinstance(payload_json, str) or len(payload_json.encode("utf-8")) > 64 * 1024:
                raise StorageError("storage_corrupt")
            try:
                payload = json.loads(payload_json)
            except (json.JSONDecodeError, RecursionError) as exc:
                raise StorageError("storage_corrupt") from exc
            result.append({"digest": str(row["capability_snapshot_digest"]), "payload": payload})
        return tuple(result)

    @staticmethod
    def _save_lifecycle_binding(
        connection: sqlite3.Connection,
        *,
        table: str,
        key_column: str,
        key_value: str,
        lifecycle_identity_digest: str,
        bound_at: int,
        capability_snapshot_digest: str | None = None,
    ) -> bool:
        columns = [key_column]
        values: list[object] = [key_value]
        if capability_snapshot_digest is not None:
            columns.append("capability_snapshot_digest")
            values.append(capability_snapshot_digest)
        columns.extend(("lifecycle_identity_digest", "bound_at"))
        values.extend((lifecycle_identity_digest, bound_at))
        existing = connection.execute(
            f"SELECT {','.join(columns[1:])} FROM {table} WHERE {key_column}=?",
            (key_value,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != tuple(values[1:]):
                raise StorageError("lifecycle_binding_changed")
            return False
        placeholders = ",".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})",
            tuple(values),
        )
        return True

    def save_lifecycle_snapshot_binding(
        self,
        *,
        capability_snapshot_digest: str,
        lifecycle_identity_digest: str,
        bound_at: int,
    ) -> bool:
        if not _HEX_64.fullmatch(capability_snapshot_digest):
            raise ValueError("capability_snapshot_digest_invalid")
        if not _HEX_64.fullmatch(lifecycle_identity_digest):
            raise ValueError("lifecycle_identity_digest_invalid")
        bound_at = _nonnegative_integer(bound_at, "bound_at_invalid")
        try:
            with self._connection() as connection:
                return self._save_lifecycle_binding(
                    connection,
                    table="lifecycle_snapshot_bindings",
                    key_column="capability_snapshot_digest",
                    key_value=capability_snapshot_digest,
                    lifecycle_identity_digest=lifecycle_identity_digest,
                    bound_at=bound_at,
                )
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def save_lifecycle_approval_binding(
        self,
        *,
        approval_id: str,
        capability_snapshot_digest: str,
        lifecycle_identity_digest: str,
        bound_at: int,
    ) -> bool:
        approval_id = _identifier(approval_id, "approval_id_invalid")
        if not _HEX_64.fullmatch(capability_snapshot_digest):
            raise ValueError("capability_snapshot_digest_invalid")
        if not _HEX_64.fullmatch(lifecycle_identity_digest):
            raise ValueError("lifecycle_identity_digest_invalid")
        bound_at = _nonnegative_integer(bound_at, "bound_at_invalid")
        try:
            with self._connection() as connection:
                return self._save_lifecycle_binding(
                    connection,
                    table="lifecycle_approval_bindings",
                    key_column="approval_id",
                    key_value=approval_id,
                    capability_snapshot_digest=capability_snapshot_digest,
                    lifecycle_identity_digest=lifecycle_identity_digest,
                    bound_at=bound_at,
                )
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def save_lifecycle_attempt_binding(
        self,
        *,
        attempt_id: str,
        lifecycle_identity_digest: str,
        bound_at: int,
    ) -> bool:
        attempt_id = _identifier(attempt_id, "attempt_id_invalid")
        if not _HEX_64.fullmatch(lifecycle_identity_digest):
            raise ValueError("lifecycle_identity_digest_invalid")
        bound_at = _nonnegative_integer(bound_at, "bound_at_invalid")
        try:
            with self._connection() as connection:
                return self._save_lifecycle_binding(
                    connection,
                    table="lifecycle_attempt_bindings",
                    key_column="attempt_id",
                    key_value=attempt_id,
                    lifecycle_identity_digest=lifecycle_identity_digest,
                    bound_at=bound_at,
                )
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc

    def lifecycle_identity_binding(self, *, authority_kind: str, authority_id: str) -> str | None:
        tables = {
            "capability_snapshot": (
                "lifecycle_snapshot_bindings",
                "capability_snapshot_digest",
            ),
            "approval": ("lifecycle_approval_bindings", "approval_id"),
            "attempt": ("lifecycle_attempt_bindings", "attempt_id"),
        }
        try:
            table, key_column = tables[authority_kind]
        except (KeyError, TypeError) as exc:
            raise ValueError("authority_kind_invalid") from exc
        if authority_kind == "capability_snapshot":
            authority_id = _identifier(authority_id, "authority_id_invalid")
            if not _HEX_64.fullmatch(authority_id):
                raise ValueError("authority_id_invalid")
        else:
            authority_id = _identifier(authority_id, "authority_id_invalid")
        try:
            with self._connection() as connection:
                if authority_kind == "capability_snapshot":
                    row = connection.execute(
                        """SELECT COALESCE(
                            (SELECT carry.new_identity_digest
                             FROM lifecycle_binding_carries AS carry
                             WHERE carry.capability_snapshot_digest = ?
                             ORDER BY carry.carry_id DESC LIMIT 1),
                            (SELECT binding.lifecycle_identity_digest
                             FROM lifecycle_snapshot_bindings AS binding
                             WHERE binding.capability_snapshot_digest = ?)
                        )""",
                        (authority_id, authority_id),
                    ).fetchone()
                    if row is None or row[0] is None:
                        return None
                    return str(row[0])
                row = connection.execute(
                    f"SELECT lifecycle_identity_digest FROM {table} WHERE {key_column}=?",
                    (authority_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
        return None if row is None else str(row[0])

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
            with self._connection() as connection:
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
            with self._connection() as connection:
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
            with self._connection() as connection:
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
            if row["status"] == "invalidated":
                raise StorageError("approval_lifecycle_stale")
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
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return None if row is None else str(row[0])

    def carry_forward_snapshot_bindings(
        self,
        *,
        previous_identity_digest: str,
        new_identity_digest: str,
        lifecycle_event_id: str,
        carried_at: int,
    ) -> tuple[str, ...]:
        for value in (previous_identity_digest, new_identity_digest, lifecycle_event_id):
            if not isinstance(value, str) or not _HEX_64.fullmatch(value):
                raise ValueError("lifecycle_identity_digest_invalid")
        if previous_identity_digest == new_identity_digest:
            raise ValueError("lifecycle_identity_digest_invalid")
        observed = _nonnegative_integer(carried_at, "carried_at_invalid")
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT binding.capability_snapshot_digest
                FROM lifecycle_snapshot_bindings AS binding
                JOIN capability_snapshots AS snapshot
                  ON snapshot.capability_snapshot_digest = binding.capability_snapshot_digest
                LEFT JOIN (
                    SELECT capability_snapshot_digest, new_identity_digest,
                           MAX(carry_id) AS carry_id
                    FROM lifecycle_binding_carries
                    GROUP BY capability_snapshot_digest
                ) AS carry
                  ON carry.capability_snapshot_digest = binding.capability_snapshot_digest
                WHERE COALESCE(carry.new_identity_digest, binding.lifecycle_identity_digest) = ?
                  AND snapshot.expires_at > ?
                ORDER BY binding.capability_snapshot_digest""",
                (previous_identity_digest, observed),
            ).fetchall()
            carried = tuple(str(row[0]) for row in rows)
            for digest in carried:
                connection.execute(
                    """INSERT INTO lifecycle_binding_carries(
                        capability_snapshot_digest,previous_identity_digest,
                        new_identity_digest,lifecycle_event_id,carried_at
                    ) VALUES (?,?,?,?,?)""",
                    (
                        digest,
                        previous_identity_digest,
                        new_identity_digest,
                        lifecycle_event_id,
                        observed,
                    ),
                )
            connection.commit()
            return carried
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise _translate_database_error(exc) from exc
        finally:
            if connection is not None:
                connection.close()

    def lifecycle_authority_targets(
        self, lifecycle_identity_digest: str
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not _HEX_64.fullmatch(lifecycle_identity_digest):
            raise ValueError("lifecycle_identity_digest_invalid")
        try:
            with self._connection() as connection:
                snapshots = tuple(
                    str(row[0])
                    for row in connection.execute(
                        """SELECT binding.capability_snapshot_digest
                        FROM lifecycle_snapshot_bindings AS binding
                        LEFT JOIN (
                            SELECT capability_snapshot_digest, new_identity_digest,
                                   MAX(carry_id) AS carry_id
                            FROM lifecycle_binding_carries
                            GROUP BY capability_snapshot_digest
                        ) AS carry
                          ON carry.capability_snapshot_digest = binding.capability_snapshot_digest
                        WHERE COALESCE(
                            carry.new_identity_digest, binding.lifecycle_identity_digest
                        ) = ?
                        ORDER BY binding.capability_snapshot_digest""",
                        (lifecycle_identity_digest,),
                    ).fetchall()
                )
                approvals = tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT binding.approval_id FROM lifecycle_approval_bindings AS binding "
                        "JOIN approvals AS approval ON approval.approval_id=binding.approval_id "
                        "WHERE binding.lifecycle_identity_digest=? AND approval.status='issued' "
                        "ORDER BY binding.approval_id",
                        (lifecycle_identity_digest,),
                    ).fetchall()
                )
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
        return snapshots, approvals

    def lifecycle_approval_binding_details(
        self, approval_id: str
    ) -> tuple[str, str] | None:
        approval_id = _identifier(approval_id, "approval_id_invalid")
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT capability_snapshot_digest,lifecycle_identity_digest "
                    "FROM lifecycle_approval_bindings WHERE approval_id=?",
                    (approval_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
        return None if row is None else (str(row[0]), str(row[1]))

    def invalidate_lifecycle_approvals(
        self, lifecycle_identity_digest: str
    ) -> tuple[str, ...]:
        if not _HEX_64.fullmatch(lifecycle_identity_digest):
            raise ValueError("lifecycle_identity_digest_invalid")
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            approval_ids = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT binding.approval_id FROM lifecycle_approval_bindings AS binding "
                    "JOIN approvals AS approval ON approval.approval_id=binding.approval_id "
                    "WHERE binding.lifecycle_identity_digest=? AND approval.status='issued' "
                    "ORDER BY binding.approval_id",
                    (lifecycle_identity_digest,),
                ).fetchall()
            )
            if approval_ids:
                placeholders = ",".join("?" for _ in approval_ids)
                connection.execute(
                    f"UPDATE approvals SET status='invalidated' WHERE status='issued' "
                    f"AND approval_id IN ({placeholders})",
                    approval_ids,
                )
            connection.commit()
            return approval_ids
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise _translate_database_error(exc) from exc
        finally:
            if connection is not None:
                connection.close()

    def reserved_token_total(self) -> int:
        with self._connection() as connection:
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
            with closing(sqlite3.connect(self.path, timeout=2.0)) as connection, connection:
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
            with closing(sqlite3.connect(self.path, timeout=2.0)) as connection, connection:
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
            with closing(sqlite3.connect(self.path, timeout=2.0)) as connection, connection:
                return int(connection.execute("SELECT COUNT(*) FROM aggregate_events").fetchone()[0])
        except sqlite3.Error as exc:
            raise _translate_database_error(exc) from exc
