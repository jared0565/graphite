# Router Track-2 Hardening Minors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development to implement task-by-task. Steps use checkbox (`- [ ]`) syntax. TDD is mandatory (superpowers:test-driven-development): write the failing test, watch it fail for the right reason, then implement — EXCEPT Task 2 Step B (#3), which is a regression-pin: it passes on first write, so validate it the pin way (break the guard, confirm red, restore).

**Goal:** Close the 4 non-blocking hardening Minors surfaced by the Track-2 final whole-branch review.

**Architecture:** Four independent, localized fixes to already-merged code (merge `04bc54e`). No new modules; no behavior change to any happy path. Each is defense-in-depth on a validation/egress boundary.

**Tech Stack:** Python 3.14, pytest, zero third-party deps (stdlib `ipaddress`, `re`).

## Global Constraints

- Branch `feat/router-track2-minors` off `04bc54e` (worktree `F:\tmp\graphite-track2-minors`). LOCAL only — never push.
- Preserve Branch B: no CLI wiring of remote providers into `graphite route`. These are validation/egress hardening only.
- Do not weaken any existing oracle, budget, or fail-closed check. Every fix only ADDS a rejection or a test; no existing assertion is loosened.
- Match each file's existing test conventions (direct private-function import + `pytest.raises`, as already used in `test_llm.py`, `test_routing_schema_validation.py`, `test_review.py`).
- Run tests with `python -m pytest <file> -q` from the worktree root.

---

### Task 1: #1 — reject CGNAT (RFC 6598) shared address space in the keyed-provider egress policy

**Files:**
- Modify: `src/graphite/llm.py` — `_validate_llm_base_url` (currently lines ~241-278) + a new module constant
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `_validate_llm_base_url(base_url: str, *, provider: str) -> None` (raises `LLMConfigurationError`); both already imported in `tests/test_llm.py`.
- Produces: no signature change — only a stricter rejection set for keyed providers (`openai/openrouter/groq`).

**Context:** On Python 3.14.5, `ipaddress.ip_address("100.64.0.1").is_private` is `False` and `.is_global` is `False` — so the current reject set (`is_loopback/is_private/is_link_local/is_reserved/is_multicast/is_unspecified`) does NOT catch CGNAT `100.64.0.0/10`. A keyed provider pointed at a CGNAT host would be accepted, leaking the bearer token to an internal target. Whether `is_private` covers CGNAT is CPython-patch-version-dependent, so the fix MUST be an explicit `ip_network` membership test — do NOT switch the logic to `is_global`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_llm.py` (place beside the other `_validate_llm_base_url` egress tests):

```python
@pytest.mark.parametrize("host", ["100.64.0.0", "100.64.0.1", "100.127.255.255"])
def test_keyed_provider_rejects_cgnat_shared_address(host):
    # RFC 6598 100.64.0.0/10 is not globally routable; a keyed cloud provider aimed there
    # is a misconfiguration that would leak the bearer token. is_private does not cover it
    # on all CPython versions, so it must be rejected explicitly.
    with pytest.raises(LLMConfigurationError):
        _validate_llm_base_url(f"https://{host}/v1", provider="groq")


@pytest.mark.parametrize("host", ["100.63.255.255", "100.128.0.0"])
def test_keyed_provider_accepts_addresses_just_outside_cgnat(host):
    # Immediately below/above the /10 -- globally routable, must remain accepted (no raise).
    _validate_llm_base_url(f"https://{host}/v1", provider="groq")
```

- [ ] **Step 2: Run to verify the reject tests fail (and the accept tests already pass)**

Run: `python -m pytest tests/test_llm.py -q -k cgnat`
Expected: the three `rejects_cgnat` cases FAIL (no exception raised); the two `accepts...just_outside` cases PASS.

- [ ] **Step 3: Implement — explicit CGNAT membership rejection**

In `src/graphite/llm.py`, add a module-level constant (near the top after `import ipaddress`, or immediately above `_validate_llm_base_url`):

```python
# RFC 6598 carrier-grade NAT / shared address space. Not classified as private by
# is_private on every CPython patch version, so reject it explicitly rather than relying
# on the stdlib's version-dependent classification.
_CGNAT_SHARED_IPV4 = ipaddress.ip_network("100.64.0.0/10")
```

Then extend the rejection condition in `_validate_llm_base_url` (the `if address.is_loopback or ...` block) with one clause:

```python
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or (address.version == 4 and address in _CGNAT_SHARED_IPV4)
    ):
        raise LLMConfigurationError("this LLM provider may not target a loopback or private host")
```

(The `address.version == 4` guard avoids a cross-version `in` check against an IPv6 address.)

- [ ] **Step 4: Run to verify all pass**

Run: `python -m pytest tests/test_llm.py -q`
Expected: all pass (new cgnat tests + all pre-existing egress tests still green — the non-keyed / DNS-name / just-outside cases must remain accepted).

- [ ] **Step 5: Commit**

```bash
git add src/graphite/llm.py tests/test_llm.py
git commit -m "fix(llm): reject CGNAT 100.64.0.0/10 in keyed-provider egress policy (Minor #1)"
```

---

### Task 2: #2 + #3 — compile-check `pattern` at the schema gate, and pin json_schema-mode nonconforming rejection

**Files:**
- Modify: `src/graphite/routing/schema_validation.py` — `is_supported_schema` (lines 29-50)
- Test (#2): `tests/test_routing_schema_validation.py`
- Test (#3): `tests/test_routing_openrouter_executor.py`

**Interfaces:**
- Consumes: `is_supported_schema(schema) -> bool` (the pre-call gate); `matches_schema(value, schema) -> bool`; executor helpers `_execute(transport, **overrides)` and `_RecordingTransport` already in the executor test file; `AdapterError`.
- Produces: `is_supported_schema` additionally returns `False` for a schema whose `pattern` is a non-compilable regex. No other behavior change.

**Context:** `is_supported_schema` is the gate the executor runs BEFORE the paid call (`openrouter_executor.py:211` → `request_invalid`). It never checks that a `pattern` string compiles, so a malformed governed regex passes the gate and then raises `re.error` inside `_matches_string` (`schema_validation.py:94`) at RESPONSE time — after the paid call. `matches_schema`'s docstring already promises "never raises"; this fix makes that true. Separately, the executor's json_schema path (default `response_format_type`) fails closed at `matches_schema` (`openrouter_executor.py:314` → `response_contract_invalid`), but only the json_OBJECT mode has a dedicated nonconforming-body test (`test_execute_json_object_rejects_nonconforming_response`). #3 pins the json_schema mode too.

**Step A — #2 (proper red-green):**

- [ ] **Step A1: Write the failing test**

In `tests/test_routing_schema_validation.py`:

```python
def test_fails_closed_on_uncompilable_pattern():
    # A malformed regex must be caught at the gate (before any paid call), not raise
    # re.error inside matches_schema at response time. The "never raises" guarantee on
    # matches_schema depends on this.
    assert is_supported_schema({"type": "string", "pattern": "["}) is False
    assert is_supported_schema({"type": "string", "pattern": "(unclosed"}) is False
    # caught recursively when nested inside properties / items
    assert is_supported_schema(
        {"type": "object", "properties": {"x": {"type": "string", "pattern": "["}}}) is False
    assert is_supported_schema(
        {"type": "array", "items": {"type": "string", "pattern": "(?P<n>"}}) is False
    # a valid pattern is still supported (guards against over-rejection)
    assert is_supported_schema({"type": "string", "pattern": "^[0-9a-f]{64}$"}) is True
```

- [ ] **Step A2: Run to verify it fails**

Run: `python -m pytest tests/test_routing_schema_validation.py::test_fails_closed_on_uncompilable_pattern -q`
Expected: FAIL (the uncompilable-pattern schemas currently return `True`).

- [ ] **Step A3: Implement the compile-check**

In `src/graphite/routing/schema_validation.py`, inside `is_supported_schema`, add this after the `additionalProperties` bool check (after line 40, before the `properties` recursion). `re` is already imported.

```python
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        try:
            re.compile(pattern)
        except re.error:
            return False
```

- [ ] **Step A4: Run to verify #2 passes**

Run: `python -m pytest tests/test_routing_schema_validation.py -q`
Expected: all pass (new test + all pre-existing, including `test_tolerates_refinement_keywords` which asserts a valid `pattern` is still supported).

**Step B — #3 (regression-pin — will pass on first write):**

- [ ] **Step B1: Add the json_schema-mode nonconforming pin**

In `tests/test_routing_openrouter_executor.py`, mirror `test_execute_json_object_rejects_nonconforming_response` (~line 292) but in the DEFAULT json_schema mode — copy that test's transport/body setup and drop the `response_format_type="json_object"` override so `_execute` runs in json_schema mode:

```python
def test_execute_json_schema_rejects_nonconforming_response() -> None:
    # Parity with the json_object nonconforming test: in the default json_schema mode a
    # structurally nonconforming completion body must also fail closed at matches_schema
    # (openrouter_executor.py -> response_contract_invalid), not slip through.
    transport = <same nonconforming-body recording transport as the json_object test>
    with pytest.raises(AdapterError, match="^response_contract_invalid$"):
        _execute(transport)   # no response_format_type override => json_schema mode
```

- [ ] **Step B2: Run — it PASSES immediately (expected for a pin)**

Run: `python -m pytest tests/test_routing_openrouter_executor.py::test_execute_json_schema_rejects_nonconforming_response -q`
Expected: PASS. This is a regression-pin, not red-green — do NOT contort it to fail.

- [ ] **Step B3: Validate the pin the pin way**

Temporarily edit `schema_validation.py` so `matches_schema` returns `True` unconditionally (e.g. `return True` at the top of the function body). Re-run Step B2 and confirm the new test now goes RED (proving it actually exercises the json_schema nonconforming path). Then REVERT the temporary edit and confirm the test is green again. Report the red output you observed.

- [ ] **Step B4: Commit Task 2**

```bash
git add src/graphite/routing/schema_validation.py tests/test_routing_schema_validation.py tests/test_routing_openrouter_executor.py
git commit -m "fix(routing): compile-check schema pattern at gate + pin json_schema nonconforming rejection (Minors #2, #3)"
```

---

### Task 3: #4 — validate `matched_nodes` as a string-list in the review formatter packet

**Files:**
- Modify: `src/graphite/review.py` — `_validate_formatter_packet` (line 585)
- Test: `tests/test_review.py`

**Interfaces:**
- Consumes: `_validate_formatter_packet(packet: Any) -> None` (raises `ReviewError("review packet is invalid")`); `ReviewError` already imported in `tests/test_review.py`.
- Produces: `matched_nodes` now validated as a string-list, exactly like its sibling impact fields.

**Context:** The impact dict carries `matched_nodes` (`review.py:502`, `:545`), but `_validate_formatter_packet` validates only `("impacted_files", "likely_tests", "missing")` as string-lists (line 585), omitting `matched_nodes`. A non-string `matched_nodes` passes validation today.

- [ ] **Step 1: Write the failing test**

In `tests/test_review.py`:

```python
def test_formatter_packet_rejects_non_string_matched_nodes():
    from graphite.review import _validate_formatter_packet
    with pytest.raises(ReviewError, match="review packet is invalid"):
        _validate_formatter_packet({"impact": {"matched_nodes": [123]}})


def test_formatter_packet_accepts_string_matched_nodes():
    from graphite.review import _validate_formatter_packet
    _validate_formatter_packet({"impact": {"matched_nodes": ["store", "cli"]}})  # no raise
```

- [ ] **Step 2: Run to verify the reject test fails**

Run: `python -m pytest tests/test_review.py -q -k matched_nodes`
Expected: `rejects_non_string_matched_nodes` FAILS (no exception today); `accepts_string_matched_nodes` PASSES.

- [ ] **Step 3: Implement — add `matched_nodes` to the validated tuple**

In `src/graphite/review.py`, line 585, change:

```python
    for field in ("impacted_files", "likely_tests", "missing"):
```

to:

```python
    for field in ("matched_nodes", "impacted_files", "likely_tests", "missing"):
```

- [ ] **Step 4: Run to verify all pass**

Run: `python -m pytest tests/test_review.py -q`
Expected: all pass (new tests + all pre-existing review tests — the existing packet fixtures use string `matched_nodes` lists, so none regress).

- [ ] **Step 5: Commit**

```bash
git add src/graphite/review.py tests/test_review.py
git commit -m "fix(review): validate matched_nodes as a string-list in formatter packet (Minor #4)"
```

---

## Self-Review

- **Spec coverage:** all 4 Minors mapped — #1 → Task 1, #2 → Task 2 Step A, #3 → Task 2 Step B, #4 → Task 3.
- **Placeholder scan:** the only intentional `<...>` is Task 2 Step B1's transport setup, which the implementer copies verbatim from the named existing json_object test in the same file — a pin by mirroring, not a placeholder to invent.
- **Type consistency:** `_validate_llm_base_url(base_url, *, provider)`, `is_supported_schema(schema)->bool`, `_validate_formatter_packet(packet)->None`, `AdapterError`/`LLMConfigurationError`/`ReviewError` all match the merged source read during planning.
