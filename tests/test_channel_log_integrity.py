"""The status log must show a DELETED event, and the audit gate must fail closed.

Two open findings on the channel's audit trail, confirmed against the code on
2026-09-25 before the 1.1.0 release:

- 5db5889: the report graded only the events still on disk, and a new event's
  number was `len(files) + 1`. An agent could delete its own `blocked`, commit
  the deletion, write a `done` into the same slot, and the report read OK.
- 0e87805: `ensure_channel_hook` set `core.hooksPath` with `check=False` and
  reported `changed: True` whatever happened, so a commit-msg gate that git
  never armed was reported as armed.
"""
from __future__ import annotations

import json
import os
import stat
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


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / ".agent-channel"
    (root / "rounds").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(root))
    _git(root, "config", "user.email", "operator@example.com")
    _git(root, "config", "user.name", "Operator")
    (root / "PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")
    aramid, graphite = tmp_path / "aramid", tmp_path / "graphite"
    aramid.mkdir()
    graphite.mkdir()
    channel.write_registry(root, {str(aramid): "aramid-agent", str(graphite): "graphite-agent"})
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root, aramid, graphite


def _commit_as(root: Path, agent: str, subject: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"{subject}\n\n{channel.trailer(agent)}\n")


def _blocked_round(tmp_path: Path) -> tuple[Path, Path, int, Path]:
    """delivered (seq 1), then blocked (seq 2): the event an agent would erase."""
    root, aramid, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]
    channel.inbox(root, aramid)
    channel.record_status(root, aramid, n, "blocked", reason="need data")
    blocked = root / "status" / f"{n:03d}" / "0002-blocked.json"
    assert blocked.exists()
    return root, aramid, n, blocked


def _kinds(report: dict, number: int) -> set[str]:
    return {a["kind"] for a in report["anomalies"] if a["round"] == number}


# --- 5db5889: deletion is as visible as tampering -----------------------------


def test_a_deleted_event_is_reported_after_the_deletion_is_committed(tmp_path: Path) -> None:
    root, _aramid, n, blocked = _blocked_round(tmp_path)
    blocked.unlink()
    _commit_as(root, "aramid-agent", "drop the blocked event")

    report = channel.build_report(root)

    assert "status_deleted" in _kinds(report, n)
    assert report["ok"] is False


def test_a_deleted_event_is_reported_before_the_deletion_is_committed(tmp_path: Path) -> None:
    root, _aramid, n, blocked = _blocked_round(tmp_path)
    blocked.unlink()

    assert "status_deleted" in _kinds(channel.build_report(root), n)


def test_deleting_the_last_event_does_not_free_its_number(tmp_path: Path) -> None:
    """The laundering move: erase `blocked`, then write `done` into its slot."""
    root, aramid, n, blocked = _blocked_round(tmp_path)
    blocked.unlink()
    _commit_as(root, "aramid-agent", "drop the blocked event")

    event = channel.record_status(root, aramid, n, "done")

    assert event["seq"] == 3
    assert not (root / "status" / f"{n:03d}" / "0002-done.json").exists()


def test_a_history_git_cannot_read_refuses_to_number_an_event(tmp_path: Path) -> None:
    """The next number reads history, and a FAILED read must not pass for an
    empty one. Only history knows seq 2 here -- its file is deleted and the
    deletion committed -- and a missing tree object makes `git log` exit 128
    with no output. Taking that as "no history" hands 2 out again."""
    root, _aramid, n, blocked = _blocked_round(tmp_path)
    blocked.unlink()
    _commit_as(root, "aramid-agent", "drop the blocked event")
    tree = _git(root, "rev-parse", "HEAD:status").strip()
    obj = root / ".git" / "objects" / tree[:2] / tree[2:]
    os.chmod(obj, stat.S_IWRITE)  # git writes objects read-only; Windows refuses the unlink
    obj.unlink()

    with pytest.raises(channel.ChannelError):
        channel._next_seq(root, n)


def test_a_channel_with_no_commits_is_an_empty_history(tmp_path: Path) -> None:
    """git exits 128 on `log` before a repository's first commit. The reads
    that grade events against history must take that as "nothing yet" --
    1.0.1's `channel list` listed an empty channel -- not as a failure."""
    root = tmp_path / ".agent-channel"
    (root / "rounds").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(root))
    (root / "PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")

    report = channel.build_report(root)

    assert report["rounds"] == [] and report["ok"] is True
    assert channel.EventGrader.load(root).commits == {}
    assert channel._next_seq(root, 1) == 1


def test_a_gap_in_the_sequence_is_an_anomaly(tmp_path: Path) -> None:
    """A hand-written event that skips a number, committed under the right
    trailer: every per-file check passes, only the sequence can see it."""
    root, aramid, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]
    channel.inbox(root, aramid)
    event = {"round": n, "seq": 3, "status": "done", "actor": "aramid-agent",
             "broker": False, "at": "2026-09-25T00:00:00Z", "reason": None}
    (root / "status" / f"{n:03d}" / "0003-done.json").write_text(json.dumps(event), encoding="utf-8")
    _commit_as(root, "aramid-agent", f"round {n}: done")

    report = channel.build_report(root)

    assert "status_seq_gap" in _kinds(report, n)
    assert report["ok"] is False


def test_a_deleted_round_is_reported(tmp_path: Path) -> None:
    """The report iterated only the rounds on disk, so a round removed with its
    deletion committed vanished from the audit view entirely."""
    root, _aramid, graphite = _setup(tmp_path)
    posted = channel.post_round(root, graphite, title="T", body="b")
    Path(posted["path"]).unlink()
    _commit_as(root, "graphite-agent", "drop a round")

    report = channel.build_report(root)

    assert any(
        a["kind"] == "round_deleted" and a["round"] == posted["round"] for a in report["anomalies"]
    )
    assert report["ok"] is False


def test_an_intact_log_raises_no_deletion_or_gap(tmp_path: Path) -> None:
    root, _aramid, n, _blocked = _blocked_round(tmp_path)

    report = channel.build_report(root)

    assert not _kinds(report, n) & {"status_deleted", "status_seq_gap"}
    assert report["ok"] is True


# --- 0e87805: the audit gate fails closed ------------------------------------


def test_hook_installation_fails_closed_when_git_cannot_arm_it(tmp_path: Path) -> None:
    """A locked `.git/config` makes `git config` fail. The gate is then NOT
    armed, and saying otherwise is the fail-open the finding describes."""
    root, _aramid, _graphite = _setup(tmp_path)
    (root / ".git" / "config.lock").write_text("", encoding="utf-8")

    with pytest.raises(channel.ChannelError):
        channel.ensure_channel_hook(root)


def test_hook_installation_fails_closed_when_an_override_outranks_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write succeeds, and git still runs another hooks path: command-line
    config outranks the repository's. Only reading back what git will USE
    catches this -- the exit status of the write cannot."""
    root, _aramid, _graphite = _setup(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "elsewhere")

    with pytest.raises(channel.ChannelError) as raised:
        channel.ensure_channel_hook(root)
    assert raised.value.code == "hook_not_armed"


def test_hook_installation_asks_for_exactly_0o755(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ARGUMENT, pinned on every platform. aramid's mutation drain found
    # `0o755 -> 0o756` (a world-writable audit hook on POSIX) surviving, and on
    # Windows `os.chmod` honours only the write bit, so no on-disk check there
    # can see it. The argument is what the mutant changes, so this kills it on
    # Windows too (channel round 276, the same shape as aramid's own
    # `tests/unit/test_doctor_probes.py`).
    root, _aramid, _graphite = _setup(tmp_path)
    hook = root / ".githooks" / "commit-msg"
    real_chmod = Path.chmod
    requested: list[int] = []

    def recording_chmod(self: Path, mode: int, *args: object, **kwargs: object) -> None:
        if self == hook:
            requested.append(mode)
        real_chmod(self, mode, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "chmod", recording_chmod)

    channel.ensure_channel_hook(root)

    assert requested == [0o755]


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows os.chmod honours only the write bit, so group/world bits are "
    "unobservable on disk there; the argument pin above covers Windows",
)
def test_installed_hook_is_executable_and_not_group_or_world_writable(tmp_path: Path) -> None:
    # The security property itself, on disk. Written as the property rather
    # than an exact mode: a deliberate tightening to 0o750 or 0o700 is not a
    # regression, and the exact value is already pinned by the argument test.
    root, _aramid, _graphite = _setup(tmp_path)

    channel.ensure_channel_hook(root)

    mode = stat.S_IMODE((root / ".githooks" / "commit-msg").stat().st_mode)
    assert mode & 0o022 == 0
    assert mode & 0o100


def test_hook_installation_reports_what_git_actually_has(tmp_path: Path) -> None:
    root, _aramid, _graphite = _setup(tmp_path)

    first = channel.ensure_channel_hook(root)
    second = channel.ensure_channel_hook(root)

    assert first["changed"] is True
    assert second["changed"] is False
    assert _git(root, "config", "--get", "core.hooksPath").strip() == ".githooks"
