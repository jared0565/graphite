"""Python lexical scopes (#70, #71): what a bare name denotes at a call site.

Each test parses real Python with graphite's own tree-sitter loader, builds the
scope table, and resolves every bare call to one name. Imports are bound by a
fake binder so these tests pin the SCOPE rules alone; import resolution has its
own tests (`test_python_import_bindings.py`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.extract import pyscope as S
from graphite.extract.ast import _LOADER

AST_PY = Path(__file__).parents[1] / "src" / "graphite" / "extract" / "ast.py"


def _fake_imports(statement):
    """`from ext... import n` -> external; other `from` -> symbol; `import ext...` -> external; else alias."""
    out = []
    text = statement.text.decode()
    if statement.type == "import_from_statement":
        module = text.split()[1]
        for part in text.split(" import ", 1)[1].replace("(", "").replace(")", "").split(","):
            bits = part.split(" as ")
            original, local = bits[0].strip(), bits[-1].strip()
            out.append((local, "external", None) if module.startswith("ext") else (local, "symbol", f"{module}:{original}"))
    else:
        for part in text[len("import "):].split(","):
            bits = part.split(" as ")
            original, local = bits[0].strip(), bits[-1].strip().split(".")[0]
            out.append((local, "external", None) if original.startswith("ext") else (local, "alias", f"file:{original}"))
    return out


def _name_of(node):
    return node.child_by_field_name("name").text.decode()


def _table(source: str):
    tree = _LOADER.parser("python").parse(source.encode())
    return tree, S.collect_scopes(tree.root_node, _fake_imports, _name_of)


def _calls(source: str, name: str) -> list:
    tree, table = _table(source)
    found = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "call":
            func = node.child_by_field_name("function")
            if func is not None and func.type == "identifier" and func.text.decode() == name:
                found.append((node.start_byte, S.resolve(table.scope_of_call(node), name)))
        stack.extend(node.children)
    return [res for _start, res in sorted(found, key=lambda pair: pair[0])]


def _one(source: str, name: str) -> S.Resolution:
    (res,) = _calls(source, name)
    return res


def _def(qualname: str, module_level: bool = False) -> S.Resolution:
    return S.Resolution("def", qualname=qualname, module_level=module_level)


VALUE = S.Resolution("value")
NONE = S.Resolution("none")
AMBIGUOUS_IMPORT = S.Resolution("ambiguous-import")

#: One body per binder kind §4.2 lists. Each binds `fn` locally before calling it.
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


def test_a_module_level_call_binds_the_module_def():
    assert _one("def f(): pass\nf()\n", "f") == _def("f", module_level=True)


def test_a_nested_def_is_found_before_the_module_one():
    src = "def f(): pass\ndef outer():\n    def f(): pass\n    return f()\n"
    assert _one(src, "f") == _def("outer.f")


def test_a_method_body_does_not_see_its_class_scope():
    src = "def run(): pass\nclass K:\n    def run(self): pass\n    def go(self):\n        return run()\n"
    assert _one(src, "run") == _def("run", module_level=True)


def test_a_method_body_with_no_module_def_resolves_to_nothing():
    src = "class K:\n    def run(self): pass\n    def go(self):\n        return run()\n"
    assert _one(src, "run") == NONE


def test_a_class_body_statement_sees_its_class_scope():
    assert _one("class K:\n    def m(): return 1\n    x = m()\n", "m") == _def("K.m")


@pytest.mark.parametrize("body", [body for _id, body in BINDER_CASES], ids=[i for i, _b in BINDER_CASES])
def test_every_local_binder_makes_a_bare_call_a_local_value(body):
    assert _one("def fn(): pass\n" + body, "fn") == VALUE


def test_a_comprehension_target_binds_only_inside_the_comprehension():
    src = "def g(): pass\ndef f(xs):\n    [g() for g in xs]\n    return g()\n"
    assert _calls(src, "g") == [VALUE, _def("g", module_level=True)]


def test_a_walrus_in_a_comprehension_binds_in_the_function():
    src = "def y(): pass\ndef f(xs):\n    [v for v in xs if (y := v)]\n    return y()\n"
    assert _one(src, "y") == VALUE


def test_a_comprehension_in_a_class_body_does_not_see_the_class_scope():
    assert _one("class K:\n    def m(): pass\n    xs = [m() for _ in range(3)]\n", "m") == NONE


def test_the_first_iterable_of_a_class_body_comprehension_sees_the_class_scope():
    """CPython evaluates the first `for` iterable in the enclosing scope."""
    assert _one("class K:\n    def m(): return [1]\n    xs = [y for y in m()]\n", "m") == _def("K.m")


def test_global_redirects_to_the_module_binding():
    src = (
        "def helper(): pass\n"
        "def outer():\n"
        "    def helper(): pass\n"
        "    def f():\n"
        "        global helper\n"
        "        return helper()\n"
    )
    assert _one(src, "helper") == _def("helper", module_level=True)


def test_an_assignment_under_global_makes_the_module_binding_ambiguous():
    src = "def handler(): pass\ndef setup():\n    global handler\n    handler = make()\nhandler()\n"
    assert _one(src, "handler") == VALUE


def test_nonlocal_rebinding_makes_the_enclosing_binding_ambiguous():
    src = (
        "def outer():\n"
        "    def helper(): pass\n"
        "    def inner():\n"
        "        nonlocal helper\n"
        "        helper = other\n"
        "    inner()\n"
        "    return helper()\n"
    )
    assert _one(src, "helper") == VALUE


def test_a_function_local_import_binds_only_in_that_function():
    src = "def tool(): pass\ndef a():\n    from ext_pkg import tool\n    tool()\ntool()\n"
    assert _calls(src, "tool") == [S.Resolution("external"), _def("tool", module_level=True)]


def test_a_function_local_module_import_binds_a_module_alias_there():
    src = "def fn(): pass\ndef use():\n    import mod as fn\n    return fn()\n"
    assert _one(src, "fn") == S.Resolution("alias", target="file:mod")


def test_an_in_repo_symbol_import_resolves_to_its_target():
    assert _one("from repo import f\nf()\n", "f") == S.Resolution("symbol", target="repo:f")


def test_two_unresolved_imports_of_one_name_stay_external():
    src = "try:\n    from extjson import loads\nexcept ImportError:\n    from extsimple import loads\nloads()\n"
    assert _one(src, "loads") == S.Resolution("external")


def test_an_optional_dependency_with_a_none_fallback_stays_external():
    """Refinement 13: `try: import X` / `except ImportError: X = None` leaves the repo or is None."""
    src = "try:\n    import extwatch\nexcept ImportError:\n    extwatch = None\nextwatch()\n"
    assert _one(src, "extwatch") == S.Resolution("external")


def test_a_none_fallback_for_an_in_repo_import_is_ambiguous():
    src = "try:\n    from repo import f\nexcept ImportError:\n    f = None\nf()\n"
    assert _one(src, "f") == AMBIGUOUS_IMPORT


def test_two_in_repo_imports_of_one_name_are_an_ambiguous_import():
    """Final review I1: `import pkg` beside `import pkg.sub` names a MODULE either way."""
    assert _one("import pkg\nimport pkg.sub\npkg()\n", "pkg") == AMBIGUOUS_IMPORT


def test_an_in_repo_module_with_an_external_fallback_is_an_ambiguous_import():
    src = "try:\n    import fastjson as json\nexcept ImportError:\n    import extjson as json\njson()\n"
    assert _one(src, "json") == AMBIGUOUS_IMPORT


def test_a_fallback_that_is_not_none_is_ambiguous():
    src = "try:\n    import extwatch\nexcept ImportError:\n    extwatch = make()\nextwatch()\n"
    assert _one(src, "extwatch") == VALUE


def test_a_none_assignment_alone_is_a_value():
    assert _one("x = None\nx()\n", "x") == VALUE


def test_a_def_and_an_import_of_one_name_are_ambiguous():
    assert _one("from repo import f\ndef f(): pass\nf()\n", "f") == VALUE


def test_a_same_scope_redefinition_is_one_binding():
    src = "if X:\n    def f(): pass\nelse:\n    def f(): pass\nf()\n"
    assert _one(src, "f") == _def("f", module_level=True)


def test_a_default_value_is_evaluated_in_the_enclosing_scope():
    src = "def make(): pass\ndef f(make, x=make()):\n    return make()\n"
    assert _calls(src, "make") == [_def("make", module_level=True), VALUE]


def test_a_methods_default_is_evaluated_in_the_class_body():
    src = "def factory(): pass\nclass K:\n    def factory(): return 1\n    def m(self, x=factory()):\n        return x\n"
    assert _one(src, "factory") == _def("K.factory")


def test_a_decorator_is_evaluated_in_the_enclosing_scope():
    assert _one("def deco(): pass\ndef outer(deco):\n    @deco()\n    def inner(): pass\n", "deco") == VALUE
    src = "def deco(): pass\nclass K:\n    def deco(self): pass\n    @deco()\n    def m(self): pass\n"
    assert _one(src, "deco") == _def("K.deco")


def test_a_module_level_annotation_alone_does_not_bind():
    """`handler: Callable` at module scope declares, it does not bind; the def does."""
    assert _one("handler: object\ndef handler(): pass\nhandler()\n", "handler") == _def("handler", module_level=True)


def test_a_def_bound_at_module_scope_through_global_keeps_its_scoped_identity():
    src = "def setup():\n    global h\n    def h(): pass\nh()\n"
    assert _one(src, "h") == _def("setup.h")


def test_a_name_bound_nowhere_resolves_to_nothing():
    assert _one("f()\n", "f") == NONE


def test_qualnames_follow_nesting():
    src = "class Outer:\n    def method(self):\n        def helper(): pass\n        return helper()\n"
    assert _one(src, "helper") == _def("Outer.method.helper")


def test_a_recursive_nested_helper_binds_to_itself_not_its_namesake():
    src = (
        "def a(tree):\n    def visit(n):\n        return visit(n)\n    return visit(tree)\n"
        "def b(tree):\n    def visit(n):\n        return visit(n)\n    return visit(tree)\n"
    )
    assert _calls(src, "visit") == [_def("a.visit")] * 2 + [_def("b.visit")] * 2


def test_definitions_record_qualname_and_module_level():
    _tree, table = _table("def f(): pass\nclass K:\n    @property\n    def size(self): pass\n")
    assert sorted(table.definitions.values()) == [("K", True), ("K.size", False), ("f", True)]


def test_module_defines_counts_module_level_definitions_only():
    _tree, table = _table("def f(): pass\nx = 1\ndef g():\n    def h(): pass\n")
    assert table.module_defines("f")
    assert not table.module_defines("x")
    assert not table.module_defines("h")


def test_call_and_definition_node_ids_are_unique_in_a_large_file():
    """`ScopeTable` keys on tree-sitter `Node.id`; prove it is unique per node."""
    tree = _LOADER.parser("python").parse(AST_PY.read_bytes())
    keyed = {"call": [], "function_definition": [], "class_definition": []}
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in keyed:
            keyed[node.type].append(node.id)
        stack.extend(node.children)
    for kind, ids in keyed.items():
        assert ids, kind
        assert len(ids) == len(set(ids)), kind
