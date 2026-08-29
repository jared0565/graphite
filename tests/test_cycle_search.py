"""The cycle report must stay bounded on graphs whose simple cycles are not.

Django 5.2 (issue #64): `tests/` and `django/` import each other densely, and
`analyze()` enumerated every simple cycle of the project graph before keeping
twenty -- 13 GB and more than thirty minutes on 2,930 source files. A complete
digraph on 14 nodes has on the order of 10^10 simple cycles, so an unbounded
enumeration cannot finish here either; the timeout marker is what turns a hang
into a named failure (measured: the unbounded search did not return in 20 s).
"""
from __future__ import annotations

import itertools

import networkx as nx
import pytest

from graphite.analyze import CYCLE_LENGTH_BOUND, CYCLE_SEARCH_BUDGET, analyze


def _complete_digraph(n: int) -> nx.DiGraph:
    g = nx.DiGraph()
    for i in range(n):
        g.add_node(f"n{i:03d}", kind="function", name=f"f{i}")
    for a, b in itertools.permutations(range(n), 2):
        g.add_edge(f"n{a:03d}", f"n{b:03d}", relation="calls")
    return g


def _two_small_cycles() -> nx.DiGraph:
    g = nx.DiGraph()
    for name in ("a", "b", "c", "d", "e"):
        g.add_node(name, kind="function", name=name)
    g.add_edge("a", "b", relation="calls")
    g.add_edge("b", "a", relation="calls")
    g.add_edge("c", "d", relation="calls")
    g.add_edge("d", "e", relation="calls")
    g.add_edge("e", "c", relation="calls")
    return g


@pytest.mark.timeout(30)
def test_a_dense_component_is_analyzed_in_seconds_with_the_shortest_cycles() -> None:
    result = analyze(_complete_digraph(14), top_n=20)

    cycles = result["cycles"]
    assert len(cycles) == 20
    # A complete digraph on 14 nodes has 91 two-cycles, so the twenty
    # shortest are all of length two, each rotated to its smallest node.
    assert all(len(cycle) == 2 for cycle in cycles)
    assert all(cycle[0] == min(cycle) for cycle in cycles)
    assert cycles == sorted(cycles)
    search = result["cycle_search"]
    # Level 2 completed with more than top_n cycles, so the search stopped
    # there: exact, and nowhere near the budget.
    assert search["complete_through_length"] == 2
    assert search["budget_exhausted"] is False
    assert search["examined"] < CYCLE_SEARCH_BUDGET
    assert search["length_bound"] == CYCLE_LENGTH_BOUND
    assert search["budget"] == CYCLE_SEARCH_BUDGET
    assert search["cyclic_components"] == 1
    assert search["largest_component"] == 14


@pytest.mark.timeout(60)
def test_the_budget_stops_the_search_and_the_answer_says_so() -> None:
    """150 nodes: 11,175 two-cycles alone exceed the budget. The report is
    still twenty two-cycles -- the best of what was examined -- and the search
    block records that it was cut off before completing any level."""
    result = analyze(_complete_digraph(150), top_n=20)

    cycles = result["cycles"]
    assert len(cycles) == 20
    assert all(len(cycle) == 2 for cycle in cycles)
    search = result["cycle_search"]
    assert search["budget_exhausted"] is True
    assert search["examined"] == CYCLE_SEARCH_BUDGET
    assert search["complete_through_length"] == 1
    assert search["largest_component"] == 150


def test_a_small_graph_reports_every_cycle_exactly() -> None:
    result = analyze(_two_small_cycles(), top_n=20)

    assert result["cycles"] == [["a", "b"], ["c", "d", "e"]]
    search = result["cycle_search"]
    assert search["budget_exhausted"] is False
    # Fewer than top_n cycles exist, so every level up to the bound completed.
    assert search["complete_through_length"] == CYCLE_LENGTH_BOUND
    assert search["cyclic_components"] == 2
    assert search["largest_component"] == 3


def test_an_acyclic_graph_reports_no_cycles_and_no_components() -> None:
    g = nx.DiGraph()
    g.add_node("a", kind="file", name="a")
    g.add_node("b", kind="file", name="b")
    g.add_edge("a", "b", relation="imports")

    result = analyze(g, top_n=20)

    assert result["cycles"] == []
    search = result["cycle_search"]
    assert search["cyclic_components"] == 0
    assert search["examined"] == 0
    assert search["budget_exhausted"] is False


def test_a_self_loop_is_a_cycle_of_length_one() -> None:
    g = nx.DiGraph()
    g.add_node("recurse", kind="function", name="recurse")
    g.add_edge("recurse", "recurse", relation="calls")

    result = analyze(g, top_n=20)

    assert result["cycles"] == [["recurse"]]
    assert result["cycle_search"]["cyclic_components"] == 1


def test_the_examined_order_is_deterministic_when_the_budget_bites() -> None:
    """Two graphs with the same edges inserted in opposite orders must report
    the same cycles even when the budget truncates the search -- the
    subgraph the search walks is rebuilt in sorted order for that reason."""
    forward = _complete_digraph(150)
    backward = nx.DiGraph()
    backward.add_nodes_from(reversed(list(forward.nodes(data=True))))
    backward.add_edges_from(reversed(list(forward.edges(data=True))))

    assert analyze(forward, top_n=20)["cycles"] == analyze(backward, top_n=20)["cycles"]
