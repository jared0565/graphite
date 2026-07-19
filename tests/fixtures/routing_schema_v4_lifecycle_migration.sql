-- Schema-v4 CLI authority delta used by the independent v4-to-v5 fixture.
-- Apply after routing_schema_v2_ca77600.sql and routing_schema_v3_94eb333.sql.

CREATE TABLE capability_snapshots (
    capability_snapshot_digest TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    effective_model TEXT NOT NULL,
    effort TEXT NOT NULL,
    executable_sha256 TEXT NOT NULL,
    cli_version TEXT NOT NULL,
    adapter_protocol_version TEXT NOT NULL,
    permission_mode TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    verified_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE task_worktrees (
    worktree_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    baseline_commit TEXT NOT NULL,
    canonical_root_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE cli_execution_attempts (
    attempt_id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL UNIQUE REFERENCES approvals(approval_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id) ON DELETE RESTRICT,
    worktree_id TEXT NOT NULL REFERENCES task_worktrees(worktree_id) ON DELETE RESTRICT,
    provider TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    effective_model TEXT NOT NULL,
    effort TEXT NOT NULL,
    cli_version TEXT NOT NULL,
    executable_sha256 TEXT NOT NULL,
    capability_snapshot_digest TEXT NOT NULL
        REFERENCES capability_snapshots(capability_snapshot_digest) ON DELETE RESTRICT,
    manifest_hash TEXT NOT NULL,
    expected_prompt_hash TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL,
    status TEXT NOT NULL,
    failure_reason TEXT,
    execution_id TEXT UNIQUE REFERENCES executions(execution_id) ON DELETE RESTRICT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE validation_results (
    attempt_id TEXT PRIMARY KEY
        REFERENCES cli_execution_attempts(attempt_id) ON DELETE RESTRICT,
    diff_hash TEXT NOT NULL,
    changed_file_count INTEGER NOT NULL,
    changed_byte_count INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    recorded_at INTEGER NOT NULL
);

CREATE TABLE review_links (
    review_attempt_id TEXT PRIMARY KEY
        REFERENCES cli_execution_attempts(attempt_id) ON DELETE RESTRICT,
    primary_attempt_id TEXT NOT NULL
        REFERENCES cli_execution_attempts(attempt_id) ON DELETE RESTRICT,
    primary_diff_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

UPDATE schema_meta SET value = '4' WHERE key = 'schema_version';
