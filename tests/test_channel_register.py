"""Registering an agent has to make it actually able to post.

The channel's `commit-msg` hook originally carried a hardcoded allowlist of
agent names. Adding a row to `agents.json` without touching that list would
produce a repo that resolves an identity and is then rejected by the hook on
every commit -- registration that lies. The hook now derives from `agents.json`,
so there is one record rather than two that drift.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from graphite import channel
from graphite.hookshim import sh_interpreter_path


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )


def _channel_with_hook(tmp_path: Path) -> Path:
    root = tmp_path / ".agent-channel"
    (root / "rounds").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(root))
    _git(root, "config", "user.email", "operator@example.com")
    _git(root, "config", "user.name", "Operator")
    (root / "PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")
    channel.write_registry(root, {})
    channel.ensure_channel_hook(root)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--no-verify", "-m", "seed")
    return root


def test_registering_lets_the_agent_actually_post(tmp_path: Path) -> None:
    """The end-to-end property. Passing this requires the hook to accept an
    agent it has never heard of, which is the whole point."""
    root = _channel_with_hook(tmp_path)
    newcomer = tmp_path / "newcomer"
    newcomer.mkdir()

    channel.register_agent(root, newcomer, "codex-agent")

    posted = channel.post_round(root, newcomer, title="Hello", body="first post")
    assert posted["author"] == "codex-agent"
    assert _git(root, "status", "--porcelain").stdout.strip() == ""


def test_an_unregistered_repo_still_cannot_post(tmp_path: Path) -> None:
    root = _channel_with_hook(tmp_path)
    stranger = tmp_path / "stranger"
    stranger.mkdir()

    with pytest.raises(channel.ChannelError) as exc:
        channel.post_round(root, stranger, title="x", body="y")
    assert exc.value.code == "unregistered_project"


def test_the_hook_rejects_a_commit_naming_no_agent(tmp_path: Path) -> None:
    """Falsifiability: if this passed, the hook would not be gating anything."""
    root = _channel_with_hook(tmp_path)
    (root / "rounds" / "x.md").write_text("x\n", encoding="utf-8")
    _git(root, "add", "-A")

    result = _git(root, "commit", "-m", "no trailer here")

    assert result.returncode != 0
    output = result.stderr + result.stdout
    assert "names no agent" in output
    # The guidance has to name an interpreter that EXISTS on the reader's
    # machine, so it interpolates the one the hook just resolved. A literal
    # `$PY` would mean the interpolation broke and the advice is unrunnable;
    # the original text said `python`, which is absent off Windows.
    #
    # `-I`, matching the flag the hook itself uses. It previously printed `-P`,
    # which contradicted the reason `-I` was chosen: `-P` is 3.11+, so on the
    # older interpreters `-I` exists to support, the printed command dies with
    # `Unknown option: -P`. The likely operator recovery is to delete the flag,
    # which lands on exactly the CWD-shadowing form the flag was added to close.
    assert "-I -m graphite channel register" in output, output
    assert "-P -m graphite" not in output, output
    assert "$PY" not in output, output


def test_a_commit_cannot_register_itself_and_use_the_identity(tmp_path: Path) -> None:
    """The trust model is "identity is derived, never declared". The gate used
    to read `agents.json` from the WORKING TREE, which the commit under review
    can itself write -- so adding a row and carrying the matching trailer in one
    commit satisfied the gate, turning a derived identity straight back into a
    declared one.

    Authorisation must come from committed state the commit cannot edit.
    """
    root = _channel_with_hook(tmp_path)
    channel.register_agent(root, tmp_path / "real", "aramid-agent")

    registry = json.loads((root / "agents.json").read_text(encoding="utf-8"))
    registry[str(tmp_path / "attacker")] = "ghost-agent"
    (root / "agents.json").write_text(json.dumps(registry), encoding="utf-8")
    (root / "rounds" / "x.md").write_text("payload\n", encoding="utf-8")
    _git(root, "add", "-A")

    result = _git(
        root, "commit", "-m", "subject\n\nCo-Authored-By: ghost-agent <ghost@agents.local>\n"
    )

    assert result.returncode != 0, "a commit must not be able to authorise itself"


def test_an_agent_registered_in_a_previous_commit_can_still_post(tmp_path: Path) -> None:
    """The other half of the change: reading authorisation from committed state
    must not lock out agents that were registered properly."""
    root = _channel_with_hook(tmp_path)
    channel.register_agent(root, tmp_path / "real", "aramid-agent")
    (root / "rounds" / "x.md").write_text("payload\n", encoding="utf-8")
    _git(root, "add", "-A")

    result = _git(
        root, "commit", "-m", "subject\n\nCo-Authored-By: aramid-agent <aramid@agents.local>\n"
    )

    assert result.returncode == 0, result.stderr


def test_the_registry_cannot_be_deleted_to_escape_the_gate(tmp_path: Path) -> None:
    """Reading from HEAD leaves one escalation: an already-registered agent
    could delete `agents.json`, and the next commit would find no committed
    registry and fall through the bootstrap path. Deleting the registry is
    never legitimate, so it is refused outright."""
    root = _channel_with_hook(tmp_path)
    channel.register_agent(root, tmp_path / "real", "aramid-agent")
    (root / "agents.json").unlink()
    _git(root, "add", "-A")

    result = _git(
        root, "commit", "-m", "subject\n\nCo-Authored-By: aramid-agent <aramid@agents.local>\n"
    )

    assert result.returncode != 0, "deleting the registry must not be a way out of the gate"


@pytest.mark.parametrize("payload", ['{}', "[]", "null", "0", '""'])
def test_emptying_the_registry_cannot_disarm_the_gate(tmp_path: Path, payload: str) -> None:
    """Blocking DELETION left the same escalation one keystroke away.

    The bootstrap branch is armed by `not registry` after `json.loads`, not by a
    missing file. So an already-registered agent could commit `agents.json = {}`
    -- carrying its own valid trailer, which the gate accepts -- and the NEXT
    commit would find no committed authority, fall through to bootstrap, and read
    the WORKING TREE, which is attacker-controlled. One registered agent could
    permanently disarm the gate for everyone.

    Parametrised over the falsy shapes because they arm bootstrap by different
    routes: `{}` is an empty dict, the rest are type-flips that `json.loads`
    accepts happily. A fix written as `if staged == {}` passes the first and
    leaves the other four open.

    Deliberately NOT a superset check. `register_agent` pops the old row when a
    repo is re-registered under a new name (`channel.py:403-406`, covered by
    `test_registering_the_same_repo_again_updates_it`), so requiring the agent
    set to only grow would reject a legitimate rename. Narrowing the registry is
    denial, not impersonation, and needs a valid trailer anyway; only the
    transition to empty hands out authority.
    """
    root = _channel_with_hook(tmp_path)
    channel.register_agent(root, tmp_path / "real", "aramid-agent")

    (root / "agents.json").write_text(payload, encoding="utf-8")
    _git(root, "add", "-A")
    emptied = _git(
        root, "commit", "-m", "subject\n\nCo-Authored-By: aramid-agent <aramid@agents.local>\n"
    )

    # The discriminating assertion. `emptied.returncode != 0` alone would go
    # green for any unrelated rejection -- what has to disappear is the
    # impersonation the emptied registry enables.
    (root / "agents.json").write_text('{"/x": "ghost-agent"}', encoding="utf-8")
    (root / "rounds" / "x.md").write_text("payload\n", encoding="utf-8")
    _git(root, "add", "-A")
    impersonation = _git(
        root, "commit", "-m", "subject\n\nCo-Authored-By: ghost-agent <ghost@agents.local>\n"
    )

    assert impersonation.returncode != 0, (
        "an emptied registry let an unregistered agent commit as itself"
    )
    assert emptied.returncode != 0, "emptying the registry must be refused outright"


def test_emptying_is_refused_by_name_not_by_blaming_the_trailer(tmp_path: Path) -> None:
    """The gate already learned once that a true rejection with a false REASON
    sends you to fix something that was already correct
    (`test_a_missing_interpreter_says_so_instead_of_blaming_the_trailer`). The
    trailer here is valid; saying "names no agent" would be a lie."""
    root = _channel_with_hook(tmp_path)
    channel.register_agent(root, tmp_path / "real", "aramid-agent")
    (root / "agents.json").write_text("{}", encoding="utf-8")
    _git(root, "add", "-A")

    result = _git(
        root, "commit", "-m", "subject\n\nCo-Authored-By: aramid-agent <aramid@agents.local>\n"
    )

    output = result.stderr + result.stdout
    assert result.returncode != 0
    assert "names no agent" not in output, output
    assert "registry" in output.lower(), output


def test_renaming_the_registry_away_is_caught_too(tmp_path: Path) -> None:
    """The deletion guard cannot see this one, and measuring says so.

    `git diff --cached --diff-filter=D --name-only` -- the guard's own command --
    returns EMPTY for a rename, because rename detection is on by default and
    git reports `R100 agents.json agents.json.bak`. So the well-named refusal
    misses the route entirely; what catches it is asking whether the INDEX still
    holds a registry that authorises anyone, which a renamed-away file does not.

    Written because the two-answer test asks whether the problem is gone, not
    whether the named route is closed.
    """
    root = _channel_with_hook(tmp_path)
    channel.register_agent(root, tmp_path / "real", "aramid-agent")
    _git(root, "mv", "agents.json", "agents.json.bak")

    renamed = _git(
        root, "commit", "-m", "subject\n\nCo-Authored-By: aramid-agent <aramid@agents.local>\n"
    )

    (root / "agents.json").write_text('{"/x": "ghost-agent"}', encoding="utf-8")
    (root / "rounds" / "x.md").write_text("payload\n", encoding="utf-8")
    _git(root, "add", "-A")
    impersonation = _git(
        root, "commit", "-m", "subject\n\nCo-Authored-By: ghost-agent <ghost@agents.local>\n"
    )

    assert impersonation.returncode != 0, "renaming the registry away opened the gate"
    assert renamed.returncode != 0, "the registry must not be renamed out of the way"
    assert "names no agent" not in renamed.stderr + renamed.stdout


def test_a_corrupt_committed_registry_wedges_instead_of_opening(tmp_path: Path) -> None:
    """A registry at HEAD that parses but is not an object of rows must reject
    every commit, not fall through to the working tree.

    Reachable only from before the guard existed, or via `--no-verify`, which is
    exactly why it needs a test: the incidental route was `registry.values()`
    raising `AttributeError` into a catch-all, and incidental fail-closed is one
    refactor away from fail-open. It also proves the hook's `$?` really carries
    the interpreter's exit code -- if it read the heredoc instead, STATUS would
    be 0 and this commit would be accepted.
    """
    root = _channel_with_hook(tmp_path)
    channel.register_agent(root, tmp_path / "real", "aramid-agent")
    (root / "agents.json").write_text("[]", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "--no-verify", "-m", "corrupt the registry")

    (root / "rounds" / "x.md").write_text("x\n", encoding="utf-8")
    _git(root, "add", "-A")
    result = _git(
        root, "commit", "-m", "subject\n\nCo-Authored-By: ghost-agent <ghost@agents.local>\n"
    )

    output = result.stderr + result.stdout
    assert result.returncode != 0, "a corrupt registry must not open the gate"
    assert "names no agent" not in output, output
    assert "unusable" in output.lower(), output


def _shim(bin_dir: Path, name: str, target: str) -> None:
    """A PATH entry that forwards to an absolute binary.

    Shims rather than PATH surgery because the two platforms disagree about
    where things live: on Ubuntu `git` and `python3` share `/usr/bin`, so a
    test that wants git-without-python cannot get there by dropping
    directories. Forwarding gives per-command control on both.
    """
    path = bin_dir / name
    path.write_text(f'#!/bin/sh\nexec "{target}" "$@"\n', encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _hook_bin(tmp_path: Path, name: str, *, with_python3: bool) -> Path:
    bin_dir = tmp_path / name
    bin_dir.mkdir()
    git = shutil.which("git")
    assert git is not None, "git is required to run the channel hook"
    _shim(bin_dir, "git", sh_interpreter_path(Path(git)))
    if with_python3:
        _shim(bin_dir, "python3", sh_interpreter_path(Path(sys.executable)))
    return bin_dir


def _hook_path(bin_dir: Path) -> str:
    """PATH for a hook run: the shims, plus what Windows needs to spawn `git`.

    The gate reads its authority from HEAD, so its Python block runs
    `subprocess.run(["git", ...])`. POSIX `exec` honours the shim's shebang and
    runs it happily; Windows `CreateProcess` has no such rule and cannot execute
    an extensionless script, so on Windows a real `git.exe` has to be reachable
    or the gate fails for a reason this test is not about.

    Withholding it looked stricter and measured less. `git`'s directory carries
    no interpreter -- which `_visible_to_the_hook` VERIFIES rather than assumes,
    because that is the whole variable under test.
    """
    entries = [str(bin_dir)]
    if os.name == "nt":
        git = shutil.which("git")
        assert git is not None
        entries.append(str(Path(git).parent))
    return os.pathsep.join(entries)


def _visible_to_the_hook(tmp_path: Path, bin_dir: Path, *names: str) -> set[str]:
    """Which of `names` the hook can actually resolve, measured where it runs.

    Computing this in the pytest process would check the wrong environment: MSYS
    `sh` prepends `/mingw64/bin:/usr/bin` when `MSYSTEM` is NOT inherited, so
    under a PowerShell parent the hook silently regains Git's whole toolchain
    while a Git Bash parent leaves PATH exactly as handed over. That difference
    made this file pass from one shell and fail from the other on identical
    source, so the restriction has to be observed through `sh`, not assumed.
    """
    sh = shutil.which("sh")
    if sh is None:  # pragma: no cover - Git for Windows and POSIX both ship one
        pytest.skip("no POSIX sh available to run the hook")
    probe = tmp_path / "PROBE_PATH.sh"
    probe.write_text(
        "#!/bin/sh\n"
        + "".join(f"command -v {n} >/dev/null 2>&1 && echo {n}\n" for n in names),
        encoding="utf-8",
        newline="\n",
    )
    result = subprocess.run(  # noqa: S603
        [sh, str(probe)],
        cwd=tmp_path,
        env={**os.environ, "PATH": _hook_path(bin_dir)},
        capture_output=True,
        text=True,
        check=False,
    )
    return set(result.stdout.split())


def _run_commit_msg_hook(root: Path, message: str, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the hook directly, with PATH built by `_hook_path`.

    Direct rather than through `git commit` because the point is to control
    which interpreters exist, and git repopulates PATH from the environment.
    """
    sh = shutil.which("sh")
    if sh is None:  # pragma: no cover - Git for Windows and POSIX both ship one
        pytest.skip("no POSIX sh available to run the hook")
    msg_file = root / "HOOK_TEST_MSG"
    msg_file.write_text(message, encoding="utf-8")
    env = {**os.environ, "PATH": _hook_path(bin_dir)}
    return subprocess.run(  # noqa: S603
        [sh, str(root / ".githooks" / "commit-msg"), str(msg_file)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_hook_works_where_the_interpreter_is_called_python3(tmp_path: Path) -> None:
    """Ubuntu ships only `python3` and macOS removed `python` in 12.3, so a hook
    hardcoding `python` rejects EVERY commit off Windows -- the channel's audit
    gate becomes an outage. Windows is the one platform where it works, which is
    why the absence has to be simulated rather than waited for."""
    root = _channel_with_hook(tmp_path)
    channel.register_agent(root, tmp_path / "a", "codex-agent")
    bin_dir = _hook_bin(tmp_path, "only-python3", with_python3=True)

    # Assert the setup ACHIEVED the isolation instead of assuming it. Measured
    # through `sh`, because that is the only place the answer is true -- see
    # `_visible_to_the_hook`. Without this the test passed under a shell that
    # handed back a full PATH, proving nothing about the property it names.
    visible = _visible_to_the_hook(tmp_path, bin_dir, "python", "python3")
    assert visible == {"python3"}, f"interpreters reachable by the hook: {visible or 'none'}"

    result = _run_commit_msg_hook(
        root, "subject\n\nCo-Authored-By: codex-agent <codex@agents.local>\n", bin_dir
    )

    assert result.returncode == 0, result.stderr


def test_a_missing_interpreter_says_so_instead_of_blaming_the_trailer(tmp_path: Path) -> None:
    """The gate must still fail closed, but the REASON has to be true. Falling
    through to "names no agent" sends you to fix a trailer that was already
    correct, which is how this stayed misdiagnosed."""
    root = _channel_with_hook(tmp_path)
    channel.register_agent(root, tmp_path / "a", "codex-agent")
    bin_dir = _hook_bin(tmp_path, "no-python", with_python3=False)

    result = _run_commit_msg_hook(
        root, "subject\n\nCo-Authored-By: codex-agent <codex@agents.local>\n", bin_dir
    )

    output = result.stderr + result.stdout
    assert result.returncode != 0, "a gate that cannot verify must not pass the commit"
    assert "names no agent" not in output, output
    assert "python" in output.lower()


def test_the_hook_rejects_an_agent_that_is_not_registered(tmp_path: Path) -> None:
    root = _channel_with_hook(tmp_path)
    channel.register_agent(root, tmp_path / "a", "aramid-agent")
    (root / "rounds" / "x.md").write_text("x\n", encoding="utf-8")
    _git(root, "add", "-A")

    result = _git(
        root, "commit", "-m", "subject\n\nCo-Authored-By: ghost-agent <ghost@agents.local>\n"
    )

    assert result.returncode != 0


def test_the_hook_rejects_a_mismatched_name_and_address(tmp_path: Path) -> None:
    """The original hardcoded regex allowed `codex-agent <aramid@agents.local>`
    because it alternated the two halves independently. Deriving the pair from
    the registry closes that."""
    root = _channel_with_hook(tmp_path)
    # Broker registered first -- see `test_registering_the_same_repo_again`.
    channel.register_agent(root, tmp_path / "graphite", "graphite-agent")
    channel.register_agent(root, tmp_path / "a", "aramid-agent")
    channel.register_agent(root, tmp_path / "c", "codex-agent")
    (root / "rounds" / "x.md").write_text("x\n", encoding="utf-8")
    _git(root, "add", "-A")

    result = _git(
        root, "commit", "-m", "subject\n\nCo-Authored-By: codex-agent <aramid@agents.local>\n"
    )

    assert result.returncode != 0


def test_a_malformed_registry_fails_closed(tmp_path: Path) -> None:
    """An unreadable registry must reject, never wave commits through."""
    root = _channel_with_hook(tmp_path)
    (root / "agents.json").write_text("{not json", encoding="utf-8")
    (root / "rounds" / "x.md").write_text("x\n", encoding="utf-8")
    _git(root, "add", "-A")

    result = _git(
        root, "commit", "-m", "subject\n\nCo-Authored-By: graphite-agent <graphite@agents.local>\n"
    )

    assert result.returncode != 0


def test_a_malformed_agent_id_is_refused(tmp_path: Path) -> None:
    root = _channel_with_hook(tmp_path)

    for bad in ("Codex-Agent", "codex", "codex agent", "", "../evil-agent"):
        with pytest.raises(channel.ChannelError) as exc:
            channel.register_agent(root, tmp_path / "x", bad)
        assert exc.value.code == "invalid_agent_id", bad


def test_registering_the_same_repo_again_updates_it(tmp_path: Path) -> None:
    root = _channel_with_hook(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()

    # The broker mutates authority state, so it must hold authority itself
    # before it can broker a second registration. A real channel registers
    # graphite first; without this the third call raises `broker_unregistered`.
    channel.register_agent(root, tmp_path / "graphite", "graphite-agent")
    channel.register_agent(root, repo, "codex-agent")
    result = channel.register_agent(root, repo, "aramid-agent")

    assert result["previous"] == "codex-agent"
    assert channel.derive_identity(root, repo) == "aramid-agent"


def test_registration_is_credited_to_graphite_not_the_new_agent(tmp_path: Path) -> None:
    """Graphite performed the registration. Crediting the agent being registered
    would read as "codex registered itself", which is false -- and the trailer
    exists precisely to keep that kind of claim honest."""
    root = _channel_with_hook(tmp_path)
    channel.register_agent(root, tmp_path / "g", "graphite-agent")

    channel.register_agent(root, tmp_path / "repo", "codex-agent")

    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    message = _git(root, "log", "-1", "--format=%B").stdout
    assert "graphite-agent" in message
    assert "codex-agent <codex@" not in message
    assert json.loads((root / "agents.json").read_text(encoding="utf-8"))


def test_the_bootstrap_registration_is_signed_by_whoever_it_registers(tmp_path: Path) -> None:
    """In an empty channel graphite is not yet registered, so the hook would
    reject a graphite trailer. Someone has to write the first row; that one
    commit is structurally unverifiable and the fallback is deliberate."""
    root = _channel_with_hook(tmp_path)

    channel.register_agent(root, tmp_path / "repo", "codex-agent")

    assert "codex-agent" in _git(root, "log", "-1", "--format=%B").stdout


def test_cli_register(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from graphite.cli import main

    monkeypatch.setenv("GRAPHITE_PROJECTS_ROOT", str(tmp_path))
    _channel_with_hook(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()

    assert main(["channel", "register", str(repo), "codex-agent"]) == 0
    assert "codex-agent" in capsys.readouterr().out
