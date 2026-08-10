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


def _graph_imports(lang_ext, bound_n, unbound_n):
    """`_graph_ratio`'s shape for the imports relation."""
    g = nx.DiGraph()
    src = f"importer{lang_ext.replace('.', '_')}"
    g.add_node(src, kind="file", source_file=f"a{lang_ext}")
    for i in range(bound_n):
        t = f"imported{i}{lang_ext.replace('.', '_')}"
        g.add_node(t, kind="file", source_file=f"b{i}{lang_ext}")
        g.add_edge(src, t, relation="imports", source_file=f"a{lang_ext}")
    for i in range(unbound_n):
        t = f"phantomimport{i}{lang_ext.replace('.', '_')}"
        g.add_node(t, kind="unknown")
        g.add_edge(src, t, relation="imports", source_file=f"a{lang_ext}")
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


def test_an_empty_calls_answer_is_never_a_trustworthy_absence():
    """`decision_grade` promises "an empty result is a trustworthy absence",
    and for `calls` the metric behind it cannot support that promise.

    `resolution_health` measures how many DETECTED call sites bound to a
    target. A function passed as a VALUE to a registrar -- `threading.Timer`,
    `atexit.register`, `Thread(target=)` -- produces no call site at all, so
    it never enters the denominator and cannot lower the ratio.

    Measured on a purpose-built repo (aramid, round 55; reproduced here): a
    file with two ordinary calls plus two callback registrations scored
    `ratio 1.0, total 2, bound 2`, and `callers` on each registered function
    returned 0 at `decision_grade`. The grade was computed from a denominator
    that excluded exactly the failure it was being read to rule out.

    A PERFECT ratio is used deliberately below -- that is the trap. No amount
    of health can license this absence.
    """
    g = _graph_ratio(".py", 10, 0)
    block = build_answer_block(g, relations=("calls",), languages=["python"], total=0)

    assert block["health"]["calls"]["python"]["ratio"] == 1.0
    assert block["health"]["calls"]["python"]["healthy"] is True
    assert block["grade"] == GRADE_ADVISORY


def test_a_nonempty_calls_answer_still_grades_decision():
    """No over-correction. The callers you DID find are real; only the
    absence claim is unsupportable, so a non-empty answer is untouched."""
    g = _graph_ratio(".py", 10, 0)
    block = build_answer_block(g, relations=("calls",), languages=["python"], total=4)

    assert block["grade"] == GRADE_DECISION


def test_the_callback_caveat_discriminates_where_the_blanket_one_cannot():
    """aramid measured `python-dynamic-dispatch` as byte-identical across six
    `callers` queries, five of them with non-zero counts, so it carries no
    signal about any particular answer. Confirmed structurally: caveat
    selection reads only relations x languages and never the result.

    A caveat that hedges an empty answer therefore has to be ABSENT when the
    answer is non-empty. An always-on caveat trains readers to ignore
    caveats, which is what made this finding cost anything at all.
    """
    g = _graph_ratio(".py", 10, 0)
    empty = build_answer_block(g, relations=("calls",), languages=["python"], total=0)
    found = build_answer_block(g, relations=("calls",), languages=["python"], total=3)

    codes_empty = {c["code"] for c in empty["caveats"]}
    codes_found = {c["code"] for c in found["caveats"]}

    assert "python-callback-registration" in codes_empty
    assert "python-callback-registration" not in codes_found
    # The blanket caveat stays on both on purpose: it is a SCOPE disclosure,
    # not a hedge on the result, and dropping it would lose that disclosure.
    assert "python-dynamic-dispatch" in codes_empty & codes_found


def test_scoped_cells_ignore_other_languages():
    """The firescraper regression: healthy python must not mask degraded ts."""
    g = _merged(_graph_ratio(".py", 9, 1), _graph_ratio(".ts", 1, 9))
    block = build_answer_block(g, relations=("calls",), languages=["typescript"], total=0)
    assert block["grade"] == GRADE_INCONCLUSIVE
    assert "python" not in block["health"]["calls"]


def test_language_fallback_is_graph_wide_only_for_an_explicit_none():
    """languages=None ("no filter") still grades against every language --
    but this is now reached only by an explicit None, never by a caller's
    computed-and-empty list. No production call site passes None today
    (every real caller derives its filter via languages_for_nodes), so this
    exercises the escape hatch directly rather than through a real path."""
    g = _merged(_graph_ratio(".py", 9, 1), _graph_ratio(".ts", 1, 9))
    block = build_answer_block(g, relations=("calls",), languages=None, total=1)
    assert set(block["languages"]) == {"python", "typescript"}
    assert block["grade"] == GRADE_ADVISORY  # ts cell degraded


def test_empty_languages_means_not_applicable_not_graph_wide():
    """languages=[] means the caller computed the matched nodes' languages
    and found none apply (e.g. the match is a markdown/config file, not
    code) -- distinct from None. It must return None (nothing to grade),
    never silently borrow an unrelated language's degraded health.

    Regression for operation-firewall dogfooding, 2026-07-31: a `README.md`
    query matched a non-code file, languages_for_nodes returned [], and the
    old `if languages else sorted(by_language)` fallback graded the query
    against the whole graph's python/rust health -- an unrelated,
    unmeasured file came back "inconclusive" and polluted the incident
    ledger."""
    g = _merged(_graph_ratio(".py", 9, 1), _graph_ratio(".ts", 1, 9))
    block = build_answer_block(g, relations=("calls",), languages=[], total=1)
    assert block is None


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
    """Exact-set on purpose: declaring a blind spot must be a deliberate act.

    A superset assertion would let an entry be added silently, and the whole
    contract rests on the registry being the complete list of what graphite
    admits it cannot see.
    """
    codes = {e["code"] for e in active_caveats()}
    assert codes == {
        "python-dynamic-dispatch",
        "python-callback-registration",
        # #49 retired all three JavaScript entries on re-measured evidence --
        # the two declared that morning plus `ts-destructured-locals-unbound`
        # from 2026-07-27 -- and left two narrower ones behind: the dynamic
        # forms that genuinely still emit nothing, and the shadowing case the
        # fix deliberately fails closed on.
        "js-dynamic-module-load-unmodelled",
        "js-shadowed-module-local-unbound",
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

    """Each retirement in this chain is sound only because the RESIDUE stayed
    declared.

    `ts-external-calls-unclassified` narrowed to `ts-destructured-locals-unbound`
    (#4). #49 then fixed the destructuring and module-object shapes outright and
    made `require()` emit a real import edge -- but the non-detection class is
    narrowed, not gone: a dynamic `require(expr)` or an `import()` expression
    still emits nothing, measured the same day. That residue is
    `js-dynamic-module-load-unmodelled`, and it is why `imports` stays in
    NON_DETECTION_RELATIONS for these languages.

    Retiring a code whose concern is MOSTLY gone, with nothing carrying the
    remainder, is exactly the close this project's two-answer rule exists to
    prevent.
    """
    by_code = {e["code"]: e for e in CAVEAT_REGISTRY}
    active = {e["code"] for e in active_caveats()}

    for retired in (
        "ts-external-calls-unclassified",
        "ts-destructured-locals-unbound",
        "js-require-emits-no-import-edge",
        "js-module-object-calls-unbound",
    ):
        assert by_code[retired]["retired_by"], retired
        assert retired not in active, retired

    # Published shape survives retirement -- a consumer that recorded this code
    # keeps the meaning it was published with.
    superseded = by_code["ts-destructured-locals-unbound"]
    assert superseded["relations"] == ("calls",)
    assert superseded["languages"] == ("typescript", "javascript")

    residue = by_code["js-dynamic-module-load-unmodelled"]
    assert "js-dynamic-module-load-unmodelled" in active
    assert residue["relations"] == ("calls", "imports")

    # `js-module-object-calls-unbound` did not retire cleanly either: a name
    # rebound in the same file is still unbound, on purpose. Retiring it with
    # nothing carrying that subset would be the same tidy-registry mistake one
    # entry over.
    shadowed = by_code["js-shadowed-module-local-unbound"]
    assert "js-shadowed-module-local-unbound" in active
    assert shadowed["relations"] == ("calls",)


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


def test_empty_marker_echoes_an_unverifiable_absence():
    """A grade the printed line does not echo is a hedge the reader never sees
    -- which is precisely how round 55's decision-grade zero got believed.

    The advisory-because-undetectable listing must say so, and must NOT borrow
    the INCONCLUSIVE wording: that means something different (degraded or
    unmeasured cells), and collapsing the two would report a good measurement
    as a failed one.
    """
    from graphite.answer_contract import INCONCLUSIVE_EMPTY, UNVERIFIED_EMPTY, empty_marker

    g = _graph_ratio(".py", 10, 0)
    healthy_empty = build_answer_block(g, relations=("calls",), languages=["python"], total=0)

    assert healthy_empty["grade"] == GRADE_ADVISORY
    assert empty_marker(healthy_empty) == UNVERIFIED_EMPTY
    assert empty_marker(healthy_empty) != INCONCLUSIVE_EMPTY
    assert "none found" in UNVERIFIED_EMPTY, "the listing still has to read as a listing"


def test_an_empty_javascript_import_answer_is_not_a_trustworthy_absence():
    """`require()` is a real import that emits no candidate edge. MEASURED.

    The registry's own admission rule for `NON_DETECTION_RELATIONS` was "add a
    relation only with a measured non-detection case, not on suspicion", and it
    recorded `imports` as exempt because "an import is a syntactic construct
    that extraction either sees or does not". CommonJS breaks that premise:
    `require('./mod')` is a call expression, so the import extractor never sees
    it, no candidate edge is emitted, and the site never reaches `total`.

    Measured on a two-file fixture -- `consumer.js` requires `./mod` TWICE and
    the graph contains exactly one import edge, the unrelated ESM one. The
    imports cell read `total 1, bound 1, ratio 1.0`, i.e. perfectly healthy,
    while `imported-by src/mod.js` returned nothing at decision_grade. That is
    the round-55 defect exactly, in the relation that was excused from it: a
    RESOLUTION metric cannot underwrite a COVERAGE claim.
    """
    g = _graph_imports(".js", 10, 0)

    block = build_answer_block(g, relations=("imports",), languages=["javascript"], total=0)

    assert block["health"]["imports"]["javascript"]["healthy"] is True, (
        "the fixture must be genuinely healthy, or this proves nothing about "
        "an absence that a GOOD ratio was underwriting"
    )
    assert block["grade"] == GRADE_ADVISORY


def test_an_empty_rust_import_answer_stays_a_trustworthy_absence():
    """Falsifiability control: the fix must be SCOPED, not a blanket downgrade.

    Adding `imports` to the non-detection set unconditionally would satisfy the
    test above while making every "nothing imports this file" answer unverified
    in languages that have no such construct. Rust `use` is syntactic and has
    no dynamic form graphite models, so its absence is still evidence.
    """
    g = _graph_imports(".rs", 10, 0)

    block = build_answer_block(g, relations=("imports",), languages=["rust"], total=0)

    assert block["health"]["imports"]["rust"]["healthy"] is True
    assert block["grade"] == GRADE_DECISION


def test_measured_healthy_answer_is_not_dragged_to_inconclusive():
    """#12's over-firing guard, preserved through the round-55 change.

    This originally asserted `decision_grade` on an EMPTY calls answer. Round
    55 showed that exact claim is unsound: the ratio measures resolution of
    DETECTED call sites and is blind to invocations that emit no site, so a
    perfect ratio cannot license "nothing calls this". An empty calls answer is
    now `advisory`.

    The guard this test exists for is untouched -- a block with real cells must
    never be graded like an unmeasured one -- so it now asserts that directly,
    plus the other half of "not over-firing": decision_grade is still reachable.
    """
    g = _graph_ratio(".py", 9, 1)
    block = build_answer_block(g, relations=("calls",), languages=["python"], total=0)

    assert block["health"], "fixture must produce cells"
    assert block["grade"] != GRADE_INCONCLUSIVE
    assert block["grade"] == GRADE_ADVISORY

    nonempty = build_answer_block(g, relations=("calls",), languages=["python"], total=2)
    assert nonempty["grade"] == GRADE_DECISION


def test_unattributable_receiver_caveat_is_retired():
    """#14 fixed mechanism A; the published code keeps its original meaning."""
    from graphite.answer_contract import CAVEAT_REGISTRY, active_caveats

    by_code = {e["code"]: e for e in CAVEAT_REGISTRY}
    entry = by_code["calls-unattributable-receiver-false-external"]
    assert entry["retired_by"], "must be retired, not deleted or reworded"
    assert "bare method name" in entry["summary"], "a published summary never changes"
    assert "calls-unattributable-receiver-false-external" not in {e["code"] for e in active_caveats()}


# --- the grade has to REACH the caller ---------------------------------------
#
# Everything above tests `build_answer_block` directly. That proves the grading
# is right and says nothing about whether a consumer ever sees it -- and a
# correct producer whose output never reaches the reader is the defect shape this
# repo keeps finding: `getattr(exc, "cause")` on a field nothing sets,
# `metadata.version` on a name nothing installed. Both looked fine and reported
# nothing.
#
# `query()` is what every agent and every MCP call actually goes through, so the
# wiring is what the contract rests on.


def test_a_degraded_graph_reaches_the_caller_as_a_degraded_grade():
    from graphite.query import query

    # 1 bound target against 9 unbound: a calls ratio of 0.1, far under the 0.8
    # line, so any answer scoped to python/calls is degraded by construction.
    degraded = _graph_ratio(".py", bound_n=1, unbound_n=9)

    result = query(degraded, "callers bound0")

    assert result["callers"], "fixture must produce a NON-empty answer to grade"
    assert result["answer"]["grade"] == GRADE_ADVISORY, (
        "a degraded graph graded decision_grade through the real query path -- "
        "the contract is only worth as much as its delivery"
    )


def test_a_healthy_graph_reaches_the_caller_as_decision_grade():
    """Firing control. Without it the test above passes against a `query` that
    hardcodes `advisory`, or one that degrades every answer indiscriminately --
    both of which deliver a grade that discriminates nothing."""
    from graphite.query import query

    healthy = _graph_ratio(".py", bound_n=10, unbound_n=0)

    result = query(healthy, "callers bound0")

    assert result["callers"]
    assert result["answer"]["grade"] == GRADE_DECISION
