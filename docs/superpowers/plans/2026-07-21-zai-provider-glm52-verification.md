# z.ai Native Provider + GLM 5.2 Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add z.ai as a first-class governed remote provider and prove `glm-5.2` reachable through z.ai's own OpenAI-compatible API with the operator's `ZAI_API_KEY`: one bounded plain-text const verification promotes a READ_ONLY verification capability snapshot (provider `zai`, model `glm-5.2`) bound to a new ACTIVE lifecycle identity.

**Architecture:** An **isolated** z.ai adapter (`zai_probe.py` + `zai_executor.py`) reusing the provider-agnostic verification/promotion path (`profiles.verify_and_save_approved_profile`) and the hardened HTTP transport (`probe_runner.run_http_probe`, extended additively for z.ai). Two schema migrations are required because two SQLite stores gate the provider value: the routing store (`events.sqlite3`, has a migration framework → v6→v7) and the **lifecycle store** (`provider-lifecycle.sqlite3`, has **no** migration framework — needs a hand-written table-rebuild → v1→v2). OpenRouter's adapter modules are left byte-untouched; the shared enum/DDL/transport files gain **additive** z.ai entries.

**Tech Stack:** Python 3.14; `graphite.routing` (`contracts`, `lifecycle`, `lifecycle_storage`, `lifecycle_service`, `profiles`, `storage`, `probe_runner`, new `zai_probe`/`zai_executor`); SQLite (WAL); z.ai OpenAI-compatible `chat/completions`.

## Global Constraints

- **Isolated adapter.** New code lives in `zai_probe.py` / `zai_executor.py`. `openrouter_executor.py` and `openrouter_probe.py` are **not edited**. Shared files (`contracts.py`, `lifecycle.py`, `lifecycle_storage.py`, `profiles.py`, `storage.py`, `probe_runner.py`) gain **additive** z.ai entries only; no OpenRouter branch is altered. The full existing suite must stay green after every task.
- **Provider wire value is `"zai"`** everywhere — `ProviderId.ZAI = "zai"`, `LifecycleProviderId.ZAI = "zai"`, and the SQL CHECK literal `'zai'` must all match exactly (a mismatch fails closed at a DDL CHECK or an enum coercion).
- **Two schema migrations, both additive + backward-compatible.** Routing v6→v7 and lifecycle v1→v2 only *widen* CHECKs; every existing row (12 capability_snapshots / 12 lifecycle_snapshot_bindings / 23 cli_telemetry_events; and the lifecycle store's observations/events) is preserved and re-counted. Both migrate a *copy* in dry-runs; the live migration is a separate governed step.
- **Verification is plain-text const**, not JSON: prompt asks for exactly `GRAPHITE_PROFILE_OK`; oracle is `result.message.strip() == "GRAPHITE_PROFILE_OK"`. No `response_format`, no `json.loads(content)`, no output-schema.
- **Operator-pinned pricing** (no z.ai catalog): `prompt "0.0000014"` ($1.40/1M), `completion "0.0000044"` ($4.40/1M). Feeds the hard cost ceiling.
- **Live inference, one call, key from `ZAI_API_KEY`** in the session env — never argv/bundle/receipt. Operator runs each live step via `!` with **no inline key** (an inline value shadows the ambient key). **Up-front restart prerequisite:** Claude Code is restarted *once, before Task 1*, so the session inherits `ZAI_API_KEY` (a User-scope var a process started earlier cannot see); the key *value* never enters the transcript, a bundle, or a receipt — only its presence (boolean + length) is ever checked. With the key ambient from session start, the offline build (Tasks 1–9), the live migration (Task 10), and the live verification (Task 11) run in one uninterrupted session — **no restart between the two live steps.** Never touches `F:\Projects\graphite` (main); no merge/push/deploy.
- **Sanitized receipts only:** digests, counts, tokens, cost, durations, outcome categories, booleans. `raw_provider_output_persistence: false`.
- **Store end-state correction:** the OpenRouter verification precedent writes **+2** telemetry rows (machine `MACHINE_VERIFIED` + human `ACCEPTED`), so verification takes the store **12/12/23 → 13/13/25** (+1 snapshot, +1 binding, +2 telemetry) — not the spec's estimated 13/13/24. This plan mirrors the +2 precedent; flag at handoff.

## Interfaces contract (fixed names/types every task shares)

```python
# Provider identity (both enums, same wire value)
ProviderId.ZAI = "zai"                 # contracts.py
LifecycleProviderId.ZAI = "zai"        # lifecycle.py
_PROVIDER_RUNTIME_KINDS[LifecycleProviderId.ZAI] = RuntimeKind.REMOTE_HTTPS
# ProviderRuntimeIdentity rules for zai: routing_policy_digest=None (FORBIDDEN for non-OPENROUTER);
#   model_identity_digest REQUIRED (ZAI added to the {OLLAMA, OPENROUTER} required-set for remote parity).

# Endpoint / model / evidence / pricing (constants in zai_probe.py unless noted)
ZAI_CANONICAL_ENDPOINT = "https://api.z.ai/api/paas/v4"
ZAI_HOST = "api.z.ai"; ZAI_CHAT_PATH = "/api/paas/v4/chat/completions"
ZAI_MODEL = "glm-5.2"
ZAI_EVIDENCE_HOST = "docs.z.ai"        # profiles._EVIDENCE_HOSTS[ProviderId.ZAI]
ZAI_PROMPT_PRICE = "0.0000014"; ZAI_COMPLETION_PRICE = "0.0000044"
ZAI_API_CONTRACT_VERSION = "1.0.0"; ZAI_ADAPTER_PROTOCOL_VERSION = "1.0.0"
ZAI_CAPABILITIES = ("remote_inference",)   # pinned identically in the probe identity AND _ZAI_POLICY

# New probe_runner entries (additive)
ProbeEndpointPurpose.ZAI_CHAT_COMPLETIONS = "zai_chat_completions"
_ZAI_HOSTS = frozenset({"api.z.ai"})
_PURPOSE_POLICY[ZAI_CHAT_COMPLETIONS] = (LifecycleProviderId.ZAI, "POST", "/api/paas/v4/chat/completions")
# ZAI_CHAT_COMPLETIONS added to _BODY_PURPOSES and _INFERENCE_PURPOSES.

# New adapter surface (zai_probe.py / zai_executor.py)
@dataclass(frozen=True, slots=True) ZaiPricing(prompt: str, completion: str)   # + .digest ; mirrors OpenRouterPricing
zai_cost_microunits(pricing: ZaiPricing, *, input_tokens: int, output_tokens: int) -> int   # clone of completion_cost_microunits
@dataclass(frozen=True, slots=True) ZaiPreflight(identity: CliIdentity, runtime: ProviderRuntimeIdentity, pricing: ZaiPricing)
preflight_zai(*, model_id: str, observed_at: int, policy_version: str) -> ZaiPreflight   # LOCAL, no network
@dataclass(frozen=True, slots=True) ZaiExecutionResult(effective_model, message, input_tokens, output_tokens, cost_microunits, duration_seconds, request_sha256, response_sha256)
execute_zai(*, api_key: str, prompt: bytes, requested_model: str, expected_effective_model: str, pricing: ZaiPricing, max_output_tokens: int, max_cost_microunits: int, timeout_seconds: float, transport: HttpProbe = run_http_probe) -> ZaiExecutionResult

# profiles.py helper (additive)
operator_zai_profile(*, model_id, supported_efforts, evidence_url, evidence_accessed) -> RequestedProfile

# Verification wiring
_ZAI_POLICY  # mirrors _OPENROUTER_POLICY: required_capabilities=ZAI_CAPABILITIES, version range containing "1.0.0"
```

## File Structure

- Modify `src/graphite/routing/contracts.py` — `ProviderId.ZAI`.
- Modify `src/graphite/routing/lifecycle.py` — `LifecycleProviderId.ZAI`, `_PROVIDER_RUNTIME_KINDS`, model-digest required-set.
- Modify `src/graphite/routing/profiles.py` — `_EVIDENCE_HOSTS[ZAI]`, `operator_zai_profile`, `_ZAI_POLICY` (or a policy module).
- Modify `src/graphite/routing/storage.py` — SCHEMA_VERSION 7, widen 2 DDL CHECKs, allow-list, `_migrate_v6_to_v7`, dispatch.
- Modify `src/graphite/routing/lifecycle_storage.py` — widen `_PROVIDER_VALUES` + 2 pairing CHECKs, `LIFECYCLE_SCHEMA_VERSION` "2", hand-written rebuild migration.
- Modify `src/graphite/routing/probe_runner.py` — `_ZAI_HOSTS`, `ZAI_CHAT_COMPLETIONS` purpose, `_PURPOSE_POLICY` row, purpose sets, `HttpProbeEndpoint.__post_init__` z.ai branch.
- Create `src/graphite/routing/zai_probe.py` — `ZaiPricing`, `zai_cost_microunits`, `preflight_zai`, model-id validation, endpoint constant.
- Create `src/graphite/routing/zai_executor.py` — `execute_zai`, `ZaiExecutionResult`.
- Create tests under `tests/` per task.
- Create harness `F:\tmp\graphite-live-acceptance-harness\_prepare_zai_glm52_verification.py` / `_execute_zai_glm52_verification.py`.
- Modify `docs/superpowers/implementation-notes/2026-07-19-provider-lifecycle-evidence.md` — evidence.

---

### Task 1: Provider enums + identity rules

**Files:** Modify `src/graphite/routing/contracts.py`, `src/graphite/routing/lifecycle.py`. Test: `tests/test_routing_lifecycle.py` (extend), `tests/test_routing_contracts.py` (extend).

**Interfaces:**
- Produces: `ProviderId.ZAI`, `LifecycleProviderId.ZAI`, the runtime-kind map entry, and the model-digest-required-set membership that later tasks (probe, verification) rely on.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routing_lifecycle.py (append)
from graphite.routing.lifecycle import (
    LifecycleProviderId, RuntimeKind, ProviderRuntimeIdentity, _PROVIDER_RUNTIME_KINDS,
)

def test_zai_is_remote_https_and_requires_model_digest_and_forbids_routing():
    assert LifecycleProviderId.ZAI.value == "zai"
    assert _PROVIDER_RUNTIME_KINDS[LifecycleProviderId.ZAI] is RuntimeKind.REMOTE_HTTPS
    # routing_policy_digest MUST be None for zai (forbidden for non-OPENROUTER)
    identity = ProviderRuntimeIdentity(
        LifecycleProviderId.ZAI, RuntimeKind.REMOTE_HTTPS, "1.0.0",
        "a" * 64, "b" * 64, None, ("remote_inference",), "1.0.0", 1_700_000_000,
    )
    assert identity.provider is LifecycleProviderId.ZAI
    # passing a routing digest for zai must fail
    import pytest
    with pytest.raises(ValueError):
        ProviderRuntimeIdentity(
            LifecycleProviderId.ZAI, RuntimeKind.REMOTE_HTTPS, "1.0.0",
            "a" * 64, "b" * 64, "c" * 64, ("remote_inference",), "1.0.0", 1_700_000_000,
        )
    # zai now REQUIRES a model_identity_digest (parity with OLLAMA/OPENROUTER): None must fail
    with pytest.raises(ValueError):
        ProviderRuntimeIdentity(
            LifecycleProviderId.ZAI, RuntimeKind.REMOTE_HTTPS, "1.0.0",
            "a" * 64, None, None, ("remote_inference",), "1.0.0", 1_700_000_000,
        )

def test_zai_provider_id_in_contracts():
    from graphite.routing.contracts import ProviderId
    assert ProviderId.ZAI.value == "zai"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="F:/tmp/graphite-claude-codex-router/src" python -B -m pytest -q -p no:cacheprovider --basetemp "F:/tmp/graphite-zai-t1" tests/test_routing_lifecycle.py::test_zai_is_remote_https_and_requires_model_digest_and_forbids_routing tests/test_routing_contracts.py::test_zai_provider_id_in_contracts -v`
Expected: FAIL (`AttributeError: ZAI` / `KeyError`).

- [ ] **Step 3: Implement the enum + rule edits**

In `contracts.py`, add to `ProviderId` (after `OPENROUTER = "openrouter"`):
```python
    ZAI = "zai"
```

In `lifecycle.py`, add to `LifecycleProviderId` (after `OPENROUTER = "openrouter"`):
```python
    ZAI = "zai"
```
Add to `_PROVIDER_RUNTIME_KINDS` (after the OPENROUTER entry, ~line 96):
```python
    LifecycleProviderId.ZAI: RuntimeKind.REMOTE_HTTPS,
```
In `ProviderRuntimeIdentity.__post_init__`, the model_identity_digest-required set (currently `{LifecycleProviderId.OLLAMA, LifecycleProviderId.OPENROUTER}`, ~lines 222-225) — add `LifecycleProviderId.ZAI`:
```python
    if self.provider in {LifecycleProviderId.OLLAMA, LifecycleProviderId.OPENROUTER, LifecycleProviderId.ZAI}:
        # ... existing "model_identity_digest required" branch unchanged
```
Leave the routing_policy_digest rule unchanged — it already requires OPENROUTER and forbids all others (including ZAI), which is what we want.

- [ ] **Step 4: Run tests to verify they pass**

Run the Step-2 command. Expected: PASS.

- [ ] **Step 5: Full suite sanity + commit**

Run: `PYTHONPATH="F:/tmp/graphite-claude-codex-router/src" python -B -m pytest -q -p no:cacheprovider --basetemp "F:/tmp/graphite-zai-suite" tests/test_routing_lifecycle.py tests/test_routing_contracts.py`
Expected: all pass.
```bash
git add src/graphite/routing/contracts.py src/graphite/routing/lifecycle.py tests/test_routing_lifecycle.py tests/test_routing_contracts.py
git commit -m "feat(zai): add ProviderId.ZAI + LifecycleProviderId.ZAI (remote-https, model-digest required, no routing digest)"
```

---

### Task 2: profiles.py — evidence host + `operator_zai_profile`

**Files:** Modify `src/graphite/routing/profiles.py`. Test: `tests/test_routing_profiles.py` (extend).

**Interfaces:**
- Consumes: `ProviderId.ZAI` (Task 1).
- Produces: `operator_zai_profile(*, model_id, supported_efforts, evidence_url, evidence_accessed) -> RequestedProfile`; `_EVIDENCE_HOSTS[ProviderId.ZAI] = "docs.z.ai"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routing_profiles.py (append)
from graphite.routing.profiles import operator_zai_profile, _EVIDENCE_HOSTS
from graphite.routing.contracts import ProviderId, Effort

def test_operator_zai_profile_builds_valid_requested_profile():
    assert _EVIDENCE_HOSTS[ProviderId.ZAI] == "docs.z.ai"
    rp = operator_zai_profile(
        model_id="glm-5.2", supported_efforts=(Effort.HIGH,),
        evidence_url="https://docs.z.ai/api-reference/introduction",
        evidence_accessed="2026-07-21",
    )
    assert rp.provider is ProviderId.ZAI
    assert rp.requested_model == "glm-5.2"
    assert rp.supported_efforts == (Effort.HIGH,)

def test_operator_zai_profile_rejects_wrong_evidence_host():
    import pytest
    from graphite.routing.profiles import ProfileError
    with pytest.raises(ProfileError):
        operator_zai_profile(
            model_id="glm-5.2", supported_efforts=(Effort.HIGH,),
            evidence_url="https://openrouter.ai/x", evidence_accessed="2026-07-21",
        )
```

- [ ] **Step 2: Run to verify fail** (`ImportError: operator_zai_profile`). Command mirrors Task 1 Step 2 with these node ids.

- [ ] **Step 3: Implement**

In `profiles.py`, add to `_EVIDENCE_HOSTS` (the dict at ~lines 29-33):
```python
    ProviderId.ZAI: "docs.z.ai",
```
Add the helper next to `operator_openrouter_profile` (~lines 173-187), mirroring it:
```python
def operator_zai_profile(
    *,
    model_id: str,
    supported_efforts: tuple[Effort, ...],
    evidence_url: str,
    evidence_accessed: str,
) -> RequestedProfile:
    """Create an operator-selected z.ai request without claiming key validity."""
    return RequestedProfile(
        ProviderId.ZAI,
        model_id,
        supported_efforts,
        evidence_url,
        evidence_accessed,
    )
```

- [ ] **Step 4: Run to verify pass.** **Step 5: suite sanity + commit** (`feat(zai): profiles evidence host + operator_zai_profile`).

---

### Task 3: Routing store migration v6 → v7 (widen provider CHECK)

**Files:** Modify `src/graphite/routing/storage.py`. Test: `tests/test_routing_storage.py` (extend).

**Interfaces:**
- Consumes: `ProviderId.ZAI` (Task 1) — the DDL literal `'zai'` must match its value.
- Produces: an `events.sqlite3` whose `capability_snapshots` and `cli_execution_attempts` CHECKs admit `'zai'`, at `schema_version` "7".

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routing_storage.py (append)
def test_v6_store_migrates_to_v7_and_admits_zai(tmp_path):
    import sqlite3
    from graphite.routing.storage import RepositoryStore, SCHEMA_VERSION
    root = tmp_path / "repo"
    (root / ".graphite" / "routing").mkdir(parents=True)
    store = RepositoryStore(root)          # fresh -> should be at SCHEMA_VERSION
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
```
(Row-preservation on a *real* v6 DB is covered by the live migration audit in Task 10 and by the existing pre-v6 migration tests, which this task must keep green — see Step 5.)

- [ ] **Step 2: Run to verify fail** (`SCHEMA_VERSION == "6"`).

- [ ] **Step 3: Implement — mirror `_migrate_pre_v6_provider_widening`**

Exact edits in `storage.py`:
1. `SCHEMA_VERSION: Final = "6"` (line 29) → `"7"`.
2. In **both** DDL constants — `_CAPABILITY_SNAPSHOTS_DDL` (the CHECK at line 259) and `_CLI_EXECUTION_ATTEMPTS_DDL` (line 285) — change `CHECK(provider IN ('claude-code', 'codex', 'openrouter'))` to `CHECK(provider IN ('claude-code', 'codex', 'openrouter', 'zai'))`.
3. The accepted-version set (line ~869) `{"1", "2", "3", "4", "5", SCHEMA_VERSION}` → `{"1", "2", "3", "4", "5", "6", SCHEMA_VERSION}` (add the literal `"6"`; SCHEMA_VERSION is now "7"). **Critical:** without adding `"6"`, every existing v6 DB raises `storage_schema_unsupported` at open.
4. Immediately **after** the `self._migrate_pre_v6_provider_widening()` call (~line 859), add `self._migrate_v6_to_v7()`.
5. Add method `_migrate_v6_to_v7`, cloned from `_migrate_pre_v6_provider_widening` (~lines 1097-1177) with exactly these substitutions:
   - docstring → "Rebuild provider-CHECK tables to admit 'zai' (schema v6→v7)."
   - version gate → `if version is None or version[0] != "6": return`
   - idempotency guard → `if all("'zai'" in sql for sql in table_sql.values()): return` (matches how `'openrouter'` is guarded).
   - backup paths → add `_pre_v7_backup_path(self, source_version)` returning `.../backups/f"events-schema-v{source_version}-pre-v7.sqlite3"` and `_pre_v7_backup_marker_path(self, source_version)` returning the `...-pre-v7.sha256.json` sibling (clone the pre-v6 helpers ~835-839); call `self._create_schema_backup(source_version=version[0], backup=self._pre_v7_backup_path(version[0]), marker=self._pre_v7_backup_marker_path(version[0]))`.
   - temp-table suffix → rename every `_pre_v6_copy` f-string to `_pre_v7_copy`.
   - **reuse `_PRE_V6_WIDENED_TABLES` unchanged** (same two tables; their DDLs now carry `'zai'`). Keep the `PRAGMA foreign_keys=OFF`, own-connection-before-`BEGIN IMMEDIATE`, per-table before/after `COUNT(*)` equality (`storage_migration_quarantined` on mismatch), `PRAGMA foreign_key_check`, `COMMIT`, `PRAGMA integrity_check == ("ok",)`, and the `StorageError`/`sqlite3.Error` rollback handling — all identical.

- [ ] **Step 4: Run to verify pass** (fresh store is v7, both DDLs carry `'zai'`).

- [ ] **Step 5: Fix the shared-DDL side-effect tests + suite**

Because the two DDL constants now carry `'zai'`, `_migrate_pre_v6_provider_widening` rebuilds v4/v5 DBs *straight to* zai-admitting tables. Any existing test asserting "v4/v5 migrates to openrouter-exactly / zai rejected" must be updated to accept `'zai'` (it is additive/harmless). Run:
`PYTHONPATH="F:/tmp/graphite-claude-codex-router/src" python -B -m pytest -q -p no:cacheprovider --basetemp "F:/tmp/graphite-zai-t3" tests/test_routing_storage.py`
Expected: all pass (update the side-effect assertions, do NOT weaken row-preservation checks). Commit: `feat(zai): routing store schema v6->v7 widening provider CHECK to admit zai`.

---

### Task 4: Lifecycle store migration v1 → v2 (hand-written rebuild)

**Files:** Modify `src/graphite/routing/lifecycle_storage.py`. Test: `tests/test_routing_lifecycle_storage.py` (extend).

**Interfaces:**
- Consumes: `LifecycleProviderId.ZAI` (Task 1).
- Produces: a `provider-lifecycle.sqlite3` whose `current_observations` and `lifecycle_events` CHECKs admit `(provider='zai' AND runtime_kind='remote-https')`, at `LIFECYCLE_SCHEMA_VERSION` "2", **including on an already-existing v1 DB**.

**Why a rebuild (the trap):** `_SCHEMA` is all `CREATE TABLE IF NOT EXISTS`; `initialize()` version-gates then idempotent-creates and `_validate_schema` only checks table/column names — it never re-applies a CHECK. So editing the DDL alone leaves any existing v1 DB with the old `openrouter`-only CHECK, silently rejecting `zai` with an opaque `lifecycle_storage_unavailable`. The live `provider-lifecycle.sqlite3` already exists at v1, so a real rebuild is mandatory.

- [ ] **Step 1: Write the failing test** (an existing v1 DB must gain zai after open)

```python
# tests/test_routing_lifecycle_storage.py (append)
def test_existing_v1_lifecycle_db_migrates_to_v2_and_admits_zai(tmp_path):
    import sqlite3
    from graphite.routing.lifecycle_storage import LifecycleStore, LIFECYCLE_SCHEMA_VERSION
    root = tmp_path / "repo"
    (root / ".graphite" / "routing").mkdir(parents=True)
    LifecycleStore(root)                      # create at current version
    assert LIFECYCLE_SCHEMA_VERSION == "2"
    db = root / ".graphite" / "routing" / "provider-lifecycle.sqlite3"
    con = sqlite3.connect(db)
    try:
        for table in ("current_observations", "lifecycle_events"):
            ddl = con.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            assert "'zai'" in ddl, table
        sv = con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        assert sv == "2"
        # triggers restored
        trigs = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()]
        assert len(trigs) >= 4
    finally:
        con.close()
```
Add a second test that pre-creates a *simulated* v1 DB (old CHECK, schema_version "1") with one openrouter observation row, opens `LifecycleStore`, and asserts the row survived and a zai observation can now be inserted. (Construct the old-DDL DB directly with sqlite3 in the test to exercise the rebuild path, not just fresh-create.)

- [ ] **Step 2: Run to verify fail** (`LIFECYCLE_SCHEMA_VERSION == "1"`).

- [ ] **Step 3: Implement — DDL widen + version bump + rebuild migration**

1. `_PROVIDER_VALUES` (line 36) → `"'claude-code','codex','ollama','openrouter','zai'"`.
2. In **both** pairing CHECKs — `current_observations` (~lines 168-172) and `lifecycle_events` (~193-197) — add a zai clause to the runtime-kind pairing, e.g. change `OR (provider='openrouter' AND runtime_kind='remote-https')` to `OR (provider IN ('openrouter','zai') AND runtime_kind='remote-https')`.
3. `LIFECYCLE_SCHEMA_VERSION = "1"` (line 32) → `"2"`.
4. Add a migration method `_migrate_v1_to_v2_zai(connection)` and **call it inside `initialize()` BEFORE the version-gate line** (`if version is not None and version[0] != LIFECYCLE_SCHEMA_VERSION: raise ...`, ~line 329). The method:
   - reads `schema_version`; if `None` (fresh, tables not yet created) or already `"2"`, return (fresh installs get the widened CHECK from `_SCHEMA` directly; no rebuild needed).
   - if `"1"`: rebuild `current_observations` then `lifecycle_events` — for each: `CREATE TABLE <name>__v2 (...widened CHECK DDL...)`, `INSERT INTO <name>__v2 SELECT <explicit column list> FROM <name>`, assert `COUNT(*)` equal before/after (else raise `LifecycleStorageError("lifecycle_migration_quarantined")`), `DROP TABLE <name>`, `ALTER TABLE <name>__v2 RENAME TO <name>`. Do this with `PRAGMA foreign_keys=OFF` on its own connection outside any transaction (mirror the routing store's off-transaction rebuild); the two tables have no cross-FK but the triggers must be dropped with the table.
   - recreate the indexes and the **four immutability triggers** (lifecycle_storage.py ~211-226) via the existing `_SCHEMA[1:]` `IF NOT EXISTS` statements (they run right after in `initialize()`), OR re-issue their exact `CREATE ... ` statements in the method. Prefer letting the subsequent `_SCHEMA[1:]` pass recreate indexes+triggers (they are `IF NOT EXISTS` and the tables now exist widened) — verify in the test that ≥4 triggers exist post-migration.
   - `PRAGMA foreign_key_check` (empty) + `PRAGMA integrity_check == ("ok",)`; then upsert `schema_version="2"`.
   - On any `sqlite3.Error`, roll back and raise `LifecycleStorageError("lifecycle_migration_failed")`; do not leave a half-renamed table.

   Source the exact widened DDL for both tables from the (now-edited) `_SCHEMA` constants so the rebuilt tables are byte-identical to fresh installs.

- [ ] **Step 4: Run to verify pass** (fresh + simulated-v1 both migrate, rows preserved, triggers restored).

- [ ] **Step 5: Suite sanity + commit**

Run: `... tests/test_routing_lifecycle_storage.py`. Expected: all pass. Commit: `feat(zai): lifecycle store v1->v2 rebuild migration admitting zai remote-https`.

---

### Task 5: probe_runner z.ai endpoint (additive)

**Files:** Modify `src/graphite/routing/probe_runner.py`. Test: `tests/test_routing_probe_runner.py` (extend).

**Interfaces:**
- Consumes: `LifecycleProviderId.ZAI` (Task 1).
- Produces: `ProbeEndpointPurpose.ZAI_CHAT_COMPLETIONS`, `_ZAI_HOSTS`, the `_PURPOSE_POLICY` row, purpose-set memberships, and an `HttpProbeEndpoint` that accepts `(ZAI, https, api.z.ai, 443, ZAI_CHAT_COMPLETIONS)` — consumed by `execute_zai` (Task 7).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routing_probe_runner.py (append)
def test_zai_chat_completions_endpoint_constructs():
    from graphite.routing.probe_runner import (
        HttpProbeEndpoint, ProbeEndpointPurpose, _ZAI_HOSTS, _PURPOSE_POLICY,
        _BODY_PURPOSES, _INFERENCE_PURPOSES,
    )
    from graphite.routing.lifecycle import LifecycleProviderId
    assert _ZAI_HOSTS == frozenset({"api.z.ai"})
    prov, method, path = _PURPOSE_POLICY[ProbeEndpointPurpose.ZAI_CHAT_COMPLETIONS]
    assert prov is LifecycleProviderId.ZAI and method == "POST" and path == "/api/paas/v4/chat/completions"
    assert ProbeEndpointPurpose.ZAI_CHAT_COMPLETIONS in _BODY_PURPOSES
    assert ProbeEndpointPurpose.ZAI_CHAT_COMPLETIONS in _INFERENCE_PURPOSES
    ep = HttpProbeEndpoint(
        LifecycleProviderId.ZAI, "https", "api.z.ai", 443,
        ProbeEndpointPurpose.ZAI_CHAT_COMPLETIONS)
    assert ep.host == "api.z.ai"

def test_openrouter_endpoint_still_constructs():  # OpenRouter path untouched
    from graphite.routing.probe_runner import HttpProbeEndpoint, ProbeEndpointPurpose
    from graphite.routing.lifecycle import LifecycleProviderId
    HttpProbeEndpoint(LifecycleProviderId.OPENROUTER, "https", "openrouter.ai", 443,
                      ProbeEndpointPurpose.OPENROUTER_CHAT_COMPLETIONS)
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement — additive edits**

1. `_ZAI_HOSTS: Final = frozenset({"api.z.ai"})` next to `_OPENROUTER_HOSTS` (~line 43).
2. Add `ZAI_CHAT_COMPLETIONS = "zai_chat_completions"` to `ProbeEndpointPurpose` (~lines 65-71).
3. `_PURPOSE_POLICY` (~74-105): add `ProbeEndpointPurpose.ZAI_CHAT_COMPLETIONS: (LifecycleProviderId.ZAI, "POST", "/api/paas/v4/chat/completions"),`.
4. Add `ZAI_CHAT_COMPLETIONS` to both `_BODY_PURPOSES` and `_INFERENCE_PURPOSES` (~106-110).
5. In `HttpProbeEndpoint.__post_init__` (~156-163), add a z.ai branch alongside the OpenRouter one — accept when `provider is LifecycleProviderId.ZAI and self.scheme == "https" and self.host in _ZAI_HOSTS and self.port == 443` (mirror the OpenRouter `elif`'s exact structure incl. the `allowed_ollama_ports` guard the sibling branches carry); otherwise the existing `else: raise ProviderProbeError("probe_endpoint_invalid")` stands. Do not alter the OPENROUTER/OLLAMA branches.

- [ ] **Step 4: Run to verify pass** (both z.ai and OpenRouter endpoints construct).

- [ ] **Step 5: Suite sanity + commit** (`... tests/test_routing_probe_runner.py`; commit `feat(zai): probe_runner z.ai chat-completions endpoint (additive)`).

---

### Task 6: `zai_probe.py` — pricing + local preflight

**Files:** Create `src/graphite/routing/zai_probe.py`. Test: `tests/test_routing_zai_probe.py`.

**Interfaces:**
- Consumes: `LifecycleProviderId.ZAI`, `RuntimeKind`, `ProviderRuntimeIdentity` (lifecycle), `ProviderId.ZAI`, `CliIdentity` (contracts).
- Produces: `ZaiPricing`, `zai_cost_microunits`, `ZaiPreflight`, `preflight_zai`, `ZAI_CANONICAL_ENDPOINT`, `ZAI_MODEL`, `_ZAI_MODEL_ID` — consumed by `execute_zai` (Task 7) and the harness (Task 9).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routing_zai_probe.py
import pytest

def test_zai_pricing_and_cost():
    from graphite.routing.zai_probe import ZaiPricing, zai_cost_microunits
    p = ZaiPricing(prompt="0.0000014", completion="0.0000044")
    # 1000 in, 100 out -> (1000*1.4e-6 + 100*4.4e-6)*1e6 = 1400 + 440 = 1840
    assert zai_cost_microunits(p, input_tokens=1000, output_tokens=100) == 1840

def test_preflight_zai_builds_identities_no_routing_digest():
    from graphite.routing.zai_probe import preflight_zai, ZAI_MODEL
    from graphite.routing.contracts import ProviderId
    from graphite.routing.lifecycle import LifecycleProviderId
    pf = preflight_zai(model_id=ZAI_MODEL, observed_at=1_700_000_000, policy_version="1.0.0")
    assert pf.runtime.provider is LifecycleProviderId.ZAI
    assert pf.runtime.routing_policy_digest is None
    assert pf.runtime.model_identity_digest is not None      # required for zai
    assert pf.identity.provider is ProviderId.ZAI
    assert len(pf.identity.executable_sha256) == 64
    assert pf.pricing.prompt == "0.0000014"

def test_preflight_zai_rejects_bad_model_id():
    from graphite.routing.zai_probe import preflight_zai
    with pytest.raises(Exception):
        preflight_zai(model_id="bad model", observed_at=1, policy_version="1.0.0")
```

- [ ] **Step 2: Run to verify fail** (module missing).

- [ ] **Step 3: Implement `zai_probe.py`**

```python
"""Local (no-network) z.ai identity + operator-pinned pricing for governed verification."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .contracts import CliIdentity, ProviderId
from .lifecycle import LifecycleProviderId, ProviderRuntimeIdentity, RuntimeKind
from .probe_runner import ProviderProbeError

ZAI_CANONICAL_ENDPOINT = "https://api.z.ai/api/paas/v4"
ZAI_MODEL = "glm-5.2"
ZAI_API_CONTRACT_VERSION = "1.0.0"
ZAI_ADAPTER_PROTOCOL_VERSION = "1.0.0"
ZAI_CAPABILITIES = ("remote_inference",)
# z.ai model ids are single-segment (no vendor/model slash, unlike OpenRouter).
_ZAI_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PRICE = re.compile(r"^(0|[0-9]{1,10}(\.[0-9]{1,18})?|\.[0-9]{1,18})$")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ZaiPricing:
    """Operator-pinned per-token USD prices as exact decimal strings."""

    prompt: str
    completion: str

    def __post_init__(self) -> None:
        for value in (self.prompt, self.completion):
            if not isinstance(value, str) or len(value) > 64 or _PRICE.fullmatch(value) is None:
                raise ProviderProbeError("probe_protocol_invalid")
            try:
                parsed = Decimal(value)
            except InvalidOperation:
                raise ProviderProbeError("probe_protocol_invalid") from None
            if not Decimal(0) <= parsed <= Decimal(1):
                raise ProviderProbeError("probe_protocol_invalid")

    @property
    def digest(self) -> str:
        return _digest({"completion": self.completion, "prompt": self.prompt})


def zai_cost_microunits(pricing: ZaiPricing, *, input_tokens: int, output_tokens: int) -> int:
    """Ceiling of the exact-decimal USD cost in microunits."""
    if not isinstance(pricing, ZaiPricing):
        raise ProviderProbeError("probe_request_invalid")
    for value in (input_tokens, output_tokens):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100_000_000:
            raise ProviderProbeError("probe_request_invalid")
    cost = (
        Decimal(input_tokens) * Decimal(pricing.prompt)
        + Decimal(output_tokens) * Decimal(pricing.completion)
    ) * Decimal(1_000_000)
    whole = int(cost)
    return whole if cost == whole else whole + 1


@dataclass(frozen=True, slots=True)
class ZaiPreflight:
    identity: CliIdentity
    runtime: ProviderRuntimeIdentity
    pricing: ZaiPricing


def preflight_zai(*, model_id: str, observed_at: int, policy_version: str) -> ZaiPreflight:
    """Construct the z.ai runtime + CLI identity and pinned pricing locally (no network)."""
    if not isinstance(model_id, str) or _ZAI_MODEL_ID.fullmatch(model_id) is None:
        raise ProviderProbeError("probe_request_invalid")
    pricing = ZaiPricing(prompt="0.0000014", completion="0.0000044")
    endpoint_digest = hashlib.sha256(ZAI_CANONICAL_ENDPOINT.encode("ascii")).hexdigest()
    model_digest = _digest(model_id)
    try:
        runtime = ProviderRuntimeIdentity(
            LifecycleProviderId.ZAI,
            RuntimeKind.REMOTE_HTTPS,
            ZAI_API_CONTRACT_VERSION,
            endpoint_digest,
            model_digest,
            None,                       # routing_policy_digest forbidden for zai
            ZAI_CAPABILITIES,
            policy_version,
            observed_at,
        )
    except ValueError:
        raise ProviderProbeError("probe_request_invalid") from None
    composite = _digest(
        {"endpoint": endpoint_digest, "model": model_digest, "pricing": pricing.digest}
    )
    identity = CliIdentity(
        ProviderId.ZAI,
        composite,
        ZAI_API_CONTRACT_VERSION,
        ZAI_ADAPTER_PROTOCOL_VERSION,
    )
    return ZaiPreflight(identity, runtime, pricing)
```

- [ ] **Step 4: Run to verify pass.** **Step 5: suite sanity + commit** (`feat(zai): zai_probe local preflight + operator-pinned pricing`).

---

### Task 7: `zai_executor.py` — one bounded plain-text completion

**Files:** Create `src/graphite/routing/zai_executor.py`. Test: `tests/test_routing_zai_executor.py`.

**Interfaces:**
- Consumes: `ZaiPricing`, `zai_cost_microunits` (Task 6); `run_http_probe`, `HttpProbeEndpoint`, `ProbeEndpointPurpose`, `HttpProbeResult`, `ProviderProbeError` (probe_runner); `LifecycleProviderId.ZAI` (Task 1); `AdapterError` (claude_executor).
- Produces: `execute_zai(...) -> ZaiExecutionResult` (plain-text `message`) — consumed by the harness (Task 9).

- [ ] **Step 1: Write the failing test (fake transport, both success + failure)**

```python
# tests/test_routing_zai_executor.py
import hashlib, json, pytest
from graphite.routing.probe_runner import HttpProbeResult, ProviderProbeError
from graphite.routing.zai_probe import ZaiPricing

PRICING = ZaiPricing(prompt="0.0000014", completion="0.0000044")

def _envelope(content, model="glm-5.2", pin=420, cout=6):
    body = json.dumps({
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": pin, "completion_tokens": cout},
    }).encode()
    return HttpProbeResult(200, body, hashlib.sha256(body).hexdigest(), 0.05)

def test_execute_zai_returns_plaintext_and_usage():
    from graphite.routing.zai_executor import execute_zai
    seen = {}
    def transport(**kw):
        seen.update(kw); return _envelope("GRAPHITE_PROFILE_OK")
    result = execute_zai(
        api_key="k", prompt=b"return GRAPHITE_PROFILE_OK", requested_model="glm-5.2",
        expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
        max_cost_microunits=100000, timeout_seconds=60.0, transport=transport)
    assert result.message == "GRAPHITE_PROFILE_OK"        # verbatim, NOT json-parsed
    assert result.input_tokens == 420 and result.output_tokens == 6
    body = json.loads(seen["request_body"])
    assert body == {"max_tokens": 64, "messages": [{"content": "return GRAPHITE_PROFILE_OK", "role": "user"}], "model": "glm-5.2", "stream": False, "temperature": 0}
    assert seen["authorization"] == "Bearer k"

def test_execute_zai_cost_ceiling():
    from graphite.routing.zai_executor import execute_zai
    from graphite.routing.claude_executor import AdapterError
    with pytest.raises(AdapterError) as e:
        execute_zai(api_key="k", prompt=b"x", requested_model="glm-5.2",
            expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
            max_cost_microunits=1, timeout_seconds=60.0,
            transport=lambda **kw: _envelope("GRAPHITE_PROFILE_OK", cout=1000))
    assert e.value.code == "cost_ceiling_exceeded"

def test_execute_zai_maps_transport_failure():
    from graphite.routing.zai_executor import execute_zai
    from graphite.routing.claude_executor import AdapterError
    def transport(**kw): raise ProviderProbeError("probe_http_status")
    with pytest.raises(AdapterError) as e:
        execute_zai(api_key="k", prompt=b"x", requested_model="glm-5.2",
            expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
            max_cost_microunits=100000, timeout_seconds=60.0, transport=transport)
    assert e.value.code == "http_status"
```

- [ ] **Step 2: Run to verify fail** (module missing).

- [ ] **Step 3: Implement `zai_executor.py`** (adapted from `execute_openrouter`; plain-text, no schema/reasoning/usage-opt-in)

```python
"""Isolated hardened adapter for one bounded z.ai chat completion (plain text)."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

from .claude_executor import AdapterError
from .lifecycle import LifecycleProviderId
from .probe_runner import (
    MAX_INFERENCE_REQUEST_BYTES, MAX_INFERENCE_RESPONSE_BYTES, MAX_INFERENCE_TIMEOUT_SECONDS,
    HttpProbe, HttpProbeEndpoint, HttpProbeResult, ProbeEndpointPurpose, ProviderProbeError,
    run_http_probe,
)
from .zai_probe import ZaiPricing, zai_cost_microunits, ZAI_HOST

MAX_TOKEN_COUNT = 10_000_000
MAX_COST_MICROUNITS = 1_000_000_000

_EXECUTION_TRANSPORT_CODES = {
    "probe_response_limit": "response_limit",
    "probe_timeout": "timeout",
    "probe_http_status": "http_status",
    "probe_redirect_rejected": "http_status",
}


@dataclass(frozen=True, slots=True)
class ZaiExecutionResult:
    effective_model: str
    message: str = field(repr=False)
    input_tokens: int
    output_tokens: int
    cost_microunits: int
    duration_seconds: float
    request_sha256: str
    response_sha256: str


def _model_name(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value) > 128
            or any(character.isspace() for character in value)):
        raise AdapterError("request_invalid")
    return value


def _token(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKEN_COUNT:
        raise AdapterError("protocol")
    return value


def execute_zai(
    *,
    api_key: str,
    prompt: bytes,
    requested_model: str,
    expected_effective_model: str,
    pricing: ZaiPricing,
    max_output_tokens: int,
    max_cost_microunits: int,
    timeout_seconds: float,
    transport: HttpProbe = run_http_probe,
) -> ZaiExecutionResult:
    """Perform exactly one bounded z.ai chat completion returning plain text; no retries."""
    if not isinstance(api_key, str) or not api_key:
        raise AdapterError("auth_required")
    if len(api_key) > 4096 or any(character in api_key for character in "\r\n\x00"):
        raise AdapterError("request_invalid")
    if not isinstance(prompt, bytes) or not prompt or len(prompt) > MAX_INFERENCE_REQUEST_BYTES:
        raise AdapterError("request_invalid")
    try:
        prompt_text = prompt.decode("utf-8")
    except UnicodeDecodeError:
        raise AdapterError("request_invalid") from None
    requested = _model_name(requested_model)
    expected = _model_name(expected_effective_model)
    if not isinstance(pricing, ZaiPricing):
        raise AdapterError("request_invalid")
    for value, maximum in ((max_output_tokens, MAX_TOKEN_COUNT), (max_cost_microunits, MAX_COST_MICROUNITS)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise AdapterError("request_invalid")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0.1 <= timeout_seconds <= MAX_INFERENCE_TIMEOUT_SECONDS):
        raise AdapterError("request_invalid")
    payload = {
        "max_tokens": max_output_tokens,
        "messages": [{"content": prompt_text, "role": "user"}],
        "model": requested,
        "stream": False,
        "temperature": 0,
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(body) > MAX_INFERENCE_REQUEST_BYTES:
        raise AdapterError("request_invalid")
    endpoint = HttpProbeEndpoint(
        LifecycleProviderId.ZAI, "https", ZAI_HOST, 443,
        ProbeEndpointPurpose.ZAI_CHAT_COMPLETIONS,
    )
    try:
        result = transport(
            endpoint=endpoint, timeout_seconds=timeout_seconds, request_body=body,
            authorization=f"Bearer {api_key}", max_response_bytes=MAX_INFERENCE_RESPONSE_BYTES,
        )
    except ProviderProbeError as error:
        raise AdapterError(_EXECUTION_TRANSPORT_CODES.get(error.code, "unavailable")) from None
    except Exception:
        raise AdapterError("unavailable") from None
    if not isinstance(result, HttpProbeResult):
        raise AdapterError("unavailable")
    try:
        envelope = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise AdapterError("unavailable") from None
    if not isinstance(envelope, dict):
        raise AdapterError("protocol")
    reported_model = envelope.get("model")
    if reported_model is not None:
        if not isinstance(reported_model, str):
            raise AdapterError("protocol")
        if reported_model != expected:
            raise AdapterError("model_mismatch")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise AdapterError("protocol")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise AdapterError("protocol")
    content = message.get("content")
    if not isinstance(content, str):
        raise AdapterError("protocol")
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        raise AdapterError("protocol")
    input_tokens = _token(usage.get("prompt_tokens"))
    output_tokens = _token(usage.get("completion_tokens"))
    try:
        cost = zai_cost_microunits(pricing, input_tokens=input_tokens, output_tokens=output_tokens)
    except ProviderProbeError:
        raise AdapterError("protocol") from None
    if cost > max_cost_microunits:
        raise AdapterError("cost_ceiling_exceeded")
    return ZaiExecutionResult(
        expected, content, input_tokens, output_tokens, cost,
        result.duration_seconds, hashlib.sha256(body).hexdigest(),
        hashlib.sha256(result.body).hexdigest(),
    )
```
Note: `ZAI_HOST` must be exported from `zai_probe.py` (add `ZAI_HOST = "api.z.ai"` there in Task 6, or import from a shared constant). Add it to Task 6's module if not already present.

- [ ] **Step 4: Run to verify pass.** **Step 5: suite sanity + commit** (`feat(zai): zai_executor one bounded plain-text completion`).

---

### Task 8: Verification wiring — `_ZAI_POLICY`

**Files:** Modify the module that defines `_OPENROUTER_POLICY` (locate in Step 1). Test: `tests/test_routing_lifecycle_service.py` (extend) or the profiles test.

**Interfaces:**
- Consumes: `ZAI_CAPABILITIES` (Task 6), `ProviderRuntimeIdentity` for zai.
- Produces: `_ZAI_POLICY` such that `_ZAI_POLICY.supports(<zai runtime identity>)` is True (so `observe()` promotes DISCOVERED→VERIFICATION_REQUIRED).

- [ ] **Step 1: Locate `_OPENROUTER_POLICY`**

Run: `grep -rn "_OPENROUTER_POLICY" src/graphite/routing/`. Read its definition and its type (a compatibility policy pinning `required_capabilities` + a `[min_version, max_version_exclusive)` range + `policy_version`). Note the exact constructor/type name.

- [ ] **Step 2: Write the failing test**

```python
def test_zai_policy_supports_zai_identity():
    from graphite.routing.<module> import _ZAI_POLICY
    from graphite.routing.zai_probe import preflight_zai, ZAI_MODEL
    pf = preflight_zai(model_id=ZAI_MODEL, observed_at=1_700_000_000, policy_version="1.0.0")
    assert _ZAI_POLICY.supports(pf.runtime) is True
```
(Use the exact `supports` API discovered in Step 1; if `supports` is not the method name, match the real one.)

- [ ] **Step 3: Implement** — define `_ZAI_POLICY` by cloning `_OPENROUTER_POLICY` with `required_capabilities=ZAI_CAPABILITIES` (`("remote_inference",)`, matching the identity the probe emits) and a version range that contains `"1.0.0"` (e.g. min `"1.0.0"`, max_exclusive `"2.0.0"`), `policy_version="1.0.0"`. Keep everything else identical to the OpenRouter policy shape.

- [ ] **Step 4: Run to verify pass.** **Step 5: suite sanity + commit** (`feat(zai): _ZAI_POLICY for lifecycle verification promotion`).

---

### Task 9: Verification harness pair (offline-buildable)

**Files:** Create `F:\tmp\graphite-live-acceptance-harness\_prepare_zai_glm52_verification.py`, `_execute_zai_glm52_verification.py`. Test (scratchpad): `dryrun_zai_verification.py`.

**Interfaces:**
- Consumes: all of the above (`preflight_zai`, `execute_zai`, `operator_zai_profile`, `_ZAI_POLICY`, `verify_and_save_approved_profile`, `ProviderLifecycleService`).
- Produces: `BUNDLE` + `BUNDLE_DIGEST`; one sanitized JSON receipt.

- [ ] **Step 1: Write `_prepare_zai_glm52_verification.py`**

A manifest module mirroring `_prepare_openrouter_verification_r3.py`'s BUNDLE shape, with these pins: `purpose "graphite_zai_glm52_verification"`; `provider "zai"`, `slug "glm-5.2"`; `endpoint ZAI_CANONICAL_ENDPOINT`; `evidence_url "https://docs.z.ai/api-reference/introduction"`, `evidence_accessed "2026-07-21"`; `pricing {"prompt":"0.0000014","completion":"0.0000044"}`; `max_output_tokens` (small, e.g. 64), `max_input_tokens` (e.g. 256), `max_cost_microunits` (e.g. 64000), `timeout_seconds 60.0`, `ttl_seconds 86400`; `verification_contract {"exact_text":"GRAPHITE_PROFILE_OK","encoding":"utf-8"}`; `expected_capabilities ("code","reasoning")` (the `VerificationEvidence.capabilities` to record — a real, unique, lowercase tuple), `context_window_tokens 200000`, `risk_ceiling "high"`; `ISSUED_AT` / `EXPIRES_AT` (now+window); `IMPLEMENTATION_COMMIT` (run-time pin, Task 11 Step 1); the live store SHA256 pins + `EXISTING_STORE_CONTRACT` (12/12/23) + `EXPECTED_FINAL_STORE_CONTRACT` (**13/13/25**); `credential_source "session_environment:ZAI_API_KEY"`, `credential_in_argv False`, `forbidden_persistence` (the standard list), `raw_provider_output_persistence False`, `live_inference True`, `store_write True`, `merge/push/deploy False`. `BUNDLE_DIGEST = digest(BUNDLE)`.

- [ ] **Step 2: Write `_execute_zai_glm52_verification.py`** — the governed verification (the 9-step sequence)

Key wiring (fail-closed `HarnessFailure` throughout; sanitized receipt):
```python
# preflight gates: --approved == BUNDLE_DIGEST; now < EXPIRES_AT; digest(BUNDLE)==BUNDLE_DIGEST;
#   file_sha256(routing)/file_sha256(lifecycle) == pins; store_audit()==EXISTING_STORE_CONTRACT (12/12/23);
#   git HEAD==IMPLEMENTATION_COMMIT and clean (feature worktree); api_key=os.environ["ZAI_API_KEY"] or credential_missing.
routing_store = RepositoryStore(FIXTURE); lifecycle_store = LifecycleStore(FIXTURE)
service = ProviderLifecycleService(lifecycle_store, routing_store)
boundary = digest({"fixture": FIXTURE_COMMIT, "provider": "zai", "model": SLUG, "runtime": "remote-https"})
preflight = preflight_zai(model_id=SLUG, observed_at=ISSUED_AT, policy_version=POLICY_VERSION)
observed = service.observe(boundary, identity=preflight.runtime, policy=_ZAI_POLICY,
                           standard_probe_passed=True, expanded_probe_passed=True)
if observed.state is not ProviderLifecycleState.VERIFICATION_REQUIRED:
    raise HarnessFailure("profile_verification_usage_invalid", state=observed.state.value)
result = execute_zai(api_key=api_key, prompt=VERIFY_PROMPT, requested_model=SLUG,
    expected_effective_model=SLUG, pricing=ZaiPricing(**PRICING),
    max_output_tokens=MAX_OUTPUT_TOKENS, max_cost_microunits=MAX_COST_MICROUNITS,
    timeout_seconds=TIMEOUT_SECONDS)
if result.message.strip() != "GRAPHITE_PROFILE_OK":
    raise HarnessFailure("profile_verification_invalid")
requested_profile = operator_zai_profile(model_id=SLUG, supported_efforts=(Effort.HIGH,),
    evidence_url=EVIDENCE_URL, evidence_accessed=EVIDENCE_ACCESSED)
snapshot = verify_and_save_approved_profile(routing_store, requested=requested_profile,
    identity=preflight.identity, effort=Effort.HIGH, verified_at=ISSUED_AT, ttl_seconds=TTL_SECONDS,
    approval_granted=True, max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
    verifier=lambda *_: VerificationEvidence(result.effective_model, EXPECTED_CAPABILITIES,
        CONTEXT_WINDOW_TOKENS, RiskTier(RISK_CEILING), result.input_tokens, result.output_tokens),
    lifecycle_identity_digest=preflight.runtime.digest)
service.activate(boundary, lifecycle_identity_digest=preflight.runtime.digest,
    capability_snapshot_digest=snapshot.digest, activated_at=ISSUED_AT)
# telemetry: 2x record_cli_telemetry (machine MACHINE_VERIFIED + human ACCEPTED), category ISOLATED_CODE
#   risk per policy, mirroring _execute_openrouter_verification_r3's record_profile_telemetry.
# final audit: capability_snapshots 13, lifecycle_snapshot_bindings 13, cli_telemetry_events 25, integrity ok.
```
The `record_profile_telemetry` helper (2 rows) and the `HarnessFailure`/`store_audit`/`file_sha256` scaffolding are transcribed from `_execute_openrouter_verification_r3.py` (same structure), substituting the z.ai preflight/executor/profile and the `zai` provider. **TWO-IDENTITY discipline:** `identity=preflight.identity` (CliIdentity → snapshot); `lifecycle_identity_digest=preflight.runtime.digest` (the binding + activate target). Never swap.

- [ ] **Step 3: Offline dry-run** (`dryrun_zai_verification.py`, against store **copies**, fake transport)

Copy `events.sqlite3` + `provider-lifecycle.sqlite3` to a workdir; point `ex.FIXTURE`/paths at the copy; stub `ex.execute_zai` (or inject a fake transport returning a canned `GRAPHITE_PROFILE_OK` envelope) so no network; run `ex.main()` with `--approved <digest>`; assert `status:passed`, the boundary reaches ACTIVE, snapshot created, and the copy's audit is 13/13/25. **Note:** the copy must itself be migrated (v7/v2) first — either run the migration harness (Task 10) against the copy, or open `RepositoryStore`/`LifecycleStore` on the copy (which auto-migrates) before the verification. Assert the migration on the copy preserves 12/12/23 then verification advances to 13/13/25.

- [ ] **Step 4: Suite sanity** (all `tests/` green) + **Step 5: no repo commit** (harness lives outside the repo).

---

### Task 10: Governed live schema migration (operator-executed)

The migrations auto-run when `RepositoryStore(FIXTURE)` / `LifecycleStore(FIXTURE)` open the live stores — so this step is a **dedicated, manifest-gated migration harness** that opens the stores once and audits, rather than letting an incidental open mutate them.

- [ ] **Step 1** Author `_prepare_zai_schema_migration.py` / `_execute_zai_schema_migration.py`: purpose `graphite_zai_schema_migration`; pins the pre-migration store SHA256s + `EXISTING_STORE_CONTRACT` (12/12/23) + `EXPECTED_FINAL_STORE_CONTRACT` (12/12/23 — **zero row changes**, schema_version routing 6→7 / lifecycle 1→2 only); `IMPLEMENTATION_COMMIT` = clean feature HEAD; `store_write True`, `live_inference False`, `network False`. The execute: preflight gates → open `RepositoryStore(FIXTURE)` + `LifecycleStore(FIXTURE)` (triggers both migrations) → assert routing `schema_version=="7"` and lifecycle `schema_version=="2"`, both CHECKs now carry `'zai'`, audit **12/12/23 unchanged**, integrity ok, 0 FK, and the automatic backups exist. Print a sanitized receipt.
- [ ] **Step 2** Offline dry-run against store copies: both migrations apply, 12/12/23 preserved, `'zai'` admitted, triggers/indexes intact.
- [ ] **Step 3** Re-pin `IMPLEMENTATION_COMMIT` = current clean feature HEAD; confirm live store hashes; display manifest; operator approves `Approved: graphite_zai_schema_migration bundle <digest>`; operator runs the execute via `!` (no key needed — no inference). Capture receipt; confirm routing v7 / lifecycle v2 / 12/12/23 / integrity ok; record the NEW post-migration store hashes as the pinned baseline for Task 11.

---

### Task 11: Governed live GLM 5.2 verification (operator-executed)

**Prerequisite:** Task 10 applied (stores at v7/v2). `ZAI_API_KEY` is *already* ambient — the session was restarted once up front (the Global-Constraints prerequisite), so **no restart happens between the migration and this step.** The harness reads the key in its **preflight gate** (`os.environ["ZAI_API_KEY"]` or `credential_missing`), *before* any store write, so a `credential_missing` here mutates nothing and leaves the manifest unspent.

- [ ] **Step 1** Re-pin `_prepare_zai_glm52_verification.py`: `IMPLEMENTATION_COMMIT` = current clean HEAD; `ROUTING_STORE_SHA256`/`LIFECYCLE_STORE_SHA256` = the post-Task-10 hashes; `EXISTING_STORE_CONTRACT` 12/12/23; `EXPECTED_FINAL_STORE_CONTRACT` **13/13/25**; confirm `now < EXPIRES_AT`. Re-run Task 9 dry-run.
- [ ] **Step 2** Display the manifest: purpose `graphite_zai_glm52_verification`, provider `zai`, model `glm-5.2`, endpoint, evidence host `docs.z.ai`, pinned pricing, cost ceiling, `live_inference:true`, `store_write:true`, `expected_mutation` (snapshots 12→13, bindings 12→13, telemetry 23→25), and `BUNDLE_DIGEST`. State plainly: **live inference, mutates the store, spends budget.** Wait for `Approved: graphite_zai_glm52_verification bundle <BUNDLE_DIGEST>`.
- [ ] **Step 3** Operator runs the execute via `!` **with no inline `ZAI_API_KEY`** (ambient env, post-restart):
```bash
PYTHONPATH="F:/tmp/graphite-claude-codex-router/src;F:/tmp/graphite-live-acceptance-harness" python "F:/tmp/graphite-live-acceptance-harness/_execute_zai_glm52_verification.py" --approved <BUNDLE_DIGEST>
```
Expected receipt: `status:passed`, verification `GRAPHITE_PROFILE_OK`, boundary ACTIVE, snapshot promoted, tokens/cost within ceiling, audit 13/13/25, integrity ok. If `credential_missing` → the session was not started with `ZAI_API_KEY` ambient. Because the credential check is a preflight gate, **nothing was mutated and the manifest is unspent** — **restart Claude Code and re-run the round** (re-run with the same `--approved <BUNDLE_DIGEST>` if still within the manifest window; re-pin + re-approve if it expired). Do **not** paste an inline `ZAI_API_KEY=...` prefix — an inline value shadows the ambient key and reintroduces the shadowing trap that burned approvals on the OpenRouter track. If `http_status`/`response_limit`/`unavailable`/`model_mismatch` → record the diagnostic (this is the real answer to "does GLM 5.2 work via z.ai"); do not weaken the oracle; a retry is a fresh governed round.
- [ ] **Step 4** Post-run: re-hash both stores (they differ now), audit 13/13/25, confirm the boundary is ACTIVE and the snapshot is loadable; record the new baseline hashes.

---

### Task 12: Record evidence and commit

**Files:** Modify `docs/superpowers/implementation-notes/2026-07-19-provider-lifecycle-evidence.md`.

- [ ] **Step 1** Append a z.ai section: the provider foundation added (enums, two migrations v7/v2, isolated adapter, probe_runner extension), the sanitized verification receipt (verdict `GRAPHITE_PROFILE_OK`, tokens, cost), the promoted READ_ONLY verification snapshot (provider `zai`, model `glm-5.2`) + ACTIVE lifecycle identity, the store transition (12/12/23 → migration 12/12/23 → verification 13/13/25), pre/post store hashes, and the statement that exactly one live inference ran, no OpenRouter code was modified, and main was untouched.
- [ ] **Step 2** Commit: `docs: record z.ai native provider + GLM 5.2 verification evidence` (Co-Authored-By trailer). Feature worktree clean. No push/merge.

---

## Self-Review Notes

- **Spec coverage:** provider enums + identity → Task 1; evidence host + profile → Task 2; routing v7 → Task 3; lifecycle v2 rebuild → Task 4; probe_runner endpoint → Task 5; pricing + preflight → Task 6; executor → Task 7; policy → Task 8; harness → Task 9; governed migration → Task 10; governed verification → Task 11; evidence → Task 12. The spec's schema-migration (§1) is split into Tasks 3+4 because the two stores differ (routing has a framework; lifecycle needs a hand-written rebuild — a trap the research surfaced). Store end-state corrected 13/13/24 → **13/13/25** (+2 telemetry, matching the OpenRouter precedent).
- **Placeholder scan:** `IMPLEMENTATION_COMMIT` + `<BUNDLE_DIGEST>` + the API key are genuine run-time values. Task 8 Step 1 is an explicit *locate* step (the `_OPENROUTER_POLICY` module/type must be read before mirroring) — flagged, not a hidden gap. No `TODO`/`TBD`.
- **Type consistency:** `ZaiPricing`/`zai_cost_microunits`/`ZaiPreflight`/`preflight_zai`/`ZaiExecutionResult`/`execute_zai`/`operator_zai_profile`/`_ZAI_POLICY`/`ZAI_CAPABILITIES`/`ZAI_HOST` names are consistent across tasks; `verify_and_save_approved_profile`, `VerificationEvidence`, `ProviderRuntimeIdentity`, `CliIdentity`, `HttpProbeEndpoint`, `ProbeEndpointPurpose.ZAI_CHAT_COMPLETIONS` match the researched signatures. Provider wire value is `"zai"` in both enums and the SQL CHECKs.
- **The load-bearing traps (all addressed):** `_EVIDENCE_HOSTS[ZAI]` unguarded KeyError (Task 2); `_PROVIDER_RUNTIME_KINDS[ZAI]` fail-closed map (Task 1); routing allow-list must add `"6"` (Task 3); lifecycle idempotent-create trap → real rebuild (Task 4); `probe_runner` host/purpose double-check (Task 5); two-identity discipline (Task 9); observe→VERIFICATION_REQUIRED requires policy/identity capability agreement (Tasks 6+8).

## Success Criteria

- `zai` admitted by both stores (routing v7 / lifecycle v2), additively, with 12/12/23 preserved through migration.
- One live `glm-5.2` verification returns `GRAPHITE_PROFILE_OK` within the pinned cost ceiling; a READ_ONLY verification capability snapshot (provider `zai`) is promoted and bound to a new ACTIVE lifecycle identity; telemetry +2 (store 13/13/25).
- Full existing suite green (OpenRouter adapter byte-untouched); sanitized receipts; evidence committed on `feat/claude-codex-router`.

## Out of Scope (later specs)

- GLM 5.2 edit-promotion (json_object edit-smoke — pending confirmation z.ai supports structured output), review, pool registration, routed smokes.
- The other z.ai models; the three unverified OpenRouter models.
- A shared OpenAI-compatible executor core refactor across OpenRouter + z.ai.
- Branch integration (push/merge of `feat/claude-codex-router`).

## Execution Handoff

**Prerequisite (before Task 1): restart Claude Code once** so the session inherits `ZAI_API_KEY` (the User-scope var a session started earlier cannot see). The **first action in the fresh session is a presence gate** — confirm `ZAI_API_KEY` is set in the process env (boolean + length only; the value is never printed). If it is absent, the restart did not take: fix it before building, do not start Task 1. With the key ambient from the start, the offline build (Tasks 1–9), the live migration (Task 10), and the live verification (Task 11) all run in one uninterrupted session — the restart is *not* deferred to between the two live steps.

Plan complete. Two execution options:
1. **Subagent-Driven (recommended)** — fresh subagent per task + adversarial verification (as the review-smoke build ran), stopping before the two governed live steps (Tasks 10–11) which the operator runs via `!`.
2. **Inline Execution** — task-by-task in-session with checkpoints.
