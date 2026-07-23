# z.ai Multi-File Plain-Text Edit Path — Design Spec

**Date:** 2026-07-22
**Program:** Router production-readiness, Track 2 (z.ai GLM-5.2 parity)
**Worktree/branch:** `feat/router-track2-zai-parity` @ `F:\tmp\graphite-track2-zai-parity`

## Goal

Give z.ai (GLM-5.2) a governed multi-file edit path at parity with OpenRouter's,
by supplying the one production piece that does not yet exist: a parser that
turns `execute_zai`'s plain-text output into the `{"files":[…],
"result":"GRAPHITE_EDIT_OK"}` payload the existing hardened apply engine already
consumes. Extract that apply engine into a provider-agnostic module so both
providers share it.

## Background — what already exists (the reuse win)

The OpenRouter routed edit-smoke flow (harness-orchestrated) is:

```
preflight → create_task_worktree → execute → <bridge> → apply_whole_file_edit
  → collect_diff_evidence → run_validation (git diff --check + pytest)
  → verify_and_save_approved_edit_profile (READ_ONLY→WORKSPACE_WRITE) → store audit
```

Every stage except `<bridge>` and `execute` is already provider-agnostic and is
reused unchanged for z.ai:

- `preflight_zai` (`zai_probe.py`, no-network; operator-pinned pricing) — exists.
- `execute_zai` (`zai_executor.py`, returns a bare plain-text message) — exists;
  the GLM-5.2 edit-format probe (2026-07-22) proved it emits a **complete**
  edit-sized plain-text payload (finish_reason=stop, reasoning_tokens=0).
- `apply_whole_file_edit` — exists (currently in `openrouter_executor.py`); the
  single security gate for file writes (path traversal, symlink/reparse-point
  rejection, scope + byte caps, atomic replace with full rollback).
- `create_task_worktree`, `collect_diff_evidence`, `run_validation`,
  `verify_and_save_approved_edit_profile`, `load_verified_capability_snapshots`,
  `RepositoryStore` — all provider-agnostic, reused unchanged.

For OpenRouter the `<bridge>` is one line — `json.loads(result.message)` —
because its content is already JSON. z.ai returns plain text, so its bridge is a
marker-block parser. **That parser is the only new provider logic.**

## Scope boundary

**In scope (this build — OFFLINE production code only, NO live inference):**
1. Extract the apply engine into a new shared module `edit_apply.py`.
2. New `zai_edit.py`: the edit-protocol constants + `parse_whole_file_edit_text`.
3. Comprehensive unit tests for both, and confirmation the existing
   `apply_whole_file_edit` tests still pass unchanged via re-export.

**Out of scope (explicitly deferred to the next, governed step):**
- The z.ai edit-smoke harness pair (`_prepare/_execute_zai_edit_smoke.py`) and
  the live governed run that performs the READ_ONLY→WORKSPACE_WRITE promotion.
  That is a separate step, built and dry-run offline, then operator-approved.
- The edit **prompt** itself stays a harness-pinned artifact (mirroring
  OpenRouter's pinned `EDIT_PROMPT`), referencing `zai_edit`'s exported protocol
  constants so prompt and parser share one source of truth. Its correctness is
  proven by the live smoke, not a unit test. No production prompt-builder (YAGNI).
- Any change to `graphite route` / CLI. Branch B holds: remote providers stay
  governed-harness-only; the executors are library functions imported by no
  other `src/graphite` module.

## Architecture

### Module 1 — `src/graphite/routing/edit_apply.py` (extraction, no behavior change)

Move, verbatim, from `openrouter_executor.py` into `edit_apply.py`:

- Public: `apply_whole_file_edit`, `EDIT_RESULT_MARKER` (`"GRAPHITE_EDIT_OK"`).
- Constants: `MAX_EDIT_FILE_BYTES`, `MAX_EDIT_TOTAL_BYTES`, `MAX_EDIT_PATH_LENGTH`,
  `MAX_EDIT_SCOPE_FILES`, `_TEMP_SUFFIX`, `_REPARSE_POINT`.
- Helpers: `_validate_edit_path`, `_cleanup_temps`, `_is_reparse_point`.
- It imports `AdapterError` from `claude_executor` (unchanged dependency).

`openrouter_executor.py` then does `from .edit_apply import apply_whole_file_edit,
EDIT_RESULT_MARKER, MAX_EDIT_FILE_BYTES` and **keeps all three in its `__all__`**
(they are exactly the moved public names currently exported there), so every
existing import resolves unchanged. Confirmed consumers of the re-export:
`tests/test_routing_openrouter_executor.py:13` imports `MAX_EDIT_FILE_BYTES` and
`apply_whole_file_edit`; the OpenRouter edit-smoke harness imports
`apply_whole_file_edit`. This is a pure move: no logic edits, and the existing
apply-engine test suite must pass byte-for-byte behavior.

**Invariant:** `apply_whole_file_edit` remains the sole authority on file-write
safety. No validation is removed or weakened by the move.

### Module 2 — `src/graphite/routing/zai_edit.py` (the bridge)

**Protocol constants (the single source of truth shared with the harness prompt):**

- `EDIT_BEGIN_TEMPLATE = "===GRAPHITE BEGIN FILE {path}==="`
- `EDIT_END_TEMPLATE = "===GRAPHITE END FILE {path}==="`
- The completion sentinel is `EDIT_RESULT_MARKER` (`"GRAPHITE_EDIT_OK"`), imported
  from `edit_apply` — one token, dual purpose: the model emits it to signal
  completion, the parser requires it, and it becomes `payload["result"]`.

Path-qualified markers (a) let one message carry N files and (b) make an
accidental collision with a file's own content improbable.

**Function:**

```
parse_whole_file_edit_text(message: str, *, edit_scope: tuple[str, ...]) -> dict
```

- Returns exactly the payload `apply_whole_file_edit` consumes:
  `{"files": [{"path": <p>, "content": <c>}, …], "result": "GRAPHITE_EDIT_OK"}`,
  with one file entry per marker block.
- Raises `AdapterError("response_contract_invalid")` for every non-conformance
  (see taxonomy). Never raises `edit_scope_violation` — that code belongs to
  `apply_whole_file_edit`, the downstream authority.
- The parser performs **structural** parsing plus a **defense-in-depth** scope
  check; it does NOT enforce byte caps, path traversal, symlink, or reparse
  rules — those remain `apply_whole_file_edit`'s job, run again on the payload.

**Parsing algorithm:**

1. Validate `message` is a non-empty `str` (else `response_contract_invalid`).
2. Scan lines for begin markers matching `EDIT_BEGIN_TEMPLATE` with a captured
   `<path>`. For each begin at path P, the **next** line matching an end marker
   must be `EDIT_END_TEMPLATE` for the **same** P. Content is the text strictly
   between the begin-marker line and the end-marker line, taken **verbatim**
   (the parser adds/strips no newlines; the whole-file content must round-trip
   byte-exact for a clean diff).
3. Collect `(path, content)` per block. Require: ≥1 block; no duplicate path;
   the set of block paths **equals** `edit_scope` exactly (missing, extra,
   duplicate, or out-of-scope path ⇒ `response_contract_invalid`).
4. Require the sentinel `EDIT_RESULT_MARKER` to appear in the **residual** text
   (the message with every begin…end block region removed), so a file's own
   content cannot satisfy the completion check.
5. Build and return the payload (files ordered by `edit_scope`).

**Failure taxonomy — all `response_contract_invalid`:** non-str/empty message;
malformed/unbalanced markers; a begin with no matching same-path end before the
next begin; zero blocks; duplicate path; block-path set ≠ `edit_scope`; missing
completion sentinel.

**Known caveat (documented):** a file whose content contains a line
byte-identical to *its own* path-qualified end marker would truncate at that
line. Path-qualified markers + first-matching-end parsing make this improbable
for real code; it is an accepted, documented limitation matching how such
delimited protocols behave.

## Downstream flow (context only — built in the next step)

The z.ai edit-smoke harness will mirror `_execute_openrouter_edit_smoke_r8.py`
with two substitutions: `execute_openrouter(...)` → `execute_zai(...)` (plain
text, pricing via `preflight_zai`), and `json.loads(result.message)` →
`parse_whole_file_edit_text(result.message, edit_scope=…)`. `preflight_zai` is
no-network (no pricing re-observation). Everything else — worktree, apply, diff,
validation, profile promotion, store audit — is identical. First smoke edits
**2 files** (multi-file parity target).

## Testing strategy (TDD, offline)

`tests/test_zai_edit.py`:
- Happy path: 2-file message → payload with both files, correct content, result
  marker; feeding that payload to the real `apply_whole_file_edit` in a temp
  workspace applies both files (integration of parser + apply).
- Content fidelity: content with blank lines, embedded `===`-like but
  non-matching lines, unicode, trailing newline preserved byte-exact.
- Failures (each asserts `response_contract_invalid`): missing sentinel; missing
  one block; extra out-of-scope block; duplicate path; begin without matching
  end; empty/malformed message; block-path set ≠ edit_scope.
- Defense-in-depth: an in-scope-looking payload with a traversal path still
  rejected by `apply_whole_file_edit` (confirms the authority layer is intact).

`edit_apply` extraction:
- The existing apply-engine tests (`tests/test_routing_openrouter_executor.py`)
  continue to pass unchanged via the `openrouter_executor` re-export — run that
  file as the regression gate for the move. Optionally add a thin
  `tests/test_edit_apply.py` importing from the new module to pin the new home.

Run from the worktree root with `PYTHONPATH=src` so branch code is imported, not
the editable install. Ruff-clean each task.

## Governance / invariants

- Branch B intact: no `graphite route` / CLI change; executors remain
  harness-called library functions.
- No live inference in this build. No store writes. No oracle/budget weakening.
- `apply_whole_file_edit` remains the single, unweakened file-write authority;
  the parser is a structural bridge with a defense-in-depth scope check.
