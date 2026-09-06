"""Tests for `graphite.cluster`, community detection and its zero-LLM labels.

Named after the module so aramid's mutation stage 1 (`tests/test_<module>.py`)
has something to run; `-k cluster` collected one unrelated test before this
file. `test_determinism.py` reaches this module only through a full build.
"""
from __future__ import annotations

import networkx as nx

from graphite.cluster import _common_prefix, _label_cluster, detect_communities


def _two_cliques() -> nx.DiGraph:
    """Two dense triangles joined by one edge: any community method splits them."""
    g = nx.DiGraph()
    for name in ("a1", "a2", "a3"):
        g.add_node(name, kind="function", source_file=f"src/pkg_a/{name}.py")
    for name in ("b1", "b2", "b3"):
        g.add_node(name, kind="class", source_file=f"src/pkg_b/{name}.py")
    for u, v in (("a1", "a2"), ("a2", "a3"), ("a3", "a1")):
        g.add_edge(u, v, relation="calls", weight=1.0)
        g.add_edge(v, u, relation="calls", weight=1.0)
    for u, v in (("b1", "b2"), ("b2", "b3"), ("b3", "b1")):
        g.add_edge(u, v, relation="calls", weight=1.0)
        g.add_edge(v, u, relation="calls", weight=1.0)
    g.add_edge("a1", "b1", relation="imports", weight=0.1)
    return g


# --- _common_prefix ------------------------------------------------------------


def test_common_prefix_of_nothing_is_empty() -> None:
    assert _common_prefix([]) == ""


def test_common_prefix_is_the_shared_leading_segments() -> None:
    assert _common_prefix(["src/graphite/a", "src/graphite/b", "src/graphite"]) == "src/graphite"


def test_common_prefix_stops_at_the_first_differing_segment() -> None:
    assert _common_prefix(["src/a/x", "src/b/x"]) == "src"
    assert _common_prefix(["src/a", "lib/a"]) == ""


# --- _label_cluster -------------------------------------------------------------


def test_label_cluster_names_the_shared_directory_and_the_dominant_kind() -> None:
    g = _two_cliques()
    assert _label_cluster(g, {"a1", "a2", "a3"}) == ["src/pkg_a", "functions"]


def test_label_cluster_breaks_a_kind_tie_by_name_not_by_set_order() -> None:
    g = nx.DiGraph()
    g.add_node("f", kind="function")
    g.add_node("c", kind="class")
    # `class` < `function`, so the tie resolves to the class label however the
    # set iterates. The label is `f"{kind}s"` verbatim -- "classs" is what the
    # artifact carries today; change it deliberately, not by accident.
    for members in ({"f", "c"}, {"c", "f"}):
        assert _label_cluster(g, members) == ["classs"]


def test_label_cluster_suppresses_an_unknown_dominant_kind() -> None:
    g = nx.DiGraph()
    g.add_node("x")
    g.add_node("y")
    g.add_node("f", kind="function")
    assert _label_cluster(g, {"x", "y", "f"}) == []


def test_label_cluster_drops_a_root_directory_label() -> None:
    g = nx.DiGraph()
    g.add_node("top", kind="file", source_file="README.md")
    assert _label_cluster(g, {"top"}) == ["files"]


# --- detect_communities ---------------------------------------------------------


def test_detect_communities_separates_two_cliques() -> None:
    result = detect_communities(_two_cliques())
    assert result["count"] == 2
    groups = {frozenset(c["members"]) for c in result["clusters"]}
    assert groups == {frozenset({"a1", "a2", "a3"}), frozenset({"b1", "b2", "b3"})}


def test_detect_communities_counts_kinds_per_cluster_and_labels_them() -> None:
    result = detect_communities(_two_cliques())
    by_members = {tuple(c["members"]): c for c in result["clusters"]}
    a = by_members[("a1", "a2", "a3")]
    b = by_members[("b1", "b2", "b3")]
    assert (a["size"], a["file_count"], a["function_count"], a["class_count"]) == (3, 0, 3, 0)
    assert (b["size"], b["file_count"], b["function_count"], b["class_count"]) == (3, 0, 0, 3)
    assert a["labels"] == ["src/pkg_a", "functions"]
    assert b["labels"] == ["src/pkg_b", "classs"]  # `f"{kind}s"`, see the tie test


def test_detect_communities_maps_every_node_and_sorts_the_map() -> None:
    g = _two_cliques()
    result = detect_communities(g)
    mapping = result["node_to_community"]
    assert list(mapping) == sorted(g.nodes())
    for cluster in result["clusters"]:
        for member in cluster["members"]:
            assert mapping[member] == cluster["id"]


def test_detect_communities_orders_clusters_largest_first() -> None:
    g = _two_cliques()
    g.add_node("lone", kind="function", source_file="src/pkg_c/lone.py")
    result = detect_communities(g)
    sizes = [c["size"] for c in result["clusters"]]
    assert sizes == sorted(sizes, reverse=True)
    assert result["count"] == 3


def test_detect_communities_is_deterministic_across_runs() -> None:
    first = detect_communities(_two_cliques())
    second = detect_communities(_two_cliques())
    assert first == second
