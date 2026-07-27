"""EXTERNAL_CALL classification: never-imported globals and external imports."""
from __future__ import annotations

from pathlib import Path

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.ingest import collect_files


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(tmp_path: Path):
    cfg = Config(workers=1, cache_dir=tmp_path / ".cache" / "graphite")
    return extract_all(collect_files(tmp_path, cfg), cfg)


def _calls(result, source_file):
    return [
        e for e in result.edges
        if e.get("relation") == "calls" and e.get("source_file") == source_file
    ]


def _confidence_by_target_suffix(result, source_file):
    return {
        e["target"].rsplit("_", 1)[-1]: e["confidence"]
        for e in _calls(result, source_file)
    }


def test_injected_test_globals_are_tagged_external(tmp_path):
    # NOTE: the helper is `addOne`, never `sum` -- `sum` is in
    # _LANGUAGE_BUILTIN_GLOBALS, so a call to it produces NO edge and this
    # test would silently assert on a smaller edge set than it appears to.
    _write(tmp_path / "src" / "dep.ts", "export function addOne(a: number) { return a; }\n")
    _write(
        tmp_path / "src" / "dep.test.ts",
        "import { addOne } from './dep';\n"
        "describe('addOne', () => {\n"
        "  it('adds', () => {\n"
        "    expect(addOne(1));\n"
        "  });\n"
        "});\n",
    )
    result = _extract(tmp_path)
    conf = _confidence_by_target_suffix(result, "src/dep.test.ts")
    assert conf["expect"] == "EXTERNAL_CALL"
    assert conf["describe"] == "EXTERNAL_CALL"
    assert conf["it"] == "EXTERNAL_CALL"
    # the in-repo helper called from inside the test still binds locally
    assert conf["addone"] == "LOCAL_CALL"


def test_in_repo_call_stays_local(tmp_path):
    _write(tmp_path / "src" / "dep.ts", "export function addOne(a: number) { return a; }\n")
    _write(
        tmp_path / "src" / "use.ts",
        "import { addOne } from './dep';\nexport function go() { return addOne(1); }\n",
    )
    result = _extract(tmp_path)
    confidences = {e["confidence"] for e in _calls(result, "src/use.ts")}
    assert confidences == {"LOCAL_CALL"}


def test_python_builtin_absent_from_drop_list_is_tagged(tmp_path):
    _write(
        tmp_path / "m.py",
        "def go():\n"
        "    raise ValueError('x')\n",
    )
    result = _extract(tmp_path)
    conf = _confidence_by_target_suffix(result, "m.py")
    assert conf["valueerror"] == "EXTERNAL_CALL"


def test_drop_list_names_still_produce_no_edge(tmp_path):
    """_LANGUAGE_BUILTIN_GLOBALS is frozen for this round (spec §4.3).

    `len` stays dropped -- it must NOT become an EXTERNAL_CALL edge, because
    that would add edges and break the pure-relabel invariant.
    """
    _write(tmp_path / "m.py", "def go(xs):\n    return len(xs)\n")
    result = _extract(tmp_path)
    assert not any(t.endswith("len") for t in
                   (e["target"] for e in _calls(result, "m.py")))


def test_member_call_root_decides_externality(tmp_path):
    """Requires the Step 4b retention change -- without it this edge is dropped
    by _resolve_method_dispatch before it can be observed."""
    _write(
        tmp_path / "src" / "t.ts",
        "export function go() { return crypto.randomUUID(); }\n",
    )
    result = _extract(tmp_path)
    confidences = {e["confidence"] for e in _calls(result, "src/t.ts")}
    assert confidences == {"EXTERNAL_CALL"}


def test_unattributable_member_call_is_still_dropped(tmp_path):
    """The retention in Step 4b is NARROW. `ctx` is a local parameter, not an
    external binding, so `ctx.json()` stays dropped -- that framework-noise
    population is the whole reason the phantom filter exists. If this test
    fails, the retention was widened to every member call and every TS graph
    just grew by the runtime-API population.
    """
    _write(
        tmp_path / "src" / "t.ts",
        "export function go(ctx: any) { return ctx.json(); }\n",
    )
    result = _extract(tmp_path)
    assert _calls(result, "src/t.ts") == []


def test_named_import_from_external_package_is_tagged(tmp_path):
    _write(
        tmp_path / "src" / "t.ts",
        "import { z } from 'zod';\n"
        "export function go() { return z.object({}); }\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "src/t.ts")} == {"EXTERNAL_CALL"}


def test_default_import_from_external_package_is_tagged(tmp_path):
    # NOTE: the method is `request`, never `get` -- `get` is in resolve.py's
    # _NOISY_MEMBER_CALLS drop-list (pre-existing, unrelated to this task), so
    # `axios.get(...)` produces NO edge at all and this test would silently
    # assert on an empty set instead of exercising the default-import path.
    _write(
        tmp_path / "src" / "t.ts",
        "import axios from 'axios';\n"
        "export function go() { return axios.request('/x'); }\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "src/t.ts")} == {"EXTERNAL_CALL"}


def test_namespace_import_from_external_package_is_tagged(tmp_path):
    _write(
        tmp_path / "src" / "t.ts",
        "import * as lib from 'some-lib';\n"
        "export function go() { return lib.run(); }\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "src/t.ts")} == {"EXTERNAL_CALL"}


def test_aliased_named_import_uses_the_local_name(tmp_path):
    _write(
        tmp_path / "src" / "t.ts",
        "import { parse as p } from 'yaml';\n"
        "export function go() { return p('x'); }\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "src/t.ts")} == {"EXTERNAL_CALL"}


def test_in_repo_import_is_not_tagged_external(tmp_path):
    """The discriminating case: same syntax, resolvable module."""
    _write(tmp_path / "src" / "dep.ts", "export function dep() { return 1; }\n")
    _write(
        tmp_path / "src" / "t.ts",
        "import { dep } from './dep';\nexport function go() { return dep(); }\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "src/t.ts")} == {"LOCAL_CALL"}


def test_python_plain_import_of_external_module_is_tagged(tmp_path):
    _write(
        tmp_path / "m.py",
        "import pathlib\n"
        "def go():\n"
        "    return pathlib.Path('.')\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "m.py")} == {"EXTERNAL_CALL"}


def test_python_aliased_import_uses_the_alias(tmp_path):
    _write(
        tmp_path / "m.py",
        "import numpy as np\n"
        "def go():\n"
        "    return np.array([])\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "m.py")} == {"EXTERNAL_CALL"}


def test_python_from_import_of_external_symbol_is_tagged(tmp_path):
    _write(
        tmp_path / "m.py",
        "from dataclasses import dataclass\n"
        "def go():\n"
        "    return dataclass()\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "m.py")} == {"EXTERNAL_CALL"}


def test_python_dotted_import_binds_the_root(tmp_path):
    """`import os.path` binds `os`, so `os.path.join()` is external."""
    _write(
        tmp_path / "m.py",
        "import os.path\n"
        "def go():\n"
        "    return os.path.join('a')\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "m.py")} == {"EXTERNAL_CALL"}


def test_python_in_repo_import_stays_local(tmp_path):
    """The discriminating case: same syntax, resolvable module."""
    _write(tmp_path / "dep.py", "def helper():\n    return 1\n")
    _write(
        tmp_path / "m.py",
        "from dep import helper\n"
        "def go():\n"
        "    return helper()\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "m.py")} == {"LOCAL_CALL"}
