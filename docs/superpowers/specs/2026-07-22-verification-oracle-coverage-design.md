# Verification-oracle coverage — design + plan (T1b)

**Date:** 2026-07-22
**Branch:** `feat/router-prod-hardening` (Track 1, same branch as T1a; batch-merge at end)
**Status:** design approved (operator), compact plan below

## Context — why T1b is small (a documented decision, not a large build)

The production-readiness audit flagged two Cat-C items: "the promotion oracle is stubbed for
non-claude providers" and "no live-provider test / live wiring is CI-invisible." Reading the
actual code shows both are largely **Branch-B by design**, not defects:

- **Oracle is dependency-injected on purpose.** `verify_approved_profile` /
  `verify_and_save_approved_profile` / `verify_and_save_approved_edit_profile`
  (`profiles.py:264/319/358`) take a `verifier: Callable[...]`. The *promotion machinery*
  (budget bounds, lifecycle binding, telemetry, atomic save) **is** genuinely tested with real
  inputs; only the injected live verifier is a canned lambda in tests. The live call + content
  check live in the governed harness by design — that is the same seam that lets the harness
  spend money under manifest+approval while pytest stays free and offline.
- **claude's** content oracle already *is* in-src and tested (const `PROFILE_VERIFICATION_MARKER`,
  `test_routing_claude_executor.py`).
- **OpenRouter's** verification oracle is now *also* in-src-enforced — as a consequence of T1a
  Task 4, `execute_openrouter` validates the response against `VERIFY_SCHEMA`
  (`{"verification": {"const": "GRAPHITE_PROFILE_OK"}}`) via `matches_schema`, so a wrong
  verification value is rejected `response_contract_invalid` before it can pass. This was
  previously unenforced in `json_object` mode.
- **z.ai's** verification oracle is a plaintext compare (`message.strip() == exact_text`), applied
  **harness-side by design**: `execute_zai` is a general executor that returns the model message
  verbatim; it does not (and should not) bake in a specific verification string.
- **No live pytest test** is correct under Branch B: an unattended paid call without a displayed
  manifest + approval would violate the governance. Live validation is the governed harness.

**Decision (recorded here):** T1b does not add live tests, does not move z.ai's plaintext oracle
into the executor, and does not "de-stub" the DI seam (which is correct design). It adds the two
genuinely-valuable, in-repo, offline tests that pin the oracle behavior that T1a made enforceable,
and documents the above so the audit item is closed with rationale rather than a hollow build.

## Goals

Pin, offline, that (a) the OpenRouter verification oracle is now enforced in-executor (T1a Task 4
consequence) and (b) z.ai returns pathological/empty content verbatim (so the harness-side plaintext
oracle is what rejects the `finish_reason=length` class that bit us live).

## Non-goals

No live calls, no cassette-recording infra, no change to the verifier DI seam, no z.ai executor
behavior change. T1c handles the audit backlog.

## Plan

### Task 1: verification-oracle coverage tests (test-only)

**Files:** `tests/test_routing_openrouter_executor.py`, `tests/test_routing_zai_executor.py`

- [ ] **Step 1 — OpenRouter: prove the verify oracle is enforced in json_object mode.**
  Add to `tests/test_routing_openrouter_executor.py`:

```python
_VERIFY_SCHEMA = {
    "additionalProperties": False,
    "properties": {"verification": {"const": "GRAPHITE_PROFILE_OK", "type": "string"}},
    "required": ["verification"],
    "type": "object",
}


def test_execute_enforces_verification_oracle_in_json_object_mode() -> None:
    # The OpenRouter verification path uses VERIFY_SCHEMA; its const IS the oracle. A wrong
    # verification value must be rejected (response_contract_invalid) even in json_object mode,
    # which previously enforced nothing. Proves T1a Task 4 closed this for the verify path.
    wrong = _RecordingTransport(_completion('{"verification":"WRONG"}'))
    with pytest.raises(AdapterError, match="^response_contract_invalid$"):
        _execute(wrong, output_schema=_VERIFY_SCHEMA,
                 output_schema_sha256=_canonical_sha256(_VERIFY_SCHEMA),
                 response_format_type="json_object")
    ok = _RecordingTransport(_completion('{"verification":"GRAPHITE_PROFILE_OK"}'))
    outcome = _execute(ok, output_schema=_VERIFY_SCHEMA,
                       output_schema_sha256=_canonical_sha256(_VERIFY_SCHEMA),
                       response_format_type="json_object")
    assert outcome.message == '{"verification":"GRAPHITE_PROFILE_OK"}'
```

- [ ] **Step 2 — z.ai: pin the empty-content behavior (the finish_reason=length class).**
  Add to `tests/test_routing_zai_executor.py`:

```python
def test_execute_zai_returns_empty_content_verbatim():
    # When glm-5.2 exhausts its budget on reasoning (finish_reason=length) it returns empty
    # content. execute_zai returns it verbatim (message==""); the harness-side plaintext oracle
    # (message.strip() == exact_text) is what rejects it. This pins that the executor stays a
    # general adapter and does not itself apply the verification string.
    from graphite.routing.zai_executor import execute_zai
    result = execute_zai(
        api_key="k", prompt=b"x", requested_model="glm-5.2",
        expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
        max_cost_microunits=100000, timeout_seconds=60.0,
        transport=lambda **kw: _envelope(""))
    assert result.message == ""
```

- [ ] **Step 3 — run:** `PYTHONPATH=src python -m pytest tests/test_routing_openrouter_executor.py tests/test_routing_zai_executor.py -v` — all pass. Both are offline (fake transport). `test_execute_enforces_verification_oracle_in_json_object_mode` is a genuine RED→GREEN only against pre-T1a code; against this branch it passes (proving Task 4's enforcement) — note that in the report.

- [ ] **Step 4 — commit** (trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`).

## Testing / green bar

Offline only. Full suite stays 1846 passed / 44 skipped / 2 known Wondershare env flakes; the two
new tests add to the pass count.
