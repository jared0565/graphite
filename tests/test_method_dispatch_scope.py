"""Method dispatch must not bind a call to a definition the caller cannot reach (#54).

`_resolve_method_dispatch` re-points `recv.method()` to any `is_method` definition
sharing that name, anywhere in the repo. Its docstring justifies that with "method
names in real codebases are almost always globally unique" -- which is true of
domain names and false of the ones every language's standard library already owns.

Measured on graphite's own graph before this gate existed: `query "callers resolve"`
returned **232 callers graded decision_grade**, every one an ordinary
`pathlib.Path(...).resolve()` re-pointed to a test double's `resolve` method in
`tests/test_git_security.py`. `src/graphite/io.py::atomic_write_text` was likewise
recorded as calling a `write` defined in `tests/test_doctor.py`.

The gate: a candidate must live in the calling file itself, or in a file the calling
file DIRECTLY imports. The legitimate cross-file case carries that import already --
`from pkg.ledger import Ledger` before `ledger.record_run()` -- while `pathlib` is
not in the repo, so nothing can reach a same-named double.

SCOPE: Python only. The gate's proxy ("does not import" => "cannot be holding one")
is never exact in a duck-typed language; it is admitted for Python because it was
measured there, not because it is sound in general. TypeScript is explicitly left
ungated -- see `test_typescript_dispatch_is_untouched`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.ingest import collect_files


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _calls(tmp_path: Path) -> set[tuple[str, str]]:
    cfg = Config(
        workers=1,
        cache_dir=tmp_path / ".cache" / "graphite",
        typescript_resolver="disabled",
    )
    result = extract_all(collect_files(tmp_path, cfg), cfg)
    return {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}


def _double(tmp_path: Path, method: str = "resolve") -> None:
    """A test double defining a stdlib method name -- the real shadow, not a toy.

    Modelled on tests/test_git_security.py:499, which defines `def resolve(self)`
    on a fake Path and captured every `Path(...).resolve()` in the repository.
    """
    _write(
        tmp_path / "tests" / "test_double.py",
        "class FakePath:\n"
        f"    def {method}(self):\n"
        "        return self\n",
    )


# --- the defect ---------------------------------------------------------------


def test_a_stdlib_call_does_not_bind_to_an_unimported_double(tmp_path: Path) -> None:
    """`Path(__file__).resolve()` in a file that imports only pathlib."""
    _double(tmp_path)
    _write(
        tmp_path / "src" / "prod.py",
        "from pathlib import Path\n"
        "\n"
        "def where():\n"
        "    return Path(__file__).resolve()\n",
    )

    assert ("src_prod_where", "tests_test_double_resolve") not in _calls(tmp_path)


def test_a_local_receiver_does_not_bind_to_an_unimported_double(tmp_path: Path) -> None:
    """The DOMINANT shape, and the one a chain-root fix would miss.

    A census of graphite's own source found 193 `v.resolve()` name-receiver sites
    against 92 `X(...).resolve()` call-receiver sites, plus 790 `v.get()`. Any fix
    that only recovers the root of a call expression leaves most of #54 in place.
    """
    _double(tmp_path)
    _write(
        tmp_path / "src" / "prod.py",
        "def where(path):\n"
        "    return path.resolve()\n",
    )

    assert ("src_prod_where", "tests_test_double_resolve") not in _calls(tmp_path)


def test_a_file_handle_write_does_not_bind_to_an_unimported_double(tmp_path: Path) -> None:
    """The second measured instance: io.py::atomic_write_text -> tests/test_doctor.py."""
    _double(tmp_path, method="write")
    _write(
        tmp_path / "src" / "prod.py",
        "def save(path, text):\n"
        "    with open(path, 'w') as f:\n"
        "        f.write(text)\n",
    )

    assert ("src_prod_save", "tests_test_double_write") not in _calls(tmp_path)


# --- the recall the gate must NOT cost ----------------------------------------
#
# Without these, a gate that refuses everything passes every assertion above.


def test_an_imported_classs_method_still_binds_across_files(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "ledger.py",
        "class Ledger:\n"
        "    def record_run(self, run_id):\n"
        "        return [run_id]\n",
    )
    _write(
        tmp_path / "src" / "pipeline.py",
        "from ledger import Ledger\n"
        "\n"
        "def run(run_id):\n"
        "    ledger = Ledger()\n"
        "    return ledger.record_run(run_id)\n",
    )

    assert ("src_pipeline_run", "src_ledger_record_run") in _calls(tmp_path)


def test_a_self_call_still_binds_in_the_same_file(tmp_path: Path) -> None:
    _write(
        tmp_path / "svc.py",
        "class Svc:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "    def run(self):\n"
        "        return self.helper()\n",
    )

    assert ("svc_run", "svc_helper") in _calls(tmp_path)


def test_typescript_dispatch_is_untouched(tmp_path: Path) -> None:
    """TS is NOT gated, and this is the regression guard for that decision.

    The gate's proxy -- "the caller does not import the definer" -- is weaker in a
    structurally typed language: `test_call_graph.py::
    test_member_call_ambiguous_small_set_links_to_all` pins `x.refresh()` on an
    `x: any` parameter binding to two classes the caller never imports. That shape
    is idiomatic TS and the gate would delete it, so TS stays ungated until someone
    measures it the way #54 measured Python.
    """
    _write(tmp_path / "src" / "store.ts", "export class Store { load() { return 1; } }\n")
    _write(tmp_path / "src" / "user.ts", "export function use(store: any) { return store.load(); }\n")

    # No import anywhere -- the Python gate would refuse this; TS must not.
    assert ("src_user_use", "src_store_load") in _calls(tmp_path)


def test_go_dispatch_still_crosses_files(tmp_path: Path) -> None:
    """Go is EXEMPT from the gate, and that has to be a test rather than a comment.

    Go's in-repo imports target a synthesized package id (`example_com_repo_store`)
    that never equals the file node id of any file in that package, so an
    import-reachability gate would refuse every legitimate cross-package dispatch.
    Files in one package also see each other with no import at all. Without this
    test, someone generalising the gate to all languages would read the resulting
    Go failure as a Go bug.
    """
    _write(
        tmp_path / "store" / "store.go",
        "package store\n"
        "\n"
        "type Store struct{}\n"
        "\n"
        "func (s *Store) Load() int { return 1 }\n",
    )
    _write(
        tmp_path / "app" / "app.go",
        "package app\n"
        "\n"
        'import "example.com/repo/store"\n'
        "\n"
        "func Use(s *store.Store) int {\n"
        "\treturn s.Load()\n"
        "}\n",
    )

    assert ("app_app_use", "store_store_load") in _calls(tmp_path)


def test_an_unknown_language_is_exempt_rather_than_gated(tmp_path: Path) -> None:
    """Fail OPEN on a file whose import model we cannot reason about.

    A language with no extraction support emits no import edges at all, so gating it
    would silently delete every dispatch in it. Exempting keeps today's behaviour for
    anything the gate was never designed against.
    """
    from graphite.extract.ast import _dispatch_is_gated

    assert _dispatch_is_gated("src/a.py") is True
    # Measured only on Python. TS/JS and Rust are ungated on purpose, not by
    # oversight -- widening this set is a per-language measurement.
    assert _dispatch_is_gated("src/a.ts") is False
    assert _dispatch_is_gated("src/a.rs") is False
    assert _dispatch_is_gated("src/a.go") is False
    assert _dispatch_is_gated("src/a.rb") is False
    assert _dispatch_is_gated("Makefile") is False
    assert _dispatch_is_gated(None) is False


def test_the_gate_reads_the_extractors_own_language_table() -> None:
    """The gate and the extractor must classify a file the same way.

    The gate decides per file whether to restrict dispatch; `collect_files` decides
    per file which extractor runs. Those are two classifications of the same thing,
    and a private suffix list in the gate would match the extractor only by
    coincidence -- `.py` is merely the sole extension currently mapping to `python`.
    Add `.pyw` to `LANGUAGE_BY_EXT` and a hardcoded list would extract that file as
    Python while exempting it from the gate, silently and in the permissive
    direction. Deriving from the one table makes the drift impossible; this test
    fails if anyone reintroduces the duplicate.
    """
    from graphite.extract.ast import _dispatch_is_gated
    from graphite.ingest import LANGUAGE_BY_EXT

    python_extensions = [ext for ext, lang in LANGUAGE_BY_EXT.items() if lang == "python"]
    assert python_extensions, "the table stopped classifying anything as Python"

    for ext, language in LANGUAGE_BY_EXT.items():
        assert _dispatch_is_gated(f"src/sample{ext}") is (language == "python"), (
            f"{ext} extracts as {language}; the gate disagrees with the extractor"
        )


def test_a_dotted_import_reaches_the_package_it_binds(tmp_path: Path) -> None:
    """`import pkg.sub` binds the name **`pkg`**, so `pkg.build()` is a real call.

    Caught by `test_external_calls.py::test_python_dotted_import_of_in_repo_package
    _stays_local` when the gate first went in: graphite records only an edge to
    `pkg/sub.py` for that statement, never to `pkg/__init__.py`, so a reachability
    check reading import edges literally refuses a dispatch onto the very name the
    statement bound. The gate expands each imported module to its ancestor packages
    rather than inheriting that gap.
    """
    _write(
        tmp_path / "pkg" / "__init__.py",
        "class Widget:\n"
        "    def build(self):\n"
        "        return 1\n",
    )
    _write(tmp_path / "pkg" / "sub.py", "def helper():\n    return 1\n")
    _write(tmp_path / "m.py", "import pkg.sub\n\ndef go():\n    return pkg.build()\n")

    assert ("m_go", "pkg_init_build") in _calls(tmp_path)


@pytest.mark.parametrize(
    "statement", ["from pkg.ledger import Ledger", "from .ledger import Ledger"]
)
def test_both_import_spellings_reach_their_target(tmp_path: Path, statement: str) -> None:
    """The gate is only as good as the import edges it reads.

    If one import spelling failed to bind to a file node, the gate would drop every
    dispatch written that way and the loss would be invisible -- an empty `callers`
    answer reads as a trustworthy absence. Both spellings are exercised because they
    take different paths through module resolution.
    """
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(
        tmp_path / "src" / "pkg" / "ledger.py",
        "class Ledger:\n"
        "    def record_run(self, run_id):\n"
        "        return [run_id]\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "pipeline.py",
        f"{statement}\n"
        "\n"
        "def run(run_id):\n"
        "    ledger = Ledger()\n"
        "    return ledger.record_run(run_id)\n",
    )

    assert ("src_pkg_pipeline_run", "src_pkg_ledger_record_run") in _calls(tmp_path)
