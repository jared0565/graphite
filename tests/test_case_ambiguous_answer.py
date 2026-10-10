"""A pick between spellings is said out loud (channel round 317, the grade).

Matching ignores case, so `Verdict` and `verdict` are both hits for either
spelling, and path depth and scope choose between them. When the node chosen is
not spelled as typed, the answer may be about a different symbol. It used to be
graded `decision_grade` like any other, with the other spelling visible only in
`alternates`, which a reader gating on the grade does not consult.

Every fixture call edge is bound, so the python cells are healthy. Each test
asserts that, because an unmeasured answer is `advisory` for a reason of its own
and would pass the hedge's assertions without the hedge.
"""
from __future__ import annotations

import pytest

from graphite.answer_contract import (
    CASE_AMBIGUOUS_CODE,
    GRADE_ADVISORY,
    GRADE_DECISION,
    empty_marker,
)
from graphite.cli import _answer_lines, _impact
from graphite.context import build_context, format_context_markdown
from graphite.graph import build_graph
from graphite.natural_query import answer_natural
from graphite.query import _find_node_detail, query


def _fn(node_id: str, name: str, source_file: str, **extra: object) -> dict:
    return {"id": node_id, "kind": "function", "name": name, "qualname": name, "source_file": source_file, **extra}


def _calls(source: str, target: str, source_file: str) -> dict:
    return {"source": source, "target": target, "relation": "calls", "source_file": source_file}


def _graph():
    """`Verdict` the class sits one directory deeper than `verdict` the function,
    so path depth picks the function whichever spelling is typed. `report` is the
    mirror image: the lowercase function is the deeper one."""
    nodes = [
        {"id": "a_py", "kind": "file", "name": "a.py", "source_file": "a.py"},
        {"id": "pkg_b_py", "kind": "file", "name": "b.py", "source_file": "pkg/b.py"},
        _fn("a_py_verdict", "verdict", "a.py"),
        {"id": "pkg_b_py_verdict_345430", "kind": "class", "name": "Verdict", "qualname": "Verdict",
         "source_file": "pkg/b.py"},
        {"id": "a_py_report_9f31c2", "kind": "class", "name": "Report", "qualname": "Report", "source_file": "a.py"},
        _fn("pkg_b_py_report", "report", "pkg/b.py"),
        # two spellings, neither of them lowercase
        _fn("a_py_shout_aa11", "Shout", "a.py"),
        _fn("pkg_b_py_shout_bb22", "SHOUT", "pkg/b.py"),
        # two definitions spelled alike: a duplicate, not a collision of case
        _fn("a_py_twin", "twin", "a.py"),
        _fn("pkg_b_py_twin", "twin", "pkg/b.py"),
        # two spellings, and nothing calls or imports the shallow one
        _fn("a_py_quiet", "quiet", "a.py"),
        _fn("pkg_b_py_quiet_cc33", "Quiet", "pkg/b.py"),
        # one spelling only
        _fn("a_py_user", "user", "a.py"),
        _fn("pkg_b_py_other", "other", "pkg/b.py"),
        # dotted qualnames in two spellings, the exact one deeper
        _fn("a_py_worker_run_1", "run", "a.py", qualname="worker.run", is_method=True),
        _fn("pkg_b_py_worker_run_2", "run", "pkg/b.py", qualname="Worker.run", is_method=True),
    ]
    edges = [
        _calls("a_py_user", "a_py_verdict", "a.py"),
        _calls("pkg_b_py_other", "pkg_b_py_verdict_345430", "pkg/b.py"),
        _calls("a_py_user", "a_py_report_9f31c2", "a.py"),
        _calls("a_py_user", "a_py_shout_aa11", "a.py"),
        _calls("a_py_user", "a_py_twin", "a.py"),
        _calls("pkg_b_py_other", "a_py_user", "pkg/b.py"),
        _calls("a_py_user", "a_py_worker_run_1", "a.py"),
        _calls("a_py_quiet", "a_py_user", "a.py"),
        {"source": "pkg_b_py", "target": "a_py", "relation": "imports", "source_file": "pkg/b.py"},
    ]
    return build_graph(nodes, edges)


def _codes(answer: dict) -> set[str]:
    return {caveat["code"] for caveat in answer["caveats"]}


def _measured_healthy(answer: dict, relation: str = "calls") -> bool:
    return answer["health"][relation]["python"]["healthy"] is True


# ------------------------------------------------------------------ the matcher --


@pytest.mark.parametrize(
    ("token", "node", "match_type", "ambiguous"),
    [
        # the spelling typed exists, and a different one was chosen
        ("Verdict", "a_py_verdict", "name", True),
        ("report", "a_py_report_9f31c2", "name", True),
        ("Worker.run", "a_py_worker_run_1", "qualname", True),
        # several spellings, none of them the one typed
        ("shout", "a_py_shout_aa11", "name", True),
        ("VERDICT", "a_py_verdict", "name", True),
        # typed and chosen agree
        ("verdict", "a_py_verdict", "name", False),
        ("Report", "a_py_report_9f31c2", "name", False),
        ("Shout", "a_py_shout_aa11", "name", False),
        ("worker.run", "a_py_worker_run_1", "qualname", False),
        # one spelling only: nothing to confuse it with, however it is typed
        ("USER", "a_py_user", "name", False),
        ("twin", "a_py_twin", "name", False),
        ("TWIN", "a_py_twin", "name", False),
        # a node id names one node
        ("PKG_B_PY_VERDICT_345430", "pkg_b_py_verdict_345430", "exact-id", False),
    ],
)
def test_the_matcher_reports_a_pick_between_spellings(
    token: str, node: str, match_type: str, ambiguous: bool
) -> None:
    detail = _find_node_detail(_graph(), token)

    assert detail is not None
    assert (detail.node, detail.match_type) == (node, match_type)
    assert detail.case_ambiguous is ambiguous


def test_the_other_spelling_counts_even_beyond_the_alternates_shown() -> None:
    """`alternates` lists three. The spelling typed may be the fifth hit, so the
    check runs over every hit and not over what is published."""
    nodes = [_fn(f"a_py_item_{i}", "item", f"a{i}.py") for i in range(5)]
    nodes.append(_fn("deep_pkg_item_zz", "Item", "deep/pkg/item.py"))
    detail = _find_node_detail(build_graph(nodes, []), "Item")

    assert detail is not None
    assert detail.node == "a_py_item_0"
    assert "deep_pkg_item_zz" not in detail.alternates
    assert detail.case_ambiguous is True


# -------------------------------------------------------------------- `query` --


def test_an_answer_about_another_spelling_is_not_decision_grade() -> None:
    out = query(_graph(), "callers Verdict")

    (resolution,) = out["resolution"]
    assert resolution["node"] == "a_py_verdict"
    assert resolution["case_ambiguous"] is True
    assert [c["id"] for c in out["callers"]] == ["a_py_user"]
    assert _measured_healthy(out["answer"])
    assert out["answer"]["grade"] == GRADE_ADVISORY
    assert CASE_AMBIGUOUS_CODE in _codes(out["answer"])
    assert out["inconclusive"] is False


def test_the_spelling_typed_and_chosen_is_graded_as_before() -> None:
    """The hedge is absent where it does not apply. An always-on caveat trains
    readers to ignore caveats."""
    out = query(_graph(), "callers verdict")

    (resolution,) = out["resolution"]
    assert resolution["node"] == "a_py_verdict"
    assert "case_ambiguous" not in resolution
    assert _measured_healthy(out["answer"])
    assert out["answer"]["grade"] == GRADE_DECISION
    assert CASE_AMBIGUOUS_CODE not in _codes(out["answer"])


def test_a_duplicate_spelled_alike_is_not_hedged() -> None:
    """Two `twin`s are listed in `alternates` as before. Hedging every such
    name would cover 260 names on graphite and 3,189 on Django."""
    out = query(_graph(), "callers twin")

    (resolution,) = out["resolution"]
    assert resolution["alternates"] == ["pkg_b_py_twin"]
    assert "case_ambiguous" not in resolution
    assert out["answer"]["grade"] == GRADE_DECISION


@pytest.mark.parametrize(
    ("question", "role"),
    [("reaches other -> Verdict", "target"), ("reaches Quiet -> user", "source"), ("path other -> report", "target")],
)
def test_either_end_of_a_two_target_question_can_be_the_ambiguous_one(question: str, role: str) -> None:
    out = query(_graph(), question)

    assert [entry["role"] for entry in out["resolution"] if entry.get("case_ambiguous")] == [role]
    assert _measured_healthy(out["answer"])
    assert out["answer"]["grade"] == GRADE_ADVISORY
    assert CASE_AMBIGUOUS_CODE in _codes(out["answer"])


def test_a_two_target_question_typed_as_chosen_is_graded_as_before() -> None:
    out = query(_graph(), "reaches other -> verdict")

    assert not any(entry.get("case_ambiguous") for entry in out["resolution"])
    assert out["answer"]["grade"] == GRADE_DECISION


def test_no_path_has_no_grade_to_hedge_and_names_the_node_it_used() -> None:
    """A limit this round leaves where it was (#76). "No path" is an error with
    no `answer` block (`test_no_path_error_has_no_answer_block`), so there is no
    grade to lower. What a reader has is the node id in the message."""
    out = query(_graph(), "reaches Verdict -> other")

    assert out["error_code"] == "no_path"
    assert "a_py_verdict" in out["error"]
    assert "answer" not in out


def test_an_empty_answer_about_another_spelling_names_that_as_the_reason() -> None:
    """`imported-by` on Python is otherwise a trustworthy absence, so the only
    reason this one is not is the pick. The line must say so, and must not
    blame a callback registration."""
    out = query(_graph(), "imported-by Quiet")

    assert out["resolution"][0]["node"] == "a_py_quiet"
    assert out["total"] == 0
    assert _measured_healthy(out["answer"], "imports")
    assert out["answer"]["grade"] == GRADE_ADVISORY
    marker = empty_marker(out["answer"])
    assert marker.startswith("none found — UNVERIFIED: the name matched more than one spelling")
    assert "callback" not in marker

    exact = query(_graph(), "imported-by quiet")
    assert exact["total"] == 0
    assert exact["answer"]["grade"] == GRADE_DECISION
    assert empty_marker(exact["answer"]) == "none found"


def test_an_empty_callers_answer_gives_both_reasons_the_pick_first() -> None:
    """An empty `callers` answer is never proof on its own account. Hedged as
    well, the likelier cause comes first: the answer is for another symbol."""
    out = query(_graph(), "callers Quiet")

    assert out["resolution"][0]["node"] == "a_py_quiet"
    assert out["total"] == 0
    marker = empty_marker(out["answer"])
    assert marker.startswith("none found — UNVERIFIED: the name matched more than one spelling")
    assert marker.index("more than one spelling") < marker.index("callback-registered")

    plain = empty_marker(query(_graph(), "callers quiet")["answer"])
    assert "more than one spelling" not in plain
    assert "callback-registered" in plain


# ------------------------------------------------------- `context` and `impact` --


def test_context_carries_the_hedge_and_prints_it() -> None:
    context = build_context(_graph(), ["Verdict"])

    (entry,) = context["matched"]
    assert entry["node"]["id"] == "a_py_verdict"
    assert entry["case_ambiguous"] is True
    assert context["impact"]["impacted_files"] or context["impact"]["likely_tests"]
    assert context["answer"]["grade"] == GRADE_ADVISORY
    assert CASE_AMBIGUOUS_CODE in _codes(context["answer"])
    rendered = format_context_markdown(context)
    assert "answer health:" in rendered
    assert "more than one spelling" in rendered


def test_context_for_the_spelling_chosen_prints_no_hedge() -> None:
    context = build_context(_graph(), ["verdict"])

    assert "case_ambiguous" not in context["matched"][0]
    assert context["answer"]["grade"] == GRADE_DECISION
    assert "answer health:" not in format_context_markdown(context)


def test_impact_carries_the_hedge_and_prints_it() -> None:
    result = _impact(_graph(), ["Verdict"], 2)

    assert result["matched_nodes"] == ["a_py_verdict"]
    assert result["impacted_files"] or result["likely_tests"]
    assert result["answer"]["grade"] == GRADE_ADVISORY
    lines = _answer_lines(result["answer"], empty=False)
    assert lines and lines[0].startswith("answer health:")
    assert any("more than one spelling" in line for line in lines)

    exact = _impact(_graph(), ["verdict"], 2)
    assert exact["answer"]["grade"] == GRADE_DECISION
    assert _answer_lines(exact["answer"], empty=False) == []


@pytest.mark.parametrize("inputs", [["Verdict", "user"], ["user", "Verdict"]])
def test_one_ambiguous_input_among_several_hedges_the_whole_answer(inputs: list[str]) -> None:
    """One answer covers every input, so one pick is enough to qualify it,
    wherever in the list it comes."""
    impact = _impact(_graph(), inputs, 2)
    context = build_context(_graph(), inputs)

    assert impact["answer"]["grade"] == GRADE_ADVISORY
    assert CASE_AMBIGUOUS_CODE in _codes(impact["answer"])
    assert context["answer"]["grade"] == GRADE_ADVISORY
    assert [bool(entry.get("case_ambiguous")) for entry in context["matched"]] == [
        name == "Verdict" for name in inputs
    ]


# -------------------------------------------------------------------- `--natural` --


def test_a_natural_question_is_judged_in_the_case_it_was_asked() -> None:
    """The question used to be lowercased before the matcher saw it. "Who calls
    Verdict?" then arrived as `verdict`, the function was "spelled as typed",
    and nothing said the answer was about another symbol."""
    out = answer_natural(_graph(), "Who calls Verdict?")

    (resolution,) = out["resolution"]
    assert resolution["input"] == "Verdict"
    assert resolution["node"] == "a_py_verdict"
    assert resolution["case_ambiguous"] is True
    assert out["answer"]["grade"] == GRADE_ADVISORY

    exact = answer_natural(_graph(), "Who calls verdict?")
    assert "case_ambiguous" not in exact["resolution"][0]
    assert exact["answer"]["grade"] == GRADE_DECISION
