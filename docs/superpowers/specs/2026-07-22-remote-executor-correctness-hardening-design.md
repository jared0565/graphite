# Remote-executor correctness hardening — design

**Date:** 2026-07-22
**Branch:** `feat/router-prod-hardening`
**Status:** design — awaiting review
**Spec:** T1a (first spec of the router production-readiness program, Track 1 "harden what exists")

## Context

A four-audit production-readiness sweep of the router/provider subsystem (2026-07-22)
confirmed the security foundation is strong — zero violations of the three hard rules
(credentials env-only, raw output never persisted, cost/oracle enforced), a TOCTOU-free
approval gate, an integrity-checked money store, and an SSRF-hardened routing HTTP path.

It also found a small set of **latent** correctness defects on the remote/paid executor
path. They are *latent* because the in-repo remote path currently has no caller — only the
external governed live-acceptance harness exercises `execute_openrouter` / `execute_zai`,
and both z.ai and OpenRouter happen to echo a `model` field in practice, so the fail-open
below has never mis-attributed a billed call. These are prerequisites to making that path
trustworthy, not active production incidents.

The router's design posture (ARCHITECTURE.md L154-156) is that `graphite route` is a
claude+codex-only change broker and remote/paid routing runs through the governed harness
("Branch B"). This spec hardens the harness-invoked executor path; it does **not** wire
remote providers into the CLI.

## Goals

Fix the confirmed correctness defects on the remote executor path, with tests, so the
harness-invoked path is trustworthy on money- and identity-critical boundaries.

## Non-goals

- No CLI wiring of remote providers (Branch B decision).
- No z.ai parity / pool-registration / smoke work (that is Track 2).
- No new runtime dependency.
- No change to `service._preflight` (see Deferred).

## Changes

### 1. Model-identity fail-open → fail-closed (money/identity-critical)

`openrouter_executor.py:296-301` and `zai_executor.py:157-162` currently do:

```python
reported_model = envelope.get("model")
if reported_model is not None:          # <-- absent model skips the check entirely
    if not isinstance(reported_model, str):
        raise AdapterError("protocol")
    if reported_model != expected:
        raise AdapterError("model_mismatch")
```

A response that **omits** `model` bypasses the identity guard, and the result is attested
and billed as the approved model. `claude_executor.py:248-256` fails closed here
(`model_identity_unverified`).

**Fix:** require a non-empty string `model` that equals `expected`. Align error codes with
claude:
- absent / non-string / empty `model` → `model_identity_unverified`
- present string but `!= expected` → `model_mismatch` (unchanged)

Rationale for safety: the OpenRouter and z.ai chat-completions APIs return `model` in every
successful completion (standard OpenAI-compatible envelope), so failing closed on an absent
`model` cannot reject a legitimate governed call; the passing routed/verification smokes all
carried `model`.

### 2. json_object response-schema validation (correctness)

`execute_openrouter` supports two `response_format_type` modes. In `json_schema` mode the
provider is asked to enforce the schema (`strict: True`); in `json_object` mode the schema
is **not** forwarded, and the response is validated only as *a* JSON object
(`openrouter_executor.py:311-316` — `json.loads` + `isinstance(dict)`). A governed caller
selecting json_object (the OpenRouter routed-edit smoke does) pins the schema's digest for
provenance but receives, and pays for, any JSON object.

**Fix:** after parsing the completion content to a dict, validate it against
`output_schema` and raise `response_contract_invalid` on mismatch. Apply the validation in
**both** modes as defense-in-depth (json_schema's provider-side enforcement is not
independently trusted on a money path).

**Validator — minimal, in-house, zero new deps.** graphite has no JSON-Schema dependency,
and adding one would enlarge the supply-chain surface the open GRA-SUP-R01 audit item is
about. Implement a small validator (`schema_validation.py`: `is_supported_schema` /
`matches_schema`).

The governed output schemas are **sha256-pinned constants, not attacker input** — so the
validator's job is to enforce the constraints those schemas express, not to police keyword
novelty. (Correction from the first draft of this spec, caught by the whole-branch review:
the real governed schemas use more than a tiny subset — `EDIT_SCHEMA` uses `maxLength`,
`REVIEW_SCHEMA` uses `pattern`, both use `minItems`/`maxItems`/`enum` — so an allow-list that
fails closed on any unknown keyword would reject a live, governed, paid request. That was a
design-premise error.)

The correct contract splits keywords two ways:

- **Combinators** (`anyOf`, `oneOf`, `allOf`, `not`, `$ref`, `patternProperties`, `if`/`then`/
  `else`, `dependencies`, `contains`, `prefixItems`, `additionalItems`, …) and a
  schema-valued `additionalProperties` **change what "valid" means**. `is_supported_schema`
  **fails closed** on these (→ `request_invalid` before the paid call), because the validator
  cannot safely approximate them.
- **Refinements/annotations** (`pattern`, `maxLength`, `minLength`, `format`, `title`, …) are
  **tolerated**. `matches_schema` enforces every one it understands — `type`, `enum`, `const`,
  object `properties`/`required`/`additionalProperties:false`, array `items`/`minItems`/
  `maxItems`, string `pattern` (via `re.fullmatch` on the pinned constant — no ReDoS, the
  pattern is governed, not caller-supplied) / `maxLength` / `minLength` — and ignores any it
  does not. Ignoring a refinement can only under-enforce a single constraint; it can never
  accept a structurally wrong shape (those are combinators, already rejected).

A regression test asserts `is_supported_schema` accepts every actual governed schema constant
(`EDIT_SCHEMA`, `REVIEW_SCHEMA`, `VERIFY_SCHEMA`) and that conforming/non-conforming responses
match/fail against each. This closes the accept-any-object hole for the governed schemas and
does not shatter when a later spec (Track 2) adds a schema using a new refinement keyword.

### 3. z.ai executor negative tests (coverage)

`test_routing_zai_executor.py` has 4 tests and no negative coverage for guards that already
exist in `zai_executor.py` (model check, malformed envelope, protocol). Add:
- `model_mismatch` (present, wrong model)
- `model_identity_unverified` (absent model — the new behavior from change 1)
- malformed envelope (non-dict, missing/!=1 `choices`, non-dict `message`, non-string `content`)
- protocol (non-dict usage, non-int / out-of-range token counts)

This brings z.ai to parity with the claude/codex/openrouter executor test suites.

### 4. Vestigial dead cross-provider guard — delete (clarity, behavior-preserving)

`route_pool.py:312-317`:

```python
if (
    len({item.provider for item in candidates}) > 1
    and not self.allow_cross_provider
    and len(candidates) == 1          # <-- unsatisfiable: >1 provider needs >=2 candidates
):
    raise RoutePoolError("route_pool_invalid")
```

`>1` distinct providers requires `>=2` candidates, contradicting `len(candidates) == 1`, so
the clause is unsatisfiable and never fires.

**Correction to the audit's framing:** "activating" this guard (the audit's suggested
`>= 2`) would be WRONG. `test_route_pool.py:231` (`test_cross_provider_fallback_requires_
explicit_authority`) deliberately constructs a cross-provider pool with
`allow_cross_provider=False` and asserts the denial happens at `select_route` time
(`cross_provider_denied`), not construction. ARCHITECTURE.md L191 confirms the one-step
capacity fallback is an intended feature and `allow_cross_provider` gates the *fallback at
selection time*, not pool construction. Making the guard reject construction would break that
test and forbid a designed feature. The real gate already exists and works.

**Fix:** delete the unsatisfiable clause as dead code. This is behavior-preserving (it never
fired) — all route_pool tests stay green — and removes a misleading guard that implies a
construction-time gate that neither exists nor should.

### 5. Intrinsic cost/token ceilings — conservative review only

The executors validate caller-supplied `max_output_tokens` / `max_cost_microunits` against
very loose intrinsic caps (`MAX_TOKEN_COUNT = 10_000_000`, `MAX_COST_MICROUNITS = 1e9`); the
real backstops are the 4 MiB response cap and 600 s timeout, and the governed harness pins
tight per-run values in its signed manifest. Tightening the intrinsic caps risks rejecting a
legitimate governed range for no real gain.

**Decision:** treat as review-only. Tighten **only** if a value can be shown safe against
every governed range in the OpenRouter/z.ai evidence; otherwise leave the caps and document
that the manifest is the operative bound. No change that could break a governed range ships
in this spec.

## Deferred (intentional)

- `service._preflight` (`service.py:285-293`) hardcodes claude/codex preflight. Under Branch B
  no remote snapshot is ever loaded through `service.py`, so the latent wrong-adapter path is
  unreachable. Left as a documented note; not fixed here.

## Testing

Test-driven, no live calls — every test drives the executors through their existing fake
`transport` seam (the pattern already used across `test_routing_*_executor.py`). For each
change: a failing test first (proving the current fail-open / missing validation / dead
guard), then the minimal fix. The full suite (~1816 passing) must stay green; the two known
Wondershare-CreatorTemp environmental failures are pre-existing and unrelated.

## Risks

- **Validator scope creep** — mitigated by the documented subset + fail-closed-on-unknown-keyword
  rule; it is a bounded function, not a schema engine.
- **Fail-closed on absent `model` rejecting a real call** — mitigated: the OpenAI-compatible
  envelope always carries `model`; all passing governed smokes carried it.
