"""Core of the agent-channel broker: identity, allocation, and create-only posts.

The security property under test throughout is that an agent cannot name itself.
All three agents commit under the operator's git identity, so the trailer is the
only answer to "who" -- an author that a caller could supply would make the whole
channel unauditable. Identity is derived from the repo the broker runs in, and an
unregistered repo is refused rather than defaulted.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

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
