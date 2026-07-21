# z.ai Native Provider Foundation + GLM 5.2 Verification

**Status:** approved design, 2026-07-21.
**Context:** Extends graphite's governed provider set (`claude-code`, `codex`,
`ollama`, `openrouter`) with a first **native z.ai** provider. It is the z.ai
analog of where the OpenRouter track began — reach a model and promote a
verification capability snapshot — and is scoped to that milestone only.
Prior art: `docs/superpowers/specs/2026-07-20-openrouter-development-participation-design.md`
and the OpenRouter live-acceptance evidence in
`docs/superpowers/implementation-notes/2026-07-19-provider-lifecycle-evidence.md`.

## Goal

Add z.ai as a first-class governed remote provider and prove **GLM 5.2**
(`glm-5.2`) reachable through z.ai's own OpenAI-compatible API with the operator's
z.ai key: one bounded verification inference promotes a READ_ONLY **verification
capability snapshot** (provider `zai`, model `glm-5.2`) plus an active lifecycle
identity and one telemetry row — the same milestone the OpenRouter track first
landed. This retires two risks together: that z.ai + the key + `glm-5.2` actually
respond, and that graphite's provider machinery admits a new remote provider.

Edit-promotion, review, pool registration, and routed smokes are **out of scope**
(later specs), as are the other z.ai models. GLM 5.2 previously failed only via
**OpenRouter** (`unavailable` — an availability failure, never auth); routing it
straight to z.ai bypasses that path entirely.

## Background — what exists, and how z.ai diverges

**Already built and reusable (provider-agnostic):**
- `profiles.verify_and_save_approved_profile(...)` invokes one approved read-only
  adapter verification and, on success, creates + saves a READ_ONLY
  `CapabilitySnapshot` and records telemetry. `verify_approved_profile` /
  `create_capability_snapshot` / `save_capability_snapshot` /
  `load_verified_capability_snapshots` are all keyed on a `ProviderRuntimeIdentity`
  and an adapter verification result, not on OpenRouter specifically.
- The lifecycle/identity/binding machinery (`lifecycle.py`,
  `lifecycle_storage.py`, `ProviderLifecycleService`) already models multiple
  providers and remote-https runtimes.
- A **direct migration precedent**: `RepositoryStore._migrate_pre_v6_provider_widening`
  (storage.py:1097) rebuilt the affected tables with a widened `provider` CHECK to
  admit `openrouter` at schema v5→v6, additively and with zero row changes. The
  v7 migration mirrors it.

**What z.ai lacks vs OpenRouter (the real divergences):**
- **No catalog pricing.** OpenRouter's `/models` returns exact per-token prices
  that `observe_openrouter_with_pricing` binds into the cost ceiling. z.ai
  publishes rates on its site but exposes no equivalent per-token catalog feed, so
  pricing must be **operator-pinned** in the manifest.
- **No confirmed non-inference observation endpoint.** OpenRouter's probe hits an
  auth-health endpoint + `/models` to observe identity without inference. z.ai's
  documented surface is the OpenAI-compatible `/chat/completions`; a `GET /models`
  list endpoint is **not documented** (to be confirmed at build with a throwaway
  probe). So z.ai's observation is thinner and the first hard reachability signal
  is the verification inference itself.
- **No confirmed `response_format` / JSON-object mode.** So verification uses a
  **plain-text const** contract, not structured output — matching the existing
  Codex verification precedent (`"Return exactly GRAPHITE_PROFILE_OK"`).

## Approved design decisions (operator, 2026-07-21)

1. **Full native z.ai integration** (not a throwaway connectivity smoke).
2. **Spec #1 stops at verification** — provider foundation + GLM 5.2 verification
   snapshot; edit/review/pool deferred to later specs.
3. **Isolated new adapter** (`zai_probe.py` + `zai_executor.py`); the OpenRouter
   adapter is left **byte-untouched**. Isolation over DRY for this first
   increment (the OpenRouter path just passed full live acceptance); a shared
   OpenAI-compatible core is a later refactor once z.ai needs the full edit
   executor.
4. **Operator-pinned pricing** from z.ai's published GLM 5.2 rates.
5. **Plain-text const verification** (`GRAPHITE_PROFILE_OK`), dodging z.ai's
   undocumented JSON mode.

## Design

### 1. Provider identity + schema migration (v6 → v7)

- `contracts.ProviderId.ZAI = "zai"`; `lifecycle.LifecycleProviderId.ZAI = "zai"`.
- `lifecycle.py`: map `ZAI → RuntimeKind.REMOTE_HTTPS`; treat z.ai like
  Claude/Codex for routing — **`routing_digest` is `None`** (z.ai has no
  OpenRouter-style routing policy; only OpenRouter carries a `routing_policy_digest`).
- **Widen every provider CHECK/validator sibling** (the OpenRouter widening
  taught these come in pairs; miss one and a write aborts far from the cause):
  - `storage.py:259` and `storage.py:285` — add `'zai'` to
    `provider IN ('claude-code','codex','openrouter')` (both tables).
  - `lifecycle_storage.py:36` — add `'zai'` to `_PROVIDER_VALUES`.
  - `lifecycle_storage.py:169–171` and `194–196` — admit
    `(provider='zai' AND runtime_kind='remote-https')` in both runtime-kind
    pairing clauses (e.g. fold into the existing `openrouter` remote-https clause).
  - No SQL provider CHECK exists on `cli_telemetry_events`; the `ProviderId` enum
    guards telemetry, so the enum member covers it. The plan must still audit for
    any other sibling (`registry.py`, `service.py`, model-id validators) before
    trusting the set.
- **Migration `_migrate_v6_to_v7_zai_provider`** mirrors
  `_migrate_pre_v6_provider_widening`: rebuild the CHECK-constrained tables with
  the widened CHECK, **additive and backward-compatible** — all existing rows
  (12 capability_snapshots / 12 lifecycle_snapshot_bindings / 23 cli_telemetry_events
  on the live fixture) preserved, `schema_meta.schema_version` 6 → 7. The migration
  is a **deliberate, governed step** applied to the live store, not a side effect
  of an offline dry-run (dry-runs migrate store *copies*).

### 2. z.ai adapter — observation + executor (`zai_probe.py`, `zai_executor.py`)

- Canonical endpoint constant `https://api.z.ai/api/paas/v4` (OpenAI-compatible);
  chat completions at `…/chat/completions`; `Authorization: Bearer <key>`; model
  `glm-5.2`.
- **Observation:** construct a `ProviderRuntimeIdentity(provider=zai,
  runtime_kind=remote-https, api_contract_version, endpoint-hash,
  model-id-digest, routing_digest=None, capabilities, policy_version, observed_at)`
  from pinned inputs. If z.ai turns out to expose a non-inference `GET …/models`
  (confirmed at build), use it for a cheap auth-health/existence check before
  spending the verification inference; otherwise the verification inference is the
  reachability proof. The observation attests only what it actually checked.
- **Executor (verification only for this spec):** a bounded OpenAI-compatible
  `/chat/completions` POST — messages array, `temperature 0`, small
  `max_tokens` — parsing `choices[0].message.content` and
  `usage.prompt_tokens`/`completion_tokens`, with a **hard cost ceiling** enforced
  from the operator-pinned pricing (same exact-decimal → microunit math as
  `completion_cost_microunits`). Model-id validation allows a single-segment id
  (`glm-5.2`, no vendor slash — unlike OpenRouter's required `vendor/model`).
  Distinct failure codes for auth, http-status, timeout, unavailable, response-limit,
  and contract-mismatch (mirroring the OpenRouter executor's diagnosable split).

### 3. Pricing — operator-pinned

No catalog feed, so the manifest pins z.ai's published GLM 5.2 rates as exact
decimal per-token USD: prompt `0.0000014` ($1.40 / 1M input), completion
`0.0000044` ($4.40 / 1M output). A small z.ai pricing value carries the two
exact-decimal rates — the same structure and microunit math as the OpenRouter
pricing, but provider-neutral so the OpenRouter type stays untouched (isolated
adapter). Pricing is part of the signed bundle, so any change requires a fresh
approval.

### 4. Verification gate

One bounded z.ai inference: prompt "Return exactly `GRAPHITE_PROFILE_OK` and
nothing else." Success oracle: `message.strip() == "GRAPHITE_PROFILE_OK"` (plain
text — no JSON-mode dependency). On success, `verify_and_save_approved_profile`
promotes a **READ_ONLY verification `CapabilitySnapshot`** (provider `zai`, model
`glm-5.2`, risk ceiling per policy) bound to a new **active** lifecycle identity,
and records one verification telemetry row — the z.ai analog of the OpenRouter
verification milestone. A non-matching or empty response fails closed
(`profile_verification_failed`/`_invalid`) with no promotion and no partial write.

### 5. Config surface

The z.ai key is read only from **`ZAI_API_KEY`** in the session environment —
never in argv, the bundle, or any receipt (the same no-inline-key discipline that
governs `OPENROUTER_API_KEY`; an inline value shadows the ambient key). The
canonical endpoint is a code constant; the key is the sole secret. The operator
runs the live steps in-session via the `!` prefix (the classifier blocks the
agent's own shell from live inference), with **no inline `ZAI_API_KEY`**.

### 6. Store mutation & governance

Two live-mutating steps, each with its own displayed manifest bundle + explicit
operator approval `Approved: <purpose> bundle <digest>`:

- **v7 migration** (purpose e.g. `graphite_zai_schema_v7`): schema 6 → 7, **zero
  row changes**; verified by a byte-diff that is identical except the widened
  CHECK/`schema_version`, and an audit still reading 12/12/23 + integrity ok.
- **GLM 5.2 verification** (purpose e.g. `graphite_zai_glm52_verification`): one
  tiny live inference; writes +1 capability_snapshot, +1 lifecycle binding, +1
  telemetry row (12/12/23 → 13/13/24). Bounded by the pinned cost ceiling.

`raw_provider_output_persistence: false`; the same `forbidden_persistence` set
(account_metadata, credential_material, prompt_body, response_body, provider
diagnostics, stdout/stderr, executable/credential paths, repository_source).
Never touches `F:\Projects\graphite` (main); no merge, push, or deploy. Offline
dry-runs run against store **copies** before either live round.

### 7. Testing / verification

- New unit tests for `zai_executor`/`zai_probe`: OpenAI-compatible request shape
  (endpoint, bearer auth, model `glm-5.2`, messages), cost math from pinned
  pricing, the const-match oracle, single-segment model-id acceptance, and each
  fail-closed code (auth / http-status / timeout / unavailable / contract).
- New migration tests: v6→v7 round-trips all existing data unchanged, admits a
  `zai` remote-https row, and still rejects an unknown provider and a
  provider/runtime-kind mismatch.
- The **full existing suite stays green** — the OpenRouter adapter and its tests
  are untouched (isolated-adapter decision).
- Offline dry check before each live round: `py_compile`; a monkeypatched dry run
  against a store **copy** with a fake transport returning a canned
  `GRAPHITE_PROFILE_OK`, asserting the verification snapshot is created and
  telemetry advances on the copy; a byte-diff proof for the migration.

## Success criteria

- Schema migrated v6 → v7 on the live fixture, additively: 12/12/23 preserved,
  integrity ok, `provider='zai'` now admissible, unknown providers still rejected.
- One live GLM 5.2 verification inference returns `GRAPHITE_PROFILE_OK` within the
  pinned cost ceiling; a READ_ONLY verification capability snapshot (provider
  `zai`, model `glm-5.2`) is promoted and bound to a new active lifecycle identity;
  one telemetry row persisted (store → 13/13/24).
- Full existing test suite green (OpenRouter path untouched); sanitized receipts;
  evidence committed on `feat/claude-codex-router`.

## Out of scope (deferred to later specs)

- GLM 5.2 edit-promotion (single-call `json_object` edit-smoke — pending
  confirmation z.ai supports structured output), review, pool registration, and
  routed smokes through `execute_approved_route_pool`.
- The other z.ai models (GLM-5.1, GLM-5-Turbo, GLM-4.7, …) and the three unverified
  OpenRouter models.
- Branch integration (push/merge of `feat/claude-codex-router`).
- A shared OpenAI-compatible executor core refactor across OpenRouter + z.ai.
