-- Provider lifecycle schema-v1 compatibility fixture captured with Task 2.
-- It intentionally contains no credentials, paths, provider output, or source.

PRAGMA foreign_keys = ON;
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO schema_meta VALUES ('schema_version', '1');

CREATE TABLE current_observations (
    boundary_digest TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    runtime_kind TEXT NOT NULL,
    identity_digest TEXT,
    state TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    identity_json TEXT,
    observed_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE lifecycle_events (
    event_id TEXT PRIMARY KEY,
    boundary_digest TEXT NOT NULL,
    provider TEXT NOT NULL,
    runtime_kind TEXT NOT NULL,
    previous_identity_digest TEXT,
    current_identity_digest TEXT,
    previous_state TEXT,
    current_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);

CREATE TABLE authority_invalidations (
    invalidation_id TEXT PRIMARY KEY,
    boundary_digest TEXT NOT NULL,
    previous_identity_digest TEXT NOT NULL,
    current_identity_digest TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    invalidated_at INTEGER NOT NULL
);
