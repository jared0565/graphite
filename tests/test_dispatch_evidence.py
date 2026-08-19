"""Method dispatch must not overrule what the call classifier proved (#56).

`_call_confidence` tags an edge `EXTERNAL_CALL` only when the call **provably
leaves the repo**: the receiver's root is attributable and is bound by an import
that did not resolve in-repo, or is an `_EXTERNAL_GLOBALS` name that no in-repo
binding shadows. An unnameable receiver returns `LOCAL_CALL` precisely so a bare
method name can never be classified external (#14 mechanism A).

`_resolve_method_dispatch` then re-pointed that same edge to an in-repo
definition **by bare method name alone**, producing an edge whose `confidence`
says the call leaves the repo and whose `target` is a function inside it. Measured
on graphite's own graph at `66c7670`: 52 such edges, every sample read at its
source line a false binding --

    os.close(root_fd)                 -> _cleanup_worker.py::close
    raw = sys.stdin.read()            -> cache.py::read
    result = subprocess.run(          -> git.py::run
    os.kill(pid, 0)                   -> three different in-repo `kill`s
    path.resolve(input.root)   (.mjs) -> tests/test_git_security.py::resolve

The #54 reachability gate cannot catch these: `os.kill` is called from files that
legitimately import the modules where the same-named in-repo methods live, so the
gate passes them. Externality is evidence that gate never consults.

Two independent invariants are pinned here.

**Evidence.** When the classifier had attributable evidence about the receiver's
root and the dispatch pass has only a name, the evidence wins. The edge is kept
as-is rather than dropped, so the external accounting `health.py` reads is
untouched.

**Interop family.** A JavaScript call site cannot reach a Python `def` -- no FFI
is modelled. Unlike the #54 gate this is an exact invariant rather than a proxy,
so it needs no per-language census: it removes only edges that are impossible.
It is scoped to a family rather than a raw language label because `LANGUAGE_BY_EXT`
maps `.ts -> typescript` and `.tsx -> tsx`, and a `.ts` module calling into a
`.tsx` component is ordinary code.
"""
from __future__ import annotations

from pathlib import Path

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.ingest import collect_files


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _edges(tmp_path: Path) -> list[dict]:
    cfg = Config(
        workers=1,
        cache_dir=tmp_path / ".cache" / "graphite",
        typescript_resolver="disabled",
    )
    result = extract_all(collect_files(tmp_path, cfg), cfg)
    return [e for e in result.edges if e["relation"] == "calls"]


def _calls(tmp_path: Path) -> set[tuple[str, str]]:
    return {(e["source"], e["target"]) for e in _edges(tmp_path)}


# --- evidence: a proven-external call is not re-pointed -----------------------


def test_a_stdlib_call_is_not_re_pointed_to_an_imported_definition(tmp_path: Path) -> None:
    """`os.read(fd, 8)` in a file that legitimately imports a class with `read`.

    This is the shape the #54 gate lets through, and the one measured 52 times on
    graphite's own graph: the import that makes the definition "reachable" is real,
    and irrelevant -- the receiver is `os`.
    """
    _write(
        tmp_path / "cache.py",
        "class Cache:\n"
        "    def read(self):\n"
        "        return 1\n",
    )
    _write(
        tmp_path / "app.py",
        "import os\n"
        "from cache import Cache\n"
        "\n"
        "def go(fd):\n"
        "    Cache()\n"
        "    return os.read(fd, 8)\n",
    )

    assert ("app_py_go", "cache_py_read") not in _calls(tmp_path)


def test_a_proven_external_typescript_call_is_not_re_pointed(tmp_path: Path) -> None:
    """The same defect in an UNGATED language, where only this invariant can help.

    TS/JS are deliberately outside the #54 reachability gate, so before this the
    only thing standing between `pool.connect()` and an in-repo `connect()` was the
    ambiguity cap -- which a single candidate passes.
    """
    _write(tmp_path / "src" / "db.ts", "export class Db { connect() { return 1; } }\n")
    _write(
        tmp_path / "src" / "app.ts",
        "import { pool } from 'pg';\n"
        "export function go() { return pool.connect(); }\n",
    )

    assert ("src_app_ts_go", "src_db_ts_connect") not in _calls(tmp_path)


def test_the_external_edge_is_kept_rather_than_dropped(tmp_path: Path) -> None:
    """Refusing to re-point must not delete the evidence.

    `health.py` excludes EXTERNAL_CALL edges from the resolution denominator, and it
    can only exclude what is present. A fix that dropped these would move the ratio
    for a reason unrelated to binding quality, and the excluded evidence would stop
    being countable in `graph.json` -- see `_EXTERNAL_GLOBALS`' own note on that.
    """
    _write(
        tmp_path / "cache.py",
        "class Cache:\n"
        "    def read(self):\n"
        "        return 1\n",
    )
    _write(
        tmp_path / "app.py",
        "import os\n"
        "from cache import Cache\n"
        "\n"
        "def go(fd):\n"
        "    Cache()\n"
        "    return os.read(fd, 8)\n",
    )

    external = [
        e for e in _edges(tmp_path)
        if e["source"] == "app_py_go" and e.get("confidence") == "EXTERNAL_CALL"
    ]
    assert external, "the os.read call lost its EXTERNAL_CALL edge entirely"
    assert all(e["target"] != "cache_py_read" for e in external)


# --- the recall this must NOT cost --------------------------------------------
#
# Without these, an invariant that refuses every dispatch passes everything above.


def test_a_local_receiver_still_dispatches_to_the_same_method_name(tmp_path: Path) -> None:
    """Identical files to the first test, except the receiver is not `os`."""
    _write(
        tmp_path / "cache.py",
        "class Cache:\n"
        "    def read(self):\n"
        "        return 1\n",
    )
    _write(
        tmp_path / "app.py",
        "from cache import Cache\n"
        "\n"
        "def go():\n"
        "    cache = Cache()\n"
        "    return cache.read()\n",
    )

    assert ("app_py_go", "cache_py_read") in _calls(tmp_path)


def test_an_in_repo_binding_still_beats_an_external_name_collision(tmp_path: Path) -> None:
    """`_call_confidence`'s in-repo precedence must keep working through this.

    `format` is in `_EXTERNAL_GLOBALS`. A repo that imports its own `format` has
    proven the name local, so the call is LOCAL_CALL and dispatch may re-point it --
    otherwise this invariant would silently delete every call to an in-repo
    definition whose name collides with the globals list.
    """
    _write(
        tmp_path / "helpers.py",
        "class Formatter:\n"
        "    def format(self, value):\n"
        "        return str(value)\n",
    )
    _write(
        tmp_path / "app.py",
        "from helpers import Formatter\n"
        "\n"
        "def go(value):\n"
        "    formatter = Formatter()\n"
        "    return formatter.format(value)\n",
    )

    assert ("app_py_go", "helpers_py_format") in _calls(tmp_path)


# --- interop family: dispatch may not cross a language boundary ---------------


def test_a_javascript_call_does_not_dispatch_to_a_python_method(tmp_path: Path) -> None:
    """The #54 residual, measured live: `path.resolve()` in `.mjs` bound to a `.py`.

    Deliberately built so the call is **LOCAL_CALL** -- `svc` is imported from a file
    that resolves in-repo, which `_call_confidence` treats as proof the name is
    local. The evidence invariant above therefore cannot reach it, and without the
    family check this edge survives.
    """
    _write(tmp_path / "web" / "svc.js", "export const svc = { tag: 'svc' };\n")
    _write(
        tmp_path / "web" / "app.js",
        "import { svc } from './svc.js';\n"
        "export function run() { return svc.handle(); }\n",
    )
    _write(
        tmp_path / "api" / "worker.py",
        "class Worker:\n"
        "    def handle(self):\n"
        "        return 1\n",
    )

    assert ("web_app_js_run", "api_worker_py_handle") not in _calls(tmp_path)


def test_typescript_and_tsx_are_one_interop_family(tmp_path: Path) -> None:
    """`.ts` and `.tsx` are different LANGUAGE_BY_EXT labels and the same ecosystem.

    This is the mutation that a raw `language(caller) == language(definer)` check
    fails: it would delete every call from a `.ts` module into a `.tsx` component,
    which is ordinary code in every React codebase graphite is pointed at.
    """
    _write(
        tmp_path / "src" / "widget.tsx",
        "export class Widget { render() { return null; } }\n",
    )
    _write(
        tmp_path / "src" / "page.ts",
        "export function draw(widget: any) { return widget.render(); }\n",
    )

    assert ("src_page_ts_draw", "src_widget_tsx_render") in _calls(tmp_path)


def test_javascript_and_typescript_are_one_interop_family(tmp_path: Path) -> None:
    """A `.js` module calling into a `.ts` one is the same ecosystem, not a crossing."""
    _write(tmp_path / "src" / "store.ts", "export class Store { load() { return 1; } }\n")
    _write(
        tmp_path / "src" / "use.js",
        "export function use(store) { return store.load(); }\n",
    )

    assert ("src_use_js_use", "src_store_ts_load") in _calls(tmp_path)


def test_the_family_table_is_derived_from_the_extractors_language_table() -> None:
    """Every language the extractor can produce must have a family, or fail open.

    A language missing from the table must be treated as its own family rather than
    as "no family" -- otherwise adding a new extractor would silently start deleting
    its dispatches, in the direction nobody would notice.
    """
    from graphite.extract.ast import _interop_family
    from graphite.ingest import LANGUAGE_BY_EXT

    assert _interop_family("src/a.ts") == _interop_family("src/a.tsx")
    assert _interop_family("src/a.js") == _interop_family("src/a.jsx")
    assert _interop_family("src/a.mjs") == _interop_family("src/a.ts")
    assert _interop_family("src/a.py") != _interop_family("src/a.ts")
    assert _interop_family("src/a.go") != _interop_family("src/a.rs")

    for ext, language in LANGUAGE_BY_EXT.items():
        assert _interop_family(f"src/sample{ext}") is not None, (
            f"{ext} extracts as {language} with no interop family; dispatch from it "
            "would be deleted rather than left alone"
        )

    # An unknown extension has no family and must not be gated by one.
    assert _interop_family("src/a.rb") is None
    assert _interop_family(None) is None
