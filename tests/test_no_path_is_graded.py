"""A "no path" answer is graded like any other empty answer (#76).

`reaches` and `path` answer an unreachable pair with the error `no_path`. Of
everything those two verbs return it is the one result that asserts an absence,
and it was the one result nothing graded: the handlers did not say which nodes
they had resolved, so the grade had no languages to scope against and no block
was built. Where most call edges were unbound, `callers` said `inconclusive`
and `reaches` said "no call path".

The grade is scoped to the two resolved ends and to nothing else. Before
2026-07-31 a `no_path` was graded against every language in the graph, which is
why it then carried no block at all. The fixture for files that are not code
holds a degraded language of its own, to show that defect has not come back;
`test_query_plan.py` holds the two-language case.
"""
from __future__ import annotations

import pytest

from graphite.answer_contract import (
    GRADE_ADVISORY,
    GRADE_DECISION,
    GRADE_INCONCLUSIVE,
    INCONCLUSIVE_EMPTY,
    UNVERIFIED_EMPTY,
    empty_marker,
)
from graphite.graph import build_graph
from graphite.natural_query import answer_natural
from graphite.query import build_plan, execute_plan, query


def _fn(node_id: str, name: str, source_file: str) -> dict:
    return {"id": node_id, "kind": "function", "name": name, "qualname": name, "source_file": source_file}


def _calls(source: str, target: str, source_file: str) -> dict:
    return {"source": source, "target": target, "relation": "calls", "source_file": source_file}


def _mostly_unbound(stem: str, source_file: str) -> tuple[list[dict], list[dict]]:
    """`start` makes ten calls: one bound, to `near`, and nine to names nothing
    defines. Nothing reaches `goal`, and a real call to it may be among the nine."""
    nodes = [_fn(f"{stem}_{name}", name, source_file) for name in ("start", "near", "goal")]
    edges = [_calls(f"{stem}_start", f"{stem}_near", source_file)]
    edges += [_calls(f"{stem}_start", f"{stem}_unbound_{i}", source_file) for i in range(9)]
    return nodes, edges


def _degraded_python():
    return build_graph(*_mostly_unbound("a_py", "a.py"))


def _healthy_python():
    """Every call edge is bound, so the python cell reads 1.0. `idle` calls
    nothing and nothing calls it."""
    nodes = [_fn(f"a_py_{name}", name, "a.py") for name in ("caller", "middle", "callee", "idle")]
    edges = [_calls("a_py_caller", "a_py_middle", "a.py"), _calls("a_py_middle", "a_py_callee", "a.py")]
    return build_graph(nodes, edges)


def _calls_cell(answer: dict, language: str = "python") -> dict:
    return answer["health"]["calls"][language]


# ------------------------------------------------------------- the degraded cell --


@pytest.mark.parametrize("question", ["reaches start -> goal", "path start -> goal"])
def test_no_path_over_a_mostly_unbound_cell_is_inconclusive(question: str) -> None:
    g = _degraded_python()

    # The control. Without it a missing grade could be a healthy graph.
    control = query(g, "callers goal")
    assert control["answer"]["grade"] == GRADE_INCONCLUSIVE
    assert control["inconclusive"] is True
    assert _calls_cell(control["answer"])["ratio"] == 0.1

    out = query(g, question)

    assert out["error_code"] == "no_path"
    assert out["truncated"] is False
    assert _calls_cell(out["answer"]) == _calls_cell(control["answer"])
    assert out["answer"]["grade"] == GRADE_INCONCLUSIVE
    assert out["inconclusive"] is True
    assert empty_marker(out["answer"]) == INCONCLUSIVE_EMPTY


# -------------------------------------------------------------- the healthy cell --


@pytest.mark.parametrize(
    ("question", "meaning"),
    [
        ("reaches idle -> callee", "no call path found within depth"),
        ("path idle -> callee", "no path found within depth"),
    ],
)
def test_no_path_over_healthy_cells_is_advisory_and_not_decision_grade(question: str, meaning: str) -> None:
    """The cell is measured and good. What it cannot support is the absence: a
    caller registered as a callback emits no edge, so a path through one is
    missing from a graph whose ratio reads 1.0. An empty `callers` answer is
    graded the same way, for the same reason."""
    g = _healthy_python()

    out = query(g, question)

    assert out["error_code"] == "no_path"
    assert _calls_cell(out["answer"])["healthy"] is True
    assert out["answer"]["grade"] == GRADE_ADVISORY
    assert out["inconclusive"] is False
    assert out["answer"]["empty_meaning"] == meaning
    assert empty_marker(out["answer"]) == UNVERIFIED_EMPTY


def test_the_healthy_fixture_can_grade_decision_grade() -> None:
    """So `advisory` above is the absence speaking, and not a fixture that could
    never grade higher."""
    out = query(_healthy_python(), "reaches caller -> callee")

    assert out["length"] == 2
    assert out["answer"]["grade"] == GRADE_DECISION
    assert "inconclusive" not in out


# -------------------------------------------------------------- what is carried --


def test_a_graded_no_path_keeps_its_error_fields_and_gains_three() -> None:
    """`error_code` is a stable code and the message is unchanged. `resolution`,
    `answer` and `inconclusive` are the additions; the per-verb `match`, `source`
    and `target` of a found path are not among them."""
    out = query(_degraded_python(), "reaches start -> goal")

    assert set(out) == {
        "schema_version", "error", "error_code", "truncated", "limits",
        "resolution", "answer", "inconclusive",
    }
    assert out["error"] == "no call path from a_py_start to a_py_goal"
    assert out["error_code"] == "no_path"
    assert out["limits"] == {"max_depth": 32}
    assert out["resolution"] == [
        {"role": "source", "input": "start", "node": "a_py_start", "type": "name"},
        {"role": "target", "input": "goal", "node": "a_py_goal", "type": "name"},
    ]
    assert out["answer"]["relations"] == ["calls"]
    assert out["answer"]["languages"] == ["python"]


def test_a_no_path_cut_short_by_the_depth_bound_stays_truncated_and_is_graded() -> None:
    """`truncated: true` keeps its meaning: the bound was hit and a longer path
    may exist. The grade is a second statement and does not replace it."""
    g = _healthy_python()
    assert query(g, "reaches caller -> callee")["length"] == 2

    out = execute_plan(g, {**build_plan("reaches caller -> callee"), "options": {"max_depth": 1}})

    assert out["error_code"] == "no_path"
    assert out["truncated"] is True
    assert out["limits"] == {"max_depth": 1}
    assert out["answer"]["grade"] == GRADE_ADVISORY
    assert out["inconclusive"] is False


# ----------------------------------------------------------- nothing to grade by --


def test_no_path_between_two_files_that_are_not_code_is_not_graded() -> None:
    """Neither end has a language whose calls or imports were measured, so there
    is nothing to grade and no block, as before. The graph holds a degraded
    typescript cell: graded against the whole graph, as a `no_path` was before
    2026-07-31, this answer would read `inconclusive` over two markdown files."""
    nodes, edges = _mostly_unbound("b_ts", "b.ts")
    nodes += [
        {"id": "readme_md", "kind": "file", "name": "README.md", "source_file": "README.md"},
        {"id": "docs_guide_md", "kind": "file", "name": "guide.md", "source_file": "docs/guide.md"},
    ]
    g = build_graph(nodes, edges)
    assert query(g, "callers goal")["answer"]["grade"] == GRADE_INCONCLUSIVE

    out = query(g, "path readme_md -> docs_guide_md")

    assert out["error_code"] == "no_path"
    assert "answer" not in out
    assert "inconclusive" not in out
    assert [entry["node"] for entry in out["resolution"]] == ["readme_md", "docs_guide_md"]


def test_one_end_that_is_code_is_enough_to_grade_by() -> None:
    nodes, edges = _mostly_unbound("b_ts", "b.ts")
    nodes.append({"id": "readme_md", "kind": "file", "name": "README.md", "source_file": "README.md"})
    g = build_graph(nodes, edges)

    out = query(g, "reaches readme_md -> goal")

    assert out["error_code"] == "no_path"
    assert out["answer"]["languages"] == ["typescript"]
    assert out["answer"]["grade"] == GRADE_INCONCLUSIVE
    assert out["inconclusive"] is True


# ------------------------------------------------------------------- `--natural` --


def test_a_natural_question_gets_the_same_graded_no_path() -> None:
    """`--natural` runs the same plan, so the grade comes with it."""
    g = _degraded_python()

    out = answer_natural(g, "can start reach goal?")

    assert out["natural"]["intent"] == "reaches"
    assert out["plan"]["operation"] == "reaches"
    assert out["error_code"] == "no_path"
    assert out["answer"]["grade"] == GRADE_INCONCLUSIVE
    assert out["inconclusive"] is True
    assert [entry["node"] for entry in out["resolution"]] == ["a_py_start", "a_py_goal"]
