"""Tests for `graphite.probe_process`, the bounded isolated process transport.

Named after the module so aramid's mutation stage 1 (`tests/test_<module>.py`)
has something to run; `-k probe_process` selected nothing before this file.
The transport's behaviour under real servers is covered by the probe suites;
this file pins its own contract -- the error record's coercions, the
environment gate, the argument gate, and the four outcomes of a short real
child (success, nonzero, output limit, timeout) -- fast enough for stage 1.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from graphite.probe_process import (
    INPUT_LIMIT_BYTES,
    OUTPUT_LIMIT_BYTES,
    ProbeProcessError,
    ProbeProcessResult,
    _observed_number,
    _validated_environment,
    run_bounded_process,
    sanitized_probe_environment,
)

_PYTHON = sys._base_executable or sys.executable


def _child(code: str) -> list[str]:
    return [_PYTHON, "-I", "-c", code]


# --- ProbeProcessError ------------------------------------------------------------------


def test_error_keeps_a_safe_code_and_folds_anything_else_to_unexpected() -> None:
    assert ProbeProcessError("timeout").code == "timeout"
    assert ProbeProcessError("C:/leaked/path").code == "unexpected"
    assert str(ProbeProcessError("nonzero")) == "probe process failed: nonzero"
    assert str(ProbeProcessError("input_failed")) == "probe input failed"


def test_error_carries_only_numeric_observations() -> None:
    error = ProbeProcessError(
        "timeout",
        os_error=32,
        elapsed_seconds=5.5,
        budget_seconds=5.0,
        stdout_bytes=0,
        stderr_bytes=True,  # a bool is not an observation
    )
    assert (error.os_error, error.elapsed_seconds, error.budget_seconds) == (32, 5.5, 5.0)
    assert error.stdout_bytes == 0
    assert error.stderr_bytes is None
    assert str(error) == "probe process failed: timeout (os=32)"
    assert ProbeProcessError("timeout", os_error=True).os_error is None
    assert ProbeProcessError("timeout", os_error="1").os_error is None  # type: ignore[arg-type]


def test_error_reports_a_failed_cleanup_beside_the_code_not_instead_of_it() -> None:
    error = ProbeProcessError("nonzero", cleanup_failed=True)
    assert (error.code, error.cleanup_failed) == ("nonzero", True)
    assert str(error) == "probe process failed: nonzero (cleanup also failed)"
    assert str(ProbeProcessError("cleanup_failed", cleanup_failed=True)) == "probe process failed: cleanup_failed"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (2.5, 2.5), (-1, None), (True, None), (float("nan"), None), (float("inf"), None), ("3", None), (None, None)],
)
def test_observed_number_admits_only_finite_non_negative_numbers(value: object, expected: float | None) -> None:
    assert _observed_number(value) == expected


def test_result_defaults_read_as_input_delivered_and_stdin_not_measured() -> None:
    result = ProbeProcessResult(0, b"", b"", 0.1)
    assert (result.input_bytes, result.input_complete, result.stdin_close_to_exit_seconds) == (0, True, -1.0)


# --- the environment --------------------------------------------------------------------------


def test_sanitized_environment_keeps_os_essentials_and_pins_the_package_path() -> None:
    source = {"PATH": "/bin", "HOME": "/home/x", "AWS_SECRET_ACCESS_KEY": "nope", "GRAPHITE_STATE_DIR": "x"}
    env = sanitized_probe_environment(source)
    assert env["PATH"] == "/bin" and env["HOME"] == "/home/x"
    assert "AWS_SECRET_ACCESS_KEY" not in env and "GRAPHITE_STATE_DIR" not in env
    assert Path(env["PYTHONPATH"]).name == "src"
    assert (env["PYTHONIOENCODING"], env["PYTHONUTF8"]) == ("utf-8", "1")


def test_validated_environment_takes_an_explicit_mapping_verbatim_including_empty() -> None:
    assert _validated_environment({}) == {}
    assert _validated_environment({"A": "1"}) == {"A": "1"}
    assert "PYTHONPATH" in _validated_environment(None)


@pytest.mark.parametrize(
    "environment",
    [{"": "v"}, {"A=B": "v"}, {"A\0": "v"}, {"A": "v\0"}, {"A": 1}, {1: "v"}],
    ids=["empty-key", "equals-in-key", "nul-in-key", "nul-in-value", "non-string-value", "non-string-key"],
)
def test_validated_environment_rejects_what_a_child_cannot_receive(environment: dict[object, object]) -> None:
    with pytest.raises(ProbeProcessError) as info:
        _validated_environment(environment)  # type: ignore[arg-type]
    assert info.value.code == "invalid_environment"


# --- argument gate ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": 1, "max_output_bytes": 0},
        {"timeout_seconds": 1, "max_output_bytes": True},
        {"timeout_seconds": 1, "max_input_bytes": 4 * 1024 * 1024 + 1},
    ],
    ids=["zero-timeout", "infinite-timeout", "zero-output", "bool-output", "input-over-cap"],
)
def test_run_rejects_bad_limits_before_launching(tmp_path: Path, kwargs: dict[str, object]) -> None:
    with pytest.raises(ProbeProcessError) as info:
        run_bounded_process(["definitely-not-a-program"], cwd=tmp_path, **kwargs)  # type: ignore[arg-type]
    assert info.value.code == "invalid_timeout"


def test_run_rejects_oversized_or_unencodable_input_before_launching(tmp_path: Path) -> None:
    with pytest.raises(ProbeProcessError) as info:
        run_bounded_process(["x"], cwd=tmp_path, stdin=b"x" * 11, timeout_seconds=1, max_input_bytes=10)
    assert info.value.code == "input_limit"
    with pytest.raises(ProbeProcessError) as info:
        run_bounded_process(["x"], cwd=tmp_path, stdin="\udcff", timeout_seconds=1)
    assert info.value.code == "input_limit"


def test_run_honours_a_cancellation_raised_before_launch(tmp_path: Path) -> None:
    with pytest.raises(ProbeProcessError) as info:
        run_bounded_process(["x"], cwd=tmp_path, timeout_seconds=1, cancelled=lambda: True)
    assert info.value.code == "cancelled"


def test_run_reports_a_program_that_cannot_start_as_launch_failed(tmp_path: Path) -> None:
    with pytest.raises(ProbeProcessError) as info:
        run_bounded_process([str(tmp_path / "no-such-program")], cwd=tmp_path, timeout_seconds=5)
    assert info.value.code == "launch_failed"


# --- a real child -------------------------------------------------------------------------------


def test_run_returns_the_child_streams_and_exit_code(tmp_path: Path) -> None:
    code = "import sys; data = sys.stdin.buffer.read(); sys.stdout.buffer.write(data.upper()); sys.stderr.write('log'); sys.exit(0)"
    result = run_bounded_process(_child(code), cwd=tmp_path, stdin=b"hello", timeout_seconds=30)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"HELLO", b"log")
    assert (result.input_bytes, result.input_complete) == (5, True)
    assert result.duration_seconds >= 0
    assert result.stdin_close_to_exit_seconds >= 0


def test_run_raises_nonzero_when_checked_and_returns_it_when_not(tmp_path: Path) -> None:
    argv = _child("import sys; sys.exit(3)")
    with pytest.raises(ProbeProcessError) as info:
        run_bounded_process(argv, cwd=tmp_path, timeout_seconds=30)
    assert info.value.code == "nonzero"
    assert run_bounded_process(argv, cwd=tmp_path, timeout_seconds=30, check=False).returncode == 3


def test_run_stops_a_child_that_exceeds_the_output_limit(tmp_path: Path) -> None:
    code = "import sys\nwhile True:\n    sys.stdout.buffer.write(b'x' * 4096); sys.stdout.flush()"
    with pytest.raises(ProbeProcessError) as info:
        run_bounded_process(_child(code), cwd=tmp_path, timeout_seconds=30, max_output_bytes=8192)
    assert info.value.code == "output_limit"
    assert info.value.cleanup_failed is False


def test_run_kills_a_child_that_outlives_the_deadline_and_reports_what_it_saw(tmp_path: Path) -> None:
    code = "import sys, time; sys.stdout.write('started'); sys.stdout.flush(); time.sleep(60)"
    with pytest.raises(ProbeProcessError) as info:
        run_bounded_process(_child(code), cwd=tmp_path, timeout_seconds=2)
    error = info.value
    assert error.code == "timeout"
    assert error.budget_seconds == 2
    # The transport reserves cleanup time INSIDE the budget, so the child is
    # stopped before 2 s elapse (measured ~1.2 s). Under load `elapsed` only
    # grows; the discriminator is that a child sleeping 60 s did not finish.
    assert error.elapsed_seconds is not None and 0 < error.elapsed_seconds < 30
    assert error.stdout_bytes == len(b"started")
    assert error.cleanup_failed is False


def test_run_runs_the_child_in_the_given_directory_with_only_the_given_environment(tmp_path: Path) -> None:
    code = "import os, sys; sys.stdout.write(os.getcwd() + '|' + os.environ.get('PROBE_MARK', '-') + '|' + os.environ.get('PATH', '-'))"
    env = sanitized_probe_environment({"PATH": "", "SYSTEMROOT": "C:\\Windows"} if sys.platform == "win32" else {"PATH": ""})
    env["PROBE_MARK"] = "yes"
    result = run_bounded_process(_child(code), cwd=tmp_path, timeout_seconds=30, environment=env)
    cwd, mark, path = result.stdout.decode("utf-8").split("|")
    assert Path(cwd).resolve() == tmp_path.resolve()
    assert (mark, path) == ("yes", "")


def test_limits_are_the_documented_constants() -> None:
    assert OUTPUT_LIMIT_BYTES == 32 * 1024
    assert INPUT_LIMIT_BYTES == 1024 * 1024
