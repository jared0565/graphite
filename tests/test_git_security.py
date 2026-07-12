"""Security tests for the shared Git execution boundary."""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import threading
import time
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
    "version_process",
    [
        pytest.param(_FakeProcess(b"private malformed output\n"), id="malformed"),
        pytest.param(
            _FakeProcess(b"prefix git version 2.39.5\n"), id="malformed-prefix"
        ),
        pytest.param(
            _FakeProcess(b"git version 2.39.5\nprivate"),
            id="trailing-control-text",
        ),
        pytest.param(
            _FakeProcess(b"private nonzero output\n", returncode=1), id="nonzero"
        ),
        pytest.param(_FakeProcess(b"private" * 1024), id="overflow"),
        pytest.param(
            _FakeProcess(b"", timeout_until_killed=True), id="timeout"
        ),
    ],
)
def test_git_runner_sanitizes_version_probe_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version_process: _FakeProcess,
) -> None:
    runner, calls = _runner_with_fake_process(
        tmp_path,
        monkeypatch,
        _FakeProcess(b"must not run"),
        version_process=version_process,
    )
    with pytest.raises(git_module.GitUnsupportedVersionError) as error:
        runner.run(["status"], timeout_seconds=2)

    assert str(error.value) == "Git 2.38 or newer is required"
    assert "private" not in str(error.value)
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
    started = time.monotonic()

    thread, outcome = _invoke_runner_in_daemon(runner, timeout_seconds=0.01)
    thread.join(0.75)

    assert thread.is_alive() is False
    assert time.monotonic() - started < 0.75
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
    started = time.monotonic()

    thread, outcome = _invoke_runner_in_daemon(runner, timeout_seconds=0.01)
    thread.join(0.75)

    assert thread.is_alive() is False
    assert time.monotonic() - started < 0.75
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
