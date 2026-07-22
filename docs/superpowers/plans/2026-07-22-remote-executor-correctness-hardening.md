# Remote-executor correctness hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix latent correctness defects on the governed remote executor path (model fail-open, json_object schema non-enforcement) and close z.ai executor test gaps, so the harness-invoked remote path is trustworthy on money/identity boundaries.

**Architecture:** Small, surgical changes to `openrouter_executor.py` / `zai_executor.py` plus one new `schema_validation.py` module (a minimal in-house JSON-Schema-subset validator, zero new deps). All changes are TDD against the executors' existing fake-`transport` seam — no live calls. Branch B: no CLI wiring of remote providers.

**Tech Stack:** Python 3.11+, pytest, stdlib only (no new dependency).

## Global Constraints

- **Worktree:** `F:/tmp/graphite-router-prod-hardening`, branch `feat/router-prod-hardening`. All paths below are relative to it.
- **Test resolution:** run every command from the worktree root with the worktree src on the path so the branch code (not the `F:/Projects/graphite` editable install) is imported: `PYTHONPATH=src python -m pytest ...`. Sanity-check once before Task 1: `PYTHONPATH=src python -c "import graphite.routing.zai_executor as m; print(m.__file__)"` must print a path under `F:/tmp/graphite-router-prod-hardening`.
- **No new runtime dependency.** `pyproject.toml` deps stay unchanged.
- **Error codes are a public contract** — reuse existing `AdapterError` codes exactly: `model_identity_unverified`, `model_mismatch`, `protocol`, `request_invalid`, `response_contract_invalid`, `cost_ceiling_exceeded`. Do not invent codes.
- **Commit trailer:** every commit ends with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Green bar:** the full suite is ~1816 passed / 44 skipped with 2 pre-existing environmental failures (`test_doctor.py::test_core_deep_probe_rejects_temp_path_outside_os_temp`, `test_windows_startup.py::test_install_startup_launcher_writes_hidden_vbs_and_idempotent_script` — Wondershare CreatorTemp). "Green" = no NEW failures beyond those two; new tests add to the passed count.
- **Change 5 (intrinsic cost/token ceilings) — DECISION: no code change.** The executors' loose intrinsic caps (`MAX_TOKEN_COUNT=10_000_000`, `MAX_COST_MICROUNITS=1_000_000_000`) are backstopped by the 4 MiB response cap + 600 s timeout, and the governed harness pins tight per-run bounds in its signed manifest. Tightening the caps risks rejecting a legitimate governed range for no real gain. No task; documented here as an explicit no-op.
- **Deferred:** `service._preflight` claude/codex hardcode — unreachable under Branch B (no remote snapshot flows through `service.py`). Not touched.

## File structure

- `src/graphite/routing/schema_validation.py` — **new.** Pure, dependency-free JSON-Schema-subset validator: `is_supported_schema(schema) -> bool`, `matches_schema(value, schema) -> bool`. One responsibility: decide subset-support and conformance. Reused by z.ai structured output later (Track 2).
- `src/graphite/routing/openrouter_executor.py` — **modify.** Model-identity fail-closed (L296-301); wire schema validation (construction gate + response check).
- `src/graphite/routing/zai_executor.py` — **modify.** Model-identity fail-closed (L157-162).
- `src/graphite/routing/route_pool.py` — **modify.** Delete the vestigial dead cross-provider clause (L312-317).
- `tests/test_routing_schema_validation.py` — **new.** Validator unit tests.
- `tests/test_routing_openrouter_executor.py` — **modify.** Missing-model + schema-enforcement tests; extend `_completion` to omit `model`.
- `tests/test_routing_zai_executor.py` — **modify.** Missing-model, wrong-model, malformed/protocol tests; extend `_envelope` to omit `model`.
- `tests/test_route_pool.py` — **modify.** Characterization test pinning cross-provider construction-allowed behavior.

---

### Task 1: Model-identity fail-open → fail-closed (both remote executors)

**Files:**
- Modify: `src/graphite/routing/openrouter_executor.py:296-301`
- Modify: `src/graphite/routing/zai_executor.py:157-162`
- Test: `tests/test_routing_openrouter_executor.py`, `tests/test_routing_zai_executor.py`

**Interfaces:**
- Produces: both executors now raise `model_identity_unverified` when the response `model` is absent/non-string/empty, and `model_mismatch` when present-but-wrong (matches `claude_executor` semantics).

- [ ] **Step 1: Extend the OpenRouter test `_completion` helper to allow omitting `model`**

In `tests/test_routing_openrouter_executor.py`, change `_completion` (currently always includes `model`):

```python
def _completion(
    content: str,
    *,
    model: str | None = "moonshotai/kimi-k3",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    usage: bool = True,
) -> bytes:
    payload: dict[str, object] = {
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }
    if model is not None:
        payload["model"] = model
    if usage:
        payload["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
    return json.dumps(payload).encode()
```

- [ ] **Step 2: Write the failing OpenRouter test**

Add to `tests/test_routing_openrouter_executor.py`:

```python
def test_execute_rejects_missing_model_echo() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}', model=None))
    with pytest.raises(AdapterError, match="^model_identity_unverified$"):
        _execute(transport)
```

- [ ] **Step 3: Extend the z.ai test `_envelope` helper + write the failing z.ai tests**

In `tests/test_routing_zai_executor.py`, change `_envelope` to omit `model` when `None`:

```python
def _envelope(content, model="glm-5.2", pin=420, cout=6):
    payload = {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": pin, "completion_tokens": cout},
    }
    if model is not None:
        payload["model"] = model
    body = json.dumps(payload).encode()
    return HttpProbeResult(200, body, hashlib.sha256(body).hexdigest(), 0.05)
```

Add:

```python
def test_execute_zai_rejects_missing_model():
    from graphite.routing.zai_executor import execute_zai
    from graphite.routing.claude_executor import AdapterError
    with pytest.raises(AdapterError) as e:
        execute_zai(api_key="k", prompt=b"x", requested_model="glm-5.2",
            expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
            max_cost_microunits=100000, timeout_seconds=60.0,
            transport=lambda **kw: _envelope("GRAPHITE_PROFILE_OK", model=None))
    assert e.value.code == "model_identity_unverified"

def test_execute_zai_rejects_wrong_model():
    from graphite.routing.zai_executor import execute_zai
    from graphite.routing.claude_executor import AdapterError
    with pytest.raises(AdapterError) as e:
        execute_zai(api_key="k", prompt=b"x", requested_model="glm-5.2",
            expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
            max_cost_microunits=100000, timeout_seconds=60.0,
            transport=lambda **kw: _envelope("GRAPHITE_PROFILE_OK", model="other-model"))
    assert e.value.code == "model_mismatch"
```

(`test_execute_zai_rejects_wrong_model` is a characterization test — the `model_mismatch` guard already exists; it should pass immediately. `test_execute_zai_rejects_missing_model` and the OpenRouter test are the true RED cases.)

- [ ] **Step 4: Run the new tests to verify the missing-model cases fail**

Run: `PYTHONPATH=src python -m pytest tests/test_routing_openrouter_executor.py::test_execute_rejects_missing_model_echo tests/test_routing_zai_executor.py::test_execute_zai_rejects_missing_model tests/test_routing_zai_executor.py::test_execute_zai_rejects_wrong_model -v`
Expected: the two `missing_model` tests FAIL (executor returns a result instead of raising — missing `model` currently skips the check); `rejects_wrong_model` PASSES.

- [ ] **Step 5: Fix both executors**

In `src/graphite/routing/openrouter_executor.py`, replace lines 296-301:

```python
    reported_model = envelope.get("model")
    if not isinstance(reported_model, str) or not reported_model:
        raise AdapterError("model_identity_unverified")
    if reported_model != expected:
        raise AdapterError("model_mismatch")
```

In `src/graphite/routing/zai_executor.py`, replace lines 157-162 with the identical four lines.

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=src python -m pytest tests/test_routing_openrouter_executor.py tests/test_routing_zai_executor.py -v`
Expected: all PASS (including the pre-existing `test_execute_rejects_wrong_model_echo`).

- [ ] **Step 7: Commit**

```bash
cd F:/tmp/graphite-router-prod-hardening
git add src/graphite/routing/openrouter_executor.py src/graphite/routing/zai_executor.py tests/test_routing_openrouter_executor.py tests/test_routing_zai_executor.py
git commit -m "fix(routing): fail closed when a remote provider omits the model field

An absent/non-string model in the completion envelope previously skipped
the identity guard, so the reply was attested and billed as the approved
model. Match claude_executor: absent/non-string/empty -> model_identity_
unverified; present-but-wrong -> model_mismatch. Both remote executors.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: z.ai executor malformed-envelope / protocol coverage

**Files:**
- Test: `tests/test_routing_zai_executor.py`

These are characterization tests for guards that already exist in `zai_executor.py:155-176` (envelope non-dict, choices shape, message/content type, usage shape, token type). They bring z.ai to parity with the claude/codex/openrouter executor suites and lock the guards against regression. They pass immediately.

- [ ] **Step 1: Add a raw-envelope helper and the coverage tests**

Add to `tests/test_routing_zai_executor.py`:

```python
def _raw(obj):
    body = json.dumps(obj).encode()
    return HttpProbeResult(200, body, hashlib.sha256(body).hexdigest(), 0.05)

@pytest.mark.parametrize("obj", [
    [1, 2],                                                                   # envelope not a dict
    {"model": "glm-5.2", "usage": {"prompt_tokens": 1, "completion_tokens": 1}},  # no choices
    {"model": "glm-5.2", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},  # 0 choices
    {"model": "glm-5.2", "choices": [{"message": {}}, {"message": {}}],        # 2 choices
     "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    {"model": "glm-5.2", "choices": [{"message": "nope"}],                     # message not a dict
     "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    {"model": "glm-5.2", "choices": [{"message": {"content": 42}}],            # content not a str
     "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    {"model": "glm-5.2", "choices": [{"message": {"content": "x"}}]},          # usage missing
    {"model": "glm-5.2", "choices": [{"message": {"content": "x"}}],           # token not an int
     "usage": {"prompt_tokens": "1", "completion_tokens": 1}},
])
def test_execute_zai_rejects_malformed_envelope(obj):
    from graphite.routing.zai_executor import execute_zai
    from graphite.routing.claude_executor import AdapterError
    with pytest.raises(AdapterError) as e:
        execute_zai(api_key="k", prompt=b"x", requested_model="glm-5.2",
            expected_effective_model="glm-5.2", pricing=PRICING, max_output_tokens=64,
            max_cost_microunits=100000, timeout_seconds=60.0, transport=lambda **kw: _raw(obj))
    assert e.value.code == "protocol"
```

- [ ] **Step 2: Run to confirm they pass (characterization)**

Run: `PYTHONPATH=src python -m pytest tests/test_routing_zai_executor.py::test_execute_zai_rejects_malformed_envelope -v`
Expected: all 8 PASS (guards already present).

- [ ] **Step 3: Commit**

```bash
cd F:/tmp/graphite-router-prod-hardening
git add tests/test_routing_zai_executor.py
git commit -m "test(routing): cover z.ai executor malformed-envelope/protocol guards

Brings z.ai executor test coverage to parity with claude/codex/openrouter.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: In-house JSON-Schema-subset validator module

**Files:**
- Create: `src/graphite/routing/schema_validation.py`
- Test: `tests/test_routing_schema_validation.py`

**Interfaces:**
- Produces: `is_supported_schema(schema: object) -> bool` — True iff `schema` is a dict using only the supported subset (object: `type`/`properties`/`required`/`additionalProperties:false`; array: `type`/`items`/`minItems`/`maxItems`; scalar/leaf: `type`∈{string,integer,number,boolean,null} with optional `const`/`enum`), recursively. Fails closed (False) on any other keyword.
- Produces: `matches_schema(value: object, schema: object) -> bool` — True iff `value` conforms to a supported `schema`; distinguishes bool from int and int from float for `type`/`const`/`enum`.

- [ ] **Step 1: Write the failing validator tests**

Create `tests/test_routing_schema_validation.py`:

```python
from graphite.routing.schema_validation import is_supported_schema, matches_schema

_EDIT = {
    "additionalProperties": False,
    "properties": {"result": {"const": "GRAPHITE_EDIT_OK", "type": "string"}},
    "required": ["result"],
    "type": "object",
}
_REVIEW = {
    "additionalProperties": False,
    "type": "object",
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "findings": {"type": "array", "items": {"type": "string"}},
    },
}

def test_supports_governed_edit_and_review_schemas():
    assert is_supported_schema(_EDIT) is True
    assert is_supported_schema(_REVIEW) is True

def test_rejects_unsupported_keywords_and_shapes():
    assert is_supported_schema({"type": "object", "properties": {}, "required": [],
                                "additionalProperties": False, "minProperties": 1}) is False
    assert is_supported_schema({"type": "object", "additionalProperties": True}) is False
    assert is_supported_schema({"anyOf": [{"type": "string"}]}) is False
    assert is_supported_schema({"type": "string", "pattern": "x"}) is False
    assert is_supported_schema({}) is False
    assert is_supported_schema("nope") is False

def test_matches_conforming_and_rejects_nonconforming():
    assert matches_schema({"result": "GRAPHITE_EDIT_OK"}, _EDIT) is True
    assert matches_schema({"result": "WRONG"}, _EDIT) is False           # const mismatch
    assert matches_schema({}, _EDIT) is False                            # missing required
    assert matches_schema({"result": "GRAPHITE_EDIT_OK", "x": 1}, _EDIT) is False  # extra key
    assert matches_schema({"verdict": "pass", "findings": []}, _REVIEW) is True
    assert matches_schema({"verdict": "maybe", "findings": []}, _REVIEW) is False   # enum miss
    assert matches_schema({"verdict": "pass", "findings": [1]}, _REVIEW) is False   # item type

def test_type_discrimination_bool_int_float():
    assert matches_schema(True, {"type": "integer"}) is False
    assert matches_schema(1, {"type": "boolean"}) is False
    assert matches_schema(1, {"type": "number"}) is True
    assert matches_schema(1.5, {"type": "integer"}) is False
    assert matches_schema(None, {"type": "null"}) is True

def test_array_bounds():
    schema = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2}
    assert matches_schema(["a"], schema) is True
    assert matches_schema([], schema) is False
    assert matches_schema(["a", "b", "c"], schema) is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_routing_schema_validation.py -v`
Expected: FAIL at import (`No module named 'graphite.routing.schema_validation'`).

- [ ] **Step 3: Implement the module**

Create `src/graphite/routing/schema_validation.py`:

```python
"""Minimal in-house JSON-Schema-subset validator for governed structured output.

Supports only the subset the governed output schemas use; fails closed
(``is_supported_schema`` returns False) on any keyword outside it, so an
unsupported schema is rejected at request construction rather than silently
unchecked. Zero third-party dependencies by design: the router money-path
must stay auditable and free of supply-chain surface.
"""
from __future__ import annotations

_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean", "null"})
_OBJECT_KEYS = frozenset({"type", "properties", "required", "additionalProperties"})
_ARRAY_KEYS = frozenset({"type", "items", "minItems", "maxItems"})
_SCALAR_KEYS = frozenset({"type", "enum", "const"})


def is_supported_schema(schema: object) -> bool:
    """True iff schema is a dict using only the supported subset, recursively."""
    if not isinstance(schema, dict) or not schema:
        return False
    type_value = schema.get("type")
    if type_value == "object":
        if not set(schema) <= _OBJECT_KEYS:
            return False
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return False
        if not all(isinstance(k, str) and is_supported_schema(v) for k, v in properties.items()):
            return False
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(k, str) for k in required):
            return False
        if schema.get("additionalProperties", False) is not False:
            return False
        return True
    if type_value == "array":
        if not set(schema) <= _ARRAY_KEYS:
            return False
        items = schema.get("items")
        if items is not None and not is_supported_schema(items):
            return False
        for bound_key in ("minItems", "maxItems"):
            bound = schema.get(bound_key)
            if bound is not None and (isinstance(bound, bool) or not isinstance(bound, int) or bound < 0):
                return False
        return True
    if not set(schema) <= _SCALAR_KEYS:
        return False
    if type_value is not None and type_value not in _SCALAR_TYPES:
        return False
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        return False
    if type_value is None and "const" not in schema and "enum" not in schema:
        return False
    return True


def matches_schema(value: object, schema: object) -> bool:
    """True iff value conforms to a supported schema. Fails closed on unknowns."""
    if not isinstance(schema, dict):
        return False
    type_value = schema.get("type")
    if type_value == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        if not all(key in value for key in schema.get("required", [])):
            return False
        if schema.get("additionalProperties", False) is False and not set(value) <= set(properties):
            return False
        return all(
            matches_schema(value[key], subschema)
            for key, subschema in properties.items()
            if key in value
        )
    if type_value == "array":
        if not isinstance(value, list):
            return False
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < minimum:
            return False
        if maximum is not None and len(value) > maximum:
            return False
        items = schema.get("items")
        return items is None or all(matches_schema(element, items) for element in value)
    if type_value is not None and not _matches_type(value, type_value):
        return False
    if "const" in schema and not _json_equal(value, schema["const"]):
        return False
    if "enum" in schema and not any(_json_equal(value, member) for member in schema["enum"]):
        return False
    return True


def _matches_type(value: object, type_value: str) -> bool:
    if type_value == "string":
        return isinstance(value, str)
    if type_value == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_value == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_value == "boolean":
        return isinstance(value, bool)
    if type_value == "null":
        return value is None
    return False


def _json_equal(value: object, target: object) -> bool:
    """Equality that does not conflate True==1 or 1==1.0 (JSON-kind aware)."""
    if isinstance(target, bool) or isinstance(value, bool):
        return value is target
    if isinstance(target, int) and not isinstance(value, float):
        return isinstance(value, int) and value == target
    return value == target
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_routing_schema_validation.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd F:/tmp/graphite-router-prod-hardening
git add src/graphite/routing/schema_validation.py tests/test_routing_schema_validation.py
git commit -m "feat(routing): minimal in-house JSON-Schema-subset validator

Dependency-free is_supported_schema/matches_schema over the subset the
governed output schemas use, failing closed on any other keyword. Used to
enforce output schemas on the remote executor path.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Enforce output schema in `execute_openrouter`

**Files:**
- Modify: `src/graphite/routing/openrouter_executor.py` (construction gate near L213; response check near L315)
- Test: `tests/test_routing_openrouter_executor.py`

**Interfaces:**
- Consumes: `is_supported_schema`, `matches_schema` from `schema_validation` (Task 3).
- Produces: `execute_openrouter` rejects an unsupported `output_schema` with `request_invalid` before any call, and a non-conforming response with `response_contract_invalid`, in both `json_schema` and `json_object` modes.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_routing_openrouter_executor.py`:

```python
def test_execute_json_object_rejects_nonconforming_response() -> None:
    # A dict that is NOT valid per _SCHEMA (const mismatch) must be rejected even
    # in json_object mode, where the schema is not provider-enforced.
    transport = _RecordingTransport(_completion('{"result":"WRONG"}'))
    with pytest.raises(AdapterError, match="^response_contract_invalid$"):
        _execute(transport, response_format_type="json_object")

def test_execute_json_object_rejects_extra_keys() -> None:
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK","x":1}'))
    with pytest.raises(AdapterError, match="^response_contract_invalid$"):
        _execute(transport, response_format_type="json_object")

def test_execute_rejects_unsupported_output_schema_before_transport() -> None:
    bad = {"type": "object", "properties": {"result": {"type": "string"}},
           "required": ["result"], "additionalProperties": False, "minProperties": 1}
    transport = _RecordingTransport(_completion('{"result":"GRAPHITE_EDIT_OK"}'))
    with pytest.raises(AdapterError, match="^request_invalid$"):
        _execute(transport, output_schema=bad, output_schema_sha256=_canonical_sha256(bad))
    assert transport.calls == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=src python -m pytest tests/test_routing_openrouter_executor.py -k "json_object_rejects or unsupported_output_schema" -v`
Expected: all three FAIL (json_object currently accepts any dict; unsupported schema currently reaches transport / returns a result).

- [ ] **Step 3: Add the construction-time supported-schema gate**

In `src/graphite/routing/openrouter_executor.py`, add the import near the top (with the other `.` imports):

```python
from .schema_validation import is_supported_schema, matches_schema
```

Immediately after the existing `output_schema` dict check (the block `if not isinstance(output_schema, dict) or not output_schema: raise AdapterError("request_invalid")`, ~L213-214), add:

```python
    if not is_supported_schema(output_schema):
        raise AdapterError("request_invalid")
```

- [ ] **Step 4: Add the response-time conformance check**

In the same file, after the existing response block that parses `structured` and checks it is a dict (`if not isinstance(structured, dict): raise AdapterError("response_contract_invalid")`, ~L315-316), add:

```python
    if not matches_schema(structured, output_schema):
        raise AdapterError("response_contract_invalid")
```

- [ ] **Step 5: Run the full OpenRouter executor suite**

Run: `PYTHONPATH=src python -m pytest tests/test_routing_openrouter_executor.py -v`
Expected: all PASS — the three new tests pass, and the existing `test_execute_json_object_mode_relaxes_response_format_only` / `test_execute_still_pins_schema_digest_in_json_object_mode` / `test_execute_builds_canonical_schema_bound_request...` stay green (their content conforms to `_SCHEMA` and `_SCHEMA` is supported).

- [ ] **Step 6: Commit**

```bash
cd F:/tmp/graphite-router-prod-hardening
git add src/graphite/routing/openrouter_executor.py tests/test_routing_openrouter_executor.py
git commit -m "fix(routing): validate the response against output_schema in both modes

json_object mode previously checked only that the reply was a JSON object,
never against the pinned schema, so a governed caller could receive and pay
for any object. Reject unsupported schemas at construction (request_invalid)
and non-conforming responses (response_contract_invalid) in both modes.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Delete the vestigial dead cross-provider guard

**Files:**
- Modify: `src/graphite/routing/route_pool.py:312-317`
- Test: `tests/test_route_pool.py`

**Interfaces:**
- Produces: no behavior change. The unsatisfiable clause is removed; the real cross-provider gate remains at `select_route` (`cross_provider_denied`).

- [ ] **Step 1: Add a characterization test pinning the intended behavior**

Add to `tests/test_route_pool.py`:

```python
def test_cross_provider_pool_constructs_when_fallback_disallowed() -> None:
    # Intended design: a cross-provider pool CAN be constructed even with
    # allow_cross_provider=False; the cross-provider gate is enforced at
    # select_route (see test_cross_provider_fallback_requires_explicit_authority),
    # not at construction. This pins that behavior against re-introducing a
    # construction-time guard.
    pool = _pool(allow_cross_provider=False)
    assert pool.allow_cross_provider is False
    assert len({candidate.provider for candidate in pool.candidates}) == 2
```

- [ ] **Step 2: Run it (passes before and after — it pins behavior)**

Run: `PYTHONPATH=src python -m pytest tests/test_route_pool.py::test_cross_provider_pool_constructs_when_fallback_disallowed -v`
Expected: PASS (the dead clause never fired, so construction already succeeds).

- [ ] **Step 3: Delete the dead clause**

In `src/graphite/routing/route_pool.py`, remove the entire block at L312-317:

```python
        if (
            len({item.provider for item in candidates}) > 1
            and not self.allow_cross_provider
            and len(candidates) == 1
        ):
            raise RoutePoolError("route_pool_invalid")
```

- [ ] **Step 4: Run the full route_pool suite to prove behavior-preserving**

Run: `PYTHONPATH=src python -m pytest tests/test_route_pool.py -v`
Expected: all PASS — including `test_cross_provider_fallback_requires_explicit_authority` (runtime `cross_provider_denied`) and the new characterization test.

- [ ] **Step 5: Commit**

```bash
cd F:/tmp/graphite-router-prod-hardening
git add src/graphite/routing/route_pool.py tests/test_route_pool.py
git commit -m "refactor(routing): remove vestigial dead cross-provider construction clause

len(providers)>1 AND len(candidates)==1 is unsatisfiable, so the clause
never fired. The real cross-provider gate is at select_route
(cross_provider_denied); construction is intentionally allowed. Delete the
misleading clause and pin the intended construction behavior with a test.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the complete suite**

Run: `PYTHONPATH=src python -m pytest -q`
Expected: previous pass count + the new tests passed; 44 skipped; exactly the 2 known environmental failures (`test_doctor.py::test_core_deep_probe_rejects_temp_path_outside_os_temp`, `test_windows_startup.py::test_install_startup_launcher_writes_hidden_vbs_and_idempotent_script`) and NO others.

- [ ] **Step 2: If any non-environmental test fails**

Do NOT paper over it. Return to the owning task, treat it as a real regression (systematic-debugging), fix at root cause, re-run. Only the two Wondershare CreatorTemp failures are acceptable.

- [ ] **Step 3: Confirm the branch state**

Run: `cd F:/tmp/graphite-router-prod-hardening && git log --oneline -7 && git status -sb`
Expected: five implementation commits (Tasks 1-5) atop the two spec commits; working tree clean.

---

## Self-review

**Spec coverage:**
- Change 1 (model fail-closed, both executors) → Task 1. ✓
- Change 2 (json_object schema validation) → Tasks 3 + 4. ✓
- Change 3 (z.ai negative tests) → Task 1 (model) + Task 2 (malformed/protocol). ✓
- Change 4 (delete dead guard) → Task 5. ✓
- Change 5 (ceilings) → Global Constraints explicit no-op decision. ✓
- Deferred `service._preflight` → Global Constraints. ✓

**Placeholder scan:** no TBD/TODO/"add appropriate…"; every code and test step shows complete code; every run step shows the command and expected result.

**Type/name consistency:** `is_supported_schema` / `matches_schema` names and signatures match between Task 3 (definition), the Task 3 tests, and Task 4 (call sites). `_completion(model=None)` / `_envelope(model=None)` helper extensions match their new test call sites. Error codes (`model_identity_unverified`, `model_mismatch`, `request_invalid`, `response_contract_invalid`, `protocol`) match the existing `AdapterError` contract.
