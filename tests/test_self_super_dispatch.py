"""`self.m()` and `super().m()` are bound by class, where the source states the chain (#73).

A member call was bound by the NAME `m`: to every method of that name in the
caller's file and the files it imports, when there are at most three. So
`self.step()` in a subclass was a call to an unrelated class's `step` as
well, `super().step()` was also a call to the method that makes it, and with
more than three methods of the name in reach neither had an edge at all.

Python says which method runs. For an object of the enclosing class itself
it is the first `m` in that class's linearisation (C3): from the class for
`self`, after it for `super()`. The call is bound to it where the source
states which class that is:

* where every base of the class, and of its bases, could be read, the
  linearisation is computed as Python computes it;
* where one could not, a base that was not read may bind `m` itself, and may
  derive from a class that was read, which Python then puts later. A method
  is bound only if what is written puts its class before every other class
  that binds `m` and before every base that was not read. What is written is
  two things Python keeps whatever the rest is: a class comes before its
  bases, and the bases of a class keep their order.

It is bound by name as before when that names no class, when the first
class binds `m` some other way than one `def`, or when a class of the chain
assigns `self.m`, which shadows a method.

A class is not only the `def`s in its body, and where the source shows
another road to a method the call is bound by name too: a decorator on the
class (`@dataclass` makes `__init__`), a metaclass or any other class
keyword, an `__init_subclass__` above the class, a `__getattribute__`, a name
set on a class from outside its body or by a class method.

What runs for an object of a SUBCLASS is not followed: an override below the
enclosing class, or what `super()` reaches when a subclass adds another base.
Two tests pin both as limits. The last test makes classes at random, takes
those roads in some of them, has Python run them, and holds every bound call
against what ran.
"""
from __future__ import annotations

import collections
import dataclasses
import random
import runpy
from pathlib import Path

import pytest

from graphite.cache import Cache
from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.graph import build_graph
from graphite.ingest import collect_files
from graphite.query import query

TWO = (
    "class Base:\n"
    "    def step(self):\n"
    "        return 'base'\n"
    "\n"
    "\n"
    "class Other:\n"
    "    def step(self):\n"
    "        return 'other'\n"
    "\n"
    "\n"
)
#: What a call bound by the name `step` reaches in a file that starts with TWO.
BY_NAME = {"Base.step", "Other.step"}
#: A class decorator, and a metaclass, that do nothing: the source cannot tell.
MARKS = "def marks(cls):\n    return cls\n\n\n"
META = "class Meta(type):\n    pass\n\n\n"
#: Under a `Mid(Base)`: `Child.run` calls `self.step()`, `Child.step` calls `super().step()`.
CHILD_RUN = "class Child(Mid):\n    def run(self):\n        return self.step()\n"
CHILD_STEP = "class Child(Mid):\n    def step(self):\n        return super().step()\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _result(tmp_path: Path):
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite", typescript_resolver="disabled")
    return extract_all(collect_files(tmp_path, cfg), cfg)


def _called(result, caller: str) -> set[str]:
    """Qualnames of the definitions `caller` has a call edge to (placeholders left out)."""
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


def _one_file(tmp_path: Path, body: str, caller: str, head: str = TWO) -> set[str]:
    _write(tmp_path / "app" / "code.py", head + body)
    return _called(_result(tmp_path), caller)


# --- self ---------------------------------------------------------------------


def test_self_binds_to_the_class_s_own_method_only(tmp_path: Path) -> None:
    body = "class Child(Base):\n    def step(self):\n        return 'child'\n\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run") == {"Child.step"}


def test_self_binds_to_the_method_the_class_inherits(tmp_path: Path) -> None:
    body = "class Child(Base):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run") == {"Base.step"}


def test_self_reaches_a_grandparent_through_a_class_without_the_method(tmp_path: Path) -> None:
    body = "class Mid(Base):\n    pass\n\n\nclass Child(Mid):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run") == {"Base.step"}


def test_self_takes_the_first_base_that_has_the_method(tmp_path: Path) -> None:
    """`class Both(Other, Base)`: Python looks in `Other` first."""
    body = "class Both(Other, Base):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Both.run") == {"Other.step"}


def test_self_follows_the_linearisation_not_the_depth(tmp_path: Path) -> None:
    """`Left` has no `step`, its base `Deep` has; `Right` has one. C3 puts
    `Right` after `Left` and `Deep`... unless `Right` derives from `Deep`,
    which it does here, so `Right` comes BEFORE `Deep`."""
    head = (
        "class Deep:\n    def step(self):\n        return 'deep'\n\n\n"
        "class Left(Deep):\n    pass\n\n\n"
        "class Right(Deep):\n    def step(self):\n        return 'right'\n\n\n"
    )
    body = "class Both(Left, Right):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Both.run", head=head) == {"Right.step"}


def test_self_is_bound_where_more_than_three_methods_share_the_name(tmp_path: Path) -> None:
    """Over the cap the call had no edge at all: the issue as filed."""
    more = "".join(f"class Extra{i}:\n    def step(self):\n        return {i}\n\n\n" for i in range(3))
    body = "class Child(Base):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run", head=TWO + more) == {"Base.step"}


def test_self_in_a_nested_function_is_the_method_s_self(tmp_path: Path) -> None:
    body = (
        "class Child(Base):\n"
        "    def run(self):\n"
        "        def inner():\n"
        "            return self.step()\n"
        "        return inner()\n"
    )
    assert _one_file(tmp_path, body, "Child.run.inner") == {"Base.step"}


def test_the_first_parameter_is_self_whatever_it_is_called(tmp_path: Path) -> None:
    body = "class Child(Base):\n    def run(this):\n        return this.step()\n"
    assert _one_file(tmp_path, body, "Child.run") == {"Base.step"}


def test_a_base_written_as_object_is_not_an_unknown_base(tmp_path: Path) -> None:
    head = "class Base(object):\n    def step(self):\n        return 'base'\n\n\nclass Other(object):\n    def step(self):\n        return 'other'\n\n\n"
    body = "class Mid(Base):\n    pass\n\n\nclass Child(Mid):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run", head=head) == {"Base.step"}


def test_a_class_keyword_is_not_a_base(tmp_path: Path) -> None:
    """Read as a base, `metaclass=type` would be one that cannot be read, and
    it would come before `Base`."""
    body = "class Mid(metaclass=type):\n    pass\n\n\nclass Child(Mid, Base):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run") == {"Base.step"}


def test_the_method_is_found_before_an_unknown_base_is_passed(tmp_path: Path) -> None:
    """`Child` defines `step`, so what `Unknown` defines does not matter."""
    body = (
        "from elsewhere import Unknown\n\n\n"
        "class Child(Unknown):\n    def step(self):\n        return 'child'\n\n    def run(self):\n        return self.step()\n"
    )
    assert _one_file(tmp_path, body, "Child.run") == {"Child.step"}


# --- super --------------------------------------------------------------------


def test_super_binds_to_the_base_s_method_and_not_to_the_method_itself(tmp_path: Path) -> None:
    body = "class Child(Base):\n    def step(self):\n        return super().step()\n"
    assert _one_file(tmp_path, body, "Child.step") == {"Base.step"}


def test_super_walks_past_a_base_without_the_method(tmp_path: Path) -> None:
    body = "class Mid(Base):\n    pass\n\n\nclass Child(Mid):\n    def step(self):\n        return super().step()\n"
    assert _one_file(tmp_path, body, "Child.step") == {"Base.step"}


def test_super_takes_the_first_base_that_has_the_method(tmp_path: Path) -> None:
    body = "class Both(Other, Base):\n    def step(self):\n        return super().step()\n"
    assert _one_file(tmp_path, body, "Both.step") == {"Other.step"}


def test_super_is_bound_where_more_than_three_methods_share_the_name(tmp_path: Path) -> None:
    more = "".join(f"class Extra{i}:\n    def step(self):\n        return {i}\n\n\n" for i in range(3))
    body = "class Child(Base):\n    def step(self):\n        return super().step()\n"
    assert _one_file(tmp_path, body, "Child.step", head=TWO + more) == {"Base.step"}


def test_super_in_a_classmethod_is_read_the_same_way(tmp_path: Path) -> None:
    head = "class Base:\n    @classmethod\n    def step(cls):\n        return 'base'\n\n\nclass Other:\n    def step(self):\n        return 'other'\n\n\n"
    body = "class Child(Base):\n    @classmethod\n    def step(cls):\n        return super().step()\n"
    assert _one_file(tmp_path, body, "Child.step", head=head) == {"Base.step"}


# --- across files ---------------------------------------------------------------


def test_a_base_imported_by_name_is_followed(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "base.py", TWO)
    _write(
        tmp_path / "pkg" / "use.py",
        "from pkg.base import Base as Root\n\n\nclass Child(Root):\n    def run(self):\n        return self.step()\n",
    )
    assert _called(_result(tmp_path), "Child.run") == {"Base.step"}


def test_a_grandparent_in_a_file_the_caller_does_not_import_is_bound(tmp_path: Path) -> None:
    """The chain is the evidence. Bound by name, a definition the caller's
    file neither holds nor imports is never a candidate."""
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "top.py", TWO)
    _write(tmp_path / "pkg" / "mid.py", "from pkg.top import Base\n\n\nclass Mid(Base):\n    pass\n")
    _write(
        tmp_path / "pkg" / "use.py",
        "from pkg.mid import Mid\n\n\nclass Child(Mid):\n    def run(self):\n        return self.step()\n\n"
        "    def step_up(self):\n        return super().step()\n",
    )
    result = _result(tmp_path)
    assert _called(result, "Child.run") == {"Base.step"}
    assert _called(result, "Child.step_up") == {"Base.step"}


def test_callers_of_an_unrelated_method_no_longer_lists_the_subclass(tmp_path: Path) -> None:
    """The answer a user reads, end to end."""
    _write(tmp_path / "app" / "code.py", TWO + "class Child(Base):\n    def run(self):\n        return self.step()\n")
    result = _result(tmp_path)
    graph = build_graph(result.nodes, result.edges)
    assert [row["qualname"] for row in query(graph, "callers Base.step")["callers"]] == ["Child.run"]
    assert query(graph, "callers Other.step")["callers"] == []


# --- where the chain is not stated, the call is bound by name as before -----------
#
# Every test below passes on the engine before this change as well, except
# where its docstring says otherwise. They are here so that a walk which goes
# further than the source states fails a test.


def test_an_unknown_base_passed_on_the_way_stops_the_walk(tmp_path: Path) -> None:
    """`Unknown` comes before `Base` and may define `step` itself."""
    body = "from elsewhere import Unknown\n\n\nclass Child(Unknown, Base):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME


def test_an_unknown_base_of_a_class_on_the_way_stops_the_walk(tmp_path: Path) -> None:
    body = (
        "from elsewhere import Unknown\n\n\n"
        "class Mid(Unknown):\n    pass\n\n\n"
        "class Child(Mid, Base):\n    def run(self):\n        return self.step()\n"
    )
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME


@pytest.mark.parametrize(
    "base",
    ["Exception", "helpers.Thing", "make()", "Generic[T]", "*bases"],
    ids=["a builtin", "an attribute", "a call", "a subscript", "a splat"],
)
def test_a_base_that_is_not_a_class_of_the_repository_is_unknown(tmp_path: Path, base: str) -> None:
    body = f"class Child({base}, Base):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME


def test_super_past_an_unknown_base_is_bound_by_name(tmp_path: Path) -> None:
    body = "from elsewhere import Unknown\n\n\nclass Child(Unknown, Base):\n    def run(self):\n        return super().step()\n"
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME


# --- an unknown base anywhere among the bases -----------------------------------
#
# The order of the classes the source states is NOT the order Python uses. A
# base that cannot be read may derive from one of them, and Python then puts
# that one later; it may also define the method itself. So a method is bound
# only where what the source states puts its class before every other class
# that binds the name and before every base that cannot be read.
# `test_what_is_bound_is_what_python_runs` runs such hierarchies as real classes.

SPLIT = (
    "from elsewhere import Unknown\n\n\n"
    "class Top:\n    def step(self):\n        return 'top'\n\n\n"
    "class Left(Top):\n    pass\n\n\n"
)


def test_an_unknown_base_further_on_may_put_another_class_first(tmp_path: Path) -> None:
    """If `Unknown` derives from `Top`, Python's order is Child, Left, Right,
    Unknown, Top, and `Right.step` runs. Reading only what is stated gives
    Child, Left, Top, Right."""
    body = (
        "class Right(Unknown):\n    def step(self):\n        return 'right'\n\n\n"
        "class Child(Left, Right):\n    def run(self):\n        return self.step()\n"
    )
    assert _one_file(tmp_path, body, "Child.run", head=SPLIT) == {"Top.step", "Right.step"}


def test_an_unknown_base_further_on_may_define_the_method_first(tmp_path: Path) -> None:
    """If `Unknown` derives from `Top` and has a `step`, that one runs."""
    body = (
        "class Aside:\n    def step(self):\n        return 'aside'\n\n\n"
        "class Right(Unknown):\n    pass\n\n\n"
        "class Child(Left, Right):\n    def run(self):\n        return self.step()\n"
    )
    assert _one_file(tmp_path, body, "Child.run", head=SPLIT) == {"Top.step", "Aside.step"}


def test_super_is_not_bound_past_such_a_base_either(tmp_path: Path) -> None:
    body = (
        "class Right(Unknown):\n    def step(self):\n        return 'right'\n\n\n"
        "class Child(Left, Right):\n    def step(self):\n        return super().step()\n"
    )
    assert _one_file(tmp_path, body, "Child.step", head=SPLIT) == {"Top.step", "Right.step", "Child.step"}


def test_two_stated_branches_are_ordered_by_python_only_while_every_base_is_read(tmp_path: Path) -> None:
    """`class Child(A, B)`, `step` on A's base and on B's base. With every
    base read, Python's order is Child, A, X, B, Y. With an unknown base on
    `Y`, that base may derive from `X`, and the order is Child, A, B, Y, ..., X."""
    head = (
        "from elsewhere import Unknown\n\n\n"
        "class X:\n    def step(self):\n        return 'x'\n\n\n"
        "class A(X):\n    pass\n\n\n"
        "class Y{base}:\n    def step(self):\n        return 'y'\n\n\n"
        "class B(Y):\n    pass\n\n\n"
    )
    body = "class Child(A, B):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path / "read", body, "Child.run", head=head.format(base="")) == {"X.step"}
    assert _one_file(tmp_path / "unknown", body, "Child.run", head=head.format(base="(Unknown)")) == {"X.step", "Y.step"}


def test_a_single_chain_is_bound_up_to_the_class_whose_base_is_unknown(tmp_path: Path) -> None:
    """Child, Mid, Base, Top, and only then whatever `Unknown` is: a class
    with one base is followed by that base's own order, unchanged."""
    head = (
        "from elsewhere import Unknown\n\n\n"
        "class Top(Unknown):\n    pass\n\n\n"
        "class Base(Top):\n    def step(self):\n        return 'base'\n\n\n"
        "class Other:\n    def step(self):\n        return 'other'\n\n\n"
    )
    body = (
        "class Mid(Base):\n    pass\n\n\n"
        "class Child(Mid):\n    def run(self):\n        return self.step()\n\n"
        "    def up(self):\n        return super().step()\n"
    )
    _write(tmp_path / "app" / "code.py", head + body)
    result = _result(tmp_path)
    assert _called(result, "Child.run") == {"Base.step"}
    assert _called(result, "Child.up") == {"Base.step"}


def test_coming_before_every_unknown_base_is_not_enough(tmp_path: Path) -> None:
    """`class Child(B, A)`: Python's order is Child, B, Y, A, X, and `Y.step`
    runs. `X` comes before the one unknown base, its own, and is not first:
    nothing written puts it before `Y`."""
    head = (
        "from elsewhere import Unknown\n\n\n"
        "class X(Unknown):\n    def step(self):\n        return 'x'\n\n\n"
        "class A(X):\n    pass\n\n\n"
        "class Y:\n    def step(self):\n        return 'y'\n\n\n"
        "class B(Y):\n    pass\n\n\n"
    )
    body = "class Child(B, A):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run", head=head) == {"X.step", "Y.step"}


def test_the_first_base_is_bound_whatever_comes_after_it(tmp_path: Path) -> None:
    """`class Child(Base, Unknown)`: the bases keep the order they are written
    in, and Python refuses a class whose later base is an ancestor of an
    earlier one. So no class of `Unknown` comes before `Base`."""
    body = (
        "from elsewhere import Unknown\n\n\n"
        "class Child(Base, Unknown):\n    def run(self):\n        return self.step()\n\n"
        "    def step_up(self):\n        return super().step()\n"
    )
    _write(tmp_path / "app" / "code.py", TWO + body)
    result = _result(tmp_path)
    assert _called(result, "Child.run") == {"Base.step"}
    assert _called(result, "Child.step_up") == {"Base.step"}


def test_the_base_of_the_first_base_is_not_bound_when_a_later_base_is_unknown(tmp_path: Path) -> None:
    """`class Child(Mid, Unknown)`, `step` on Mid's base: `Unknown` may derive
    from that base, and then comes before it."""
    body = (
        "from elsewhere import Unknown\n\n\n"
        "class Mid(Base):\n    pass\n\n\n"
        "class Child(Mid, Unknown):\n    def run(self):\n        return self.step()\n"
    )
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME


def test_a_method_is_bound_past_a_stated_class_that_has_no_unknown_base(tmp_path: Path) -> None:
    """`class Child(Mixin, Real)`: `Real` is the only class that binds `step`
    and the only one with an unknown base, so wherever Python puts `Mixin`,
    `Real.step` is the first `step`."""
    head = (
        "from elsewhere import Unknown\n\n\n"
        "class Mixin:\n    pass\n\n\n"
        "class Real(Unknown):\n    def step(self):\n        return 'real'\n\n\n"
        "class Other:\n    def step(self):\n        return 'other'\n\n\n"
    )
    body = "class Child(Mixin, Real):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run", head=head) == {"Real.step"}


def test_a_method_is_not_bound_past_a_class_whose_unknown_base_comes_first(tmp_path: Path) -> None:
    """The same, with the unknown base on `Mixin`: it comes before `Real`."""
    head = (
        "from elsewhere import Unknown\n\n\n"
        "class Mixin(Unknown):\n    pass\n\n\n"
        "class Real:\n    def step(self):\n        return 'real'\n\n\n"
        "class Other:\n    def step(self):\n        return 'other'\n\n\n"
    )
    body = "class Child(Mixin, Real):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run", head=head) == {"Real.step", "Other.step"}


def test_a_class_written_with_object_is_walked_past(tmp_path: Path) -> None:
    """`class Mixin(object)` has no base that cannot be read."""
    body = "class Mixin(object):\n    pass\n\n\nclass Child(Mixin, Base):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run") == {"Base.step"}


@pytest.mark.parametrize(
    "binding",
    [
        "    step = helper\n",
        "    step = property(lambda self: None)\n",
        "    class step:\n        pass\n",
        "    if FLAG:\n        def step(self):\n            return 1\n    else:\n        def step(self):\n            return 2\n",
        "    def step(self):\n        return 1\n    step = staticmethod(step)\n",
        "    def step(self):\n        return 1\n    type step = int\n",
    ],
    ids=["an assignment", "a property object", "a nested class", "two defs", "a def and an assignment", "a def and a type statement"],
)
def test_a_class_that_binds_the_name_some_other_way_stops_the_walk(tmp_path: Path, binding: str) -> None:
    """`Mid.step` is not one method node. Walking on to `Base.step` would bind
    the call to a method that `Mid` hides."""
    body = f"class Mid(Base):\n{binding}\n\nclass Child(Mid):\n    def run(self):\n        return self.step()\n"
    called = _one_file(tmp_path, body, "Child.run")
    assert BY_NAME <= called, called


@pytest.mark.parametrize(
    "statement",
    [
        "self.step = f",
        "self.step: object = f",
        "self.step += f",
        "(self.step, other) = f",
        "*self.step, other = f",
        "for self.step in f:\n            pass",
        "with f as self.step:\n            pass",
    ],
    ids=["an assignment", "an annotated one", "an augmented one", "an unpacking", "a starred one", "a loop target", "a with target"],
)
def test_an_instance_attribute_of_the_name_stops_the_walk(tmp_path: Path, statement: str) -> None:
    """A function is not a data descriptor: after `self.step = f`, `self.step()` calls `f`."""
    body = (
        "class Child(Base):\n"
        "    def __init__(self, f):\n"
        f"        {statement}\n\n"
        "    def run(self):\n"
        "        return self.step()\n"
    )
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME


@pytest.mark.parametrize(
    "statement",
    ["self.step: object", "f.step = self", "self.other = f", "self.step.count = f", "self.other.step = f"],
    ids=[
        "an annotation with no value", "another object's attribute", "another attribute",
        "an attribute of the method", "the name on an attribute of the instance",
    ],
)
def test_what_does_not_set_the_instance_attribute_does_not_stop_the_walk(tmp_path: Path, statement: str) -> None:
    """Fails on the engine before this change, which bound `Other.step` as well."""
    body = (
        "class Child(Base):\n"
        "    def __init__(self, f):\n"
        f"        {statement}\n\n"
        "    def run(self):\n"
        "        return self.step()\n"
    )
    assert _one_file(tmp_path, body, "Child.run") == {"Base.step"}


def test_an_instance_attribute_set_by_a_base_class_stops_the_walk(tmp_path: Path) -> None:
    head = (
        "class Base:\n    def __init__(self, f):\n        self.step, self.other = f, f\n\n"
        "    def step(self):\n        return 'base'\n\n\n"
        "class Other:\n    def step(self):\n        return 'other'\n\n\n"
    )
    body = "class Child(Base):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run", head=head) == BY_NAME


def test_an_instance_attribute_does_not_stop_super(tmp_path: Path) -> None:
    """`super()` looks on the classes and never in the instance. Fails on the
    engine before this change, which bound `Child.step` as well."""
    body = (
        "class Child(Base):\n"
        "    def __init__(self, f):\n"
        "        self.step = f\n\n"
        "    def step(self):\n"
        "        return super().step()\n"
    )
    assert _one_file(tmp_path, body, "Child.step") == {"Base.step"}


PRIVATE = TWO.replace("step", "__step")


def test_a_private_name_is_found_on_the_class_it_is_written_in(tmp_path: Path) -> None:
    """Fails on the engine before this change, which bound all three `__step`."""
    body = "class Child(Base):\n    def __step(self):\n        return 'child'\n\n    def run(self):\n        return self.__step()\n"
    assert _one_file(tmp_path, body, "Child.run", head=PRIVATE) == {"Child.__step"}


def test_a_private_name_is_not_looked_for_on_a_base(tmp_path: Path) -> None:
    """`self.__step` in `Child` is `self._Child__step`. `Base.__step` is
    `_Base__step`: another name, and no base can provide the first."""
    body = "class Child(Base):\n    def run(self):\n        return self.__step()\n\n    def up(self):\n        return super().__step()\n"
    _write(tmp_path / "app" / "code.py", PRIVATE + body)
    result = _result(tmp_path)
    assert _called(result, "Child.run") == {"Base.__step", "Other.__step"}
    assert _called(result, "Child.up") == {"Base.__step", "Other.__step"}


def test_a_dunder_method_is_walked_like_any_other(tmp_path: Path) -> None:
    """`__init__` begins with two underscores and is not a private name."""
    head = TWO.replace("step", "__init__")
    body = "class Child(Base):\n    def __init__(self):\n        super().__init__()\n"
    assert _one_file(tmp_path, body, "Child.__init__", head=head) == {"Base.__init__"}


def test_an_annotated_first_parameter_is_still_the_instance(tmp_path: Path) -> None:
    body = "class Child(Base):\n    def run(self: 'Child'):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run") == {"Base.step"}


def test_a_bare_super_outside_any_class_is_bound_by_name(tmp_path: Path) -> None:
    """It cannot run, and it must not stop the file from being read."""
    assert _one_file(tmp_path, "def run(self):\n    return super().step()\n", "run") == BY_NAME


def test_a_base_that_names_a_function_is_unknown(tmp_path: Path) -> None:
    """A name the file binds, but not to a class: what it would add to the chain is not known."""
    body = "def factory():\n    return Other\n\n\nclass Child(factory, Base):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME


def test_object_beside_a_star_import_is_an_unknown_base(tmp_path: Path) -> None:
    """`helpers` may export an `object` of its own."""
    body = "from helpers import *\n\n\nclass Mid(object):\n    pass\n\n\nclass Child(Mid, Base):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME


def test_a_self_that_is_given_another_value_is_bound_by_name(tmp_path: Path) -> None:
    body = "class Child(Base):\n    def run(self, other):\n        self = other\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME


@pytest.mark.parametrize(
    "method",
    [
        "    @staticmethod\n    def run(thing):\n        return thing.step()\n",
        "    @classmethod\n    def run(cls):\n        return cls.step()\n",
        "    def run(*args):\n        return args.step()\n",
        "    run = lambda self: self.step()\n",
    ],
    ids=["a static method", "a class method", "no first parameter", "a lambda"],
)
def test_a_first_parameter_that_is_not_the_instance_is_bound_by_name(tmp_path: Path, method: str) -> None:
    body = f"class Child(Base):\n{method}"
    _write(tmp_path / "app" / "code.py", TWO + body)
    result = _result(tmp_path)
    names = {n["id"]: n.get("qualname", n["name"]) for n in result.nodes}
    targets = {names.get(e["target"]) for e in result.edges if e["relation"] == "calls"}
    assert BY_NAME <= targets, targets


def test_a_function_outside_a_class_has_no_self(tmp_path: Path) -> None:
    body = "def run(self):\n    return self.step()\n"
    assert _one_file(tmp_path, body, "run") == BY_NAME


@pytest.mark.parametrize(
    "call",
    ["super(Child, self).step()", "(lambda: super().step())()", "[super().step() for _ in (1,)][0]"],
    ids=["two arguments", "in a lambda", "in a comprehension"],
)
def test_a_super_that_is_not_the_bare_one_in_a_method_is_bound_by_name(tmp_path: Path, call: str) -> None:
    body = f"class Child(Base):\n    def run(self):\n        return {call}\n"
    _write(tmp_path / "app" / "code.py", TWO + body)
    result = _result(tmp_path)
    names = {n["id"]: n.get("qualname", n["name"]) for n in result.nodes}
    targets = {names.get(e["target"]) for e in result.edges if e["relation"] == "calls"}
    assert BY_NAME <= targets, targets


@pytest.mark.parametrize(
    "shadow",
    ["def super():\n    return Other()\n\n\n", "from helpers import *\n\n\n"],
    ids=["the file defines super", "a star import may"],
)
def test_a_super_the_file_may_bind_is_bound_by_name(tmp_path: Path, shadow: str) -> None:
    body = shadow + "class Child(Base):\n    def run(self):\n        return super().step()\n"
    assert BY_NAME <= _one_file(tmp_path, body, "Child.run")


def test_a_method_no_class_in_the_chain_has_is_bound_by_name(tmp_path: Path) -> None:
    """A mixin calls what its host provides. The chain is known to its end
    and has no `step`: nothing is stated, and the name is all there is."""
    body = "class Mixin:\n    def run(self):\n        return self.step()\n\n    def up(self):\n        return super().step()\n"
    _write(tmp_path / "app" / "code.py", TWO + body)
    result = _result(tmp_path)
    assert _called(result, "Mixin.run") == BY_NAME
    assert _called(result, "Mixin.up") == BY_NAME


def test_classes_that_derive_from_each_other_are_bound_by_name(tmp_path: Path) -> None:
    """Not valid Python to run, and still a file graphite is handed: no hang, no error."""
    body = "class Loop(Back):\n    def run(self):\n        return self.step()\n\n\nclass Back(Loop):\n    pass\n"
    assert _one_file(tmp_path, body, "Loop.run") == BY_NAME


def test_bases_with_no_consistent_order_are_bound_by_name(tmp_path: Path) -> None:
    """`class Both(Base, Sub)` with `Sub(Base)` is a TypeError in Python: there is no linearisation."""
    body = "class Sub(Base):\n    pass\n\n\nclass Both(Base, Sub):\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Both.run") == BY_NAME


def test_bases_with_no_consistent_order_are_bound_by_name_beside_an_unknown_base(tmp_path: Path) -> None:
    """The same refused class, with a base that cannot be read as well."""
    body = (
        "from elsewhere import Unknown\n\n\n"
        "class Sub(Base):\n    pass\n\n\n"
        "class Both(Base, Sub, Unknown):\n    def run(self):\n        return self.step()\n"
    )
    assert _one_file(tmp_path, body, "Both.run") == BY_NAME


def test_a_stated_class_that_inherits_the_method_is_still_bound_by_name(tmp_path: Path) -> None:
    """#79's receivers are not walked: `Child().step()` is a follow-up, pinned there and here."""
    body = "class Child(Base):\n    pass\n\n\ndef use():\n    return Child().step()\n"
    assert _one_file(tmp_path, body, "use") == BY_NAME | {"Child"}


# --- a class is not only the `def`s in its body ------------------------------------
#
# A class gets a method by other roads than a `def`: a decorator on the class
# (`@dataclass` makes `__init__`), a metaclass, a base's `__init_subclass__`,
# an assignment to the class from outside its body. Where the source shows such
# a road, the `def`s do not say which method runs, and the call is bound by
# name as before.


@pytest.mark.parametrize(
    "statement",
    [
        "    if FLAG:\n        def step(self):\n            return 'mid'\n",
        "    if FLAG:\n        pass\n    else:\n        def step(self):\n            return 'mid'\n",
        "    try:\n        def step(self):\n            return 'mid'\n    except NameError:\n        pass\n",
        "    for _ in FLAG:\n        def step(self):\n            return 'mid'\n",
        "    while FLAG:\n        def step(self):\n            return 'mid'\n",
        "    with FLAG:\n        def step(self):\n            return 'mid'\n",
        "    if FLAG:\n        @marks\n        def step(self):\n            return 'mid'\n",
    ],
    ids=["under-if", "under-else", "under-try", "under-for", "under-while", "under-with", "decorated-under-if"],
)
@pytest.mark.parametrize("child", [CHILD_RUN, CHILD_STEP], ids=["self", "super"])
def test_a_def_the_class_body_may_not_have_run_is_bound_by_name(tmp_path: Path, statement: str, child: str) -> None:
    """`Mid.step` is there if the statement around it ran it. If not, the
    call goes on to `Base.step`. The source does not say which."""
    body = MARKS + "FLAG = ()\n\n\nclass Mid(Base):\n" + statement + "\n\n" + child
    caller = "Child.run" if child == CHILD_RUN else "Child.step"
    assert _one_file(tmp_path, body, caller, head=TWO.replace("class Other:\n    def step(self):\n        return 'other'\n\n\n", "")) - {"Child.step"} == {
        "Base.step",
        "Mid.step",
    }


def test_a_def_written_straight_in_the_body_is_bound_beside_one_that_is_not(tmp_path: Path) -> None:
    """Only the name under the `if` is in doubt. `Mid.other` is written
    straight in the body, and `Child` calls it."""
    body = (
        "FLAG = ()\n\n\nclass Mid(Base):\n"
        "    def other(self):\n        return 'mid'\n\n"
        "    if FLAG:\n        def step(self):\n            return 'mid'\n\n\n"
        "class Apart:\n    def other(self):\n        return 'apart'\n\n\n"
        "class Child(Mid):\n    def run(self):\n        return self.other()\n"
    )
    assert _one_file(tmp_path, body, "Child.run") == {"Mid.other"}


def test_a_decorated_def_written_straight_in_the_body_is_bound(tmp_path: Path) -> None:
    """A decorator on the METHOD is not read: the call is bound to the `def`."""
    body = MARKS + "class Mid(Base):\n    @marks\n    def step(self):\n        return 'mid'\n\n\n" + CHILD_RUN
    assert _one_file(tmp_path, body, "Child.run") == {"Mid.step"}


def test_a_decorated_class_may_have_the_method_without_a_def(tmp_path: Path) -> None:
    """`Mid` has no `def step`, and what `marks` returns may have a `step` all the same."""
    body = MARKS + "@marks\nclass Mid(Base):\n    pass\n\n\n" + CHILD_RUN
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME


def test_super_is_not_bound_past_a_decorated_class(tmp_path: Path) -> None:
    body = MARKS + "@marks\nclass Mid(Base):\n    pass\n\n\n" + CHILD_STEP
    assert _one_file(tmp_path, body, "Child.step") == BY_NAME | {"Child.step"}


def test_a_dataclass_makes_an_init_no_def_wrote(tmp_path: Path) -> None:
    """The everyday case. `super().__init__()` in `Leaf` runs the `__init__`
    that `@dataclass` made for `Mid`, which calls no other. Bound by class it
    was a call to `Base.__init__`, however many `__init__` the file has."""
    head = (
        "from dataclasses import dataclass\n\n\n"
        "class Base:\n    def __init__(self):\n        self.ready = True\n\n\n"
        + "".join(f"class Extra{i}:\n    def __init__(self):\n        self.n = {i}\n\n\n" for i in range(3))
    )
    body = "@dataclass\nclass Mid(Base):\n    size: int = 0\n\n\nclass Leaf(Mid):\n    def __init__(self):\n        super().__init__()\n"
    assert _one_file(tmp_path, body, "Leaf.__init__", head=head) == set()


def test_a_decorated_class_is_not_trusted_for_its_own_def_either(tmp_path: Path) -> None:
    """A decorator may replace what the body defines. No list of decorators
    that do not is kept here."""
    body = MARKS + "@marks\nclass Child(Base):\n    def step(self):\n        return 'child'\n\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME | {"Child.step"}


def test_a_method_found_before_a_decorated_class_is_bound(tmp_path: Path) -> None:
    """`Mid.step` comes first whatever the decorator did to `Root`."""
    head = (
        MARKS
        + "@marks\nclass Root:\n    def step(self):\n        return 'root'\n\n\n"
        + "class Other:\n    def step(self):\n        return 'other'\n\n\n"
        + "class Mid(Root):\n    def step(self):\n        return 'mid'\n\n\n"
    )
    assert _one_file(tmp_path, CHILD_RUN, "Child.run", head=head) == {"Mid.step"}


def test_a_decorated_method_does_not_make_the_class_a_decorated_one(tmp_path: Path) -> None:
    body = MARKS + "class Mid(Base):\n    @marks\n    def other(self):\n        return 1\n\n\n" + CHILD_RUN
    assert _one_file(tmp_path, body, "Child.run") == {"Base.step"}


@pytest.mark.parametrize(
    "statement",
    [
        "class Mid(Base, metaclass=Meta):",
        "class Mid(Base, flag=True):",
        "class Mid(Base, flag=type):",
        "class Mid(Base, **options):",
    ],
    ids=["a-metaclass", "another-keyword", "another-keyword-given-type", "keywords-in-a-mapping"],
)
def test_a_class_made_with_a_keyword_is_bound_by_name(tmp_path: Path, statement: str) -> None:
    """A metaclass runs for the class and for every class derived from it, and
    may set anything on each. Any other keyword goes to a metaclass or to an
    `__init_subclass__`."""
    body = META + "options = {}\n\n\n" + statement + "\n    pass\n\n\n" + CHILD_RUN
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME


def test_a_class_made_with_a_keyword_is_not_trusted_for_its_own_def(tmp_path: Path) -> None:
    body = META + "class Child(Base, metaclass=Meta):\n    def step(self):\n        return 'child'\n\n    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME | {"Child.step"}


def test_super_is_bound_by_name_under_a_class_made_with_a_keyword(tmp_path: Path) -> None:
    body = META + "class Mid(Base, metaclass=Meta):\n    pass\n\n\n" + CHILD_STEP
    assert _one_file(tmp_path, body, "Child.step") == BY_NAME | {"Child.step"}


def test_a_keyword_on_a_class_that_is_not_among_the_bases_says_nothing(tmp_path: Path) -> None:
    body = META + "class Apart(metaclass=Meta):\n    pass\n\n\nclass Mid(Base):\n    pass\n\n\n" + CHILD_RUN
    assert _one_file(tmp_path, body, "Child.run") == {"Base.step"}


@pytest.mark.parametrize(
    "shadow",
    ["def type(*args):\n    return None\n\n\n", "from elsewhere import *\n\n\n"],
    ids=["type-is-defined-in-the-file", "a-star-import"],
)
def test_metaclass_type_says_nothing_only_where_type_is_the_builtin(tmp_path: Path, shadow: str) -> None:
    """`test_a_class_keyword_is_not_a_base` has the other half: with the
    builtin `type`, the class is made the way every class is."""
    body = shadow + "class Mid(Base, metaclass=type):\n    pass\n\n\n" + CHILD_RUN
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME


@pytest.mark.parametrize(
    "hook",
    [
        "    def __init_subclass__(cls, **kwargs):\n        super().__init_subclass__(**kwargs)\n",
        "    __init_subclass__ = classmethod(lambda cls, **kwargs: None)\n",
    ],
    ids=["a-def", "an-assignment"],
)
def test_an_init_subclass_above_the_class_is_bound_by_name(tmp_path: Path, hook: str) -> None:
    """It runs when `Child` is made and can set anything on it."""
    body = "class Mid(Base):\n" + hook + "\n\n" + CHILD_RUN
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME


def test_super_is_bound_by_name_under_an_init_subclass(tmp_path: Path) -> None:
    body = "class Mid(Base):\n    def __init_subclass__(cls, **kwargs):\n        super().__init_subclass__(**kwargs)\n\n\n" + CHILD_STEP
    assert _one_file(tmp_path, body, "Child.step") == BY_NAME | {"Child.step"}


def test_an_init_subclass_on_the_class_itself_does_not_run_for_it(tmp_path: Path) -> None:
    body = (
        "class Child(Base):\n"
        "    def __init_subclass__(cls, **kwargs):\n        super().__init_subclass__(**kwargs)\n\n"
        "    def run(self):\n        return self.step()\n"
    )
    assert _one_file(tmp_path, body, "Child.run") == {"Base.step"}


@pytest.mark.parametrize("where", ["Mid", "Child"], ids=["on-a-base", "on-the-class-itself"])
def test_a_getattribute_among_the_classes_is_bound_by_name(tmp_path: Path, where: str) -> None:
    """Every `self.step` goes through it, and it may answer with anything."""
    hook = "    def __getattribute__(self, name):\n        return super().__getattribute__(name)\n"
    mid = "class Mid(Base):\n" + (hook if where == "Mid" else "    pass\n") + "\n\n"
    child = "class Child(Mid):\n" + (hook + "\n" if where == "Child" else "") + "    def run(self):\n        return self.step()\n"
    assert _one_file(tmp_path, mid + child, "Child.run") == BY_NAME


def test_a_getattribute_does_not_stop_super(tmp_path: Path) -> None:
    """`super()` looks in the classes itself and does not ask `__getattribute__`."""
    body = "class Mid(Base):\n    def __getattribute__(self, name):\n        return super().__getattribute__(name)\n\n\n" + CHILD_STEP
    assert _one_file(tmp_path, body, "Child.step") == {"Base.step"}


def test_a_getattr_does_not_stop_the_walk(tmp_path: Path) -> None:
    """`__getattr__` is asked only when the name is found nowhere."""
    body = "class Mid(Base):\n    def __getattr__(self, name):\n        return None\n\n\n" + CHILD_RUN
    assert _one_file(tmp_path, body, "Child.run") == {"Base.step"}


@pytest.mark.parametrize(
    "statement",
    [
        "Mid.step = None\n",
        "Base.step = None\n",
        "Mid.step += 1\n",
        "del Base.step\n",
        "Mid.step, spare = None, None\n",
        "*Mid.step, spare = None, None\n",
        "for Mid.step in ():\n    pass\n",
        "with open('f') as Mid.step:\n    pass\n",
        "def patch():\n    Mid.step = None\n",
    ],
    ids=["assigned", "assigned-on-the-base", "augmented", "deleted", "in-a-tuple", "starred", "a-loop-target", "a-with-target", "in-a-function"],
)
def test_a_name_set_on_a_class_from_outside_its_body_is_bound_by_name(tmp_path: Path, statement: str) -> None:
    body = "class Mid(Base):\n    pass\n\n\n" + CHILD_RUN + "\n\n" + statement
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME


@pytest.mark.parametrize(
    "statement",
    [
        "Other.step = None\n",
        "Mid.other = None\n",
        "spare = Mid.step\n",
        "mid = Mid()\nmid.step = None\n",
        "def patch(Mid):\n    Mid.step = None\n",
    ],
    ids=["on-a-class-apart", "another-name", "read-not-set", "on-an-object", "on-a-parameter-of-the-name"],
)
def test_what_does_not_set_the_name_on_a_class_of_the_chain_does_not_stop_the_walk(tmp_path: Path, statement: str) -> None:
    body = "class Mid(Base):\n    pass\n\n\n" + CHILD_RUN + "\n\n" + statement
    assert _one_file(tmp_path, body, "Child.run") == {"Base.step"}


def test_a_name_set_on_a_class_stops_super_too(tmp_path: Path) -> None:
    """Unlike an instance attribute, it is on the class, where `super()` looks."""
    body = "class Mid(Base):\n    pass\n\n\n" + CHILD_STEP + "\n\nMid.step = None\n"
    assert _one_file(tmp_path, body, "Child.step") == BY_NAME | {"Child.step"}


@pytest.mark.parametrize(
    "method",
    [
        "    @classmethod\n    def arm(cls):\n        cls.step = None\n",
        "    def __new__(cls):\n        cls.step = None\n        return super().__new__(cls)\n",
        "    def __class_getitem__(cls, item):\n        cls.step = None\n",
    ],
    ids=["a-class-method", "new", "class-getitem"],
)
@pytest.mark.parametrize("child", [CHILD_RUN, CHILD_STEP], ids=["self", "super"])
def test_a_name_set_on_the_class_by_a_class_method_is_bound_by_name(tmp_path: Path, method: str, child: str) -> None:
    """`cls` is the class or one derived from it: `Child.arm()` sets `Child.step`."""
    body = "class Mid(Base):\n" + method + "\n\n" + child
    caller = "Child.run" if child == CHILD_RUN else "Child.step"
    assert _one_file(tmp_path, body, caller) - {"Child.step"} == BY_NAME


def test_an_init_subclass_that_sets_the_name_is_read_on_the_class_itself(tmp_path: Path) -> None:
    """On the class itself an `__init_subclass__` says nothing, but `cls.step
    = ...` in it is still a class method setting the name."""
    body = (
        "class Child(Base):\n"
        "    def __init_subclass__(cls, **kwargs):\n        cls.step = None\n\n"
        "    def run(self):\n        return self.step()\n"
    )
    assert _one_file(tmp_path, body, "Child.run") == BY_NAME


@pytest.mark.parametrize(
    "method",
    [
        "    @staticmethod\n    def arm(cls):\n        cls.step = None\n",
        "    @classmethod\n    def arm(cls):\n        cls.other = None\n",
        "    @classmethod\n    def arm(cls, step):\n        return cls.step == step\n",
    ],
    ids=["a-static-method", "another-name", "read-not-set"],
)
def test_what_does_not_set_the_name_on_the_class_does_not_stop_the_walk(tmp_path: Path, method: str) -> None:
    body = "class Mid(Base):\n" + method + "\n\n" + CHILD_RUN
    assert _one_file(tmp_path, body, "Child.run") == {"Base.step"}


def test_cls_in_new_is_not_the_instance(tmp_path: Path) -> None:
    """`__new__` is handed the class. A call on it is bound by name, like a
    call on `cls` in a class method."""
    body = "class Child(Base):\n    def __new__(cls):\n        cls.step()\n        return super().__new__(cls)\n"
    assert _one_file(tmp_path, body, "Child.__new__") >= BY_NAME


# --- a class NAME that the scope binds more than once ---------------------------
#
# `Mid = marks(Mid)` is a class decorator under another spelling, and a class
# written twice (`if FLAG: class Mid ... else: class Mid ...`) is two classes
# under one name and, in the graph, one node. Either way the name does not say
# which class it is: nothing is bound by class in such a class, and for a class
# derived from it, it is a base that was not read.

NO_OTHER = TWO.replace("class Other:\n    def step(self):\n        return 'other'\n\n\n", "")
#: `Mid` written twice. Only the first has a `step`.
TWICE = (
    "FLAG = ()\n\n\n"
    "if FLAG:\n    class Mid(Base):\n        def step(self):\n            return 'mid'\n"
    "else:\n    class Mid(Base):\n        pass\n\n\n"
)


@pytest.mark.parametrize(
    "again",
    ["Mid = marks(Mid)\n", "del Mid\n", "for Mid in ():\n    pass\n", "def swap():\n    global Mid\n    Mid = None\n"],
    ids=["assigned", "deleted", "a-loop-target", "assigned-as-a-global"],
)
def test_a_call_in_a_class_whose_name_is_bound_again_is_bound_by_name(tmp_path: Path, again: str) -> None:
    """What `marks` returns may have a `step` that no `def` wrote."""
    body = MARKS + "class Mid(Base):\n    def run(self):\n        return self.step()\n\n\n" + again
    assert _one_file(tmp_path, body, "Mid.run") == BY_NAME


def test_super_in_a_class_whose_name_is_bound_again_is_bound_by_name(tmp_path: Path) -> None:
    body = MARKS + "class Mid(Base):\n    def step(self):\n        return super().step()\n\n\nMid = marks(Mid)\n"
    assert _one_file(tmp_path, body, "Mid.step") - {"Mid.step"} == BY_NAME


@pytest.mark.parametrize("child", [CHILD_RUN, CHILD_STEP], ids=["self", "super"])
def test_a_class_written_twice_is_not_read_by_a_class_derived_from_it(tmp_path: Path, child: str) -> None:
    """Which `Mid` it derives from is decided when the file runs. One has a
    `step`; under the other the call goes on to `Base.step`."""
    caller = "Child.run" if child == CHILD_RUN else "Child.step"
    assert _one_file(tmp_path, TWICE + child, caller, head=NO_OTHER) - {"Child.step"} == {"Base.step", "Mid.step"}


def test_a_call_in_a_class_written_twice_is_bound_by_name(tmp_path: Path) -> None:
    """The two classes are one node. The `step` of the first is not a method
    of the second, where the call is written."""
    body = (
        "FLAG = ()\n\n\n"
        "if FLAG:\n    class Mid(Base):\n        def step(self):\n            return 'mid'\n"
        "else:\n    class Mid(Base):\n        def run(self):\n            return self.step()\n"
    )
    assert _one_file(tmp_path, body, "Mid.run", head=NO_OTHER) == {"Base.step", "Mid.step"}


def test_super_in_a_class_written_twice_is_bound_by_name(tmp_path: Path) -> None:
    """The two statements name different bases, and the node has one list."""
    body = (
        "FLAG = ()\n\n\n"
        "if FLAG:\n    class Mid(Other):\n        pass\n"
        "else:\n    class Mid(Base):\n        def go(self):\n            return super().step()\n"
    )
    assert _one_file(tmp_path, body, "Mid.go") == BY_NAME


@pytest.mark.parametrize(
    "tail",
    [
        "class Mid(Base):\n    def step(self):\n        return 'mid'\n\n\nMid = Base\n",
        TWICE,
    ],
    ids=["bound-again", "written-twice"],
)
@pytest.mark.parametrize("child", [CHILD_RUN, CHILD_STEP], ids=["self", "super"])
def test_a_base_imported_from_a_file_that_binds_the_name_again_is_not_read(tmp_path: Path, tail: str, child: str) -> None:
    """The import names `Mid`. What `Mid` is when the importing file runs is
    for the file that binds it twice to say, and it does not."""
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "base.py", NO_OTHER + tail)
    _write(tmp_path / "pkg" / "use.py", "from pkg.base import Mid\n\n\n" + child)
    caller = "Child.run" if child == CHILD_RUN else "Child.step"
    assert _called(_result(tmp_path), caller) - {"Child.step"} == {"Base.step", "Mid.step"}


def test_another_name_bound_again_says_nothing_about_the_class(tmp_path: Path) -> None:
    body = MARKS + "class Mid(Base):\n    pass\n\n\nOther = marks(Other)\nspare = Mid\n\n\n" + CHILD_RUN
    assert _one_file(tmp_path, body, "Child.run") == {"Base.step"}


def test_a_name_bound_once_in_each_of_two_scopes_is_bound_once(tmp_path: Path) -> None:
    """`Inner` is written in two class bodies. Each scope binds it once."""
    body = (
        "class First:\n    class Inner(Base):\n        def run(self):\n            return self.step()\n\n\n"
        "class Second:\n    class Inner(Other):\n        def go(self):\n            return self.step()\n"
    )
    _write(tmp_path / "app" / "code.py", TWO + body)
    result = _result(tmp_path)
    assert _called(result, "First.Inner.run") == {"Base.step"}
    assert _called(result, "Second.Inner.go") == {"Other.step"}


@pytest.mark.parametrize("child", [CHILD_RUN, CHILD_STEP], ids=["self", "super"])
def test_a_def_under_a_global_declaration_is_not_the_class_s_method(tmp_path: Path, child: str) -> None:
    """`global step` in a class body: the `def` binds the MODULE's `step`,
    `Mid` has none, and the call goes on to `Base.step`."""
    body = "class Mid(Base):\n    global step\n\n    def step(self):\n        return 'mid'\n\n\n" + child
    caller = "Child.run" if child == CHILD_RUN else "Child.step"
    assert _one_file(tmp_path, body, caller, head=NO_OTHER) - {"Child.step"} == {"Base.step", "Mid.step"}


def test_a_def_under_a_nonlocal_declaration_is_not_the_class_s_method(tmp_path: Path) -> None:
    body = (
        "def make():\n    step = None\n\n"
        "    class Mid(Base):\n        nonlocal step\n\n        def step(self):\n            return 'mid'\n\n"
        "    class Child(Mid):\n        def run(self):\n            return self.step()\n\n"
        "    return Child\n"
    )
    assert _one_file(tmp_path, body, "make.Child.run", head=NO_OTHER) == {"Base.step", "make.Mid.step"}


def test_a_road_seen_in_the_first_build_is_seen_from_the_cache(tmp_path: Path) -> None:
    """How a class is made, and what is set on it, travel through the
    extraction cache with the bases."""
    body = (
        MARKS + META
        + "@marks\nclass Marked(Base):\n    pass\n\n\n"
        + "class Keyed(Base, metaclass=Meta):\n    pass\n\n\n"
        + "class Set(Base):\n    pass\n\n\n"
        + "class Plain(Base):\n    pass\n\n\n"
        + "class Again(Base):\n    def run(self):\n        return self.step()\n\n\n"
        + "Again = marks(Again)\n\n\n"
        + "".join(
            f"class Under{name}({name}):\n    def run(self):\n        return self.step()\n\n\n"
            for name in ("Marked", "Keyed", "Set", "Plain")
        )
        + "Set.step = None\n"
    )
    _write(tmp_path / "app" / "code.py", TWO + body)
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite", typescript_resolver="disabled")

    def build():
        cache = Cache(cfg.cache_dir, cfg.cache_version, engine="a" * 64)
        return extract_all(collect_files(tmp_path, cfg), cfg, cache)

    first = build()
    assert list(cfg.cache_dir.rglob("*.json")), "the first build cached nothing"
    for result in (first, build()):
        for name in ("Marked", "Keyed", "Set"):
            assert _called(result, f"Under{name}.run") == BY_NAME, name
        assert _called(result, "Again.run") == BY_NAME
        assert _called(result, "UnderPlain.run") == {"Base.step"}


# --- what runs for an object of a SUBCLASS is not followed: limits, pinned ---------


def test_an_override_in_a_subclass_is_not_listed(tmp_path: Path) -> None:
    """For a `Sub`, `self.step()` in `Base.run` runs `Sub.step`. That edge is
    MISSING. Before this change it was there by accident of the name, beside
    `Other.step`, which was false."""
    body = "class Sub(Base):\n    def step(self):\n        return 'sub'\n"
    head = TWO.replace("        return 'base'\n", "        return 'base'\n\n    def run(self):\n        return self.step()\n")
    assert _one_file(tmp_path, body, "Base.run", head=head) == {"Base.step"}


def test_super_is_bound_for_the_class_it_is_written_in(tmp_path: Path) -> None:
    """`Join(Left, Right)`: for a `Join`, `super().step()` in `Left` runs
    `Right.step`, not `Root.step`. That edge is MISSING."""
    head = (
        "class Root:\n    def step(self):\n        return 'root'\n\n\n"
        "class Left(Root):\n    def step(self):\n        return super().step()\n\n\n"
        "class Right(Root):\n    def step(self):\n        return 'right'\n\n\n"
    )
    body = "class Join(Left, Right):\n    pass\n"
    assert _one_file(tmp_path, body, "Left.step", head=head) == {"Root.step"}


# --- what the two passes hand each other is not left in the graph -------------------


def test_no_tag_reaches_a_node_or_an_edge(tmp_path: Path) -> None:
    body = (
        MARKS + META
        + "@marks\nclass Child(Base, metaclass=Meta):\n"
        "    def __init__(self, f):\n"
        "        self.hook = f\n\n"
        "    @classmethod\n    def arm(cls):\n        cls.armed = True\n\n"
        "    def run(self):\n"
        "        return self.step(), super().step(), self.missing()\n"
        "\n\nChild.late = None\n"
    )
    _write(tmp_path / "app" / "code.py", TWO + body)
    result = _result(tmp_path)
    assert result.nodes and result.edges
    for record in [*result.nodes, *result.edges]:
        assert not [key for key in record if key.startswith("_")], record


def test_a_second_build_from_the_cache_binds_the_same_calls(tmp_path: Path) -> None:
    """The tags travel through the extraction cache: a file that did not
    change is not extracted again, and its calls must be bound the same."""
    body = (
        "class Child(Base):\n"
        "    def run(self):\n"
        "        return self.step(), Other().step()\n\n"
        "    def step(self):\n"
        "        return super().step()\n"
    )
    _write(tmp_path / "app" / "code.py", TWO + body)
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite", typescript_resolver="disabled")

    def build():
        cache = Cache(cfg.cache_dir, cfg.cache_version, engine="a" * 64)
        return extract_all(collect_files(tmp_path, cfg), cfg, cache)

    first = build()
    assert list(cfg.cache_dir.rglob("*.json")), "the first build cached nothing"
    second = build()
    for result in (first, second):
        assert _called(result, "Child.run") == {"Child.step", "Other", "Other.step"}
        assert _called(result, "Child.step") == {"Base.step"}


# --- what is bound is what Python runs ------------------------------------------------
#
# Small random hierarchies, each written to a file that is read twice: by
# Python, which makes the classes and calls the two methods of each, and by
# the extractor. A base written through an alias (`H2 = K2`) is one the
# extractor cannot read, while Python knows exactly what it is. So what stands
# behind an unknown base is real here: it derives from classes the extractor
# does see, and it may have a `step` of its own.


_CLASSES = (4, 7)  # the classes `K` of a hierarchy, fewest and most
_BASES = (1, 2, 2, 3)  # how many bases a class is given, where that many classes exist
_UNKNOWN = 0.25  # the share of bases the extractor cannot read
#: How a class binds `step`, with the weight of each: a `def`, `step = None`,
#: not at all, and a `def` under an `if` that runs it or does not.
_BINDINGS = {"def": 8, "other": 2, "none": 10, "if True": 1, "if False": 1}
_PLAIN = 0.1  # the share kept of hierarchies whose order the readable bases alone give
_ROADS = 0.08  # the share of draws given a `step` that no `def` in a class body wrote
#: Roads that change one class, and roads that change the classes derived from it.
_CLASS_ROADS = ("decorator", "assigned", "deleted")
_LINE_ROADS = ("metaclass", "init_subclass", "getattribute", "class_method")

#: Written at the head of a file that takes one of the roads.
_ROAD_HEAD = (
    "def _made(label):\n"
    "    return lambda self: label\n"
    "\n\n"
    "def _sets(label):\n"
    "    def decorate(cls):\n"
    "        cls.step = _made(label)\n"
    "        return cls\n"
    "    return decorate\n"
    "\n\n"
    "class _Meta(type):\n"
    "    def __new__(mcs, name, bases, namespace, **kwargs):\n"
    "        made = super().__new__(mcs, name, bases, namespace, **kwargs)\n"
    "        if namespace.get('_hit'):\n"
    "            made.step = _made('!the metaclass, on ' + name)\n"
    "        return made\n"
    "\n\n"
)
_ROAD_BODY = {
    "init_subclass": (
        "    def __init_subclass__(cls, **kwargs):\n"
        "        super().__init_subclass__(**kwargs)\n"
        "        if vars(cls).get('_hit'):\n"
        "            setattr(cls, 'step', _made('!__init_subclass__, on ' + cls.__name__))\n\n"
    ),
    "getattribute": (
        "    def __getattribute__(self, name):\n"
        "        if name == 'step':\n"
        "            return lambda: '!__getattribute__'\n"
        "        return super().__getattribute__(name)\n\n"
    ),
    "class_method": (
        "    @classmethod\n"
        "    def arm(cls):\n"
        "        cls.step = _made('!a class method, on ' + cls.__name__)\n\n"
    ),
}


@dataclasses.dataclass
class _Hierarchy:
    source: str
    #: an unreadable base changes the ORDER of the classes that can be read
    reordered: bool
    #: the classes whose body has a `def step`: what a call bound by name reaches
    defs: set[str]
    #: the roads taken, by kind
    roads: list[str]
    #: a class has its `def step` under an `if`
    conditional: bool


def _hierarchy(rng: random.Random) -> _Hierarchy:
    """Source for a few classes `K0`, `K1`, ...: each binds `step` by a `def`,
    by an assignment, by a `def` under an `if`, or not at all, and derives
    from up to three classes in an order Python accepts. A base is an earlier
    `K`, or a class `Z` of its
    own that derives from one or two earlier `K`, may have a `step` too, and
    is written through an alias, which the extractor cannot read.

    Some hierarchies also give a class a `step` that no `def` in its body
    wrote: a decorator on the class sets it, or a metaclass, a base's
    `__init_subclass__`, a class method, an assignment after the class; a
    `__getattribute__` answers for it; or the `def` is deleted. Python runs
    all of it, so what the call reaches is known here whatever the road.

    One thing is kept out, because the extractor cannot see it and says so: a
    road that works on the classes DERIVED from the one it is written on is
    written only on a class that no `K` reaches through an unreadable base
    alone."""
    made: dict[str, type] = {}
    read: dict[str, type | None] = {}
    known: list[str] = []
    hidden = 0
    reordered = False
    drawn: list[tuple[list[str], list[tuple[str, list[str], bool]], str]] = []
    for index in range(rng.randint(*_CLASSES)):
        bases: list[str] = []
        behind: list[tuple[str, list[str], bool]] = []
        for _attempt in range(8):
            bases, behind, trial = [], [], dict(made)
            try:
                for pick in rng.sample(known, min(len(known), rng.choice(_BASES))):
                    if rng.random() < _UNKNOWN:
                        name = f"Z{hidden + len(behind)}"
                        parents = rng.sample(known, min(len(known), rng.choice((1, 1, 2))))
                        trial[name] = type(name, tuple(trial[parent] for parent in parents), {})
                        behind.append((name, parents, rng.random() < 0.5))
                        bases.append(name)
                    else:
                        bases.append(pick)
                trial[f"K{index}"] = type(f"K{index}", tuple(trial[base] for base in bases), {})
            except TypeError:  # an order of bases Python refuses
                continue
            made = trial
            break
        else:
            bases, behind = [], []
            made[f"K{index}"] = type(f"K{index}", (), {})
        hidden += len(behind)
        # The same class over its readable bases only, made by Python as well.
        readable = [read[base] for base in bases if base[0] == "K"]
        alone: type | None = None
        if None not in readable:
            try:
                alone = type(f"K{index}", tuple(base for base in readable if base is not None), {})
            except TypeError:  # the readable bases alone admit no order
                alone = None
        read[f"K{index}"] = alone
        seen = [k.__name__ for k in alone.__mro__] if alone is not None else None
        reordered = reordered or seen != [k.__name__ for k in made[f"K{index}"].__mro__ if seen and k.__name__ in seen]
        drawn.append((bases, behind, rng.choices(list(_BINDINGS), weights=list(_BINDINGS.values()))[0]))
        known.append(f"K{index}")

    # The roads. `line[k]` is every class Python looks in for an object of `k`;
    # `stated[k]` is the part of it the extractor reaches through bases it reads.
    line = {k: {c.__name__ for c in made[k].__mro__} for k in known}
    stated: dict[str, set[str]] = {}
    for k, (bases, _behind, _binding) in zip(known, drawn, strict=True):
        stated[k] = {k}.union(*(stated[base] for base in bases if base[0] == "K"))
    in_sight = [r for r in known if all(r in stated[k] for k in known if r in line[k])]
    decorated: dict[str, str] = {}
    keyword: set[str] = set()
    extra: dict[str, list[str]] = collections.defaultdict(list)
    hit: set[str] = set()
    after: list[str] = []
    roads: list[str] = []
    for _road in range(rng.choice((1, 1, 2)) if rng.random() < _ROADS else 0):
        kind = rng.choice(_CLASS_ROADS + _LINE_ROADS)
        if kind in _LINE_ROADS and not in_sight:
            continue
        target = rng.choice(in_sight if kind in _LINE_ROADS else known)
        below = [k for k in known if target in line[k]]
        if kind == "decorator":
            decorated[target] = f"@_sets('!a decorator, on {target}')\n"
        elif kind == "assigned":
            after.append(f"{target}.step = _made('!an assignment, on {target}')\n")
        elif kind == "deleted":
            if drawn[known.index(target)][2] in ("none", "if False") or f"del {target}.step\n" in after:
                continue
            after.append(f"del {target}.step\n")
        elif kind == "metaclass":
            keyword.add(target)
            hit.update(k for k in below if rng.random() < 0.6)
        elif kind == "init_subclass":
            if kind in extra[target]:
                continue
            extra[target].append(kind)
            hit.update(k for k in below if k != target and rng.random() < 0.6)
        elif kind == "class_method":
            if kind not in extra[target]:
                extra[target].append(kind)
            after.append(f"{rng.choice(below)}.arm()\n")
        else:
            if kind in extra[target]:
                continue
            extra[target].append(kind)
        roads.append(kind)

    text: list[str] = [_ROAD_HEAD] if roads else []
    defs: set[str] = set()
    conditional = False
    for k, (bases, behind, binding) in zip(known, drawn, strict=True):
        for name, parents, has_step in behind:
            text.append(f"class {name}({', '.join(parents)}):\n")
            text.append(f"    def step(self):\n        return '{name}'\n\n\n" if has_step else "    pass\n\n\n")
            text.append(f"H{name[1:]} = {name}\n\n\n")
            defs.update({name} if has_step else ())
        written = [f"H{base[1:]}" if base[0] == "Z" else base for base in bases]
        written += ["metaclass=_Meta"] if k in keyword else []
        text.append(decorated.get(k, ""))
        text.append(f"class {k}({', '.join(written)}):\n" if written else f"class {k}:\n")
        if k in hit:
            text.append("    _hit = True\n\n")
        if binding == "def":
            text.append(f"    def step(self):\n        return '{k}'\n\n")
            defs.add(k)
        elif binding == "other":
            text.append("    step = None\n\n")
        elif binding != "none":
            text.append(f"    {binding}:\n        def step(self):\n            return '{k}'\n\n")
            defs.add(k)
            conditional = True
        text.extend(_ROAD_BODY[kind] for kind in extra[k])
        text.append("    def via_self(self):\n        return self.step()\n\n")
        text.append("    def via_super(self):\n        return super().step()\n\n\n")
    text.extend(after)
    return _Hierarchy("".join(text), reordered, defs, roads, conditional)


def _hierarchies(rng: random.Random, count: int) -> list[_Hierarchy]:
    """`count` hierarchies. Drawn evenly, few have an order an unreadable base
    changes, and those are the ones a walk over the readable bases alone gets
    wrong. So every such draw is kept, every draw that takes a road, and a
    small share of the others."""
    kept: list[_Hierarchy] = []
    while len(kept) < count:
        drawn = _hierarchy(rng)
        if drawn.reordered or drawn.roads or rng.random() < _PLAIN:
            kept.append(drawn)
    return kept


def _python_runs(path: Path) -> dict[str, str | None]:
    """Run the file. For each `K<i>.via_self` and `K<i>.via_super`: what the
    `step` its call ran returned, or None where it ran none. A `def step`
    returns the name of its class; a `step` no `def` wrote, a text that
    starts with `!`."""
    classes = {value.__name__: value for value in runpy.run_path(str(path)).values() if isinstance(value, type)}
    runs: dict[str, str | None] = {}
    for name, made in classes.items():
        for caller in ("via_self", "via_super") if name[0] == "K" else ():
            try:
                runs[f"{name}.{caller}"] = getattr(made(), caller)()
            except (AttributeError, TypeError):  # no `step`, or `step = None` called
                runs[f"{name}.{caller}"] = None
    return runs


def _engine_binds(result) -> dict[tuple[str, str], set[str]]:
    """(file name, caller) -> the definitions its call edges end at, by qualified name."""
    names = {
        n["id"]: (n["source_file"].rsplit("/", 1)[-1], n.get("qualname", n["name"]))
        for n in result.nodes
        if n.get("kind") in ("function", "class")
    }
    binds: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    for e in result.edges:
        if e["relation"] == "calls" and e["source"] in names and e["target"] in names:
            assert names[e["source"]][0] == names[e["target"]][0], "a call bound into another file"
            binds[names[e["source"]]].add(names[e["target"]][1])
    return binds


def _compare_with_python(root: Path, seed: int, count: int) -> collections.Counter[str]:
    """Write `count` hierarchies under `root` and hold every call the
    extractor binds against the method Python ran. A call must be bound either
    to exactly that method, or by name: to every `def step` of its file when
    there are one to three, and to nothing otherwise."""
    rng = random.Random(seed)
    cases = []
    for number, drawn in enumerate(_hierarchies(rng, count)):
        _write(root / "app" / f"h{number:05}.py", drawn.source)
        cases.append((root / "app" / f"h{number:05}.py", drawn))
    binds = _engine_binds(_result(root))
    tally: collections.Counter[str] = collections.Counter()
    for path, drawn in cases:
        by_name = {f"{name}.step" for name in drawn.defs} if 1 <= len(drawn.defs) <= 3 else set()
        unknown = "(H" in drawn.source or ", H" in drawn.source
        tally["hierarchies"] += 1
        tally["hierarchies whose order an unreadable base changes"] += drawn.reordered
        tally["hierarchies that take a road"] += bool(drawn.roads)
        ran_off_road: set[str] = set()
        for caller, returned in _python_runs(path).items():
            # What ran, as the method a `def` wrote; None where none did.
            ran = f"{returned}.step" if returned in drawn.defs else None
            if returned is not None and ran is None:
                assert returned.startswith("!"), f"{path.name}: {caller} ran {returned!r}\n{drawn.source}"
                ran_off_road.add(returned.split(",")[0])
            bound = binds.get((path.name, caller), set())
            tally["calls"] += 1
            if bound == by_name:
                tally["by name"] += 1
                tally["by name, where Python ran another set than that"] += ran is not None and {ran} != by_name
                tally["by name, where what ran is a method no def wrote"] += returned is not None and ran is None
                continue
            assert ran is not None and bound == {ran}, (
                f"seed {seed}, {path.name}: {caller} is bound to {sorted(bound)}, "
                f"Python ran {returned!r}, by name is {sorted(by_name)}\n{drawn.source}"
            )
            tally["by class"] += 1
            tally["by class, with an unknown base in the file"] += unknown
            tally["by class, through super"] += caller.endswith("via_super")
            tally["by class, in a file that takes a road"] += bool(drawn.roads)
        for road in ran_off_road:
            tally[f"hierarchies where a call ran {road[1:]}"] += 1
        if "deleted" in drawn.roads:
            tally["hierarchies with a def deleted"] += 1
        tally["hierarchies with a def under an if"] += drawn.conditional
    return tally


#: Every road must have been RUN in some hierarchy, or it was not compared.
_ROADS_RUN = (
    "a decorator", "an assignment", "the metaclass", "__init_subclass__", "__getattribute__", "a class method",
)


def test_what_is_bound_is_what_python_runs(tmp_path: Path) -> None:
    tally = _compare_with_python(tmp_path, seed=73, count=400)
    # Nothing was compared unless calls were bound by class here, beside bases
    # that cannot be read and through `super()`, others were left to the name,
    # and the hierarchies a careless reading gets wrong were among them.
    assert tally["by class"] >= 400, tally
    assert tally["by class, with an unknown base in the file"] >= 300, tally
    assert tally["by class, through super"] >= 100, tally
    assert tally["by class, in a file that takes a road"] >= 50, tally
    assert tally["by name, where Python ran another set than that"] >= 200, tally
    assert tally["by name, where what ran is a method no def wrote"] >= 100, tally
    assert tally["hierarchies whose order an unreadable base changes"] >= 30, tally
    for road in _ROADS_RUN:
        assert tally[f"hierarchies where a call ran {road}"] >= 5, (road, tally)
    assert tally["hierarchies with a def deleted"] >= 5, tally
    assert tally["hierarchies with a def under an if"] >= 30, tally
