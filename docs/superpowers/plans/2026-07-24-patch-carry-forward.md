# Patch Carry-Forward Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A patch-level CLI update (e.g. Claude Code 2.1.214 → 2.1.215) on an ACTIVE `LOCAL_CLI` lifecycle boundary carries verified capability authority forward automatically instead of dropping to `verification_required`.

**Architecture:** One new assessment branch in `lifecycle.py` returns `ACTIVE`/`PATCH_CARRIED_FORWARD` for in-family patch changes on active Claude Code / Codex boundaries. A new append-only routing-store table `lifecycle_binding_carries` supersedes a snapshot's original identity binding at read time (the original `lifecycle_snapshot_bindings` row is PK-locked and immutable, so re-binding is a new-table concern, not new rows in the old table). `observe()` gains a carry path: write carry rows first (routing store), invalidate stale approvals, then record the ACTIVE→ACTIVE lifecycle event (lifecycle store) — partial failure is fail-closed and self-heals on the next observation.

**Tech Stack:** Python 3.11+, sqlite3, pytest, ruff. No live providers, no network, no subscription use anywhere in this plan.

## Global Constraints

- Work in worktree `F:\tmp\graphite-patch-carry-forward`, branch `feat/patch-carry-forward`. NEVER `pip install -e .` from this worktree.
- Every test command needs: `Set-Location F:\tmp\graphite-patch-carry-forward`, `Remove-Item Env:TMPDIR -ErrorAction SilentlyContinue`, `$env:CI='1'`, `$env:PYTHONPATH='F:\tmp\graphite-patch-carry-forward\src'` (PowerShell) so the worktree's src beats the machine-wide editable install.
- Spec: `docs/superpowers/specs/2026-07-24-patch-carry-forward-design.md`. Operator decisions (binding): PATCH only (`hash_only` stays fail-closed); 24h TTL unchanged (carry never extends `expires_at`); automatic on standard-probe pass; append-only carry-forward mechanism.
- `LOCAL_CLI` boundaries only (claude-code, codex). No behavior change for any other change class, runtime kind, provider, or starting state.
- Append-only stores: never UPDATE or DELETE rows in guarded tables; never weaken an existing immutability trigger. DDL in migrations (DROP/CREATE TRIGGER, CREATE TABLE) is allowed.
- Fail closed everywhere: any partial carry state must deny route authority, never grant it.
- Canonical graph commands are untouched by this plan; nothing here may import or affect build/scan/query paths.
- Ordinary tests never contact providers or consume quota. All fixtures are offline.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Lifecycle contracts — reason code, assessment branch, event-validator rule

**Files:**
- Modify: `src/graphite/routing/lifecycle.py`
- Test: `tests/test_provider_lifecycle.py`

**Interfaces:**
- Consumes: existing `assess_identity_change`, `ProviderCompatibilityAssessment(change, state, reason, probe_level)`, `ProviderLifecycleEvent`.
- Produces: `LifecycleReasonCode.PATCH_CARRIED_FORWARD` (value `"patch_carried_forward"`); `assess_identity_change` returning `ProviderCompatibilityAssessment(IdentityChange.PATCH, ProviderLifecycleState.ACTIVE, LifecycleReasonCode.PATCH_CARRIED_FORWARD, CompatibilityProbeLevel.STANDARD)` under the carry conditions; `ProviderLifecycleEvent` accepting ACTIVE→ACTIVE identity-changed events only with this reason. Tasks 2–4 rely on these exact names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_lifecycle.py` (the file's `_identity()` helper defaults to claude-code `2.1.215`, digest `"a"*64`; `_policy()` spans `2.1.0`–`3.0.0`):

```python
def test_patch_on_active_local_cli_carries_authority_forward() -> None:
    previous = _identity()
    current = _identity(
        version="2.1.216", runtime_digest="b" * 64, observed_at=1_721_433_600
    )

    assessment = assess_identity_change(
        previous, current, _policy(), existing_state=ProviderLifecycleState.ACTIVE
    )

    assert assessment.change is IdentityChange.PATCH
    assert assessment.state is ProviderLifecycleState.ACTIVE
    assert assessment.reason is LifecycleReasonCode.PATCH_CARRIED_FORWARD
    assert assessment.probe_level is CompatibilityProbeLevel.STANDARD


def test_hash_only_on_active_boundary_still_requires_verification() -> None:
    previous = _identity()
    current = _identity(runtime_digest="b" * 64, observed_at=1_721_433_600)

    assessment = assess_identity_change(
        previous, current, _policy(), existing_state=ProviderLifecycleState.ACTIVE
    )

    assert assessment.change is IdentityChange.HASH_ONLY
    assert assessment.state is ProviderLifecycleState.VERIFICATION_REQUIRED
    assert assessment.reason is LifecycleReasonCode.HASH_CHANGED


def test_patch_on_active_non_local_cli_still_requires_verification() -> None:
    previous = _identity(
        provider=LifecycleProviderId.OLLAMA,
        runtime_kind=RuntimeKind.LOCAL_HTTP,
        version="0.5.1",
    )
    current = _identity(
        provider=LifecycleProviderId.OLLAMA,
        runtime_kind=RuntimeKind.LOCAL_HTTP,
        version="0.5.2",
        observed_at=1_721_433_600,
    )
    # runtime_digest deliberately unchanged: for non-CLI runtimes a digest change
    # classifies as ENDPOINT (checked before PATCH), which would dodge the gate
    # this test exists to pin.
    policy = _policy(
        provider=LifecycleProviderId.OLLAMA,
        runtime_kind=RuntimeKind.LOCAL_HTTP,
        minimum_version="0.1.0",
        maximum_version_exclusive="1.0.0",
    )

    assessment = assess_identity_change(
        previous, current, policy, existing_state=ProviderLifecycleState.ACTIVE
    )

    assert assessment.change is IdentityChange.PATCH
    assert assessment.state is ProviderLifecycleState.VERIFICATION_REQUIRED
    assert assessment.reason is LifecycleReasonCode.PATCH_CHANGED


def test_patch_from_verification_required_does_not_resurrect_authority() -> None:
    previous = _identity()
    current = _identity(
        version="2.1.216", runtime_digest="b" * 64, observed_at=1_721_433_600
    )

    assessment = assess_identity_change(
        previous,
        current,
        _policy(),
        existing_state=ProviderLifecycleState.VERIFICATION_REQUIRED,
    )

    assert assessment.state is ProviderLifecycleState.VERIFICATION_REQUIRED
    assert assessment.reason is LifecycleReasonCode.PATCH_CHANGED


def test_patch_with_failed_standard_probe_is_incompatible_not_carried() -> None:
    previous = _identity()
    current = _identity(
        version="2.1.216", runtime_digest="b" * 64, observed_at=1_721_433_600
    )

    assessment = assess_identity_change(
        previous,
        current,
        _policy(),
        standard_probe_passed=False,
        existing_state=ProviderLifecycleState.ACTIVE,
    )

    assert assessment.state is ProviderLifecycleState.INCOMPATIBLE
    assert assessment.reason is LifecycleReasonCode.PROBE_FAILED


def test_patch_outside_policy_range_is_incompatible_not_carried() -> None:
    previous = _identity()
    current = _identity(
        version="2.1.216", runtime_digest="b" * 64, observed_at=1_721_433_600
    )
    policy = _policy(maximum_version_exclusive="2.1.216")

    assessment = assess_identity_change(
        previous, current, policy, existing_state=ProviderLifecycleState.ACTIVE
    )

    assert assessment.state is ProviderLifecycleState.INCOMPATIBLE
    assert assessment.reason is LifecycleReasonCode.POLICY_RANGE_UNSUPPORTED


def test_event_active_to_active_identity_change_requires_carry_reason() -> None:
    ProviderLifecycleEvent(
        "carry-event-1",
        LifecycleProviderId.CLAUDE_CODE,
        RuntimeKind.LOCAL_CLI,
        "a" * 64,
        "b" * 64,
        ProviderLifecycleState.ACTIVE,
        ProviderLifecycleState.ACTIVE,
        LifecycleReasonCode.PATCH_CARRIED_FORWARD,
        "1.0.0",
        1_721_433_600,
    )
    with pytest.raises(ValueError, match="lifecycle_transition_invalid"):
        ProviderLifecycleEvent(
            "carry-event-2",
            LifecycleProviderId.CLAUDE_CODE,
            RuntimeKind.LOCAL_CLI,
            "a" * 64,
            "b" * 64,
            ProviderLifecycleState.ACTIVE,
            ProviderLifecycleState.ACTIVE,
            LifecycleReasonCode.PATCH_CHANGED,
            "1.0.0",
            1_721_433_600,
        )


def test_event_carry_reason_is_rejected_outside_active_to_active_change() -> None:
    with pytest.raises(ValueError, match="lifecycle_transition_invalid"):
        ProviderLifecycleEvent(
            "carry-event-3",
            LifecycleProviderId.CLAUDE_CODE,
            RuntimeKind.LOCAL_CLI,
            "a" * 64,
            "b" * 64,
            ProviderLifecycleState.VERIFICATION_REQUIRED,
            ProviderLifecycleState.VERIFICATION_REQUIRED,
            LifecycleReasonCode.PATCH_CARRIED_FORWARD,
            "1.0.0",
            1_721_433_600,
        )
    with pytest.raises(ValueError, match="lifecycle_transition_invalid"):
        ProviderLifecycleEvent(
            "carry-event-4",
            LifecycleProviderId.CLAUDE_CODE,
            RuntimeKind.LOCAL_CLI,
            "a" * 64,
            "a" * 64,
            ProviderLifecycleState.ACTIVE,
            ProviderLifecycleState.ACTIVE,
            LifecycleReasonCode.PATCH_CARRIED_FORWARD,
            "1.0.0",
            1_721_433_600,
        )
```

Note: `_policy()` in this file takes keyword overrides via `**changes`; passing `provider=`, `runtime_kind=`, `minimum_version=`, `maximum_version_exclusive=` works as written. The non-CLI test uses OLLAMA because `_validate_boundary` pins each provider to its runtime kind (`_PROVIDER_RUNTIME_KINDS`), and both CLIs are `LOCAL_CLI`.

- [ ] **Step 2: Run the tests to verify they fail**

Run (PowerShell, after the Global Constraints env setup):
`python -m pytest tests/test_provider_lifecycle.py -q -k "carr or hash_only_on_active or resurrect or non_local_cli or outside_policy or failed_standard"`
Expected: FAIL — `AttributeError: PATCH_CARRIED_FORWARD`.

- [ ] **Step 3: Implement in `src/graphite/routing/lifecycle.py`**

(a) Append to `LifecycleReasonCode` (after `POLICY_PROMOTED`, keeping enum append-only):

```python
    PATCH_CARRIED_FORWARD = "patch_carried_forward"
```

(b) In `assess_identity_change`, immediately after the `if not probe_passed:` block (which returns `INCOMPATIBLE`/`PROBE_FAILED`) and before the final `return ProviderCompatibilityAssessment(change, ProviderLifecycleState.VERIFICATION_REQUIRED, ...)`, insert:

```python
    if (
        change is IdentityChange.PATCH
        and current.runtime_kind is RuntimeKind.LOCAL_CLI
        and existing is ProviderLifecycleState.ACTIVE
    ):
        return ProviderCompatibilityAssessment(
            change,
            ProviderLifecycleState.ACTIVE,
            LifecycleReasonCode.PATCH_CARRIED_FORWARD,
            probe_level,
        )
```

(All earlier gates already ran: policy range, required capabilities, and the standard probe — PATCH is in `_STANDARD_CHANGES`, so `probe_level` is `STANDARD` here.)

(c) In `ProviderLifecycleEvent.__post_init__`, replace the same-state reason check

```python
            if not identity_unchanged and reason not in set(_CHANGE_REASONS.values()):
                raise ValueError("lifecycle_transition_invalid")
```

with

```python
            if not identity_unchanged:
                if current_state is ProviderLifecycleState.ACTIVE:
                    if reason is not LifecycleReasonCode.PATCH_CARRIED_FORWARD:
                        raise ValueError("lifecycle_transition_invalid")
                elif reason not in set(_CHANGE_REASONS.values()):
                    raise ValueError("lifecycle_transition_invalid")
```

and, directly after the `elif current_state not in _VALID_TRANSITIONS[previous_state]:` clause, add the reverse pin:

```python
        if reason is LifecycleReasonCode.PATCH_CARRIED_FORWARD and not (
            previous_state is ProviderLifecycleState.ACTIVE
            and current_state is ProviderLifecycleState.ACTIVE
            and previous_digest != current_digest
        ):
            raise ValueError("lifecycle_transition_invalid")
```

- [ ] **Step 4: Run the new tests, then the whole file**

`python -m pytest tests/test_provider_lifecycle.py -q`
Expected: PASS (all). If a pre-existing test enumerates every `LifecycleReasonCode` value, add `"patch_carried_forward"` to its expected list — that is the only sanctioned pre-existing-test edit in this task; report any other failure instead of adapting it.

- [ ] **Step 5: Commit**

```powershell
git add src/graphite/routing/lifecycle.py tests/test_provider_lifecycle.py
git commit -m "feat(routing): patch carry-forward assessment + event rule for LOCAL_CLI"
```

---

### Task 2: Routing store — carry table, schema v8, effective-binding resolution

**Files:**
- Modify: `src/graphite/routing/storage.py`
- Test: `tests/test_routing_storage.py`

**Interfaces:**
- Consumes: existing `_SCHEMA` list, `initialize()`, `_migrate_v6_to_v7` pattern, `_create_schema_backup`, `_HEX_64`, `_nonnegative_integer`, `save_lifecycle_snapshot_binding`, `capability_snapshots.expires_at` column.
- Produces (Task 3 depends on these exact signatures):
  - `RepositoryStore.carry_forward_snapshot_bindings(*, previous_identity_digest: str, new_identity_digest: str, lifecycle_event_id: str, carried_at: int) -> tuple[str, ...]` — returns the re-bound snapshot digests, sorted.
  - `lifecycle_identity_binding(authority_kind="capability_snapshot", ...)` returning the **effective** identity (latest carry wins, else original binding).
  - `lifecycle_authority_targets(digest)` matching snapshots by **effective** binding.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routing_storage.py`. Use the same public helpers the service tests use (they are importable from any test file):

```python
import sqlite3

from graphite.routing.contracts import CliIdentity, Effort, PermissionMode, ProviderId, RiskTier
from graphite.routing.profiles import (
    BUNDLED_REQUESTED_PROFILES,
    create_capability_snapshot,
    save_capability_snapshot,
)
```

(merge these into the file's existing imports; do not duplicate ones already present)

```python
def _carry_store(tmp_path):
    root = tmp_path / "carry-repo"
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    return store


def _carry_snapshot(store, lifecycle_digest, *, verified_at=101, ttl_seconds=3_600):
    snapshot = create_capability_snapshot(
        requested=BUNDLED_REQUESTED_PROFILES["claude-code/sonnet"],
        identity=CliIdentity(ProviderId.CLAUDE_CODE, "a" * 64, "2.1.214", "1.0.0"),
        effective_model="claude-sonnet-5",
        effort=Effort.HIGH,
        capabilities=("code", "reasoning"),
        context_window_tokens=200_000,
        risk_ceiling=RiskTier.MEDIUM,
        permission_mode=PermissionMode.READ_ONLY,
        verified_at=verified_at,
        ttl_seconds=ttl_seconds,
    )
    save_capability_snapshot(store, snapshot)
    store.save_lifecycle_snapshot_binding(
        capability_snapshot_digest=snapshot.digest,
        lifecycle_identity_digest=lifecycle_digest,
        bound_at=verified_at,
    )
    return snapshot


def test_carry_forward_rebinds_unexpired_snapshots_and_resolution_follows(tmp_path):
    store = _carry_store(tmp_path)
    old, new = "1" * 64, "2" * 64
    snapshot = _carry_snapshot(store, old)

    carried = store.carry_forward_snapshot_bindings(
        previous_identity_digest=old,
        new_identity_digest=new,
        lifecycle_event_id="c" * 64,
        carried_at=200,
    )

    assert carried == (snapshot.digest,)
    assert (
        store.lifecycle_identity_binding(
            authority_kind="capability_snapshot", authority_id=snapshot.digest
        )
        == new
    )
    assert store.lifecycle_authority_targets(new)[0] == (snapshot.digest,)
    assert store.lifecycle_authority_targets(old)[0] == ()


def test_carry_forward_excludes_expired_snapshots_and_is_retry_idempotent(tmp_path):
    store = _carry_store(tmp_path)
    old, new = "1" * 64, "2" * 64
    snapshot = _carry_snapshot(store, old, verified_at=101, ttl_seconds=50)  # expires 151

    first = store.carry_forward_snapshot_bindings(
        previous_identity_digest=old,
        new_identity_digest=new,
        lifecycle_event_id="c" * 64,
        carried_at=200,
    )
    second = store.carry_forward_snapshot_bindings(
        previous_identity_digest=old,
        new_identity_digest=new,
        lifecycle_event_id="d" * 64,
        carried_at=201,
    )

    assert first == ()
    assert second == ()
    # Never carried: the expired snapshot keeps its original (old) binding.
    assert (
        store.lifecycle_identity_binding(
            authority_kind="capability_snapshot", authority_id=snapshot.digest
        )
        == old
    )


def test_carry_chain_latest_carry_wins_and_supports_downgrade_revisit(tmp_path):
    store = _carry_store(tmp_path)
    first, second = "1" * 64, "2" * 64
    snapshot = _carry_snapshot(store, first)

    store.carry_forward_snapshot_bindings(
        previous_identity_digest=first,
        new_identity_digest=second,
        lifecycle_event_id="c" * 64,
        carried_at=200,
    )
    store.carry_forward_snapshot_bindings(
        previous_identity_digest=second,
        new_identity_digest=first,
        lifecycle_event_id="d" * 64,
        carried_at=201,
    )

    assert (
        store.lifecycle_identity_binding(
            authority_kind="capability_snapshot", authority_id=snapshot.digest
        )
        == first
    )
    assert store.lifecycle_authority_targets(first)[0] == (snapshot.digest,)


def test_carry_rows_are_append_only(tmp_path):
    store = _carry_store(tmp_path)
    old, new = "1" * 64, "2" * 64
    _carry_snapshot(store, old)
    store.carry_forward_snapshot_bindings(
        previous_identity_digest=old,
        new_identity_digest=new,
        lifecycle_event_id="c" * 64,
        carried_at=200,
    )

    connection = sqlite3.connect(store.path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="lifecycle_binding_carry_immutable"):
            connection.execute("DELETE FROM lifecycle_binding_carries")
        with pytest.raises(sqlite3.DatabaseError, match="lifecycle_binding_carry_immutable"):
            connection.execute("UPDATE lifecycle_binding_carries SET carried_at = 999")
    finally:
        connection.close()


def test_approval_binding_guard_accepts_carried_identity(tmp_path):
    store = _carry_store(tmp_path)
    old, new = "1" * 64, "2" * 64
    snapshot = _carry_snapshot(store, old)
    store.carry_forward_snapshot_bindings(
        previous_identity_digest=old,
        new_identity_digest=new,
        lifecycle_event_id="c" * 64,
        carried_at=200,
    )
    store.save_approval_record(
        approval_id="approval-carried",
        task_id=None,
        decision_id=None,
        nonce_hash="1" * 64,
        manifest_hash="2" * 64,
        expires_at=999,
        reserved_tokens=1,
    )

    store.save_lifecycle_approval_binding(
        approval_id="approval-carried",
        capability_snapshot_digest=snapshot.digest,
        lifecycle_identity_digest=new,
        bound_at=201,
    )

    assert store.lifecycle_approval_binding_details("approval-carried") == (
        snapshot.digest,
        new,
    )


def test_v7_store_migrates_to_v8_with_backup_guard_swap_and_carry_table(tmp_path):
    store = _carry_store(tmp_path)
    connection = sqlite3.connect(store.path)
    connection.executescript(
        "DROP TRIGGER lifecycle_approval_binding_insert_guard_v8;"
        "CREATE TRIGGER lifecycle_approval_binding_insert_guard "
        "BEFORE INSERT ON lifecycle_approval_bindings "
        "WHEN NOT EXISTS ("
        "  SELECT 1 FROM lifecycle_snapshot_bindings AS binding"
        "  WHERE binding.capability_snapshot_digest = NEW.capability_snapshot_digest"
        "    AND binding.lifecycle_identity_digest = NEW.lifecycle_identity_digest"
        ") BEGIN SELECT RAISE(ABORT, 'lifecycle_snapshot_binding_missing'); END;"
        "DROP TABLE lifecycle_binding_carries;"
        "UPDATE schema_meta SET value='7' WHERE key='schema_version';"
    )
    connection.close()

    migrated = RepositoryStore(store.root)
    migrated.initialize()

    connection = sqlite3.connect(store.path)
    try:
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')"
            )
        }
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert version == "8"
    assert "lifecycle_binding_carries" in names
    assert "lifecycle_approval_binding_insert_guard_v8" in names
    assert "lifecycle_approval_binding_insert_guard" not in names
    assert (store.path.parent / f"{store.path.name}.pre-v8.backup").exists()
```

Adjust only these mechanical details while writing: (a) if `RepositoryStore` exposes its root under a different attribute than `.root`, read the constructor and use the real one; (b) if `save_approval_record`'s signature differs from the call shown (it is copied verbatim from `tests/test_provider_lifecycle_service.py`), mirror that file. Do not weaken any assertion.

- [ ] **Step 2: Run the tests to verify they fail**

`python -m pytest tests/test_routing_storage.py -q -k "carry or v8"`
Expected: FAIL — `AttributeError: carry_forward_snapshot_bindings` (and missing table for the migration test).

- [ ] **Step 3: Implement in `src/graphite/routing/storage.py`**

(a) `SCHEMA_VERSION: Final = "8"`.

(b) In `initialize()`, the allowed prior-version set gains `"7"`:

```python
            if existing_version is not None and existing_version[0] not in {
                "1", "2", "3", "4", "5", "6", "7", SCHEMA_VERSION
            }:
```

and after `self._migrate_v6_to_v7()` add `self._migrate_v7_to_v8()`.

(c) In `_SCHEMA`, delete the old `lifecycle_approval_binding_insert_guard` statement and add, after the `lifecycle_attempt_bindings` guard statements:

```python
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
```

(The carry-table statement must precede the v8 trigger in the list. The guard remains defense-in-depth: it proves the (snapshot, identity) pair existed in binding history; currency is enforced at the service layer exactly as before.)

(d) Migration helper, following `_migrate_v6_to_v7`'s shape (place directly after it). Mirror `_pre_v7_backup_path`/`_pre_v7_backup_marker_path` as `_pre_v8_backup_path`/`_pre_v8_backup_marker_path` (same directory/naming pattern with `v8`), then:

```python
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
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DROP TRIGGER IF EXISTS lifecycle_approval_binding_insert_guard"
            )
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise _translate_database_error(exc) from exc
        finally:
            if connection is not None:
                connection.close()
```

(The new table, its guards, and the v8 trigger are created by the ordinary `_SCHEMA` loop in `initialize()`; the migration's only jobs are the backup and dropping the superseded trigger. If the migration test's backup-filename assertion mismatches the `_pre_v8_backup_path` naming, fix the TEST to the helper's actual name — the helper mirrors the established v7 pattern.)

(e) New method on `RepositoryStore` (place next to `lifecycle_authority_targets`):

```python
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
```

(f) In `lifecycle_identity_binding`, keep the `tables` dispatch for validation, but give the `capability_snapshot` kind effective resolution — replace the single generic SELECT with:

```python
        try:
            with self._connect() as connection:
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
```

(g) In `lifecycle_authority_targets`, replace the snapshots query with the effective-binding form:

```python
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
```

(the approvals query is untouched — approval bindings pin literal digests and are never carried).

- [ ] **Step 4: Run the new tests, then the whole file**

`python -m pytest tests/test_routing_storage.py -q`
Expected: PASS. Any pre-existing failure in this file means (f)/(g) changed behavior a test pinned — investigate before touching that test, and report it in the task summary.

- [ ] **Step 5: Commit**

```powershell
git add src/graphite/routing/storage.py tests/test_routing_storage.py
git commit -m "feat(routing): schema v8 lifecycle_binding_carries + effective-binding resolution"
```

---

### Task 3: Lifecycle service — carry path in observe()

**Files:**
- Modify: `src/graphite/routing/lifecycle_service.py`
- Modify: `tests/test_provider_lifecycle_service.py` (one parametrize row + new tests)

**Interfaces:**
- Consumes: Task 1's `PATCH_CARRIED_FORWARD` + event rule; Task 2's `carry_forward_snapshot_bindings`, effective `lifecycle_identity_binding`.
- Produces: `LifecycleObservationResult` gains `carried_snapshots: tuple[str, ...]` (last field, in `_public_fields` too); `observe()` returns it; new service error code `"lifecycle_carry_forward_failed"`.

- [ ] **Step 1: Update the one existing test this spec changes**

In `tests/test_provider_lifecycle_service.py::test_observation_classifies_each_cli_drift_without_reactivation`, delete the parametrize row `({"version": "2.1.215", "observed_at": 200}, IdentityChange.PATCH),` — patch drift on an active CLI boundary now legitimately reactivates; the new tests below cover it. Touch nothing else in the file yet.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_provider_lifecycle_service.py`:

```python
def test_patch_update_on_active_boundary_carries_authority_forward(tmp_path: Path) -> None:
    service, lifecycle, routing = _service(tmp_path)
    boundary = "b" * 64
    identity = _identity()
    service.observe(boundary_digest=boundary, identity=identity, policy=_policy())
    snapshot = _snapshot(routing, identity.digest)
    service.activate(
        boundary_digest=boundary,
        lifecycle_identity_digest=identity.digest,
        capability_snapshot_digest=snapshot.digest,
        activated_at=102,
    )
    routing.save_approval_record(
        approval_id="approval-stale", task_id=None, decision_id=None,
        nonce_hash="1" * 64, manifest_hash="2" * 64, expires_at=999, reserved_tokens=1,
    )
    routing.save_lifecycle_approval_binding(
        approval_id="approval-stale",
        capability_snapshot_digest=snapshot.digest,
        lifecycle_identity_digest=identity.digest,
        bound_at=103,
    )
    updated = replace(identity, version="2.1.215", runtime_digest="e" * 64, observed_at=200)

    result = service.observe(boundary_digest=boundary, identity=updated, policy=_policy())

    assert result.state is ProviderLifecycleState.ACTIVE
    assert result.reason is LifecycleReasonCode.PATCH_CARRIED_FORWARD
    assert result.change is IdentityChange.PATCH
    assert result.carried_snapshots == (snapshot.digest,)
    assert result.invalidated_snapshots == ()
    assert result.invalidated_approvals == ("approval-stale",)
    assert (
        routing.lifecycle_identity_binding(
            authority_kind="capability_snapshot", authority_id=snapshot.digest
        )
        == updated.digest
    )
    observation = lifecycle.current_observation(boundary)
    assert observation.state is ProviderLifecycleState.ACTIVE
    assert observation.identity.digest == updated.digest
    events = lifecycle.events(boundary)
    assert events[-1].reason is LifecycleReasonCode.PATCH_CARRIED_FORWARD
    assert events[-1].previous_identity_digest == identity.digest
    assert events[-1].current_identity_digest == updated.digest


def test_patch_carry_with_expired_snapshot_keeps_boundary_active_but_carries_nothing(
    tmp_path: Path,
) -> None:
    service, lifecycle, routing = _service(tmp_path)
    boundary = "b" * 64
    identity = _identity()
    service.observe(boundary_digest=boundary, identity=identity, policy=_policy())
    snapshot = _snapshot(routing, identity.digest)  # ttl 3600, verified 101 -> expires 3701
    service.activate(
        boundary_digest=boundary,
        lifecycle_identity_digest=identity.digest,
        capability_snapshot_digest=snapshot.digest,
        activated_at=102,
    )
    updated = replace(identity, version="2.1.215", runtime_digest="e" * 64, observed_at=4_000)

    result = service.observe(boundary_digest=boundary, identity=updated, policy=_policy())

    assert result.state is ProviderLifecycleState.ACTIVE
    assert result.carried_snapshots == ()
    assert (
        routing.lifecycle_identity_binding(
            authority_kind="capability_snapshot", authority_id=snapshot.digest
        )
        == identity.digest
    )


def test_consecutive_patch_updates_chain_the_carry(tmp_path: Path) -> None:
    service, _lifecycle, routing = _service(tmp_path)
    boundary = "b" * 64
    identity = _identity()
    service.observe(boundary_digest=boundary, identity=identity, policy=_policy())
    snapshot = _snapshot(routing, identity.digest)
    service.activate(
        boundary_digest=boundary,
        lifecycle_identity_digest=identity.digest,
        capability_snapshot_digest=snapshot.digest,
        activated_at=102,
    )
    second = replace(identity, version="2.1.215", runtime_digest="e" * 64, observed_at=200)
    third = replace(identity, version="2.1.216", runtime_digest="f" * 64, observed_at=300)

    first_result = service.observe(boundary_digest=boundary, identity=second, policy=_policy())
    second_result = service.observe(boundary_digest=boundary, identity=third, policy=_policy())

    assert first_result.carried_snapshots == (snapshot.digest,)
    assert second_result.carried_snapshots == (snapshot.digest,)
    assert second_result.state is ProviderLifecycleState.ACTIVE
    assert (
        routing.lifecycle_identity_binding(
            authority_kind="capability_snapshot", authority_id=snapshot.digest
        )
        == third.digest
    )


def test_minor_drift_after_carry_invalidates_carried_authority(tmp_path: Path) -> None:
    service, _lifecycle, routing = _service(tmp_path)
    boundary = "b" * 64
    identity = _identity()
    service.observe(boundary_digest=boundary, identity=identity, policy=_policy())
    snapshot = _snapshot(routing, identity.digest)
    service.activate(
        boundary_digest=boundary,
        lifecycle_identity_digest=identity.digest,
        capability_snapshot_digest=snapshot.digest,
        activated_at=102,
    )
    patched = replace(identity, version="2.1.215", runtime_digest="e" * 64, observed_at=200)
    service.observe(boundary_digest=boundary, identity=patched, policy=_policy())
    minor = replace(identity, version="2.2.0", runtime_digest="f" * 64, observed_at=300)

    result = service.observe(boundary_digest=boundary, identity=minor, policy=_policy())

    assert result.state is ProviderLifecycleState.VERIFICATION_REQUIRED
    assert result.invalidated_snapshots == (snapshot.digest,)
    assert result.carried_snapshots == ()


def test_patch_on_verification_required_boundary_does_not_carry(tmp_path: Path) -> None:
    service, _lifecycle, routing = _service(tmp_path)
    boundary = "b" * 64
    identity = _identity()
    service.observe(boundary_digest=boundary, identity=identity, policy=_policy())
    updated = replace(identity, version="2.1.215", runtime_digest="e" * 64, observed_at=200)

    result = service.observe(boundary_digest=boundary, identity=updated, policy=_policy())

    assert result.state is ProviderLifecycleState.VERIFICATION_REQUIRED
    assert result.reason is LifecycleReasonCode.PATCH_CHANGED
    assert result.carried_snapshots == ()


def test_carry_partial_failure_fails_closed_and_heals_on_next_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, lifecycle, routing = _service(tmp_path)
    boundary = "b" * 64
    identity = _identity()
    service.observe(boundary_digest=boundary, identity=identity, policy=_policy())
    snapshot = _snapshot(routing, identity.digest)
    service.activate(
        boundary_digest=boundary,
        lifecycle_identity_digest=identity.digest,
        capability_snapshot_digest=snapshot.digest,
        activated_at=102,
    )
    updated = replace(identity, version="2.1.215", runtime_digest="e" * 64, observed_at=200)
    original_record = lifecycle.record_transition

    def _explode(*args: object, **kwargs: object) -> bool:
        raise LifecycleStorageError("induced_failure")

    monkeypatch.setattr(lifecycle, "record_transition", _explode)
    with pytest.raises(LifecycleServiceError, match="lifecycle_persistence_failed"):
        service.observe(boundary_digest=boundary, identity=updated, policy=_policy())
    monkeypatch.setattr(lifecycle, "record_transition", original_record)

    # Fail-closed while partial: binding moved, observation did not.
    assert (
        routing.lifecycle_identity_binding(
            authority_kind="capability_snapshot", authority_id=snapshot.digest
        )
        == updated.digest
    )
    assert lifecycle.current_observation(boundary).identity.digest == identity.digest
    with pytest.raises(LifecycleServiceError, match="lifecycle_binding_missing"):
        service.require_snapshot_authority(
            boundary_digest=boundary,
            lifecycle_identity_digest=identity.digest,
            capability_snapshot_digest=snapshot.digest,
            live_identity=identity,
        )

    healed = service.observe(boundary_digest=boundary, identity=updated, policy=_policy())

    assert healed.state is ProviderLifecycleState.ACTIVE
    assert healed.reason is LifecycleReasonCode.PATCH_CARRIED_FORWARD
    assert lifecycle.current_observation(boundary).identity.digest == updated.digest
```

Add `from graphite.routing.lifecycle_storage import LifecycleStore, LifecycleStorageError` to the file's imports (extend the existing import line).

- [ ] **Step 3: Run the tests to verify they fail**

`python -m pytest tests/test_provider_lifecycle_service.py -q -k "carr or heals"`
Expected: FAIL — `TypeError`/`AttributeError` around `carried_snapshots`, and observe() returning `VERIFICATION_REQUIRED` where ACTIVE is expected.

- [ ] **Step 4: Implement in `src/graphite/routing/lifecycle_service.py`**

(a) Import `ProviderCompatibilityAssessment` and `CompatibilityProbeLevel` is NOT needed — extend the existing `from .lifecycle import (...)` with `ProviderCompatibilityAssessment` only.

(b) `LifecycleObservationResult`: append field `carried_snapshots: tuple[str, ...]` after `invalidated_approvals`, and append `"carried_snapshots"` to `_public_fields`.

(c) In `observe()`, inside the final `else:` branch, after the `POLICY_PROMOTED` override, divert the carry case:

```python
            if reason is LifecycleReasonCode.PATCH_CARRIED_FORWARD:
                return self._carry_forward(boundary_digest, identity, previous, assessment)
```

(the `assessment.state is ACTIVE` case only arises with this reason, so the existing `_record`/invalidation flow below stays byte-identical for every other path). Update the function's final `return LifecycleObservationResult(...)` to pass `()` for `carried_snapshots`, and the two early-construction sites if any (there is exactly one `return` today — extend it).

(d) Add the carry method to `ProviderLifecycleService`:

```python
    def _carry_forward(
        self,
        boundary_digest: str,
        identity: ProviderRuntimeIdentity,
        previous: CurrentLifecycleObservation,
        assessment: ProviderCompatibilityAssessment,
    ) -> LifecycleObservationResult:
        if previous.identity is None:
            raise LifecycleServiceError("lifecycle_observation_invalid")
        event = _event(
            boundary_digest,
            identity,
            previous,
            ProviderLifecycleState.ACTIVE,
            LifecycleReasonCode.PATCH_CARRIED_FORWARD,
            identity.observed_at,
        )
        try:
            carried = self.routing_store.carry_forward_snapshot_bindings(
                previous_identity_digest=previous.identity.digest,
                new_identity_digest=identity.digest,
                lifecycle_event_id=event.event_id,
                carried_at=identity.observed_at,
            )
            invalidated_approvals = self.routing_store.invalidate_lifecycle_approvals(
                previous.identity.digest
            )
            self.lifecycle_store.record_invalidations(
                boundary_digest=boundary_digest,
                previous_identity_digest=previous.identity.digest,
                current_identity_digest=identity.digest,
                capability_snapshot_digests=(),
                approval_ids=invalidated_approvals,
                reason=LifecycleReasonCode.PATCH_CARRIED_FORWARD,
                invalidated_at=identity.observed_at,
            )
        except (StorageError, LifecycleStorageError, ValueError):
            raise LifecycleServiceError("lifecycle_carry_forward_failed") from None
        try:
            self.lifecycle_store.record_transition(boundary_digest, identity, event)
            current = self.lifecycle_store.current_observation(boundary_digest)
        except (LifecycleStorageError, ValueError):
            raise LifecycleServiceError("lifecycle_persistence_failed") from None
        if current is None:
            raise LifecycleServiceError("lifecycle_persistence_failed")
        return LifecycleObservationResult(
            identity.digest,
            assessment.change,
            current.state,
            assessment.reason,
            (),
            invalidated_approvals,
            carried,
        )
```

Ordering is deliberate (bind → invalidate → record): a crash between steps leaves route authority DENIED (binding points at the new identity while the current observation still names the old one — `lifecycle_binding_missing`), and the next observation of the same binary classifies PATCH again, re-enters `_carry_forward`, the carry query finds nothing left to move (effective bindings already point at the new identity), and the event record completes the heal. Do not reorder.

- [ ] **Step 5: Run the service tests, then the neighboring routing suites**

`python -m pytest tests/test_provider_lifecycle_service.py tests/test_provider_lifecycle.py tests/test_routing_storage.py tests/test_provider_lifecycle_storage.py -q`
Expected: PASS. If any OTHER test file asserts on `LifecycleObservationResult` field counts (e.g. serialization), extend it with `carried_snapshots` and note it in the summary.

- [ ] **Step 6: Commit**

```powershell
git add src/graphite/routing/lifecycle_service.py tests/test_provider_lifecycle_service.py
git commit -m "feat(routing): observe() carries patch updates forward on active CLI boundaries"
```

---

### Task 4: Route-authority integration pin, spec wording sync, full gate

**Files:**
- Test: `tests/test_provider_lifecycle_service.py` (one integration test)
- Modify: `docs/superpowers/specs/2026-07-24-patch-carry-forward-design.md`

**Interfaces:**
- Consumes: everything above; `ProviderLifecycleService.route_authority`, `graphite.routing.route_pool.select_route` / `ApprovedRoutePool` / `ApprovedRouteCandidate` / `RoutePoolError`.

- [ ] **Step 1: Write the integration test**

Append to `tests/test_provider_lifecycle_service.py`. Build the candidate/pool exactly the way `tests/test_route_pool.py`'s existing fixtures do (copy its candidate-construction helper verbatim and adapt only the digests/expiры below — the dataclass fields include `candidate_id`, `provider`, `runtime_kind`, `lifecycle_identity_digest`, `capability_snapshot_digest`, `model_identity_digest`, `snapshot_expires_at`; verify against `route_pool.py` before writing):

```python
def test_route_authority_selects_carried_snapshot_and_rejects_stale_pins(tmp_path: Path) -> None:
    service, _lifecycle, routing = _service(tmp_path)
    boundary = "b" * 64
    identity = _identity()
    service.observe(boundary_digest=boundary, identity=identity, policy=_policy())
    snapshot = _snapshot(routing, identity.digest)
    service.activate(
        boundary_digest=boundary,
        lifecycle_identity_digest=identity.digest,
        capability_snapshot_digest=snapshot.digest,
        activated_at=102,
    )
    updated = replace(identity, version="2.1.215", runtime_digest="e" * 64, observed_at=200)
    service.observe(boundary_digest=boundary, identity=updated, policy=_policy())

    fresh_candidate = _route_candidate(  # helper copied from tests/test_route_pool.py
        candidate_id="carried-primary",
        lifecycle_identity_digest=updated.digest,
        capability_snapshot_digest=snapshot.digest,
    )
    authority = service.route_authority(boundary, fresh_candidate)
    assert authority.state is ProviderLifecycleState.ACTIVE
    assert authority.lifecycle_identity_digest == updated.digest

    stale_candidate = _route_candidate(
        candidate_id="carried-primary",
        lifecycle_identity_digest=identity.digest,
        capability_snapshot_digest=snapshot.digest,
    )
    from graphite.routing.route_pool import RoutePoolError, _validate_authority

    with pytest.raises(RoutePoolError, match="route_identity_changed"):
        _validate_authority(stale_candidate, authority, now=210)
```

If `_validate_authority` is not importable (private), assert through `select_route` with a one-candidate pool instead — same expected error code. The load-bearing assertions are: post-carry `route_authority` succeeds against fresh pins, and pre-update pins fail `route_identity_changed`.

- [ ] **Step 2: Run it**

`python -m pytest tests/test_provider_lifecycle_service.py -q -k "route_authority_selects_carried"`
Expected: PASS with no production-code change (Tasks 1–3 already make it true). If it fails, the defect is in Tasks 1–3 — fix there, never by weakening this test.

- [ ] **Step 3: Sync the spec's mechanism wording to the built reality**

In `docs/superpowers/specs/2026-07-24-patch-carry-forward-design.md`:

1. Section 6, replace the first sentence "Recording a carry-forward is one atomic store transaction containing:" and its numbered list with:

   > Recording a carry-forward is two single-store transactions in a fixed,
   > fail-closed order (the bindings live in the routing store and the
   > observation in the lifecycle store — separate SQLite databases, so one
   > cross-store transaction is not available):
   >
   > 1. one routing-store transaction inserting one row into the new
   >    append-only `lifecycle_binding_carries` table per currently-bound,
   >    unexpired capability snapshot of the boundary, re-pointing its
   >    effective binding to the new identity digest (the original
   >    `lifecycle_snapshot_bindings` row is immutable and is superseded at
   >    read time by the latest carry row), followed by invalidation of
   >    approvals still pinned to the previous identity; then
   > 2. one lifecycle-store write recording the `active` → `active`
   >    observation with reason `patch_carried_forward`.
   >
   > A failure between the two leaves route authority denied (bindings name
   > the new identity while the current observation still names the old one)
   > and the next observation of the same binary retries and completes the
   > carry.

2. Section 6, replace "Each new binding row records: the predecessor binding it supersedes, a carried-forward marker, and the digest of the probe receipt that justified the carry." with:

   > Each carry row records the previous effective identity digest, the new
   > identity digest, the event id of the `patch_carried_forward` lifecycle
   > event that justified it, and the carry time.

3. Section 9, replace the first sentence ("The lifecycle store's binding table gains …") with:

   > The routing store gains the append-only `lifecycle_binding_carries`
   > table (schema v7 → v8, with a pre-migration backup and a rollback
   > fixture); the approval-binding insert guard is recreated carry-aware.
   > `verified_at`/`expires_at` are never copied or altered — expiry always
   > reads from the immutable snapshot row itself.

   and delete the sentence fragment "(the implementation plan pins the exact schema version numbers after reading `lifecycle_storage.py`)".

4. Section 9, replace the closing sentence "The carry-forward lifecycle event carries: provider, runtime kind, old identity digest, new identity digest, old and new versions, probe level, and the count of re-bound snapshots — no secrets, prompts, source, or raw provider diagnostics." with:

   > The carry audit trail is the `patch_carried_forward` lifecycle event
   > (provider, runtime kind, old and new identity digests, states, policy
   > version, occurred-at) plus one carry row per re-bound snapshot
   > referencing that event's id — no secrets, prompts, source, or raw
   > provider diagnostics anywhere.

- [ ] **Step 4: Full gate**

```powershell
Set-Location F:\tmp\graphite-patch-carry-forward
Remove-Item Env:TMPDIR -ErrorAction SilentlyContinue
$env:CI='1'; $env:PYTHONPATH='F:\tmp\graphite-patch-carry-forward\src'
python -m ruff check .
python -m pytest -q
```
Expected: ruff clean; full suite passes with 0 failures (44 skips are normal). Any failure outside the files this plan touches must be reported, not adapted.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_provider_lifecycle_service.py docs/superpowers/specs/2026-07-24-patch-carry-forward-design.md
git commit -m "test(routing): pin carried-authority route selection; sync spec wording to built mechanism"
```
