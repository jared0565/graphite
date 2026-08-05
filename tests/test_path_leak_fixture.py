"""The `assert_json_omits` fixture, and the vacuity it exists to remove.

`needle not in json.dumps(payload)` was used in five places to prove a path did
not leak. On Windows that check cannot fail when the needle is a path:
`json.dumps` doubles every backslash, so the raw path never matches its own
serialised form. Five guards that guarded nothing, on the only platform CI ran.

The portability matrix surfaced it the first time it executed a POSIX leg, where
paths have no backslashes to escape and the same assertion suddenly bit.
"""
from __future__ import annotations

import json

import pytest

WINDOWS_PATH = r"C:\Users\someone\private\.graphite-daemon\status.json"
POSIX_PATH = "/home/someone/private/.graphite-daemon/status.json"


def test_the_naive_form_is_vacuous_for_a_windows_path() -> None:
    """The bug itself, pinned. If this ever fails, `json.dumps` stopped escaping
    backslashes and the fixture's reason for existing is gone."""
    payload = {"status_path": WINDOWS_PATH}

    # The path is RIGHT THERE, and the naive assertion sails through.
    assert WINDOWS_PATH in payload["status_path"]
    assert WINDOWS_PATH not in json.dumps(payload)


def test_the_fixture_catches_the_windows_path_the_naive_form_misses(assert_json_omits) -> None:
    with pytest.raises(AssertionError):
        assert_json_omits(WINDOWS_PATH, {"status_path": WINDOWS_PATH})


def test_the_fixture_still_catches_a_posix_path(assert_json_omits) -> None:
    """The naive form already worked here -- the fixture must not regress it."""
    with pytest.raises(AssertionError):
        assert_json_omits(POSIX_PATH, {"status_path": POSIX_PATH})


def test_the_fixture_accepts_a_payload_that_genuinely_omits_the_needle(assert_json_omits) -> None:
    assert_json_omits(WINDOWS_PATH, {"status_path": "<redacted>", "name": "repo"})
    assert_json_omits(POSIX_PATH, {"status_path": "<redacted>", "name": "repo"})


def test_the_fixture_accepts_a_path_object_not_only_a_string(assert_json_omits, tmp_path) -> None:
    """Callers pass `Path` objects; stringifying is the fixture's job, not the
    caller's -- that detail is exactly what the old form got wrong."""
    with pytest.raises(AssertionError):
        assert_json_omits(tmp_path, {"where": str(tmp_path)})
    assert_json_omits(tmp_path, {"where": "<redacted>"})


def test_the_fixture_accepts_a_prepared_json_string(assert_json_omits) -> None:
    with pytest.raises(AssertionError):
        assert_json_omits(WINDOWS_PATH, json.dumps({"status_path": WINDOWS_PATH}))
