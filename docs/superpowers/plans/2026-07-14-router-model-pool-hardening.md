# Router Model Pool Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the expiring router profiles with a conservative four-model Ollama Cloud pool and make deterministic selection lifecycle-, role-, usage-, evidence-, and quota-aware.

**Architecture:** The registry remains the only authority allowlist and gains immutable lifecycle, role, and coarse usage metadata. Policy applies lifecycle and risk as hard gates, then adds bounded role-fit and usage-class components to the existing evidence-based score; exact model ID remains only the final tie-break. The service continues to construct candidates exclusively from the intersection of the validated local inventory and the allowlist, while the executor and approval boundary remain unchanged.

**Tech Stack:** Python 3.11+, frozen dataclasses and `StrEnum`, standard-library `datetime`, SQLite-backed existing evidence store, pytest, Ruff, Ollama loopback API.

---

## File Responsibility Map

- `src/graphite/routing/registry.py`: exact active profiles, typed role/usage/lifecycle metadata, lifecycle validation.
- `src/graphite/routing/effort.py`: exact default-only request fragments for every active profile.
- `src/graphite/routing/policy.py`: lifecycle gates and deterministic role/usage scoring; no provider I/O.
- `src/graphite/routing/service.py`: inventory/allowlist intersection and recommendation orchestration; no model-specific special cases.
- `src/graphite/cli.py`: immediate interactive display of approved ephemeral output; no JSON/non-interactive execution.
- `tests/test_routing_registry.py`: allowlist, metadata, inventory, digest, and lifecycle contracts.
- `tests/test_routing_policy.py`: hard gates, role fit, usage class, ordering, and high-risk handoff.
- `tests/test_routing_service.py`: offline end-to-end recommendation with an unknown inventory entry present.
- `tests/test_routing_executor.py`: exact new-model payload acceptance and unchanged fail-closed transport boundary.
- `README.md`, `ARCHITECTURE.md`, `docs/superpowers/implementation-notes/`: operator-facing pool and evidence record.
- `docs/superpowers/specs/2026-07-14-router-model-pool-hardening-design.md`: implementation status and acceptance evidence.
- `docs/superpowers/plans/2026-07-14-adaptive-development-router.md`: live-smoke completion record only after an approved provider call.

---

### Task 1: Replace the active registry and effort allowlists

**Files:**
- Modify: `src/graphite/routing/registry.py:31-86`
- Modify: `src/graphite/routing/effort.py:16-29`
- Modify: `tests/test_routing_registry.py:1-75`

- [ ] **Step 1: Write failing exact-pool and metadata tests**

Add imports for `ModelRole` and `UsageClass`, then replace the existing pool assertion with:

```python
def test_bundled_profiles_are_the_approved_nonexpiring_pool() -> None:
    assert set(BUNDLED_PROFILES) == {
        "kimi-k2.7-code:cloud",
        "minimax-m2.7:cloud",
        "nemotron-3-super:cloud",
        "minimax-m3:cloud",
    }
    expected = {
        "kimi-k2.7-code:cloud": (UsageClass.HIGH, {ModelRole.CODING_PRIMARY, ModelRole.CODING}),
        "minimax-m2.7:cloud": (UsageClass.MEDIUM, {ModelRole.CODING, ModelRole.AGENTIC}),
        "nemotron-3-super:cloud": (UsageClass.MEDIUM, {ModelRole.REASONING, ModelRole.REVIEW}),
        "minimax-m3:cloud": (UsageClass.HIGH, {ModelRole.LONG_CONTEXT, ModelRole.AGENTIC}),
    }
    for model_id, profile in BUNDLED_PROFILES.items():
        usage, roles = expected[model_id]
        assert profile.usage_class is usage
        assert set(profile.roles) == roles
        assert profile.retirement_date is None
        assert profile.profile.supported_efforts == (Effort.DEFAULT,)
        assert profile.effort_payloads == {Effort.DEFAULT: {}}


def test_removed_and_unapproved_models_are_not_profiles() -> None:
    for model_id in (
        "glm-5:cloud",
        "kimi-k2.6:cloud",
        "deepseek-v4-flash:cloud",
        "gemma4:31b-cloud",
        "qwen3.5:cloud",
    ):
        assert model_id not in BUNDLED_PROFILES
```

Add table-driven validation tests proving duplicate roles, empty roles, malformed evidence dates, invalid usage classes, and retirement dates on active profiles raise stable `ValueError` codes.

- [ ] **Step 2: Run the registry tests to verify red state**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -B -m pytest -q -p no:cacheprovider --basetemp F:\tmp\graphite-model-pool-task1-red tests/test_routing_registry.py
```

Expected: failures because the old Kimi 2.6/GLM profiles remain and typed role/usage metadata does not exist.

- [ ] **Step 3: Implement typed immutable registry metadata**

In `registry.py`, define:

```python
class UsageClass(StrEnum):
    MEDIUM = "medium"
    HIGH = "high"


class ModelRole(StrEnum):
    CODING_PRIMARY = "coding_primary"
    CODING = "coding"
    AGENTIC = "agentic"
    REASONING = "reasoning"
    REVIEW = "review"
    LONG_CONTEXT = "long_context"
```

Extend `RegistryProfile` with `roles: tuple[ModelRole, ...]` and
`usage_class: UsageClass`. Its `__post_init__` must normalize enums, reject empty or
duplicate roles, validate `evidence_accessed` and optional `retirement_date` as exact
ISO dates via `date.fromisoformat`, and reject a retirement date not later than the
evidence access date.

Replace `BUNDLED_PROFILES` with the exact four entries from the spec using these
minimum context windows:

```python
{
    "kimi-k2.7-code:cloud": 262_144,
    "minimax-m2.7:cloud": 204_800,
    "nemotron-3-super:cloud": 262_144,
    "minimax-m3:cloud": 524_288,
}
```

Use profile version `2026-07-14.2`, official Ollama evidence URLs, access date
`2026-07-14`, `provisional=True`, and no retirement date. Capabilities must include
`code` for Kimi/MiniMax M2.7, `reasoning` for Nemotron, and `architecture` plus
`reasoning` for MiniMax M3, alongside only provider-supported completion/tool/
thinking/vision capabilities.

- [ ] **Step 4: Replace exact effort mappings**

Set `EFFORT_PAYLOADS` to exactly the same four identifiers, each mapping only
`Effort.DEFAULT` to an empty immutable mapping. Do not enable `think`, `reasoning`,
or undocumented effort values.

- [ ] **Step 5: Run focused tests and lint**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -B -m pytest -q -p no:cacheprovider --basetemp F:\tmp\graphite-model-pool-task1 tests/test_routing_registry.py tests/test_routing_contracts.py
python -B -m ruff check src/graphite/routing/registry.py src/graphite/routing/effort.py tests/test_routing_registry.py
git diff --check
```

Expected: all pass; registry contains exactly four profiles.

- [ ] **Step 6: Commit registry replacement**

```powershell
git add src/graphite/routing/registry.py src/graphite/routing/effort.py tests/test_routing_registry.py
git commit -m "feat: replace expiring router profiles"
```

---

### Task 2: Add lifecycle runway and capability-aware policy scoring

**Files:**
- Modify: `src/graphite/routing/registry.py`
- Modify: `src/graphite/routing/policy.py:12-225`
- Modify: `tests/test_routing_policy.py`

- [ ] **Step 1: Write failing lifecycle gate tests**

Add a pure helper contract:

```python
@pytest.mark.parametrize(
    ("retirement", "current", "eligible"),
    [
        (None, "2026-07-14", True),
        ("2026-08-14", "2026-07-14", True),
        ("2026-08-13", "2026-07-14", False),
        ("2026-07-14", "2026-07-14", False),
    ],
)
def test_lifecycle_requires_thirty_full_days_of_runway(retirement, current, eligible):
    assert lifecycle_is_eligible(retirement, current, minimum_runway_days=30) is eligible
```

Add malformed-date and invalid-runway cases that must raise `ValueError` with
`lifecycle_date_invalid` or `lifecycle_runway_invalid`.

Create a temporary `RegistryProfile` with a retirement date inside the runway,
monkeypatch it into `BUNDLED_PROFILES`, and prove `rank_candidates` returns manual
handoff with reason `model_retiring` before scoring.

- [ ] **Step 2: Write failing deterministic role/usage ranking tests**

Use snapshots containing all four exact models and equal evidence metrics. Prove:

```python
def test_low_risk_code_prefers_kimi_role_fit_over_usage_class() -> None:
    result = rank_candidates(
        _task(category=TaskCategory.ISOLATED_CODE, risk=RiskTier.LOW),
        _approved_pool_snapshot(),
        tuple(_candidate(model_id) for model_id in BUNDLED_PROFILES),
        PolicyGates(current_date="2026-07-14"),
    )
    assert result.selected.model_id == "kimi-k2.7-code:cloud"
    assert [item.model_id for item in result.ranked[:3]] == [
        "kimi-k2.7-code:cloud",
        "minimax-m2.7:cloud",
        "nemotron-3-super:cloud",
    ]
```

Also prove a medium-usage model beats a high-usage model when role fit and evidence
are equal; MiniMax M3 is the sole eligible candidate when the requested context
exceeds 262,144 but fits 524,288; architecture/high-risk still returns manual
handoff because every profile remains provisional; reversing input order never
changes ranking.

- [ ] **Step 3: Run policy tests to verify red state**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -B -m pytest -q -p no:cacheprovider --basetemp F:\tmp\graphite-model-pool-task2-red tests/test_routing_policy.py
```

Expected: failures because lifecycle has no runway helper and scoring ignores roles
and usage class.

- [ ] **Step 4: Implement lifecycle hard gate**

Add `lifecycle_is_eligible(retirement_date, current_date, *, minimum_runway_days=30)`
to `registry.py` using `datetime.date` and `timedelta`. `None` is eligible; malformed
dates and non-integer/negative/greater-than-365 runway fail closed.

In `_candidate_failure`, call the helper before inventory lookup. Return
`model_retiring` for an otherwise valid profile without revealing the date.

- [ ] **Step 5: Implement bounded score components**

Bump `POLICY_VERSION` to `2`. Change `_score` to accept `task` and read immutable
metadata from `BUNDLED_PROFILES`. Add these fixed components:

```python
_ROLE_BONUS = {
    TaskCategory.DOCUMENTATION: {ModelRole.CODING_PRIMARY: 100, ModelRole.CODING: 40},
    TaskCategory.ISOLATED_CODE: {ModelRole.CODING_PRIMARY: 100, ModelRole.CODING: 40},
    TaskCategory.FEATURE: {ModelRole.CODING_PRIMARY: 100, ModelRole.CODING: 40, ModelRole.AGENTIC: 20},
    TaskCategory.REFACTOR: {ModelRole.CODING_PRIMARY: 80, ModelRole.CODING: 40, ModelRole.REASONING: 20},
    TaskCategory.ARCHITECTURE: {ModelRole.LONG_CONTEXT: 80, ModelRole.REASONING: 40},
}
_USAGE_PENALTY = {UsageClass.MEDIUM: 0, UsageClass.HIGH: 40}
```

Use the maximum matching role bonus, add it to reliability, subtract usage penalty,
and clamp the final score to 0–1,000. Include `role_fit_bonus` and `usage_penalty` in
`ScoredCandidate.components`. Do not read objective text, inventory order, mutable
aliases, or provider responses during scoring.

- [ ] **Step 6: Run policy, registry, telemetry, and lint checks**

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -B -m pytest -q -p no:cacheprovider --basetemp F:\tmp\graphite-model-pool-task2 tests/test_routing_policy.py tests/test_routing_registry.py tests/test_routing_telemetry.py tests/test_routing_shadow.py
python -B -m ruff check src/graphite/routing/registry.py src/graphite/routing/policy.py tests/test_routing_policy.py
git diff --check
```

Expected: all pass; confidence and promotion tests remain unchanged because metadata
cannot grant authority.

- [ ] **Step 7: Commit policy hardening**

```powershell
git add src/graphite/routing/registry.py src/graphite/routing/policy.py tests/test_routing_policy.py
git commit -m "feat: rank active router models by role and usage"
```

---

### Task 3: Prove offline service selection and executor compatibility

**Files:**
- Create: `tests/test_routing_service.py`
- Modify: `tests/test_routing_executor.py`
- Modify: `tests/test_routing_cli.py`
- Modify: `src/graphite/routing/service.py`
- Modify: `src/graphite/cli.py`

- [ ] **Step 1: Write an offline service recommendation test**

Construct a temporary repository and monkeypatch only external evidence providers:
`check_graph_freshness` returns `{"stale": False}`, graph loading returns a minimal
valid `networkx.DiGraph`, context construction returns a two-item `ContextBundle`,
and cached inventory returns all four exact models plus an unknown
`grok-untrusted:cloud` entry. Patch `socket.socket` to raise if opened.

Call:

```python
recommendation = RoutingService(root, state_dir=tmp_path / "machine").recommend(
    objective="Review this isolated formatting helper",
    targets=("src/listing_summary.py",),
)
assert recommendation.model_id == "kimi-k2.7-code:cloud"
assert recommendation.effort == "default"
assert recommendation.policy_version == "2"
assert "grok-untrusted:cloud" not in str(recommendation.to_dict())
assert list(root.rglob("events.sqlite3")) == []
```

The test must also assert the public recommendation contains no source text,
absolute path, prompt, response, digest, or database location.

Add a service execution-result contract test with a monkeypatched executor:

```python
result = service.execute_approved(recommendation)
assert result.text == "bounded suggestion"
assert result.receipt["outcome"] == "succeeded"
assert "bounded suggestion" not in json.dumps(result.to_public_dict())
assert b"bounded suggestion" not in service.store.path.read_bytes()
```

Add a CLI test proving an approved interactive text run prints the ephemeral text
once followed by the sanitized receipt, while JSON/non-TTY modes still stop before
execution and can never print it.

- [ ] **Step 2: Add executor table cases for every active model**

Parameterize the existing fake server success contract over the four exact IDs and
their fixed digests. For each, issue an approval for `Effort.DEFAULT`, return the
same exact model in `/api/chat`, and assert request keys remain exactly
`model/messages/options/stream`; no undocumented effort fragment is emitted.

Add removed-model cases proving `glm-5:cloud` and `kimi-k2.6:cloud` fail with
`model_profile_missing` before `/api/chat` and cannot consume a new approval.

- [ ] **Step 3: Run tests to verify red state**

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -B -m pytest -q -p no:cacheprovider --basetemp F:\tmp\graphite-model-pool-task3-red tests/test_routing_service.py tests/test_routing_executor.py tests/test_routing_cli.py
```

Expected: failures until new profiles, policy version, and ephemeral service result
are wired consistently.

- [ ] **Step 4: Implement the generic ephemeral service result**

The service must continue building candidates with:

```python
for model in snapshot.models
if model.model_id in BUNDLED_PROFILES
```

Do not add per-model branches to `service.py`. If policy version or typed metadata
serialization is stale, consume the public values exported by policy/registry rather
than duplicating constants.

Define:

```python
@dataclass(frozen=True)
class ApprovedExecution:
    text: str
    receipt: dict[str, Any]

    def to_public_dict(self) -> dict[str, Any]:
        return dict(self.receipt)
```

After the executor returns, persist only `ExecutionReceipt` fields and return
`ApprovedExecution(result.text, receipt.to_dict())`. In `cmd_route_run`, execution
remains impossible in JSON/non-TTY/CI/`--yes` modes; after interactive approval,
print the model text to the immediate terminal, then serialize only
`to_public_dict()`. Never pass text to `_route_print`, storage, logger, exception,
status, policy, or outcome recording.

- [ ] **Step 5: Run service and authority regressions**

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -B -m pytest -q -p no:cacheprovider --basetemp F:\tmp\graphite-model-pool-task3 tests/test_routing_service.py tests/test_routing_executor.py tests/test_routing_cli.py tests/test_routing_approval.py tests/test_routing_security.py tests/test_llm.py
python -B -m ruff check src/graphite/routing/service.py tests/test_routing_service.py tests/test_routing_executor.py tests/test_routing_cli.py
git diff --check
```

Expected: all pass; no network, approval, or execution authority regression.

- [ ] **Step 6: Commit service acceptance**

```powershell
git add tests/test_routing_service.py tests/test_routing_executor.py tests/test_routing_cli.py src/graphite/routing/service.py src/graphite/cli.py
git commit -m "test: verify hardened router selection"
```

Stage `service.py` only if it changed.

---

### Task 4: Update operator documentation and evidence

**Files:**
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`
- Create: `docs/superpowers/implementation-notes/2026-07-14-router-model-pool-evidence.md`
- Modify: `tests/test_documentation.py`

- [ ] **Step 1: Write failing documentation contract tests**

Require the combined documents to contain all four exact active identifiers,
`30-day minimum retirement runway`, `provider-reported usage class`, and
`inventory presence does not authorize a model`. Require `glm-5:cloud` to appear
only in migration/removal history, never in an active-pool code block.

- [ ] **Step 2: Run the documentation test to verify red state**

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -B -m pytest -q -p no:cacheprovider --basetemp F:\tmp\graphite-model-pool-task4-red tests/test_documentation.py -k routing
```

Expected: failure because the current docs describe routing generally but not the
hardened pool.

- [ ] **Step 3: Update operator and architecture documentation**

Document the exact pool and roles, lifecycle gate, coarse usage metadata, unknown
inventory exclusion, provisional/high-risk boundary, manual handoff, and the fact
that usage class is not a USD price or measured cost saving.

The implementation note must list for each profile: exact model ID, locally observed
digest and context length from the 2026-07-14 `/api/tags` snapshot, official Ollama
URL/access date, roles, usage class, and default-only effort decision. Never include
Ollama credentials, prompts, responses, user paths, or repository source.

- [ ] **Step 4: Run docs, registry, and lint checks**

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -B -m pytest -q -p no:cacheprovider --basetemp F:\tmp\graphite-model-pool-task4 tests/test_documentation.py tests/test_routing_registry.py
python -B -m ruff check tests/test_documentation.py
git diff --check
```

Expected: all pass and no secret/path audit match.

- [ ] **Step 5: Commit documentation**

```powershell
git add README.md ARCHITECTURE.md tests/test_documentation.py docs/superpowers/implementation-notes/2026-07-14-router-model-pool-evidence.md
git commit -m "docs: document hardened router model pool"
```

---

### Task 5: Complete offline acceptance and prepare the live smoke gate

**Files:**
- Modify: `docs/superpowers/specs/2026-07-14-router-model-pool-hardening-design.md`
- Modify: `docs/superpowers/plans/2026-07-14-router-model-pool-hardening.md`
- Modify only after successful/attempted approved smoke: `docs/superpowers/plans/2026-07-14-adaptive-development-router.md`

- [x] **Step 1: Run focused model-pool suites**

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -B -m pytest -q -p no:cacheprovider --basetemp F:\tmp\graphite-model-pool-focused tests/test_routing_registry.py tests/test_routing_policy.py tests/test_routing_service.py tests/test_routing_executor.py tests/test_routing_cli.py tests/test_routing_approval.py tests/test_routing_security.py tests/test_routing_telemetry.py tests/test_routing_shadow.py tests/test_documentation.py
```

Expected: all pass; only documented platform permission skips are acceptable.

- [x] **Step 2: Run full static and automated acceptance**

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -B -m pytest -q -p no:cacheprovider --basetemp F:\tmp\graphite-model-pool-full
python -B -m ruff check .
git diff --check
```

Expected: all pass; no routing security or authority test is skipped.

- [x] **Step 3: Rebuild and validate Graphite's graph**

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -B -m graphite build .
python -B -m graphite check . --json
python -B -m graphite validate --graph-json graph-out/graph.json --json
```

Expected: fresh graph; zero validation errors and warnings.

- [x] **Step 4: Rebuild the synthetic smoke fixture and refresh inventory**

Use only `F:\tmp\graphite-router-smoke`. Verify its three source/test/documentation
files contain no secret pattern, rebuild from that directory, run its unit test, and
explicitly refresh the bounded local inventory:

```powershell
$env:PYTHONPATH='F:\Projects\graphite\src'
python -B -m graphite build .
python -B -m pytest -q -p no:cacheprovider --basetemp F:\tmp\graphite-router-smoke-final tests/test_listing_summary.py
python -B -m graphite route policy . --refresh-models --json
python -B -m graphite route recommend . --objective 'Review this isolated formatting helper and suggest one robustness improvement without editing files.' --target src/listing_summary.py --json
```

Expected recommendation: `kimi-k2.7-code:cloud`, `default`, low risk, source manifest
containing only the fixture source and test. Record estimated tokens and the maximum
reservation (`max_input_tokens + max_output_tokens`) before proceeding.

- [x] **Step 5: Stop and request explicit live-call approval**

Present the exact model ID and current digest, effort, manifest paths/hashes/bytes,
estimated tokens, maximum reserved tokens, timeout, and the fact that Ollama labels
the model high usage. Do not infer consent from plan approval, `--yes`, CI, JSON, or
redirected input. Wait for an explicit yes to that one provider call.

- [ ] **Step 6: Execute one approved call and record sanitized evidence**

After approval, invoke `RoutingService` in the synthetic fixture and use its
in-memory recommendation object so the one-time signed approval binds the
same graph, context, model, digest, effort, and quotas. Do not print or persist the
prompt/source payload. Read `ApprovedExecution.text` only for the immediate operator,
serialize only `ApprovedExecution.to_public_dict()`, then
record receipt ID, exact model, effort, input/output usage, latency, hashes, and a
human accepted/rejected verdict. If provider execution fails, record the stable
failure reason without response body or credentials; do not retry or switch models.

- [x] **Step 7: Mark acceptance status accurately**

Set the hardening design status to `Implemented` only after offline acceptance.
Record live smoke separately as `passed`, `declined`, or `external provider failure`.
Check the original adaptive-router smoke item only when an approved call was actually
attempted and its state was recorded. Never mark it complete based on unit tests.

- [x] **Step 8: Commit acceptance evidence**

```powershell
git add docs/superpowers/specs/2026-07-14-router-model-pool-hardening-design.md docs/superpowers/plans/2026-07-14-router-model-pool-hardening.md
git commit -m "docs: record hardened model pool offline acceptance"
```

Stage the original adaptive-router plan only if its live-smoke state changed.

- [x] **Step 9: Review branch without pushing**

```powershell
git status --short --branch
git log --oneline --decorate --max-count 12
git diff main...HEAD --stat
git diff main...HEAD --check
```

Expected: clean feature branch with reviewable commits. Use the
`finishing-a-development-branch` skill for integration; push only after explicit
user direction.

#### Offline acceptance record — 2026-07-14

- Focused routing/documentation suites, with cache disabled and a controlled
  `F:\tmp` basetemp: 279 passed, 1 skipped in 71.36 seconds. The sole skip was
  `tests/test_routing_storage.py:981`, because POSIX permission bits are not
  authoritative on Windows.
- Full suite, with cache disabled and a controlled `F:\tmp` basetemp: 1,369 passed,
  44 skipped in 272.55 seconds. Skips were Windows/POSIX contract cases and the
  unavailable optional TypeScript compiler bridge. No routing security or
  approval-authority test was skipped.
- `python -B -m ruff check .` and `git diff --check`: passed.
- Rebuilt repository graph: fresh, 133 source/manifest files, 5,506 nodes, 11,918
  edges, zero validation errors, and zero warnings.
- Synthetic fixture inventory: authored `README.md`, `src/listing_summary.py`, and
  `tests/test_listing_summary.py`; other files are generated Graphite cache, graph,
  and routing-state artifacts. The credential scan found no secret material. The
  fixture unit test passed (1 test), and the bounded loopback `/api/tags` refresh
  completed without a provider chat call.

#### Current live-approval preflight (not issued)

- Task: `task-444452e9f8f89d3fbd0d37ee`; policy version `2`; low risk; data policy
  `source_allowed`.
- Model: `kimi-k2.7-code:cloud`; digest
  `eda07a6592375dcbde7cf167b6d6b368cdd28e244f9d71559fb59919aca882fa`;
  effort `default`; provider usage class `high`.
- Graph fingerprint:
  `b405a65af40d3c1d43959bc114f2c9578934022bb66800f5fe14f0327ea5c001`.
- Context manifest hash:
  `6dff489286124e90c54fd5d4f27a5f68256656adfd93bd234de681ec72e48024`;
  753 total bytes; zero exclusions and zero redactions.
- `src/listing_summary.py`: 560 bytes, SHA-256
  `49601f28e6addf0602801a9664fa9221504b87b87107ac833bada201d08922b8`.
- `tests/test_listing_summary.py`: 193 bytes, SHA-256
  `a4c17d1890223e12964073b4d12e84afcb0583e56f0d4a87b495d6618efcfcb0`.
- Estimated context input: 189 tokens; canonical request estimate: 507 tokens
  (2,025 bytes); estimated output ceiling: 4,096 tokens; recommendation estimate:
  4,285 tokens.
- Maximum input/output: 32,768/4,096 tokens; total reservation: 36,864 tokens;
  timeout: 180 seconds; approval TTL if later issued: 300 seconds.
- Canonical prompt binding hash:
  `6f048355d18dd5691cfd36de805cd10bec46f1f656e990ab000b4827f8eda360`.
- A future signed approval must additionally bind a newly allocated approval ID,
  decision ID, issue/expiry timestamps, and nonce. None was allocated for this
  preflight. Execution is single-shot with no retry, redirect, model fallback, or
  model pull.

Live smoke state: **pending explicit approval**. No model inference/provider chat
call occurred, and no approval was issued or consumed. The original adaptive-router
live-smoke item remains unchecked.

---

## Plan Self-Review

- Spec coverage: Tasks 1–2 implement the exact pool, removal, lifecycle runway,
  role fit, usage preference, high-risk boundary, and deterministic scoring.
- Authority coverage: Task 3 proves inventory cannot authorize an unknown model and
  retains exact digest/default-effort/approval behavior across all four profiles.
- Operations coverage: Task 4 documents evidence and limitations; Task 5 separates
  mandatory offline acceptance from the explicitly approved provider call.
- Type consistency: `UsageClass`, `ModelRole`, `RegistryProfile.roles`,
  `RegistryProfile.usage_class`, and `lifecycle_is_eligible` use identical names in
  registry, policy, tests, and documentation.
- No package installation, model pull, OpenRouter call, Claude/Codex launch, source
  mutation, automatic fallback, retry, or autonomous authority is introduced.
