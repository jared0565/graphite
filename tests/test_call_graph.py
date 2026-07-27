"""Function-level call attribution, cross-file target resolution, and call-graph query verbs.

Covers the two extraction defects (calls attributed to the file instead of the
enclosing function; cross-file calls resolving to a same-file phantom) plus the
new ``callers`` / ``calls`` / ``reaches`` query verbs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.config import Config
from graphite.extract.ast import _safe_label, extract_all
from graphite.graph import build_graph
from graphite.ingest import collect_files
from graphite.query import query


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ts_fixture(tmp_path: Path) -> None:
    """A two-file TS project exercising all three call shapes."""
    _write(
        tmp_path / "src" / "mod.ts",
        "export function target() { return 1; }\n"
        "export function other() { return 2; }\n",
    )
    _write(
        tmp_path / "src" / "consumer.ts",
        # cross-file named import of the definitions in mod.ts
        "import { target, other } from './mod';\n"
        "\n"
        # (c) two distinct functions calling the same cross-file target
        "export function alpha() { return target(); }\n"
        "export function beta() { return target(); }\n"
        # a different callee, plus a same-file caller for reaches()
        "export function gamma() { return other(); }\n"
        "export function delta() { return alpha(); }\n"
        "\n"
        # (b) a call inside an arrow function nested in an object in an array literal
        "export const handlers = [\n"
        "  { name: 'h1', run: () => { target(); } },\n"
        "];\n",
    )


def _extract(tmp_path: Path, resolver: str):
    cfg = Config(
        workers=1,
        cache_dir=tmp_path / ".cache" / "graphite",
        typescript_resolver=resolver,
    )
    entries = collect_files(tmp_path, cfg)
    return extract_all(entries, cfg)


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_calls_are_function_scoped_and_cross_file_resolved(tmp_path: Path, resolver: str) -> None:
    _ts_fixture(tmp_path)
    result = _extract(tmp_path, resolver)
    calls = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}

    # (a) cross-file: the call resolves to the *definition* node in mod.ts,
    # not a phantom in the calling file's namespace.
    assert ("src_consumer_alpha", "src_mod_target") in calls
    assert not any(tgt == "src_consumer_target" for _src, tgt in calls), (
        "cross-file call must not resolve to a same-file phantom"
    )

    # (c) a second, distinct function calling the same target survives dedup.
    assert ("src_consumer_beta", "src_mod_target") in calls

    # The source is the enclosing function, never the file node.
    assert not any(src == "src_consumer" for src, tgt in calls if tgt == "src_mod_target")

    # (b) the arrow function nested in the array/object literal is its own caller.
    arrow_callers = [src for src, tgt in calls if tgt == "src_mod_target" and src.startswith("src_consumer_run")]
    assert len(arrow_callers) == 1, f"expected one arrow caller, got {arrow_callers}"


def test_arrow_caller_is_a_materialized_function_node(tmp_path: Path) -> None:
    _ts_fixture(tmp_path)
    result = _extract(tmp_path, "disabled")
    nodes = {n["id"]: n for n in result.nodes}

    calls = [e for e in result.edges if e["relation"] == "calls"]
    arrow_src = next(e["source"] for e in calls if e["target"] == "src_mod_target" and e["source"].startswith("src_consumer_run"))

    # The anonymous arrow must have been materialized as a real function node
    # (not left dangling for build_graph to invent as kind="unknown").
    assert arrow_src in nodes
    assert nodes[arrow_src]["kind"] == "function"
    assert nodes[arrow_src]["source_file"] == "src/consumer.ts"


def _golden_graph():
    nodes = [
        {"id": "src_app", "kind": "file", "name": "app.ts", "source_file": "src/app.ts"},
        {"id": "src_app_main", "kind": "function", "name": "main", "source_file": "src/app.ts"},
        {"id": "src_lib", "kind": "file", "name": "lib.ts", "source_file": "src/lib.ts"},
        {"id": "src_lib_helper", "kind": "function", "name": "helper", "source_file": "src/lib.ts"},
    ]
    edges = [
        {"source": "src_app", "target": "src_lib", "relation": "imports"},
        {"source": "src_app_main", "target": "src_lib_helper", "relation": "calls"},
    ]
    return build_graph(nodes, edges)


def test_query_verb_outputs_are_golden_stable() -> None:
    """Exact-output pins so interface refactors are provably inert."""
    g = _golden_graph()
    # Pinned verbatim from an actual resolution_health(g) run over the fixture
    # above (schema-3 shape) — independent of the function under test.
    health_block = {
        "schema": 3,
        "placeholder_nodes": {"total": 4, "unknown": 0, "share": 0.0},
        "by_relation": {
            "calls": {"total": 1, "bound": 1, "ratio": 1.0, "external": 0},
            "imports": {"total": 1, "bound": 1, "ratio": 1.0, "external": 0},
        },
        "by_language": {
            "other": {
                "calls": {"total": 1, "bound": 1, "ratio": 1.0, "external": 0},
                "imports": {"total": 1, "bound": 1, "ratio": 1.0, "external": 0},
            }
        },
        "healthy": True,
        "threshold": 0.8,
    }
    # Pinned verbatim from actual `answer` output (spec 2026-07-26): the golden
    # fixture's edges carry no source_file, so resolution_health's by_language
    # bucket is keyed "other" and the node-seeded "typescript" language never
    # has a matching health cell -> health is empty but the language-scoped
    # caveat still fires (relations/languages match on the seed's language,
    # independent of whether a health cell exists for it).
    ts_caveat = {
        "code": "ts-external-calls-unclassified",
        "summary": "calls to external-package symbols, runtime globals, and destructured locals count as unbound",
    }
    answer_calls_only = {
        "schema": 1,
        "relations": ["calls"],
        "languages": ["typescript"],
        "health": {},
        "grade": "decision_grade",
        "caveats": [ts_caveat],
    }
    answer_calls_and_imports = {
        "schema": 1,
        "relations": ["calls", "imports"],
        "languages": ["typescript"],
        "health": {},
        "grade": "decision_grade",
        "caveats": [ts_caveat],
    }
    answer_imports_only = {
        "schema": 1,
        "relations": ["imports"],
        "languages": ["typescript"],
        "health": {},
        "grade": "decision_grade",
        "caveats": [],
    }

    assert query(g, "callers helper") == {
        "schema_version": 1,
        "node": "src_lib_helper",
        "match": {"input": "helper", "node": "src_lib_helper", "type": "name"},
        "count": 1,
        "total": 1,
        "truncated": False,
        "limits": {"max_results": 200},
        "callers": [
            {"id": "src_app_main", "name": "main", "kind": "function", "source_file": "src/app.ts"}
        ],
        "resolution": [
            {"role": "node", "input": "helper", "node": "src_lib_helper", "type": "name"}
        ],
        "resolution_health": health_block,
        "inconclusive": False,
        "answer": answer_calls_only,
    }
    assert query(g, "calls main") == {
        "schema_version": 1,
        "node": "src_app_main",
        "match": {"input": "main", "node": "src_app_main", "type": "name"},
        "count": 1,
        "total": 1,
        "truncated": False,
        "limits": {"max_results": 200},
        "calls": [
            {"id": "src_lib_helper", "name": "helper", "kind": "function", "source_file": "src/lib.ts"}
        ],
        "resolution": [
            {"role": "node", "input": "main", "node": "src_app_main", "type": "name"}
        ],
        "resolution_health": health_block,
        "inconclusive": False,
        "answer": answer_calls_only,
    }
    assert query(g, "depends-on src_app") == {
        "schema_version": 1,
        "node": "src_app",
        "match": {"input": "src_app", "node": "src_app", "type": "exact-id"},
        "count": 1,
        "total": 1,
        "truncated": False,
        "limits": {"max_results": 200},
        "depends_on": [
            {"id": "src_lib", "name": "lib.ts", "kind": "file", "source_file": "src/lib.ts"}
        ],
        "resolution": [
            {"role": "node", "input": "src_app", "node": "src_app", "type": "exact-id"}
        ],
        "resolution_health": health_block,
        "inconclusive": False,
        "answer": answer_calls_and_imports,
    }
    assert query(g, "imported-by src_lib") == {
        "schema_version": 1,
        "node": "src_lib",
        "match": {"input": "src_lib", "node": "src_lib", "type": "exact-id"},
        "count": 1,
        "total": 1,
        "truncated": False,
        "limits": {"max_results": 200},
        "imported_by": [
            {"id": "src_app", "name": "app.ts", "kind": "file", "source_file": "src/app.ts"}
        ],
        "resolution": [
            {"role": "node", "input": "src_lib", "node": "src_lib", "type": "exact-id"}
        ],
        "resolution_health": health_block,
        "inconclusive": False,
        "answer": answer_imports_only,
    }
    assert query(g, "reaches main -> helper") == {
        "schema_version": 1,
        "source": "src_app_main",
        "target": "src_lib_helper",
        "match": {
            "source": {"input": "main", "node": "src_app_main", "type": "name"},
            "target": {"input": "helper", "node": "src_lib_helper", "type": "name"},
        },
        "length": 1,
        "path": [
            {"id": "src_app_main", "name": "main", "kind": "function", "source_file": "src/app.ts"},
            {"id": "src_lib_helper", "name": "helper", "kind": "function", "source_file": "src/lib.ts"},
        ],
        "truncated": False,
        "limits": {"max_depth": 32},
        "resolution": [
            {"role": "source", "input": "main", "node": "src_app_main", "type": "name"},
            {"role": "target", "input": "helper", "node": "src_lib_helper", "type": "name"},
        ],
        "answer": answer_calls_only,
    }
    assert query(g, "path src_app -> src_lib") == {
        "schema_version": 1,
        "source": "src_app",
        "target": "src_lib",
        "match": {
            "source": {"input": "src_app", "node": "src_app", "type": "exact-id"},
            "target": {"input": "src_lib", "node": "src_lib", "type": "exact-id"},
        },
        "length": 1,
        "path": [
            {"id": "src_app", "name": "app.ts", "kind": "file"},
            {"id": "src_lib", "name": "lib.ts", "kind": "file"},
        ],
        "truncated": False,
        "limits": {"max_depth": 32},
        "resolution": [
            {"role": "source", "input": "src_app", "node": "src_app", "type": "exact-id"},
            {"role": "target", "input": "src_lib", "node": "src_lib", "type": "exact-id"},
        ],
        "answer": answer_calls_and_imports,
    }
    assert query(g, "community-of src_app") == {
        "schema_version": 1,
        "node": "src_app",
        "match": {"input": "src_app", "node": "src_app", "type": "exact-id"},
        "community": None,
        "name": "app.ts",
        "resolution": [
            {"role": "node", "input": "src_app", "node": "src_app", "type": "exact-id"}
        ],
        "answer": {**answer_calls_and_imports, "empty_meaning": "no community assigned"},
    }

    stats = query(g, "stats")
    density = stats.pop("density")
    assert density == pytest.approx(2 / 12)
    assert stats == {
        "schema_version": 1,
        "resolution": [],
        "resolution_health": health_block,
        "node_count": 4,
        "edge_count": 2,
        "community_count": 0,
        "nodes_by_kind": {"file": 2, "function": 2},
        "edges_by_relation": {"imports": 1, "calls": 1},
        "top_incoming": [
            {"id": "src_lib", "name": "lib.ts", "kind": "file", "source_file": "src/lib.ts", "in_degree": 1},
            {"id": "src_lib_helper", "name": "helper", "kind": "function", "source_file": "src/lib.ts", "in_degree": 1},
            {"id": "src_app", "name": "app.ts", "kind": "file", "source_file": "src/app.ts", "in_degree": 0},
            {"id": "src_app_main", "name": "main", "kind": "function", "source_file": "src/app.ts", "in_degree": 0},
        ],
        "top_outgoing": [
            {"id": "src_app", "name": "app.ts", "kind": "file", "source_file": "src/app.ts", "out_degree": 1},
            {"id": "src_app_main", "name": "main", "kind": "function", "source_file": "src/app.ts", "out_degree": 1},
            {"id": "src_lib", "name": "lib.ts", "kind": "file", "source_file": "src/lib.ts", "out_degree": 0},
            {"id": "src_lib_helper", "name": "helper", "kind": "function", "source_file": "src/lib.ts", "out_degree": 0},
        ],
    }


def test_query_error_outputs_are_golden_stable() -> None:
    g = _golden_graph()

    assert query(g, "") == {
        "schema_version": 1,
        "error": "empty query",
        "error_code": "empty_query",
    }

    unknown = query(g, "how does pairing work")
    assert unknown["error"] == "unknown query verb: how"
    assert unknown["error_code"] == "unknown_query_verb"
    assert any("graphite search" in s for s in unknown["suggestions"])
    assert any("graphite capabilities" in s for s in unknown["suggestions"])
    assert any("callers" in s for s in unknown["suggestions"])

    assert query(g, "callers zzqx") == {
        "schema_version": 1,
        "error": "node not found: zzqx",
        "error_code": "node_not_found",
        "candidates": [],
    }
    assert query(g, "path src_app") == {
        "schema_version": 1,
        "error": "path query format: path <a> -> <b>",
        "error_code": "invalid_query_format",
    }
    assert query(g, "reaches src_app") == {
        "schema_version": 1,
        "error": "reaches query format: reaches <a> -> <b>",
        "error_code": "invalid_query_format",
    }
    assert query(g, "reaches src_lib -> src_app") == {
        "schema_version": 1,
        "error": "no call path from src_lib to src_app",
        "error_code": "no_path",
        "truncated": False,
        "limits": {"max_depth": 32},
        # no_path carries no resolved seeds, so languages_for_nodes([]) falls
        # back to every language in resolution_health's by_language ("other",
        # since this fixture's edges carry no source_file) -> health IS
        # populated here, unlike the node-seeded verbs above, and no
        # typescript-only caveat fires.
        "answer": {
            "schema": 1,
            "relations": ["calls"],
            "languages": ["other"],
            "health": {
                "calls": {"other": {"total": 1, "bound": 1, "ratio": 1.0, "external": 0, "healthy": True}}
            },
            "grade": "decision_grade",
            "caveats": [],
            "empty_meaning": "no call path found within depth",
        },
    }


def test_call_graph_query_verbs(tmp_path: Path) -> None:
    _ts_fixture(tmp_path)
    result = _extract(tmp_path, "auto")
    g = build_graph(result.nodes, result.edges)

    # callers <symbol> — function-level predecessors, file node excluded.
    callers = query(g, "callers target")
    ids = {c["id"] for c in callers["callers"]}
    assert "src_consumer_alpha" in ids
    assert "src_consumer_beta" in ids
    assert any(i.startswith("src_consumer_run") for i in ids)
    assert "src_consumer" not in ids  # the file must NOT be a caller
    assert len(callers["callers"]) == 3
    for c in callers["callers"]:
        assert c["kind"] == "function"
        assert c["source_file"]  # id/name/kind/source_file all present

    # calls <symbol> (and its callees alias) — function-level successors.
    callees = query(g, "calls alpha")
    assert any(c["id"] == "src_mod_target" for c in callees["calls"])
    assert query(g, "callees alpha")["calls"] == callees["calls"]

    # reaches <a> -> <b> — directed path restricted to call/reference edges.
    reach = query(g, "reaches delta -> target")
    path_ids = [p["id"] for p in reach["path"]]
    assert path_ids[0] == "src_consumer_delta"
    assert path_ids[-1] == "src_mod_target"
    assert "src_consumer_alpha" in path_ids


def test_reaches_reports_no_path_when_unreachable(tmp_path: Path) -> None:
    _ts_fixture(tmp_path)
    result = _extract(tmp_path, "disabled")
    g = build_graph(result.nodes, result.edges)
    # target does not call gamma, so no call path exists.
    res = query(g, "reaches target -> gamma")
    assert "error" in res


def test_existing_query_verbs_unchanged(tmp_path: Path) -> None:
    _ts_fixture(tmp_path)
    result = _extract(tmp_path, "disabled")
    g = build_graph(result.nodes, result.edges)

    stats = query(g, "stats")
    assert stats["node_count"] > 0 and "edge_count" in stats
    # consumer.ts imports mod.ts -> depends-on/imported-by still work at file level.
    dep = query(g, "depends-on src/consumer.ts")
    assert "depends_on" in dep
    assert "error" not in query(g, "imported-by src/mod.ts")


def test_stats_includes_breakdowns_and_top_degree_nodes(tmp_path: Path) -> None:
    _ts_fixture(tmp_path)
    result = _extract(tmp_path, "disabled")
    g = build_graph(result.nodes, result.edges)

    stats = query(g, "stats")
    assert stats["nodes_by_kind"].get("function", 0) >= 5
    assert stats["nodes_by_kind"].get("file", 0) == 2
    assert stats["edges_by_relation"].get("calls", 0) >= 4
    assert 1 <= len(stats["top_incoming"]) <= 5
    assert 1 <= len(stats["top_outgoing"]) <= 5
    # target is the most-called symbol in the fixture.
    top_ids = {n["id"] for n in stats["top_incoming"]}
    assert "src_mod_target" in top_ids
    for entry in stats["top_incoming"]:
        assert {"id", "name", "kind", "source_file", "in_degree"} <= set(entry)
    assert "community_count" in stats


def test_query_reports_match_type_and_alternates(tmp_path: Path) -> None:
    """How a token matched must be visible, so fuzzy/ambiguous picks aren't silent."""
    _write(tmp_path / "src" / "a.ts", "export function dupe() { return 1; }\n")
    _write(tmp_path / "src" / "b.ts", "export function dupe() { return 2; }\n")
    result = _extract(tmp_path, "disabled")
    g = build_graph(result.nodes, result.edges)

    exact = query(g, "calls src_a_dupe")
    assert exact["match"] == {"input": "src_a_dupe", "node": "src_a_dupe", "type": "exact-id"}

    by_name = query(g, "calls dupe")
    assert by_name["match"]["type"] == "name"
    assert by_name["match"]["alternates"], "second same-named fn must be reported"
    assert {by_name["node"], *by_name["match"]["alternates"]} == {"src_a_dupe", "src_b_dupe"}

    by_path = query(g, "depends-on src/a.ts")
    assert by_path["match"]["type"] == "path-suffix"
    assert by_path["node"] == "src_a"


def test_not_found_error_suggests_close_candidates(tmp_path: Path) -> None:
    """Agents pass slightly-wrong node refs; the error must offer corrections."""
    _ts_fixture(tmp_path)
    result = _extract(tmp_path, "disabled")
    g = build_graph(result.nodes, result.edges)

    # `alpha()` (with call parens) matches no node id/name/path exactly.
    res = query(g, "callers src/consumer.ts:nosuchfn alpha()")
    assert "error" in res
    candidate_ids = {c["id"] for c in res["candidates"]}
    assert "src_consumer_alpha" in candidate_ids

    # path variant reports candidates for the missing endpoint too.
    res = query(g, "path src/consumer.ts -> mod.zz")
    assert "error" in res
    assert isinstance(res.get("candidates"), list)

    # Complete gibberish yields an empty (but present) candidate list.
    res = query(g, "callers zzz_qqq_www")
    assert "error" in res
    assert res["candidates"] == []


def test_safe_label_strips_non_ascii_and_collapses_whitespace() -> None:
    # Synthetic names come from arbitrary source strings; the node label must
    # stay console-safe (this is what broke `query` printing on cp1252 Windows).
    assert _safe_label("a ↔ b") == "a b"
    assert _safe_label("multi\n  line\tname") == "multi line name"
    assert _safe_label("\U0001f600\U0001f600") == "anon"
    assert _safe_label(None) == "anon"
    assert all(ord(c) < 128 for c in _safe_label("route → \U0001f680 handler"))


def test_synthetic_callback_name_from_unicode_string_arg_is_ascii(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "svc.ts",
        "export function target() { return 1; }\n",
    )
    _write(
        tmp_path / "src" / "app.ts",
        "import { target } from './svc';\n"
        "route('sync ↔ store', () => { target(); });\n",
    )
    result = _extract(tmp_path, "disabled")
    calls = [e for e in result.edges if e["relation"] == "calls" and e["target"] == "src_svc_target"]
    assert calls, "callback should call the cross-file target"
    nodes = {n["id"]: n for n in result.nodes}
    for e in calls:
        name = nodes[e["source"]]["name"]
        assert all(ord(c) < 128 for c in name), f"label not console-safe: {name!r}"


def _py_fixture(tmp_path: Path) -> None:
    _write(
        tmp_path / "mod.py",
        "def helper():\n"
        "    return 1\n"
        "\n"
        "def caller_one():\n"
        "    return helper()\n"
        "\n"
        "def caller_two():\n"
        "    return helper()\n",
    )


def test_python_calls_are_function_scoped(tmp_path: Path) -> None:
    # Also guards against the latent NameError in the Python extraction path.
    _py_fixture(tmp_path)
    result = _extract(tmp_path, "disabled")
    calls = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}

    assert ("mod_caller_one", "mod_helper") in calls
    assert ("mod_caller_two", "mod_helper") in calls
    # attributed to the enclosing function, never the file node.
    assert not any(src == "mod" for src, tgt in calls if tgt == "mod_helper")

    g = build_graph(result.nodes, result.edges)
    callers = query(g, "callers helper")
    ids = {c["id"] for c in callers["callers"]}
    assert ids == {"mod_caller_one", "mod_caller_two"}


# --- method dispatch: `recv.method()` resolves to the class method definition ---


def _method_fixture(tmp_path: Path) -> None:
    """A store class in one file; a consumer that dispatches on an instance of it.

    Mirrors the real shape (profit-console.ts calling store.listProfitVerdicts):
    the receiver's type arrives via `import type`, and the call is a member
    expression on that instance.
    """
    _write(
        tmp_path / "src" / "store.ts",
        "export class Store {\n"
        "  async listVerdicts(limit = 500) { return []; }\n"
        "  async putThing(x: number) { return x; }\n"
        "}\n",
    )
    _write(
        tmp_path / "src" / "consumer.ts",
        "import type { Store } from './store';\n"
        "export async function loadConsole(store: Store) {\n"
        "  const rows = await store.listVerdicts(500);\n"
        "  return rows.length;\n"
        "}\n",
    )


def test_member_call_links_to_class_method_cross_file(tmp_path: Path) -> None:
    _method_fixture(tmp_path)
    result = _extract(tmp_path, "disabled")
    calls = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}

    # The member call on the instance links to the *definition* in store.ts.
    assert ("src_consumer_loadconsole", "src_store_listverdicts") in calls
    # ...and not to a leftover file-namespaced phantom in the calling file.
    assert not any(tgt == "src_consumer_store_listverdicts" for _s, tgt in calls)

    # The private dispatch annotation must never leak into the graph.
    assert all("_member" not in e for e in result.edges)

    # The method definition node is tagged so the post-pass can find it.
    nodes = {n["id"]: n for n in result.nodes}
    assert nodes["src_store_listverdicts"].get("is_method") is True

    g = build_graph(result.nodes, result.edges)
    # callers <method> now includes the cross-file dispatch caller.
    callers = query(g, "callers listVerdicts")
    assert "src_consumer_loadconsole" in {c["id"] for c in callers["callers"]}
    # calls <caller> lists the resolved method as a callee.
    callees = query(g, "calls loadConsole")
    assert any(c["id"] == "src_store_listverdicts" for c in callees["calls"])


def test_member_call_ambiguous_small_set_links_to_all(tmp_path: Path) -> None:
    # Two classes define `refresh`; the receiver type is unknown (`any`). The
    # documented policy for a small ambiguous set is to link to every candidate.
    _write(tmp_path / "src" / "a.ts", "export class A {\n  async refresh() { return 1; }\n}\n")
    _write(tmp_path / "src" / "b.ts", "export class B {\n  async refresh() { return 2; }\n}\n")
    _write(tmp_path / "src" / "use.ts", "export function run(x: any) { return x.refresh(); }\n")

    result = _extract(tmp_path, "disabled")
    calls = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}
    assert ("src_use_run", "src_a_refresh") in calls
    assert ("src_use_run", "src_b_refresh") in calls

    g = build_graph(result.nodes, result.edges)
    caller_ids = {c["id"] for c in query(g, "callers refresh")["callers"]}
    assert "src_use_run" in caller_ids


def test_member_call_over_cap_is_left_unresolved(tmp_path: Path) -> None:
    # Four classes share the method name -> exceeds _MAX_METHOD_DISPATCH_CANDIDATES,
    # so the call is left as an unresolved phantom (linked to none of them).
    for i in range(4):
        _write(
            tmp_path / "src" / f"c{i}.ts",
            f"export class C{i} {{\n  async shared() {{ return {i}; }}\n}}\n",
        )
    _write(tmp_path / "src" / "use.ts", "export function run(x: any) { return x.shared(); }\n")

    result = _extract(tmp_path, "disabled")
    calls = {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}

    real_method_ids = {f"src_c{i}_shared" for i in range(4)}
    assert not any(src == "src_use_run" and tgt in real_method_ids for src, tgt in calls), (
        "over-cap dispatch must not link to any candidate"
    )
    # v4: the unresolved member-call phantom is dropped entirely (it points at
    # no real node and previously only polluted degree stats).
    assert ("src_use_run", "src_use_x_shared") not in calls
    assert not any(src == "src_use_run" for src, _tgt in calls)
    # And no annotation leaked.
    assert all("_member" not in e for e in result.edges)
