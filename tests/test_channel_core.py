"""Core of the agent-channel broker: identity, allocation, and create-only posts.

The security property under test throughout is that an agent cannot name itself.
All three agents commit under the operator's git identity, so the trailer is the
only answer to "who" -- an author that a caller could supply would make the whole
channel unauditable. Identity is derived from the repo the broker runs in, and an
unregistered repo is refused rather than defaulted.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from graphite import channel


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _make_channel(tmp_path: Path, registry: dict[str, str] | None = None) -> Path:
    root = tmp_path / ".agent-channel"
    (root / "rounds").mkdir(parents=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "operator@example.com")
    _git(root, "config", "user.name", "Operator")
    (root / "PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")
    if registry is not None:
        channel.write_registry(root, registry)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _repo(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


# --- identity ---------------------------------------------------------------


def test_identity_is_derived_from_the_repo_the_broker_runs_in(tmp_path: Path) -> None:
    aramid = _repo(tmp_path, "aramid")
    root = _make_channel(tmp_path, {str(aramid): "aramid-agent"})

    assert channel.derive_identity(root, aramid) == "aramid-agent"


def test_an_unregistered_repo_is_refused_not_defaulted(tmp_path: Path) -> None:
    """A permissive default here is the whole design's weak point: it would let
    any unregistered checkout post under a name nobody assigned it."""
    stranger = _repo(tmp_path, "stranger")
    root = _make_channel(tmp_path, {str(_repo(tmp_path, "aramid")): "aramid-agent"})

    with pytest.raises(channel.ChannelError) as exc:
        channel.derive_identity(root, stranger)
    assert exc.value.code == "unregistered_project"


def test_identity_survives_a_non_canonical_path_spelling(tmp_path: Path) -> None:
    """Registry lookup must not be defeated by `..` or a trailing separator --
    otherwise the refusal above becomes trivial to trip by accident."""
    aramid = _repo(tmp_path, "aramid")
    root = _make_channel(tmp_path, {str(aramid): "aramid-agent"})

    spelled = aramid.parent / "." / "aramid"
    assert channel.derive_identity(root, spelled) == "aramid-agent"


def test_post_has_no_parameter_that_could_name_a_different_author(tmp_path: Path) -> None:
    """Structural guard. If someone later adds an `author=` argument, the
    derivation above becomes advisory and this test should stop them."""
    import inspect

    params = set(inspect.signature(channel.post_round).parameters)
    assert "author" not in params
    assert "agent" not in params


# --- allocation -------------------------------------------------------------


def test_the_broker_allocates_the_round_number(tmp_path: Path) -> None:
    aramid = _repo(tmp_path, "aramid")
    root = _make_channel(tmp_path, {str(aramid): "aramid-agent"})

    first = channel.post_round(root, aramid, title="One", body="a")
    second = channel.post_round(root, aramid, title="Two", body="b")

    assert first["round"] == 1
    assert second["round"] == 2


def test_allocation_continues_past_pre_existing_rounds(tmp_path: Path) -> None:
    """The live channel already holds rounds 1-42 written by hand; the broker
    must not restart at 1 and collide with them."""
    aramid = _repo(tmp_path, "aramid")
    root = _make_channel(tmp_path, {str(aramid): "aramid-agent"})
    (root / "rounds" / "2026-07-31-legacy-round-42-something.md").write_text("x", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "legacy")

    assert channel.post_round(root, aramid, title="Next", body="b")["round"] == 43


# --- create-only ------------------------------------------------------------


def test_the_agent_never_supplies_a_path(tmp_path: Path) -> None:
    """Traversal is removed as a class rather than filtered for: the filename is
    generated, so a hostile title cannot escape rounds/."""
    aramid = _repo(tmp_path, "aramid")
    root = _make_channel(tmp_path, {str(aramid): "aramid-agent"})

    posted = channel.post_round(
        root, aramid, title="../../etc/passwd and \\ other: junk", body="b"
    )

    written = Path(posted["path"])
    assert written.parent.name == "rounds"
    assert ".." not in written.name
    assert (root / "rounds" / written.name).is_file()


def test_posting_refuses_to_overwrite_an_existing_round(tmp_path: Path) -> None:
    aramid = _repo(tmp_path, "aramid")
    root = _make_channel(tmp_path, {str(aramid): "aramid-agent"})
    posted = channel.post_round(root, aramid, title="One", body="a")

    with pytest.raises(channel.ChannelError) as exc:
        channel.post_round(root, aramid, title="One", body="a", _force_number=posted["round"])
    assert exc.value.code == "round_exists"


# --- stamped metadata + commit ---------------------------------------------


def test_the_round_carries_stamped_front_matter(tmp_path: Path) -> None:
    aramid = _repo(tmp_path, "aramid")
    root = _make_channel(tmp_path, {str(aramid): "aramid-agent"})

    posted = channel.post_round(
        root, aramid, title="Please run init", body="Body text.", to=["graphite-agent"]
    )
    meta, body = channel.parse_round(Path(posted["path"]).read_text(encoding="utf-8"))

    assert meta["author"] == "aramid-agent"
    assert meta["round"] == 1
    assert meta["to"] == ["graphite-agent"]
    assert meta["title"] == "Please run init"
    assert meta["posted"].endswith("Z")
    assert "Body text." in body


def test_every_post_is_committed_with_the_agents_trailer(tmp_path: Path) -> None:
    """The channel's commit-msg hook rejects commits naming no agent. The broker
    must satisfy that hook rather than route around it."""
    aramid = _repo(tmp_path, "aramid")
    root = _make_channel(tmp_path, {str(aramid): "aramid-agent"})

    posted = channel.post_round(root, aramid, title="One", body="a")

    assert _git(root, "status", "--porcelain").strip() == ""
    message = _git(root, "log", "-1", "--format=%B")
    assert "Co-Authored-By: aramid-agent <aramid@agents.local>" in message
    assert posted["commit"] == _git(root, "rev-parse", "--short", "HEAD").strip()


def test_a_post_never_touches_anything_outside_rounds(tmp_path: Path) -> None:
    aramid = _repo(tmp_path, "aramid")
    root = _make_channel(tmp_path, {str(aramid): "aramid-agent"})
    before = (root / "PROTOCOL.md").read_text(encoding="utf-8")

    channel.post_round(root, aramid, title="One", body="a")

    assert (root / "PROTOCOL.md").read_text(encoding="utf-8") == before
    changed = _git(root, "show", "--name-only", "--format=", "HEAD").split()
    assert all(name.startswith("rounds/") for name in changed), changed


# --- reading ----------------------------------------------------------------


def test_rounds_can_be_listed_and_read_back(tmp_path: Path) -> None:
    aramid = _repo(tmp_path, "aramid")
    root = _make_channel(tmp_path, {str(aramid): "aramid-agent"})
    channel.post_round(root, aramid, title="One", body="first")
    channel.post_round(root, aramid, title="Two", body="second")

    listed = channel.list_rounds(root)
    assert [r.number for r in listed] == [1, 2]
    assert [r.title for r in listed] == ["One", "Two"]
    assert "second" in channel.read_round(root, 2).body


def test_a_legacy_round_without_front_matter_still_lists(tmp_path: Path) -> None:
    """37 relocated rounds have no front matter. They must not break the reader
    -- an audit tool that crashes on the data it exists to audit is useless."""
    root = _make_channel(tmp_path, {})
    (root / "rounds" / "2026-07-30-aramid-review-request.md").write_text(
        "# Round 12 - review request\n\nbody\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "legacy")

    listed = channel.list_rounds(root)
    assert len(listed) == 1
    assert listed[0].author is None
    assert listed[0].legacy is True


def test_a_round_without_a_title_field_is_named_by_its_heading_then_its_filename(
    tmp_path: Path,
) -> None:
    """Legacy rounds carry no `title:`, so the heading names them in every
    view, and a round with no heading falls back to its filename rather than to
    nothing. (aramid mutation finding 9d47b26d: `heading or stem` swapped to
    `heading and stem` survived the suite.)"""
    root = _make_channel(tmp_path, {})
    rounds = root / "rounds"
    (rounds / "2026-07-30-aramid-review-request.md").write_text(
        "# Round 12 - review request\n\nbody\n", encoding="utf-8"
    )
    (rounds / "2026-07-31-aramid-no-heading.md").write_text("just prose\n", encoding="utf-8")

    titles = {entry.path.name: entry.title for entry in channel.list_rounds(root)}

    assert titles["2026-07-30-aramid-review-request.md"] == "Round 12 - review request"
    assert titles["2026-07-31-aramid-no-heading.md"] == "2026-07-31-aramid-no-heading"


def test_a_missing_title_is_taken_from_the_body_never_the_front_matter(tmp_path: Path) -> None:
    """Front matter is metadata, not prose: a hand-written `# ...` line inside it
    must not become the round's title when the body has a heading of its own.
    (aramid mutation finding 9d47b26d: `body or text` swapped to `body and text`
    searched the whole file, front matter included, and survived the suite.)"""
    root = _make_channel(tmp_path, {})
    (root / "rounds" / "2026-08-02-aramid-agent-round-7-x.md").write_text(
        "---\nround: 7\n# hand-added note\n---\n\n# The real heading\n\nbody\n",
        encoding="utf-8",
    )

    assert channel.read_round(root, 7).title == "The real heading"


def test_git_output_is_decoded_as_utf8_regardless_of_the_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_git` ran `text=True` with no `encoding=`, so Python decoded git's stdout
    with the LOCALE codec -- cp1252 on this machine.

    The failure mode is nastier than an exception. subprocess reads the pipe on a
    worker thread; the `UnicodeDecodeError` is raised THERE, the thread dies, and
    `result.stdout` comes back `None`. git's own returncode is 0, so the
    `check` branch sees nothing wrong and `_git` returns `None` to a caller whose
    annotation promises `str`.

    `locale.getencoding` is forced so this discriminates on a UTF-8 machine too.
    Without it the test would pass everywhere the bug cannot occur, which is the
    definition of a check that isn't one.
    """
    import locale

    monkeypatch.setattr(locale, "getencoding", lambda: "cp1252")
    root = _make_channel(tmp_path)
    # U+00CF encodes to C3 8F, and cp1252 leaves 0x8F undefined -- the exact byte
    # and position the live crash reported.
    (root / "note.md").write_text("dash — and Ï\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "add note")

    text = channel._git(root, "show", "HEAD:note.md")

    assert text is not None, "the reader thread died and `_git` returned None"
    assert "Ï" in text


def test_the_report_survives_a_round_the_locale_codec_cannot_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observed on the LIVE channel, not constructed.

    `graphite channel report` died with `'NoneType' object has no attribute
    'startswith'` -- `build_report` -> `_verify` -> `_git(root, "show", ...)`
    on a round whose body holds a character cp1252 cannot represent, with the
    `None` from the decode failure landing in `parse_round` three frames later.

    The report is what makes the channel auditable, so this was not cosmetic:
    the audit surface was unusable for every agent.
    """
    import locale

    monkeypatch.setattr(locale, "getencoding", lambda: "cp1252")
    aramid = _repo(tmp_path, "aramid")
    root = _make_channel(tmp_path, {str(aramid): "aramid-agent"})
    # ASCII title, non-ASCII body: the commit subject stays decodable so the
    # POST succeeds, isolating the failure to the report's `git show` of the
    # round -- which is where it actually happened.
    channel.post_round(root, aramid, title="Interop", body="round trip: Ï\n")

    report = channel.build_report(root)

    assert report is not None


def test_git_does_not_hand_the_brokers_stdin_to_the_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The broker runs as a stdio MCP server, so its stdin IS the JSON-RPC pipe.

    Letting git inherit it puts a child process's hands on the protocol stream:
    it can block on it, and anything it reads is protocol the server never sees.
    A live wedge traced to exactly this shape -- `git add` blocked for 45 minutes
    at 0.03s CPU while holding the channel lock.
    """
    import graphite.channel as channel

    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> object:
        captured.update(kwargs)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(channel.subprocess, "run", fake_run)
    channel._git(tmp_path, "status")

    assert captured.get("stdin") is subprocess.DEVNULL


def test_git_refuses_rather_than_hanging_forever(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unbounded git call wedges every agent, not just the caller.

    `_git` runs inside the channel lock, so a stall there is a channel-wide
    outage that only a manual kill clears. A bounded failure is recoverable.
    """
    import graphite.channel as channel

    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> object:
        seen.update(kwargs)
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, (int, float)) and timeout > 0, "no timeout passed"
        raise subprocess.TimeoutExpired(argv, float(timeout))

    monkeypatch.setattr(channel.subprocess, "run", fake_run)

    with pytest.raises(channel.ChannelError) as excinfo:
        channel._git(tmp_path, "add", "--", "status/045/0001-delivered.json")

    assert excinfo.value.code == "git_timeout"


# --- which git the broker launches (channel round 304) ------------------------


def test_channel_git_is_launched_by_an_absolute_path_outside_both_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_repo = tmp_path / "agent repo"
    channel_root = tmp_path / "channel"
    trusted = tmp_path / "trusted bin"
    for directory in (agent_repo, channel_root, trusted):
        directory.mkdir()
    name = "git.exe" if os.name == "nt" else "git"
    real = trusted / name
    for candidate in (real, agent_repo / name, channel_root / name):
        candidate.write_text("", encoding="utf-8")
        candidate.chmod(0o755)
    monkeypatch.chdir(agent_repo)
    monkeypatch.setenv(
        "PATH", os.pathsep.join(("", ".", str(agent_repo), str(channel_root), str(trusted)))
    )
    seen: list[list[str]] = []

    def record(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(channel.subprocess, "run", record)

    channel._git(channel_root, "status")

    assert [Path(argv[0]) for argv in seen] == [real.resolve()]


def test_channel_git_missing_outside_the_roots_is_a_channel_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel_root = tmp_path / "channel"
    channel_root.mkdir()
    (channel_root / ("git.exe" if os.name == "nt" else "git")).write_text("", encoding="utf-8")
    monkeypatch.chdir(channel_root)
    monkeypatch.setenv("PATH", str(channel_root))

    with pytest.raises(channel.ChannelError) as caught:
        channel._git(channel_root, "status")

    assert caught.value.code == "git_unavailable"


def _hook_function(name: str) -> Any:
    """One function from the commit-msg hook's embedded Python, compiled alone.

    The hook runs under `-I` and cannot import graphite, so its git lookup is
    its own copy and has to be tested where it lives.
    """
    text = channel.COMMIT_MSG_HOOK
    opener = "<<'PYEOF'\n"
    start = text.index(opener) + len(opener)
    program = ast.parse(text[start : text.index("\nPYEOF\n", start)])
    function = next(
        node for node in program.body if isinstance(node, ast.FunctionDef) and node.name == name
    )
    classes = [node for node in program.body if isinstance(node, ast.ClassDef)]
    namespace: dict[str, Any] = {"os": os}
    exec(compile(ast.Module([*classes, function], []), "<commit-msg hook>", "exec"), namespace)  # noqa: S102
    return namespace[name]


def _hook_program() -> str:
    text = channel.COMMIT_MSG_HOOK
    opener = "<<'PYEOF'\n"
    start = text.index(opener) + len(opener)
    return text[start : text.index("\nPYEOF\n", start) + 1]


def test_commit_msg_hook_runs_git_by_absolute_path_outside_the_channel_and_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # git starts the hook in the channel's root; a bare `git` there was looked
    # up in that directory before PATH on Windows.
    channel_root = tmp_path / "channel"
    trusted = tmp_path / "trusted bin"
    channel_root.mkdir()
    trusted.mkdir()
    name = "git.exe" if os.name == "nt" else "git"
    for candidate in (channel_root / name, trusted / name):
        candidate.write_text("", encoding="utf-8")
        candidate.chmod(0o755)
    monkeypatch.chdir(channel_root)
    monkeypatch.setenv("PATH", os.pathsep.join(("", ".", str(channel_root), str(trusted))))

    found = _hook_function("_git_executable")(str(channel_root))

    assert Path(found) == (trusted / name).resolve()


def test_commit_msg_hook_skips_a_symlink_in_a_path_entry_inside_the_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # aramid llm-review 1d8d8cf, in the hook's inlined copy: a PATH directory
    # inside the channel is skipped whole, not judged by where a symlink in it
    # points.
    channel_root, elsewhere = tmp_path / "channel", tmp_path / "elsewhere"
    name = "git.exe" if os.name == "nt" else "git"
    outside = tmp_path / "outside" / ("other" + Path(name).suffix)
    trusted = tmp_path / "trusted bin" / name
    for path in (outside, trusted):
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
        path.chmod(0o755)
    (channel_root / "bin").mkdir(parents=True)
    try:
        (channel_root / "bin" / name).symlink_to(outside)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - privilege-dependent
        pytest.skip(f"symlinks unavailable on this machine: {exc}")
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("PATH", os.pathsep.join((str(channel_root / "bin"), str(trusted.parent))))
    find = _hook_function("_git_executable")

    assert Path(find(str(elsewhere))) == outside.resolve(), (
        "control: with the channel not refused the symlink is taken"
    )
    assert Path(find(str(channel_root))) == trusted.resolve()


@pytest.mark.parametrize("shape", ["entry links into the channel", "entry inside links out"])
def test_commit_msg_hook_judges_a_path_entry_both_as_written_and_as_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    # Both spellings of the directory, as in `graphite.programs`: an entry
    # outside the channel that links into it, and an entry written inside the
    # channel that links out to a directory of the repo's choosing.
    channel_root, elsewhere = tmp_path / "channel", tmp_path / "elsewhere"
    name = "git.exe" if os.name == "nt" else "git"
    chosen = tmp_path / "outside dir" / name
    trusted = tmp_path / "trusted bin" / name
    for path in (chosen, trusted):
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
        path.chmod(0o755)
    channel_root.mkdir()
    try:
        if shape == "entry links into the channel":
            (channel_root / "bin").mkdir()
            (channel_root / "bin" / name).symlink_to(chosen)
            entry = tmp_path / "linked bin"
            entry.symlink_to(channel_root / "bin", target_is_directory=True)
        else:
            entry = channel_root / "bin"
            entry.symlink_to(chosen.parent, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - privilege-dependent
        pytest.skip(f"symlinks unavailable on this machine: {exc}")
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("PATH", os.pathsep.join((str(entry), str(trusted.parent))))
    find = _hook_function("_git_executable")

    assert Path(find(str(elsewhere))) == chosen.resolve(), "control: unrefused, the entry is taken"
    assert Path(find(str(channel_root))) == trusted.resolve()

def test_commit_msg_hook_does_not_refuse_tools_beneath_its_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same rule as `graphite.programs`: the cwd itself is refused, not
    # everything beneath it, or a cwd above git's install refuses git.
    channel_root = tmp_path / "channel"
    channel_root.mkdir()
    name = "git.exe" if os.name == "nt" else "git"
    trusted = tmp_path / "Program Files" / "Git" / "cmd" / name
    trusted.parent.mkdir(parents=True)
    trusted.write_text("", encoding="utf-8")
    trusted.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(trusted.parent))

    assert Path(_hook_function("_git_executable")(str(channel_root))) == trusted.resolve()

def test_commit_msg_hook_with_no_git_outside_the_channel_raises_so_the_gate_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Returning nothing would let `_registry_at` read the registry as ABSENT,
    # which is the bootstrap branch: the working tree would be trusted. The
    # raise lands in the hook's own exit code instead (see the next test).
    channel_root = tmp_path / "channel"
    channel_root.mkdir()
    (channel_root / ("git.exe" if os.name == "nt" else "git")).write_text("", encoding="utf-8")
    monkeypatch.chdir(channel_root)
    monkeypatch.setenv("PATH", str(channel_root))
    find = _hook_function("_git_executable")

    with pytest.raises(find.__globals__["GitUnavailable"]):
        find(str(channel_root))

    registry_at = channel.COMMIT_MSG_HOOK[channel.COMMIT_MSG_HOOK.index("def _registry_at") :]
    assert "[_git_executable(root)," in registry_at.split("def _authorises_anyone")[0]


def test_commit_msg_hook_with_no_git_exits_with_its_own_status(tmp_path: Path) -> None:
    # Status 1 is "this commit names no agent". A missing git used to land
    # there too, through the hook's catch-all, and send the committer to fix a
    # trailer that was already correct. The shell maps 4 to its own banner.
    channel_root = tmp_path / "channel"
    channel_root.mkdir()
    (channel_root / ("git.exe" if os.name == "nt" else "git")).write_text("", encoding="utf-8")
    message = tmp_path / "MSG"
    message.write_text("subject\n", encoding="utf-8")

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-", str(message), str(channel_root)],
        input=_hook_program(),
        cwd=channel_root,
        env={**os.environ, "PATH": str(channel_root)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 4, result.stderr
