# Secure Change Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent repository-controlled script execution in Graphite's HTML viewer and add a deterministic, zero-LLM `review-changes` acceptance packet.

**Architecture:** Harden HTML serialization, DOM rendering, and publication at the exporter boundary. Add a focused `graphite.review` module for typed change collection, bounded Git discovery, graph-derived evidence, transparent risks, and acceptance criteria; keep CLI wiring and formatting separate from model-provider code.

**Tech Stack:** Python 3.11+, Python standard library, existing NetworkX graph utilities, pytest, Ruff.

---

## File Map

- Create `src/graphite/review.py`: review-domain logic and Markdown rendering.
- Create `tests/test_html_security.py`: hostile viewer inputs and atomic-write regression tests.
- Create `tests/test_review.py`: change discovery, review evidence, determinism, and CLI tests.
- Modify `src/graphite/export/html.py`: context-safe output and atomic publication.
- Modify `src/graphite/cli.py`: `review-changes` command; remove the existing unused `start_daemon_task` import in this touched file.
- Modify `README.md`: usage, exit semantics, and trust boundaries.
- Create `docs/audits/2026-07-11-security-reliability-audit.md`: findings and acceptance recommendation.

### Task 1: Harden the HTML viewer

**Files:**
- Create: `tests/test_html_security.py`
- Modify: `src/graphite/export/html.py`

- [ ] **Step 1: Write the failing security tests**

Create `tests/test_html_security.py`:

```python
from pathlib import Path

from graphite.export.html import to_html


def _render(tmp_path: Path, label: str, title: str = "repo") -> str:
    output = tmp_path / "graph.html"
    to_html(
        {"nodes": [{"id": "n", "name": label, "kind": "file"}], "edges": [], "metadata": {"node_count": 1, "edge_count": 0}},
        {"count": 1, "clusters": [{"id": 0, "members": ["n"], "labels": [label]}]},
        {},
        {"root": title},
        output,
    )
    return output.read_text(encoding="utf-8")


def test_hostile_graph_values_remain_inert(tmp_path: Path) -> None:
    payload = "</script><script>globalThis.pwned=true</script>"
    document = _render(tmp_path, payload, "<unsafe & repo>")
    assert payload not in document
    assert "\\u003c/script\\u003e" in document
    assert "<title>Graphite — &lt;unsafe &amp; repo&gt;</title>" in document
    assert ".innerHTML" not in document
    assert ".textContent" in document


def test_html_export_uses_atomic_writer(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr("graphite.export.html.atomic_write_text", lambda path, text: calls.append((path, text)))
    output = tmp_path / "graph.html"
    to_html({"nodes": [], "edges": [], "metadata": {}}, {"count": 0, "clusters": []}, {}, {"root": "repo"}, output)
    assert len(calls) == 1
    assert calls[0][0] == output
    assert calls[0][1].startswith("<!DOCTYPE html>")
```

- [ ] **Step 2: Confirm RED**

Run `python -m pytest -q tests/test_html_security.py --basetemp F:\tmp\graphite-html-red`.

Expected: both tests fail because dangerous script text and `innerHTML` remain and the atomic writer is not called.

- [ ] **Step 3: Implement the minimal hardening**

In `src/graphite/export/html.py`, import `escape` from `html`. Add:

```python
def _json_for_script(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
```

Replace the dynamic `innerHTML` branch in `updateSelection` with:

```javascript
function addSelectionLine(el, label, value) {
  const line = document.createElement('span');
  const strong = document.createElement('strong');
  strong.textContent = label;
  line.append(strong, document.createTextNode(value));
  el.append(line, document.createElement('br'));
}

function updateSelection() {
  const el = document.getElementById('selection');
  el.replaceChildren();
  if (!hovered) { el.textContent = 'Hover or click a node.'; return; }
  const incoming = edges.filter(e => e.target === hovered.id).length;
  const outgoing = edges.filter(e => e.source === hovered.id).length;
  const cluster = clusters[hovered.cluster];
  addSelectionLine(el, '', `${hovered.label} (${hovered.kind})`);
  addSelectionLine(el, 'cluster: ', cluster ? cluster.labels.join(', ') : 'none');
  addSelectionLine(el, 'links: ', `in: ${incoming} / out: ${outgoing}`);
}
```

In `to_html`, use `_json_for_script(bundle)`, `escape(str(manifest.get("root", "codebase")))`, and `atomic_write_text(output_path, html)` instead of direct `open`.

- [ ] **Step 4: Confirm GREEN and commit**

Run:

```powershell
python -m pytest -q tests/test_html_security.py --basetemp F:\tmp\graphite-html-green
python -m ruff check src/graphite/export/html.py tests/test_html_security.py
git add src/graphite/export/html.py tests/test_html_security.py
git commit -m "fix: harden generated HTML viewer"
```

Expected: 2 passed; Ruff clean; one focused commit.

### Task 2: Collect changes safely

**Files:**
- Create: `src/graphite/review.py`
- Create: `tests/test_review.py`

- [ ] **Step 1: Write failing change-collection tests**

Create tests covering these exact contracts:

```python
def test_explicit_changes_are_contained_unique_and_sorted(tmp_path):
    assert [c.to_dict() for c in normalize_explicit_changes(tmp_path, ["b.py", "a.py", "b.py"])] == [
        {"path": "a.py", "status": "explicit"},
        {"path": "b.py", "status": "explicit"},
    ]
    with pytest.raises(ReviewError, match="outside project root"):
        normalize_explicit_changes(tmp_path, ["../escape.py"])


def test_git_discovery_covers_all_statuses(tmp_path):
    # Initialize and configure a local Git repository, commit baseline files, then create
    # staged, unstaged, untracked, deleted, and `git mv` changes.
    changes = discover_git_changes(tmp_path)
    assert [c.to_dict() for c in changes] == [
        {"path": "after.py", "status": "renamed"},
        {"path": "deleted.py", "status": "deleted"},
        {"path": "staged.py", "status": "modified"},
        {"path": "unstaged.py", "status": "modified"},
        {"path": "untracked.py", "status": "untracked"},
    ]


def test_git_discovery_rejects_non_git_directory(tmp_path):
    with pytest.raises(ReviewError, match="not a Git worktree"):
        discover_git_changes(tmp_path)
```

Use a `_git(root, *args)` helper that calls `subprocess.run(["git", *args], cwd=root, stdin=DEVNULL, capture_output=True, check=True)`; no shell.

- [ ] **Step 2: Confirm RED**

Run `python -m pytest -q tests/test_review.py --basetemp F:\tmp\graphite-review-collect-red`.

Expected: import failure because `graphite.review` does not exist.

- [ ] **Step 3: Implement typed changes and discovery**

Create `src/graphite/review.py` with:

```python
class ReviewError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class Change:
    path: str
    status: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)
```

`normalize_explicit_changes` must resolve each absolute or root-relative path, enforce `resolved.relative_to(root.resolve())`, reject the root itself, normalize to POSIX form, deduplicate, and sort.

`discover_git_changes` must run exactly:

```python
subprocess.run(
    ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
    cwd=root,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
    timeout=timeout_seconds,
)
```

Parse NUL records without a shell. Map `??` to `untracked`, any `D` to `deleted`, any `A` to `added`, `R`/`C` to `renamed` using the first `-z` path as destination while consuming the second source path, and all remaining tracked states to `modified`. Convert missing Git, timeout, non-worktree, malformed output, and OS errors to bounded `ReviewError` messages.

- [ ] **Step 4: Confirm GREEN and commit**

Run:

```powershell
python -m pytest -q tests/test_review.py --basetemp F:\tmp\graphite-review-collect-green
python -m ruff check src/graphite/review.py tests/test_review.py
git add src/graphite/review.py tests/test_review.py
git commit -m "feat: collect review change evidence"
```

Expected: collection tests pass and Ruff is clean.

### Task 3: Derive graph evidence, risks, and acceptance criteria

**Files:**
- Modify: `src/graphite/review.py`
- Modify: `tests/test_review.py`

- [ ] **Step 1: Write failing evidence tests**

Build a three-node fixture (`src/store.py`, dependent `src/api.py`, and `tests/test_store.py`) and assert:

```python
packet = build_review_packet(
    root_name="sample",
    changes=[Change("src/store.py", "modified")],
    discovery="explicit",
    graph_bundle=bundle,
    graph_status={"stale": False},
    depth=2,
)
assert packet == build_review_packet(
    root_name="sample", changes=[Change("src/store.py", "modified")],
    discovery="explicit", graph_bundle=bundle, graph_status={"stale": False}, depth=2,
)
assert packet["impact"]["impacted_files"] == ["src/api.py"]
assert packet["impact"]["likely_tests"] == ["tests/test_store.py"]
assert packet["risk"]["level"] == "low"
assert {c["id"] for c in packet["acceptance_criteria"]} >= {"REVIEW_SCOPE", "RUN_LIKELY_TESTS"}
```

Also assert stale graph + deleted missing file + `pyproject.toml` yields `GRAPH_STALE`, `DELETED_FILES`, `MISSING_GRAPH_MATCHES`, `SENSITIVE_CONFIG`, high risk, and criteria `REFRESH_GRAPH`, `REVIEW_DELETIONS`, `VERIFY_CONFIG`, `ADD_TEST_PLAN`. Assert `None` graph creates only `MISSING_GRAPH`; an invalid edge target creates only `INVALID_GRAPH`. Assert Markdown includes the same paths and criterion IDs.

- [ ] **Step 2: Confirm RED**

Run `python -m pytest -q tests/test_review.py --basetemp F:\tmp\graphite-review-evidence-red`.

Expected: missing `build_review_packet` and `format_review_markdown`.

- [ ] **Step 3: Implement the packet contract**

Use `validate_graph_bundle`, `graph_from_json`, and `build_context`. Return this stable shape with no timestamp or absolute root:

```python
{
    "schema_version": 1,
    "project": root_name,
    "discovery": discovery,
    "changes": [change.to_dict() for change in sorted(changes)],
    "graph": {"status": graph_status, "validation": validation_or_none},
    "impact": {"matched_nodes": [], "missing": [], "impacted_files": [], "likely_tests": []},
    "risk": {"level": "low|medium|high", "signals": []},
    "acceptance_criteria": [],
    "warnings": [],
    "blockers": [],
}
```

Risk rules are exact: stale/deletion/sensitive config/broad impact of at least 10 files are high; missing graph matches and no likely tests are medium. Sensitive names are `pyproject.toml`, dependency manifests/lockfiles, `Dockerfile`, and `.github/workflows/*`. Highest signal wins; none is low.

Acceptance rules are exact: no changes → `CONFIRM_CLEAN`; otherwise always `REVIEW_SCOPE`; stale → `REFRESH_GRAPH`; any impact → `REVIEW_IMPACT`; likely tests → `RUN_LIKELY_TESTS`, otherwise `ADD_TEST_PLAN`; deletion → `REVIEW_DELETIONS`; sensitive config → `VERIFY_CONFIG`. Every criterion has `id`, `description`, and `verify`.

`format_review_markdown` must render Changes, Impact, Risk Signals, Acceptance Criteria, optional Blockers, and optional Warnings using only text interpolation into Markdown.

- [ ] **Step 4: Confirm GREEN and commit**

Run:

```powershell
python -m pytest -q tests/test_review.py --basetemp F:\tmp\graphite-review-evidence-green
python -m ruff check src/graphite/review.py tests/test_review.py
git add src/graphite/review.py tests/test_review.py
git commit -m "feat: derive review acceptance evidence"
```

Expected: evidence tests pass and Ruff is clean.

### Task 4: Add the CLI command

**Files:**
- Modify: `src/graphite/cli.py`
- Modify: `tests/test_review.py`

- [ ] **Step 1: Write failing CLI tests**

Write a valid `graph-out/graph.json` and matching manifest in a temporary project. Call `main(["review-changes", root, "src/store.py", "--json"])` twice and assert code 0, byte-identical output, schema version 1, no absolute root, and no `llm` key. With no graph, assert normal mode returns 0 with `MISSING_GRAPH`, while `--fail-on-blocker` returns 1.

- [ ] **Step 2: Confirm RED**

Run `python -m pytest -q tests/test_review.py --basetemp F:\tmp\graphite-review-cli-red`.

Expected: argparse rejects `review-changes`.

- [ ] **Step 3: Implement thin CLI wiring**

Import the review functions. Add `cmd_review_changes(args)` that:

```python
root = Path(args.path).resolve()
changes = normalize_explicit_changes(root, args.files) if args.files else discover_git_changes(root, timeout_seconds=args.git_timeout)
cfg = _project_scoped_config(args, root)
graph_path = Path(args.graph_json).resolve() if args.graph_json else cfg.output_dir / "graph.json"
```

Load JSON with bounded, sanitized `FileNotFoundError`, `OSError`, and `JSONDecodeError` handling; call `build_review_packet(root_name=root.name, ..., graph_status=_check_status(root, cfg))`; print `json.dumps(..., ensure_ascii=False, indent=2, sort_keys=True)` or Markdown; return 1 only when `args.fail_on_blocker and packet["blockers"]`.

Register:

```python
p_review = sub.add_parser("review-changes", help="Produce deterministic review evidence and acceptance criteria")
p_review.add_argument("path", nargs="?", default=".")
p_review.add_argument("files", nargs="*")
p_review.add_argument("--graph-json", default=None)
p_review.add_argument("--depth", type=int, default=2)
p_review.add_argument("--git-timeout", type=float, default=5.0)
p_review.add_argument("--fail-on-blocker", action="store_true")
p_review.add_argument("--json", action="store_true")
p_review.set_defaults(func=cmd_review_changes)
```

Remove the existing unused `start_daemon_task` import because `cli.py` is touched and must lint clean.

- [ ] **Step 4: Confirm GREEN and commit**

Run:

```powershell
python -m pytest -q tests/test_review.py --basetemp F:\tmp\graphite-review-cli-green
python -m ruff check src/graphite/cli.py src/graphite/review.py tests/test_review.py
git add src/graphite/cli.py tests/test_review.py
git commit -m "feat: add review-changes command"
```

Expected: all review tests pass and touched files lint clean.

### Task 5: Document findings and usage

**Files:**
- Modify: `README.md`
- Create: `docs/audits/2026-07-11-security-reliability-audit.md`

- [ ] **Step 1: Document the command**

Add examples for Git discovery, explicit files, JSON, and `--fail-on-blocker`. State that output is deterministic, local, zero-LLM, and advisory rather than proof of security or correctness.

- [ ] **Step 2: Write the audit report**

Include executive summary and numbered findings:

- `GRA-SEC-001` High: repository-controlled script execution in generated HTML; fixed with script-context encoding, title escaping, and `textContent`.
- `GRA-REL-001` Medium: direct non-atomic HTML write; fixed with `atomic_write_text`.
- `GRA-OPS-001` Medium: missing repository-level acceptance evidence; fixed with `review-changes`.

For every finding, include post-change file/line evidence and test references. Residual recommendations: LLM endpoint egress policy, response-size limits, separate repository-wide Ruff cleanup, approved dependency scanning, and artifact signing across trust boundaries. Recommend acceptance only after full tests, touched-file Ruff, graph validation, and blocker-free live review.

- [ ] **Step 3: Verify docs and commit**

Run:

```powershell
python -m graphite review-changes --help
rg -n "T[B]D|T[O]DO|PLACEH[O]LDER|:L[I]NE" README.md docs/audits/2026-07-11-security-reliability-audit.md
git add README.md docs/audits/2026-07-11-security-reliability-audit.md
git commit -m "docs: publish secure review guidance"
```

Expected: help works; placeholder scan is empty; documentation commit succeeds.

### Task 6: Verify and produce acceptance evidence

**Files:**
- Modify only when a failure is directly caused by Tasks 1–5.

- [ ] **Step 1: Run focused and full tests**

```powershell
python -m pytest -q tests/test_html_security.py tests/test_review.py --basetemp F:\tmp\graphite-focused-final
python -m pytest -q --basetemp F:\tmp\graphite-full-final
```

Expected: all tests pass, with only documented environment-dependent skips/warnings.

- [ ] **Step 2: Run lint and integrity checks**

```powershell
python -m ruff check src/graphite/export/html.py src/graphite/review.py src/graphite/cli.py tests/test_html_security.py tests/test_review.py
git diff --check HEAD~4..HEAD
git status --short
```

Expected: no lint or whitespace defects; clean worktree.

- [ ] **Step 3: Rebuild, validate, and inspect the live packet**

```powershell
python -m graphite build .
python -m graphite validate
python -m graphite review-changes . --json --fail-on-blocker
```

Expected: valid artifacts; valid JSON; no blockers; no absolute root, LLM field, timestamp, or secret-bearing environment value.

## Self-Review

- Spec coverage: viewer encoding/DOM/atomic writes, explicit and Git scope, containment, timeout/error handling, graph freshness/validation, deterministic JSON, Markdown parity, risk signals, acceptance criteria, audit, and model independence all map to tasks.
- Scope: no dependency, model call, automatic project command execution, broad refactor, or unrelated lint cleanup.
- Type consistency: `Change`, `ReviewError`, `build_review_packet`, `format_review_markdown`, and CLI option names are defined before use and consistent.
- Placeholder scan: no implementation placeholder is permitted in the completed repository.
