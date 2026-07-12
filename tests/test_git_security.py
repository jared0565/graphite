"""Security tests for the shared Git execution boundary."""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

import graphite.git as git_module


def test_shared_git_boundary_module_exists() -> None:
    assert importlib.util.find_spec("graphite.git") is not None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, b"tracked.py\0", b"")

    monkeypatch.setattr(git_module.subprocess, "run", fake_run)

    result = git_module.GitRunner(repository).run(
        ["ls-files", "-z"], timeout_seconds=7.5
    )

    assert result.stdout == b"tracked.py\0"
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        str(executable.resolve()),
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "ls-files",
        "-z",
    ]
    assert kwargs == {
        "cwd": repository.resolve(),
        "check": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": 7.5,
        "shell": False,
        "env": kwargs["env"],
    }
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

    def broken_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise raised

    monkeypatch.setattr(git_module.subprocess, "run", broken_run)

    error_type = getattr(git_module, error_name)
    with pytest.raises(error_type) as error:
        git_module.GitRunner(repository).run(["status"], timeout_seconds=1)

    assert str(error.value) == message
    assert "private" not in str(error.value)
