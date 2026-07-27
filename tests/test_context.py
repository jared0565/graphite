"""Tests for Graphite agent context summaries."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.cli import main


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sample_project(root: Path) -> None:
    _write(root / "src" / "lib.ts", "export function add(a: number, b: number): number { return a + b; }\n")
    _write(root / "src" / "app.ts", "import { add } from './lib';\nexport const total = add(1, 2);\n")
    _write(root / "src" / "app.test.ts", "import { total } from './app';\ntest('total', () => expect(total).toBe(3));\n")


def test_context_cli_json_returns_dependencies_dependents_and_impact(tmp_path: Path, monkeypatch, capsys) -> None:
    _sample_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["build", "."]) == 0
    capsys.readouterr()

    result = main(["context", "src/lib.ts", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["missing"] == []
    assert output["matched"][0]["input"] == "src/lib.ts"
    matched_id = output["matched"][0]["node"]["id"]
    dependents = output["direct_dependents"][matched_id]
    assert any(item["source_file"] == "src/app.ts" for item in dependents)
    assert "src/app.ts" in output["impact"]["impacted_files"]
    assert "src/app.test.ts" in output["impact"]["likely_tests"]


def test_context_cli_markdown_is_compact_and_human_readable(tmp_path: Path, monkeypatch, capsys) -> None:
    _sample_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["build", "."]) == 0
    capsys.readouterr()

    result = main(["context", "src/app.ts"])
    output = capsys.readouterr().out

    assert result == 0
    assert "# Graphite Context" in output
    assert "## Impact" in output
    assert "## Direct Dependencies" in output
    assert "src/app.ts" in output


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
    assert context["resolution_health"]["healthy"] is False
    assert context["inconclusive"] is True
    text = format_context_markdown(context)
    assert "INCONCLUSIVE" in text
    assert "no direct dependents found — inconclusive (resolution health low)" in text
    assert "Impacted files: none found\n" not in text
    # The scoped answer block computes a real meaning here, so the legacy
    # aggregate-ratio wording must not print (it would self-contradict the
    # scoped "answer health:" line right below it).
    assert "% of import edges" not in text
    assert "no impacted files or tests reachable through bound edges" in text
    assert "answer health: " in text


def test_context_markdown_inconclusive_line_fail_open_aggregate_wording(monkeypatch):
    """When build_answer_block fails open (returns None), the legacy
    aggregate-ratio INCONCLUSIVE wording is the only signal available and
    must still print — and no answer-health lines should appear."""
    import graphite.context as context_mod

    monkeypatch.setattr(context_mod, "build_answer_block", lambda *a, **k: None)
    context = context_mod.build_context(_trust_graph(healthy=False), ["lonely"])
    assert context.get("answer") is None
    text = context_mod.format_context_markdown(context)
    assert "INCONCLUSIVE" in text
    assert "only" in text and "of import edges and" in text and "of call edges resolved in this" in text
    assert "answer health:" not in text


def test_context_unchanged_on_healthy_graph():
    from graphite.context import build_context, format_context_markdown

    context = build_context(_trust_graph(healthy=True), ["lonely"])
    assert context["inconclusive"] is False
    text = format_context_markdown(context)
    assert "INCONCLUSIVE" not in text
    assert "Impacted files: none found" in text
    # Healthy-empty parity with cmd_impact (cli.py): the answer block's
    # empty_meaning is appended as a tail, not just the bare legacy line.
    meaning = context["answer"]["empty_meaning"]
    assert f"Impacted files: none found — {meaning}" in text


def test_context_markdown_healthy_empty_tail_absent_without_empty_meaning(monkeypatch):
    """When the answer block carries no empty_meaning (or is absent), the
    healthy-empty branch falls back to the bare legacy line — no dangling
    ' — ' separator with nothing after it."""
    import graphite.context as context_mod

    monkeypatch.setattr(context_mod, "build_answer_block", lambda *a, **k: None)
    context = context_mod.build_context(_trust_graph(healthy=True), ["lonely"])
    assert context.get("answer") is None
    text = context_mod.format_context_markdown(context)
    assert "Impacted files: none found\n" in text


def test_context_notes_incomplete_when_nonempty_but_unhealthy():
    import networkx as nx

    from graphite.context import build_context, format_context_markdown

    g = nx.DiGraph()
    g.add_node("caller_file", kind="file", source_file="caller.py")
    g.add_node("target_file", kind="file", source_file="target.py")
    g.add_node("ghost", kind="unknown")
    g.add_edge("caller_file", "target_file", relation="imports", source_file="caller.py")
    g.add_edge("caller_file", "ghost", relation="calls", source_file="caller.py")
    context = build_context(g, ["target_file"])
    text = format_context_markdown(context)
    assert "may be incomplete" in text
    # The impacted-files half is non-empty and must not be marked/replaced;
    # its empty sibling (likely_tests) is allowed to carry the grade-aware
    # INCONCLUSIVE marker now that this round makes it grade-aware (spec §5) --
    # same precedent as cli.py's test_cmd_impact_human_note_when_nonempty_but_unhealthy.
    assert "INCONCLUSIVE" not in text.split("Likely tests:")[0]


def test_context_carries_full_health_block():
    import networkx as nx

    from graphite.context import build_context

    context = build_context(nx.DiGraph(), [])
    block = context["resolution_health"]
    assert set(block) >= {"schema", "placeholder_nodes", "by_relation", "by_language", "healthy", "threshold"}


def test_context_build_carries_answer_block():
    from graphite.context import build_context

    context = build_context(_trust_graph(healthy=True), ["lonely"])
    assert context["answer"]["relations"] == ["calls", "imports"]
    assert context["answer"]["grade"] == "decision_grade"


def test_context_markdown_advisory_line_on_nonempty_degraded():
    """Same scenario as cli.py's test_cmd_impact_human_advisory_line_on_nonempty_degraded
    (tests/test_health.py), through the context markdown renderer: non-empty
    impact + a degraded scoped cell (language union across two queried
    nodes) grades advisory, and the normal output still prints before the
    epistemology tail."""
    import networkx as nx

    from graphite.context import build_context, format_context_markdown

    g = nx.DiGraph()
    g.add_node("src_a", kind="file", name="a.py", source_file="src/a.py")
    g.add_node("src_b", kind="file", name="b.py", source_file="src/b.py")
    g.add_edge("src_b", "src_a", relation="imports", source_file="src/b.py")
    g.add_node("t", kind="file", name="t.ts", source_file="src/t.ts")
    for i in range(9):
        ph = f"tsph{i}"
        g.add_node(ph, kind="unknown")
        g.add_edge("t", ph, relation="calls", source_file="src/t.ts")
    g.add_node("py_callee", kind="function", name="callee", source_file="src/a.py")
    for i in range(41):
        fn = f"py_fn{i}"
        g.add_node(fn, kind="function", name=f"f{i}", source_file="src/a.py")
        g.add_edge(fn, "py_callee", relation="calls", source_file="src/a.py")

    context = build_context(g, ["src_a", "t"])
    text = format_context_markdown(context)
    assert "src/b.py" in text
    # The impacted-files half is non-empty and must not be marked/replaced;
    # its empty sibling (likely_tests) is allowed to carry the grade-aware
    # INCONCLUSIVE marker now that this round makes it grade-aware (spec §5) --
    # same precedent as cli.py's test_cmd_impact_human_advisory_line_on_nonempty_degraded.
    assert "INCONCLUSIVE" not in text.split("Likely tests:")[0]
    assert "answer health: " in text
    assert "advisory" in text
    assert "known limits:" in text
    # Normal output first, THEN the epistemology tail (not interleaved).
    assert text.index("src/b.py") < text.index("answer health: ")


def test_context_markdown_answer_health_on_empty_or_degraded_not_on_healthy_nonempty():
    from graphite.context import build_context, format_context_markdown

    degraded_empty = format_context_markdown(build_context(_trust_graph(healthy=False), ["lonely"]))
    assert "answer health: " in degraded_empty

    # Same fixture, queried from the other end: "tgt" has a bound incoming
    # edge from "src", so impact is non-empty and the graph is healthy.
    healthy_nonempty = format_context_markdown(build_context(_trust_graph(healthy=True), ["tgt"]))
    assert "answer health:" not in healthy_nonempty


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


def test_context_marks_the_tests_half_when_impacted_files_is_empty():
    """Regression found in self-review: the brief's literal Step 5 guard
    (`if impact["impacted_files"]:`) drops a genuinely non-empty likely_tests
    list whenever impacted_files is empty -- the default shape for a leaf
    module whose only predecessor is its own test file (see
    _reverse_impact: a test-file source lands in likely_tests before the
    `elif node not in start_nodes` check that populates impacted_files).
    cli.py's cmd_impact guards this correctly with `or`
    (`if result["impacted_files"] or result["likely_tests"]:`); context.py
    must match that guard for the "cmd_impact behaves identically" claim in
    the brief to actually hold."""
    from graphite.context import format_context_markdown

    text = format_context_markdown(
        _ctx(impacted=[], tests=["src/app.test.ts"], answer=HEALTHY_ANSWER)
    )

    assert "Likely tests:\n- `src/app.test.ts`" in text
    # The impacted-files guard now matches the tests-half guard (both use
    # `or`), so this half enters the listing branch too and gets a marked
    # empty body -- not the bare single-line sentence.
    assert "Impacted files:\n- none found" in text


def test_context_impacted_files_half_marked_empty_when_only_tests_found():
    """Mirror of the regression above, and the coordinator's follow-up
    finding: with the tests-half guard fixed to `or` but the impacted-files
    guard left as `if impact["impacted_files"]:` alone, this exact shape
    (impacted empty, tests non-empty) fell into the elif/else chain built
    for the genuinely-both-empty case and printed a sentence that speaks
    for BOTH halves (e.g. the `empty_meaning` tail, "no impacted files or
    tests reachable through bound edges") right above a Likely tests
    section that lists a real, reachable test -- a self-contradiction.
    Both guards must agree, matching cli.py's cmd_impact, which wraps both
    listings in a single `if impacted_files or likely_tests:` and only
    falls through to its empty_meaning sentence when both are genuinely
    empty."""
    from graphite.context import format_context_markdown

    answer = dict(
        HEALTHY_ANSWER,
        empty_meaning="no impacted files or tests reachable through bound edges",
    )
    text = format_context_markdown(
        _ctx(impacted=[], tests=["tests/test_a.py"], answer=answer)
    )

    assert "Impacted files:\n- none found" in text
    assert "Likely tests:\n- `tests/test_a.py`" in text
    # The contradictory both-halves sentence must not appear: this half is
    # genuinely empty and is marked as such, but it must not claim tests
    # were unreachable too when a test file is listed right below it.
    assert "no impacted files or tests reachable through bound edges" not in text


def _hub_graph(fanout: int):
    """A hub node with `fanout` dependents and `fanout` dependencies, all bound."""
    import networkx as nx

    g = nx.DiGraph()
    g.add_node("hub", kind="function", source_file="hub.py")
    for i in range(fanout):
        dependency = f"dep{i:03d}"
        dependent = f"caller{i:03d}"
        g.add_node(dependency, kind="function", source_file=f"{dependency}.py")
        g.add_node(dependent, kind="function", source_file=f"{dependent}.py")
        g.add_edge("hub", dependency, relation="calls", source_file="hub.py")
        g.add_edge(dependent, "hub", relation="calls", source_file=f"{dependent}.py")
    return g


def test_context_markdown_honours_neighbor_limit_above_twenty():
    """#11: a hardcoded render-layer cap silently overrode --neighbor-limit.

    The JSON honoured the flag and the markdown did not, so the same command
    against the same graph disagreed with itself.
    """
    from graphite.context import build_context, format_context_markdown

    context = build_context(_hub_graph(30), ["hub"], neighbor_limit=30)
    text = format_context_markdown(context)

    missing = [f"dep{i:03d}" for i in range(30) if f"`dep{i:03d}`" not in text]
    assert not missing, f"markdown dropped {len(missing)} neighbours the flag asked for: {missing[:5]}"


def test_context_markdown_marks_truncated_neighbors():
    """A capped list must say so -- silent truncation reads as completeness."""
    from graphite.context import build_context, format_context_markdown

    context = build_context(_hub_graph(30), ["hub"], neighbor_limit=5)
    text = format_context_markdown(context)

    assert "... 25 more" in text


def test_context_json_carries_neighbor_totals():
    """Without the total, a full-length list is indistinguishable from a capped one."""
    from graphite.context import build_context

    context = build_context(_hub_graph(30), ["hub"], neighbor_limit=5)

    assert len(context["direct_dependencies"]["hub"]) == 5
    assert context["neighbor_totals"]["direct_dependencies"]["hub"] == 30
    assert context["neighbor_totals"]["direct_dependents"]["hub"] == 30


def test_context_neighbor_totals_absent_marker_when_complete():
    """No marker when nothing was hidden -- the marker must mean something."""
    from graphite.context import build_context, format_context_markdown

    context = build_context(_hub_graph(3), ["hub"], neighbor_limit=20)
    text = format_context_markdown(context)

    assert "more" not in text.split("## Direct Dependencies")[1]
