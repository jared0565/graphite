"""Python scope identity (#70, #71): one node per definition, bare names bound the
way CPython resolves them.

Spec: docs/superpowers/specs/2026-10-04-python-scope-identity-design.md.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.config import Config
from graphite.extract.ast import (
    _LOADER,
    _MAX_ID_LEN,
    ExtractionResult,
    _edge,
    _extract_python,
    _file_node_id,
    _make_id,
    _merge,
    _scoped_id,
    extract_all,
)
from graphite.graph import build_graph
from graphite.health import resolution_health
from graphite.ingest import collect_files


def test_merge_keeps_the_numerically_first_location():
    """As strings `L12` sorts before `L9`, so the surviving edge named a later site (§4.5)."""
    late = _edge("m_py_f", "m_py_g", "calls", "m.py", 12)
    early = _edge("m_py_f", "m_py_g", "calls", "m.py", 9)
    (edge,) = _merge([ExtractionResult(edges=[late, early])]).edges
    assert edge["source_location"] == "L9"
    assert edge["weight"] == 2.0


def test_merge_sorts_a_missing_location_after_every_line():
    unplaced = _edge("m_py_f", "m_py_g", "calls", "m.py")
    placed = _edge("m_py_f", "m_py_g", "calls", "m.py", 3)
    (edge,) = _merge([ExtractionResult(edges=[unplaced, placed])]).edges
    assert edge["source_location"] == "L3"


def test_a_scoped_id_always_carries_a_discriminator():
    assert _scoped_id("m_py", "Worker.run") == "m_py_worker_run_7c6b1a"


def test_a_nested_definition_never_shares_a_module_definitions_id():
    """`_make_id` folds `.` into `_` as lossless: the trap `_scoped_id` exists to avoid."""
    assert _make_id("m_py", "outer.inner") == _make_id("m_py", "outer_inner") == "m_py_outer_inner"
    assert _scoped_id("m_py", "outer.inner") == "m_py_outer_inner_b0dec9"


def test_scoped_ids_differ_by_file_and_by_qualname():
    assert _scoped_id("a_py", "K.run") != _scoped_id("b_py", "K.run")
    assert _scoped_id("m_py", "test_one.fake_build") == "m_py_test_one_fake_build_4e414e"
    assert _scoped_id("m_py", "test_two.fake_build") == "m_py_test_two_fake_build_1dc280"


def test_a_scoped_id_is_capped_at_the_id_length_limit():
    longest = _scoped_id("m_py", "a" * 300)
    assert len(longest) == _MAX_ID_LEN
    assert longest != _scoped_id("m_py", "a" * 299)


def test_the_local_phantom_is_not_a_definitions_id():
    """`<` is no identifier character, so no definition's qualname hashes to this marker."""
    assert _scoped_id("m_py", "<local>.generate") == "m_py_local_generate_1c9caf"
    assert _scoped_id("m_py", "<local>.generate") != _scoped_id("m_py", "local.generate")


# --- extraction (Task 6) --------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(tmp_path: Path, files: dict[str, str]):
    for rel, text in files.items():
        _write(tmp_path / rel, text)
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite", typescript_resolver="disabled")
    return extract_all(collect_files(tmp_path, cfg), cfg)


def _extract_one(source: str, rel: str = "m.py"):
    """The extraction stage for one file: before `_merge`, before dispatch, no index."""
    tree = _LOADER.parser("python").parse(source.encode())
    return _extract_python(_file_node_id(rel), rel, source.encode(), tree, None)


def _defs(result) -> dict[tuple[str, str], dict]:
    return {(n["source_file"], n["qualname"]): n for n in result.nodes if "qualname" in n}


def _ids(result) -> dict[str, str]:
    return {qualname: n["id"] for (_file, qualname), n in _defs(result).items()}


def _call_edges(result) -> list[dict]:
    return [e for e in result.edges if e["relation"] == "calls"]


def _pairs(result) -> set[tuple[str, str]]:
    return {(e["source"], e["target"]) for e in _call_edges(result)}


#: Same cases, same ids, as tests/test_pyscope.py's BINDER_CASES: the mutation
#: run (Task 8) names a failing case by id in BOTH files.
BINDER_CASES = [
    ("param", "def use(fn):\n    return fn()\n"),
    ("posonly", "def use(a, /, fn):\n    return fn()\n"),
    ("kwonly", "def use(*, fn):\n    return fn()\n"),
    ("default", "def use(fn=None):\n    return fn()\n"),
    ("typed-default", "def use(fn: int = 1):\n    return fn()\n"),
    ("typed", "def use(fn: int):\n    return fn()\n"),
    ("star", "def use(*fn):\n    return fn()\n"),
    ("starstar", "def use(**fn):\n    return fn()\n"),
    ("lambda", "use = lambda fn: fn()\n"),
    ("assign", "def use():\n    fn = make()\n    return fn()\n"),
    ("annotated", "def use():\n    fn: int = make()\n    return fn()\n"),
    ("annotation-only", "def use():\n    fn: int\n    return fn()\n"),
    ("augmented", "def use():\n    fn += 1\n    return fn()\n"),
    ("walrus", "def use():\n    if (fn := make()):\n        return fn()\n"),
    ("unpack", "def use():\n    a, (b, *fn) = t\n    return fn()\n"),
    ("for", "def use(xs):\n    for fn in xs:\n        fn()\n"),
    ("async-for", "async def use(xs):\n    async for fn in xs:\n        fn()\n"),
    ("with", "def use():\n    with ctx() as fn:\n        fn()\n"),
    ("async-with", "async def use():\n    async with ctx() as (a, fn):\n        fn()\n"),
    ("except", "def use():\n    try:\n        pass\n    except E as fn:\n        fn()\n"),
    ("del", "def use():\n    del fn\n    return fn()\n"),
    ("match-star", "def use(t):\n    match t:\n        case [*fn]:\n            fn()\n"),
    ("match-mapping-rest", "def use(t):\n    match t:\n        case {'k': 1, **fn}:\n            fn()\n"),
    ("match-keyword", "def use(t):\n    match t:\n        case P(x=fn):\n            fn()\n"),
    ("match-as", "def use(t):\n    match t:\n        case P() as fn:\n            fn()\n"),
    ("match-capture", "def use(t):\n    match t:\n        case fn:\n            fn()\n"),
]

SHARED_NAMES = (
    "class Alpha:\n"
    "    def setup(self):\n"
    "        return 1\n"
    "class Beta:\n"
    "    def setup(self):\n"
    "        return 2\n"
    "def test_one():\n"
    "    def fake_build():\n"
    "        return 1\n"
    "    return fake_build()\n"
    "def test_two():\n"
    "    def fake_build():\n"
    "        return 2\n"
    "    return fake_build()\n"
)


def test_methods_and_nested_definitions_get_their_own_node(tmp_path):
    """#70: two `setup` methods, and two nested `fake_build`s, were ONE node each."""
    result = _extract(tmp_path, {"m.py": SHARED_NAMES})
    defs = _defs(result)
    assert defs[("m.py", "Alpha.setup")]["id"] == _scoped_id("m_py", "Alpha.setup")
    assert defs[("m.py", "Beta.setup")]["id"] == _scoped_id("m_py", "Beta.setup")
    assert defs[("m.py", "Alpha.setup")]["is_method"] is True
    one, two = "m_py_test_one_fake_build_4e414e", "m_py_test_two_fake_build_1dc280"
    assert defs[("m.py", "test_one.fake_build")]["id"] == one
    assert defs[("m.py", "test_two.fake_build")]["id"] == two
    calls = _pairs(result)
    assert ("m_py_test_one", one) in calls
    assert ("m_py_test_two", two) in calls
    assert ("m_py_test_two", one) not in calls


def test_module_scope_ids_are_unchanged_and_every_definition_has_a_qualname(tmp_path):
    source = "def helper():\n    return 1\nclass Box:\n    pass\nif X:\n    def guarded():\n        pass\n"
    defs = _defs(_extract(tmp_path, {"m.py": source}))
    for name in ("helper", "Box", "guarded"):
        assert defs[("m.py", name)]["id"] == _make_id("m_py", name)
        assert defs[("m.py", name)]["name"] == name


def test_a_nested_outer_inner_and_a_module_outer_inner_are_two_nodes(tmp_path):
    source = "def outer():\n    def inner():\n        return 1\n    return inner()\ndef outer_inner():\n    return 2\n"
    ids = _ids(_extract(tmp_path, {"m.py": source}))
    assert ids["outer_inner"] == "m_py_outer_inner"
    assert ids["outer.inner"] != ids["outer_inner"]


@pytest.mark.parametrize("body", [body for _id, body in BINDER_CASES], ids=[i for i, _b in BINDER_CASES])
def test_a_call_through_any_local_binding_is_an_unbound_local_value(tmp_path, body):
    """#71, one binder kind per case. The edge stays (the denominator must see the
    site) and points at a phantom no definition owns -- never at `def fn`."""
    result = _extract(tmp_path, {"m.py": "def fn(): pass\n" + body})
    phantom = _scoped_id("m_py", "<local>.fn")
    through = [e for e in _call_edges(result) if e["target"] in (phantom, "m_py_fn")]
    assert through, "the call through the local binding must keep an edge"
    assert {e["target"] for e in through} == {phantom}
    assert {e["confidence"] for e in through} == {"LOCAL_CALL"}


def test_a_local_value_call_stays_in_the_calls_denominator(tmp_path):
    """Dropping the edge would hide the site from the ratio (round 55): kept, unbound."""
    result = _extract(tmp_path, {"m.py": "def generate(): pass\ndef route_c(generate):\n    return generate()\n"})
    cell = resolution_health(build_graph(result.nodes, result.edges))["by_language"]["python"]["calls"]
    assert (cell["total"], cell["bound"]) == (1, 0)


def test_scope_rules_reach_the_graph(tmp_path):
    source = (
        "def helper(): pass\n"
        "def outer():\n"
        "    def helper(): pass\n"
        "    def f():\n"
        "        global helper\n"
        "        return helper()\n"
        "    return helper()\n"
        "def run(): pass\n"
        "class K:\n"
        "    def run(self): pass\n"
        "    def go(self):\n"
        "        return run()\n"
    )
    result = _extract(tmp_path, {"m.py": source})
    ids, calls = _ids(result), _pairs(result)
    assert (ids["outer.f"], ids["helper"]) in calls  # `global` reaches the module def
    assert (ids["outer"], ids["outer.helper"]) in calls  # the enclosing def wins
    assert (ids["K.go"], ids["run"]) in calls  # a method body skips its class scope
    assert (ids["K.go"], ids["K.run"]) not in calls


def test_a_function_local_import_binds_only_in_its_function(tmp_path):
    files = {
        "pkg/__init__.py": "",
        "pkg/util.py": "def tool():\n    return 1\n",
        "m.py": (
            "def tool():\n    return 2\n"
            "def a():\n    from pkg.util import tool\n    return tool()\n"
            "def b():\n    return tool()\n"
        ),
    }
    calls = _pairs(_extract(tmp_path, files))
    imported = _make_id(_file_node_id("pkg/util.py"), "tool")
    assert ("m_py_a", imported) in calls
    assert ("m_py_b", "m_py_tool") in calls
    assert ("m_py_b", imported) not in calls


def test_a_function_local_module_alias_binds_only_in_its_function(tmp_path):
    """§4.2: `module.attr` resolves `module` through the same scope walk."""
    files = {
        "flatmod.py": "def func():\n    return 1\n",
        "m.py": (
            "def a():\n    import flatmod as fm\n    return fm.func()\n"
            "def b():\n    return fm.func()\n"
        ),
    }
    calls = _pairs(_extract(tmp_path, files))
    assert ("m_py_a", "flatmod_py_func") in calls
    assert ("m_py_b", "flatmod_py_func") not in calls


def test_an_unbound_local_call_keeps_todays_placeholder_unless_it_would_bind():
    no_def = _call_edges(_extract_one("def route(generate):\n    return generate()\n"))
    assert [e["target"] for e in no_def] == ["m_py_generate"]
    with_def = _call_edges(_extract_one("def generate(): pass\ndef route(generate):\n    return generate()\n"))
    assert [e["target"] for e in with_def] == [_scoped_id("m_py", "<local>.generate")]


def test_two_unresolved_imports_of_one_name_stay_external(tmp_path):
    """Regression guard (passes before this task too): refinement 4 must not lose it."""
    source = "try:\n    from ujson import loads\nexcept ImportError:\n    from json import loads\nloads()\n"
    (edge,) = _call_edges(_extract(tmp_path, {"m.py": source}))
    assert edge["confidence"] == "EXTERNAL_CALL"


def test_a_local_value_named_like_a_global_is_a_local_receiver():
    """Refinement 2a: `it` is a test-framework global, but here a loop variable."""
    (edge,) = _call_edges(_extract_one("def f(items):\n    for it in items:\n        it.process()\n"))
    assert edge["confidence"] == "LOCAL_CALL"
    (unbound,) = _call_edges(_extract_one("def f():\n    return it.process()\n"))
    assert unbound["confidence"] == "EXTERNAL_CALL"


def test_a_module_definition_named_like_a_global_is_a_local_call():
    """Refinement 2b."""
    (edge,) = _call_edges(_extract_one("def test():\n    return 1\ntest()\n"))
    assert (edge["target"], edge["confidence"]) == ("m_py_test", "LOCAL_CALL")


def test_decorated_methods_get_scoped_ids(tmp_path):
    source = (
        "class K:\n"
        "    @property\n"
        "    def size(self):\n"
        "        return 1\n"
        "    @staticmethod\n"
        "    def make():\n"
        "        return K()\n"
    )
    result = _extract(tmp_path, {"m.py": source})
    defs = _defs(result)
    for qualname in ("K.size", "K.make"):
        assert defs[("m.py", qualname)]["id"] == _scoped_id("m_py", qualname)
        assert defs[("m.py", qualname)]["is_method"] is True
    assert (defs[("m.py", "K.make")]["id"], defs[("m.py", "K")]["id"]) in _pairs(result)


def test_a_recursive_nested_helper_calls_itself_not_its_namesake(tmp_path):
    """The shape graphite's own `extract/ast.py` had: two nested `visit`s, one bound wrong."""
    source = (
        "def a(tree):\n    def visit(n):\n        return visit(n)\n    return visit(tree)\n"
        "def b(tree):\n    def visit(n):\n        return visit(n)\n    return visit(tree)\n"
    )
    result = _extract(tmp_path, {"m.py": source})
    ids, calls = _ids(result), _pairs(result)
    expected = {
        (ids["a.visit"], ids["a.visit"]), (ids["a"], ids["a.visit"]),
        (ids["b.visit"], ids["b.visit"]), (ids["b"], ids["b.visit"]),
    }
    assert expected <= calls
    assert (ids["b.visit"], ids["a.visit"]) not in calls
    assert (ids["b"], ids["a.visit"]) not in calls


def test_a_methods_default_binds_the_class_level_helper(tmp_path):
    """Resolved in the class body; attributed to `m` as today (refinement 11)."""
    source = (
        "def factory(): pass\n"
        "class K:\n"
        "    def factory(): return 1\n"
        "    def m(self, x=factory()):\n"
        "        return x\n"
    )
    result = _extract(tmp_path, {"m.py": source})
    ids, calls = _ids(result), _pairs(result)
    assert (ids["K.m"], ids["K.factory"]) in calls
    assert (ids["K.m"], ids["factory"]) not in calls
