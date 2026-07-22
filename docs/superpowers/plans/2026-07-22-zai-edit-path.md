# z.ai Multi-File Plain-Text Edit Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give z.ai (GLM-5.2) a governed multi-file edit path at parity with OpenRouter by adding the one missing production piece — a plain-text→payload parser — and extracting the shared apply engine into a provider-agnostic module.

**Architecture:** Move `apply_whole_file_edit` + friends out of `openrouter_executor.py` into a new `edit_apply.py` (both providers import it; `openrouter_executor` re-exports for backward compatibility). Add `zai_edit.py` whose `parse_whole_file_edit_text` turns `execute_zai`'s plain-text marker output into the exact `{"files":[…],"result":"GRAPHITE_EDIT_OK"}` payload the apply engine already consumes. All other edit-smoke stages are reused unchanged.

**Tech Stack:** Python 3.11+ (stdlib only: `re`), pytest, ruff. Source spec: `docs/superpowers/specs/2026-07-22-zai-edit-path-design.md`.

## Global Constraints

- **Branch B:** no change to `graphite route` / CLI; executors stay harness-called library functions imported by no other `src/graphite` module.
- **Offline only:** no live inference, no store writes, no network in this build.
- **`apply_whole_file_edit` stays the sole, unweakened file-write authority.** No validation removed or softened. The parser does NOT enforce byte caps / traversal / symlink / reparse — only structural parsing plus a defense-in-depth scope-set check.
- **Parser failure code:** every non-conformance raises `AdapterError("response_contract_invalid")`. The parser never raises `edit_scope_violation` (that is the apply engine's code).
- **Task 1 is a pure move:** no logic edits to the apply engine. `tests/test_routing_openrouter_executor.py` must stay green byte-for-byte, via re-export.
- **Test resolution:** run from the worktree root with `PYTHONPATH=src` (PowerShell: `$env:PYTHONPATH="src"; python -m pytest …`) so branch code is imported, not the editable install. **Ruff-clean each task.**

---

## File Structure

- **Create:** `src/graphite/routing/edit_apply.py` — provider-agnostic apply engine (moved verbatim).
- **Modify:** `src/graphite/routing/openrouter_executor.py` — remove the moved definitions; import + re-export them from `edit_apply`.
- **Create:** `src/graphite/routing/zai_edit.py` — edit-protocol constants + `parse_whole_file_edit_text`.
- **Create:** `tests/test_edit_apply.py` — pins the new home + re-export identity.
- **Create:** `tests/test_zai_edit.py` — parser TDD suite + parser→apply integration.
- **Unchanged regression gate:** `tests/test_routing_openrouter_executor.py`.

---

## Task 1: Extract the shared apply engine into `edit_apply.py`

**Files:**
- Create: `src/graphite/routing/edit_apply.py`
- Modify: `src/graphite/routing/openrouter_executor.py:41-486` (remove moved defs; add import + re-export)
- Create: `tests/test_edit_apply.py`
- Regression: `tests/test_routing_openrouter_executor.py`

**Interfaces:**
- Produces: `edit_apply.apply_whole_file_edit(*, workspace, payload, edit_scope, max_total_bytes) -> tuple[str, ...]`; `edit_apply.EDIT_RESULT_MARKER = "GRAPHITE_EDIT_OK"`; `edit_apply.MAX_EDIT_FILE_BYTES`, `MAX_EDIT_TOTAL_BYTES`, `MAX_EDIT_PATH_LENGTH`, `MAX_EDIT_SCOPE_FILES`.
- Consumes: `AdapterError` from `.claude_executor`.

The moved code is exactly these names from `openrouter_executor.py`, verbatim:
constants `MAX_EDIT_FILE_BYTES`, `MAX_EDIT_TOTAL_BYTES`, `MAX_EDIT_PATH_LENGTH`, `MAX_EDIT_SCOPE_FILES`, `EDIT_RESULT_MARKER`, `_TEMP_SUFFIX`, `_REPARSE_POINT`; helpers `_is_reparse_point`, `_validate_edit_path`, `_cleanup_temps`; and `apply_whole_file_edit`. Their imports needed in the new module: `os`, `stat`, `from pathlib import Path`, `from typing import Final`, `from .claude_executor import AdapterError`.

- [ ] **Step 1: Write the failing test for the new module**

Create `tests/test_edit_apply.py`:

```python
"""The shared, provider-agnostic whole-file apply engine lives in edit_apply."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.routing.claude_executor import AdapterError
from graphite.routing.edit_apply import (
    EDIT_RESULT_MARKER,
    MAX_EDIT_FILE_BYTES,
    apply_whole_file_edit,
)


def test_result_marker_value():
    assert EDIT_RESULT_MARKER == "GRAPHITE_EDIT_OK"
    assert MAX_EDIT_FILE_BYTES == 1_048_576


def test_openrouter_reexports_are_the_same_objects():
    from graphite.routing import edit_apply, openrouter_executor

    assert openrouter_executor.apply_whole_file_edit is edit_apply.apply_whole_file_edit
    assert openrouter_executor.EDIT_RESULT_MARKER is edit_apply.EDIT_RESULT_MARKER
    assert openrouter_executor.MAX_EDIT_FILE_BYTES == edit_apply.MAX_EDIT_FILE_BYTES


def test_applies_a_single_file_atomically(tmp_path: Path):
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    payload = {
        "files": [{"path": "a.py", "content": "new\n"}],
        "result": EDIT_RESULT_MARKER,
    }
    applied = apply_whole_file_edit(
        workspace=tmp_path, payload=payload, edit_scope=("a.py",), max_total_bytes=4096
    )
    assert applied == ("a.py",)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "new\n"


def test_rejects_traversal_path(tmp_path: Path):
    payload = {
        "files": [{"path": "../escape.py", "content": "x\n"}],
        "result": EDIT_RESULT_MARKER,
    }
    with pytest.raises(AdapterError) as info:
        apply_whole_file_edit(
            workspace=tmp_path,
            payload=payload,
            edit_scope=("../escape.py",),
            max_total_bytes=4096,
        )
    assert info.value.code == "edit_scope_violation"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `$env:PYTHONPATH="src"; python -m pytest tests/test_edit_apply.py -q`
Expected: FAIL — `ModuleNotFoundError: graphite.routing.edit_apply`.

- [ ] **Step 3: Create `edit_apply.py` by moving the code verbatim**

Create `src/graphite/routing/edit_apply.py` with a module docstring, the imports listed in Interfaces, and the moved constants/helpers/`apply_whole_file_edit` **copied exactly** from `openrouter_executor.py` (no logic changes). Header:

```python
"""Provider-agnostic, hardened whole-file edit apply engine.

The single authority on edit-payload safety: path-traversal rejection,
symlink/reparse-point rejection, per-file and total byte caps, and atomic
replace with full rollback on any mid-set failure. Shared by every provider
executor (OpenRouter, z.ai); it consumes a validated payload and is agnostic
to how that payload was produced (JSON vs plain-text marker parse)."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final

from .claude_executor import AdapterError
```
Then paste the moved `MAX_EDIT_*`, `EDIT_RESULT_MARKER`, `_TEMP_SUFFIX`, `_REPARSE_POINT`, `_is_reparse_point`, `_validate_edit_path`, `_cleanup_temps`, `apply_whole_file_edit` — unchanged.

- [ ] **Step 4: Rewire `openrouter_executor.py` to import + re-export**

In `openrouter_executor.py`: delete the moved definitions (the `MAX_EDIT_*` constants, `EDIT_RESULT_MARKER`, `_TEMP_SUFFIX`, `_REPARSE_POINT`, `_is_reparse_point`, `_validate_edit_path`, `_cleanup_temps`, `apply_whole_file_edit`). Then re-export exactly the three names that `__all__` publishes and external code imports:

```python
from .edit_apply import (
    EDIT_RESULT_MARKER,
    MAX_EDIT_FILE_BYTES,
    apply_whole_file_edit,
)
```

Import **only** those three — they are the moved names in `__all__` (lines 68–72) and the only moved names referenced outside this module (`test_routing_openrouter_executor.py`, the OpenRouter harness). `MAX_EDIT_TOTAL_BYTES` / `MAX_EDIT_PATH_LENGTH` / `MAX_EDIT_SCOPE_FILES` were private to the apply engine and are referenced nowhere else — do **not** import them (ruff F401). Leave `__all__` unchanged. Finally, remove the now-unused top-level `import os` and `import stat` (they served only the moved apply engine — confirm with a grep that nothing remaining in the file uses `os.` or `stat.`), to stay ruff-clean.

- [ ] **Step 5: Run the new test AND the regression suite**

Run: `$env:PYTHONPATH="src"; python -m pytest tests/test_edit_apply.py tests/test_routing_openrouter_executor.py -q`
Expected: PASS (both). The OpenRouter suite passing unchanged proves the move preserved behavior and the re-export is complete.

- [ ] **Step 6: Ruff + commit**

Run: `$env:PYTHONPATH="src"; python -m ruff check src/graphite/routing/edit_apply.py src/graphite/routing/openrouter_executor.py tests/test_edit_apply.py`
Expected: clean.

```bash
git add src/graphite/routing/edit_apply.py src/graphite/routing/openrouter_executor.py tests/test_edit_apply.py
git commit -m "refactor(routing): extract shared apply engine to edit_apply.py"
```

---

## Task 2: `zai_edit.py` — plain-text → apply payload parser

**Files:**
- Create: `src/graphite/routing/zai_edit.py`
- Test: `tests/test_zai_edit.py`

**Interfaces:**
- Consumes: `AdapterError` from `.claude_executor`; `EDIT_RESULT_MARKER`, `apply_whole_file_edit` from `.edit_apply` (Task 1).
- Produces: `EDIT_BEGIN_TEMPLATE`, `EDIT_END_TEMPLATE` (str templates with a `{path}` field); `parse_whole_file_edit_text(message: str, *, edit_scope: tuple[str, ...]) -> dict` returning `{"files":[{"path","content"}, …], "result":"GRAPHITE_EDIT_OK"}` (files ordered by `edit_scope`) or raising `AdapterError("response_contract_invalid")`.

**Content semantics (byte-exact):** for each block, content is `message[<offset just after the begin-marker line's newline> : <offset where the end-marker line begins>]` — taken verbatim from the raw message, so a file's trailing newline is preserved and the applied file round-trips byte-exact. Markers are matched on their own line (a trailing `\r` is tolerated for detection only; content bytes are never altered).

- [ ] **Step 1: Write the failing tests (happy path + protocol constants)**

Create `tests/test_zai_edit.py`:

```python
"""Parser: z.ai plain-text whole-file edit response -> apply payload."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.routing.claude_executor import AdapterError
from graphite.routing.edit_apply import EDIT_RESULT_MARKER, apply_whole_file_edit
from graphite.routing.zai_edit import (
    EDIT_BEGIN_TEMPLATE,
    EDIT_END_TEMPLATE,
    parse_whole_file_edit_text,
)


def _block(path: str, content: str) -> str:
    return (
        EDIT_BEGIN_TEMPLATE.format(path=path)
        + "\n"
        + content
        + EDIT_END_TEMPLATE.format(path=path)
        + "\n"
    )


def _message(*blocks: str, sentinel: bool = True) -> str:
    body = "".join(blocks)
    return body + (f"{EDIT_RESULT_MARKER}\n" if sentinel else "done\n")


def test_templates_and_sentinel():
    assert EDIT_BEGIN_TEMPLATE.format(path="a/b.py") == "===GRAPHITE BEGIN FILE a/b.py==="
    assert EDIT_END_TEMPLATE.format(path="a/b.py") == "===GRAPHITE END FILE a/b.py==="
    assert EDIT_RESULT_MARKER == "GRAPHITE_EDIT_OK"


def test_parses_two_files_ordered_by_scope():
    msg = _message(_block("y.py", "def y():\n    return 2\n"),
                   _block("x.py", "def x():\n    return 1\n"))
    payload = parse_whole_file_edit_text(msg, edit_scope=("x.py", "y.py"))
    assert payload["result"] == EDIT_RESULT_MARKER
    assert [f["path"] for f in payload["files"]] == ["x.py", "y.py"]
    assert payload["files"][0]["content"] == "def x():\n    return 1\n"
    assert payload["files"][1]["content"] == "def y():\n    return 2\n"


def test_trailing_newline_preserved_byte_exact():
    msg = _message(_block("a.py", "line1\nline2\n"))
    payload = parse_whole_file_edit_text(msg, edit_scope=("a.py",))
    assert payload["files"][0]["content"] == "line1\nline2\n"


def test_content_with_pseudo_marker_line_not_matching_path():
    # A content line containing '===' but not a real path-qualified marker.
    body = "x = '=== not a marker ==='\n"
    msg = _message(_block("a.py", body))
    payload = parse_whole_file_edit_text(msg, edit_scope=("a.py",))
    assert payload["files"][0]["content"] == body
```

- [ ] **Step 2: Run to verify failure**

Run: `$env:PYTHONPATH="src"; python -m pytest tests/test_zai_edit.py -q`
Expected: FAIL — `ModuleNotFoundError: graphite.routing.zai_edit`.

- [ ] **Step 3: Implement `zai_edit.py`**

Create `src/graphite/routing/zai_edit.py`:

```python
"""Bridge a z.ai plain-text whole-file edit response into the shared apply payload.

z.ai's native executor returns a bare plain-text message (no JSON envelope), so
the model emits each edited file verbatim between path-qualified markers and a
completion sentinel. This parser converts that into the exact payload
``apply_whole_file_edit`` consumes. It performs structural parsing plus a
defense-in-depth scope-set check only; ``apply_whole_file_edit`` remains the
sole authority on path safety and byte caps. Every non-conformance raises
``AdapterError("response_contract_invalid")``.

Preamble and interstitial prose (e.g. "Here are the files:") are tolerated by
design — non-marker lines outside blocks are skipped and the live smoke relies
on this. Two accepted, documented consequences: (1) a file whose content
contains a line byte-identical to its own path-qualified end marker truncates
at that line; (2) prose containing a begin-marker-shaped line yields a phantom
block, which surfaces as a scope-set mismatch (rejected, not applied).
Path-qualified markers plus first-matching-end parsing make both improbable.
"""
from __future__ import annotations

import re

from .claude_executor import AdapterError
from .edit_apply import EDIT_RESULT_MARKER

EDIT_BEGIN_TEMPLATE = "===GRAPHITE BEGIN FILE {path}==="
EDIT_END_TEMPLATE = "===GRAPHITE END FILE {path}==="

# Trailing spaces/tabs and a CR are tolerated on marker lines (models append
# them constantly); a content line must still start with the exact marker
# prefix to match, so this stays false-positive-safe.
_BEGIN_RE = re.compile(r"^===GRAPHITE BEGIN FILE (?P<path>.+?)===[ \t]*\r?$")
_END_RE = re.compile(r"^===GRAPHITE END FILE (?P<path>.+?)===[ \t]*\r?$")


def _fail() -> AdapterError:
    return AdapterError("response_contract_invalid")


def parse_whole_file_edit_text(message: str, *, edit_scope: tuple[str, ...]) -> dict:
    """Parse a plain-text multi-file edit response into an apply payload.

    Returns ``{"files": [{"path","content"}, …], "result": EDIT_RESULT_MARKER}``
    with files ordered to match ``edit_scope``. Raises
    ``AdapterError("response_contract_invalid")`` on any non-conformance.
    """
    if not isinstance(message, str) or not message:
        raise _fail()
    if (
        not isinstance(edit_scope, tuple)
        or not edit_scope
        or len(set(edit_scope)) != len(edit_scope)
    ):
        raise _fail()

    lines = message.splitlines(keepends=True)
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    total = offset

    files: list[dict[str, str]] = []
    seen: set[str] = set()
    covered: list[tuple[int, int]] = []
    idx = 0
    n = len(lines)
    while idx < n:
        begin = _BEGIN_RE.match(lines[idx].rstrip("\n"))
        if begin is None:
            idx += 1
            continue
        path = begin.group("path")
        content_start = starts[idx] + len(lines[idx])
        jdx = idx + 1
        end_idx: int | None = None
        while jdx < n:
            stripped = lines[jdx].rstrip("\n")
            end = _END_RE.match(stripped)
            if end is not None and end.group("path") == path:
                end_idx = jdx
                break
            if _BEGIN_RE.match(stripped) is not None:
                break  # a new begin before our end -> malformed block
            jdx += 1
        if end_idx is None:
            raise _fail()
        if path in seen:
            raise _fail()
        seen.add(path)
        files.append({"path": path, "content": message[content_start:starts[end_idx]]})
        covered.append((starts[idx], starts[end_idx] + len(lines[end_idx])))
        idx = end_idx + 1

    if not files or seen != set(edit_scope):
        raise _fail()

    residual: list[str] = []
    cursor = 0
    for block_start, block_end in covered:
        residual.append(message[cursor:block_start])
        cursor = block_end
    residual.append(message[cursor:total])
    if EDIT_RESULT_MARKER not in "".join(residual):
        raise _fail()

    ordered = sorted(files, key=lambda item: edit_scope.index(item["path"]))
    return {"files": ordered, "result": EDIT_RESULT_MARKER}
```

- [ ] **Step 4: Run happy-path tests to green**

Run: `$env:PYTHONPATH="src"; python -m pytest tests/test_zai_edit.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Write the failure-taxonomy + integration tests**

Append to `tests/test_zai_edit.py`:

```python
def test_missing_sentinel_rejected():
    msg = _message(_block("a.py", "x\n"), sentinel=False)
    with pytest.raises(AdapterError) as info:
        parse_whole_file_edit_text(msg, edit_scope=("a.py",))
    assert info.value.code == "response_contract_invalid"


def test_missing_one_block_rejected():
    msg = _message(_block("a.py", "x\n"))
    with pytest.raises(AdapterError) as info:
        parse_whole_file_edit_text(msg, edit_scope=("a.py", "b.py"))
    assert info.value.code == "response_contract_invalid"


def test_extra_out_of_scope_block_rejected():
    msg = _message(_block("a.py", "x\n"), _block("evil.py", "y\n"))
    with pytest.raises(AdapterError) as info:
        parse_whole_file_edit_text(msg, edit_scope=("a.py",))
    assert info.value.code == "response_contract_invalid"


def test_duplicate_path_rejected():
    msg = _message(_block("a.py", "x\n"), _block("a.py", "y\n"))
    with pytest.raises(AdapterError) as info:
        parse_whole_file_edit_text(msg, edit_scope=("a.py",))
    assert info.value.code == "response_contract_invalid"


def test_begin_without_matching_end_rejected():
    msg = (
        EDIT_BEGIN_TEMPLATE.format(path="a.py") + "\nx\n"
        + f"{EDIT_RESULT_MARKER}\n"
    )
    with pytest.raises(AdapterError) as info:
        parse_whole_file_edit_text(msg, edit_scope=("a.py",))
    assert info.value.code == "response_contract_invalid"


def test_empty_and_non_str_rejected():
    for bad in ("", 123, None):
        with pytest.raises(AdapterError) as info:
            parse_whole_file_edit_text(bad, edit_scope=("a.py",))  # type: ignore[arg-type]
        assert info.value.code == "response_contract_invalid"


def test_parsed_payload_applies_via_apply_engine(tmp_path: Path):
    (tmp_path / "x.py").write_text("old x\n", encoding="utf-8")
    (tmp_path / "y.py").write_text("old y\n", encoding="utf-8")
    msg = _message(_block("x.py", "new x\n"), _block("y.py", "new y\n"))
    payload = parse_whole_file_edit_text(msg, edit_scope=("x.py", "y.py"))
    applied = apply_whole_file_edit(
        workspace=tmp_path, payload=payload, edit_scope=("x.py", "y.py"),
        max_total_bytes=4096,
    )
    assert set(applied) == {"x.py", "y.py"}
    assert (tmp_path / "x.py").read_text(encoding="utf-8") == "new x\n"
    assert (tmp_path / "y.py").read_text(encoding="utf-8") == "new y\n"


def test_preamble_and_interstitial_prose_tolerated():
    # The live smoke relies on this: models emit prose despite instructions.
    msg = (
        "Here are the edited files:\n"
        + _block("x.py", "new x\n")
        + "\nAnd the second one:\n\n"
        + _block("y.py", "new y\n")
        + f"{EDIT_RESULT_MARKER}\n"
    )
    payload = parse_whole_file_edit_text(msg, edit_scope=("x.py", "y.py"))
    assert [f["path"] for f in payload["files"]] == ["x.py", "y.py"]
    assert payload["files"][0]["content"] == "new x\n"
    assert payload["files"][1]["content"] == "new y\n"


def test_trailing_whitespace_on_marker_lines_tolerated():
    msg = (
        "===GRAPHITE BEGIN FILE a.py===  \n"
        "body\n"
        "===GRAPHITE END FILE a.py===\t\n"
        f"{EDIT_RESULT_MARKER}\n"
    )
    payload = parse_whole_file_edit_text(msg, edit_scope=("a.py",))
    assert payload["files"][0]["content"] == "body\n"
```

- [ ] **Step 6: Run the full suite**

Run: `$env:PYTHONPATH="src"; python -m pytest tests/test_zai_edit.py -q`
Expected: PASS (all). If any failure test does not already pass with the Step 3 parser, fix the parser (do not weaken a test).

- [ ] **Step 7: Ruff + commit**

Run: `$env:PYTHONPATH="src"; python -m ruff check src/graphite/routing/zai_edit.py tests/test_zai_edit.py`
Expected: clean.

```bash
git add src/graphite/routing/zai_edit.py tests/test_zai_edit.py
git commit -m "feat(routing): z.ai plain-text multi-file edit parser (zai_edit)"
```

---

## Final Whole-Branch Review

After both tasks: run the full routing suite as a regression check
(`$env:PYTHONPATH="src"; python -m pytest tests/ -q`; the two known Wondershare-CreatorTemp
env flakes in `test_doctor.py` / `test_windows_startup.py` are not regressions),
then dispatch the final code reviewer (superpowers:requesting-code-review) on the
branch diff. Confirm: Branch B intact (no CLI/route change), apply engine
unweakened, parser raises only `response_contract_invalid`, re-export complete.
