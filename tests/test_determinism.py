"""Build outputs must not depend on iteration order.

`PYTHONHASHSEED` randomizes string hashing per process, so any `set` whose
iteration order reaches an artifact makes that artifact vary between two builds
of one commit. Measured on this repository before these were written: two builds
of the same tree produced `graph.json` files differing in `analysis.cycles` and
in one cluster's `labels`, while `nodes`, `edges` and `metadata` were identical.

These tests vary input order explicitly rather than spawning interpreters with
different seeds. That is a faithful model -- arbitrary order is exactly what a
set supplies -- and unlike a seed sweep it fails deterministically rather than
when a hash collision happens to land the right way.
"""
from __future__ import annotations

import networkx as nx

from graphite.analyze import analyze
from graphite.cluster import _label_cluster


def _graph_with_cycles(order: list[str]) -> nx.DiGraph:
    """Three disjoint 2-cycles plus a 3-cycle, inserted in the given order."""
    g = nx.DiGraph()
    for name in order:
        g.add_node(name, kind="function", name=name, source_file="src/mod.py")
    for left, right in (("b", "a"), ("d", "c"), ("f", "e")):
        g.add_edge(left, right, relation="calls")
        g.add_edge(right, left, relation="calls")
    g.add_edge("g", "h", relation="calls")
    g.add_edge("h", "i", relation="calls")
    g.add_edge("i", "g", relation="calls")
    return g


NODES = ["a", "b", "c", "d", "e", "f", "g", "h", "i"]


def test_reported_cycles_do_not_depend_on_node_insertion_order() -> None:
    """`[:top_n]` truncates whatever order `nx.simple_cycles` happened to yield.

    So this was not merely a cosmetic ordering difference: with more cycles than
    `top_n`, two builds of one commit reported a *different set* of cycles and
    disagreed about which cycles the repository contains.
    """
    forward = analyze(_graph_with_cycles(NODES), top_n=2)["cycles"]
    reverse = analyze(_graph_with_cycles(list(reversed(NODES))), top_n=2)["cycles"]

    assert forward == reverse
    assert len(forward) == 2


def test_each_cycle_is_rotated_to_a_canonical_start() -> None:
    """A cycle is the same cycle under rotation, so one spelling must be chosen.

    Without this, `[a, b, c]` and `[b, c, a]` are both emitted for the same
    cycle depending on where the traversal entered it.
    """
    cycles = analyze(_graph_with_cycles(list(reversed(NODES))), top_n=20)["cycles"]

    assert cycles, "expected the fixture's cycles to be found at all"
    for cycle in cycles:
        assert cycle[0] == min(cycle), cycle


def test_cluster_labels_do_not_depend_on_member_order() -> None:
    """The kind label came from `max()`, which returns the FIRST maximum.

    Its input dict was built by iterating the member set, so a tie between two
    kinds resolved to whichever the set happened to yield first. Measured: one
    cluster's labels were `['src/graphite']` in one build and
    `['src/graphite', 'functions']` in the next, because `unknown` and
    `function` tied and `unknown` suppresses the label.
    """
    g = nx.DiGraph()
    g.add_node("f1", kind="function", source_file="src/pkg/a.py")
    g.add_node("f2", kind="function", source_file="src/pkg/b.py")
    g.add_node("c1", kind="class", source_file="src/pkg/c.py")
    g.add_node("c2", kind="class", source_file="src/pkg/d.py")

    members = ["f1", "f2", "c1", "c2"]
    forward = _label_cluster(g, members)
    reverse = _label_cluster(g, list(reversed(members)))

    assert forward == reverse
    # A tie must resolve by name, not by arrival: `class` sorts before
    # `function`, so the label is pinned rather than merely stable.
    assert forward == ["src/pkg", "classs"]
