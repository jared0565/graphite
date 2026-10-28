"""A ledger entry carries the session that made it, so two sessions open in
one repository do not count each other's use (#110).

The usage ledger is per repository and every session appends to it. An entry
said nothing about who made it, so where two sessions were open at once each
one's Stop counted the other's queries as its own: the second session's Stop
saw the first's query, because that query was written after the second
session's books were opened, and the first session's next Stop saw the
second's.

The tag is what the process can know: `GRAPHITE_SESSION_ID` first, then the
harness's own variable. An entry no session can be attributed to is counted
as it was, so that no real use is dropped -- a person running graphite in a
terminal in the same repository is real use too.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite import usage_ledger
from graphite.agent_hooks import handle_session_start, handle_stop
from graphite.savings import estimate_entry
from graphite.usage_ledger import iter_entries, read_cursor, record_usage

SESSION_VARS = ("GRAPHITE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_COMPANION_SESSION_ID")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "alpha.py").write_text("def alpha():\n    return 1\n" * 40, encoding="utf-8")
    return root


def _event(root: Path, event: str, session: str) -> dict:
    return {"session_id": session, "cwd": str(root), "hook_event_name": event}


def _start(root: Path, session: str) -> None:
    handle_session_start(_event(root, "SessionStart", session))


def _stop(root: Path, session: str) -> dict | None:
    return handle_stop(_event(root, "Stop", session))


def _use(root: Path, monkeypatch: pytest.MonkeyPatch, session: str | None = None) -> None:
    """Record one use, as the named session (untagged when none is named)."""
    if session is not None:
        monkeypatch.setenv("GRAPHITE_SESSION_ID", session)
    record_usage(root, cmd="context", wall_ms=50, result={"files": [{"path": "alpha.py"}]})


def _one_entry_tokens(root: Path) -> int:
    """Every `_use` writes the same entry, so one entry's estimate is the unit."""
    estimates = {estimate_entry(entry)["tokens_saved"] for entry in iter_entries(root)}
    assert len(estimates) == 1 and estimates != {0}
    return estimates.pop()


def _session_tokens(root: Path, session: str) -> int:
    return read_cursor(root)["sessions"][session]["tokens"]


def test_a_recorded_entry_carries_the_session_that_made_it(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAPHITE_SESSION_ID", "A")

    _use(repo, monkeypatch, "A")

    assert [entry.get("session") for entry in iter_entries(repo)] == ["A"]


def test_an_entry_no_session_can_name_carries_no_tag(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use(repo, monkeypatch)

    assert "session" not in list(iter_entries(repo))[0]


def test_two_sessions_open_at_once_each_count_only_their_own_use(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reproduction from the issue, in the order it gives it."""
    _start(repo, "A")
    _start(repo, "B")
    _use(repo, monkeypatch, "A")
    assert _stop(repo, "A") is not None
    _use(repo, monkeypatch, "B")
    assert _stop(repo, "B") is not None

    unit = _one_entry_tokens(repo)
    assert _session_tokens(repo, "A") == unit
    assert _session_tokens(repo, "B") == unit

    # A has run nothing since its last Stop, so it has nothing to report.
    assert _stop(repo, "A") is None
    assert _session_tokens(repo, "A") == unit


def test_a_session_total_holds_only_its_own_use(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _start(repo, "A")
    _start(repo, "B")
    _use(repo, monkeypatch, "A")
    _use(repo, monkeypatch, "A")
    _use(repo, monkeypatch, "B")
    _stop(repo, "A")
    _stop(repo, "B")

    unit = _one_entry_tokens(repo)
    assert _session_tokens(repo, "A") == 2 * unit
    assert _session_tokens(repo, "B") == unit


def test_a_session_that_used_nothing_stays_silent_while_another_ran(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _start(repo, "A")
    _use(repo, monkeypatch, "B")

    assert _stop(repo, "A") is None


def test_an_entry_no_session_can_name_is_counted_for_every_session(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A person running graphite in a terminal in the same repository is real
    use, and dropping it would understate what the graph saved."""
    _start(repo, "A")
    _start(repo, "B")
    _use(repo, monkeypatch)
    assert _stop(repo, "A") is not None
    assert _stop(repo, "B") is not None

    unit = _one_entry_tokens(repo)
    assert _session_tokens(repo, "A") == unit
    assert _session_tokens(repo, "B") == unit


def test_the_session_id_comes_from_graphite_session_id_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAPHITE_SESSION_ID", "g")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "c")
    monkeypatch.setenv("CODEX_COMPANION_SESSION_ID", "x")

    assert usage_ledger.current_session_id() == "g"


def test_the_session_id_falls_back_to_the_harness_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "c")
    monkeypatch.setenv("CODEX_COMPANION_SESSION_ID", "x")

    assert usage_ledger.current_session_id() == "c"


def test_the_session_id_falls_back_to_the_companion_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_COMPANION_SESSION_ID", "x")

    assert usage_ledger.current_session_id() == "x"


def test_no_session_id_is_reported_when_the_environment_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    assert usage_ledger.current_session_id() == ""


def test_a_command_run_by_a_session_is_tagged_with_that_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """End to end: the command records the entry, and the entry knows who ran it."""
    from graphite.cli import main

    (tmp_path / "alpha.py").write_text("def target_symbol():\n    return 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["build", "."]) == 0
    monkeypatch.setenv("GRAPHITE_SESSION_ID", "A")

    assert main(["search", "target_symbol"]) == 0
    capsys.readouterr()

    assert [entry.get("session") for entry in iter_entries(tmp_path)] == ["A"]


def _write_entry_with_tag(root: Path, tag: str) -> None:
    """Write one ledger entry by hand, carrying a tag the recorder cannot.

    `record_usage` writes the session tag only when it is truthy, so an EMPTY
    tag can only reach the ledger from outside -- a hand-edited file, or one
    half-written by something else. That is exactly the case
    `_entry_is_this_session` is defensive about, so the fixture writes the
    entry in the recorder's own shape.
    """
    entry = {
        "ts": "2026-01-01T00:00:00+00:00",
        "cmd": "context",
        "wall_ms": 50,
        "output_bytes": 30,
        "files": [{"path": "alpha.py", "bytes": (root / "alpha.py").stat().st_size}],
        "session": tag,
    }
    path = usage_ledger.ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def test_an_entry_whose_session_tag_is_empty_counts_for_every_session(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An EMPTY tag names no session, exactly as no tag at all does.

    `_entry_is_this_session` accepts an entry that names no session, and the
    condition that does it is `not isinstance(tag, str) or not tag`. Read as
    `and`, an empty tag would fall through to `tag == session_id` and be
    counted for NOBODY -- so a person running graphite in a terminal, whose
    tag was written empty, would have their real use dropped. This pins the
    `or`.
    """
    _start(repo, "A")
    _start(repo, "B")
    _write_entry_with_tag(repo, "")

    assert _stop(repo, "A") is not None
    assert _stop(repo, "B") is not None

    unit = _one_entry_tokens(repo)
    assert _session_tokens(repo, "A") == unit
    assert _session_tokens(repo, "B") == unit


def test_a_stop_with_no_ledger_file_leaves_the_cursor_at_offset_zero(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No ledger means no bytes to read, so the cursor starts at zero.

    `handle_stop` takes the ledger's byte length as `size`, and zero when
    there is no ledger. That number is the cursor's offset, and the offset is
    where the NEXT Stop reads from -- so a one-byte offset would mean
    starting one byte into a ledger that did not exist yet, and the first
    entry in it would be read from the wrong place. This pins the zero.
    """
    assert not usage_ledger.ledger_path(repo).exists()

    assert _stop(repo, "A") is None

    assert read_cursor(repo)["sessions"]["A"]["offset"] == 0


def test_a_cursor_state_without_an_offset_reads_the_ledger_from_its_start(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state that lost its offset is a damaged cursor, and a damaged
    cursor reads the ledger from the beginning.

    The ledger is newline-delimited JSON, so the offset decides where a
    line starts. An offset of 1 would cut the FIRST entry in half: it no
    longer parses, and the turn's own use would go uncounted without a
    word. The default is 0 because that is what this function already does
    with a damaged cursor elsewhere -- an offset past the end of the ledger
    resyncs to 0.
    """
    _start(repo, "A")
    _use(repo, monkeypatch, "A")

    # Damage the cursor: the session's state keeps no offset, and no inode
    # either, so the rotation check cannot fire and the offset alone decides.
    cursor = read_cursor(repo)
    cursor["sessions"]["A"].pop("offset", None)
    cursor["sessions"]["A"].pop("ino", None)
    usage_ledger.write_cursor(repo, cursor)

    summary = _stop(repo, "A")

    unit = _one_entry_tokens(repo)
    assert summary is not None
    assert _session_tokens(repo, "A") == unit
