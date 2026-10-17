"""A call through a local alias binds to what the alias names (#90).

`t = mod.target` followed by `t(x)` bound the call to a placeholder named for
the local variable, so `callers target` left the function out and said
nothing about it. An alias is read only where the source leaves no doubt: one
statement binds the name in its scope, and the right side is a bare name or a
`module.attr` whose module is an import of the file.
"""
from __future__ import annotations

from pathlib import Path

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.ingest import collect_files


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(root: Path):
    cfg = Config(workers=1, cache_dir=root / ".cache" / "graphite")
    return extract_all(collect_files(root, cfg), cfg)


def _node_id(result, qualname: str) -> str:
    ids = [n["id"] for n in result.nodes if n.get("qualname") == qualname]
    assert len(ids) == 1, f"{qualname}: {ids}"
    return ids[0]


def _call_targets(result, caller_qualname: str) -> set[str]:
    caller = _node_id(result, caller_qualname)
    return {e["target"] for e in result.edges if e["relation"] == "calls" and e["source"] == caller}


def _package(root: Path, user: str) -> None:
    _write(root / "pkg" / "__init__.py", "")
    _write(root / "pkg" / "mod.py", "def target(x):\n    return x\n\n\ndef other(x):\n    return x\n")
    _write(root / "pkg" / "user.py", user)


def test_a_call_through_an_alias_of_a_module_attribute_binds_to_it(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "from pkg import mod\n\n\ndef aliased(x):\n    t = mod.target\n    return t(x)\n",
    )
    result = _extract(tmp_path)

    assert _call_targets(result, "aliased") == {_node_id(result, "target")}


def test_the_alias_binds_the_way_the_direct_call_does(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "from pkg import mod\n\n\ndef direct(x):\n    return mod.target(x)\n\n\n"
        "def aliased(x):\n    t = mod.target\n    return t(x)\n",
    )
    result = _extract(tmp_path)

    assert _call_targets(result, "aliased") == _call_targets(result, "direct")


def test_a_module_level_alias_is_read_inside_a_function(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "from pkg import mod\n\nt = mod.target\n\n\ndef run(x):\n    return t(x)\n",
    )
    result = _extract(tmp_path)

    assert _call_targets(result, "run") == {_node_id(result, "target")}


def test_an_alias_of_a_name_the_file_imports_binds_to_the_import(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "from pkg.mod import target\n\n\ndef aliased(x):\n    t = target\n    return t(x)\n",
    )
    result = _extract(tmp_path)

    assert _call_targets(result, "aliased") == {_node_id(result, "target")}


def test_an_alias_of_a_function_the_file_defines_binds_to_it(tmp_path: Path) -> None:
    _write(
        tmp_path / "solo.py",
        "def target(x):\n    return x\n\n\ndef aliased(x):\n    t = target\n    return t(x)\n",
    )
    result = _extract(tmp_path)

    assert _call_targets(result, "aliased") == {_node_id(result, "target")}


def test_an_alias_of_an_alias_is_followed(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "from pkg import mod\n\n\ndef aliased(x):\n    t = mod.target\n    u = t\n    return u(x)\n",
    )
    result = _extract(tmp_path)

    assert _call_targets(result, "aliased") == {_node_id(result, "target")}


def test_a_name_bound_twice_to_different_things_is_not_read(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "from pkg import mod\n\n\ndef aliased(x, flag):\n"
        "    t = mod.target\n    if flag:\n        t = mod.other\n    return t(x)\n",
    )
    result = _extract(tmp_path)

    assert _call_targets(result, "aliased").isdisjoint(
        {_node_id(result, "target"), _node_id(result, "other")}
    )


def test_an_alias_that_a_parameter_also_binds_is_not_read(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "from pkg import mod\n\n\ndef aliased(x, t):\n    t = mod.target\n    return t(x)\n",
    )
    result = _extract(tmp_path)

    assert _node_id(result, "target") not in _call_targets(result, "aliased")


def test_an_alias_of_a_module_the_file_binds_twice_is_not_read(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "from pkg import mod\n\nmod = None\n\n\ndef aliased(x):\n    t = mod.target\n    return t(x)\n",
    )
    result = _extract(tmp_path)

    assert _node_id(result, "target") not in _call_targets(result, "aliased")


def test_a_cycle_of_aliases_terminates_and_binds_nothing(tmp_path: Path) -> None:
    _write(
        tmp_path / "loop.py",
        "def run(x):\n    a = b\n    b = a\n    return a(x)\n",
    )
    result = _extract(tmp_path)

    assert _call_targets(result, "run")  # an edge is kept, so the site is counted
    assert not any(t.startswith("loop_py_run") for t in _call_targets(result, "run"))


def test_what_a_with_statement_hands_over_is_not_an_alias(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "from pkg import mod\n\n\ndef aliased(x):\n    with mod.target as t:\n        return t(x)\n",
    )
    result = _extract(tmp_path)

    assert _node_id(result, "target") not in _call_targets(result, "aliased")
