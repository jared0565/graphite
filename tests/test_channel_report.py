"""The audit report must grade every row rather than render them all alike.

An agent that still has filesystem access can write a round by hand and bypass
the broker entirely. The report's job is to make that visible. A report that
showed a hand-written round identically to a brokered one would launder
uncertainty into apparent fact, which is worse than having no report.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def _row(report: dict, number: int) -> dict:
    return next(r for r in report["rounds"] if r["round"] == number)


# --- verification grading ---------------------------------------------------


def test_a_brokered_round_grades_verified(tmp_path: Path) -> None:
    root, _, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b")["round"]

    assert _row(channel.build_report(root), n)["verification"] == "verified"


def test_a_legacy_round_grades_legacy_not_verified(tmp_path: Path) -> None:
    """The 37 relocated rounds all read as graphite's in this repo's history.
    Rendering that as fact would be a lie."""
    root, _, graphite = _setup(tmp_path)
    (root / "rounds" / "2026-07-30-aramid-round-12-review.md").write_text("# old\n", encoding="utf-8")
    _commit_as(root, "graphite-agent", "relocate")

    assert _row(channel.build_report(root), 12)["verification"] == "legacy"


def test_a_round_edited_after_creation_grades_modified(tmp_path: Path) -> None:
    root, _, graphite = _setup(tmp_path)
    posted = channel.post_round(root, graphite, title="T", body="b")
    Path(posted["path"]).write_text("tampered\n", encoding="utf-8")
    _commit_as(root, "graphite-agent", "edit")

    assert _row(channel.build_report(root), posted["round"])["verification"] == "modified"


def test_an_edit_that_is_never_committed_still_grades_modified(tmp_path: Path) -> None:
    """The quieter bypass: edit a tracked round and just don't commit. The file
    is still tracked and still has exactly one commit, so every other signal
    reads clean."""
    root, _, graphite = _setup(tmp_path)
    posted = channel.post_round(root, graphite, title="T", body="b")
    Path(posted["path"]).write_text("tampered in the working tree\n", encoding="utf-8")

    assert _row(channel.build_report(root), posted["round"])["verification"] == "modified"


def test_erasing_the_front_matter_does_not_launder_a_round_into_legacy(tmp_path: Path) -> None:
    """`legacy` is the one degraded grade that does not fail the check, so it is
    the grade an attacker wants. Overwriting a brokered round removes its stamped
    author -- verification must read the ORIGINAL commit, not the current text."""
    root, _, graphite = _setup(tmp_path)
    posted = channel.post_round(root, graphite, title="T", body="b")
    Path(posted["path"]).write_text("no front matter any more\n", encoding="utf-8")
    _commit_as(root, "graphite-agent", "erase")

    report = channel.build_report(root)
    assert _row(report, posted["round"])["verification"] == "modified"
    assert report["ok"] is False


def test_an_untracked_round_grades_uncommitted(tmp_path: Path) -> None:
    root, _, _ = _setup(tmp_path)
    (root / "rounds" / "2026-08-01-graphite-round-7-sneaky.md").write_text(
        channel.render_round(
            {"round": 7, "author": "graphite-agent", "posted": "2026-08-01T00:00:00Z", "title": "S"},
            "body",
        ),
        encoding="utf-8",
    )

    assert _row(channel.build_report(root), 7)["verification"] == "uncommitted"


def test_an_author_that_disagrees_with_the_trailer_grades_discrepancy(tmp_path: Path) -> None:
    """The forgery signature: front matter claims one agent, the commit says
    another."""
    root, _, _ = _setup(tmp_path)
    (root / "rounds" / "2026-08-01-aramid-round-8-forged.md").write_text(
        channel.render_round(
            {"round": 8, "author": "aramid-agent", "posted": "2026-08-01T00:00:00Z", "title": "F"},
            "body",
        ),
        encoding="utf-8",
    )
    _commit_as(root, "graphite-agent", "forged")

    assert _row(channel.build_report(root), 8)["verification"] == "discrepancy"


# --- status, staleness, anomalies -------------------------------------------


def test_the_row_carries_current_status_with_who_set_it(tmp_path: Path) -> None:
    root, aramid, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]
    channel.inbox(root, aramid)
    channel.record_status(root, aramid, n, "blocked", reason="need data")

    row = _row(channel.build_report(root), n)
    assert row["status"] == "blocked"
    assert row["status_actor"] == "aramid-agent"
    assert row["reason"] == "need data"


def test_delivered_and_unanswered_past_the_threshold_is_stalled(tmp_path: Path) -> None:
    root, aramid, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]
    channel.inbox(root, aramid)

    later = datetime.now(timezone.utc) + timedelta(days=4)
    report = channel.build_report(root, stale_days=3, now=later)
    assert _row(report, n)["stalled"] is True
    assert n in [item["round"] for item in report["stalled"]]


def test_it_is_not_stalled_before_the_threshold(tmp_path: Path) -> None:
    root, aramid, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]
    channel.inbox(root, aramid)

    later = datetime.now(timezone.utc) + timedelta(days=2)
    assert _row(channel.build_report(root, stale_days=3, now=later), n)["stalled"] is False


def test_done_without_delivered_is_reported_as_an_anomaly(tmp_path: Path) -> None:
    root, aramid, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b", to=["aramid-agent"])["round"]
    channel.record_status(root, aramid, n, "done")

    report = channel.build_report(root)
    assert any(a["round"] == n and a["kind"] == "done_without_delivery" for a in report["anomalies"])


def test_supersedes_pointing_at_a_missing_round_is_an_anomaly(tmp_path: Path) -> None:
    root, _, graphite = _setup(tmp_path)
    n = channel.post_round(root, graphite, title="T", body="b", supersedes=999)["round"]

    report = channel.build_report(root)
    assert any(a["round"] == n and a["kind"] == "supersedes_missing" for a in report["anomalies"])


def test_a_clean_channel_reports_ok_and_a_dirty_one_does_not(tmp_path: Path) -> None:
    """Exit code carries the verdict so the report can run as a check, not just
    be read."""
    root, _, graphite = _setup(tmp_path)
    channel.post_round(root, graphite, title="T", body="b")
    assert channel.build_report(root)["ok"] is True

    (root / "rounds" / "2026-08-01-graphite-round-9-loose.md").write_text("x\n", encoding="utf-8")
    assert channel.build_report(root)["ok"] is False


def test_legacy_rounds_get_no_invented_lifecycle(tmp_path: Path) -> None:
    root, _, _ = _setup(tmp_path)
    (root / "rounds" / "2026-07-30-aramid-round-12-review.md").write_text("# old\n", encoding="utf-8")
    _commit_as(root, "graphite-agent", "relocate")

    row = _row(channel.build_report(root), 12)
    assert row["status"] is None
    assert row["stalled"] is False


def test_the_human_rendering_names_every_degraded_row(tmp_path: Path) -> None:
    """If a row is anything other than `verified`, a human reading the default
    output must see it without passing --json."""
    root, _, graphite = _setup(tmp_path)
    channel.post_round(root, graphite, title="Fine", body="b")
    (root / "rounds" / "2026-08-01-graphite-round-9-loose.md").write_text("x\n", encoding="utf-8")

    text = channel.render_report(channel.build_report(root))
    assert "UNCOMMITTED" in text.upper()
    assert "round 9" in text
