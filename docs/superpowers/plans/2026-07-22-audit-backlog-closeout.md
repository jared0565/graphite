# Audit-backlog Closeout (T1c) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three standing router/overlay audit-backlog items — ruff lint (GRA-QUAL-R01), bounded review output (GRA-REL-R02), and a provider-class overlay egress policy (GRA-SEC-R01) — completing Track 1.

**Architecture:** Three independent changes in three files. Task 1 is a lint-only edit to a test file. Task 2 bounds the review packet's serialized list fields while leaving all risk analysis over the full inputs. Task 3 adds an operator-misconfiguration egress guard to the overlay LLM providers. No shared new interfaces between tasks.

**Tech Stack:** Python 3, stdlib only (`ipaddress`, `urllib.parse`), pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-07-22-audit-backlog-closeout-design.md`

## Global Constraints

Every task's requirements implicitly include this section.

- **Test resolution:** run every test from the worktree root with `PYTHONPATH=src` so the branch code is imported, not the `F:\Projects\graphite` editable install. On Windows PowerShell: `$env:PYTHONPATH="src"; python -m pytest ...`.
- **Commit trailer (exact):** every commit ends with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **No new runtime dependency:** stdlib only.
- **Never weaken an oracle or budget:** in Task 2, all risk-signal / acceptance-criteria / blocker detection MUST run over the *full* change and impact sets. Only the *serialized* packet lists are truncated. Truncating a serialized list must never change a risk signal, criterion, or blocker.
- **Offline only:** no live network calls in any test. Task 3 tests construct providers and validate URLs; they must not open sockets.
- **Green bar:** baseline is **1846 passed / 44 skipped / 2 failed**, where the 2 failures are the known Wondershare-CreatorTemp environmental flakes (`test_doctor.py::test_core_deep_probe_rejects_temp_path_outside_os_temp`, `test_windows_startup.py::test_install_startup_launcher_writes_hidden_vbs_and_idempotent_script`) — NOT regressions. "Green" = no NEW failures. New tests add to the pass count.

---

### Task 1: Ruff findings in `tests/test_routing_zai_executor.py` (GRA-QUAL-R01)

**Files:**
- Modify: `tests/test_routing_zai_executor.py:1`, `:25`, `:45`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing (behavior-preserving lint fix).

Context: the ruff config (`pyproject.toml` `[tool.ruff]`, only `line-length = 100`) uses ruff's default rule set (E4/E7/E9/F); isort (I) rules are NOT enabled, so splitting the import onto plain separate lines will not introduce an I001 finding.

- [ ] **Step 1: Confirm the findings fail first.**

Run: `$env:PYTHONPATH="src"; python -m ruff check tests/test_routing_zai_executor.py`
Expected: reports E401 at line 1 and E702 at lines 25 and 45 (3 findings).

- [ ] **Step 2: Split the multi-import (E401).**

Replace line 1:
```python
import hashlib, json, pytest
```
with:
```python
import hashlib
import json
import pytest
```

- [ ] **Step 3: Split the two compound statements (E702).**

At line 25 (inside `test_execute_zai_returns_plaintext_and_usage`) and line 45 (inside `test_execute_zai_disables_thinking_for_deterministic_plaintext`), replace each occurrence of:
```python
        seen.update(kw); return _envelope("GRAPHITE_PROFILE_OK")
```
with:
```python
        seen.update(kw)
        return _envelope("GRAPHITE_PROFILE_OK")
```
Both occurrences are identical; edit each of the two `def transport(**kw):` bodies. (These are the only two `seen.update(kw); return ...` lines in the file.)

- [ ] **Step 4: Confirm ruff is clean.**

Run: `$env:PYTHONPATH="src"; python -m ruff check tests/test_routing_zai_executor.py`
Expected: `All checks passed!` (zero findings). If any unexpected finding appears, run `python -m ruff check --fix tests/test_routing_zai_executor.py` and re-confirm zero findings.

- [ ] **Step 5: Confirm the file's tests still pass.**

Run: `$env:PYTHONPATH="src"; python -m pytest tests/test_routing_zai_executor.py -q`
Expected: all pass (same count as before; behavior-preserving).

- [ ] **Step 6: Commit.**

```
git add tests/test_routing_zai_executor.py
git commit -m "style(zai-tests): split multi-import and compound statements (ruff E401/E702)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Bound the review packet output (GRA-REL-R02)

**Files:**
- Modify: `src/graphite/review.py` (add a cap constant + `_bounded_list` helper; truncate serialized lists in `build_review_packet`)
- Test: `tests/test_review.py`

**Interfaces:**
- Consumes: existing `build_review_packet(...)`, `Change`, `_review_bundle`, `_packet` (test helpers).
- Produces: module constant `MAX_REVIEW_PACKET_ITEMS = 10_000`; helper `_bounded_list(values) -> tuple[list, bool]`; a new fixed warning `{"code": "OUTPUT_TRUNCATED", "message": ...}` appended to a packet's `warnings` when any serialized list is truncated.

**Design invariant (load-bearing):** risk signals, acceptance criteria, and blockers are already computed from the full `ordered_changes` and full `impact` earlier in `build_review_packet`. Do NOT move that computation. Only build truncated copies for the returned packet. Every risk threshold is `<= 10`, so a 10 000-item cap cannot change any signal.

- [ ] **Step 1: Write the failing tests.**

Add to `tests/test_review.py` (after the existing `_packet` helper / with the other packet tests):

```python
from graphite.review import MAX_REVIEW_PACKET_ITEMS


def test_review_packet_truncates_changes_and_flags_output():
    changes = [Change(f"a/f{index:06d}.py", "modified") for index in range(MAX_REVIEW_PACKET_ITEMS + 1)]
    packet = _packet(changes)
    assert len(packet["changes"]) == MAX_REVIEW_PACKET_ITEMS
    assert {"code": "OUTPUT_TRUNCATED", "message": "Review output was truncated to a bounded size; some entries are not shown."} in packet["warnings"]


def test_review_packet_within_cap_is_not_flagged():
    packet = _packet([Change("src/store.py", "modified")])
    assert len(packet["changes"]) == 1
    assert all(warning.get("code") != "OUTPUT_TRUNCATED" for warning in packet["warnings"])


def test_review_packet_detects_sensitive_change_beyond_cap():
    # A sensitive change sorted BEYOND the cap index is dropped from the serialized
    # list but MUST still raise its risk signal — detection runs over the full set.
    bulk = [Change(f"a/f{index:06d}.py", "modified") for index in range(MAX_REVIEW_PACKET_ITEMS + 1)]
    changes = bulk + [Change("z/pyproject.toml", "modified")]  # basename => sensitive; sorts last
    packet = _packet(changes)
    serialized_paths = {change["path"] for change in packet["changes"]}
    assert "z/pyproject.toml" not in serialized_paths  # truncated out of the visible list
    assert "SENSITIVE_CONFIG" in packet["risk"]["signals"]  # but detected over the full set
    assert len(packet["changes"]) == MAX_REVIEW_PACKET_ITEMS


def test_review_packet_truncates_impact_lists():
    bundle = _review_bundle(dependent_count=MAX_REVIEW_PACKET_ITEMS + 1)
    packet = _packet([Change("src/store.py", "modified")], bundle=bundle)
    assert len(packet["impact"]["impacted_files"]) == MAX_REVIEW_PACKET_ITEMS
    assert {"code": "OUTPUT_TRUNCATED", "message": "Review output was truncated to a bounded size; some entries are not shown."} in packet["warnings"]
```

- [ ] **Step 2: Run the tests to verify they fail.**

Run: `$env:PYTHONPATH="src"; python -m pytest tests/test_review.py -k "truncates or beyond_cap or within_cap" -q`
Expected: FAIL — `MAX_REVIEW_PACKET_ITEMS` does not exist (ImportError) / packets are not truncated.

- [ ] **Step 3: Add the cap constant.**

In `src/graphite/review.py`, directly below `MAX_GIT_STATUS_RECORDS = 100_000` (line 27):
```python
MAX_REVIEW_PACKET_ITEMS = 10_000
```

- [ ] **Step 4: Add the `_bounded_list` helper.**

Add near the other small module helpers in `src/graphite/review.py` (e.g., above `_empty_impact`):
```python
def _bounded_list(values: list[Any]) -> tuple[list[Any], bool]:
    """Return values truncated to MAX_REVIEW_PACKET_ITEMS and whether truncation occurred."""
    if len(values) > MAX_REVIEW_PACKET_ITEMS:
        return values[:MAX_REVIEW_PACKET_ITEMS], True
    return values, False
```

- [ ] **Step 5: Truncate the serialized lists in `build_review_packet`.**

In `src/graphite/review.py`, immediately before the `return {` statement (currently line 338), insert:
```python
    change_dicts, changes_truncated = _bounded_list(
        [change.to_dict() for change in ordered_changes]
    )
    bounded_impact: dict[str, list[str]] = {}
    impact_truncated = False
    for impact_field in ("matched_nodes", "missing", "impacted_files", "likely_tests"):
        bounded_values, field_truncated = _bounded_list(impact[impact_field])
        bounded_impact[impact_field] = bounded_values
        impact_truncated = impact_truncated or field_truncated
    if changes_truncated or impact_truncated:
        warnings.append(
            {
                "code": "OUTPUT_TRUNCATED",
                "message": "Review output was truncated to a bounded size; some entries are not shown.",
            }
        )
```
Then change the returned dict's two fields:
- `"changes": [change.to_dict() for change in ordered_changes],` → `"changes": change_dicts,`
- `"impact": impact,` → `"impact": bounded_impact,`

Leave every other line of the return (risk, acceptance_criteria, warnings, blockers) unchanged. Do not alter the risk/criteria/warning computation above the insertion point.

- [ ] **Step 6: Run the new tests to verify they pass.**

Run: `$env:PYTHONPATH="src"; python -m pytest tests/test_review.py -k "truncates or beyond_cap or within_cap" -q`
Expected: PASS (4 tests).

- [ ] **Step 7: Run the full review test module (no regressions).**

Run: `$env:PYTHONPATH="src"; python -m pytest tests/test_review.py -q`
Expected: all pass (existing tests unaffected — they use small inputs, so no `OUTPUT_TRUNCATED` and no impact-key change).

- [ ] **Step 8: Commit.**

```
git add src/graphite/review.py tests/test_review.py
git commit -m "fix(review): bound serialized review-packet lists (GRA-REL-R02)

Cap changes and impact lists at MAX_REVIEW_PACKET_ITEMS with an
OUTPUT_TRUNCATED warning. Risk/criteria/blocker detection still runs
over the full inputs, so no signal is weakened and no drop is silent.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Provider-class overlay egress policy (GRA-SEC-R01)

**Files:**
- Modify: `src/graphite/llm.py` (add `import ipaddress`, `from urllib.parse import urlsplit`; add `_provider_requires_secure_egress` + `_validate_llm_base_url`; call both providers' `__init__`)
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: existing `OpenAICompatibleProvider`, `OllamaProvider`, `make_provider`, `LLMConfigurationError`, `Config`, `_provider_requires_api_key`.
- Produces: `_provider_requires_secure_egress(provider) -> bool`; `_validate_llm_base_url(base_url, *, provider) -> None` (raises `LLMConfigurationError` on policy violation).

**Policy:** every provider rejects non-`http(s)` schemes. The keyed cloud set (`openai`/`openrouter`/`groq`) additionally requires `https` and rejects IP-literal loopback/private/link-local/reserved/multicast/unspecified hosts and the literal name `localhost`. DNS-name hosts are accepted (name resolution is out of scope — `base_url` is operator-controlled, no rebinding adversary). Local providers (`ollama`/`local`/`lmstudio`/`vllm`) and generic `openai-compatible` gateways keep `http` + loopback/private.

- [ ] **Step 1: Write the failing tests.**

Add to `tests/test_llm.py`:
```python
from graphite.llm import (
    LLMConfigurationError,
    OpenAICompatibleProvider,
    _validate_llm_base_url,
    make_provider,
)


def test_keyed_provider_rejects_http_base_url():
    cfg = Config(llm_provider="openrouter", llm_base_url="http://openrouter.ai/api/v1")
    with pytest.raises(LLMConfigurationError):
        OpenAICompatibleProvider(cfg)


def test_keyed_provider_rejects_loopback_and_private_hosts():
    for base_url in ("https://127.0.0.1/v1", "https://10.0.0.5/v1", "https://[::1]/v1"):
        with pytest.raises(LLMConfigurationError):
            _validate_llm_base_url(base_url, provider="openrouter")


def test_keyed_provider_rejects_localhost_name():
    with pytest.raises(LLMConfigurationError):
        _validate_llm_base_url("https://localhost/v1", provider="groq")


def test_keyed_provider_accepts_public_https_default():
    # openrouter's built-in default base URL must satisfy the policy.
    provider = make_provider(Config(llm_provider="openrouter", llm_mode="cloud"))
    assert provider.base_url == "https://openrouter.ai/api/v1"


def test_keyed_provider_accepts_dns_name_host():
    # A DNS name is out of scope for host filtering (operator-controlled input).
    _validate_llm_base_url("https://internal-gateway.corp/v1", provider="groq")


def test_local_provider_allows_loopback_http():
    provider = make_provider(Config(llm_provider="vllm", llm_base_url="http://localhost:8000/v1"))
    assert provider.base_url == "http://localhost:8000/v1"


def test_any_provider_rejects_non_http_scheme():
    with pytest.raises(LLMConfigurationError):
        _validate_llm_base_url("file:///etc/passwd", provider="ollama")
```

- [ ] **Step 2: Run the tests to verify they fail.**

Run: `$env:PYTHONPATH="src"; python -m pytest tests/test_llm.py -k "provider_ and (rejects or accepts or allows) or non_http" -q`
Expected: FAIL — `_validate_llm_base_url` / `_provider_requires_secure_egress` do not exist, and the loopback/http URLs currently construct without error.

- [ ] **Step 3: Add the imports.**

In `src/graphite/llm.py`, add near the existing imports (top of file):
```python
import ipaddress
from urllib.parse import urlsplit
```

- [ ] **Step 4: Add the predicate and the validator.**

In `src/graphite/llm.py`, add next to `_provider_requires_api_key` (line 227):
```python
def _provider_requires_secure_egress(provider: str) -> bool:
    normalized = provider.strip().lower().replace("_", "-")
    return normalized in {"openai", "openrouter", "groq"}


def _validate_llm_base_url(base_url: str, *, provider: str) -> None:
    """Apply the provider-class egress policy to an operator-supplied base URL.

    Every provider rejects non-HTTP(S) schemes. The keyed cloud providers
    (openai/openrouter/groq) additionally require HTTPS and reject IP-literal
    loopback/private/link-local/reserved hosts and the name 'localhost' -- those
    providers have no legitimate internal target, so an internal target there is a
    misconfiguration that would leak the bearer token. Local and generic
    openai-compatible providers keep http + loopback/private, which are intended.
    DNS-name hosts are accepted; name-based private targets are out of scope
    because base_url is operator-controlled (no DNS-rebinding adversary).
    """
    parts = urlsplit(base_url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise LLMConfigurationError("GRAPHITE_LLM_BASE_URL must use http or https")

    if not _provider_requires_secure_egress(provider):
        return

    if scheme != "https":
        raise LLMConfigurationError("this LLM provider requires an https GRAPHITE_LLM_BASE_URL")
    host = parts.hostname or ""
    if host.lower() == "localhost":
        raise LLMConfigurationError("this LLM provider may not target a loopback or private host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return  # A DNS name; name-based private targets are out of scope.
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise LLMConfigurationError("this LLM provider may not target a loopback or private host")
```

- [ ] **Step 5: Wire the validator into both providers.**

In `OpenAICompatibleProvider.__init__`, immediately after `self.base_url = base_url.rstrip("/")` (line 106):
```python
        _validate_llm_base_url(self.base_url, provider=cfg.llm_provider)
```
In `OllamaProvider.__init__`, immediately after `self.base_url = (cfg.llm_base_url or "http://localhost:11434").rstrip("/")` (line 157):
```python
        _validate_llm_base_url(self.base_url, provider=cfg.llm_provider)
```
(Ollama is not in the keyed set, so this applies only the scheme allow-list and preserves its loopback default.)

- [ ] **Step 6: Run the new tests to verify they pass.**

Run: `$env:PYTHONPATH="src"; python -m pytest tests/test_llm.py -k "provider_ and (rejects or accepts or allows) or non_http" -q`
Expected: PASS (7 tests).

- [ ] **Step 7: Run the full llm test module (no regressions).**

Run: `$env:PYTHONPATH="src"; python -m pytest tests/test_llm.py -q`
Expected: all pass. (Existing tests use `Config()` default provider `ollama` with the loopback default, or `enrich_report` in `mode=none`; the scheme check passes for `http://localhost:11434`.)

- [ ] **Step 8: Confirm ruff is clean on the modified source.**

Run: `$env:PYTHONPATH="src"; python -m ruff check src/graphite/llm.py tests/test_llm.py`
Expected: `All checks passed!`

- [ ] **Step 9: Commit.**

```
git add src/graphite/llm.py tests/test_llm.py
git commit -m "fix(overlay): provider-class egress policy for LLM base_url (GRA-SEC-R01)

Reject non-http(s) schemes for all providers; require https + reject
loopback/private/link-local hosts for the keyed cloud providers
(openai/openrouter/groq). Defense-in-depth against operator
misconfiguration leaking the bearer token; base_url is env/CLI-only.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full suite.**

Run: `$env:PYTHONPATH="src"; python -m pytest -q`
Expected: **1846+ passed / 44 skipped / 2 failed**, where the 2 failures are exactly the known Wondershare-CreatorTemp env flakes (`test_doctor.py::test_core_deep_probe_rejects_temp_path_outside_os_temp`, `test_windows_startup.py::test_install_startup_launcher_writes_hidden_vbs_and_idempotent_script`). New pass count = baseline 1846 + 4 (Task 2) + 7 (Task 3) = 1857. No NEW failures.

- [ ] **Step 2: Confirm ruff is clean repo-wide.**

Run: `$env:PYTHONPATH="src"; python -m ruff check .`
Expected: `All checks passed!` (GRA-QUAL-R01 fully closed).

- [ ] **Step 3: Record the result** in the SDD progress ledger (controller does this, not a subagent).
