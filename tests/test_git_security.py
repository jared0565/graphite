"""Security tests for the shared Git execution boundary."""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import threading
from pathlib import Path

import pytest

import graphite.git as git_module


def test_shared_git_boundary_module_exists() -> None:
    assert importlib.util.find_spec("graphite.git") is not None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _FakeProcess:
    def __init__(
        self,
        output: bytes,
        *,
        returncode: int = 0,
        timeout_until_killed: bool = False,
    ) -> None:
        self.stdout = _TrackingBytesIO(output)
        self.returncode = returncode
        self.timeout_until_killed = timeout_until_killed
        self.killed = False
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.timeout_until_killed and not self.killed:
            raise subprocess.TimeoutExpired("private", timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class _BlockingVersionProcess(_FakeProcess):
    def __init__(
        self, started: threading.Event, release: threading.Event
    ) -> None:
        super().__init__(b"git version 2.38.0\n")
        self._started = started
        self._release = release

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        self._started.set()
        if not self._release.wait(timeout):
            raise subprocess.TimeoutExpired("private", timeout)
        return self.returncode


class _DescendantHeldPipe:
    def __init__(self, *, close_must_not_run: bool = False) -> None:
        self.close_attempted = False
        self.close_must_not_run = close_must_not_run
        self._closed = threading.Event()

    def read(self, size: int = -1) -> bytes:
        self._closed.wait()
        return b""

    def close(self) -> None:
        self.close_attempted = True
        if self.close_must_not_run:
            raise AssertionError("close must not run while reader owns the pipe")
        self._closed.set()


class _HeldPipeProcess:
    def __init__(
        self,
        *,
        wait_always_times_out: bool = False,
        close_must_not_run: bool = False,
    ) -> None:
        self.stdout = _DescendantHeldPipe(close_must_not_run=close_must_not_run)
        self.returncode = 0
        self.wait_always_times_out = wait_always_times_out
        self.killed = False
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.wait_always_times_out:
            if timeout is None:
                threading.Event().wait()
            raise subprocess.TimeoutExpired("private", timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class _TrackingBytesIO(io.BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.close_attempted = False

    def close(self) -> None:
        self.close_attempted = True
        super().close()


# A hang guard, not a performance bound.
#
# The worker thread can only finish when `runner.run` returns or raises, so a
# cleanup that wedges leaves it alive *forever*. `is_alive()` therefore catches
# a hang at any join timeout at all, which makes a generous one strictly
# better: identical discriminating power, no exposure to scheduler jitter. The
# earlier 0.75s join -- plus a redundant `monotonic() - started < 0.75`
# measured from before the thread even started -- proved nothing extra and
# failed under full-suite load (graphite#50).
#
# Same reasoning as `test_provider_probe_runner.py`'s DNS timeout test
# (graphite#46): prove liveness structurally, and leave time out of it. Do not
# "tighten" this back; precision here would measure the machine, not the code.
_CLEANUP_HANG_GUARD_SECONDS = 30


def _invoke_runner_in_daemon(
    runner: object, *, timeout_seconds: float
) -> tuple[threading.Thread, dict[str, object]]:
    outcome: dict[str, object] = {}

    def invoke() -> None:
        try:
            outcome["result"] = runner.run(  # type: ignore[attr-defined]
                ["status"], timeout_seconds=timeout_seconds, max_stdout_bytes=64
            )
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    return thread, outcome


def _runner_with_fake_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process: _FakeProcess,
    *,
    version_process: _FakeProcess | None = None,
) -> tuple[object, list[tuple[list[str], dict[str, object]]]]:
    repository = tmp_path / "repository"
    trusted_bin = tmp_path / "trusted"
    repository.mkdir()
    trusted_bin.mkdir()
    executable = trusted_bin / ("git.exe" if os.name == "nt" else "git")
    _write(executable, "trusted")
    if os.name != "nt":
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(trusted_bin))
    calls: list[tuple[list[str], dict[str, object]]] = []
    selected_version_process = version_process or _FakeProcess(b"git version 2.38.0\n")

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        calls.append((command, kwargs))
        if command[1:] == ["--version"]:
            return selected_version_process
        return process

    def forbidden_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("GitRunner must not use subprocess.run")

    monkeypatch.setattr(git_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(git_module.subprocess, "run", forbidden_run)
    return git_module.GitRunner(repository), calls


def test_git_runner_rejects_git_2_37_with_a_sanitized_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, calls = _runner_with_fake_process(
        tmp_path,
        monkeypatch,
        _FakeProcess(b"tracked.py\0"),
        version_process=_FakeProcess(b"git version 2.37.9\n"),
    )
    with pytest.raises(git_module.GitUnsupportedVersionError) as error:
        runner.run(["ls-files", "-z"], timeout_seconds=2)

    assert str(error.value) == "Git 2.38 or newer is required"
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[1:] == ["--version"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["shell"] is False
    assert {
        name for name in kwargs["env"] if name.casefold().startswith("git_")
    } == {"GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT"}


@pytest.mark.parametrize(
    "version_output",
    [
        b"git version 2.38\n",
        b"git version 2.38.0\n",
        b"git version 2.53.0.windows.1\n",
        b"git version 2.39.5 (Apple Git-154)\n",
    ],
)
def test_git_runner_accepts_supported_version_formats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version_output: bytes
) -> None:
    runner, calls = _runner_with_fake_process(
        tmp_path,
        monkeypatch,
        _FakeProcess(b"tracked.py\0"),
        version_process=_FakeProcess(version_output),
    )

    result = runner.run(["ls-files", "-z"], timeout_seconds=2)

    assert result.stdout == b"tracked.py\0"
    assert calls[0][0][1:] == ["--version"]
    assert calls[1][0][-2:] == ["ls-files", "-z"]


@pytest.mark.parametrize(
    ("version_process", "expected_reason"),
    [
        pytest.param(
            _FakeProcess(b"private malformed output\n"), "unreadable", id="malformed"
        ),
        pytest.param(
            _FakeProcess(b"prefix git version 2.39.5\n"),
            "unreadable",
            id="malformed-prefix",
        ),
        pytest.param(
            _FakeProcess(b"git version 2.39.5\nprivate"),
            "unreadable",
            id="trailing-control-text",
        ),
        pytest.param(
            _FakeProcess(b"private nonzero output\n", returncode=1),
            "unreadable",
            id="nonzero",
        ),
        pytest.param(
            _FakeProcess(b"private" * 1024), "probe_GitOutputLimitError", id="overflow"
        ),
        pytest.param(
            _FakeProcess(b"", timeout_until_killed=True),
            "probe_GitTimeoutError",
            id="timeout",
        ),
    ],
)
def test_git_runner_sanitizes_version_probe_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version_process: _FakeProcess,
    expected_reason: str,
) -> None:
    """Sanitized AND correctly attributed.

    These six inputs used to assert one shared message, which meant the test
    could not tell an unreadable version string from a probe that never ran --
    it only proved Git's own text stayed out of the message. `expected_reason`
    adds the half that was missing: a case silently reclassified now fails here
    rather than passing under a message that fits everything.
    """
    runner, calls = _runner_with_fake_process(
        tmp_path,
        monkeypatch,
        _FakeProcess(b"must not run"),
        version_process=version_process,
    )
    with pytest.raises(git_module.GitUnsupportedVersionError) as error:
        runner.run(["status"], timeout_seconds=2)

    message = str(error.value)
    assert error.value.reason == expected_reason
    # Drawn from the constant table, never from Git's output -- the property
    # this test exists for, and the reason `review` can surface it verbatim.
    assert message == git_module.git_version_failure_message(expected_reason)
    assert "private" not in message
    assert len(calls) == 1


def test_git_runner_caches_a_successful_version_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = _runner_with_fake_process(
        tmp_path, monkeypatch, _FakeProcess(b"unused")
    )
    calls: list[list[str]] = []

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        calls.append(command)
        if command[1:] == ["--version"]:
            return _FakeProcess(b"git version 2.38.0\n")
        return _FakeProcess(b"ok")

    monkeypatch.setattr(git_module.subprocess, "Popen", fake_popen)

    runner.run(["status"], timeout_seconds=2)
    runner.run(["ls-files"], timeout_seconds=2)

    assert [command[1:] for command in calls].count(["--version"]) == 1
    assert len(calls) == 3


def test_git_runner_serializes_a_simultaneous_version_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = _runner_with_fake_process(
        tmp_path, monkeypatch, _FakeProcess(b"unused")
    )
    start_barrier = threading.Barrier(3)
    both_invoking = threading.Event()
    version_started = threading.Event()
    release_version = threading.Event()
    state_lock = threading.Lock()
    invoking_count = 0
    calls: list[list[str]] = []
    results: list[bytes] = []
    errors: list[BaseException] = []

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        with state_lock:
            calls.append(command)
        if command[1:] == ["--version"]:
            return _BlockingVersionProcess(version_started, release_version)
        return _FakeProcess(b"ok")

    def invoke(arguments: list[str]) -> None:
        nonlocal invoking_count
        start_barrier.wait()
        with state_lock:
            invoking_count += 1
            if invoking_count == 2:
                both_invoking.set()
        try:
            result = runner.run(arguments, timeout_seconds=2)
        except BaseException as exc:
            with state_lock:
                errors.append(exc)
        else:
            with state_lock:
                results.append(result.stdout)

    monkeypatch.setattr(git_module.subprocess, "Popen", fake_popen)
    threads = [
        threading.Thread(target=invoke, args=(["status"],)),
        threading.Thread(target=invoke, args=(["ls-files"],)),
    ]
    for thread in threads:
        thread.start()

    start_barrier.wait(timeout=1)
    assert both_invoking.wait(1)
    assert version_started.wait(1)
    release_version.set()
    for thread in threads:
        thread.join(2)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert results == [b"ok", b"ok"]
    assert [command[1:] for command in calls].count(["--version"]) == 1
    assert len(calls) == 3


def test_git_runner_streams_bounded_stdout_with_stderr_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess(b"tracked.py\0")
    runner, calls = _runner_with_fake_process(tmp_path, monkeypatch, process)

    result = runner.run(
        ["ls-files", "-z"], timeout_seconds=2, max_stdout_bytes=64
    )

    assert result.returncode == 0
    assert result.stdout == b"tracked.py\0"
    assert len(calls) == 2
    _, kwargs = calls[1]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["shell"] is False
    assert process.stdout.close_attempted is True


def test_git_runner_kills_process_when_stdout_limit_is_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess(b"private-overflow")
    runner, _ = _runner_with_fake_process(tmp_path, monkeypatch, process)

    error_type = getattr(git_module, "GitOutputLimitError", git_module.GitError)
    with pytest.raises(error_type, match="Git command output limit exceeded") as error:
        runner.run(["status"], timeout_seconds=2, max_stdout_bytes=4)

    assert process.killed is True
    assert "private" not in str(error.value)


def test_git_runner_kills_and_waits_after_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess(b"", timeout_until_killed=True)
    runner, _ = _runner_with_fake_process(tmp_path, monkeypatch, process)

    with pytest.raises(git_module.GitTimeoutError, match="Git command timeout"):
        runner.run(["status"], timeout_seconds=0.01, max_stdout_bytes=64)

    assert process.killed is True
    assert len(process.wait_calls) >= 2


def test_git_runner_bounds_cleanup_when_descendant_holds_stdout_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _HeldPipeProcess(close_must_not_run=True)
    runner, _ = _runner_with_fake_process(tmp_path, monkeypatch, process)  # type: ignore[arg-type]

    thread, outcome = _invoke_runner_in_daemon(runner, timeout_seconds=0.01)
    thread.join(_CLEANUP_HANG_GUARD_SECONDS)

    assert thread.is_alive() is False
    assert isinstance(outcome.get("error"), git_module.GitLaunchError)
    assert process.killed is True
    assert process.stdout.close_attempted is False
    assert all(timeout is not None for timeout in process.wait_calls)


def test_git_runner_bounds_cleanup_when_wait_keeps_timing_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _HeldPipeProcess(
        wait_always_times_out=True, close_must_not_run=True
    )
    runner, _ = _runner_with_fake_process(tmp_path, monkeypatch, process)  # type: ignore[arg-type]

    thread, outcome = _invoke_runner_in_daemon(runner, timeout_seconds=0.01)
    thread.join(_CLEANUP_HANG_GUARD_SECONDS)

    assert thread.is_alive() is False
    assert isinstance(outcome.get("error"), git_module.GitTimeoutError)
    assert process.killed is True
    assert process.stdout.close_attempted is False
    assert all(timeout is not None for timeout in process.wait_calls)


def test_git_runner_resolves_only_external_absolute_windows_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository_bin = repository / "tools"
    trusted_bin = tmp_path / "trusted tools"
    repository_bin.mkdir(parents=True)
    trusted_bin.mkdir()
    _write(repository_bin / "git.exe", "malicious")
    _write(trusted_bin / "git.exe", "trusted")
    monkeypatch.setenv("PATH", os.pathsep.join(("", ".", str(repository_bin), str(trusted_bin))))

    runner = git_module.GitRunner(repository, platform_name="nt")

    assert runner.executable == (trusted_bin / "git.exe").resolve()
    assert runner.executable.is_absolute()


def test_git_runner_rejects_only_project_contained_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "private repository"
    repository_bin = repository / "tools"
    repository_bin.mkdir(parents=True)
    _write(repository_bin / "git.exe", "malicious")
    monkeypatch.setenv("PATH", str(repository_bin))

    with pytest.raises(git_module.GitUnavailableError) as error:
        git_module.GitRunner(repository, platform_name="nt")

    assert str(error.value) == "Git executable was not found"
    assert str(repository) not in str(error.value)


def test_git_runner_sanitizes_project_root_resolution_failure() -> None:
    class BrokenRoot:
        def resolve(self) -> Path:
            raise OSError("C:\\private\\repo\nINJECTED")

    with pytest.raises(git_module.GitLaunchError) as error:
        git_module.GitRunner(BrokenRoot())  # type: ignore[arg-type]

    assert str(error.value) == "unable to run Git command"
    assert "private" not in str(error.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permission contract")
def test_git_runner_skips_non_executable_posix_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    first_bin = tmp_path / "first bin"
    trusted_bin = tmp_path / "trusted bin"
    repository.mkdir()
    first_bin.mkdir()
    trusted_bin.mkdir()
    _write(first_bin / "git", "not executable")
    _write(trusted_bin / "git", "#!/bin/sh\nexit 0\n")
    (first_bin / "git").chmod(0o600)
    (trusted_bin / "git").chmod(0o700)
    monkeypatch.setenv("PATH", os.pathsep.join((str(first_bin), str(trusted_bin))))

    runner = git_module.GitRunner(repository, platform_name="posix")

    assert runner.executable == (trusted_bin / "git").resolve()


def test_git_runner_hardens_every_process_without_mutating_source_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    trusted_bin = tmp_path / "trusted tools"
    repository.mkdir()
    trusted_bin.mkdir()
    executable = trusted_bin / ("git.exe" if os.name == "nt" else "git")
    _write(executable, "trusted")
    if os.name != "nt":
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(trusted_bin))
    hostile = {
        "GiT_DiR": "private-dir",
        "GIT_INDEX_FILE": "private-index",
        "git_exec_path": "private-exec",
    }
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GRAPHITE_GIT_TEST", "retained")
    source_environment = dict(os.environ)
    calls: list[tuple[list[str], dict[str, object]]] = []

    process = _FakeProcess(b"tracked.py\0")

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        calls.append((command, kwargs))
        if command[1:] == ["--version"]:
            return _FakeProcess(b"git version 2.38.0\n")
        return process

    monkeypatch.setattr(git_module.subprocess, "Popen", fake_popen)

    result = git_module.GitRunner(repository).run(
        ["ls-files", "-z"], timeout_seconds=7.5
    )

    assert result.stdout == b"tracked.py\0"
    assert len(calls) == 2
    version_command, version_kwargs = calls[0]
    assert version_command == [str(executable.resolve()), "--version"]
    assert version_kwargs["shell"] is False
    command, kwargs = calls[1]
    assert command == [
        str(executable.resolve()),
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"safe.directory={repository.resolve()}",
        "ls-files",
        "-z",
    ]
    assert kwargs == {
        "cwd": repository.resolve(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "shell": False,
        "env": kwargs["env"],
    }
    assert process.wait_calls == [7.5]
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["GRAPHITE_GIT_TEST"] == "retained"
    assert environment["GIT_OPTIONAL_LOCKS"] == "0"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert {
        name for name in environment if name.casefold().startswith("git_")
    } == {"GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT"}
    assert dict(os.environ) == source_environment


@pytest.mark.parametrize(
    ("raised", "error_name", "message"),
    [
        (subprocess.TimeoutExpired("private", 1), "GitTimeoutError", "Git command timeout"),
        (OSError("C:\\private\\repo\nINJECTED"), "GitLaunchError", "unable to run Git command"),
    ],
)
def test_git_runner_sanitizes_process_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raised: BaseException,
    error_name: str,
    message: str,
) -> None:
    repository = tmp_path / "repository"
    trusted_bin = tmp_path / "trusted"
    repository.mkdir()
    trusted_bin.mkdir()
    executable = trusted_bin / ("git.exe" if os.name == "nt" else "git")
    _write(executable, "trusted")
    if os.name != "nt":
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(trusted_bin))

    if isinstance(raised, subprocess.TimeoutExpired):
        process = _FakeProcess(b"", timeout_until_killed=True)

        def timeout_popen(command: list[str], **kwargs: object) -> _FakeProcess:
            if command[1:] == ["--version"]:
                return _FakeProcess(b"git version 2.38.0\n")
            return process

        monkeypatch.setattr(git_module.subprocess, "Popen", timeout_popen)
    else:
        def broken_popen(command: list[str], **kwargs: object) -> _FakeProcess:
            if command[1:] == ["--version"]:
                return _FakeProcess(b"git version 2.38.0\n")
            raise raised

        monkeypatch.setattr(git_module.subprocess, "Popen", broken_popen)

    error_type = getattr(git_module, error_name)
    with pytest.raises(error_type) as error:
        git_module.GitRunner(repository).run(["status"], timeout_seconds=1)

    assert str(error.value) == message
    assert "private" not in str(error.value)


# --- #37: which launch failure was it, and what did the OS say? -------------
#
# `git_unavailable` is the one sighting recorded on graphite#37, and it is a
# bucket, not a cause. `_run_git` widens `GitError` into it, so three unrelated
# faults arrive wearing the same string: no executable found, found but would
# not start, started but its protected config was unreadable. `b3ae61a` split
# those by class name -- which is one bit of information for SEVEN distinct
# `GitLaunchError` raise sites inside `_run_bounded` alone.
#
# The errno is the discriminator, and today every site throws it away: three
# sites hold a live `OSError` and raise a bare message from it, and the reader
# thread catches `OSError` into a boolean `Event` where even the exception is
# gone. A boolean says WHICH step failed and never WHY -- the same lesson the
# macOS cleanup round paid for.
#
# The invariant these must not break: the sanitized MESSAGE stays exactly what
# it is (a path-free constant, asserted verbatim above), because Git's own text
# routinely embeds a path. So the detail goes in a separate `diagnostic()`, and
# it carries a fixed-vocabulary step token and an integer errno -- never a
# message, never `filename`, never argv.


def test_a_launch_failure_carries_the_os_error_number() -> None:
    """Without this, `git_unavailable` on a CI runner is unactionable.

    A Windows spawn failure that is `ENOMEM`-shaped (the runner is out of paging
    file) needs a completely different answer from one that is `EACCES`-shaped
    (antivirus holding the executable). Both currently print the same string.
    """
    error = git_module.GitLaunchError(
        "unable to run Git command", reason="popen", os_error=22
    )

    assert "os=22" in error.diagnostic()
    assert "popen" in error.diagnostic()
    assert "GitLaunchError" in error.diagnostic()


def test_the_sanitized_message_is_unchanged_by_the_diagnostic() -> None:
    """The detail must ride beside the message, never inside it.

    `test_git_runner_sanitizes_process_errors` asserts `str(error.value)` is
    equal to a constant, and that assertion is load-bearing: it is what proves
    Git's path-bearing text never escapes. Appending the diagnostic to `__str__`
    would defeat it while looking like an improvement.
    """
    error = git_module.GitLaunchError(
        "unable to run Git command", reason="popen", os_error=22
    )

    assert str(error) == "unable to run Git command"


def test_a_launch_failure_with_no_os_error_claims_none() -> None:
    """Vacuity guard. Four of the seven sites have no `OSError` in hand at all
    (no stdout pipe, reader still alive, close failed). If the format emitted a
    placeholder those would read as `os=None` -- an errno-shaped field that is
    not an errno, which is worse than an absent one."""
    error = git_module.GitLaunchError("unable to run Git command", reason="no_stdout")

    assert "os=" not in error.diagnostic()
    assert "None" not in error.diagnostic()
    assert "no_stdout" in error.diagnostic()


def test_the_os_error_filename_never_reaches_the_diagnostic() -> None:
    """`OSError` carries `.filename`, and it is a PATH.

    This is the one way the errno work could reintroduce exactly the leak the
    `from None` sanitising exists to prevent -- stringify the exception instead
    of reading `.errno` and the checkout path ships to the CI log.
    """
    private = r"C:\Users\someone\private\checkout\git.exe"
    source = OSError(13, "Permission denied", private)
    assert source.filename == private, "fixture must actually carry a path"

    error = git_module.GitLaunchError(
        "unable to run Git command", reason="popen", os_error=source.errno
    )

    assert "os=13" in error.diagnostic()
    assert private not in error.diagnostic()
    assert "someone" not in error.diagnostic()
    assert "Permission denied" not in error.diagnostic()


def test_distinct_launch_steps_are_distinguishable() -> None:
    """One string for seven raise sites is what made #37 a dead end."""
    steps = [
        git_module.GitLaunchError("unable to run Git command", reason=reason).diagnostic()
        for reason in ("resolve", "popen", "wait", "no_stdout", "reader_hang", "read", "close")
    ]

    assert len(set(steps)) == len(steps), f"launch steps collapse together: {steps}"


def test_a_plain_git_error_still_reports_just_its_class() -> None:
    """Errors that never had a step or an errno must not grow noise."""
    assert git_module.GitUnavailableError("nope").diagnostic() == "GitUnavailableError"


def test_a_real_spawn_failure_wires_its_own_errno_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tests above construct the error by hand, so they pin the FORMAT and
    say nothing about whether any raise site fills it in. That gap is the whole
    defect restated: a field that exists and is never populated reports exactly
    as much as no field at all.

    So drive a real `Popen` failure through `GitRunner` and read what comes out.
    The `OSError` here carries a filename, because that is the realistic shape --
    a spawn refusal names the executable it refused.
    """
    import errno as errno_module

    repository = tmp_path / "repository"
    trusted_bin = tmp_path / "trusted"
    repository.mkdir()
    trusted_bin.mkdir()
    executable = trusted_bin / ("git.exe" if os.name == "nt" else "git")
    _write(executable, "trusted")
    if os.name != "nt":
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(trusted_bin))

    private = str(tmp_path / "private" / "git.exe")
    refusal = OSError(errno_module.EACCES, "Permission denied", private)

    def refusing_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        if command[1:] == ["--version"]:
            return _FakeProcess(b"git version 2.38.0\n")
        raise refusal

    monkeypatch.setattr(git_module.subprocess, "Popen", refusing_popen)

    with pytest.raises(git_module.GitLaunchError) as error:
        git_module.GitRunner(repository).run(["status"], timeout_seconds=1)

    assert error.value.os_error == errno_module.EACCES, "the errno was dropped at the raise site"
    assert error.value.reason == "popen", "the step was not recorded"
    assert f"os={errno_module.EACCES}" in error.value.diagnostic()
    assert private not in error.value.diagnostic(), "the OSError filename leaked"
    assert str(error.value) == "unable to run Git command", "the sanitized message moved"


def _trusted_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = tmp_path / "repository"
    trusted_bin = tmp_path / "trusted"
    repository.mkdir()
    trusted_bin.mkdir()
    executable = trusted_bin / ("git.exe" if os.name == "nt" else "git")
    _write(executable, "trusted")
    if os.name != "nt":
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(trusted_bin))
    return repository


def test_a_version_probe_that_timed_out_does_not_read_as_an_old_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The likeliest reading of graphite#37, and today it is unfalsifiable.

    `_ensure_supported_version` spends at most `_GIT_VERSION_TIMEOUT_SECONDS`
    (2.0) on `git --version`, and folds GitTimeoutError, GitLaunchError and
    GitOutputLimitError into `GitUnsupportedVersionError` -- which `_run_git`
    then buckets into `git_unavailable`. So "this runner was too slow to spawn a
    process for two seconds" and "this machine has Git 2.20" arrive as the same
    string, and the first is exactly what a loaded Windows CI runner produces
    intermittently while every earlier Git call in the same test succeeded.

    Naming the inner cause is what makes the next sighting decide between them
    instead of restating the bucket.
    """
    repository = _trusted_repository(tmp_path, monkeypatch)

    def slow_version_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        if command[1:] == ["--version"]:
            return _FakeProcess(b"", timeout_until_killed=True)
        return _FakeProcess(b"")

    monkeypatch.setattr(git_module.subprocess, "Popen", slow_version_popen)

    with pytest.raises(git_module.GitUnsupportedVersionError) as error:
        git_module.GitRunner(repository).run(["status"], timeout_seconds=1)

    assert "GitTimeoutError" in error.value.diagnostic(), (
        "a probe that ran out of time must say so; reporting the bucket alone is "
        "what left #37 undiagnosable"
    )
    # Sanitized, but no longer the version sentence: the message is now looked
    # up per cause, and no version was read here. See
    # `test_a_probe_failure_does_not_tell_the_operator_to_upgrade_git`.
    assert str(error.value) == git_module.git_version_failure_message(
        "probe_GitTimeoutError"
    ), "message must stay sanitized"


def test_a_probe_failure_does_not_tell_the_operator_to_upgrade_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The message was a false statement, not merely a vague one.

    `diagnostic()` carries the inner cause for a reader of CI logs, but the
    MESSAGE -- the sentence an operator actually acts on -- still said "Git 2.38
    or newer is required" when no version had been read at all. Two of the three
    conditions reaching this class are transient probe failures on a Git that is
    installed, current, and working. The advice is not merely unhelpful there:
    it names a specific remedy, and that remedy is wrong.

    Same shape as the channel audit gate printing "this commit names no agent"
    when the interpreter was missing -- an error that sends you to fix the one
    thing that is not broken costs more than one that admits it does not know.
    """
    repository = _trusted_repository(tmp_path, monkeypatch)

    def slow_version_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        if command[1:] == ["--version"]:
            return _FakeProcess(b"", timeout_until_killed=True)
        return _FakeProcess(b"")

    monkeypatch.setattr(git_module.subprocess, "Popen", slow_version_popen)

    with pytest.raises(git_module.GitUnsupportedVersionError) as error:
        git_module.GitRunner(repository).run(["status"], timeout_seconds=1)

    message = str(error.value)
    assert "2.38" not in message, (
        "no version was read, so the message must not assert a version requirement"
    )
    assert "timed out" in message
    # Still sanitized: every message is a module constant, never Git's output.
    assert message == git_module.git_version_failure_message("probe_GitTimeoutError")


def test_a_genuinely_old_git_still_says_which_version_it_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falsifiability control: the useful message must survive the fix.

    Deleting the version requirement from every message would satisfy the test
    above while removing the one case where "upgrade Git" is exactly right.
    """
    runner, _calls = _runner_with_fake_process(
        tmp_path,
        monkeypatch,
        _FakeProcess(b"tracked.py\0"),
        version_process=_FakeProcess(b"git version 2.20.1\n"),
    )

    with pytest.raises(git_module.GitUnsupportedVersionError) as error:
        runner.run(["status"], timeout_seconds=2)

    assert str(error.value) == "Git 2.38 or newer is required"
    assert error.value.reason == "too_old"


def test_a_genuinely_old_git_stays_distinguishable_from_a_slow_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falsifiability guard for the test above.

    If every `GitUnsupportedVersionError` grew the same suffix, that test would
    pass while still collapsing the two causes it exists to separate.
    """
    repository = _trusted_repository(tmp_path, monkeypatch)

    def old_git_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        return _FakeProcess(b"git version 2.20.0\n")

    monkeypatch.setattr(git_module.subprocess, "Popen", old_git_popen)

    with pytest.raises(git_module.GitUnsupportedVersionError) as error:
        git_module.GitRunner(repository).run(["status"], timeout_seconds=1)

    diagnostic = error.value.diagnostic()
    assert "GitTimeoutError" not in diagnostic, "an old Git must not report a timeout"
    assert "too_old" in diagnostic
