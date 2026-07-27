"""Tests for the answer-scoped confidence contract (spec 2026-07-26)."""
import networkx as nx

from graphite.answer_contract import (
    ANSWER_SCHEMA,
    GRADE_ADVISORY,
    GRADE_DECISION,
    GRADE_INCONCLUSIVE,
    active_caveats,
    build_answer_block,
    languages_for_nodes,
)


# NOTE: nx.DiGraph collapses parallel edges; the loops above produce ONE
# edge per (u, v) pair. Build distinct phantom targets instead:
def _graph_ratio(lang_ext, bound_n, unbound_n):
    g = nx.DiGraph()
    src = f"caller{lang_ext.replace('.', '_')}"
    g.add_node(src, kind="function", source_file=f"a{lang_ext}")
    for i in range(bound_n):
        t = f"bound{i}"
        g.add_node(t, kind="function", source_file=f"b{lang_ext}")
        g.add_edge(src, t, relation="calls", source_file=f"a{lang_ext}")
    for i in range(unbound_n):
        t = f"phantom{i}"
        g.add_node(t, kind="unknown")
        g.add_edge(src, t, relation="calls", source_file=f"a{lang_ext}")
    return g


def _merged(g1, g2):
    return nx.compose(g1, g2)


def test_grade_decision_when_all_cells_healthy():
    g = _graph_ratio(".py", 9, 1)  # python calls 0.9 >= 0.8
    block = build_answer_block(g, relations=("calls",), languages=["python"], total=5)
    assert block["schema"] == ANSWER_SCHEMA
    assert block["grade"] == GRADE_DECISION
    assert block["health"]["calls"]["python"]["healthy"] is True
    assert "empty_meaning" not in block


def test_grade_advisory_when_degraded_and_nonempty():
    g = _graph_ratio(".ts", 1, 9)  # typescript calls 0.1 < 0.8
    block = build_answer_block(g, relations=("calls",), languages=["typescript"], total=3)
    assert block["grade"] == GRADE_ADVISORY


def test_grade_inconclusive_when_degraded_and_empty():
    g = _graph_ratio(".ts", 1, 9)
    block = build_answer_block(
        g, relations=("calls",), languages=["typescript"], total=0,
        empty_meaning="no bound callers found",
    )
    assert block["grade"] == GRADE_INCONCLUSIVE
    assert block["empty_meaning"] == "no bound callers found"


def test_scoped_cells_ignore_other_languages():
    """The firescraper regression: healthy python must not mask degraded ts."""
    g = _merged(_graph_ratio(".py", 9, 1), _graph_ratio(".ts", 1, 9))
    block = build_answer_block(g, relations=("calls",), languages=["typescript"], total=0)
    assert block["grade"] == GRADE_INCONCLUSIVE
    assert "python" not in block["health"]["calls"]


def test_language_fallback_is_graph_wide():
    g = _merged(_graph_ratio(".py", 9, 1), _graph_ratio(".ts", 1, 9))
    block = build_answer_block(g, relations=("calls",), languages=[], total=1)
    assert set(block["languages"]) == {"python", "typescript"}
    assert block["grade"] == GRADE_ADVISORY  # ts cell degraded


def test_missing_cells_are_omitted_and_do_not_degrade():
    g = _graph_ratio(".py", 9, 1)  # no imports edges at all
    block = build_answer_block(g, relations=("calls", "imports"), languages=["python"], total=1)
    assert "imports" not in block["health"] or block["health"]["imports"] == {}
    assert block["grade"] == GRADE_DECISION


def test_caveat_filtering_by_relation_and_language():
    g = _graph_ratio(".py", 9, 1)
    block = build_answer_block(g, relations=("calls",), languages=["python"], total=1)
    codes = {c["code"] for c in block["caveats"]}
    assert "python-dynamic-dispatch" in codes
    assert "ts-external-calls-unclassified" not in codes
    imports_block = build_answer_block(g, relations=("imports",), languages=["python"], total=1)
    assert imports_block["caveats"] == []


def test_caveats_project_only_code_and_summary():
    g = _graph_ratio(".py", 9, 1)
    block = build_answer_block(g, relations=("calls",), languages=["python"], total=1)
    assert set(block["caveats"][0].keys()) == {"code", "summary"}


def test_retired_caveats_never_emitted(monkeypatch):
    import graphite.answer_contract as ac
    retired = {
        "code": "test-retired", "relations": ("calls",), "languages": ("python",),
        "summary": "x", "since": "2026-07-26", "retired_by": "v8",
    }
    monkeypatch.setattr(ac, "CAVEAT_REGISTRY", (*ac.CAVEAT_REGISTRY, retired))
    assert all(e["code"] != "test-retired" for e in ac.active_caveats())
    g = _graph_ratio(".py", 9, 1)
    block = ac.build_answer_block(g, relations=("calls",), languages=["python"], total=1)
    assert all(c["code"] != "test-retired" for c in block["caveats"])


def test_registry_initial_entries():
    codes = {e["code"] for e in active_caveats()}
    assert codes == {
        "python-dynamic-dispatch",
        "ts-destructured-locals-unbound",
    }


def test_fail_open_returns_none(monkeypatch):
    import graphite.answer_contract as ac
    def boom(_g):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(ac, "resolution_health", boom)
    g = _graph_ratio(".py", 1, 0)
    assert build_answer_block(g, relations=("calls",), languages=["python"], total=1) is None


def test_languages_for_nodes():
    g = _merged(_graph_ratio(".py", 1, 0), _graph_ratio(".ts", 1, 0))
    assert languages_for_nodes(g, ["caller_py", "caller_ts"]) == ["python", "typescript"]
    assert languages_for_nodes(g, ["phantom0"]) == []
    assert languages_for_nodes(g, ["no-such-node"]) == []


def test_unattributable_receiver_caveat_kept_its_published_shape_when_retired():
    """Originally F4 pinned this caveat as ACTIVE, because the false external it
    warns about was a live blindspot. #14 fixed the blindspot, so the entry is
    now retired -- but its published fields must not have drifted in the
    process. A consumer that recorded this code keeps the meaning it was
    published with."""
    from graphite.answer_contract import CAVEAT_REGISTRY

    by_code = {e["code"]: e for e in CAVEAT_REGISTRY}
    entry = by_code["calls-unattributable-receiver-false-external"]
    assert entry["relations"] == ("calls",)
    assert entry["languages"] == ("typescript", "javascript", "python")
    assert entry["since"] == "2026-07-27"
    assert entry["retired_by"] == "2026-07-27"


def test_ts_external_calls_caveat_is_retired_with_a_successor():
    from graphite.answer_contract import CAVEAT_REGISTRY, active_caveats

    by_code = {e["code"]: e for e in CAVEAT_REGISTRY}
    assert by_code["ts-external-calls-unclassified"]["retired_by"]
    active = {e["code"] for e in active_caveats()}
    assert "ts-external-calls-unclassified" not in active
    assert "ts-destructured-locals-unbound" in active
    successor = by_code["ts-destructured-locals-unbound"]
    assert successor["relations"] == ("calls",)
    assert successor["languages"] == ("typescript", "javascript")


def test_zero_cell_answer_is_not_decision_grade_when_empty():
    """#12: no cells is no evidence, so an empty answer cannot claim a trustworthy absence."""
    g = _graph_ratio(".py", 9, 1)  # python-only graph
    block = build_answer_block(g, relations=("calls",), languages=["typescript"], total=0)

    assert block["health"] == {}, "fixture must actually produce zero cells"
    assert block["grade"] == GRADE_INCONCLUSIVE


def test_zero_cell_answer_is_advisory_when_nonempty():
    """A non-empty answer measured against nothing is usable but unverified."""
    g = _graph_ratio(".py", 9, 1)
    block = build_answer_block(g, relations=("calls",), languages=["typescript"], total=3)

    assert block["health"] == {}
    assert block["grade"] == GRADE_ADVISORY


def test_zero_cell_empty_listing_is_marked_inconclusive():
    """The bare 'none found' claimed an absence nothing had verified."""
    from graphite.answer_contract import INCONCLUSIVE_EMPTY, empty_marker, is_unmeasured

    g = _graph_ratio(".py", 9, 1)
    block = build_answer_block(g, relations=("calls",), languages=["typescript"], total=0)

    assert is_unmeasured(block) is True
    assert empty_marker(block) == INCONCLUSIVE_EMPTY


def test_fail_open_block_stays_permissive():
    """A None block is the fail-open path and must NOT be treated as unmeasured."""
    from graphite.answer_contract import empty_marker, is_unmeasured

    assert is_unmeasured(None) is False
    assert empty_marker(None) == "none found"


def test_measured_healthy_answer_still_grades_decision():
    """Guard against over-firing: real cells must still reach decision_grade."""
    g = _graph_ratio(".py", 9, 1)
    block = build_answer_block(g, relations=("calls",), languages=["python"], total=0)

    assert block["health"], "fixture must produce cells"
    assert block["grade"] == GRADE_DECISION


def test_unattributable_receiver_caveat_is_retired():
    """#14 fixed mechanism A; the published code keeps its original meaning."""
    from graphite.answer_contract import CAVEAT_REGISTRY, active_caveats

    by_code = {e["code"]: e for e in CAVEAT_REGISTRY}
    entry = by_code["calls-unattributable-receiver-false-external"]
    assert entry["retired_by"], "must be retired, not deleted or reworded"
    assert "bare method name" in entry["summary"], "a published summary never changes"
    assert "calls-unattributable-receiver-false-external" not in {e["code"] for e in active_caveats()}
