"""Tests for `scripts/mutation_tests.py`, the certifying command of aramid's
mutation gate (`[mutation].test_command` in aramid.toml).

Written because the gate's first live run (2026-09-04, item 0bb89cb2) mutated
the launcher itself -- it was the only Python file in the commit range -- and
every one of its 20 mutants survived stage 1: no test file was named after the
module, so aramid had nothing to run. The launcher is a link in the gate's
chain of custody (it decides WHICH interpreter tests WHICH tree, and how a kill
is reported), so its behaviour has to be pinned by tests that aramid's stage 1
can find in seconds. This file is `tests/test_<module>.py` for that reason.

The script is loaded from its path rather than imported as a package: it is not
part of `graphite`, and under aramid's mutation consumer the copy under test is
`<worktree>/scripts/mutation_tests.py`, which `Path(__file__)` resolves to.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mutation_tests.py"

_VENV_LEAF = Path("Scripts", "python.exe") if os.name == "nt" else Path("bin", "python")


@pytest.fixture(scope="module")
def launcher() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mutation_tests_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> None:
    # Fixture plumbing only: literal arguments, no shell, git by name as the
    # launcher itself does.
    subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        timeout=60,
    )


def _fake_venv(base: Path) -> Path:
    """`<base>/.venvs/graphite-dev/<leaf>`, the layout the launcher looks for."""
    python = base / ".venvs" / "graphite-dev" / _VENV_LEAF
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    return python


@pytest.fixture
def repo_and_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    # Stop git from walking above tmp_path when a directory holds no repo, so
    # the "plain directory" arms mean the same thing on every machine.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "one")
    worktree = tmp_path / "copy"
    _git(repo, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
    return repo, worktree


# --- pure helpers ------------------------------------------------------------


def test_targeted_tests_maps_each_changed_module_to_its_named_test_files(
    tmp_path: Path, launcher: ModuleType
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in (
        "test_foo.py",
        "test_foo_extra.py",
        "test_foobar.py",  # `test_foo_*` must not match this
        "test_bar.py",
        "test___init__.py",  # exists so a launcher that stops skipping `__init__` is caught
    ):
        (tests / name).write_text("", encoding="utf-8")

    changed = [
        "src/graphite/foo.py",
        "src/graphite/__init__.py",
        "src/graphite/foo.py",  # listed twice: must appear once
        "src/graphite/pkg/bar.py",
    ]
    assert launcher._targeted_tests(tmp_path, changed) == [
        "tests/test_foo.py",
        "tests/test_foo_extra.py",
        "tests/test_bar.py",
    ]
    assert launcher._targeted_tests(tmp_path, []) == []
    assert launcher._targeted_tests(tmp_path, ["src/graphite/nothing_named_after_this.py"]) == []


def test_dev_python_is_the_venv_beside_the_first_root_that_has_one(
    tmp_path: Path, launcher: ModuleType
) -> None:
    with_venv = tmp_path / "a" / "repo"
    with_venv.mkdir(parents=True)
    python = _fake_venv(tmp_path / "a")
    without = tmp_path / "b" / "repo"
    without.mkdir(parents=True)

    assert launcher._dev_python(with_venv) == python
    assert launcher._dev_python(without) is None
    assert launcher._dev_python(without, with_venv) == python
    assert launcher._dev_python(with_venv, without) == python
    assert launcher._dev_python() is None


def test_canonical_root_resolves_a_worktree_copy_to_its_main_repository(
    tmp_path: Path, launcher: ModuleType, repo_and_worktree: tuple[Path, Path]
) -> None:
    repo, worktree = repo_and_worktree
    plain = tmp_path / "plain"
    plain.mkdir()

    assert launcher._canonical_root(worktree).resolve() == repo.resolve()
    assert launcher._canonical_root(repo).resolve() == repo.resolve()
    # Not a repository at all: the launcher must fall back to the directory it
    # was given, never to a parent or to an empty path.
    assert launcher._canonical_root(plain) == plain


def test_git_helper_returns_stdout_on_success_and_empty_on_any_failure(
    tmp_path: Path, launcher: ModuleType, repo_and_worktree: tuple[Path, Path]
) -> None:
    repo, _ = repo_and_worktree
    assert launcher._git(repo, "rev-parse", "--is-inside-work-tree") == "true"
    assert launcher._git(repo, "rev-parse", "--verify", "--quiet", "no-such-ref") == ""
    assert launcher._git(tmp_path / "does-not-exist", "status") == ""


def test_changed_sources_lists_only_python_files_under_src_and_scripts(
    launcher: ModuleType, repo_and_worktree: tuple[Path, Path]
) -> None:
    # `scripts/` is in scope because the launcher lives there and aramid
    # mutates it: the gate's first live drain (2026-09-04) confirmed a launcher
    # mutant with the FULL suite (465 s) because this diff looked at src/ only.
    repo, _ = repo_and_worktree
    (repo / "src").mkdir()
    (repo / "scripts").mkdir()
    (repo / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "src" / "notes.txt").write_text("not python\n", encoding="utf-8")
    (repo / "scripts" / "tool.py").write_text("z = 1\n", encoding="utf-8")
    (repo / "scripts" / "run.sh").write_text("echo\n", encoding="utf-8")
    (repo / "elsewhere.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    assert launcher._changed_sources(repo) == []

    (repo / "src" / "mod.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "src" / "notes.txt").write_text("changed too\n", encoding="utf-8")
    (repo / "scripts" / "tool.py").write_text("z = 2\n", encoding="utf-8")
    (repo / "scripts" / "run.sh").write_text("echo changed\n", encoding="utf-8")
    (repo / "elsewhere.py").write_text("y = 3\n", encoding="utf-8")
    assert launcher._changed_sources(repo) == ["scripts/tool.py", "src/mod.py"]


def test_targeted_tests_maps_a_changed_script_to_its_named_test_file(
    tmp_path: Path, launcher: ModuleType
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_tool.py").write_text("", encoding="utf-8")
    assert launcher._targeted_tests(tmp_path, ["scripts/tool.py"]) == ["tests/test_tool.py"]


# --- main(): staging, exit codes, the log line --------------------------------


class _Runs:
    """Stand-in for `_run`: records every argv/cwd and replays scripted codes."""

    def __init__(self, codes: list[int]) -> None:
        self.codes = list(codes)
        self.calls: list[tuple[list[str], Path]] = []
        self.envs: list[dict[str, str] | None] = []

    def __call__(self, argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> int:
        self.calls.append((list(argv), cwd))
        self.envs.append(env)
        assert self.codes, "the launcher started more stages than the test scripted"
        return self.codes.pop(0)


@pytest.fixture
def staged(
    tmp_path: Path, launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """A fake repo root with one targeted test file, a fake dev venv beside it,
    and the launcher pointed at that root. Returns (root, python, log_path)."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "tests" / "test_foo.py").write_text("", encoding="utf-8")
    python = _fake_venv(tmp_path)
    monkeypatch.setattr(launcher, "__file__", str(root / "scripts" / "mutation_tests.py"))
    monkeypatch.setattr(launcher, "_changed_sources", lambda _root: ["src/graphite/foo.py"])
    monkeypatch.setattr(launcher, "_canonical_root", lambda given: given)
    monkeypatch.setattr(launcher, "_git", lambda _root, *args: "abc1234")
    return root, python, tmp_path / ".venvs" / "graphite-mutation-tests.log"


def _base(python: Path, *extra: str) -> list[str]:
    return [str(python), "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *extra]


def _log_lines(log_path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def test_main_runs_the_targeted_files_first_then_the_rest_with_them_ignored(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    staged: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, python, log_path = staged
    runs = _Runs([0, 0])
    monkeypatch.setattr(launcher, "_run", runs)
    monkeypatch.setattr(launcher, "_dev_python", lambda *roots: python)

    assert launcher.main(["--maxfail=3"]) == 0

    assert runs.calls == [
        (_base(python, "--maxfail=3", "tests/test_foo.py"), root),
        (_base(python, "--maxfail=3", "tests", "--ignore=tests/test_foo.py"), root),
    ]
    (record,) = _log_lines(log_path)
    assert record["rc"] == 0
    assert record["rc_targeted"] == 0
    assert record["rc_rest"] == 0
    assert record["in_place"] is True
    assert record["head"] == "abc1234"
    assert record["changed"] == ["src/graphite/foo.py"]
    assert record["targeted"] == ["tests/test_foo.py"]
    assert record["extra"] == ["--maxfail=3"]
    assert record["python"] == str(python)
    assert record["root"] == str(root)
    assert record["cwd"] == os.getcwd()
    assert isinstance(record["duration_s"], float)
    assert str(record["at"]).endswith("Z")
    assert "mutation_tests: {" in capsys.readouterr().err


def test_main_stops_at_the_first_targeted_failure(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, staged: tuple[Path, Path, Path]
) -> None:
    root, python, log_path = staged
    runs = _Runs([1])
    monkeypatch.setattr(launcher, "_run", runs)
    monkeypatch.setattr(launcher, "_dev_python", lambda *roots: python)

    assert launcher.main([]) == 1

    assert [argv for argv, _ in runs.calls] == [_base(python, "tests/test_foo.py")]
    (record,) = _log_lines(log_path)
    assert record["rc"] == 1
    assert record["rc_targeted"] == 1
    assert "rc_rest" not in record


def test_main_falls_through_to_the_rest_when_the_targeted_files_hold_no_tests(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, staged: tuple[Path, Path, Path]
) -> None:
    root, python, log_path = staged
    runs = _Runs([5, 3])  # 5 = pytest "no tests collected"; the rest still decides
    monkeypatch.setattr(launcher, "_run", runs)
    monkeypatch.setattr(launcher, "_dev_python", lambda *roots: python)

    assert launcher.main([]) == 3

    assert len(runs.calls) == 2
    (record,) = _log_lines(log_path)
    assert record["rc_targeted"] == 5
    assert record["rc_rest"] == 3
    assert record["rc"] == 3


def test_main_without_targets_runs_a_single_full_stage(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, staged: tuple[Path, Path, Path]
) -> None:
    root, python, log_path = staged
    monkeypatch.setattr(launcher, "_changed_sources", lambda _root: [])
    runs = _Runs([2])
    monkeypatch.setattr(launcher, "_run", runs)
    monkeypatch.setattr(launcher, "_dev_python", lambda *roots: python)

    assert launcher.main([]) == 2

    assert runs.calls == [(_base(python, "tests"), root)]
    (record,) = _log_lines(log_path)
    assert record["targeted"] == []
    assert "rc_targeted" not in record
    assert record["rc_rest"] == 2


def test_main_dry_run_prints_the_stages_and_starts_nothing(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    staged: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, python, log_path = staged
    runs = _Runs([])  # any call would fail the scripted-codes assertion
    monkeypatch.setattr(launcher, "_run", runs)
    monkeypatch.setattr(launcher, "_dev_python", lambda *roots: python)

    assert launcher.main(["--dry-run", "-k", "thing"]) == 0

    assert runs.calls == []
    out = capsys.readouterr().out
    assert out.startswith("targeted: ")
    assert "\nrest: " in out
    assert "--dry-run" not in out  # stripped: it is the launcher's flag, not pytest's
    assert "-k thing" in out
    (record,) = _log_lines(log_path)
    assert record["extra"] == ["-k", "thing"]
    assert record["rc"] == 0


def test_main_reports_a_missing_dev_venv_as_exit_78_without_starting_anything(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    staged: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _python, log_path = staged
    runs = _Runs([])
    monkeypatch.setattr(launcher, "_run", runs)
    monkeypatch.setattr(launcher, "_dev_python", lambda *roots: None)

    assert launcher.main([]) == 78

    assert runs.calls == []
    assert "no dev venv" in capsys.readouterr().err
    # The fallback log location is `<root>/../.venvs/...`, which here is the
    # same directory the fake venv lives in.
    (record,) = _log_lines(log_path)
    assert record["rc"] == 78
    assert record["python"] is None


def test_run_reports_an_unstartable_interpreter_as_127(
    tmp_path: Path, launcher: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "no-such-python"
    assert launcher._run([str(missing), "-m", "pytest"], tmp_path) == 127
    err = capsys.readouterr().err
    # The message must name the interpreter that could not start -- that is
    # the whole diagnostic. aramid's mutation gate found the first version
    # of this test satisfied by a message naming argv[1] instead (row 5180).
    assert "cannot start" in err
    assert str(missing) in err
    assert "-m" not in err.split("cannot start", 1)[1].split(":", 1)[0]


def test_run_returns_the_child_exit_code_and_starts_it_in_the_given_cwd(
    tmp_path: Path, launcher: ModuleType
) -> None:
    # Every main() test above replaces `_run` with a recorder, so this is the
    # only place the real one is exercised; a mutant inside it would otherwise
    # survive the whole suite. The exit code IS the gate's verdict (aramid reads
    # nothing else), and the cwd decides which tree pytest collects, so both
    # must come back exactly as the child produced them.
    marker = "started-here.txt"
    script = f"import pathlib, sys; pathlib.Path({marker!r}).write_text('x'); sys.exit(3)"
    assert launcher._run([sys.executable, "-c", script], tmp_path) == 3
    assert (tmp_path / marker).read_text() == "x"


# --- which git the launcher runs (channel round 304) ---------------------------


def test_git_helper_launches_git_by_an_absolute_path_outside_the_tree(
    launcher: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "worktree"
    trusted = tmp_path / "trusted bin"
    tree.mkdir()
    trusted.mkdir()
    name = "git.exe" if os.name == "nt" else "git"
    real = trusted / name
    for candidate in (real, tree / name):
        candidate.write_text("", encoding="utf-8")
        candidate.chmod(0o755)
    monkeypatch.chdir(tree)
    monkeypatch.setenv("PATH", os.pathsep.join(("", ".", str(tree), str(trusted))))
    seen: list[list[str]] = []

    def record(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(launcher.subprocess, "run", record)

    assert launcher._git(tree, "status") == "ok"
    assert [Path(argv[0]) for argv in seen] == [real.resolve()]


def test_git_helper_skips_a_symlink_in_a_path_entry_inside_the_tree(
    launcher: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # aramid llm-review 1d8d8cf, the same rule as `graphite.programs`: a PATH
    # directory inside the tree is skipped whole, not judged by where a
    # symlink in it points.
    tree, elsewhere = tmp_path / "worktree", tmp_path / "elsewhere"
    name = "git.exe" if os.name == "nt" else "git"
    outside = tmp_path / "outside" / ("other" + Path(name).suffix)
    real = tmp_path / "trusted bin" / name
    for path in (outside, real):
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
        path.chmod(0o755)
    (tree / "bin").mkdir(parents=True)
    try:
        (tree / "bin" / name).symlink_to(outside)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - privilege-dependent
        pytest.skip(f"symlinks unavailable on this machine: {exc}")
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("PATH", os.pathsep.join((str(tree / "bin"), str(real.parent))))

    assert launcher._git_executable(elsewhere) == outside.resolve(), (
        "control: with the tree not refused the symlink is taken"
    )
    assert launcher._git_executable(tree) == real.resolve()


@pytest.mark.parametrize("shape", ["entry links into the tree", "entry inside links out"])
def test_git_helper_judges_a_path_entry_both_as_written_and_as_resolved(
    launcher: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    tree, elsewhere = tmp_path / "worktree", tmp_path / "elsewhere"
    name = "git.exe" if os.name == "nt" else "git"
    chosen = tmp_path / "outside dir" / name
    real = tmp_path / "trusted bin" / name
    for path in (chosen, real):
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
        path.chmod(0o755)
    tree.mkdir()
    try:
        if shape == "entry links into the tree":
            (tree / "bin").mkdir()
            (tree / "bin" / name).symlink_to(chosen)
            entry = tmp_path / "linked bin"
            entry.symlink_to(tree / "bin", target_is_directory=True)
        else:
            entry = tree / "bin"
            entry.symlink_to(chosen.parent, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - privilege-dependent
        pytest.skip(f"symlinks unavailable on this machine: {exc}")
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("PATH", os.pathsep.join((str(entry), str(real.parent))))

    assert launcher._git_executable(elsewhere) == chosen.resolve(), "control: unrefused, the entry is taken"
    assert launcher._git_executable(tree) == real.resolve()

def test_git_helper_does_not_refuse_tools_beneath_its_current_directory(
    launcher: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "worktree"
    tree.mkdir()
    name = "git.exe" if os.name == "nt" else "git"
    real = tmp_path / "Program Files" / "Git" / "cmd" / name
    real.parent.mkdir(parents=True)
    real.write_text("", encoding="utf-8")
    real.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(real.parent))

    assert launcher._git_executable(tree) == real.resolve()

def test_git_helper_with_no_git_outside_the_tree_reads_as_a_failure(
    launcher: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "worktree"
    tree.mkdir()
    (tree / ("git.exe" if os.name == "nt" else "git")).write_text("", encoding="utf-8")
    monkeypatch.chdir(tree)
    monkeypatch.setenv("PATH", str(tree))

    assert launcher._git(tree, "status") == ""


# --- the tree under test is the tree that is imported ------------------------
#
# MEASURED 2026-10-28 on this machine, in aramid's own mutation shape: the
# consumer applies a mutation to a WORKTREE and runs this launcher from it.
# The launcher runs the suite through the dev venv, whose editable install
# names the CANONICAL repository's `src` -- so the worktree's mutation was
# never the code the tests imported, and the gate certified nothing.
#
#   mutation   src/graphite/cli.py:786, `"schema_version": 1,` -> `2`
#   killer     tests/test_published_schemas.py::
#              test_check_output_below_a_repository_root_matches_published_schema
#
#   in the consumer's shape (worktree, no PYTHONPATH)  the killer PASSED
#   with the tree under test on PYTHONPATH            the killer FAILED
#                                                        (`assert 2 == 1`)
#
# The same held for a mutation that makes a module unimportable, at the head
# the consumer itself used (43fdfcf): 589 tests passed in the consumer's shape
# and collection ERRORED with the tree under test imported.
#
# So the import path is pinned here, and PINNED LOUDLY: a launcher that
# cannot confirm the mutated module is the one that will be imported must
# refuse to certify rather than report a clean run.


def _src_layout(root: Path) -> Path:
    """A `src/graphite` package under `root`, as this repository has."""
    package = root / "src" / "graphite"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    return package


def test_package_root_is_the_src_directory_of_a_src_layout(
    tmp_path: Path, launcher: ModuleType
) -> None:
    root = tmp_path / "repo"
    _src_layout(root)

    assert launcher._package_root(root) == root / "src"


def test_package_root_is_the_root_itself_when_there_is_no_src_layout(
    tmp_path: Path, launcher: ModuleType
) -> None:
    root = tmp_path / "repo"
    (root / "graphite").mkdir(parents=True)
    (root / "graphite" / "__init__.py").write_text("", encoding="utf-8")

    assert launcher._package_root(root) == root


def test_test_environment_puts_the_tree_under_test_first_on_the_import_path(
    tmp_path: Path, launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _src_layout(root)
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("PYTHONPATH", str(elsewhere))

    env = launcher._test_environment(root)

    assert env["PYTHONPATH"].split(os.pathsep) == [str(root / "src"), str(elsewhere)]


def test_imported_module_reports_where_the_interpreter_resolves_it(
    tmp_path: Path, launcher: ModuleType
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "thing.py").write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(package)

    assert launcher._imported_module(sys.executable, env, "thing", tmp_path) == str(
        package / "thing.py"
    )
    # A module that cannot be imported is not a verdict about the tree.
    assert launcher._imported_module(sys.executable, env, "no_such_module", tmp_path) is None


def test_main_gives_the_pytest_stages_the_tree_under_test_on_the_import_path(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, staged: tuple[Path, Path, Path]
) -> None:
    root, python, _log_path = staged
    _src_layout(root)
    runs = _Runs([0, 0])
    monkeypatch.setattr(launcher, "_run", runs)
    monkeypatch.setattr(launcher, "_dev_python", lambda *roots: python)
    monkeypatch.setattr(
        launcher,
        "_imported_module",
        lambda *args: str(root / "src" / "graphite" / "foo.py"),
    )

    assert launcher.main([]) == 0

    assert runs.envs, "the stages ran with no environment at all"
    for env in runs.envs:
        assert env is not None
        assert env["PYTHONPATH"].split(os.pathsep)[0] == str(root / "src")


def test_main_refuses_to_certify_a_mutation_imported_from_outside_the_tree(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    staged: tuple[Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, python, log_path = staged
    _src_layout(root)
    runs = _Runs([])
    monkeypatch.setattr(launcher, "_run", runs)
    monkeypatch.setattr(launcher, "_dev_python", lambda *roots: python)
    # The module resolves -- but somewhere else, which is the case the gate
    # must NOT certify: the mutation would never be exercised.
    monkeypatch.setattr(
        launcher,
        "_imported_module",
        lambda *args: str(Path("F:/Projects/graphite/src/graphite/foo.py")),
    )

    assert launcher.main([]) == 70

    assert runs.calls == [], "a stage ran while the tree under test was not the imported one"
    err = capsys.readouterr().err
    assert "outside" in err
    (record,) = _log_lines(log_path)
    assert record["rc"] == 70
    assert record["import_root"] == str(Path("F:/Projects/graphite/src/graphite/foo.py"))


# --- the import probe's budget is a policy, and it is pinned ------------------
#
# MEASURED 2026-10-28: the mutation gate, working for the first time, found
# this survivor -- `mutant survived: 120 -> 121 in _imported_module`. The old
# bare `120` was in the `subprocess.run` call and NOTHING asserted it, so a
# change to it was invisible to the whole suite. A budget nobody checks is a
# budget nobody has decided.
#
# MEASURED 2026-10-29: the SAME class, twice more, in the fix's own new code.
# The boundary `value > 0` was asserted nowhere, so the mutation that moved it
# to `value > 1` survived (ledger 1b1643b5); and `str.isdigit` admits a
# superscript two, which `int` cannot parse, so an operator's value CRASHED
# the launcher instead of falling back. A budget nobody checks is a budget
# nobody has decided -- including its edges.


def test_import_timeout_is_the_pinned_default_without_an_operator_value(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(launcher._IMPORT_TIMEOUT_ENV, raising=False)

    assert launcher._import_timeout_seconds() == 120


def test_import_timeout_follows_the_operator_value(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(launcher._IMPORT_TIMEOUT_ENV, "45")

    assert launcher._import_timeout_seconds() == 45


@pytest.mark.parametrize("nonsense", ["", "  ", "abc", "0", "-5", "1.5", "45s"])
def test_import_timeout_falls_back_to_the_default_on_a_nonsense_value(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, nonsense: str
) -> None:
    monkeypatch.setenv(launcher._IMPORT_TIMEOUT_ENV, nonsense)

    assert launcher._import_timeout_seconds() == 120


def test_import_timeout_honours_the_smallest_positive_value(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The LOWER boundary is a policy too: a positive value is the operator's
    # budget, including the smallest one. The mutation that moved this
    # boundary (`value > 0` -> `value > 1`) SURVIVED the suite -- the drain
    # recorded it on 2026-10-28, against PR #129's own new code.
    monkeypatch.setenv(launcher._IMPORT_TIMEOUT_ENV, "1")

    assert launcher._import_timeout_seconds() == 1


def test_import_timeout_falls_back_on_a_digit_that_is_not_a_number(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `"²".isdigit()` is True and `int("²")` raises, so a value the operator
    # set used to CRASH the gate's launcher instead of falling back. MEASURED
    # 2026-10-29: `GRAPHITE_MUTATION_IMPORT_TIMEOUT_S=²` raised ValueError.
    monkeypatch.setenv(launcher._IMPORT_TIMEOUT_ENV, "²")

    assert launcher._import_timeout_seconds() == 120


def test_imported_module_gives_the_probe_the_configured_budget(
    launcher: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, object]] = []

    def record(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="x\n", stderr="")

    monkeypatch.setattr(launcher.subprocess, "run", record)
    monkeypatch.setenv(launcher._IMPORT_TIMEOUT_ENV, "45")

    launcher._imported_module(Path("python"), dict(os.environ), "thing", tmp_path)

    assert seen and seen[0]["timeout"] == 45
