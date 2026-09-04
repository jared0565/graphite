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


def test_changed_sources_lists_only_python_files_under_src(
    launcher: ModuleType, repo_and_worktree: tuple[Path, Path]
) -> None:
    repo, _ = repo_and_worktree
    (repo / "src").mkdir()
    (repo / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "src" / "notes.txt").write_text("not python\n", encoding="utf-8")
    (repo / "elsewhere.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    assert launcher._changed_sources(repo) == []

    (repo / "src" / "mod.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "src" / "notes.txt").write_text("changed too\n", encoding="utf-8")
    (repo / "elsewhere.py").write_text("y = 3\n", encoding="utf-8")
    assert launcher._changed_sources(repo) == ["src/mod.py"]


# --- main(): staging, exit codes, the log line --------------------------------


class _Runs:
    """Stand-in for `_run`: records every argv/cwd and replays scripted codes."""

    def __init__(self, codes: list[int]) -> None:
        self.codes = list(codes)
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, argv: list[str], cwd: Path) -> int:
        self.calls.append((list(argv), cwd))
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
