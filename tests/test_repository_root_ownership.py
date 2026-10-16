"""Whose directory graphite takes for the repository from below it.

From a folder below a repository's root, the agent hooks (since #80) and the
CLI's reads (#81) take that root for the repository: its graph is read, its
ledger written, and the root registered with the daemon. The root is the
nearest directory upward that holds `.git`. Nothing asked who made that
directory or its `.git`.

An ancestor is now taken only when the current user owns it and its `.git`,
and never a drive or filesystem root. Anywhere else the folder stands for
itself, as it did before #81. `build`, `report`, `scan` and `check` are still
refused below such a `.git`, but no longer name it as the place to run.

Who owns a path is asked of the operating system (`graphite.ownership`). On
Windows the decision is a pure function over SID strings, so each of its arms
runs on every platform; the calls that gather its inputs run on real paths.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from graphite import activation, agent_hooks, ingest, ownership
from graphite.cli import main
from graphite.git import GitRunner

USER = "S-1-5-21-1000000001-1000000002-1000000003-1001"
OTHER = "S-1-5-21-1000000001-1000000002-1000000003-1002"
SYSTEM = "S-1-5-18"

APP = {
    "app/__init__.py": "",
    "app/core.py": "def compute_total(items):\n    return sum(items)\n",
}


# --- the decision on Windows, as a pure function -----------------------------


@pytest.mark.parametrize(
    ("owner", "elevated", "owned"),
    [
        (USER, False, True),
        (USER, True, True),
        (ownership.ADMINISTRATORS, True, True),
        (ownership.ADMINISTRATORS, False, False),
        (OTHER, True, False),
        (OTHER, False, False),
        (SYSTEM, True, False),
        ("", True, False),
    ],
)
def test_who_owns_a_path_on_windows(owner: str, elevated: bool, owned: bool) -> None:
    """The owner is the user, or the Administrators group while this process
    runs with an elevated administrator's token, since a Windows policy can
    make that group the owner of what such a process creates. Nobody else."""
    assert ownership.owner_is_current_user(owner, USER, elevated_admin=elevated) is owned


def test_an_unknown_user_owns_nothing() -> None:
    """Two SIDs that could not be read are not the same owner."""
    assert ownership.owner_is_current_user("", "", elevated_admin=False) is False
    assert ownership.owner_is_current_user(ownership.ADMINISTRATORS, "", elevated_admin=True) is False


# --- ownership of real paths -------------------------------------------------


def test_a_directory_made_here_is_owned(tmp_path: Path) -> None:
    assert ownership.owned_by_current_user(tmp_path) is True


def test_a_file_made_here_is_owned(tmp_path: Path) -> None:
    made = tmp_path / "made.txt"
    made.write_text("x", encoding="utf-8")
    assert ownership.owned_by_current_user(made) is True


def test_a_name_outside_ascii_is_asked_about_as_it_is(tmp_path: Path) -> None:
    named = tmp_path / "répertoire-目录"
    named.mkdir()
    assert ownership.owned_by_current_user(named) is True


def test_a_path_that_is_not_there_is_owned_by_no_one(tmp_path: Path) -> None:
    assert ownership.owned_by_current_user(tmp_path / "not-there") is False


def test_a_directory_of_the_system_is_not_the_users() -> None:
    if sys.platform == "win32":
        system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    else:
        if os.geteuid() == 0:
            pytest.skip("this process runs as root, which owns /")
        system = Path("/")
    assert ownership.owned_by_current_user(system) is False


def test_the_elevated_token_is_read_as_windows_reads_it() -> None:
    """The Administrators arm turns on whether this process runs elevated.
    `IsUserAnAdmin` asks the same question by another road: a token that
    counts the group, not one that holds it for denying only."""
    if sys.platform != "win32":
        pytest.skip("Windows only: an elevated token")
    import ctypes

    is_user_an_admin = ctypes.WinDLL("shell32").IsUserAnAdmin
    is_user_an_admin.argtypes = []
    is_user_an_admin.restype = ctypes.c_int
    assert ownership._windows_is_elevated_admin() is bool(is_user_an_admin())


def test_which_owner_a_new_directory_has_on_windows(tmp_path: Path) -> None:
    """Which arm of the decision a new directory passes on this machine.

    The passing state is a skip whose reason names the arm: CI prints it
    (`-rs`). Where Windows gives an elevated administrator's files to the
    Administrators group, every test below the root of a repository made
    there relies on the second arm, and this says whether that machine is
    one."""
    if sys.platform != "win32":
        pytest.skip("Windows only: which SID owns a new directory")
    owner = ownership._windows_owner_sid(tmp_path)
    user = ownership._windows_user_sid()
    elevated = ownership._windows_is_elevated_admin()
    if owner and owner == user:
        arm = "the user"
    elif owner == ownership.ADMINISTRATORS and elevated:
        arm = "the Administrators group, with an elevated token"
    else:
        pytest.fail(f"neither arm: owner={owner} user={user} elevated={elevated}")
    assert ownership.owned_by_current_user(tmp_path) is True
    pytest.skip(f"a new directory is owned by {arm}")


# --- the rule: which ancestor is taken ---------------------------------------


def _git_init(directory: Path) -> None:
    """`git init` through graphite's own runner: the git it resolves, in an
    isolated environment, with `directory` as its working directory."""
    directory.mkdir(parents=True, exist_ok=True)
    GitRunner(directory).run(["init", "--quiet"], timeout_seconds=60)


def _set_up(root: Path) -> Path:
    """A git repository graphite is set up in (its committed marker), with a
    folder `app/` below its root."""
    (root / "app").mkdir(parents=True)
    _git_init(root)
    (root / "GRAPHITE.md").write_text("set up\n", encoding="utf-8")
    return root


def _owned_except(monkeypatch: pytest.MonkeyPatch, *not_owned: Path) -> None:
    """Ownership as asked: every path is the user's except `not_owned`."""
    refused = {path.resolve() for path in not_owned}
    monkeypatch.setattr(
        ownership, "owned_by_current_user", lambda path: Path(path).resolve() not in refused
    )


def test_an_owned_root_is_taken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _set_up(tmp_path / "repo").resolve()
    _owned_except(monkeypatch)
    assert ingest.repository_root(root / "app") == root


def test_a_root_someone_else_owns_is_not_taken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _set_up(tmp_path / "repo").resolve()
    _owned_except(monkeypatch, root)
    assert ingest.repository_root(root / "app") == root / "app"
    assert ingest.trusted_root(root) is False


def test_a_root_whose_git_someone_else_owns_is_not_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory is the user's and its `.git` is not."""
    root = _set_up(tmp_path / "repo").resolve()
    _owned_except(monkeypatch, root / ".git")
    assert ingest.repository_root(root / "app") == root / "app"
    assert ingest.trusted_root(root) is False


def test_a_drive_or_filesystem_root_is_never_taken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _owned_except(monkeypatch)
    top = Path(tmp_path.resolve().anchor)
    assert ingest.trusted_root(top) is False
    below = tmp_path.resolve()
    monkeypatch.setattr(ingest, "enclosing_git_root", lambda path: top)
    monkeypatch.setattr(ingest, "_is_set_up", lambda path: True)
    assert ingest.repository_root(below) == below


def test_ownership_is_asked_only_where_the_root_would_be_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hooks ask for the root at every tool call: below a checkout
    graphite is not set up in, nobody is asked who owns it."""
    root = tmp_path / "plain"
    (root / "app").mkdir(parents=True)
    _git_init(root)
    asked: list[Path] = []
    monkeypatch.setattr(ownership, "owned_by_current_user", lambda path: asked.append(Path(path)) or True)
    assert ingest.repository_root((root / "app").resolve()) == (root / "app").resolve()
    assert asked == []


def test_the_hooks_take_the_same_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _set_up(tmp_path / "repo").resolve()
    _owned_except(monkeypatch, root)
    assert agent_hooks._repo_root(root / "app") == root / "app"


def test_the_hooks_register_nothing_below_a_root_someone_else_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _set_up(tmp_path / "repo").resolve()
    _owned_except(monkeypatch, root)
    agent_hooks._activate(agent_hooks._repo_root(root / "app"))
    assert list(activation.read_active()) == []


# --- the CLI below a root someone else owns ----------------------------------


@pytest.fixture
def foreign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Path:
    """A repository with a graph built at its root, then asked about as if
    another user owned that root."""
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git_init(root)
    for name, text in APP.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    monkeypatch.chdir(root)
    assert main(["build", "."]) == 0
    for marker in activation.active_dir().glob("*.json"):
        marker.unlink()
    capsys.readouterr()
    _owned_except(monkeypatch, root)
    return root


def _run(
    directory: Path, argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    monkeypatch.chdir(directory)
    capsys.readouterr()
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _files_under(directory: Path) -> set[str]:
    return {path.relative_to(directory).as_posix() for path in directory.rglob("*")} if directory.exists() else set()


def test_a_read_below_a_root_someone_else_owns_does_not_follow_it(
    foreign: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The read answers as it did before #81, from the folder it is typed in;
    nothing is written into the root's ledger and nothing is registered."""
    ledger_before = _files_under(foreign / ".graphite")
    code, out, err = _run(foreign / "app", ["query", "stats"], monkeypatch, capsys)
    assert code == 1
    assert "graph_missing" in out + err
    assert _files_under(foreign / ".graphite") == ledger_before
    assert list(activation.read_active()) == []


def test_build_below_a_root_someone_else_owns_is_refused_without_sending_anyone_there(
    foreign: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _out, err = _run(foreign / "app", ["build", "."], monkeypatch, capsys)
    assert code == 1
    assert "run it from" not in err
    assert "not owned by the current user" in err
    assert not (foreign / "app" / ".cache").exists()


def test_check_below_a_root_someone_else_owns_names_no_root_to_run_from(
    foreign: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _err = _run(foreign / "app", ["check", ".", "--json"], monkeypatch, capsys)
    answer = json.loads(out)
    assert code == 1
    assert answer["reason"] == "below_repository_root"
    assert answer["repository_root"] is None
    code, _out, err = _run(foreign / "app", ["check", "."], monkeypatch, capsys)
    assert code == 1
    assert "run it from" not in err


def test_below_an_owned_root_check_still_names_it(
    foreign: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _owned_except(monkeypatch)
    code, out, _err = _run(foreign / "app", ["check", ".", "--json"], monkeypatch, capsys)
    assert code == 1
    assert json.loads(out)["repository_root"] == foreign.as_posix()
