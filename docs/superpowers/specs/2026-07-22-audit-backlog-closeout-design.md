# Audit-backlog closeout — design (T1c)

**Date:** 2026-07-22
**Branch:** `feat/router-prod-hardening` (Track 1, same branch as T1a/T1b; batch-merge at end)
**Status:** design — awaiting operator review
**Spec:** T1c (third and final spec of the router production-readiness program, Track 1 "harden what exists")

## Context

T1a hardened the remote-executor correctness boundary; T1b pinned the verification-oracle
coverage. T1c closes the remaining three standing audit-backlog items so Track 1 is complete:

1. **GRA-QUAL-R01 (ruff):** three lint findings in `tests/test_routing_zai_executor.py`.
2. **GRA-REL-R02 (unbounded review output):** the review packet has no per-field element cap.
3. **GRA-SEC-R01 (overlay LLM egress):** the overlay `base_url` has no scheme/host policy.

Each item was re-grounded in the current code before scoping (the lesson from the T1a
validator Critical: verify against the real files, do not assume the audit's one-line framing).
That grounding materially narrowed two of the three items — recorded per change below.

## Non-goals

- No router changes (T1a/T1b covered the router; the router HTTP path is already SSRF-hardened).
- No CLI wiring of remote providers (Branch-B decision, unchanged).
- No new runtime dependency (`ipaddress`/`urllib.parse` are stdlib).
- No depth cap on `graphite review` (see R02 — impact is node-count-bounded, so a depth cap
  chases nothing).
- No DNS-resolution-based egress filtering (see R01 — `base_url` is operator-controlled, not
  attacker-controlled; there is no DNS-rebinding adversary to defend against).

## Change 1 — Ruff findings (GRA-QUAL-R01)

`tests/test_routing_zai_executor.py` has three ruff findings, verified current on-branch:

- **E401** (line 1): `import hashlib, json, pytest` — multiple imports on one line.
- **E702** (lines 25, 45): `seen.update(kw); return _envelope(...)` — compound statement.

**Fix:** split the import onto three lines; split each compound statement onto two lines.
Behavior-preserving; the file's tests must stay green. `ruff check .` must report zero findings
in this file.

## Change 2 — Bounded review packet output (GRA-REL-R02)

### What grounding changed

The review packet is **loosely** bounded, not unbounded:

- Git-discovered changes are capped at `MAX_GIT_STATUS_RECORDS = 100_000` (`review.py:27`).
- Explicit changes come from `argv` (`normalize_explicit_changes`), OS-argv-bounded.
- The graph bundle is byte-capped at load (`_MAX_REVIEW_GRAPH_BYTES = 128 MiB`, `cli.py:81`),
  so the graph-derived impact lists (`impacted_files`, `likely_tests`, `matched_nodes`,
  `missing`) are node-count-bounded — large, but bounded.
- `--depth` (`_review_depth`, `cli.py:692`) has no upper bound, but depth only drives BFS over
  that byte-capped graph; the reachable set can never exceed the graph's node count. **A depth
  cap would bound nothing new** and is intentionally not added.

The real gap the audit named is the absence of a **per-field element cap**: a pathological but
in-bounds input (a 128 MiB graph with hundreds of thousands of nodes, or a whole-tree 100 k-change
diff) yields a review packet whose list fields, once serialized by `json.dumps(packet)`
(`cli.py:784`), amplify into tens of MiB of memory and stdout/log output.

### Design

Add one module-level cap in `review.py`:

```python
MAX_REVIEW_PACKET_ITEMS = 10_000
```

10 000 sits far above any reviewable change set or genuine impact fan-out (a human cannot review
10 000 files), yet bounds the pathological case to a fixed ceiling.

**Bound the output representation, never the analysis.** All risk/criteria/warning computation in
`build_review_packet` continues to run over the *full* `ordered_changes` and *full* `impact`
lists — so the `deleted`, `sensitive`, and broad-impact detections are unchanged (governance:
never weaken an oracle). Only the serialized list fields emitted in the returned packet are
truncated to `MAX_REVIEW_PACKET_ITEMS`. Every risk threshold is `<= 10` (`BROAD_IMPACT` is
`len(impacted_files) >= 10`; `NO_LIKELY_TESTS`/`MISSING_GRAPH_MATCHES` are non-empty checks), so
truncating a list at 10 000 cannot change any risk flag or acceptance criterion.

Concretely, introduce a helper:

```python
def _bounded_list(values: list[Any]) -> tuple[list[Any], bool]:
    """Return values truncated to MAX_REVIEW_PACKET_ITEMS and whether truncation occurred."""
    if len(values) > MAX_REVIEW_PACKET_ITEMS:
        return values[:MAX_REVIEW_PACKET_ITEMS], True
    return values, False
```

Apply it, in `build_review_packet`, to the five list fields as they are placed into the packet:
`changes` and `impact["impacted_files"]`, `impact["likely_tests"]`, `impact["matched_nodes"]`,
`impact["missing"]`. (The `impact` sub-dict is rebuilt with bounded lists for the packet; the full
`impact` is what risk was computed from, above.)

**Truncation is surfaced, never silent.** When any field is truncated, append one warning to the
packet's existing `warnings` list with a fixed code so the operator sees the review is bounded:

```python
{
    "code": "OUTPUT_TRUNCATED",
    "message": "Review output was truncated to a bounded size; some entries are not shown.",
}
```

A single `OUTPUT_TRUNCATED` warning is emitted if *any* field truncated (not one per field), to
keep the warnings list itself bounded. `format_review_markdown` already renders `warnings`
verbatim, so the marker appears in both `--json` and Markdown output with no formatter change.

This directly answers the audit ("no per-field/total-byte cap") by fixing the element count of
every packet list, which in turn bounds `json.dumps(packet)`, while preserving review integrity
(no dropped change escapes detection — detection ran over the full set — and any drop from the
*visible* list is flagged).

## Change 3 — Provider-class egress policy for the overlay LLM (GRA-SEC-R01)

### What grounding changed — and the confirmed threat model

`base_url` (`llm.py:100-106`, `OpenAICompatibleProvider.__init__`) has exactly three sources,
all operator-controlled:

- the dataclass default `None` (`config.py:43`),
- the `GRAPHITE_LLM_BASE_URL` environment variable (`config.py:170`),
- the `--llm-base-url` CLI argument (`cli.py:130`).

`Config` has **no project-file loader** and `_project_scoped_config` (`cli.py:442`) only rewrites
`output_dir`/`cache_dir`. **`base_url` is never sourced from repo, graph, or any other untrusted
input** — an attacker who commits a config file to a reviewed repository cannot influence it.
There is therefore no attacker-controlled-URL path, and this change is **defense-in-depth against
operator misconfiguration**: the concrete failure it prevents is an operator who fat-fingers a
keyed cloud provider's `base_url` to an internal/loopback host and thereby transmits their
`Authorization: Bearer <api_key>` (`llm.py:129`) to the wrong destination. `_NoRedirectHandler`
(`llm.py:69-82`) already blocks redirect-based egress; this closes the initial-URL gap.

**Operator decision (recorded):** implement the *provider-class* policy — strict egress rules for
the keyed cloud providers, loopback/`http` preserved for the local providers.

### Design

The code already distinguishes provider classes: `_provider_requires_api_key` returns True for
`openai`/`openrouter`/`groq` (`llm.py:227`); the local set is `ollama`/`local`/`lmstudio`/
`lm-studio`/`vllm`. The policy keys off that same class boundary.

Add a dedicated predicate (decoupled from the api-key predicate for clarity, even though the sets
currently coincide):

```python
def _provider_requires_secure_egress(provider: str) -> bool:
    normalized = provider.strip().lower().replace("_", "-")
    return normalized in {"openai", "openrouter", "groq"}
```

Add a base-URL policy function:

```python
import ipaddress
from urllib.parse import urlsplit


def _validate_llm_base_url(base_url: str, *, provider: str) -> None:
    """Apply the provider-class egress policy to an operator-supplied base URL.

    Minimal scheme allow-list for every provider (reject non-HTTP(S) schemes such as
    file://, gopher://, data://). For the keyed cloud providers (openai/openrouter/groq),
    additionally require HTTPS and reject IP-literal loopback/private/link-local/reserved
    hosts and the name 'localhost' — those providers have no legitimate internal target,
    so an internal target there is a misconfiguration that would leak the bearer token.
    Local providers (ollama/lmstudio/vllm) and generic openai-compatible gateways keep
    http + loopback/private, which are intended for them.
    """
    parts = urlsplit(base_url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise LLMConfigurationError("GRAPHITE_LLM_BASE_URL must use http or https")

    if not _provider_requires_secure_egress(provider):
        return

    if scheme != "https":
        raise LLMConfigurationError(
            "this LLM provider requires an https GRAPHITE_LLM_BASE_URL"
        )
    host = parts.hostname or ""
    if host.lower() == "localhost":
        raise LLMConfigurationError(
            "this LLM provider may not target a loopback or private host"
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return  # A DNS name; name-based private targets are out of scope (operator-controlled).
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise LLMConfigurationError(
            "this LLM provider may not target a loopback or private host"
        )
```

Wire it into `OpenAICompatibleProvider.__init__` immediately after `self.base_url` is computed
(`llm.py:106`), passing `cfg.llm_provider`. Also call the scheme-only branch from
`OllamaProvider.__init__` (local providers still reject non-HTTP(S) schemes as cheap DiD, but keep
loopback). The error messages are fixed strings that name the policy without echoing the raw URL,
matching the module's non-reflection convention (`canonical_provider_name`).

**Faithful to the operator's choice and non-breaking:** the well-known public HTTPS endpoints for
openai/openrouter/groq (`_default_openai_base_url`) all satisfy the policy; ollama/lmstudio/vllm
loopback defaults are untouched; a deliberate enterprise private gateway configured as a generic
`openai-compatible` provider is unaffected (only the three named keyed providers are restricted).

## Testing

Test-driven, no live calls, all offline. For each change, a failing test first, then the fix.

- **Ruff:** `PYTHONPATH=src python -m ruff check tests/test_routing_zai_executor.py` reports zero
  findings; the file's pytest tests stay green.
- **R02:** unit tests on `build_review_packet` — (a) a change list and an impact list each just
  over `MAX_REVIEW_PACKET_ITEMS` truncate to the cap and emit exactly one `OUTPUT_TRUNCATED`
  warning; (b) a list at or under the cap is untruncated and emits no such warning; (c) a
  sensitive/deleted change placed *beyond* the cap index still sets its risk signal (proves
  detection runs over the full set, not the truncated output).
- **R01:** unit tests on `_validate_llm_base_url` / provider construction — (a) a keyed provider
  (`openrouter`) with an `http://` or loopback/private-IP `base_url` raises
  `LLMConfigurationError`; (b) a keyed provider with its public HTTPS default succeeds; (c) a
  local provider (`ollama`/`vllm`) with an `http://localhost` `base_url` succeeds; (d) any
  provider with a `file://` scheme raises; (e) a keyed provider with a DNS-name host is accepted
  (name resolution out of scope).

Full suite stays green: 1846 passed / 44 skipped / 2 known Wondershare-CreatorTemp env flakes;
the new tests add to the pass count.

## Risks

- **R02 truncation hiding a change** — mitigated: detection (deleted/sensitive/broad-impact) runs
  over the full set before truncation, and any truncation of the *visible* list emits
  `OUTPUT_TRUNCATED`. No change escapes the oracle; no drop is silent.
- **R01 breaking a legitimate egress** — mitigated: only the three named keyed providers are
  host-restricted; local providers, generic compatible gateways, and all public HTTPS defaults
  are unaffected. Scheme allow-list rejects only non-HTTP(S) schemes, which no provider uses.
- **R01 giving a false sense of SSRF protection** — mitigated by documentation: this is DiD
  against operator misconfiguration, not attacker-controlled-URL defense (no such path exists);
  DNS-name private targets are explicitly out of scope.
