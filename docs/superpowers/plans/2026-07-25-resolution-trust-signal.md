# Resolution Trust Signal + Honest-Empty Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deterministic resolution-health signal computed from the canonical graph, persisted in build artifacts, surfaced on stats/impact/context/relation-verbs/check, gating strict-mode denials, and documented in the managed template (DOC_VERSION 6).

**Architecture:** One pure function `resolution_health(g)` in a new `src/graphite/health.py` is the single source of the metric. Build persists it via `analyze()` (it then flows into `.graphite_analysis.json` and the `graph.json` bundle automatically); query surfaces compute it live from the loaded graph; `check` and the strict hook read the persisted copy fail-open via `persisted_resolution(root)`.

**Tech Stack:** Python 3.11+, networkx, pytest. No new dependencies. No LLM/inference anywhere (Canonical Graph Isolation).

**Spec:** `docs/superpowers/specs/2026-07-25-resolution-trust-signal-design.md` (operator-approved). Baseline: suite 2101 passed / 44 skipped at branch point `62f5235`.

## Global Constraints

- `RESOLUTION_HEALTHY_RATIO: Final = 0.8` — defined ONCE in `src/graphite/health.py`; every consumer imports it. `>= 0.8` is healthy (0.8 exactly is healthy).
- Relation universe for the metric is exactly `("calls", "imports")`. All other relations are ignored entirely.
- "Bound" = the edge's target node has `kind != "unknown"` (missing `kind` counts as `"unknown"`, matching `_verb_stats`).
- Ratios and shares are `round(x, 3)`; a zero denominator yields `None` (JSON `null`), and null ratios are excluded from the health verdict; if both relations have zero edges, `healthy` is `true`.
- Language attribution = extension of the edge's `source_file` attribute via the fixed table in Task 1; missing/unknown extension → `"other"`.
- All consumers of the PERSISTED block (`check`, strict hook) are fail-open: missing file, malformed JSON, absent key → `null` / suspend-deny. Never an exception to the user.
- `_not_found` query results are NEVER marked inconclusive.
- Tests run in the worktree via: `cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest <target> -q` (bash). NEVER `pip install -e` from the worktree.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Ruff must stay clean: `PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m ruff check src tests` from the worktree.

---

### Task 1: `health.py` — the metric + persisted reader + unit tests

**Files:**
- Create: `src/graphite/health.py`
- Test: `tests/test_health.py` (new file)

**Interfaces:**
- Produces: `resolution_health(g: nx.DiGraph) -> dict[str, Any]`, `persisted_resolution(root: Path) -> dict[str, Any] | None`, `ratio_percent(block: dict[str, Any], relation: str) -> str`, constant `RESOLUTION_HEALTHY_RATIO: Final = 0.8`. Every later task imports from `graphite.health`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_health.py`:

```python
"""Unit tests for the resolution-health trust signal."""
from __future__ import annotations

import json

import networkx as nx

from graphite.health import (
    RESOLUTION_HEALTHY_RATIO,
    persisted_resolution,
    ratio_percent,
    resolution_health,
)


def _graph(nodes, edges):
    g = nx.DiGraph()
    for node_id, kind, source_file in nodes:
        g.add_node(node_id, kind=kind, source_file=source_file)
    for src, dst, relation, source_file in edges:
        g.add_edge(src, dst, relation=relation, source_file=source_file)
    return g


def test_empty_graph_is_healthy_with_null_ratios():
    block = resolution_health(nx.DiGraph())
    assert block["schema"] == 1
    assert block["healthy"] is True
    assert block["threshold"] == RESOLUTION_HEALTHY_RATIO
    assert block["placeholder_nodes"] == {"total": 0, "unknown": 0, "share": None}
    assert block["by_relation"]["calls"] == {"total": 0, "bound": 0, "ratio": None}
    assert block["by_relation"]["imports"] == {"total": 0, "bound": 0, "ratio": None}
    assert block["by_language"] == {}


def test_bound_and_unbound_edges_counted_per_relation():
    g = _graph(
        nodes=[
            ("f1", "function", "a.py"),
            ("f2", "function", "b.py"),
            ("ghost", "unknown", None),
        ],
        edges=[
            ("f1", "f2", "calls", "a.py"),
            ("f1", "ghost", "calls", "a.py"),
            ("f1", "ghost", "imports", "a.py"),
        ],
    )
    block = resolution_health(g)
    assert block["by_relation"]["calls"] == {"total": 2, "bound": 1, "ratio": 0.5}
    assert block["by_relation"]["imports"] == {"total": 1, "bound": 0, "ratio": 0.0}
    assert block["healthy"] is False
    assert block["placeholder_nodes"] == {"total": 3, "unknown": 1, "share": 0.333}


def test_structural_relations_ignored():
    g = _graph(
        nodes=[("file", "file", "a.py"), ("f1", "function", "a.py")],
        edges=[("file", "f1", "contains", "a.py")],
    )
    block = resolution_health(g)
    assert block["by_relation"]["calls"]["total"] == 0
    assert block["by_relation"]["imports"]["total"] == 0
    assert block["healthy"] is True  # vacuously: nothing to distrust


def test_threshold_boundary_exact_is_healthy():
    nodes = [("t", "function", "a.py"), ("ghost", "unknown", None)]
    edges = [(f"s{i}", "t", "calls", "a.py") for i in range(8)]
    edges += [(f"s{i}", "ghost", "calls", "a.py") for i in range(8, 10)]
    for i in range(10):
        nodes.append((f"s{i}", "function", "a.py"))
    block = resolution_health(_graph(nodes, edges))
    assert block["by_relation"]["calls"]["ratio"] == 0.8
    assert block["healthy"] is True


def test_language_attribution_from_edge_source_file():
    g = _graph(
        nodes=[("a", "function", "x.py"), ("b", "function", "y.ts"), ("ghost", "unknown", None)],
        edges=[
            ("a", "b", "calls", "src/x.py"),
            ("b", "ghost", "calls", "src/app.ts"),
            ("a", "ghost", "imports", None),  # missing source_file -> other
        ],
    )
    block = resolution_health(g)
    assert block["by_language"]["python"]["calls"] == {"total": 1, "bound": 1, "ratio": 1.0}
    assert block["by_language"]["typescript"]["calls"] == {"total": 1, "bound": 0, "ratio": 0.0}
    assert block["by_language"]["other"]["imports"] == {"total": 1, "bound": 0, "ratio": 0.0}
    # languages appear only when they carry at least one counted edge
    assert "go" not in block["by_language"]


def test_missing_kind_counts_as_unknown():
    g = nx.DiGraph()
    g.add_node("a", kind="function")
    g.add_node("mystery")  # no kind attribute
    g.add_edge("a", "mystery", relation="calls", source_file="a.py")
    block = resolution_health(g)
    assert block["by_relation"]["calls"] == {"total": 1, "bound": 0, "ratio": 0.0}
    assert block["placeholder_nodes"]["unknown"] == 1


def test_ratio_percent_formats_and_handles_null():
    block = resolution_health(nx.DiGraph())
    assert ratio_percent(block, "calls") == "n/a"
    g = _graph(
        nodes=[("a", "function", "a.py"), ("ghost", "unknown", None)],
        edges=[("a", "ghost", "imports", "a.py")],
    )
    assert ratio_percent(resolution_health(g), "imports") == "0.0%"
    assert ratio_percent({}, "calls") == "n/a"


def test_persisted_resolution_reads_block(tmp_path):
    out = tmp_path / "graph-out"
    out.mkdir()
    (out / ".graphite_analysis.json").write_text(
        json.dumps({"resolution": {"schema": 1, "healthy": False}}), encoding="utf-8"
    )
    block = persisted_resolution(tmp_path)
    assert block == {"schema": 1, "healthy": False}


def test_persisted_resolution_fails_open(tmp_path):
    assert persisted_resolution(tmp_path) is None  # no graph-out at all
    out = tmp_path / "graph-out"
    out.mkdir()
    assert persisted_resolution(tmp_path) is None  # no analysis file
    (out / ".graphite_analysis.json").write_text("{not json", encoding="utf-8")
    assert persisted_resolution(tmp_path) is None  # malformed
    (out / ".graphite_analysis.json").write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert persisted_resolution(tmp_path) is None  # key absent
    (out / ".graphite_analysis.json").write_text(json.dumps({"resolution": "nope"}), encoding="utf-8")
    assert persisted_resolution(tmp_path) is None  # wrong type
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest tests/test_health.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'graphite.health'`

- [ ] **Step 3: Write the implementation**

Create `src/graphite/health.py`:

```python
"""Resolution-health ("trust") signal computed from the canonical graph.

Pure arithmetic over the loaded graph — no inference, no I/O in
resolution_health itself. persisted_resolution is the fail-open reader for
consumers that must not pay a full graph load (check, strict hook).
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any, Final

import networkx as nx

RESOLUTION_HEALTHY_RATIO: Final = 0.8

_COUNTED_RELATIONS: Final = ("calls", "imports")

_EXTENSION_LANGUAGES: Final = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
}

_MAX_ANALYSIS_BYTES: Final = 64 * 1024 * 1024


def _edge_language(source_file: object) -> str:
    if not isinstance(source_file, str) or not source_file:
        return "other"
    suffix = PurePosixPath(source_file).suffix.lower()
    return _EXTENSION_LANGUAGES.get(suffix, "other")


def _cell(bound: int, total: int) -> dict[str, Any]:
    return {
        "total": total,
        "bound": bound,
        "ratio": None if total == 0 else round(bound / total, 3),
    }


def resolution_health(g: nx.DiGraph) -> dict[str, Any]:
    """Measured resolver health: bound-edge ratios per relation and language."""
    node_total = g.number_of_nodes()
    unknown_nodes = sum(
        1 for _n, data in g.nodes(data=True) if data.get("kind", "unknown") == "unknown"
    )
    relation_counts = {rel: [0, 0] for rel in _COUNTED_RELATIONS}  # [bound, total]
    language_counts: dict[str, dict[str, list[int]]] = {}
    for _u, v, data in g.edges(data=True):
        relation = data.get("relation")
        counts = relation_counts.get(relation)
        if counts is None:
            continue
        bound = int(g.nodes[v].get("kind", "unknown") != "unknown")
        counts[0] += bound
        counts[1] += 1
        language = _edge_language(data.get("source_file"))
        buckets = language_counts.setdefault(
            language, {rel: [0, 0] for rel in _COUNTED_RELATIONS}
        )
        buckets[relation][0] += bound
        buckets[relation][1] += 1
    by_relation = {rel: _cell(c[0], c[1]) for rel, c in relation_counts.items()}
    by_language = {
        language: {rel: _cell(c[0], c[1]) for rel, c in buckets.items()}
        for language, buckets in sorted(language_counts.items())
    }
    ratios = [cell["ratio"] for cell in by_relation.values() if cell["ratio"] is not None]
    return {
        "schema": 1,
        "placeholder_nodes": {
            "total": node_total,
            "unknown": unknown_nodes,
            "share": None if node_total == 0 else round(unknown_nodes / node_total, 3),
        },
        "by_relation": by_relation,
        "by_language": by_language,
        "healthy": all(ratio >= RESOLUTION_HEALTHY_RATIO for ratio in ratios),
        "threshold": RESOLUTION_HEALTHY_RATIO,
    }


def ratio_percent(block: dict[str, Any], relation: str) -> str:
    """Human rendering of one relation's bound ratio: '4.6%' or 'n/a'."""
    try:
        ratio = block["by_relation"][relation]["ratio"]
    except (KeyError, TypeError):
        return "n/a"
    if not isinstance(ratio, (int, float)):
        return "n/a"
    return f"{ratio * 100:.1f}%"


def persisted_resolution(root: Path) -> dict[str, Any] | None:
    """Fail-open read of the persisted block from graph-out/.graphite_analysis.json."""
    path = root / "graph-out" / ".graphite_analysis.json"
    try:
        if not path.is_file() or path.stat().st_size > _MAX_ANALYSIS_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    block = data.get("resolution")
    return block if isinstance(block, dict) else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest tests/test_health.py -q`
Expected: all PASS

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m ruff check src/graphite/health.py tests/test_health.py
git add src/graphite/health.py tests/test_health.py
git commit -m "feat(health): resolution-health trust signal + fail-open persisted reader

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Build persistence via `analyze()`

**Files:**
- Modify: `src/graphite/analyze.py:20-30` (the `analyze` function)
- Test: `tests/test_health.py` (append)

**Interfaces:**
- Consumes: `resolution_health` from Task 1.
- Produces: `analyze(g)` result gains top-level key `"resolution"`. Because `cli._report` writes analysis into `.graphite_analysis.json` and `export/json.py:build_bundle` embeds `analysis` wholesale into `graph.json`, no other code changes are needed for persistence.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_health.py`:

```python
def test_analyze_includes_resolution_block():
    from graphite.analyze import analyze

    g = _graph(
        nodes=[("f1", "function", "a.py"), ("ghost", "unknown", None)],
        edges=[("f1", "ghost", "calls", "a.py")],
    )
    result = analyze(g)
    assert result["resolution"]["by_relation"]["calls"] == {
        "total": 1, "bound": 0, "ratio": 0.0,
    }
    assert result["resolution"]["healthy"] is False


def test_build_persists_resolution_in_artifacts(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text(
        "from b import helper\n\ndef f():\n    helper()\n", encoding="utf-8"
    )
    (tmp_path / "b.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    from graphite.cli import _build_project
    from graphite.config import Config

    _build_project(tmp_path, Config())
    analysis = json.loads(
        (tmp_path / "graph-out" / ".graphite_analysis.json").read_text(encoding="utf-8")
    )
    assert analysis["resolution"]["schema"] == 1
    bundle = json.loads(
        (tmp_path / "graph-out" / "graph.json").read_text(encoding="utf-8")
    )
    assert bundle["analysis"]["resolution"]["schema"] == 1
    assert persisted_resolution(tmp_path) == analysis["resolution"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest tests/test_health.py -q`
Expected: the two new tests FAIL with `KeyError: 'resolution'`

- [ ] **Step 3: Implement** — in `src/graphite/analyze.py`, add the import and one key:

```python
from .health import resolution_health
```

and in `analyze()` change the return to:

```python
    return {
        "god_nodes": god_nodes(g, top_n),
        "orphans": orphan_nodes(g, top_n),
        "entry_points": entry_points(g, top_n),
        "surprising_connections": surprising_connections(g, top_n),
        "cycles": list(nx.simple_cycles(project_subgraph))[:top_n],
        "top_files_by_links": top_files_by_links(g, top_n),
        "resolution": resolution_health(g),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest tests/test_health.py tests/test_smoke.py tests/test_graph_io.py -q`
Expected: all PASS (smoke/graph_io guard the build path)

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m ruff check src/graphite/analyze.py tests/test_health.py
git add src/graphite/analyze.py tests/test_health.py
git commit -m "feat(build): persist resolution health in analysis + graph.json bundle

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Query surfaces — `stats` block + `inconclusive` on relation verbs

**Files:**
- Modify: `src/graphite/query.py` — `_capped_edge_listing` (lines ~20-45), `_neighbor_listing` (lines ~112-130), `_verb_stats` (lines ~85-109)
- Test: `tests/test_health.py` (append)

**Interfaces:**
- Consumes: `resolution_health` from Task 1.
- Produces: `_verb_stats` result gains `"resolution"`; `_capped_edge_listing` and `_neighbor_listing` results gain `"resolution"` and `"inconclusive"` (covers verbs `callers`, `calls`, `depends-on`, `imported-by`). New module-private helper `_attach_resolution(result, g) -> dict` in query.py. `_not_found` results unchanged.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_health.py`:

```python
def _unhealthy_graph():
    # lonely + one unbound call edge elsewhere -> calls ratio 0.0 -> unhealthy
    return _graph(
        nodes=[("lonely", "function", "a.py"), ("src", "function", "a.py"), ("ghost", "unknown", None)],
        edges=[("src", "ghost", "calls", "a.py")],
    )


def _healthy_graph():
    return _graph(
        nodes=[("lonely", "function", "a.py"), ("src", "function", "a.py"), ("dst", "function", "b.py")],
        edges=[("src", "dst", "calls", "a.py")],
    )


def test_stats_includes_resolution():
    from graphite.query import query

    result = query(_unhealthy_graph(), "stats")
    assert result["resolution"]["healthy"] is False


def test_callers_empty_on_unhealthy_graph_is_inconclusive():
    from graphite.query import query

    result = query(_unhealthy_graph(), "callers lonely")
    assert result["total"] == 0
    assert result["inconclusive"] is True
    assert result["resolution"]["healthy"] is False


def test_callers_empty_on_healthy_graph_is_conclusive():
    from graphite.query import query

    result = query(_healthy_graph(), "callers lonely")
    assert result["total"] == 0
    assert result["inconclusive"] is False


def test_callers_nonempty_not_inconclusive_even_when_unhealthy():
    from graphite.query import query

    result = query(_unhealthy_graph(), "imported-by ghost")
    assert result["total"] >= 1
    assert result["inconclusive"] is False


def test_not_found_result_has_no_inconclusive_field():
    from graphite.query import query

    result = query(_unhealthy_graph(), "callers does_not_exist_anywhere")
    assert result["error_code"] == "node_not_found"
    assert "inconclusive" not in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest tests/test_health.py -q`
Expected: new tests FAIL (`KeyError: 'resolution'` / `KeyError: 'inconclusive'`)

- [ ] **Step 3: Implement** — in `src/graphite/query.py`:

Add import near the top:

```python
from .health import resolution_health
```

Add the helper directly above `_capped_edge_listing`:

```python
def _attach_resolution(result: dict[str, Any], g: nx.DiGraph) -> dict[str, Any]:
    """Trust signal + honest-empty marker for relation listings (spec 2026-07-25)."""
    health = resolution_health(g)
    result["resolution"] = health
    result["inconclusive"] = result.get("total", 0) == 0 and not health["healthy"]
    return result
```

In `_capped_edge_listing`, wrap the successful return (NOT the `_not_found` return):

```python
    return _attach_resolution({
        "node": node_id,
        "match": _match_meta(token, detail),
        "count": len(shown),
        "total": len(full),
        "truncated": len(full) > cap,
        "limits": {"max_results": cap},
        key: [_node_view(g, n) for n in shown],
    }, g)
```

In `_neighbor_listing`, wrap its successful return dict the same way (same pattern: `return _attach_resolution({...existing dict...}, g)`; leave its `_not_found` early return untouched).

In `_verb_stats`, add to the returned dict:

```python
        "resolution": resolution_health(g),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest tests/test_health.py tests/test_call_graph.py tests/test_search.py tests/test_natural_query.py tests/test_query_plan.py -q`
Expected: all PASS (the neighbor-verb suites guard existing result shapes; additive keys must not break them)

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m ruff check src/graphite/query.py tests/test_health.py
git add src/graphite/query.py tests/test_health.py
git commit -m "feat(query): resolution block on stats + inconclusive marker on relation verbs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `impact` honest-empty + `check --json` passthrough

**Files:**
- Modify: `src/graphite/cli.py` — `_impact` (lines ~295-337), `cmd_impact` (lines ~896-915), `cmd_check` (lines ~394-411)
- Test: `tests/test_health.py` (append)

**Interfaces:**
- Consumes: `resolution_health`, `ratio_percent`, `persisted_resolution` from Task 1.
- Produces: `_impact` result gains `"resolution"` + `"inconclusive"`; `cmd_impact` human output per spec §6.2; `cmd_check --json` output gains `"resolution"` (block or `null`).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_health.py`:

```python
def test_impact_json_inconclusive_on_unhealthy_graph():
    from graphite.cli import _impact

    g = _unhealthy_graph()
    result = _impact(g, ["lonely"], depth=2)
    assert result["impacted_files"] == [] and result["likely_tests"] == []
    assert result["inconclusive"] is True
    assert result["resolution"]["healthy"] is False


def test_impact_json_conclusive_on_healthy_graph():
    from graphite.cli import _impact

    result = _impact(_healthy_graph(), ["lonely"], depth=2)
    assert result["inconclusive"] is False


def test_cmd_impact_human_inconclusive_line(capsys, monkeypatch):
    import argparse

    from graphite import cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: _unhealthy_graph())
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    args = argparse.Namespace(graph_json="graph-out/graph.json", files=["lonely"], depth=2, json=False)
    cli.cmd_impact(args)
    out = capsys.readouterr().out
    assert "INCONCLUSIVE" in out
    assert "confirm with grep" in out
    assert "Impacted files:\n" not in out  # empty listings are replaced, not printed


def test_cmd_impact_human_note_when_nonempty_but_unhealthy(capsys, monkeypatch):
    import argparse

    from graphite import cli

    g = _graph(
        nodes=[
            ("caller_file", "file", "caller.py"),
            ("target_file", "file", "target.py"),
            ("ghost", "unknown", None),
        ],
        edges=[
            ("caller_file", "target_file", "imports", "caller.py"),
            ("caller_file", "ghost", "calls", "caller.py"),
        ],
    )
    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: g)
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    args = argparse.Namespace(graph_json="graph-out/graph.json", files=["target_file"], depth=2, json=False)
    cli.cmd_impact(args)
    out = capsys.readouterr().out
    assert "caller.py" in out
    assert "may be incomplete" in out
    assert "INCONCLUSIVE" not in out


def test_cmd_impact_human_unchanged_on_healthy_graph(capsys, monkeypatch):
    import argparse

    from graphite import cli

    monkeypatch.setattr(cli, "_load_graph", lambda *a, **k: _healthy_graph())
    monkeypatch.setattr(cli, "_record_canonical_usage", lambda *a, **k: None)
    args = argparse.Namespace(graph_json="graph-out/graph.json", files=["lonely"], depth=2, json=False)
    cli.cmd_impact(args)
    out = capsys.readouterr().out
    assert "Impacted files:" in out
    assert "INCONCLUSIVE" not in out and "may be incomplete" not in out


def test_cmd_check_json_resolution_passthrough(tmp_path, capsys, monkeypatch):
    import argparse

    from graphite import cli

    monkeypatch.setattr(
        cli, "check_graph_freshness", lambda *a, **k: {"stale": False}
    )
    out_dir = tmp_path / "graph-out"
    out_dir.mkdir()
    (out_dir / ".graphite_analysis.json").write_text(
        json.dumps({"resolution": {"schema": 1, "healthy": True}}), encoding="utf-8"
    )
    args = argparse.Namespace(path=str(tmp_path), json=True, ignore_engine=False)
    cli.cmd_check(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["resolution"] == {"schema": 1, "healthy": True}


def test_cmd_check_json_resolution_null_when_absent(tmp_path, capsys, monkeypatch):
    import argparse

    from graphite import cli

    monkeypatch.setattr(
        cli, "check_graph_freshness", lambda *a, **k: {"stale": False}
    )
    args = argparse.Namespace(path=str(tmp_path), json=True, ignore_engine=False)
    cli.cmd_check(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["resolution"] is None
```

NOTE for the implementer: if `cmd_check`/`cmd_impact` argument namespaces carry more attributes than shown (e.g. config args consumed by `_config_from_args`), add the minimal extra `Namespace` attributes the real functions read — assertions stay as written.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest tests/test_health.py -q`
Expected: new tests FAIL

- [ ] **Step 3: Implement** — in `src/graphite/cli.py`:

Add to the existing graphite imports:

```python
from .health import persisted_resolution, ratio_percent, resolution_health
```

In `_impact`, before the final return, compute the health block and extend the returned dict:

```python
    health = resolution_health(g)
    return {
        "changed": changes,
        "matched_nodes": sorted(start_nodes),
        "missing": missing,
        "depth": depth,
        "impacted_files": sorted(impacted_files),
        "likely_tests": sorted(likely_tests),
        "resolution": health,
        "inconclusive": not impacted_files and not likely_tests and not health["healthy"],
    }
```

In `cmd_impact`, replace the non-JSON branch with:

```python
    else:
        health = result["resolution"]
        if result["inconclusive"]:
            print(
                "Impacted files: none found — INCONCLUSIVE: only "
                f"{ratio_percent(health, 'imports')} of import edges and "
                f"{ratio_percent(health, 'calls')} of call edges resolved in this "
                "graph; treat as unverified and confirm with grep."
            )
        else:
            print("Impacted files:")
            for path in result["impacted_files"]:
                print(f"  - {path}")
            print("Likely tests:")
            for path in result["likely_tests"]:
                print(f"  - {path}")
            if not health["healthy"] and (result["impacted_files"] or result["likely_tests"]):
                print(
                    f"note: resolution health low (imports {ratio_percent(health, 'imports')}, "
                    f"calls {ratio_percent(health, 'calls')}) — this list may be incomplete."
                )
        if result["missing"]:
            print("Missing inputs:")
            for item in result["missing"]:
                print(f"  - {item}")
```

(Keep the existing `missing` rendering exactly as it is today — the block above shows it only to fix its position after the new branch; do not alter its text.)

In `cmd_check`, in the `args.json` branch, add the persisted block before printing:

```python
    if args.json:
        status["resolution"] = persisted_resolution(Path(args.path).resolve())
        print(json.dumps(status, ensure_ascii=False, indent=2))
```

Human (non-json) check output: unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest tests/test_health.py tests/test_smoke.py -q`
Expected: all PASS

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m ruff check src/graphite/cli.py tests/test_health.py
git add src/graphite/cli.py tests/test_health.py
git commit -m "feat(cli): honest-empty impact + resolution passthrough on check --json

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: `context` honest-empty

**Files:**
- Modify: `src/graphite/context.py` — `build_context` (lines ~23-72), `format_context_markdown` (lines ~75-118)
- Test: `tests/test_context.py` (append)

**Interfaces:**
- Consumes: `resolution_health`, `ratio_percent` from Task 1.
- Produces: `build_context` result gains top-level `"resolution"` + `"inconclusive"`; markdown rendering per spec §6.3.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_context.py` (reuse that file's existing graph-fixture helpers if present; otherwise build `nx.DiGraph` inline as below):

```python
def _trust_graph(healthy: bool):
    import networkx as nx

    g = nx.DiGraph()
    g.add_node("lonely", kind="function", source_file="a.py")
    g.add_node("src", kind="function", source_file="a.py")
    target_kind = "function" if healthy else "unknown"
    g.add_node("tgt", kind=target_kind, source_file="b.py")
    g.add_edge("src", "tgt", relation="calls", source_file="a.py")
    return g


def test_context_marks_inconclusive_on_unhealthy_graph():
    from graphite.context import build_context, format_context_markdown

    context = build_context(_trust_graph(healthy=False), ["lonely"])
    assert context["resolution"]["healthy"] is False
    assert context["inconclusive"] is True
    text = format_context_markdown(context)
    assert "INCONCLUSIVE" in text
    assert "no direct dependents found — inconclusive (resolution health low)" in text
    assert "Impacted files: none found\n" not in text


def test_context_unchanged_on_healthy_graph():
    from graphite.context import build_context, format_context_markdown

    context = build_context(_trust_graph(healthy=True), ["lonely"])
    assert context["inconclusive"] is False
    text = format_context_markdown(context)
    assert "INCONCLUSIVE" not in text
    assert "Impacted files: none found" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest tests/test_context.py -q`
Expected: new tests FAIL (`KeyError: 'resolution'`)

- [ ] **Step 3: Implement** — in `src/graphite/context.py`:

Add import:

```python
from .health import ratio_percent, resolution_health
```

In `build_context`, after `impact = _reverse_impact(...)` and before the return, compute:

```python
    health = resolution_health(g)
    inconclusive = (
        not impact["impacted_files"]
        and not impact["likely_tests"]
        and not health["healthy"]
    )
```

and add to the returned dict:

```python
        "resolution": health,
        "inconclusive": inconclusive,
```

In `format_context_markdown`, replace the Impact section body with:

```python
    lines.extend(["", "## Impact"])
    impact = context["impact"]
    health = context.get("resolution") or {}
    unhealthy = health.get("healthy") is False
    if impact["impacted_files"]:
        lines.append("Impacted files:")
        lines.extend(f"- `{path}`" for path in impact["impacted_files"][:30])
    elif context.get("inconclusive"):
        lines.append(
            "Impacted files: none found — INCONCLUSIVE: only "
            f"{ratio_percent(health, 'imports')} of import edges and "
            f"{ratio_percent(health, 'calls')} of call edges resolved in this "
            "graph; treat as unverified and confirm with grep."
        )
    else:
        lines.append("Impacted files: none found")
    if impact["likely_tests"]:
        lines.append("Likely tests:")
        lines.extend(f"- `{path}`" for path in impact["likely_tests"][:30])
```

and replace the Direct Dependents section with:

```python
    lines.extend(["", "## Direct Dependents"])
    dependents = context["direct_dependents"]
    if unhealthy and all(not neighbors for neighbors in dependents.values()):
        lines.append("no direct dependents found — inconclusive (resolution health low)")
    else:
        _append_neighbor_section(lines, dependents)
```

(Direct Dependencies and all other sections: unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest tests/test_context.py tests/test_health.py -q`
Expected: all PASS

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m ruff check src/graphite/context.py tests/test_context.py
git add src/graphite/context.py tests/test_context.py
git commit -m "feat(context): honest-empty impact + dependents on unhealthy graphs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Strict-hook health gate

**Files:**
- Modify: `src/graphite/agent_hooks.py` — `handle_pre_tool_use` (lines ~257-281)
- Test: `tests/test_agent_hooks.py` (append)

**Interfaces:**
- Consumes: `persisted_resolution` from Task 1.
- Produces: strict denial fires ONLY when the persisted block exists and has `healthy: true`; otherwise the hook returns the remind-style `additionalContext` with the suspension sentence appended. Constant `STRICT_SUSPENSION_NOTE` exported for the tests.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_agent_hooks.py`, following that file's existing payload/fixture conventions for `handle_pre_tool_use` (it already builds repo dirs with `graph-out/graph.json` and Grep payloads; reuse those helpers). The four cases:

```python
def _write_analysis(root, healthy):
    import json as _json

    (root / "graph-out").mkdir(exist_ok=True)
    (root / "graph-out" / ".graphite_analysis.json").write_text(
        _json.dumps({"resolution": {"schema": 1, "healthy": healthy}}), encoding="utf-8"
    )


def test_strict_denial_preserved_when_graph_healthy(tmp_path):
    # arrange a repo dir + graph.json + Grep payload exactly as the existing
    # strict-denial test in this file does, THEN:
    _write_analysis(root, healthy=True)
    result = handle_pre_tool_use(payload, mode="strict")
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_strict_denial_suspended_when_graph_unhealthy(tmp_path):
    _write_analysis(root, healthy=False)
    result = handle_pre_tool_use(payload, mode="strict")
    output = result["hookSpecificOutput"]
    assert "permissionDecision" not in output
    assert "strict denial suspended" in output["additionalContext"]


def test_strict_denial_suspended_when_analysis_missing(tmp_path):
    # no .graphite_analysis.json written
    result = handle_pre_tool_use(payload, mode="strict")
    output = result["hookSpecificOutput"]
    assert "permissionDecision" not in output
    assert "strict denial suspended" in output["additionalContext"]


def test_strict_denial_suspended_when_analysis_malformed(tmp_path):
    (root / "graph-out" / ".graphite_analysis.json").write_text("{bad", encoding="utf-8")
    result = handle_pre_tool_use(payload, mode="strict")
    assert "strict denial suspended" in result["hookSpecificOutput"]["additionalContext"]
```

The `# arrange` comment lines are instructions, not code: copy the arrangement (root dir, graph.json bundle with a node whose name the Grep pattern hits, payload dict) from the existing strict-mode test in `tests/test_agent_hooks.py` so the denial path is genuinely reached in all four tests. Also assert in the existing remind-mode test file section that remind behavior is unchanged (no new assertions needed if a remind test already exists — just run it).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest tests/test_agent_hooks.py -q`
Expected: `test_strict_denial_suspended_*` FAIL (denial currently fires unconditionally); `test_strict_denial_preserved_when_graph_healthy` PASSES already (guard test)

- [ ] **Step 3: Implement** — in `src/graphite/agent_hooks.py`:

Add import:

```python
from .health import persisted_resolution
```

Add the constant next to `PRE_TOOL_REMINDER`:

```python
STRICT_SUSPENSION_NOTE = (
    " (strict denial suspended: graph resolution health is low or unknown — "
    "grep fallback allowed.)"
)
```

Replace the strict branch in `handle_pre_tool_use`:

```python
        if mode == "strict" and payload.get("tool_name") == "Grep":
            denial = _strict_denial(payload, root)
            if denial is not None:
                health = persisted_resolution(root)
                if isinstance(health, dict) and health.get("healthy") is True:
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": denial,
                        }
                    }
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": PRE_TOOL_REMINDER + STRICT_SUSPENSION_NOTE,
                    }
                }
```

(The trailing default remind return and the outer `except Exception: return None` stay exactly as they are.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest tests/test_agent_hooks.py tests/test_health.py -q`
Expected: all PASS

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m ruff check src/graphite/agent_hooks.py tests/test_agent_hooks.py
git add src/graphite/agent_hooks.py tests/test_agent_hooks.py
git commit -m "feat(hooks): strict denial requires proven resolution health

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Template (DOC_VERSION 6) + agent-integration docs + schema note

**Files:**
- Modify: `src/graphite/init.py` — `DOC_VERSION` (line 17) and the managed GRAPHITE.md template's "Before non-trivial code changes" numbered list (template text around init.py lines 45-51)
- Modify: `docs/agent-integration.md` — new section before `## Non-goals (governance)`
- Modify: `docs/schemas/query-result.v1.schema.json` — document the new optional fields
- Test: `tests/test_init.py` (append), plus run `tests/test_published_schemas.py` and `tests/test_documentation.py` unchanged

**Interfaces:**
- Consumes: nothing from earlier tasks (documentation of their outputs).
- Produces: `DOC_VERSION = 6`; template note text (exact, below); agent-integration section; schema properties `resolution` (object) and `inconclusive` (boolean) marked optional.

- [ ] **Step 1: Write the failing test** — append to `tests/test_init.py`, following that file's existing convention for asserting template content (it has helpers/tests that render the managed template):

```python
def test_doc_version_is_6_and_template_documents_resolution_health():
    from graphite import init as graphite_init

    assert graphite_init.DOC_VERSION == 6
    template = graphite_init.GRAPHITE_DOC
    assert "resolution-health" in template
    assert "INCONCLUSIVE" in template
    assert '"inconclusive": true' in template
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest tests/test_init.py -q`
Expected: new test FAILS (`DOC_VERSION == 5`)

- [ ] **Step 3: Implement**

In `src/graphite/init.py`: set `DOC_VERSION = 6` (line 17). In the managed template's "Before non-trivial code changes" numbered list (after the current item 5 about `capabilities --json`), append item 6 verbatim:

```
6. Graph answers include a resolution-health signal. If a result says INCONCLUSIVE (or JSON has `"inconclusive": true`), the graph could not bind enough edges to answer — treat empty as unknown, not safe, and verify with grep. `python -m graphite query "stats"` shows the ratios.
```

In `docs/agent-integration.md`, insert before `## Non-goals (governance)`:

```markdown
## 7. Resolution health (trust signal)

Every graph carries a measured resolver-health block; consumers must use it
to distinguish "no results" from "the resolver could not bind".

- Shape (in `stats`, `impact`, `context`, relation-verb JSON, `check --json`,
  and persisted in `graph.json` under `analysis.resolution` and in
  `graph-out/.graphite_analysis.json`):

```json
{
  "schema": 1,
  "placeholder_nodes": {"total": 4519, "unknown": 2463, "share": 0.545},
  "by_relation": {
    "calls":   {"total": 6631, "bound": 1730, "ratio": 0.261},
    "imports": {"total": 2047, "bound": 94,   "ratio": 0.046}
  },
  "by_language": {"python": {"calls": {"total": 6631, "bound": 1730, "ratio": 0.261},
                              "imports": {"total": 2047, "bound": 94, "ratio": 0.046}}},
  "healthy": false,
  "threshold": 0.8
}
```

- `healthy` is `true` iff every non-null `by_relation` ratio is `>= threshold`
  (0.8). Zero-edge relations have `ratio: null` and do not count against health.
- `impact`, `context`, and the relation verbs (`callers`, `calls`,
  `imported-by`, `depends-on`) additionally return `"inconclusive": true` when
  the result is EMPTY and the graph is unhealthy. **An inconclusive empty
  answer means "unknown", never "safe"** — fall back to grep and say so.
- ABSENT block (graphs built before 2026-07-25): treat exactly like
  `inconclusive` on empty results — fail open, never assume health.
- `check --json` reports `"resolution": null` when no persisted block exists.
```

In `docs/schemas/query-result.v1.schema.json`, add to the top-level `properties` (alongside the existing optional properties — the schema is `additionalProperties: true`, this documents the fields):

```json
"resolution": {
  "type": "object",
  "description": "Measured resolution-health block (schema 1): placeholder_nodes, by_relation, by_language, healthy, threshold. Present on stats/impact/context/relation-verb results from 2026-07-25."
},
"inconclusive": {
  "type": "boolean",
  "description": "True when the result is empty AND the graph's resolution health is below threshold — treat as unknown, not as 'no dependents'."
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest tests/test_init.py tests/test_published_schemas.py tests/test_documentation.py -q`
Expected: all PASS (the init suite includes `test_template_change_requires_doc_version_bump`, which this bump satisfies)

- [ ] **Step 5: Ruff + commit**

```bash
cd /f/tmp/graphite-resolution-trust-signal && PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m ruff check src/graphite/init.py tests/test_init.py
git add src/graphite/init.py tests/test_init.py docs/agent-integration.md docs/schemas/query-result.v1.schema.json
git commit -m "feat(init): DOC_VERSION 6 — resolution-health guidance in template + docs + schema

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Completion

After all tasks: full suite (`PYTHONPATH='F:/tmp/graphite-resolution-trust-signal/src' python -m pytest -q`, expect ≥ 2101+new passed / 44 skipped / 0 failed) + full `ruff check src tests`, then the final whole-branch review per superpowers:subagent-driven-development, then superpowers:finishing-a-development-branch (operator decides merge). Rollout (post-merge, operator-gated, NOT part of this plan's tasks): re-init consumer repos, run spec §11 acceptance on aramid, notify aramid's agent via the reply-file channel.
