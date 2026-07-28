"""Class-field arrow functions must be bindable call targets (#19).

`handle = () => 1` inside a class parses as `public_field_definition`, not
`method_definition`, so it produced no definition node at all -- and the call
`this.handle()` produced no `calls` edge either, because the method-dispatch
post-pass only retains a phantom when the target is a known node.

This is the residual #16 scoped out: it fixed the variable-declarator form
(`const f = () => ...`) and deliberately left class fields, which are invoked
through the method-dispatch path rather than by bare name.

Like #15 and #16 this was a silent absence rather than a degraded signal: the
answer still grades `decision_grade`, so an agent is told an empty result is a
trustworthy absence when the relation never carried the data. Class fields
holding arrows are idiomatic in React and in any class passing bound handlers
around, precisely because the arrow captures `this`.
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


def _extract(tmp_path: Path, resolver: str = "auto"):
    cfg = Config(
        workers=1,
        cache_dir=tmp_path / ".cache" / "graphite",
        typescript_resolver=resolver,
    )
    return extract_all(collect_files(tmp_path, cfg), cfg)


def _calls(result) -> set[tuple[str, str]]:
    return {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}


def _node_ids(result) -> set[str]:
    return {n["id"] for n in result.nodes}


def _seed(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "w.ts",
        "export class Widget {\n"
        "  handle = () => 1;\n"
        "  run() { return this.handle(); }\n"
        "}\n"
        "export const plain = () => 2;\n",
    )


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_class_field_arrow_produces_a_bindable_node(tmp_path: Path, resolver: str) -> None:
    """The definition side: the field must exist as a call target at all."""
    _seed(tmp_path)

    result = _extract(tmp_path, resolver)
    ids = _node_ids(result)

    assert "src_w_plain" in ids, f"control failed -- #16 regressed: {sorted(ids)}"
    assert "src_w_handle" in ids, (
        f"class-field arrow produced no bindable node: {sorted(ids)}"
    )


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_this_dot_field_call_binds_to_the_field_definition(
    tmp_path: Path, resolver: str
) -> None:
    """The call side. #19 predicted fixing the definition alone is sufficient,
    because the dispatch post-pass then has a real node to re-point to. This is
    the measurement that predicted-sufficient claim has to survive."""
    _seed(tmp_path)

    result = _extract(tmp_path, resolver)

    assert ("src_w_run", "src_w_handle") in _calls(result), (
        f"this.handle() emitted no edge to the field definition: {sorted(_calls(result))}"
    )


def test_member_call_from_another_file_binds_to_the_field(tmp_path: Path) -> None:
    """`obj.handle()` across files is what the is_method dispatch index buys."""
    _write(
        tmp_path / "src" / "w.ts",
        "export class Widget {\n  handle = () => 1;\n}\n",
    )
    _write(
        tmp_path / "src" / "use.ts",
        "import { Widget } from './w';\n"
        "export function go() {\n"
        "  const w = new Widget();\n"
        "  return w.handle();\n"
        "}\n",
    )

    result = _extract(tmp_path)

    assert ("src_use_go", "src_w_handle") in _calls(result), (
        f"cross-file member call did not reach the field definition: {sorted(_calls(result))}"
    )


def test_object_literal_property_arrows_stay_anonymous(tmp_path: Path) -> None:
    """Guard: only CLASS fields gain a bindable name.

    An object-literal property is not something a caller can invoke by bare
    name, and #16 deliberately kept those anonymous. Widening the class-field
    fix into object literals would regress that.
    """
    _write(
        tmp_path / "src" / "o.ts",
        "export const obj = { run: () => 1 };\n",
    )

    ids = _node_ids(_extract(tmp_path))

    assert "src_o_run" not in ids, (
        f"object-literal property arrow was wrongly made bindable by name: {sorted(ids)}"
    )
