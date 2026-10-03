"""Resolving external programs without consulting the current directory.

Channel round 304: on Windows a bare program name is looked up in the CALLER's
current directory before PATH -- by CreateProcess for a list-form
`subprocess.run(["git", ...])`, by cmd.exe, and by `shutil.which` -- unless
NoDefaultCurrentDirectoryInExePath is set. graphite's builds, hooks and broker
run with a repository root as their current directory, so a program planted
there ran in place of the real one. Every test here plants the hostile
candidate in the current directory AND inside the excluded root, and gives
PATH the empty, relative and in-repo entries an attacker would want honoured.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

from graphite import programs


def _plant(path: Path, body: str = "planted") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o755)
    return path


def _elsewhere(tmp_path: Path) -> Path:
    """A current directory that holds nothing. The cwd's whole subtree is
    excluded, so a trusted bin directory must not sit beneath it."""
    cwd = tmp_path / "elsewhere"
    cwd.mkdir(exist_ok=True)
    return cwd


def _symlink_or_skip(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - privilege-dependent
        pytest.skip(f"symlinks unavailable on this machine: {exc}")


def test_a_symlink_on_an_excluded_path_entry_cannot_pick_an_outside_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # aramid llm-review 1d8d8cf. Only the candidate's RESOLVED target was
    # judged, so a symlink committed in an in-repo PATH directory (POSIX git
    # keeps committed symlinks) passed whenever it pointed outside the repo --
    # letting the repository choose which outside binary ran as `git`.
    repo = tmp_path / "repo"
    name = "git.exe" if os.name == "nt" else "git"
    outside = _plant(tmp_path / "outside" / ("other" + Path(name).suffix))
    trusted = _plant(tmp_path / "trusted bin" / name)
    _symlink_or_skip(repo / "bin" / name, outside)
    monkeypatch.chdir(_elsewhere(tmp_path))
    monkeypatch.setenv("PATH", os.pathsep.join((str(repo / "bin"), str(trusted.parent))))

    assert programs.resolve_program("git") == outside.resolve(), (
        "control: with nothing excluded the symlink is taken"
    )
    assert programs.resolve_program("git", exclude=(repo,)) == trusted.resolve()


def test_a_path_entry_that_links_into_an_excluded_root_is_judged_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other spelling: the PATH entry itself sits OUTSIDE the repo but is a
    # directory link INTO it, so only its resolved form is inside.
    repo = tmp_path / "repo"
    name = "git.exe" if os.name == "nt" else "git"
    outside = _plant(tmp_path / "outside" / ("other" + Path(name).suffix))
    trusted = _plant(tmp_path / "trusted bin" / name)
    _symlink_or_skip(repo / "bin" / name, outside)
    entry = tmp_path / "linked bin"
    try:
        entry.symlink_to(repo / "bin", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - privilege-dependent
        pytest.skip(f"directory symlinks unavailable on this machine: {exc}")
    monkeypatch.chdir(_elsewhere(tmp_path))
    monkeypatch.setenv("PATH", os.pathsep.join((str(entry), str(trusted.parent))))

    assert programs.resolve_program("git") == outside.resolve(), (
        "control: with nothing excluded the linked entry is taken"
    )
    assert programs.resolve_program("git", exclude=(repo,)) == trusted.resolve()


def test_a_path_entry_spelled_inside_an_excluded_root_is_refused_wherever_it_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The mirror case: the entry is written INSIDE the repo but is a committed
    # directory link to somewhere outside it, so only its written form is
    # inside. Judging the resolved form alone would let the repo point PATH at
    # any outside directory of its choosing.
    repo = tmp_path / "repo"
    name = "git.exe" if os.name == "nt" else "git"
    chosen = _plant(tmp_path / "outside dir" / name)
    trusted = _plant(tmp_path / "trusted bin" / name)
    repo.mkdir()
    try:
        (repo / "bin").symlink_to(chosen.parent, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - privilege-dependent
        pytest.skip(f"directory symlinks unavailable on this machine: {exc}")
    monkeypatch.chdir(_elsewhere(tmp_path))
    monkeypatch.setenv("PATH", os.pathsep.join((str(repo / "bin"), str(trusted.parent))))

    assert programs.resolve_program("git") == chosen.resolve(), (
        "control: with nothing excluded the linked directory is taken"
    )
    assert programs.resolve_program("git", exclude=(repo,)) == trusted.resolve()


def test_the_cwd_subtree_is_excluded_not_only_the_cwd_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _plant(repo / "node_modules" / ".bin" / "node.exe")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", str(repo / "node_modules" / ".bin"))

    assert programs.resolve_program("node", platform_name="nt") is None


def test_never_resolves_into_the_current_directory_or_the_excluded_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "untrusted repo"
    trusted = tmp_path / "trusted bin"
    _plant(repo / "node.exe")
    _plant(repo / "tools" / "node.exe")
    real = _plant(trusted / "node.exe", "real")
    monkeypatch.chdir(repo)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(("", ".", "tools", str(repo), str(repo / "tools"), str(trusted))),
    )

    found = programs.resolve_program("node", exclude=(repo,), platform_name="nt")

    assert found == real.resolve()
    assert found.is_absolute()


def test_the_current_directory_is_excluded_even_when_it_is_not_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The channel broker's target root is `.agent-channel`, while the MCP
    # process stands in the AGENT's repository: both must be refused.
    channel = tmp_path / "channel"
    agent_repo = tmp_path / "agent repo"
    trusted = tmp_path / "trusted bin"
    channel.mkdir()
    _plant(agent_repo / "git.exe")
    real = _plant(trusted / "git.exe", "real")
    monkeypatch.chdir(agent_repo)
    monkeypatch.setenv("PATH", os.pathsep.join((str(agent_repo), str(trusted))))

    assert programs.resolve_program("git", exclude=(channel,), platform_name="nt") == real.resolve()


def test_a_relative_path_entry_that_climbs_out_of_the_cwd_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Excluding the cwd does not cover this one: `..\sibling` resolves OUTSIDE
    # it, to wherever the current directory happens to sit. A relative entry
    # means something different in every directory, so none is read at all.
    repo = tmp_path / "repo"
    repo.mkdir()
    _plant(tmp_path / "sibling" / "node.exe")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", os.path.join("..", "sibling"))

    assert programs.resolve_program("node", exclude=(repo,), platform_name="nt") is None


def test_returns_none_rather_than_a_planted_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _plant(repo / "git.exe")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", os.pathsep.join(("", ".", str(repo))))

    assert programs.resolve_program("git", exclude=(repo,), platform_name="nt") is None


def test_windows_names_default_to_exe_only_like_createprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = tmp_path / "bin"
    _plant(trusted / "codex.cmd")
    monkeypatch.chdir(_elsewhere(tmp_path))
    monkeypatch.setenv("PATH", str(trusted))

    assert programs.resolve_program("codex", platform_name="nt") is None
    found = programs.resolve_program("codex", extensions=(".exe", ".cmd"), platform_name="nt")
    assert found == (trusted / "codex.cmd").resolve()


def test_extensions_are_tried_in_order_within_each_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _plant(first / "claude.cmd")
    _plant(first / "claude.exe")
    _plant(second / "claude.exe")
    monkeypatch.chdir(_elsewhere(tmp_path))
    monkeypatch.setenv("PATH", os.pathsep.join((str(first), str(second))))

    found = programs.resolve_program("claude", extensions=(".exe", ".cmd"), platform_name="nt")

    assert found == (first / "claude.exe").resolve()


def test_a_name_that_already_carries_an_extension_is_used_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = tmp_path / "bin"
    real = _plant(trusted / "schtasks.exe")
    monkeypatch.chdir(_elsewhere(tmp_path))
    monkeypatch.setenv("PATH", str(trusted))

    assert programs.resolve_program("schtasks.exe", platform_name="nt") == real.resolve()


@pytest.mark.parametrize("name", ["", "bin/git", "..\\git", str(Path.cwd() / "git")])
def test_only_a_bare_name_is_accepted(name: str) -> None:
    with pytest.raises(ValueError):
        programs.resolve_program(name)


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute bit; Windows has none to check")
def test_posix_skips_a_candidate_without_the_execute_bit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    unexecutable = _plant(first / "git")
    unexecutable.chmod(0o644)
    real = _plant(second / "git", "real")
    monkeypatch.chdir(_elsewhere(tmp_path))
    monkeypatch.setenv("PATH", os.pathsep.join((str(first), str(second))))

    assert programs.resolve_program("git") == real.resolve()


@pytest.mark.skipif(os.name == "nt", reason="an empty PATH entry means the cwd on POSIX only")
def test_posix_empty_path_entry_is_not_read_as_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant(tmp_path / "git")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(("", ":")))

    assert programs.resolve_program("git") is None


def test_require_program_raises_file_not_found_naming_the_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")

    with pytest.raises(FileNotFoundError) as caught:
        programs.require_program("git")

    assert caught.value.filename == "git"


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows system directory")
def test_system_program_is_anchored_in_the_real_system_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The API answer, not the environment: SystemRoot is caller-controlled.
    monkeypatch.setenv("SystemRoot", r"C:\nowhere")
    monkeypatch.setenv("windir", r"C:\nowhere")

    schtasks = programs.system_program("schtasks.exe")
    powershell = programs.system_program("WindowsPowerShell", "v1.0", "powershell.exe")

    assert schtasks.is_file()
    assert powershell.is_file()
    assert schtasks.parent == powershell.parents[2]
    assert "nowhere" not in str(schtasks).lower()


# --- the guard: nothing launches a program by a bare literal name --------------
#
# Inventory by CALL, not by what an argv happens to start with: the seventh `-P`
# surface hid behind a wrapper because a sweep looked for a shape. A literal
# program name in a launch position is a bare name -- resolved by the OS, from
# the current directory first on Windows -- or a hardcoded path; either way it
# bypasses `resolve_program`. A program held in a variable is not judged here;
# the per-site tests pin those.

_REPO = Path(__file__).resolve().parents[1]
_LAUNCHERS = {
    ("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),
    ("subprocess", "check_call"), ("subprocess", "check_output"),
    ("asyncio", "create_subprocess_exec"), ("asyncio", "create_subprocess_shell"),
    ("os", "system"), ("os", "startfile"), ("os", "execv"), ("os", "execvp"),
    ("os", "execvpe"), ("os", "spawnv"), ("os", "spawnvp"),
}


def _launch_violations(source: str, label: str, *, which_banned: bool) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        owner = func.value.id if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else None
        attr = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if which_banned and (owner, attr) == ("shutil", "which"):
            found.append(f"{label}:{node.lineno} shutil.which")
            continue
        if (owner, attr) not in _LAUNCHERS and attr != "spawn_detached":
            continue
        for keyword in node.keywords:
            if keyword.arg == "shell" and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False):
                found.append(f"{label}:{node.lineno} shell=")
        if not node.args:
            continue
        program = node.args[0]
        if isinstance(program, (ast.List, ast.Tuple)) and program.elts:
            program = program.elts[0]
        if isinstance(program, ast.Constant) and isinstance(program.value, str):
            found.append(f"{label}:{node.lineno} literal program {program.value!r}")
    return found


def _embedded_hook_program() -> str:
    from graphite import channel

    opener = "<<'PYEOF'\n"
    start = channel.COMMIT_MSG_HOOK.index(opener) + len(opener)
    return channel.COMMIT_MSG_HOOK[start : channel.COMMIT_MSG_HOOK.index("\nPYEOF\n", start)]


def test_the_guard_flags_every_shape_it_exists_to_refuse() -> None:
    # The positive control: a guard that cannot fail is not a guard.
    sample = "\n".join(
        (
            "import asyncio, os, shutil, subprocess",
            'subprocess.run(["git", "status"])',
            'subprocess.Popen(("node", "x.mjs"))',
            'subprocess.check_output("git status", shell=True)',
            'os.system("schtasks.exe /Query")',
            'spawn_detached(["python", "-m", "graphite"])',
            'asyncio.create_subprocess_exec("node")',
            'shutil.which("claude")',
            "subprocess.run([str(resolved), 'status'])",
            "subprocess.run([sys.executable, '-P', '-m', 'graphite'])",
        )
    )

    found = _launch_violations(sample, "sample", which_banned=True)

    assert [line.split(" ", 1)[1] for line in found] == [
        "literal program 'git'",
        "literal program 'node'",
        "shell=",
        "literal program 'git status'",
        "literal program 'schtasks.exe /Query'",
        "literal program 'python'",
        "literal program 'node'",
        "shutil.which",
    ]


def test_no_source_launches_a_program_by_a_literal_name() -> None:
    found: list[str] = []
    for root, which_banned in ((_REPO / "src" / "graphite", True), (_REPO / "scripts", False)):
        for path in sorted(root.rglob("*.py")):
            label = path.relative_to(_REPO).as_posix()
            found += _launch_violations(path.read_text(encoding="utf-8"), label, which_banned=which_banned)
    found += _launch_violations(_embedded_hook_program(), "channel.COMMIT_MSG_HOOK", which_banned=True)

    assert found == []
