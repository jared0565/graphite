-- Canonical routing schema v2 DDL copied verbatim from the _SCHEMA tuple in
-- commit ca77600, followed only by the schema-version metadata row required
-- to represent an initialized v2 database.
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    risk TEXT NOT NULL,
    objective_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE,
    model_id TEXT,
    effort TEXT,
    policy_version TEXT NOT NULL,
    evidence_version TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE,
    decision_id TEXT REFERENCES decisions(decision_id) ON DELETE CASCADE,
    nonce_hash TEXT NOT NULL UNIQUE,
    manifest_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    reserved_tokens INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
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
);
CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
    provenance TEXT NOT NULL,
    success INTEGER NOT NULL CHECK (success IN (0, 1)),
    severe_failure INTEGER NOT NULL CHECK (severe_failure IN (0, 1)),
    recorded_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_comparisons (
    comparison_id TEXT PRIMARY KEY,
    primary_execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
    shadow_execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
    verdict TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_versions (
    policy_version TEXT PRIMARY KEY,
    policy_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_ledger (
    entry_id TEXT PRIMARY KEY,
    approval_id TEXT REFERENCES approvals(approval_id) ON DELETE SET NULL,
    execution_id TEXT REFERENCES executions(execution_id) ON DELETE SET NULL,
    entry_type TEXT NOT NULL,
    token_amount INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS confidence_stats (
    model_id TEXT NOT NULL,
    effort TEXT NOT NULL,
    category TEXT NOT NULL,
    risk TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    success_count INTEGER NOT NULL,
    severe_failure_count INTEGER NOT NULL,
    PRIMARY KEY (model_id, effort, category, risk)
);
CREATE INDEX IF NOT EXISTS outcomes_recorded_at_idx ON outcomes(recorded_at);
CREATE INDEX IF NOT EXISTS executions_task_idx ON executions(task_id);
CREATE TABLE IF NOT EXISTS execution_evidence (
    execution_id TEXT PRIMARY KEY REFERENCES executions(execution_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    graph_fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_attempts (
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
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'completed', 'failed', 'persistence_failed', 'legacy_unrecoverable'
    )),
    failure_reason TEXT,
    execution_id TEXT UNIQUE REFERENCES executions(execution_id) ON DELETE RESTRICT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS staged_execution_receipts (
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
);
CREATE TABLE IF NOT EXISTS execution_receipts (
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
);
CREATE TABLE IF NOT EXISTS incident_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE CASCADE,
    reviewed_at INTEGER NOT NULL,
    UNIQUE(execution_id, reviewed_at)
);
CREATE TABLE IF NOT EXISTS blind_comparisons (
    comparison_id TEXT PRIMARY KEY,
    primary_execution_id TEXT NOT NULL,
    shadow_execution_id TEXT NOT NULL,
    label_a_hash TEXT NOT NULL,
    label_b_hash TEXT NOT NULL,
    label_a_is_shadow INTEGER NOT NULL CHECK (label_a_is_shadow IN (0, 1)),
    verdict TEXT,
    recorded_at INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS registry_snapshots (
    snapshot_id INTEGER PRIMARY KEY CHECK (snapshot_id = 1),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    refreshed_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2');
