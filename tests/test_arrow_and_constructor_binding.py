"""Binding for arrow-assigned definitions (#16) and `new X()` construction (#15).

Both were silent absences rather than degraded signals. A call to an
arrow-assigned const could not resolve even inside the file that defined it,
because the arrow was registered under a line/column-disambiguated anonymous id
that `_resolve_call` can never produce. `new X()` emitted no `calls` edge at
all, because the walk looked for a `function` field that a `new_expression`
does not have.

The negative tests matter as much as the positive ones: naming arrows must not
regress the deliberate anonymity of callbacks and object-literal properties,
where the surrounding name is not something callers can invoke.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.ingest import collect_files


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(tmp_path: Path, resolver: str):
    cfg = Config(
        workers=1,
        cache_dir=tmp_path / ".cache" / "graphite",
        typescript_resolver=resolver,
    )
    entries = collect_files(tmp_path, cfg)
    return extract_all(entries, cfg)


def _calls(result) -> set[tuple[str, str]]:
    return {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}


def _node_ids(result) -> set[str]:
    return {n["id"] for n in result.nodes}


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_arrow_assigned_const_binds_within_one_file(tmp_path: Path, resolver: str) -> None:
    """#16's core case: same-file resolution works for `function`, must work here too."""
    _write(
        tmp_path / "src" / "theme.ts",
        "const getThing = () => 1;\n"
        "export function run() { return getThing(); }\n",
    )

    result = _extract(tmp_path, resolver)

    assert "src_theme_ts_getthing_3e9756" in _node_ids(result), "arrow-assigned const produced no bindable node"
    assert ("src_theme_ts_run", "src_theme_ts_getthing_3e9756") in _calls(result)


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_arrow_assigned_const_binds_across_files(tmp_path: Path, resolver: str) -> None:
    """The import machinery already keys on `_make_id(file, name)`; only the id shape was wrong."""
    _write(tmp_path / "src" / "util.ts", "export const helper = () => 1;\n")
    _write(
        tmp_path / "src" / "app.ts",
        "import { helper } from './util';\n"
        "export function run() { return helper(); }\n",
    )

    result = _extract(tmp_path, resolver)

    assert ("src_app_ts_run", "src_util_ts_helper") in _calls(result)
    # The edge alone proves nothing: the import machinery emits it whether or
    # not the definition node exists, and an edge to a non-existent node is a
    # phantom that health counts as UNBOUND. The node must actually be there.
    assert "src_util_ts_helper" in _node_ids(result), "edge points at a phantom, not a definition"
    assert not any(
        tgt == "src_app_ts_helper" for _src, tgt in _calls(result)
    ), "cross-file call must not resolve to a same-file phantom"


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_const_assigned_function_expression_also_binds(tmp_path: Path, resolver: str) -> None:
    """`const f = function () {}` is the same shape with a different keyword."""
    _write(
        tmp_path / "src" / "legacy.ts",
        "const buildThing = function () { return 1; };\n"
        "export function run() { return buildThing(); }\n",
    )

    result = _extract(tmp_path, resolver)

    assert ("src_legacy_ts_run", "src_legacy_ts_buildthing_a6ac14") in _calls(result)
    assert "src_legacy_ts_buildthing_a6ac14" in _node_ids(result), "edge points at a phantom, not a definition"


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_bare_parameter_arrow_is_not_named_after_its_parameter(tmp_path: Path, resolver: str) -> None:
    """The guard the original comment existed for: `x => x + 1` must not become `x`."""
    _write(
        tmp_path / "src" / "list.ts",
        "export function run(xs: number[]) { return xs.map(x => x + 1); }\n",
    )

    result = _extract(tmp_path, resolver)

    assert "src_list_ts_x" not in _node_ids(result)


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_object_property_arrow_is_not_a_bare_call_target(tmp_path: Path, resolver: str) -> None:
    """`{ toLabel: () => ... }` is not invocable as a bare `toLabel()`."""
    _write(
        tmp_path / "src" / "obj.ts",
        "export const api = { toLabel: () => 'x' };\n",
    )

    result = _extract(tmp_path, resolver)

    assert "src_obj_ts_tolabel_659c13" not in _node_ids(result)


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_new_expression_produces_a_calls_edge(tmp_path: Path, resolver: str) -> None:
    """#15: the `new_expression` arm was dead code -- wrong tree-sitter field name."""
    _write(
        tmp_path / "src" / "shapes.ts",
        "export class Widget { }\n"
        "export function buildThing() { return new Widget(); }\n",
    )

    result = _extract(tmp_path, resolver)

    assert ("src_shapes_ts_buildthing_3dcd5a", "src_shapes_ts_widget_0de36c") in _calls(result)


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_new_expression_binds_across_files(tmp_path: Path, resolver: str) -> None:
    """A `callers` query for a class must see its construction sites."""
    _write(tmp_path / "src" / "parser.ts", "export class Parser { }\n")
    _write(
        tmp_path / "src" / "main.ts",
        "import { Parser } from './parser';\n"
        "export function buildThing() { return new Parser(); }\n",
    )

    result = _extract(tmp_path, resolver)

    assert ("src_main_ts_buildthing_200fb6", "src_parser_ts_parser_bd59b9") in _calls(result)


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_new_member_expression_is_construction_not_dispatch(tmp_path: Path, resolver: str) -> None:
    """`new x.Foo()` must emit an edge but must not carry the method-dispatch stash."""
    _write(tmp_path / "src" / "lib.ts", "export class Parser { }\n")
    _write(
        tmp_path / "src" / "ns.ts",
        "import * as lib from './lib';\n"
        "export function buildThing() { return new lib.Parser(); }\n",
    )

    result = _extract(tmp_path, resolver)

    emitted = [
        e for e in result.edges
        if e["relation"] == "calls" and e["source"] == "src_ns_ts_buildthing_43d8e5"
    ]
    assert emitted, "construction should still produce an edge"
    assert all("_member" not in e for e in emitted), "construction must not be treated as dispatch"
