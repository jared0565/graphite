# Listing Marker Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** No graphite human-output listing prints a header over an empty body, and none drops an item silently.

**Architecture:** One new pure module, `src/graphite/listing.py`, owns the render contract for a bounded list and is wired at seven call sites across `cli.py`, `context.py`, and `daemon_health.py`. A further change moves the answer-health epistemology lines to column 0 so they cannot be mistaken for list entries. Renderer-only: no result dict, JSON payload, or published schema changes.

**Site 7 (context neighbours) is deliberately absent.** Planning found it is a different defect — a hardcoded render cap that overrides `--neighbor-limit`, over a data-layer cap that discards the total, affecting `context --json` as well. Fixing it requires changing the result payload, which this round's R6 forbids. Filed as issue #11; spec §7.1.

**Tech Stack:** Python 3.11+, pytest, ruff. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-26-listing-marker-contract-design.md`

## Global Constraints

- **Renderer-only (spec R6).** No change to any result dict, JSON payload, or published schema. `DOC_VERSION` stays at `9` in `src/graphite/init.py`. No consumer-repo rollout.
- **Truncation wording is `... N more`**, matching the existing precedent at `daemon_health.py:349`. Not "and N more", not "…".
- **The truncation line carries no `- ` bullet** (spec R3). The empty line does carry one.
- **Marker indentation matches the list it belongs to** — two spaces in `cli.py`, empty string in `context.py` (which renders at column 0 with backticks).
- **Run the suite by redirecting to a file and reading `$?` directly.** `python -m pytest > out.txt 2>&1; echo $?` — never `pytest | tail`, which reports the pipe's status and has made failing runs look green.
- **Branch:** `feat/listing-marker-contract`, base `9c0d943`. Spec already committed at `9e77e30`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/graphite/listing.py` | **Create.** `listing_lines()` — the whole contract. Pure, no I/O. |
| `src/graphite/answer_contract.py` | **Modify.** Add `is_degraded()`; the degraded-cell test is currently duplicated in two renderers and this plan would add a third. |
| `src/graphite/cli.py` | **Modify.** Sites 1–4 and 9. |
| `src/graphite/context.py` | **Modify.** Sites 5–6. Neighbours untouched — issue #11. |
| `src/graphite/daemon_health.py` | **Modify.** Site 8. |
| `tests/test_listing.py` | **Create.** Unit tests for the contract. |
| `tests/test_listing_surfaces.py` | **Create.** Table test: every listing surface, its kind, over-cap input. |
| `tests/test_health.py` | **Modify.** One assertion updated; grade-aware empty-marker tests added. |
| `tests/test_context.py` | **Modify.** Markdown-side marker tests added. |

---

### Task 1: The `listing.py` contract module

**Files:**
- Create: `src/graphite/listing.py`
- Test: `tests/test_listing.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `listing_lines(values, render=str, *, header=None, cap=None, indent="  ", empty="none found", more_hint=None) -> list[str]`. Every later task calls this.

**Design note for the implementer.** `empty=None` is the mode that makes the contract enforceable: when the list is empty and `empty is None`, the function returns `[]` — dropping the header along with the body. That is why no call site can produce a bare header. Surfaces that should print nothing when empty pass `empty=None`; surfaces that must answer explicitly pass a string.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_listing.py`:

```python
"""The listing contract: no bare headers, no silent drops."""

from graphite.listing import listing_lines


def test_empty_with_marker_prints_header_and_marker():
    assert listing_lines([], header="Likely tests:") == [
        "Likely tests:",
        "  - none found",
    ]


def test_empty_with_none_marker_drops_the_header_too():
    """empty=None means 'say nothing' — and R1 forbids a header over nothing."""
    assert listing_lines([], header="Likely tests:", empty=None) == []


def test_under_cap_has_no_truncation_line():
    assert listing_lines(["a", "b"], header="Files:", cap=5) == [
        "Files:",
        "  - a",
        "  - b",
    ]


def test_exactly_at_cap_has_no_truncation_line():
    """Off-by-one falsifier: 3 values, cap 3, nothing dropped."""
    assert listing_lines(["a", "b", "c"], cap=3) == ["  - a", "  - b", "  - c"]


def test_over_cap_names_the_number_dropped():
    assert listing_lines(["a", "b", "c", "d", "e"], cap=2) == [
        "  - a",
        "  - b",
        "  ... 3 more",
    ]


def test_truncation_line_carries_no_bullet():
    """R3: the marker must not be readable as a list entry."""
    lines = listing_lines(["a", "b", "c"], cap=1)
    assert lines[-1] == "  ... 2 more"
    assert not lines[-1].lstrip().startswith("- ")


def test_no_cap_shows_everything():
    assert listing_lines(["a", "b", "c"]) == ["  - a", "  - b", "  - c"]
    assert listing_lines(["a", "b", "c"], cap=None) == ["  - a", "  - b", "  - c"]


def test_cap_of_zero_or_less_is_treated_as_no_cap():
    """A body of nothing under a header would violate R1, so 0 means 'no cap'."""
    assert listing_lines(["a", "b"], cap=0) == ["  - a", "  - b"]
    assert listing_lines(["a", "b"], cap=-3) == ["  - a", "  - b"]


def test_render_callback_formats_each_item():
    assert listing_lines([1, 2], lambda v: f"n={v}") == ["  - n=1", "  - n=2"]


def test_custom_indent_applies_to_body_and_marker():
    lines = listing_lines(["a", "b"], cap=1, indent="")
    assert lines == ["- a", "... 1 more"]


def test_more_hint_is_appended_verbatim():
    lines = listing_lines(["a", "b"], cap=1, more_hint=" — use --json for the full list")
    assert lines[-1] == "  ... 1 more — use --json for the full list"


def test_header_omitted_when_none():
    assert listing_lines(["a"]) == ["  - a"]


def test_custom_empty_string():
    assert listing_lines([], header="Deps:", empty="none") == ["Deps:", "  - none"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_listing.py -v > /tmp/t.txt 2>&1; echo $?`
Expected: non-zero. `ModuleNotFoundError: No module named 'graphite.listing'`

- [ ] **Step 3: Write the implementation**

Create `src/graphite/listing.py`:

```python
"""Render a bounded list so that emptiness and truncation are always visible.

Every human-output listing goes through :func:`listing_lines`. Two guarantees
hold by construction: a header is never printed over an empty body, and a
capped list always names how many items it dropped.

See docs/superpowers/specs/2026-07-26-listing-marker-contract-design.md.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

__all__ = ["listing_lines"]


def listing_lines(
    values: Sequence[Any],
    render: Callable[[Any], str] = str,
    *,
    header: str | None = None,
    cap: int | None = None,
    indent: str = "  ",
    empty: str | None = "none found",
    more_hint: str | None = None,
) -> list[str]:
    """Lines for one bounded listing: header, body, and a truncation marker.

    ``cap`` of None (or <= 0) means no cap. ``empty=None`` means the whole
    listing is omitted when there is nothing to show -- header included, since
    a header over an empty body is the defect this module exists to prevent.
    ``more_hint`` is appended verbatim; callers supply their own separator.
    """
    if not values and empty is None:
        return []

    lines: list[str] = []
    if header is not None:
        lines.append(header)

    if not values:
        lines.append(f"{indent}- {empty}")
        return lines

    shown = list(values) if cap is None or cap <= 0 else list(values[:cap])
    lines.extend(f"{indent}- {render(value)}" for value in shown)
    dropped = len(values) - len(shown)
    if dropped > 0:
        lines.append(f"{indent}... {dropped} more{more_hint or ''}")
    return lines
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_listing.py -v > /tmp/t.txt 2>&1; echo $?`
Expected: `0`, 13 passed.

- [ ] **Step 5: Lint**

Run: `python -m ruff check src/graphite/listing.py tests/test_listing.py`
Expected: no findings.

- [ ] **Step 6: Commit**

```bash
git add src/graphite/listing.py tests/test_listing.py
git commit -m "feat(listing): contract module for bounded human-output lists"
```

---

### Task 2: `is_degraded()` and the column-0 epistemology lines (site 9)

**Files:**
- Modify: `src/graphite/answer_contract.py` (add one function after `active_caveats`, ~line 49)
- Modify: `src/graphite/cli.py:1067-1087` (`_answer_lines`)
- Modify: `src/graphite/context.py:165-181`
- Test: `tests/test_health.py:453` (update), `tests/test_listing.py` (no change)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `answer_contract.is_degraded(block: dict[str, Any] | None) -> bool`. Task 3 and Task 6 both use it.

**Why this is its own task.** The degraded-cell test is currently written out twice — `cli.py:1071-1075` and `context.py:166-170` — and Task 3 would add a third copy. Extracting it first means the grade-aware empty marker has one definition of "degraded" to depend on.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_health.py`:

```python
def test_is_degraded_reads_scoped_cells():
    from graphite.answer_contract import is_degraded

    assert is_degraded(None) is False
    assert is_degraded({}) is False
    assert is_degraded({"health": {}}) is False
    assert is_degraded(
        {"health": {"calls": {"python": {"ratio": 0.95, "healthy": True}}}}
    ) is False
    assert is_degraded(
        {"health": {"calls": {"typescript": {"ratio": 0.54, "healthy": False}}}}
    ) is True


def test_answer_lines_render_at_column_zero():
    """R3: these lines must not share the two-space list indentation."""
    from graphite import cli

    block = {
        "grade": "decision_grade",
        "health": {
            "imports": {"python": {"ratio": 0.80, "healthy": True}},
            "calls": {"python": {"ratio": 0.95, "healthy": True}},
        },
        "caveats": [],
    }
    lines = cli._answer_lines(block, empty=True)
    assert lines == ["answer health: calls (python) 0.95, imports (python) 0.80 — decision-grade"]
    assert not lines[0].startswith(" ")
```

- [ ] **Step 2: Update the existing assertion that pins the old indent**

In `tests/test_health.py:453`, change:

```python
    assert lines == ["  answer health: calls (python) 0.95, imports (python) 0.80 — decision-grade"]
```

to:

```python
    assert lines == ["answer health: calls (python) 0.95, imports (python) 0.80 — decision-grade"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_health.py -k "is_degraded or column_zero or sorted" -v > /tmp/t.txt 2>&1; echo $?`
Expected: non-zero. `ImportError: cannot import name 'is_degraded'`, and the indent assertions fail on the two-space prefix.

- [ ] **Step 4: Add `is_degraded` to `answer_contract.py`**

Insert after `active_caveats()` (currently ends at line 48):

```python
def is_degraded(block: dict[str, Any] | None) -> bool:
    """True when any scoped health cell in an answer block is below threshold."""
    if not block:
        return False
    return any(
        not cell.get("healthy", True)
        for langs in block.get("health", {}).values()
        for cell in langs.values()
    )
```

- [ ] **Step 5: Rewrite `_answer_lines` in `cli.py`**

Replace `cli.py:1067-1087` in full:

```python
def _answer_lines(block: dict[str, Any] | None, *, empty: bool) -> list[str]:
    """Human epistemology lines; [] unless empty or a scoped cell is degraded.

    Rendered at column 0, matching context.py and the `note:` line, so they
    cannot be read as entries of the list they follow.
    """
    if not block:
        return []
    if not empty and not is_degraded(block):
        return []
    cells = ", ".join(
        f"{relation} ({language}) {langs[language]['ratio']:.2f}"
        for relation, langs in sorted(block.get("health", {}).items())
        for language in sorted(langs)
    )
    grade = block.get("grade", "").replace("_", "-")
    lines = [f"answer health: {cells} — {grade}"] if cells else [f"answer health: — {grade}"]
    if block.get("caveats"):
        lines.append("known limits: " + "; ".join(c["summary"] for c in block["caveats"]))
    return lines
```

Add `is_degraded` to the existing `answer_contract` import in `cli.py` (the line that already imports `build_answer_block`, `languages_for_nodes`).

- [ ] **Step 6: Use `is_degraded` in `context.py`**

Replace `context.py:165-171`:

```python
    if answer:
        degraded = any(
            not cell.get("healthy", True)
            for langs in answer.get("health", {}).values()
            for cell in langs.values()
        )
        if empty or degraded:
```

with:

```python
    if answer:
        if empty or is_degraded(answer):
```

Add `is_degraded` to `context.py`'s existing `answer_contract` import.

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_health.py tests/test_context.py -v > /tmp/t.txt 2>&1; echo $?`
Expected: `0`.

- [ ] **Step 8: Commit**

```bash
git add src/graphite/answer_contract.py src/graphite/cli.py src/graphite/context.py tests/test_health.py
git commit -m "fix(answer): epistemology lines at column 0; extract is_degraded"
```

---

### Task 3: `cmd_impact` — the #10 fix with a grade-aware empty marker

**Files:**
- Modify: `src/graphite/cli.py:1115-1132` (`cmd_impact` human branch)
- Test: `tests/test_health.py`

**Interfaces:**
- Consumes: `listing_lines` (Task 1), `is_degraded` (Task 2).
- Produces: `cli._empty_marker(block: dict[str, Any] | None) -> str`, private to `cli.py`. Task 6 defines a same-named twin in `context.py` rather than importing this one — `context` must not import `cli`, the dependency runs the other way. Both are three lines over the shared `is_degraded`.

**Why the marker is grade-aware.** `cli.py:405` computes `total = len(impacted_files) + len(likely_tests)`, so an answer with 2 impacted files and 0 tests grades `advisory`, not `inconclusive` — even when the scoped cells are degraded. The `likely_tests` half is nonetheless empty *and* degraded, which is the contract's definition of inconclusive. A bare `none found` there would claim a trustworthy absence the graph did not earn. This is spec §5.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_health.py`:

```python
def _impact_args(files=("src/a.py",)):
    import argparse

    return argparse.Namespace(
        graph_json="graph-out/graph.json", files=list(files), depth=2, json=False
    )


def test_impact_empty_tests_half_is_marked_on_a_healthy_graph(capsys, monkeypatch):
    """Partial-empty answer: the header gets a body, and it claims nothing extra."""
    from graphite import cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: _answer_graph())
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_record_inconclusive", lambda *a, **k: None)
    cli.cmd_impact(_impact_args())
    out = capsys.readouterr().out

    assert "Likely tests:\n  - none found\n" in out
    assert "INCONCLUSIVE" not in out


def test_impact_empty_tests_half_is_marked_inconclusive_when_degraded(capsys, monkeypatch):
    """The firescraper shape (spec §5 falsifier): degraded + empty half.

    Without the grade-aware branch this renders a bare `- none found`, which
    asserts an absence the graph did not earn.
    """
    from graphite import cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: _answer_graph(degraded_ts=True))
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_record_inconclusive", lambda *a, **k: None)
    cli.cmd_impact(_impact_args(files=["src/t.ts"]))
    out = capsys.readouterr().out

    assert "- none found — INCONCLUSIVE: treat as unverified and confirm with grep" in out


def test_impact_never_prints_a_bare_header(capsys, monkeypatch):
    """R1, asserted structurally: no header line is the last line or is
    followed by a line that is not part of its body."""
    from graphite import cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: _answer_graph())
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_record_inconclusive", lambda *a, **k: None)
    cli.cmd_impact(_impact_args())
    lines = capsys.readouterr().out.splitlines()

    for index, line in enumerate(lines):
        if line.endswith(":") and not line.startswith(" "):
            assert index + 1 < len(lines), f"bare header at end of output: {line!r}"
            assert lines[index + 1].startswith("  "), f"bare header: {line!r}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_health.py -k "empty_tests_half or bare_header" -v > /tmp/t.txt 2>&1; echo $?`
Expected: non-zero. The `Likely tests:` header currently has no body line.

- [ ] **Step 3: Add the empty-marker helper to `cli.py`**

Insert immediately above `_answer_lines` (currently line 1067):

```python
_INCONCLUSIVE_EMPTY = "none found — INCONCLUSIVE: treat as unverified and confirm with grep"


def _empty_marker(block: dict[str, Any] | None) -> str:
    """Empty-listing text for an answer surface, scoped to the answer's grade.

    A degraded-and-empty listing is `inconclusive` by the answer contract's own
    definition, even when the answer as a whole graded `advisory` because its
    other half was non-empty. See spec §5.
    """
    return _INCONCLUSIVE_EMPTY if is_degraded(block) else "none found"
```

- [ ] **Step 4: Rewrite the listing branch of `cmd_impact`**

Replace `cli.py:1116-1122`:

```python
            if result["impacted_files"] or result["likely_tests"]:
                print("Impacted files:")
                for path in result["impacted_files"]:
                    print(f"  - {path}")
                print("Likely tests:")
                for path in result["likely_tests"]:
                    print(f"  - {path}")
```

with:

```python
            if result["impacted_files"] or result["likely_tests"]:
                marker = _empty_marker(result.get("answer"))
                for line in listing_lines(
                    result["impacted_files"], header="Impacted files:", empty=marker
                ):
                    print(line)
                for line in listing_lines(
                    result["likely_tests"], header="Likely tests:", empty=marker
                ):
                    print(line)
```

Leave the `else:` branch (both lists empty) unchanged — its `empty_meaning` sentence already covers both halves. Add `from .listing import listing_lines` to `cli.py`'s imports.

Note: no `cap` here. `cmd_impact` prints the full lists today and this round does not add a cap to it.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_health.py -v > /tmp/t.txt 2>&1; echo $?`
Expected: `0`.

- [ ] **Step 6: Commit**

```bash
git add src/graphite/cli.py tests/test_health.py
git commit -m "fix(impact): mark the empty half of a partial answer (#10)"
```

---

### Task 4: Count-in-summary surfaces — `daemon-status` (#8) and `validate`

**Files:**
- Modify: `src/graphite/cli.py:1344-1348` (`cmd_daemon_status`), `src/graphite/cli.py:878-879` (`cmd_validate`)
- Test: `tests/test_daemon_health.py`

**Interfaces:**
- Consumes: `listing_lines` (Task 1).
- Produces: module constants `_DAEMON_STATUS_PROJECT_CAP = 20`, `_VALIDATE_ERROR_CAP = 10` in `cli.py`. Task 7's table test imports both.

**Why no empty marker here.** Neither surface has a list header — `daemon-status` prints rows straight after `[graphite] updated: …`, `validate` after `graph invalid (N errors, …)` — and both summary lines already carry the count, so emptiness is not lost. Both pass `empty=None`, which omits the listing entirely. Spec §4.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daemon_health.py`:

```python
def test_daemon_status_marks_truncation_and_points_at_json(capsys, monkeypatch, tmp_path):
    import argparse

    from graphite import cli

    status = {
        "status": "ok",
        "project_count": 32,
        "failing_projects": 0,
        "pending_projects": 0,
        "updated_at": "2026-07-26T00:00:00Z",
        "projects": [
            {"root": f"/repo{i}", "build_count": 1, "failure_count": 0, "file_count": 10}
            for i in range(32)
        ],
    }
    monkeypatch.setattr(cli, "read_daemon_status", lambda *a, **k: status)
    args = argparse.Namespace(base_path=str(tmp_path), state_dir=None, json=False)
    cli.cmd_daemon_status(args)
    out = capsys.readouterr().out

    assert "  ... 12 more — use --json for the full list" in out
    assert out.count("builds=") == 20


def test_daemon_status_truncation_count_reconciles_with_the_header(capsys, monkeypatch, tmp_path):
    """20 shown + N dropped must equal the count the summary line claims."""
    import argparse
    import re

    from graphite import cli

    status = {
        "status": "ok",
        "project_count": 27,
        "failing_projects": 0,
        "pending_projects": 0,
        "updated_at": "2026-07-26T00:00:00Z",
        "projects": [
            {"root": f"/repo{i}", "build_count": 1, "failure_count": 0, "file_count": 10}
            for i in range(27)
        ],
    }
    monkeypatch.setattr(cli, "read_daemon_status", lambda *a, **k: status)
    cli.cmd_daemon_status(argparse.Namespace(base_path=str(tmp_path), state_dir=None, json=False))
    out = capsys.readouterr().out

    dropped = int(re.search(r"\.\.\. (\d+) more", out).group(1))
    assert out.count("builds=") + dropped == 27


def test_daemon_status_empty_project_list_prints_no_marker(capsys, monkeypatch, tmp_path):
    """Count-in-summary: the header already says 0; a dangling marker would be noise."""
    import argparse

    from graphite import cli

    status = {
        "status": "ok",
        "project_count": 0,
        "failing_projects": 0,
        "pending_projects": 0,
        "updated_at": "2026-07-26T00:00:00Z",
        "projects": [],
    }
    monkeypatch.setattr(cli, "read_daemon_status", lambda *a, **k: status)
    cli.cmd_daemon_status(argparse.Namespace(base_path=str(tmp_path), state_dir=None, json=False))
    out = capsys.readouterr().out

    assert "none found" not in out
    assert "more" not in out


def test_daemon_status_json_stays_uncapped(capsys, monkeypatch, tmp_path):
    import argparse
    import json as jsonlib

    from graphite import cli

    status = {
        "status": "ok",
        "project_count": 32,
        "failing_projects": 0,
        "pending_projects": 0,
        "updated_at": "2026-07-26T00:00:00Z",
        "projects": [
            {"root": f"/repo{i}", "build_count": 1, "failure_count": 0, "file_count": 10}
            for i in range(32)
        ],
    }
    monkeypatch.setattr(cli, "read_daemon_status", lambda *a, **k: status)
    cli.cmd_daemon_status(argparse.Namespace(base_path=str(tmp_path), state_dir=None, json=True))
    payload = jsonlib.loads(capsys.readouterr().out)

    assert len(payload["projects"]) == 32
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_daemon_health.py -k "daemon_status" -v > /tmp/t.txt 2>&1; echo $?`
Expected: non-zero on the truncation tests (no `... N more` is printed today); the JSON test passes already and is a guard.

- [ ] **Step 3: Add the cap constants to `cli.py`**

Near the other module-level constants at the top of `cli.py`:

```python
_DAEMON_STATUS_PROJECT_CAP = 20
_VALIDATE_ERROR_CAP = 10
```

- [ ] **Step 4: Rewrite the `daemon-status` listing**

Replace `cli.py:1344-1348`:

```python
        for project in status.get("projects", [])[:20]:
            print(
                f"  - {project.get('root')} | builds={project.get('build_count')} "
                f"failures={project.get('failure_count')} files={project.get('file_count')}"
            )
```

with:

```python
        for line in listing_lines(
            status.get("projects", []),
            lambda p: (
                f"{p.get('root')} | builds={p.get('build_count')} "
                f"failures={p.get('failure_count')} files={p.get('file_count')}"
            ),
            cap=_DAEMON_STATUS_PROJECT_CAP,
            empty=None,
            more_hint=" — use --json for the full list",
        ):
            print(line)
```

- [ ] **Step 5: Rewrite the `validate` error listing**

Replace `cli.py:878-879`:

```python
            for issue in report["errors"][:10]:
                print(f"  - {issue['code']}: {issue['message']} [{issue['path']}]")
```

with:

```python
            for line in listing_lines(
                report["errors"],
                lambda issue: f"{issue['code']}: {issue['message']} [{issue['path']}]",
                cap=_VALIDATE_ERROR_CAP,
                empty=None,
            ):
                print(line)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_daemon_health.py tests/test_hardening.py -v > /tmp/t.txt 2>&1; echo $?`
Expected: `0`.

- [ ] **Step 7: Commit**

```bash
git add src/graphite/cli.py tests/test_daemon_health.py
git commit -m "fix(cli): truncation markers for daemon-status and validate (#8)"
```

---

### Task 5: Conditional-report surfaces — watch impact and daemon-health

**Files:**
- Modify: `src/graphite/cli.py:462-469` (`_print_watch_impact`)
- Modify: `src/graphite/daemon_health.py:336-350` (`_issue_lines`), `:373-376` (callers)
- Test: `tests/test_daemon_health.py`

**Interfaces:**
- Consumes: `listing_lines` (Task 1).
- Produces: `cli._WATCH_IMPACTED_CAP = 20`, `cli._WATCH_TESTS_CAP = 30`; `daemon_health._issue_lines(label, issues, *, cap=_MAX_ISSUE_LINES)` — signature change, the `[:20]` moves from the caller into the function.

**Deviation, stated deliberately.** `daemon_health._issue_lines` does **not** use `listing_lines`. Its body is grouped by issue code and emits nested detail lines, so it is not a flat list of rendered items. It gets the marker by hand, using the same `... N more` wording it already uses at line 349 for its inner cap. Task 7's table test covers it, so the guarantee is still asserted centrally even though the implementation is local.

**Why watch keeps its guards.** Watch is a change stream, not an answer — it fires on every file save, and `likely tests: none found` per save would add noise to the surface this round protects. Passing `empty=None` reproduces the current silence exactly while adding the truncation marker. Spec §4.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daemon_health.py`:

```python
def _watch_cfg(tmp_path):
    """Config has no `root` field -- the root is a separate argument to
    _print_watch_impact. Only output_dir matters here, since the function
    looks for `cfg.output_dir / "graph.json"`."""
    from graphite.config import Config

    (tmp_path / "graph.json").write_text("{}", encoding="utf-8")
    return Config(output_dir=tmp_path)


def test_watch_impact_marks_truncation(capsys, monkeypatch, tmp_path):
    from graphite import cli

    result = {
        "impacted_files": [f"src/f{i}.py" for i in range(25)],
        "likely_tests": [f"tests/t{i}.py" for i in range(35)],
    }
    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: object())
    monkeypatch.setattr(cli, "_impact", lambda *a, **k: result)

    change = cli.WatchChange(added=[], changed=["src/f0.py"], removed=[])
    cli._print_watch_impact(tmp_path, _watch_cfg(tmp_path), change, 2)
    out = capsys.readouterr().out

    before, after = out.split("likely tests:")
    assert "  ... 5 more" in before
    assert "  ... 5 more" in after


def test_watch_impact_stays_silent_on_empty_lists(capsys, monkeypatch, tmp_path):
    """Conditional report: no header, no marker, nothing at all."""
    from graphite import cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: object())
    monkeypatch.setattr(cli, "_impact", lambda *a, **k: {"impacted_files": [], "likely_tests": []})

    change = cli.WatchChange(added=[], changed=["src/f0.py"], removed=[])
    cli._print_watch_impact(tmp_path, _watch_cfg(tmp_path), change, 2)

    assert capsys.readouterr().out == ""


def test_daemon_health_outer_cap_is_marked():
    from graphite.daemon_health import _issue_lines

    issues = [{"code": f"code{i}", "message": f"m{i}"} for i in range(26)]
    lines = _issue_lines("Errors", issues, cap=20)

    assert lines[0] == "Errors:"
    assert lines[-1] == "  ... 6 more"


def test_daemon_health_under_cap_has_no_marker():
    from graphite.daemon_health import _issue_lines

    issues = [{"code": f"code{i}", "message": f"m{i}"} for i in range(3)]
    lines = _issue_lines("Errors", issues, cap=20)

    assert not any("more" in line for line in lines)
```

Note on the first test: the caps differ (20 impacted, 30 tests) but both lists are over by 5, so the same marker string appears once in each section. The two `split` assertions prove one marker landed in each.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_daemon_health.py -k "watch_impact or daemon_health_outer or under_cap" -v > /tmp/t.txt 2>&1; echo $?`
Expected: non-zero. `_issue_lines` takes no `cap` keyword; watch prints no markers.

- [ ] **Step 3: Add watch cap constants to `cli.py`**

Beside the constants from Task 4:

```python
_WATCH_IMPACTED_CAP = 20
_WATCH_TESTS_CAP = 30
```

- [ ] **Step 4: Rewrite `_print_watch_impact`**

Replace `cli.py:462-469`:

```python
    if result["impacted_files"]:
        print("[graphite] impacted files:")
        for path in result["impacted_files"][:20]:
            print(f"  - {path}")
    if result["likely_tests"]:
        print("[graphite] likely tests:")
        for path in result["likely_tests"][:30]:
            print(f"  - {path}")
```

with:

```python
    for line in listing_lines(
        result["impacted_files"],
        header="[graphite] impacted files:",
        cap=_WATCH_IMPACTED_CAP,
        empty=None,
    ):
        print(line)
    for line in listing_lines(
        result["likely_tests"],
        header="[graphite] likely tests:",
        cap=_WATCH_TESTS_CAP,
        empty=None,
    ):
        print(line)
```

- [ ] **Step 5: Move the outer cap into `_issue_lines`**

In `daemon_health.py`, add a module constant beside `_MAX_GROUP_DETAILS`:

```python
_MAX_ISSUE_LINES = 20
```

Replace `daemon_health.py:336-350` in full:

```python
def _issue_lines(
    label: str, issues: list[dict[str, Any]], *, cap: int = _MAX_ISSUE_LINES
) -> list[str]:
    shown = issues[:cap]
    dropped = len(issues) - len(shown)
    lines = [f"{label}:"]
    groups: dict[str, list[dict[str, Any]]] = {}
    for issue in shown:
        groups.setdefault(str(issue.get("code")), []).append(issue)
    for code, members in groups.items():
        if len(members) == 1:
            lines.append(f"  - {_safe_text(code)}: {_safe_text(members[0].get('message'))}")
            continue
        lines.append(f"  - {_safe_text(code)} ({len(members)}):")
        for issue in members[:_MAX_GROUP_DETAILS]:
            lines.append(f"      {_safe_text(issue.get('message'))}")
        if len(members) > _MAX_GROUP_DETAILS:
            lines.append(f"      ... {len(members) - _MAX_GROUP_DETAILS} more")
    if dropped > 0:
        lines.append(f"  ... {dropped} more")
    return lines
```

- [ ] **Step 6: Drop the caller-side slices**

Replace `daemon_health.py:373-376`:

```python
    if report["errors"]:
        lines.extend(_issue_lines("Errors", report["errors"][:20]))
    if report["warnings"]:
        lines.extend(_issue_lines("Warnings", report["warnings"][:20]))
```

with:

```python
    if report["errors"]:
        lines.extend(_issue_lines("Errors", report["errors"]))
    if report["warnings"]:
        lines.extend(_issue_lines("Warnings", report["warnings"]))
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_daemon_health.py tests/test_daemon.py -v > /tmp/t.txt 2>&1; echo $?`
Expected: `0`.

- [ ] **Step 8: Commit**

```bash
git add src/graphite/cli.py src/graphite/daemon_health.py tests/test_daemon_health.py
git commit -m "fix(cli,daemon-health): truncation markers on conditional reports"
```

---

### Task 6: `context.py` — the markdown answer surfaces

**Files:**
- Modify: `src/graphite/context.py:130-132` (impacted), `:155-157` (tests)
- Test: `tests/test_context.py`

**Interfaces:**
- Consumes: `listing_lines` (Task 1), `is_degraded` (Task 2).
- Produces: `context._CONTEXT_LIST_CAP = 30`. Task 7's table test imports it.

**Do not touch `_append_neighbor_section` (`context.py:201-212`).** Its `[:20]` looked like a sibling defect and is not one: `build_context` already caps neighbours at `neighbor_limit` in `_neighbors`, so at the default the render slice is dead, and above the default it silently overrides the user's `--neighbor-limit`. Live: `context src/graphite/cli.py --neighbor-limit 50` returns 50 dependencies in `--json` and prints 20 in markdown with no marker. A render marker cannot fix it — the true dropped count lives in `_neighbors`, which discards it. Issue #11, spec §7.1.

**Two things to get right.**

1. `context.py` renders list items at **column 0** with backticks — `- \`path\`` — so every call passes `indent=""`. The marker lands at column 0 too.
2. The `Likely tests:` header becomes unconditional **only inside the listing branch**, i.e. when `impact["impacted_files"]` is non-empty. When *both* lists are empty the existing single sentence stands: its `empty_meaning` is already "no impacted files **or tests** reachable through bound edges", which answers both halves, and `cmd_impact` behaves identically. Diverging here would be a bug, not a fix.

`context.py` defines its own `_empty_marker` rather than importing `cli`'s: `context` must not import `cli` (the dependency runs the other way). Both are three lines over the same shared `is_degraded`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_context.py`:

```python
HEALTHY_ANSWER = {"grade": "decision_grade", "health": {}, "caveats": []}
DEGRADED_ANSWER = {
    "grade": "advisory",
    "health": {"calls": {"typescript": {"ratio": 0.54, "healthy": False}}},
    "caveats": [],
}


def _ctx(*, impacted, tests, answer=None, **overrides):
    """A complete context dict.

    format_context_markdown reads metadata, depth, missing, matched, impact,
    direct_dependencies, direct_dependents and risk at the top level -- every
    one of them unguarded. Omitting any raises KeyError, so build the whole
    shape here rather than per test.
    """
    context = {
        "metadata": {"node_count": 2, "edge_count": 1, "density": 0.5},
        "inputs": ["src/a.py"],
        "matched": [],
        "missing": [],
        "depth": 2,
        "direct_dependencies": {},
        "direct_dependents": {},
        "impact": {"impacted_files": impacted, "likely_tests": tests, "missing": []},
        "communities": {},
        "risk": [],
        "resolution_health": {"healthy": True},
        "inconclusive": False,
    }
    if answer is not None:
        context["answer"] = answer
    context.update(overrides)
    return context


def test_context_marks_the_empty_tests_half():
    """Markdown sibling of #10: the block is emitted, not omitted."""
    from graphite.context import format_context_markdown

    text = format_context_markdown(
        _ctx(impacted=["src/b.py"], tests=[], answer=HEALTHY_ANSWER)
    )

    assert "Likely tests:\n- none found" in text


def test_context_marks_the_empty_tests_half_as_inconclusive_when_degraded():
    from graphite.context import format_context_markdown

    text = format_context_markdown(
        _ctx(impacted=["src/b.py"], tests=[], answer=DEGRADED_ANSWER)
    )

    assert "- none found — INCONCLUSIVE: treat as unverified and confirm with grep" in text


def test_context_impacted_files_cap_is_marked():
    from graphite.context import format_context_markdown

    text = format_context_markdown(
        _ctx(impacted=[f"src/f{i}.py" for i in range(35)], tests=[], answer=HEALTHY_ANSWER)
    )

    assert "... 5 more" in text


def test_context_both_halves_empty_keeps_the_single_sentence():
    """Not a bare header and not two blocks -- empty_meaning covers both halves."""
    from graphite.context import format_context_markdown

    answer = dict(
        HEALTHY_ANSWER,
        empty_meaning="no impacted files or tests reachable through bound edges",
    )
    text = format_context_markdown(_ctx(impacted=[], tests=[], answer=answer))

    assert "Impacted files: none found — no impacted files or tests reachable" in text
    assert "Likely tests:" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_context.py -k "marks_the_empty or cap_is_marked or both_halves" -v > /tmp/t.txt 2>&1; echo $?`
Expected: non-zero — the `Likely tests:` block is omitted today and no markers are printed.

- [ ] **Step 3: Add constants and the empty-marker helper to `context.py`**

Near the top of `context.py`, beside the existing module constants:

```python
_CONTEXT_LIST_CAP = 30
_INCONCLUSIVE_EMPTY = "none found — INCONCLUSIVE: treat as unverified and confirm with grep"


def _empty_marker(block: dict[str, Any] | None) -> str:
    """Empty-listing text for an answer surface, scoped to the answer's grade."""
    return _INCONCLUSIVE_EMPTY if is_degraded(block) else "none found"
```

Add `from .listing import listing_lines` to the imports.

- [ ] **Step 4: Rewrite the impacted-files listing**

Replace `context.py:130-132`:

```python
    if impact["impacted_files"]:
        lines.append("Impacted files:")
        lines.extend(f"- `{path}`" for path in impact["impacted_files"][:30])
```

with:

```python
    if impact["impacted_files"]:
        marker = _empty_marker(answer)
        lines.extend(
            listing_lines(
                impact["impacted_files"],
                lambda path: f"`{path}`",
                header="Impacted files:",
                cap=_CONTEXT_LIST_CAP,
                indent="",
                empty=marker,
            )
        )
```

- [ ] **Step 5: Rewrite the likely-tests listing**

Replace `context.py:155-157`:

```python
    if impact["likely_tests"]:
        lines.append("Likely tests:")
        lines.extend(f"- `{path}`" for path in impact["likely_tests"][:30])
```

with:

```python
    if impact["impacted_files"]:
        lines.extend(
            listing_lines(
                impact["likely_tests"],
                lambda path: f"`{path}`",
                header="Likely tests:",
                cap=_CONTEXT_LIST_CAP,
                indent="",
                empty=_empty_marker(answer),
            )
        )
```

Read the condition carefully: it is `impacted_files`, not `likely_tests`. Inside the listing branch the tests half must be answered; when both halves are empty the single sentence above already covers them.

- [ ] **Step 6: Confirm `_append_neighbor_section` is unchanged**

Run:

```bash
git diff src/graphite/context.py > /tmp/ctxdiff.txt
grep -c "_append_neighbor_section\|_neighbors" /tmp/ctxdiff.txt || echo "0 (clean)"
```

Expected: `0 (clean)`. If either name appears in the diff, revert that hunk — it belongs to issue #11, not this round. (`grep -c` exits 1 on zero matches, which is why the count is written to a file and the `|| echo` guard is there rather than reading `$?` from a pipe.)

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_context.py -v > /tmp/t.txt 2>&1; echo $?`
Expected: `0`.

- [ ] **Step 8: Commit**

```bash
git add src/graphite/context.py tests/test_context.py
git commit -m "fix(context): mark the empty tests half and the impacted-files cap"
```

---

### Task 7: Surface table test, full suite, spec truth-up

**Files:**
- Create: `tests/test_listing_surfaces.py`
- Modify: `docs/superpowers/specs/2026-07-26-listing-marker-contract-design.md`

**Interfaces:**
- Consumes: every constant and call site from Tasks 1–6.
- Produces: nothing.

**Purpose.** Tasks 3–6 each assert their own surface. This task asserts the *set* — that every listing surface has a marker, and that a ninth surface cannot be added without declaring its kind. That is what turns the convention from folklore into something a test enforces.

- [ ] **Step 1: Write the table test**

Create `tests/test_listing_surfaces.py`:

```python
"""Every human-output listing surface, its kind, and its marker.

Adding a listing surface means adding a row here. A surface with no row is
not covered by the contract, which is the failure mode this round removed.
See docs/superpowers/specs/2026-07-26-listing-marker-contract-design.md §4.
"""

import re

import pytest

from graphite import cli, context, daemon_health

MARKER = re.compile(r"\.\.\. \d+ more")

# (id, kind) -- kinds are ANSWER, COUNT_IN_SUMMARY, CONDITIONAL (spec §4)
SURFACES = [
    ("cli.cmd_impact", "ANSWER"),
    ("cli.cmd_daemon_status", "COUNT_IN_SUMMARY"),
    ("cli.cmd_validate", "COUNT_IN_SUMMARY"),
    ("cli._print_watch_impact", "CONDITIONAL"),
    ("context.impacted_files", "ANSWER"),
    ("context.likely_tests", "ANSWER"),
    ("daemon_health._issue_lines", "CONDITIONAL"),
]

# context._append_neighbor_section is deliberately absent: its cap is a
# data-layer defect that also affects `context --json`, tracked as issue #11
# and out of this round's renderer-only charter (spec §7.1).


def test_every_surface_declares_a_known_kind():
    assert {kind for _, kind in SURFACES} <= {"ANSWER", "COUNT_IN_SUMMARY", "CONDITIONAL"}
    assert len(SURFACES) == len({name for name, _ in SURFACES})


def test_caps_are_named_constants_not_inline_slices():
    """An inline [:N] is how every defect in this round was written."""
    assert cli._DAEMON_STATUS_PROJECT_CAP == 20
    assert cli._VALIDATE_ERROR_CAP == 10
    assert cli._WATCH_IMPACTED_CAP == 20
    assert cli._WATCH_TESTS_CAP == 30
    assert context._CONTEXT_LIST_CAP == 30
    assert daemon_health._MAX_ISSUE_LINES == 20


@pytest.mark.parametrize(
    "cap_value,total",
    [(20, 26), (10, 15), (30, 41)],
)
def test_listing_lines_marker_shape_is_uniform(cap_value, total):
    """Whatever the surface, the marker reads the same and reconciles."""
    from graphite.listing import listing_lines

    lines = listing_lines([f"v{i}" for i in range(total)], cap=cap_value)
    marker = lines[-1]

    assert MARKER.search(marker)
    shown = sum(1 for line in lines if line.lstrip().startswith("- "))
    dropped = int(re.search(r"(\d+) more", marker).group(1))
    assert shown + dropped == total


def test_answer_surfaces_emit_their_header_when_empty():
    """ANSWER-kind surfaces answer every half of the question they were asked."""
    from graphite.listing import listing_lines

    assert listing_lines([], header="Likely tests:", empty="none found") == [
        "Likely tests:",
        "  - none found",
    ]


def test_non_answer_surfaces_emit_nothing_when_empty():
    from graphite.listing import listing_lines

    assert listing_lines([], header="[graphite] likely tests:", empty=None) == []
```

- [ ] **Step 2: Run the table test**

Run: `python -m pytest tests/test_listing_surfaces.py -v > /tmp/t.txt 2>&1; echo $?`
Expected: `0`.

- [ ] **Step 3: Lint everything touched**

Run: `python -m ruff check src/graphite tests`
Expected: no findings. Fix any that appear.

- [ ] **Step 4: Run the full suite, reading the exit code directly**

Run:

```bash
python -m pytest > /tmp/suite.txt 2>&1
echo "rc=$?"
tail -5 /tmp/suite.txt
```

Expected: `rc=0`. Baseline before this round was 2242 passed / 44 skipped / 0 failed; the new tests raise the pass count. **Do not pipe pytest into `tail`** — `$?` would report `tail`'s status and a failing run would look green.

- [ ] **Step 5: Truth up the spec against what was built**

Two refinements were made during implementation planning and must be folded back into the design doc, matching this repo's precedent of a `docs(spec): truth-up` commit:

In §3, after the emitted-order list, add:

```markdown
`empty=None` omits the listing entirely when there is nothing to show —
header included. This is what makes R1 enforceable by construction: a caller
that wants silence on empty cannot accidentally leave a header behind.
Count-in-summary and conditional-report surfaces (§4) all pass it.
```

In §4, change the Answer row's Header cell from `unconditional` to
`unconditional within the listing branch`, and add below the table:

```markdown
"Unconditional" is scoped to the branch that lists results. When *every*
list in the answer is empty, `impact` and `context` both print one sentence
whose `empty_meaning` is already "no impacted files or tests reachable
through bound edges" — that answers both halves, and adding per-half blocks
there would make the two renderers diverge for no gain.
```

In §7 row 8, note that `daemon_health._issue_lines` implements the marker
locally rather than through `listing_lines`, because its body is grouped by
issue code rather than being a flat list; the surface table test covers it.

- [ ] **Step 6: Commit**

```bash
git add tests/test_listing_surfaces.py docs/superpowers/specs/2026-07-26-listing-marker-contract-design.md
git commit -m "test(listing): surface table; truth up spec against implementation"
```

---

## Acceptance (post-merge, live)

Run these against real repos, writing the falsifier down before each run. Spec §10.

- [ ] **A1.** `python -m graphite impact src/router.ts` in `F:\Projects\FireScraper` prints `Likely tests:` followed by a marked empty body carrying the `INCONCLUSIVE` suffix, and `answer health:` at column 0. *Falsifier:* a bare header, an indented `answer health:`, or a plain `none found` on this degraded answer.
- [ ] **A2.** `python -m graphite daemon-status` with more than 20 projects prints `... N more — use --json for the full list`, and N + rows shown equals the project count in the summary line. *Falsifier:* 20 rows and no marker, or an N that does not reconcile.
- [ ] **A3.** `python -m graphite daemon-status --json` still lists every project. *Falsifier:* the JSON gains a cap.
- [ ] **A4.** `python -m graphite impact <file>` on a healthy Python repo with an empty tests half prints `- none found` with **no** `INCONCLUSIVE` suffix. *Falsifier:* the suffix appearing on a decision-grade answer.
- [ ] **A5.** Full suite green, exit code read directly from a redirected run.

## Follow-ups this round does not take

Recorded in spec §12; do not fold them in.

- **Issue #11** — context neighbour cap overrides `--neighbor-limit`, and
  `_neighbors` discards the total (affects `context --json`). Found while
  planning this round; needs a round that can change the result payload.
- Sub-answer grading in `answer_contract` (spec D7, §5).
- `context.py:185-186` aggregate → scoped health gate (spec D8, §7.1).
- `export/md.py` markers — five bare headers, four unmarked caps (spec §7.2).
