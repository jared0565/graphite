"""A name a package re-exports is followed to the file that defines it (#87).

`from pkg import Base` names the package's `__init__.py`, which only repeats
`from pkg.base import Base`. The binding used to stop there, so the call or the
base class ended at a placeholder in `__init__.py` and `self.step()` in a
subclass had no edge at all.
"""
from __future__ import annotations

from pathlib import Path

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


def _node_id(result, qualname: str) -> str:
    ids = [n["id"] for n in result.nodes if n.get("qualname") == qualname]
    assert len(ids) == 1, f"{qualname}: {ids}"
    return ids[0]


def _call_targets(result, caller_qualname: str) -> set[str]:
    caller = _node_id(result, caller_qualname)
    return {e["target"] for e in result.edges if e["relation"] == "calls" and e["source"] == caller}


def _package(root: Path, init: str) -> None:
    _write(root / "pkg" / "__init__.py", init)
    _write(root / "pkg" / "base.py", "class Base:\n    def step(self):\n        return 1\n")
    _write(root / "pkg" / "util.py", "def helper():\n    return 2\n")


_USER = (
    "from pkg import Base, helper\n\n\n"
    "class Through(Base):\n    def run(self):\n        return self.step()\n\n\n"
    "def use():\n    return helper()\n"
)


def test_a_re_exported_function_call_binds_to_its_definition(tmp_path: Path) -> None:
    _package(tmp_path, "from pkg.base import Base\nfrom pkg.util import helper\n")
    _write(tmp_path / "through.py", _USER)
    result = _extract(tmp_path)

    assert _call_targets(result, "use") == {_node_id(result, "helper")}


def test_self_call_on_a_re_exported_base_binds_to_the_base_method(tmp_path: Path) -> None:
    _package(tmp_path, "from pkg.base import Base\nfrom pkg.util import helper\n")
    _write(tmp_path / "through.py", _USER)
    result = _extract(tmp_path)

    assert _call_targets(result, "Through.run") == {_node_id(result, "Base.step")}


def test_relative_and_aliased_re_exports_are_followed(tmp_path: Path) -> None:
    _package(tmp_path, "from .base import Base as Root\nfrom .util import helper as assist\n")
    _write(
        tmp_path / "through.py",
        "from pkg import Root, assist\n\n\n"
        "class Through(Root):\n    def run(self):\n        return self.step()\n\n\n"
        "def use():\n    return assist()\n",
    )
    result = _extract(tmp_path)

    assert _call_targets(result, "Through.run") == {_node_id(result, "Base.step")}
    assert _call_targets(result, "use") == {_node_id(result, "helper")}


def test_a_chain_of_re_exports_is_followed_to_the_end(tmp_path: Path) -> None:
    _write(tmp_path / "outer" / "__init__.py", "from outer.inner import helper\n")
    _write(tmp_path / "outer" / "inner" / "__init__.py", "from outer.inner.util import helper\n")
    _write(tmp_path / "outer" / "inner" / "util.py", "def helper():\n    return 2\n")
    _write(tmp_path / "user.py", "from outer import helper\n\n\ndef use():\n    return helper()\n")
    result = _extract(tmp_path)

    assert _call_targets(result, "use") == {_node_id(result, "helper")}


def test_a_cycle_of_re_exports_terminates_and_stays_unbound(tmp_path: Path) -> None:
    _write(tmp_path / "a" / "__init__.py", "from b import helper\n")
    _write(tmp_path / "b" / "__init__.py", "from a import helper\n")
    _write(tmp_path / "user.py", "from a import helper\n\n\ndef use():\n    return helper()\n")
    result = _extract(tmp_path)

    targets = _call_targets(result, "use")
    assert len(targets) == 1
    assert not any(n["id"] in targets for n in result.nodes if n["kind"] == "function" and n.get("qualname") == "helper")


def test_a_name_the_package_binds_twice_is_not_followed(tmp_path: Path) -> None:
    _package(
        tmp_path,
        "from pkg.util import helper\n\ntry:\n    from pkg.base import helper\nexcept ImportError:\n    pass\n",
    )
    _write(tmp_path / "user.py", "from pkg import helper\n\n\ndef use():\n    return helper()\n")
    result = _extract(tmp_path)

    assert _node_id(result, "helper") not in _call_targets(result, "use")


def test_a_name_the_package_defines_itself_is_not_followed(tmp_path: Path) -> None:
    _package(tmp_path, "from pkg.util import helper\n\n\ndef helper():\n    return 3\n")
    _write(tmp_path / "user.py", "from pkg import helper\n\n\ndef use():\n    return helper()\n")
    result = _extract(tmp_path)

    defined = [n["id"] for n in result.nodes if n.get("qualname") == "helper"]
    util_helper = next(i for i in defined if i.startswith("pkg_util_py"))
    assert util_helper not in _call_targets(result, "use")


def test_changing_a_re_export_re_resolves_an_unchanged_importer_through_the_cache(
    tmp_path: Path,
) -> None:
    _package(tmp_path, "")
    _write(tmp_path / "user.py", "from pkg import helper\n\n\ndef use():\n    return helper()\n")
    cache = _cache(tmp_path)

    before = _extract(tmp_path, cache)
    assert _node_id(before, "helper") not in _call_targets(before, "use")

    importer_bytes = (tmp_path / "user.py").read_bytes()
    _write(tmp_path / "pkg" / "__init__.py", "from pkg.util import helper\n")
    assert (tmp_path / "user.py").read_bytes() == importer_bytes

    after = _extract(tmp_path, cache)
    assert _call_targets(after, "use") == {_node_id(after, "helper")}
