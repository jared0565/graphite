"""Tests for `graphite.process_contracts`, the pure launch-contract builders.

Named after the module so aramid's mutation stage 1 (`tests/test_<module>.py`)
has something to run: before this file, `-k process_contracts` selected no
test, so every mutant here was booked a stage-1 survivor and sent to a
full-suite confirm.
"""
from __future__ import annotations

import threading

import pytest

from graphite import process_contracts
from graphite.process_contracts import (
    WINDOWS_PROCESS_CREATION_LOCK,
    build_windows_environment_block,
)


def test_environment_block_is_sorted_case_insensitively_and_double_nul_terminated() -> None:
    block = build_windows_environment_block({"b": "2", "A": "1", "c": "3"})
    assert block == "A=1\0b=2\0c=3\0\0"


def test_environment_block_of_no_variables_is_just_the_terminator() -> None:
    assert build_windows_environment_block({}) == "\0\0"


def test_environment_block_keeps_equals_signs_inside_values() -> None:
    assert build_windows_environment_block({"K": "a=b"}) == "K=a=b\0\0"


@pytest.mark.parametrize(
    "environment",
    [
        {"": "value"},
        {"KEY=BAD": "value"},
        {"KEY\0BAD": "value"},
        {"KEY": "bad\0value"},
    ],
    ids=["empty-key", "equals-in-key", "nul-in-key", "nul-in-value"],
)
def test_environment_block_rejects_keys_and_values_a_block_cannot_encode(
    environment: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        build_windows_environment_block(environment)


def test_creation_lock_is_reentrant_for_the_holding_thread() -> None:
    # Launchers nest: a repository-owned launcher that enables inheritance
    # may call into another that takes the same lock. A plain Lock would
    # deadlock the second acquisition.
    assert WINDOWS_PROCESS_CREATION_LOCK is process_contracts.WINDOWS_PROCESS_CREATION_LOCK
    with WINDOWS_PROCESS_CREATION_LOCK:
        assert WINDOWS_PROCESS_CREATION_LOCK.acquire(blocking=False)
        WINDOWS_PROCESS_CREATION_LOCK.release()


def test_creation_lock_excludes_other_threads_while_held() -> None:
    acquired_elsewhere: list[bool] = []

    def try_from_another_thread() -> None:
        got = WINDOWS_PROCESS_CREATION_LOCK.acquire(blocking=False)
        acquired_elsewhere.append(got)
        if got:
            WINDOWS_PROCESS_CREATION_LOCK.release()

    with WINDOWS_PROCESS_CREATION_LOCK:
        worker = threading.Thread(target=try_from_another_thread)
        worker.start()
        worker.join(timeout=10)
    assert acquired_elsewhere == [False]
