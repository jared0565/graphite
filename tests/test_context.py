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
