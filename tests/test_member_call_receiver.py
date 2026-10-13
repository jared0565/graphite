"""A member call is bound by what its receiver IS, where the source says so (#79).

`_resolve_method_dispatch` binds `recv.m()` by the NAME `m`: to every method of
that name the caller's file defines or imports, up to three. So
`Beta().setup()` was also a call to `Alpha.setup`, and `callers Alpha.setup`
listed a function that only ever builds a `Beta`, at `decision_grade`. With one
candidate it is a plain re-point: `f.read()` on an opened file was a call to
the one `read` method in reach.

Where the source states the receiver, that now decides:

* a class called to build it (`Beta().m()`), the class itself (`Beta.m()`), or
  a local name bound only from such a call binds to that class's own `m`;
* a literal, the result of a builtin that returns a builtin type
  (`open(p).read()`), or a local name bound only from those is not an object
  of the repository, and the call gets no edge.

Everything else is bound by name as before, and the tests at the bottom pin
that: a parameter, `self`, `super()`, a function's result, a method the class
inherits, a name bound two ways.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.graph import build_graph
from graphite.ingest import collect_files
from graphite.query import query

TWO_CLASSES = (
    "class Alpha:\n"
    "    def setup(self):\n"
    "        return 'alpha'\n"
    "\n"
    "\n"
    "class Beta:\n"
    "    def setup(self):\n"
    "        return 'beta'\n"
    "\n"
    "\n"
)
#: What a call bound by the name `setup` reaches in a file that starts with TWO_CLASSES.
BY_NAME = {"Alpha.setup", "Beta.setup"}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _result(tmp_path: Path):
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite", typescript_resolver="disabled")
    return extract_all(collect_files(tmp_path, cfg), cfg)


def _called(result, caller: str) -> set[str]:
    """Qualnames of the definitions `caller` has a call edge to.

    Looked up, never spelled: an edge to a placeholder reaches no definition
    and is left out, and a caller that does not exist raises.
    """
    names = {
        n["id"]: n.get("qualname", n["name"]) for n in result.nodes if n.get("kind") in ("function", "class")
    }
    sources = [i for i, name in names.items() if name == caller]
    assert len(sources) == 1, f"expected one definition named {caller!r}, found {len(sources)}"
    return {
        names[e["target"]]
        for e in result.edges
        if e["relation"] == "calls" and e["source"] == sources[0] and e["target"] in names
    }


def _one_file(tmp_path: Path, body: str, head: str = TWO_CLASSES) -> set[str]:
    _write(tmp_path / "app" / "code.py", head + body)
    return _called(_result(tmp_path), "use")


# --- the receiver is one class -------------------------------------------------


def test_a_call_on_a_newly_built_object_binds_to_its_class_only(tmp_path: Path) -> None:
    """The issue's own reproduction."""
    assert _one_file(tmp_path, "def use():\n    return Beta().setup()\n") == {"Beta", "Beta.setup"}


def test_a_call_on_the_class_itself_binds_to_that_class_only(tmp_path: Path) -> None:
    assert _one_file(tmp_path, "def use(thing):\n    return Beta.setup(thing)\n") == {"Beta.setup"}


def test_a_local_bound_only_from_one_class_binds_to_that_class_only(tmp_path: Path) -> None:
    body = "def use():\n    thing = Beta()\n    return thing.setup()\n"
    assert _one_file(tmp_path, body) == {"Beta", "Beta.setup"}


def test_a_module_level_name_bound_from_one_class_binds_to_that_class_only(tmp_path: Path) -> None:
    body = "THING = Beta()\n\n\ndef use():\n    return THING.setup()\n"
    assert _one_file(tmp_path, body) == {"Beta.setup"}


def test_a_name_set_to_none_and_then_to_one_class_binds_to_that_class(tmp_path: Path) -> None:
    """`None` has no method to call, so it says nothing against the class."""
    body = "def use(flag):\n    thing = None\n    if flag:\n        thing = Beta()\n    return thing.setup()\n"
    assert _one_file(tmp_path, body) == {"Beta", "Beta.setup"}


@pytest.mark.parametrize(
    "body",
    [
        "def use():\n    return (Beta()).setup()\n",
        "def use():\n    thing = (Beta())\n    return (thing).setup()\n",
        "def use():\n    return (  # built here\n        Beta()\n    ).setup()\n",
    ],
    ids=["around the call", "around the value and the name", "with a comment inside"],
)
def test_parentheses_around_the_receiver_change_nothing(tmp_path: Path, body: str) -> None:
    assert _one_file(tmp_path, body) == {"Beta", "Beta.setup"}


def test_a_class_defined_inside_a_function_is_read_the_same_way(tmp_path: Path) -> None:
    body = (
        "def use():\n"
        "    class Inner:\n"
        "        def setup(self):\n"
        "            return 'inner'\n"
        "    return Inner().setup()\n"
    )
    assert _one_file(tmp_path, body) == {"use.Inner", "use.Inner.setup"}


@pytest.mark.parametrize(
    "imported, built",
    [("from .store import Cache", "Cache"), ("from .store import Cache as Kept", "Kept")],
    ids=["by its name", "under another name"],
)
def test_a_call_on_an_imported_class_binds_to_that_class_only(tmp_path: Path, imported: str, built: str) -> None:
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(
        tmp_path / "pkg" / "store.py",
        "class Cache:\n    def read(self):\n        return 1\n\n\nclass Other:\n    def read(self):\n        return 2\n",
    )
    _write(
        tmp_path / "pkg" / "use.py",
        f"{imported}\n\n\nclass Local:\n    def read(self):\n        return 3\n\n\n"
        f"def use():\n    return {built}().read()\n",
    )
    assert _called(_result(tmp_path), "use") == {"Cache", "Cache.read"}


def test_callers_of_a_method_no_longer_lists_a_caller_of_its_namesake(tmp_path: Path) -> None:
    """Through the built graph and `query`, as the issue's table was taken."""
    _write(tmp_path / "app" / "tests.py", TWO_CLASSES + "def use_beta():\n    return Beta().setup()\n")
    result = _result(tmp_path)
    g = build_graph(result.nodes, result.edges)

    alpha = query(g, "callers Alpha.setup")
    beta = query(g, "callers Beta.setup")
    calls = query(g, "calls use_beta")

    assert [row["name"] for row in alpha["callers"]] == []
    assert [row["name"] for row in beta["callers"]] == ["use_beta"]
    assert {row.get("qualname", row["name"]) for row in calls["calls"]} == {"Beta", "Beta.setup"}


# --- the receiver is not an object of the repository -----------------------------

HOLDER = (
    "class Holder:\n"
    "    def {method}(self, *args):\n"
    "        return args\n"
    "\n"
    "\n"
)


def _with_one_method(tmp_path: Path, method: str, body: str) -> set[str]:
    """`use` in a file whose only method named `method` belongs to `Holder`."""
    return _one_file(tmp_path, body, head=HOLDER.format(method=method))


@pytest.mark.parametrize(
    "method, receiver",
    [
        ("join", '", "'),
        ("join", '(", ")'),
        ("upper", '"a" "b"'),
        ("format", 'f"{items}"'),
        ("decode", 'b"x"'),
        ("append", "[]"),
        ("get", "{}"),
        ("add", "{1}"),
        ("count", "(1, 2)"),
        ("sort", "[i for i in items]"),
        ("add", "{i for i in items}"),
        ("get", "{i: i for i in items}"),
        ("send", "(i for i in items)"),
        ("bit_length", "(7)"),
        ("hex", "(1.5)"),
        ("bit_length", "True"),
        ("bit_length", "False"),
    ],
)
def test_a_method_of_a_literal_is_not_a_call_into_the_repository(tmp_path: Path, method: str, receiver: str) -> None:
    assert _with_one_method(tmp_path, method, f"def use(items):\n    return {receiver}.{method}(items)\n") == set()


#: The builtins read as returning an instance of exactly a builtin type. Spelled
#: out here, not imported: taking a name out of the engine's list, or putting
#: one in, has to fail a test.
BUILTIN_TYPED = {
    "all", "any", "bin", "bool", "bytearray", "callable", "chr", "dict", "dir",
    "enumerate", "filter", "float", "frozenset", "hasattr", "hash", "hex", "id",
    "input", "int", "isinstance", "issubclass", "len", "list", "map", "memoryview",
    "object", "oct", "open", "ord", "print", "range", "set", "slice", "sorted",
    "tuple", "zip",
}
#: Builtins that are NOT read that way, and why. What `str`, `repr`, `format`,
#: `bytes` and `ascii` return is what `__str__`, `__repr__`, `__format__` or
#: `__bytes__` returned, which may be an instance of a subclass the repository
#: defines; the rest return whatever their argument decides.
NOT_BUILTIN_TYPED = [
    "str", "repr", "format", "bytes", "ascii", "reversed", "divmod", "complex",
    "getattr", "next", "iter", "min", "max", "sum", "type", "vars", "abs", "round",
]


def test_the_builtins_read_as_returning_a_builtin_type_are_exactly_these() -> None:
    from graphite.extract import pyscope

    assert set(pyscope._BUILTIN_TYPED_RESULTS) == BUILTIN_TYPED
    assert not BUILTIN_TYPED & set(NOT_BUILTIN_TYPED)


@pytest.mark.parametrize("builtin", sorted(BUILTIN_TYPED))
def test_a_method_of_what_a_builtin_returns_is_not_a_call_into_the_repository(tmp_path: Path, builtin: str) -> None:
    assert _with_one_method(tmp_path, "render", f"def use(items):\n    return {builtin}(items).render(items)\n") == set()


@pytest.mark.parametrize("builtin", NOT_BUILTIN_TYPED)
def test_a_method_of_what_these_builtins_return_is_still_bound_by_name(tmp_path: Path, builtin: str) -> None:
    called = _with_one_method(tmp_path, "render", f"def use(items):\n    return {builtin}(items).render(items)\n")
    assert called == {"Holder.render"}


@pytest.mark.parametrize(
    "method, body",
    [
        ("read", "def use(path):\n    with open(path) as handle:\n        return handle.read()\n"),
        ("add", "def use(path):\n    seen = set()\n    seen.add(path)\n    return seen\n"),
        ("format", "def use(path):\n    sep = ', '\n    return sep.format(path)\n"),
        ("extend", "def use(path):\n    out = []\n    out.extend(path)\n    return out\n"),
        ("read", "def use(path):\n    handle = None\n    handle = open(path)\n    return handle.read()\n"),
        ("add", "def use(path):\n    seen = set()\n    seen = {1}\n    seen.add(path)\n    return seen\n"),
        ("read", "def use(path):\n    handle = (open(path))\n    return (handle).read()\n"),
    ],
    ids=["with open", "a set", "a string", "a list", "None and then open", "two builtins", "in parentheses"],
)
def test_a_local_bound_only_from_builtins_is_not_a_call_into_the_repository(
    tmp_path: Path, method: str, body: str
) -> None:
    assert _with_one_method(tmp_path, method, body) == set()


def test_a_literal_s_method_is_not_a_call_to_a_function_of_that_name(tmp_path: Path) -> None:
    """With no method of the name in reach the placeholder was the module's own
    `join`, a real node, so the edge was kept and pointed at it."""
    body = "def join(parts):\n    return parts\n\n\ndef use(parts):\n    return ', '.join(parts)\n"
    assert _one_file(tmp_path, body, head="") == set()


# --- everything else is bound by name, as before --------------------------------
#
# These pass on the engine before this change as well. They are here so that a
# rule which reads more into a receiver than the source states fails a test.


def test_a_parameter_is_still_bound_by_name(tmp_path: Path) -> None:
    assert _one_file(tmp_path, "def use(thing):\n    return thing.setup()\n") == BY_NAME


def test_a_parameter_annotated_with_a_class_is_still_bound_by_name(tmp_path: Path) -> None:
    """An annotation admits a subclass, whose own method may be the one called."""
    assert _one_file(tmp_path, "def use(thing: Beta):\n    return thing.setup()\n") == BY_NAME


def test_what_a_function_returns_is_still_bound_by_name(tmp_path: Path) -> None:
    body = "def build_one():\n    return Beta()\n\n\ndef use():\n    return build_one().setup()\n"
    assert _one_file(tmp_path, body) == BY_NAME | {"build_one"}


def test_a_local_bound_from_a_function_s_result_is_still_bound_by_name(tmp_path: Path) -> None:
    body = "def build_one():\n    return Beta()\n\n\ndef use():\n    thing = build_one()\n    return thing.setup()\n"
    assert _one_file(tmp_path, body) == BY_NAME | {"build_one"}


def test_a_name_bound_from_two_classes_is_still_bound_by_name(tmp_path: Path) -> None:
    body = "def use(flag):\n    thing = Alpha()\n    if flag:\n        thing = Beta()\n    return thing.setup()\n"
    assert _one_file(tmp_path, body) == BY_NAME | {"Alpha", "Beta"}


def test_a_name_bound_from_a_class_and_another_way_is_still_bound_by_name(tmp_path: Path) -> None:
    body = "def use(things):\n    thing = Beta()\n    for thing in things:\n        pass\n    return thing.setup()\n"
    assert _one_file(tmp_path, body) == BY_NAME | {"Beta"}


def test_a_parameter_given_a_new_value_is_still_bound_by_name(tmp_path: Path) -> None:
    body = "def use(thing):\n    thing = Beta()\n    return thing.setup()\n"
    assert _one_file(tmp_path, body) == BY_NAME | {"Beta"}


def test_what_a_context_manager_hands_over_is_still_bound_by_name(tmp_path: Path) -> None:
    """`__enter__` may return anything; only `open` is known to return itself."""
    body = "def use():\n    with Beta() as thing:\n        return thing.setup()\n"
    assert _one_file(tmp_path, body) == BY_NAME | {"Beta"}


def test_a_chained_assignment_is_still_bound_by_name(tmp_path: Path) -> None:
    """`first = second = Beta()`: the value of `first` is another assignment,
    which is not read. A limit, pinned: only the innermost name is stated."""
    body = "def use():\n    first = second = Beta()\n    return first.setup(), second\n"
    assert _one_file(tmp_path, body) == BY_NAME | {"Beta"}


def test_a_name_bound_inside_an_expression_is_still_bound_by_name(tmp_path: Path) -> None:
    """`(thing := Beta())` is not read either. A limit, pinned."""
    body = "def use():\n    if (thing := Beta()):\n        return thing.setup()\n"
    assert _one_file(tmp_path, body) == BY_NAME | {"Beta"}


def test_a_name_bound_from_another_name_is_still_bound_by_name(tmp_path: Path) -> None:
    """`other = thing` is not followed back to what `thing` holds. A limit, pinned."""
    body = "def use():\n    thing = Beta()\n    other = thing\n    return other.setup()\n"
    assert _one_file(tmp_path, body) == BY_NAME | {"Beta"}


def test_an_unpacked_name_is_still_bound_by_name(tmp_path: Path) -> None:
    """`thing` is a part of what `Beta()` yields when iterated, not a `Beta`."""
    body = "def use():\n    thing, other = Beta()\n    return thing.setup(), other\n"
    assert _one_file(tmp_path, body) == BY_NAME | {"Beta"}


def test_what_a_method_returns_is_still_bound_by_name(tmp_path: Path) -> None:
    """`box.build()` is a call, but not of a name: what it returns is not stated."""
    body = "def use(box):\n    thing = box.build()\n    return thing.setup(), box.build().setup()\n"
    assert _one_file(tmp_path, body) == BY_NAME


def test_a_name_the_file_binds_nowhere_is_still_bound_by_name(tmp_path: Path) -> None:
    """It may be a builtin, and it may come from a star import."""
    assert _with_one_method(tmp_path, "fromkeys", "def use(items):\n    return dict.fromkeys(items)\n") == {
        "Holder.fromkeys"
    }


def test_what_an_except_clause_binds_is_the_exception(tmp_path: Path) -> None:
    """`except open(path) as handle` is valid to write, and `handle` is not a file."""
    body = "def use(path):\n    try:\n        pass\n    except open(path) as handle:\n        return handle.read()\n"
    assert _with_one_method(tmp_path, "read", body) == {"Holder.read"}


def test_no_receiver_to_read_is_not_a_stated_receiver() -> None:
    """A call whose function has no object node: nothing is stated, nothing raises."""
    from graphite.extract.pyscope import PyScope, stated_receiver

    assert stated_receiver(None, PyScope("module", None, "")) is None


def test_a_method_the_class_inherits_is_still_bound_by_name(tmp_path: Path) -> None:
    """`Child` has no `setup` of its own. Bound to it alone the call would lose
    `Base.setup`, the method that runs."""
    head = "class Base:\n    def setup(self):\n        return 'base'\n\n\nclass Child(Base):\n    pass\n\n\n"
    assert _one_file(tmp_path, "def use():\n    return Child().setup()\n", head=head) == {"Child", "Base.setup"}


def test_self_and_super_receivers_are_left_as_they_were(tmp_path: Path) -> None:
    """#73 owns these two receivers; this change must not move them."""
    body = (
        "class Gamma(Beta):\n"
        "    def one(self):\n"
        "        return self.setup()\n"
        "\n"
        "    def two(self):\n"
        "        return super().setup()\n"
    )
    _write(tmp_path / "app" / "code.py", TWO_CLASSES + body)
    result = _result(tmp_path)
    assert _called(result, "Gamma.one") == BY_NAME
    assert _called(result, "Gamma.two") == BY_NAME


@pytest.mark.parametrize(
    "shadow",
    [
        "def open(path):\n    return Holder()\n\n\n",
        "open = Holder\n\n\n",
    ],
    ids=["a function named open", "a name bound to something else"],
)
def test_a_builtin_s_name_the_file_binds_is_still_bound_by_name(tmp_path: Path, shadow: str) -> None:
    """`open` here is the file's own, and what it returns is not known."""
    called = _with_one_method(tmp_path, "read", shadow + "def use(path):\n    return open(path).read()\n")
    assert "Holder.read" in called


STAR = "from helpers import *\n\n\n"


@pytest.mark.parametrize(
    "body",
    [
        STAR + "def use(path):\n    return open(path).read()\n",
        STAR + "def use(path):\n    handle = open(path)\n    return handle.read()\n",
        STAR + "def use(path):\n    with open(path) as handle:\n        return handle.read()\n",
        "def use(path):\n    return open(path).read()\n\n\n" + STAR,
        "try:\n    from helpers import *\nexcept ImportError:\n    pass\n\n\ndef use(path):\n    return open(path).read()\n",
    ],
    ids=["in the call", "through a local", "through with", "the import comes later", "the import is under try"],
)
def test_a_builtin_s_name_a_star_import_may_bind_is_still_bound_by_name(tmp_path: Path, body: str) -> None:
    """The file binds `open` nowhere it can be read, but `helpers` may export one."""
    assert _with_one_method(tmp_path, "read", body) == {"Holder.read"}


def test_a_literal_beside_a_star_import_is_still_not_a_call_into_the_repository(tmp_path: Path) -> None:
    """No import can change what a literal is."""
    assert _with_one_method(tmp_path, "format", STAR + "def use(path):\n    return 'x'.format(path)\n") == set()


def test_a_class_beside_a_star_import_is_read_as_the_call_that_builds_it_is(tmp_path: Path) -> None:
    """`Beta()` is already recorded as a call to this file's `Beta`, star import
    or not. The method called on it takes the same reading and no other."""
    assert _one_file(tmp_path, STAR + "def use():\n    return Beta().setup()\n") == {"Beta", "Beta.setup"}


def test_a_local_bound_from_a_builtin_and_another_way_is_still_bound_by_name(tmp_path: Path) -> None:
    body = "def use(path, other):\n    handle = open(path)\n    handle = other\n    return handle.read()\n"
    assert _with_one_method(tmp_path, "read", body) == {"Holder.read"}


def test_a_method_of_none_is_still_bound_by_name(tmp_path: Path) -> None:
    """A name only ever set to `None` says nothing about what it holds later."""
    body = "def use():\n    thing = None\n    return thing.read()\n"
    assert _with_one_method(tmp_path, "read", body) == {"Holder.read"}


# --- what the post-pass reads is not left in the graph ----------------------------


def test_the_receiver_tag_never_reaches_an_edge(tmp_path: Path) -> None:
    """One edge of each fate: bound to its class, bound by name, and dropped."""
    body = (
        "def use(thing):\n"
        "    Beta().setup()\n"
        "    Beta.setup(thing)\n"
        "    thing.setup()\n"
        "    Beta().missing()\n"
        "    return thing\n"
    )
    _write(tmp_path / "app" / "code.py", TWO_CLASSES + body)
    result = _result(tmp_path)
    assert _called(result, "use") == BY_NAME | {"Beta"}
    assert all("_receiver" not in e and "_member" not in e for e in result.edges)
