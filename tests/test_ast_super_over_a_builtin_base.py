"""`super().m()` has no edge where the method that runs is a builtin's (#106).

`class StepError(AppError)` with `AppError(RuntimeError)`: for a `StepError`,
`super().__init__(message)` runs `RuntimeError.__init__`, which is no method
of the repository. The binding by class (#73) reads a builtin base as one it
cannot read; it found no class of the repository that binds `__init__`, and
left the call to the name, so it was a call to every `__init__` in the
caller's file and the files it imports, up to three, at `decision_grade`.

A call of a builtin has no edge, and this one now has none either, where:

* the call is a bare `super().m()` in a method;
* no class of the chain that the source states binds `m`;
* every base along the chain that is not a class of the repository is a
  builtin class the file does not bind (no star import);
* and one of those builtins, or a class it derives from, has `m` in its own
  namespace (`object` among them, so `__init__` and `__eq__` count).

Everything else is as before. A name no builtin of the chain has (a mixin
calling what its host provides) is bound by name, a base that cannot be read
beside the builtin keeps the call to the name, `self.m()` is not changed, and
a builtin base is still not placed in the order of the classes that were read.
"""
from __future__ import annotations

import builtins
import collections
import random
import runpy
from pathlib import Path

import pytest

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.graph import build_graph
from graphite.ingest import collect_files
from graphite.query import query

#: The issue's fixture.
ERRS = (
    "class AppError(RuntimeError):\n    pass\n\n\n"
    "class StepError(AppError):\n    def __init__(self, message):\n        super().__init__(message)\n\n\n"
    "class DirectError(RuntimeError):\n    def __init__(self, message):\n        super().__init__(message)\n\n\n"
    "class Runner:\n    def __init__(self, root):\n        self.root = root\n"
)
FAMILY = (
    "class Parent:\n    def __init__(self, m):\n        self.m = m\n\n\n"
    "class Child(Parent):\n    def __init__(self, m):\n        super().__init__(m)\n"
)
#: Two classes of the repository with an `__init__`, a `step` and a `with_traceback`.
TWO = (
    "class Alpha:\n"
    "    def __init__(self):\n        self.a = 1\n\n"
    "    def step(self):\n        return 'alpha'\n\n"
    "    def with_traceback(self, tb):\n        return tb\n\n\n"
    "class Beta:\n"
    "    def __init__(self):\n        self.b = 2\n\n"
    "    def step(self):\n        return 'beta'\n\n"
    "    def with_traceback(self, tb):\n        return tb\n\n\n"
)


def by_name(method: str) -> set[str]:
    return {f"Alpha.{method}", f"Beta.{method}"}


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


def test_the_issue_s_super_calls_have_no_edge_and_the_control_keeps_its_own(tmp_path: Path) -> None:
    _write(tmp_path / "errs.py", ERRS)
    _write(tmp_path / "family.py", FAMILY)
    result = _result(tmp_path)
    graph = build_graph(result.nodes, result.edges)

    assert query(graph, "callers Runner.__init__")["callers"] == []
    assert _called(result, "StepError.__init__") == set()
    assert _called(result, "DirectError.__init__") == set()
    assert _called(result, "Child.__init__") == {"Parent.__init__"}


def test_a_builtin_base_written_straight_has_no_edge(tmp_path: Path) -> None:
    body = "class Err(RuntimeError):\n    def __init__(self):\n        super().__init__('x')\n"
    assert _one_file(tmp_path, body, "Err.__init__") == set()


def test_a_method_object_has_is_a_builtin_s_too(tmp_path: Path) -> None:
    """`__eq__` is not in `RuntimeError`'s namespace; it is in `object`'s,
    which every builtin derives from."""
    head = TWO.replace("    def step(self):\n        return 'alpha'\n", "    def __eq__(self, other):\n        return True\n")
    head = head.replace("    def step(self):\n        return 'beta'\n", "    def __eq__(self, other):\n        return True\n")
    body = "class Err(RuntimeError):\n    def same(self, other):\n        return super().__eq__(other)\n"
    assert _one_file(tmp_path, body, "Err.same", head=head) == set()


def test_a_name_no_builtin_of_the_chain_has_is_still_bound_by_name(tmp_path: Path) -> None:
    body = "class Err(RuntimeError):\n    def run(self):\n        return super().step()\n"
    assert _one_file(tmp_path, body, "Err.run") == by_name("step")


def test_a_name_only_the_metaclass_has_is_not_a_builtin_s(tmp_path: Path) -> None:
    """`hasattr(RuntimeError, "mro")` is true because `type` defines `mro`;
    `super()` looks along the instance's classes and never finds it there."""
    head = "class Alpha:\n    def mro(self):\n        return 1\n\n\nclass Beta:\n    def mro(self):\n        return 2\n\n\n"
    body = "class Err(RuntimeError):\n    def run(self):\n        return super().mro()\n"
    assert _one_file(tmp_path, body, "Err.run", head=head) == {"Alpha.mro", "Beta.mro"}


def test_a_class_of_the_repository_that_binds_the_name_is_still_the_one_bound(tmp_path: Path) -> None:
    body = (
        "class Mid(RuntimeError):\n    def __init__(self):\n        super().__init__('mid')\n\n\n"
        "class Low(Mid):\n    def __init__(self):\n        super().__init__()\n"
    )
    result_of = _one_file(tmp_path, body, "Low.__init__")
    assert result_of == {"Mid.__init__"}


def test_a_base_that_cannot_be_read_beside_the_builtin_keeps_the_name(tmp_path: Path) -> None:
    """`Unknown` may define `__init__` itself, before or after `RuntimeError`."""
    body = "from elsewhere import Unknown\n\n\nclass Err(Unknown, RuntimeError):\n    def run(self):\n        return super().__init__()\n"
    assert _one_file(tmp_path, body, "Err.run") == by_name("__init__")


@pytest.mark.parametrize(
    "shadow",
    ["class RuntimeError:\n    pass\n\n\n", "from helpers import *\n\n\n"],
    ids=["the file defines it", "a star import may bind it"],
)
def test_a_builtin_s_name_the_file_may_bind_is_not_the_builtin(tmp_path: Path, shadow: str) -> None:
    body = shadow + "class Err(RuntimeError):\n    def run(self):\n        return super().__init__()\n"
    assert by_name("__init__") <= _one_file(tmp_path, body, "Err.run")


def test_a_builtin_s_name_the_file_assigns_is_the_class_assigned(tmp_path: Path) -> None:
    body = "RuntimeError = Alpha\n\n\nclass Err(RuntimeError):\n    def run(self):\n        return super().__init__()\n"
    assert _one_file(tmp_path, body, "Err.run") == {"Alpha.__init__"}


def test_self_over_a_builtin_base_is_not_changed(tmp_path: Path) -> None:
    """A subclass may override what `self.m()` reaches: that call keeps the name."""
    body = (
        "class Err(RuntimeError):\n"
        "    def run(self):\n        return self.with_traceback(None)\n\n"
        "    def up(self):\n        return super().with_traceback(None)\n"
    )
    _write(tmp_path / "app" / "code.py", TWO + body)
    result = _result(tmp_path)
    assert _called(result, "Err.run") == by_name("with_traceback")
    assert _called(result, "Err.up") == set()


def test_a_class_with_no_base_is_not_changed(tmp_path: Path) -> None:
    """A mixin's `super().__init__()` reaches the next class of its HOST, so
    nothing is stated for it, and `object` is not read as its base."""
    body = "class Mixin:\n    def __init__(self):\n        super().__init__()\n"
    assert _one_file(tmp_path, body, "Mixin.__init__") == {"Alpha.__init__", "Beta.__init__", "Mixin.__init__"}


def test_super_with_arguments_is_not_changed(tmp_path: Path) -> None:
    body = "class Err(RuntimeError):\n    def run(self):\n        return super(Err, self).__init__()\n"
    assert by_name("__init__") <= _one_file(tmp_path, body, "Err.run")


# --- what is dropped is where a builtin's method runs ---------------------------------
#
# Random hierarchies over builtin bases and classes of the file, each read by
# Python, which says whose method `super().m()` reaches for an object of the
# class the call is written in, and by the extractor.

_BUILTIN_BASES = ("Exception", "RuntimeError", "KeyError", "ValueError", "dict", "list")
_NAMES = ("__init__", "step", "with_traceback", "get", "__repr__")


def _hierarchy(rng: random.Random) -> str:
    made: dict[str, type] = {}
    text: list[str] = []
    for index in range(rng.randint(3, 6)):
        name = f"K{index}"
        for _attempt in range(8):
            bases: list[str] = []
            for _ in range(rng.choice((0, 1, 1, 2))):
                bases.append(rng.choice(list(made)) if made and rng.random() < 0.6 else rng.choice(_BUILTIN_BASES))
            bases = list(dict.fromkeys(bases))
            defines = [method for method in _NAMES if rng.random() < 0.25]
            try:
                parents = tuple(made[base] if base in made else getattr(builtins, base) for base in bases)
                made[name] = type(name, parents, {})
            except TypeError:  # an order or a layout Python refuses
                continue
            break
        else:
            bases, defines = [], []
            made[name] = type(name, (), {})
        text.append(f"class {name}({', '.join(bases)}):\n" if bases else f"class {name}:\n")
        for method in defines:
            text.append(f"    def {method}(self, *args):\n        return '{name}'\n\n")
        for number, method in enumerate(_NAMES):
            text.append(f"    def via{number}(self):\n        return super().{method}()\n\n")
        text.append("\n")
    return "".join(text)


def test_what_is_dropped_is_where_a_builtin_s_method_runs(tmp_path: Path) -> None:
    rng = random.Random(106)
    paths = []
    for number in range(150):
        path = tmp_path / "app" / f"h{number:04}.py"
        _write(path, _hierarchy(rng))
        paths.append(path)
    result = _result(tmp_path)
    names = {n["id"]: (n["source_file"].rsplit("/", 1)[-1], n.get("qualname", n["name"])) for n in result.nodes if n.get("kind") in ("function", "class")}
    binds: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    for e in result.edges:
        if e["relation"] == "calls" and e["source"] in names and e["target"] in names:
            binds[names[e["source"]]].add(names[e["target"]][1])
    tally: collections.Counter[str] = collections.Counter()
    for path in paths:
        classes = {k: v for k, v in runpy.run_path(str(path)).items() if isinstance(v, type) and k[0] == "K"}
        for cls_name, cls in classes.items():
            for number, method in enumerate(_NAMES):
                defs = {f"{k}.{method}" for k, v in classes.items() if method in vars(v)}
                named = defs if 1 <= len(defs) <= 3 else set()
                owner = next((c for c in cls.__mro__[1:] if method in vars(c)), None)
                bound = binds.get((path.name, f"{cls_name}.via{number}"), set())
                tally["calls"] += 1
                if owner is not None and owner.__name__ not in classes:
                    stated_by_file = any(c.__name__ in classes for c in cls.__mro__[1:] if method in vars(c))
                    written_builtin = any(c.__name__ not in classes and c is not object for c in cls.__mro__[1:])
                    if not stated_by_file and not written_builtin:
                        # `object` alone: the chain is a mixin's, left to the name.
                        assert bound == named, (path.name, cls_name, method, bound)
                        tally["by name, object only"] += 1
                        continue
                    if not stated_by_file:
                        assert bound == set(), (path.name, cls_name, method, bound)
                        tally["dropped where a builtin's method runs"] += 1
                        continue
                    # A class of the file binds it too, behind the builtin: the order is not read.
                    assert bound == named, (path.name, cls_name, method, bound)
                    tally["by name, a builtin first"] += 1
                    continue
                if owner is not None:
                    assert bound in ({f"{owner.__name__}.{method}"}, named), (path.name, cls_name, method, bound)
                    tally["bound by class" if bound == {f"{owner.__name__}.{method}"} else "by name"] += 1
                    continue
                assert bound == named, (path.name, cls_name, method, bound)
                tally["by name, nothing runs"] += 1
    assert tally["dropped where a builtin's method runs"] >= 200, tally
    assert tally["bound by class"] >= 100, tally
    assert tally["by name, nothing runs"] >= 50, tally
