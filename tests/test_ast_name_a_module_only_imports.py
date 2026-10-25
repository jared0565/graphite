"""A name a module only imports is followed to the file that defines it (#107).

`front.py` binds `target` by `from pkg.impl import target` and nothing else.
A call `front.target(x)` from another file, the local alias `t = front.target`
(#90), or `from pkg.front import target`, used to end at a placeholder
`pkg_front_py_target` with no source file: `callers target` left the caller
out. #87 followed such a name through a package's `__init__.py` only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.cache import Cache
from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.ingest import collect_files


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(root: Path, cache: Cache | None = None):
    cfg = Config(workers=1, cache_dir=root / ".cache" / "graphite")
    return extract_all(collect_files(root, cfg), cfg, cache)


def _cache(root: Path) -> Cache:
    cfg = Config(workers=1, cache_dir=root / ".cache" / "graphite")
    return Cache(cfg.cache_dir, cfg.cache_version, engine="test-engine")


def _def_id(result, qualname: str, source_file: str) -> str:
    ids = [
        n["id"] for n in result.nodes
        if n.get("qualname") == qualname and n.get("source_file") == source_file
    ]
    assert len(ids) == 1, f"{qualname} in {source_file}: {ids}"
    return ids[0]


def _targets(result, caller: str, source_file: str, relation: str = "calls") -> set[str]:
    source = _def_id(result, caller, source_file)
    return {e["target"] for e in result.edges if e["relation"] == relation and e["source"] == source}


def _defined(result) -> set[str]:
    return {n["id"] for n in result.nodes if n["kind"] in ("function", "class")}


_IMPL = "def target(x):\n    return x\n\n\ndef other(x):\n    return x\n"


def _package(root: Path, front: str = "from pkg.impl import target\n") -> None:
    _write(root / "pkg" / "__init__.py", "")
    _write(root / "pkg" / "impl.py", _IMPL)
    _write(root / "pkg" / "front.py", front)


_DIRECT = "from pkg import front\n\n\ndef direct(x):\n    return front.target(x)\n"


def test_the_issue_fixture_binds_the_module_attribute_call_to_the_definition(tmp_path: Path) -> None:
    _package(tmp_path)
    _write(tmp_path / "user.py", _DIRECT)
    _write(
        tmp_path / "more.py",
        "from pkg import impl\n" + "".join(f"\n\ndef f{i}(x):\n    return impl.other(x)\n" for i in range(12)),
    )
    result = _extract(tmp_path)

    assert _targets(result, "direct", "user.py") == {_def_id(result, "target", "pkg/impl.py")}
    assert _targets(result, "f0", "more.py") == {_def_id(result, "other", "pkg/impl.py")}


def test_a_local_alias_of_the_attribute_binds_to_the_definition(tmp_path: Path) -> None:
    _package(tmp_path)
    _write(
        tmp_path / "user.py",
        "from pkg import front\n\n\ndef aliased(x):\n    t = front.target\n    return t(x)\n",
    )
    result = _extract(tmp_path)

    assert _targets(result, "aliased", "user.py") == {_def_id(result, "target", "pkg/impl.py")}


def test_importing_the_name_from_the_module_binds_to_the_definition(tmp_path: Path) -> None:
    _package(tmp_path)
    _write(tmp_path / "user.py", "from pkg.front import target\n\n\ndef named(x):\n    return target(x)\n")
    result = _extract(tmp_path)

    assert _targets(result, "named", "user.py") == {_def_id(result, "target", "pkg/impl.py")}


def test_a_package_attribute_call_follows_what_the_package_imports(tmp_path: Path) -> None:
    _package(tmp_path)
    _write(tmp_path / "pkg" / "__init__.py", "from pkg.impl import target\n")
    _write(tmp_path / "user.py", "import pkg\n\n\ndef through(x):\n    return pkg.target(x)\n")
    result = _extract(tmp_path)

    assert _targets(result, "through", "user.py") == {_def_id(result, "target", "pkg/impl.py")}


def test_a_chain_through_modules_is_followed_to_the_end(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "from b import helper\n")
    _write(tmp_path / "b.py", "from c import helper\n")
    _write(tmp_path / "c.py", "def helper():\n    return 2\n")
    _write(tmp_path / "user.py", "import a\n\n\ndef use():\n    return a.helper()\n")
    result = _extract(tmp_path)

    assert _targets(result, "use", "user.py") == {_def_id(result, "helper", "c.py")}


def test_a_relative_and_aliased_import_in_the_module_is_followed(tmp_path: Path) -> None:
    _package(tmp_path, "from .impl import target as aim\n")
    _write(tmp_path / "user.py", "from pkg import front\n\n\ndef direct(x):\n    return front.aim(x)\n")
    result = _extract(tmp_path)

    assert _targets(result, "direct", "user.py") == {_def_id(result, "target", "pkg/impl.py")}


@pytest.mark.parametrize(
    "front",
    [
        pytest.param(
            "from pkg.impl import target\n\ntry:\n    from pkg.impl import other as target\nexcept ImportError:\n    pass\n",
            id="imported-twice",
        ),
        pytest.param(
            "from pkg.impl import target\n\n\ndef swap(new):\n    global target\n    target = new\n",
            id="rebound-under-global",
        ),
        pytest.param(
            "from pkg.impl import target\n\ntarget = staticmethod(target)\n",
            id="assigned-too",
        ),
    ],
)
def test_a_name_the_module_binds_some_other_way_too_is_not_followed(tmp_path: Path, front: str) -> None:
    _package(tmp_path, front)
    _write(tmp_path / "user.py", _DIRECT)
    result = _extract(tmp_path)

    targets = _targets(result, "direct", "user.py")
    assert len(targets) == 1
    assert not targets & _defined(result)


def test_a_name_the_module_defines_itself_binds_to_its_own_definition(tmp_path: Path) -> None:
    _package(tmp_path, "from pkg.impl import target\n\n\ndef target(x):\n    return -x\n")
    _write(tmp_path / "user.py", _DIRECT)
    result = _extract(tmp_path)

    assert _targets(result, "direct", "user.py") == {_def_id(result, "target", "pkg/front.py")}


def test_a_submodule_the_module_imports_is_not_followed_as_a_name(tmp_path: Path) -> None:
    _package(tmp_path, "from pkg import impl\n")
    _write(tmp_path / "pkg" / "__init__.py", "def impl():\n    return 0\n")
    _write(tmp_path / "user.py", "from pkg import front\n\n\ndef direct():\n    return front.impl()\n")
    result = _extract(tmp_path)

    # `pkg.impl` is the module pkg/impl.py, not the function the package
    # defines under the same name, so the call is not bound to that function.
    assert _def_id(result, "impl", "pkg/__init__.py") not in _targets(result, "direct", "user.py")


def test_a_cycle_terminates_and_stays_unbound(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "from b import helper\n")
    _write(tmp_path / "b.py", "from a import helper\n")
    _write(tmp_path / "user.py", "import a\n\n\ndef use():\n    return a.helper()\n")
    result = _extract(tmp_path)

    targets = _targets(result, "use", "user.py")
    assert len(targets) == 1
    assert not targets & _defined(result)


_CLASSES = (
    "class Base:\n    def step(self):\n        return 1\n\n\n"
    "class Other:\n    def step(self):\n        return 2\n"
)


def test_a_base_class_imported_through_a_module_is_inherited_and_dispatched(tmp_path: Path) -> None:
    _write(tmp_path / "impl.py", _CLASSES)
    _write(tmp_path / "front.py", "from impl import Base\n")
    _write(
        tmp_path / "user.py",
        "from front import Base\n\n\nclass Child(Base):\n    def go(self):\n        return self.step()\n",
    )
    result = _extract(tmp_path)

    base = _def_id(result, "Base", "impl.py")
    assert _targets(result, "Child", "user.py", "inherits") == {base}
    assert _targets(result, "Child.go", "user.py") == {_def_id(result, "Base.step", "impl.py")}


def test_a_stated_receiver_imported_through_a_module_binds_to_its_own_method(tmp_path: Path) -> None:
    _write(tmp_path / "impl.py", _CLASSES)
    _write(tmp_path / "front.py", "from impl import Base\n")
    _write(tmp_path / "user.py", "from front import Base\n\n\ndef make():\n    return Base().step()\n")
    result = _extract(tmp_path)

    # `Base()` is a call of the class, `.step()` one of its own method.
    assert _targets(result, "make", "user.py") == {
        _def_id(result, "Base", "impl.py"), _def_id(result, "Base.step", "impl.py"),
    }


@pytest.mark.parametrize("twin", ["pkg_front.py", "pkg_impl.py"])
def test_a_file_whose_id_collides_is_followed_by_its_own_id(tmp_path: Path, twin: str) -> None:
    _package(tmp_path)
    # `pkg_front.py` shares the plain file id `pkg_front_py` with
    # `pkg/front.py`, which imports the name; `pkg_impl.py` shares
    # `pkg_impl_py` with `pkg/impl.py`, which defines it.
    _write(tmp_path / twin, "def target(x):\n    return 0\n")
    _write(tmp_path / "user.py", _DIRECT)
    result = _extract(tmp_path)

    assert _targets(result, "direct", "user.py") == {_def_id(result, "target", "pkg/impl.py")}


def test_no_tag_of_the_follow_reaches_the_graph(tmp_path: Path) -> None:
    _package(tmp_path)
    _write(tmp_path / "user.py", _DIRECT)
    result = _extract(tmp_path)

    assert not [n["id"] for n in result.nodes if "_reexports" in n]


def test_a_second_build_from_the_cache_binds_the_same_calls(tmp_path: Path) -> None:
    _package(tmp_path)
    _write(tmp_path / "user.py", _DIRECT)
    cache = _cache(tmp_path)

    first = _extract(tmp_path, cache)
    second = _extract(tmp_path, cache)

    assert _targets(second, "direct", "user.py") == {_def_id(second, "target", "pkg/impl.py")}
    assert sorted(map(repr, first.edges)) == sorted(map(repr, second.edges))


def test_changing_only_the_module_re_points_an_unchanged_caller_through_the_cache(tmp_path: Path) -> None:
    _package(tmp_path, "def target(x):\n    return -x\n")
    _write(tmp_path / "user.py", _DIRECT)
    cache = _cache(tmp_path)

    before = _extract(tmp_path, cache)
    assert _targets(before, "direct", "user.py") == {_def_id(before, "target", "pkg/front.py")}

    caller_bytes = (tmp_path / "user.py").read_bytes()
    _write(tmp_path / "pkg" / "front.py", "from pkg.impl import target\n")
    assert (tmp_path / "user.py").read_bytes() == caller_bytes

    after = _extract(tmp_path, cache)
    assert _targets(after, "direct", "user.py") == {_def_id(after, "target", "pkg/impl.py")}
