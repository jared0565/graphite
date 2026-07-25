# Quota-as-Capacity Classification Implementation Plan

> **STATUS: IMPLEMENTED** — all 3 tasks + final-review fix wave merged to main
> as `9704c1e` (2026-07-25, suite 2101/44/0); the spec §9 live acceptance
> smoke executed and PASSED the same day. Kept for reference only.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quota/rate-limit exhaustion on the claude/codex CLI paths classifies as `capacity_unavailable` on both the nonzero-exit transport path and the exit-0 adapter path, so 2-candidate route pools can advance to their fallback candidate.

**Architecture:** Three small, ordered changes: (1) a structured quota-detection phase inside the transport's nonzero-exit classifier (`process_runner.py`) — the only place the failed process's stdout bytes exist; (2) the codex exit-0 parser adopts the shared marker vocabulary; (3) a canonical total mapping function in `route_pool.py` from adapter failure codes/diagnostics to allowlisted route-attempt categories. Spec: `docs/superpowers/specs/2026-07-25-quota-capacity-classification-design.md`.

**Tech Stack:** Python 3.12, pytest, ruff. No new dependencies.

## Global Constraints

- Work in worktree `F:/tmp/graphite-quota-capacity`, branch `feat/quota-capacity-classification`. **NEVER run `pip install -e .` from the worktree** (it breaks `import graphite` machine-wide). Run all tests with `PYTHONPATH=F:/tmp/graphite-quota-capacity/src` so imports resolve to the worktree, not the machine-wide editable install.
- Sanitization invariant (spec §5): no provider output text may be persisted, logged, or attached to any error. Decoded text and parsed events must never escape `_classify_nonzero_failure` / `_stdout_reports_quota` — only the category string is returned. `CliProcessFailureDiagnostics` fields are unchanged (hashes only).
- Shared marker vocabulary, exact value (spec §4): `QUOTA_MARKERS: Final = ("quota", "rate_limit", "rate limit", "usage_limit", "usage limit")`. Matching is always against lowercased text.
- Claude transport matching uses **only** the provider-authored `subtype` field of error-shaped `result` events — never serialized events (their `result` field can carry model-authored text). Claude's `_parse_result` in `claude_executor.py` is **not modified**.
- Structured phase inspects **stdout only**; stderr keeps only the existing exact-line capacity match. Quota events are matched at **any position** in the stream.
- The frozen category sets (`process_runner.py` `_FAILURE_CATEGORIES`, `route_pool.py` `_FAILURE_CATEGORIES`, `route_pool_execution.py` outcome set) and the 2-candidate `allowed_fallback_reasons` pin `("capacity_unavailable",)` are **unchanged**.
- No persistence-schema changes (routing store stays at v8). No changes to lifecycle/verification flows.
- Lint: `python -m ruff check src tests` must be clean after each task.
- **Not in this plan:** the spec §9 store-copy live fallback smoke is a post-merge operational phase (harness authoring + operator-approved live run under the established live-acceptance workflow, window before 2026-07-29 ~09:40). It is controller-executed, not an SDD task.

---

### Task 1: Transport structured quota classification

**Files:**
- Modify: `src/graphite/routing/process_runner.py` (imports ~line 4; constants near `_CAPACITY_DIAGNOSTICS` ~line 57; `_classify_nonzero_failure` at end of file ~line 393)
- Test: `tests/test_routing_process_runner.py` (append after `test_capacity_phrase_embedded_in_other_diagnostic_fails_closed`, ~line 349)

**Interfaces:**
- Consumes: existing `run_cli_process`, `CliProcessError`, `ProbeProcessResult`, test helpers `_credential_home(tmp_path)` and `FAKE_CLI` already defined in the test file.
- Produces: public constant `QUOTA_MARKERS: Final = ("quota", "rate_limit", "rate limit", "usage_limit", "usage limit")` in `process_runner.py` (Task 2 imports it); private `_CLAUDE_SUBTYPE_MARKERS: Final = ("quota", "rate", "limit")`; private helper `_stdout_reports_quota(provider: ProviderId, stdout: bytes) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routing_process_runner.py` (module already imports `hashlib`, `sys`, `Path`, `pytest`, `ProbeProcessResult`, `CliProcessError`, `ProviderId`, `run_cli_process`; verify and reuse):

```python
def _classified_category(
    tmp_path: Path,
    provider: ProviderId,
    stdout: bytes,
    stderr: bytes = b"",
) -> str:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    credentials = _credential_home(tmp_path)

    def runner(argv: list[str], **kwargs: object) -> ProbeProcessResult:
        return ProbeProcessResult(1, stdout, stderr, 0.25)

    with pytest.raises(CliProcessError, match="^process_nonzero$") as caught:
        run_cli_process(
            argv=(sys.executable, str(FAKE_CLI), "echo"),
            cwd=workspace,
            stdin=b"",
            provider=provider,
            credential_home=credentials,
            timeout_seconds=5,
            runner=runner,
            source_environment={},
        )
    diagnostics = caught.value.diagnostics
    assert diagnostics is not None
    return diagnostics.failure_category


_CODEX_USAGE_LIMIT_EVENT = (
    b'{"type":"error","message":"You\'ve hit your usage limit. Visit '
    b"https://chatgpt.com/codex/settings/usage to purchase more credits or "
    b'try again at Jul 29th, 2026 9:40 AM."}'
)


def test_codex_usage_limit_error_event_classifies_as_capacity(tmp_path: Path) -> None:
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, _CODEX_USAGE_LIMIT_EVENT)
        == "capacity_unavailable"
    )


def test_codex_turn_failed_rate_limit_classifies_as_capacity(tmp_path: Path) -> None:
    stdout = b'{"type":"turn.failed","error":{"code":"rate_limit"}}'
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, stdout)
        == "capacity_unavailable"
    )


def test_codex_quota_event_amid_noise_still_classifies(tmp_path: Path) -> None:
    stdout = (
        b"WARNING: skills context budget exceeded\n"
        + _CODEX_USAGE_LIMIT_EVENT
        + b"\nnot json trailing line"
    )
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, stdout)
        == "capacity_unavailable"
    )


def test_codex_error_event_without_quota_markers_fails_closed(tmp_path: Path) -> None:
    stdout = b'{"type":"error","message":"internal server error"}'
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, stdout)
        == "provider_process_failure"
    )


def test_codex_agent_message_with_marker_text_fails_closed(tmp_path: Path) -> None:
    stdout = (
        b'{"type":"item.completed","item":{"type":"agent_message",'
        b'"text":"the api rate limit is 10 requests per minute"}}'
    )
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, stdout)
        == "provider_process_failure"
    )


def test_codex_quota_event_on_stderr_only_fails_closed(tmp_path: Path) -> None:
    assert (
        _classified_category(
            tmp_path, ProviderId.CODEX, b"", stderr=_CODEX_USAGE_LIMIT_EVENT
        )
        == "provider_process_failure"
    )


def test_codex_non_json_stdout_fails_closed(tmp_path: Path) -> None:
    stdout = b"plain text mentioning usage limit\nsecond line"
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, stdout)
        == "provider_process_failure"
    )


def test_codex_undecodable_stdout_fails_closed(tmp_path: Path) -> None:
    stdout = b"\xff\xfe" + _CODEX_USAGE_LIMIT_EVENT
    assert (
        _classified_category(tmp_path, ProviderId.CODEX, stdout)
        == "provider_process_failure"
    )


def test_claude_quota_subtype_result_classifies_as_capacity(tmp_path: Path) -> None:
    stdout = b'{"type":"result","subtype":"error_rate_limit","is_error":true}'
    assert (
        _classified_category(tmp_path, ProviderId.CLAUDE_CODE, stdout)
        == "capacity_unavailable"
    )


def test_claude_marker_only_in_result_text_fails_closed(tmp_path: Path) -> None:
    stdout = (
        b'{"type":"result","subtype":"error_during_execution","is_error":true,'
        b'"result":"I hit a rate limit while calling the api"}'
    )
    assert (
        _classified_category(tmp_path, ProviderId.CLAUDE_CODE, stdout)
        == "provider_process_failure"
    )


def test_claude_informational_rate_limit_event_fails_closed(tmp_path: Path) -> None:
    stdout = b'{"type":"rate_limit_event","rate_limit":{"status":"warning"}}'
    assert (
        _classified_category(tmp_path, ProviderId.CLAUDE_CODE, stdout)
        == "provider_process_failure"
    )


def test_claude_success_result_never_classifies_as_capacity(tmp_path: Path) -> None:
    stdout = b'{"type":"result","subtype":"success","is_error":false}'
    assert (
        _classified_category(tmp_path, ProviderId.CLAUDE_CODE, stdout)
        == "provider_process_failure"
    )
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run (from `F:/tmp/graphite-quota-capacity`):
```bash
PYTHONPATH=F:/tmp/graphite-quota-capacity/src python -m pytest tests/test_routing_process_runner.py -q -k "usage_limit or turn_failed or amid_noise or quota_subtype"
```
Expected: the capacity-classifying tests FAIL with `assert 'provider_process_failure' == 'capacity_unavailable'`. The fails-closed tests pass already (current behavior) — that is expected; they pin the boundary.

- [ ] **Step 3: Implement the structured phase**

In `src/graphite/routing/process_runner.py`:

(a) Add `import json` to the stdlib import block (after `import hashlib`).

(b) After the `_CAPACITY_DIAGNOSTICS` constant (~line 64), add:

```python
QUOTA_MARKERS: Final = ("quota", "rate_limit", "rate limit", "usage_limit", "usage limit")
_CLAUDE_SUBTYPE_MARKERS: Final = ("quota", "rate", "limit")
```

(c) Replace `_classify_nonzero_failure` (end of file) with:

```python
def _classify_nonzero_failure(
    provider: ProviderId,
    stdout: bytes,
    stderr: bytes,
) -> str:
    """Classify one nonzero exit; decodes transiently, returns only an allowlisted category."""
    patterns = _CAPACITY_DIAGNOSTICS[provider]
    for output in (stdout, stderr):
        if any(line.strip() in patterns for line in output.lower().splitlines()):
            return "capacity_unavailable"
    if _stdout_reports_quota(provider, stdout):
        return "capacity_unavailable"
    return "provider_process_failure"


def _stdout_reports_quota(provider: ProviderId, stdout: bytes) -> bool:
    """Detect provider-authored quota/rate-limit error events; retains nothing."""
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return False
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if provider is ProviderId.CODEX:
            if event.get("type") not in {"error", "turn.failed"}:
                continue
            serialized = json.dumps(
                event, ensure_ascii=True, separators=(",", ":")
            ).lower()
            if any(marker in serialized for marker in QUOTA_MARKERS):
                return True
        elif provider is ProviderId.CLAUDE_CODE:
            if event.get("type") != "result" or (
                event.get("subtype") == "success"
                and event.get("is_error") is False
            ):
                continue
            subtype = str(event.get("subtype", "")).lower()
            if any(marker in subtype for marker in _CLAUDE_SUBTYPE_MARKERS):
                return True
    return False
```

Design notes the implementer must preserve: the codex branch serializes the whole event because codex `error`/`turn.failed` payloads are provider-authored (model text lives in `item.*` events, which are skipped by the type gate). The claude branch matches only the `subtype` string because claude error results can embed model-authored text in `result`. The error-shaped condition for claude is `subtype != "success" or is_error is not False` — its negation (`subtype == "success" and is_error is False`) is what `continue`s.

- [ ] **Step 4: Run the full test file, verify all pass**

```bash
PYTHONPATH=F:/tmp/graphite-quota-capacity/src python -m pytest tests/test_routing_process_runner.py -q
```
Expected: all pass, including the three pre-existing capacity tests (`test_exact_allowlisted_capacity_diagnostic_is_sanitized`, `test_ambiguous_capacity_text_does_not_authorize_fallback`, `test_capacity_phrase_embedded_in_other_diagnostic_fails_closed`) unchanged.

- [ ] **Step 5: Lint and commit**

```bash
python -m ruff check src tests
git add src/graphite/routing/process_runner.py tests/test_routing_process_runner.py
git commit -m "feat(routing): structured quota detection in nonzero-exit transport classifier"
```

---

### Task 2: Codex exit-0 parser adopts the shared marker vocabulary

**Files:**
- Modify: `src/graphite/routing/codex_executor.py` (import block ~line 16; `_failure_code` ~line 205)
- Test: `tests/test_routing_codex_executor.py` (append after `test_execution_normalizes_structured_rate_limit_failure`, ~line 677)

**Interfaces:**
- Consumes: `QUOTA_MARKERS` from `graphite.routing.process_runner` (Task 1); existing test helpers `_paths`, `_result`, `_jsonl`, `ScriptedTransport` in the test file.
- Produces: no new names. Behavior change only: usage-limit error events on the exit-0 path raise `AdapterError("quota")` instead of `AdapterError("unavailable")`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_routing_codex_executor.py`:

```python
def test_execution_normalizes_usage_limit_failure_as_quota(tmp_path: Path) -> None:
    executable, workspace, credentials = _paths(tmp_path)
    events = (
        {
            "type": "error",
            "message": (
                "You've hit your usage limit. Visit "
                "https://chatgpt.com/codex/settings/usage to purchase more "
                "credits or try again at Jul 29th, 2026 9:40 AM."
            ),
        },
    )
    with pytest.raises(AdapterError, match="^quota$"):
        execute_codex(
            executable=executable,
            workspace=workspace,
            credential_home=credentials,
            prompt=b"task",
            requested_model="gpt-5.6-codex",
            expected_effective_model="gpt-5.6-codex",
            effort=Effort.XHIGH,
            permission_mode=PermissionMode.READ_ONLY,
            transport=ScriptedTransport([_result(_jsonl(*events))]),
        )
```

- [ ] **Step 2: Run it to verify it fails**

```bash
PYTHONPATH=F:/tmp/graphite-quota-capacity/src python -m pytest tests/test_routing_codex_executor.py::test_execution_normalizes_usage_limit_failure_as_quota -q
```
Expected: FAIL — raises `AdapterError` matching `unavailable`, not `quota` (the message contains none of the current inline markers).

- [ ] **Step 3: Implement**

In `src/graphite/routing/codex_executor.py`:

(a) Extend the `.process_runner` import to include `QUOTA_MARKERS` (keep ruff isort order — uppercase sorts between the `Cli*` names and the lowercase functions):

```python
from .process_runner import (
    CliProcessError,
    CliProcessResult,
    QUOTA_MARKERS,
    decode_cli_output,
    run_cli_process,
)
```

(b) Replace the quota line in `_failure_code`:

```python
def _failure_code(event: dict[str, object]) -> str:
    text = json.dumps(event, ensure_ascii=True, separators=(",", ":")).lower()
    if "auth" in text or "login" in text or "unauthorized" in text:
        return "auth_required"
    if any(marker in text for marker in QUOTA_MARKERS):
        return "quota"
    return "unavailable"
```

The auth check stays first and unchanged. Do not modify `claude_executor.py` — its subtype markers are deliberately untouched (spec §4).

- [ ] **Step 4: Run the full test file, verify all pass**

```bash
PYTHONPATH=F:/tmp/graphite-quota-capacity/src python -m pytest tests/test_routing_codex_executor.py -q
```
Expected: all pass, including the pre-existing `test_execution_normalizes_structured_rate_limit_failure` (rate_limit was already a marker).

- [ ] **Step 5: Lint and commit**

```bash
python -m ruff check src tests
git add src/graphite/routing/codex_executor.py tests/test_routing_codex_executor.py
git commit -m "feat(routing): codex exit-0 failure parser adopts shared quota marker vocabulary"
```

---

### Task 3: Canonical adapter-failure → route-attempt-category mapping

**Files:**
- Modify: `src/graphite/routing/route_pool.py` (import block ~line 11; new function after the `SideEffectState` class, ~line 55)
- Test: `tests/test_route_pool.py` (imports ~line 17; new tests after `test_coordinator_automatically_selects_cross_provider_capacity_fallback`, ~line 474)

**Interfaces:**
- Consumes: `CliProcessFailureDiagnostics` from `graphite.routing.process_runner` (fields: `exit_classification`, `exit_code`, `duration_seconds`, `stdout_sha256`, `stderr_sha256`, `failure_category`; all validated in `__post_init__`); existing test helpers `_pool`, `_authorities`, `_capacity_attempt`, `_approval_authority` in the test file.
- Produces: `failure_category_for_adapter(code: object, diagnostics: CliProcessFailureDiagnostics | None = None) -> str` — public, total, never raises. This is the contract the §9 acceptance harness and any future `RouteRunner` use to build `RouteAttemptEvidence.failure_category`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_route_pool.py`, add `failure_category_for_adapter` to the existing `graphite.routing.route_pool` import block, add a new import line `from graphite.routing.process_runner import CliProcessFailureDiagnostics`, then append:

```python
def _process_diagnostics(category: str) -> CliProcessFailureDiagnostics:
    return CliProcessFailureDiagnostics(
        exit_classification="nonzero_exit",
        exit_code=1,
        duration_seconds=5.6,
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
        failure_category=category,
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("quota", "capacity_unavailable"),
        ("capacity_unavailable", "capacity_unavailable"),
        ("auth_required", "provider_process_failure"),
        ("timeout", "provider_process_failure"),
        ("unavailable", "provider_process_failure"),
        ("model_mismatch", "provider_process_failure"),
        (None, "provider_process_failure"),
        (7, "provider_process_failure"),
    ],
)
def test_failure_category_for_adapter_maps_codes(
    code: object, expected: str
) -> None:
    assert failure_category_for_adapter(code) == expected


def test_failure_category_for_adapter_prefers_transport_diagnostics() -> None:
    assert (
        failure_category_for_adapter(
            "unavailable", _process_diagnostics("capacity_unavailable")
        )
        == "capacity_unavailable"
    )
    assert (
        failure_category_for_adapter(
            "quota", _process_diagnostics("provider_process_failure")
        )
        == "provider_process_failure"
    )


def test_coordinator_advances_on_adapter_quota_mapped_category(
    tmp_path: Path,
) -> None:
    _store, approval_authority = _approval_authority(tmp_path)
    pool = _pool()
    signed = approval_authority.issue(pool)
    persisted: list[RouteExecutionEvidence] = []

    def runner(selection) -> RouteExecutionResult:
        if selection.attempt_ordinal == 1:
            raise RouteAttemptFailure(
                _capacity_attempt(
                    pool,
                    failure_category=failure_category_for_adapter("quota"),
                )
            )
        return RouteExecutionResult(
            candidate_id=selection.candidate.candidate_id,
            candidate_digest=selection.candidate.digest,
            attempt_ordinal=2,
            output="fallback output",
            input_tokens=1_000,
            output_tokens=100,
            duration_ms=5_000,
            cost_microunits=None,
        )

    result = execute_approved_route_pool(
        pool=pool,
        signed_approval=signed,
        approval_authority=approval_authority,
        authority_loader=lambda: _authorities(pool),
        runner=runner,
        evidence_sink=persisted.append,
        repository_quota_tokens=30_000,
        machine_quota_tokens=30_000,
        now=lambda: 150,
    )

    assert result.attempt_ordinal == 2
    assert [item.outcome_category for item in persisted] == [
        "capacity_unavailable",
        "succeeded",
    ]
```

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH=F:/tmp/graphite-quota-capacity/src python -m pytest tests/test_route_pool.py -q -k "failure_category_for_adapter or adapter_quota"
```
Expected: FAIL at import time — `ImportError: cannot import name 'failure_category_for_adapter'`.

- [ ] **Step 3: Implement**

In `src/graphite/routing/route_pool.py`:

(a) Add to the intra-package imports (after the `.lifecycle` import block):

```python
from .process_runner import CliProcessFailureDiagnostics
```

(No cycle: `process_runner` imports only `graphite.probe_process` and `.contracts`.)

(b) After the `SideEffectState` class, add:

```python
def failure_category_for_adapter(
    code: object,
    diagnostics: CliProcessFailureDiagnostics | None = None,
) -> str:
    """Map one adapter failure to an allowlisted route-attempt category."""
    if isinstance(diagnostics, CliProcessFailureDiagnostics):
        return diagnostics.failure_category
    if isinstance(code, str) and code in {"quota", "capacity_unavailable"}:
        return "capacity_unavailable"
    return "provider_process_failure"
```

Semantics the implementer must preserve (spec §6): diagnostics presence means the transport saw the raw bytes — its classification is authoritative in **both** directions; only the exact codes `"quota"` and `"capacity_unavailable"` map to capacity; everything else — including `auth_required`, `timeout`, `cancelled`, protocol and model-identity codes, unknown or non-string input — degrades to `provider_process_failure` (fail-closed: fallback denied). The function is total and never raises.

- [ ] **Step 4: Run the full test file, verify all pass**

```bash
PYTHONPATH=F:/tmp/graphite-quota-capacity/src python -m pytest tests/test_route_pool.py -q
```
Expected: all pass.

- [ ] **Step 5: Lint, run the adjacent suites, commit**

```bash
python -m ruff check src tests
PYTHONPATH=F:/tmp/graphite-quota-capacity/src python -m pytest tests/test_routing_process_runner.py tests/test_routing_codex_executor.py tests/test_routing_claude_executor.py tests/test_route_pool.py -q
git add src/graphite/routing/route_pool.py tests/test_route_pool.py
git commit -m "feat(routing): canonical adapter-failure to route-attempt-category mapping"
```

Expected: all four files green (`test_routing_claude_executor.py` proves the claude adapter is untouched).
