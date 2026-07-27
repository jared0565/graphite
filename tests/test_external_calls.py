"""EXTERNAL_CALL classification: never-imported globals and external imports."""
from __future__ import annotations

from pathlib import Path

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.graph import build_graph
from graphite.health import resolution_health
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
    that would move it off `_LANGUAGE_BUILTIN_GLOBALS`, which spec §4.3
    freezes for this round, and add an edge that shouldn't exist.
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
    """The retention gate (ast.py:1342) keys on the edge's CONFIDENCE LABEL,
    not on receiver attributability. `ctx` is a plain identifier, so
    `_call_target_name` stringifies the receiver directly and `_call_confidence`
    tests the root `ctx` -- it is in neither `_EXTERNAL_GLOBALS` nor
    `external_names`, so the edge classifies LOCAL_CALL, resolves to no known
    definition, and the member-dispatch post-pass drops it. That framework-noise
    population (`ctx.json()`, `db.prepare()`, `stmt.bind()`) is the whole
    reason the phantom filter exists.

    This is NOT a general "every unattributable receiver is dropped"
    guarantee: when a receiver can't be stringified at all (a regex literal, a
    string literal, a call result -- `_simple_object_name` returns `None`,
    ast.py:585-600), `_call_target_name` falls back to the BARE METHOD NAME
    with no object prefix, and if that bare name collides with
    `_EXTERNAL_GLOBALS` the call IS tagged and retained regardless -- see
    `test_computed_receiver_bare_name_collision_is_a_false_external`. `ctx`
    never hits that fallback because it stringifies fine; `json` also doesn't
    collide with `_EXTERNAL_GLOBALS`, so this fixture drops for two
    independent reasons, neither of which is "receivers are always safe".

    If this test fails, either the drop-list/global-list changed, or the
    dispatch retention gate (ast.py:1342) was widened to keep every
    unresolved member call outright.
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


def test_namespace_import_of_in_repo_module_stays_local(tmp_path):
    """F2: an in-repo import binding must win over an _EXTERNAL_GLOBALS name
    collision. `crypto` is a MODULE name here (`src/crypto.ts`), not the Web
    Crypto global -- it collides with `_EXTERNAL_GLOBALS` by spelling only.
    Nothing before this fix checked whether the TS import walk had already
    resolved that name to an in-repo file, so a namespace import of a
    same-named local module lost to the global collision every time.

    NOTE: the method is `encrypt`, never `format`/`test`/`get`/... -- see the
    fixture footgun list (this file's module docstring / the task brief):
    `_LANGUAGE_BUILTIN_GLOBALS` and `_EXTERNAL_GLOBALS` in
    src/graphite/extract/ast.py, `_NOISY_MEMBER_CALLS` and `_BUILTIN_OBJECTS`
    in src/graphite/resolve.py. `encrypt` is in none of them.

    The method lives on a class, not a bare function, so the member-dispatch
    post-pass (`_resolve_method_dispatch`) has a real `is_method` node named
    `encrypt` to re-point the phantom edge to. Without that, the edge would be
    DROPPED outright regardless of confidence (LOCAL_CALL with no known
    target survives nothing), and this test would silently pass on an empty
    edge set instead of exercising the classification -- the exact false-pass
    shape this round has already produced once.
    """
    _write(
        tmp_path / "src" / "crypto.ts",
        "export class Cipher {\n"
        "  encrypt() { return 1; }\n"
        "}\n",
    )
    _write(
        tmp_path / "src" / "use.ts",
        "import * as crypto from './crypto';\n"
        "export function go() { return crypto.encrypt(); }\n",
    )
    result = _extract(tmp_path)
    conf = _confidence_by_target_suffix(result, "src/use.ts")
    assert conf["encrypt"] == "LOCAL_CALL"


def test_named_import_of_in_repo_module_wins_over_global_collision(tmp_path):
    """F2 covers ALL binding forms, not just namespace imports -- `in_repo` is
    collected from the same `_iter_bound_local_names` walk used for
    `external`, which already handles default/namespace/named uniformly.
    `test` is deliberately the bound name: it collides with `_EXTERNAL_GLOBALS`
    (the mocha/jest global), so before this fix `import { test } from
    './helpers'` resolving in-repo still tagged the call EXTERNAL_CALL --
    mislabeling a call whose target `_resolve_call` already resolves directly
    via the named-import symbol map (no dispatch post-pass involved, so this
    is a distinct code path from the namespace-import test above).
    """
    _write(tmp_path / "src" / "helpers.ts", "export function test() { return 1; }\n")
    _write(
        tmp_path / "src" / "use2.ts",
        "import { test } from './helpers';\n"
        "export function go() { return test(); }\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "src/use2.ts")} == {"LOCAL_CALL"}


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


def test_python_dotted_import_of_in_repo_package_stays_local(tmp_path):
    """The false-external this fix round exists to prevent: `import pkg.sub`
    binds only the root `pkg`, but when `pkg` itself resolves in-repo, a
    call through it must NOT be excluded from the health ratio as external
    just because the import statement was dotted.
    """
    _write(
        tmp_path / "pkg" / "__init__.py",
        "class Widget:\n"
        "    def build(self):\n"
        "        return 1\n",
    )
    _write(tmp_path / "pkg" / "sub.py", "def helper():\n    return 1\n")
    _write(
        tmp_path / "m.py",
        "import pkg.sub\n"
        "def go():\n"
        "    return pkg.build()\n",
    )
    result = _extract(tmp_path)
    assert {e["confidence"] for e in _calls(result, "m.py")} == {"LOCAL_CALL"}


def test_python_attribute_root_falls_back_sanely_on_broken_chain(tmp_path):
    """`_python_attribute_root` only walks nested `attribute` object fields;
    a subscript (`items[0].render()`) or an intervening call
    (`pathlib.Path('.').render()`) breaks that chain. Both must fall back to
    classifying off the leaf name rather than mis-rooting -- neither call may
    be tagged external just because `pathlib` appears in the same expression
    or file. The leaf is a real in-repo method (`Renderer.render`) so both
    calls resolve via method-name dispatch to a known node and their
    confidence is directly observable, instead of silently vanishing (dropped
    by the D7 retention gate for lacking a known target) and masking the
    assertion as a false pass.

    NOTE: the leaf is `render`, never `describe` -- `describe` is itself in
    `_EXTERNAL_GLOBALS` (mocha/jest global), a fourth drop-list beyond the
    three the brief calls out, and would make this assert on the wrong
    reason.
    """
    _write(
        tmp_path / "m.py",
        "import pathlib\n"
        "class Renderer:\n"
        "    def render(self):\n"
        "        return 1\n"
        "def go(items):\n"
        "    items[0].render()\n"
        "    pathlib.Path('.').render()\n",
    )
    result = _extract(tmp_path)
    conf = _confidence_by_target_suffix(result, "m.py")
    assert conf["render"] == "LOCAL_CALL"
    assert conf["path"] == "EXTERNAL_CALL"


def _mixed_fixture(tmp_path: Path) -> None:
    """Six calls, one of each kind, counted by hand from the source below.

    src/dep.ts   -- in-repo definition
    src/t.ts     -- dep() local, z.object() external-import,
                    crypto.randomUUID() external-global, missing() in-repo MISS
    src/t.test.ts -- it(), expect()  external-globals

    All six survive `should_keep_call_target`: `crypto` is not in
    `_BUILTIN_OBJECTS` and `randomUUID`/`object` are not in
    `_NOISY_MEMBER_CALLS` (both checked in src/graphite/resolve.py:15-27).
    """
    _write(tmp_path / "src" / "dep.ts", "export function dep() { return 1; }\n")
    _write(
        tmp_path / "src" / "t.ts",
        "import { dep } from './dep';\n"
        "import { z } from 'zod';\n"
        "export function go() {\n"
        "  dep();\n"
        "  z.object({});\n"
        "  crypto.randomUUID();\n"
        "  missing();\n"
        "  return 1;\n"
        "}\n",
    )
    _write(
        tmp_path / "src" / "t.test.ts",
        # No chained assertion: `expect(1).toBe(1)` emits a SECOND calls edge
        # for `toBe` (the member call on the result), which would make the
        # counts below wrong. Keep the fixture to unchained calls.
        "it('works', () => { expect(1); });\n",
    )


def test_every_call_still_produces_an_edge(tmp_path):
    """Nothing-is-deleted invariant: classification must not drop edges.

    The fixture contains exactly six calls that survive the drop-list and the
    phantom filter. If a future change 'improves' the ratio by deleting noisy
    calls instead of tagging them, this count falls and the test fails.

    Two of the six -- `z.object()` and `crypto.randomUUID()` -- exist ONLY
    because of operator decision D7, which stopped `_resolve_method_dispatch`
    dropping member calls whose root is a known external binding. Before that
    change this count was four. If someone reverts D7, this test is where it
    shows up.
    """
    _mixed_fixture(tmp_path)
    result = _extract(tmp_path)
    total = len(_calls(result, "src/t.ts")) + len(_calls(result, "src/t.test.ts"))
    assert total == 6


def test_classification_splits_as_expected(tmp_path):
    _mixed_fixture(tmp_path)
    result = _extract(tmp_path)
    edges = _calls(result, "src/t.ts") + _calls(result, "src/t.test.ts")
    external = [e for e in edges if e["confidence"] == "EXTERNAL_CALL"]
    local = [e for e in edges if e["confidence"] == "LOCAL_CALL"]
    # z.object, crypto.randomUUID, expect, it
    assert len(external) == 4
    # dep() and missing() -- the in-repo hit and the in-repo MISS
    assert len(local) == 2


def test_genuine_in_repo_miss_still_counts_unbound(tmp_path):
    """The falsifier. A real binding failure must survive classification.

    `missing()` is called but never defined, so it stays LOCAL_CALL, stays
    unbound, and must still drag the calls ratio below 1.0. A version of this
    feature that tags everything external would report 1.0 here and pass every
    other test in this file.
    """
    _mixed_fixture(tmp_path)
    result = _extract(tmp_path)
    g = build_graph(result.nodes, result.edges)
    cell = resolution_health(g)["by_relation"]["calls"]
    assert cell["external"] == 4
    assert cell["ratio"] is not None
    assert cell["ratio"] < 1.0, "the unresolved missing() call must still count against the ratio"


def test_computed_receiver_bare_name_collision_is_a_false_external(tmp_path):
    """Pins a false-external risk flagged by Task 2's implementer; not a fix.

    When a call's receiver is not a simple identifier -- here `getThing()`,
    itself a call expression -- `_simple_object_name` returns None (it only
    stringifies identifiers and member chains, deliberately refusing to
    stringify a "complex literal" like a call result:
    src/graphite/extract/ast.py:585-600). `_call_target_name` then falls back
    to the bare property name alone, `test`, with no object prefix. That bare
    name collides with an `_EXTERNAL_GLOBALS` entry (mocha's focused-test
    global, alongside `format`, `property`, `fit`), so `_call_confidence`
    tags the call EXTERNAL_CALL.

    THIS IS A FALSE EXTERNAL: `getThing` is a same-file, in-repo function
    returning a plain object literal -- the receiver was never proven to
    leave the repo, and no in-repo `test` method exists for the dispatch
    post-pass to re-point the phantom target to. Empirically, the edge is
    neither dropped by the phantom filter nor bound; it survives as an
    EXTERNAL_CALL phantom (confirmed via a direct extract_all() run before
    writing this assertion). `_EXTERNAL_GLOBALS` and `_call_confidence` are
    frozen for this round (spec), so this test pins the current behaviour as
    a known limitation for the follow-up round rather than "fixing" it here.

    Contrast with `test_unattributable_member_call_is_still_dropped` above:
    `ctx.json()` has the exact same unattributable-receiver shape and is
    DROPPED entirely by the phantom filter. `getThing().test()` differs only
    in that its bare method name happens to collide with an
    `_EXTERNAL_GLOBALS` entry -- so instead of being dropped, it is kept and
    silently excluded from the calls health ratio, which is worse: it looks
    like attributable evidence instead of vanishing.

    That exclusion is the concrete, measurable consequence, not just a label:
    `resolution_health`'s calls cell for this exact fixture reports
    `total: 1, bound: 1, ratio: 1.0, external: 1` -- a perfect 1.0 ratio
    while an unbound, unproven-external call sits right there, invisible to
    the ratio. That is the masking failure mode this round's health schema
    exists to prevent, reproduced in miniature by a single name collision.
    """
    _write(
        tmp_path / "src" / "t.ts",
        "function getThing() { return { test: () => 1 }; }\n"
        "export function go() { return getThing().test(); }\n",
    )
    result = _extract(tmp_path)
    conf = _confidence_by_target_suffix(result, "src/t.ts")
    assert conf["getthing"] == "LOCAL_CALL"
    assert conf["test"] == "EXTERNAL_CALL"  # false external -- see docstring above

    g = build_graph(result.nodes, result.edges)
    cell = resolution_health(g)["by_relation"]["calls"]
    assert cell == {"total": 1, "bound": 1, "ratio": 1.0, "external": 1}, (
        "the getThing().test() edge should be excluded as external, leaving "
        "a perfect 1.0 ratio that hides it -- the masking failure this test "
        "documents as a known limitation, not asserts as correct"
    )
