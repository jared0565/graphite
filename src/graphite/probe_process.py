"""Bounded subprocess transport with sanitized defaults or exact child environments."""
from __future__ import annotations

import math
import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

OUTPUT_LIMIT_BYTES = 32 * 1024
INPUT_LIMIT_BYTES = 1024 * 1024
_CLEANUP_SECONDS = 1.0
_GRACEFUL_CLEANUP_SECONDS = 0.1
_SAFE_ERROR_CODES = frozenset(
    {
        "timeout",
        "output_limit",
        "nonzero",
        "launch_failed",
        "io_failed",
        "input_failed",
        "cleanup_failed",
        "invalid_timeout",
        "invalid_environment",
        "input_limit",
        "cancelled",
    }
)
_ESSENTIAL_ENV = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "PATH",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
)


class ProbeProcessError(Exception):
    """A classified transport failure containing no process or environment data."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _SAFE_ERROR_CODES else "unexpected"
        message = "probe input failed" if self.code == "input_failed" else f"probe process failed: {self.code}"
        super().__init__(message)


@dataclass(frozen=True)
class ProbeProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float


def sanitized_probe_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return OS essentials plus the exact Graphite package path, excluding ambient secrets."""
    ambient = os.environ if source is None else source
    env = {name: ambient[name] for name in _ESSENTIAL_ENV if name in ambient}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _validated_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    validated = sanitized_probe_environment() if environment is None else dict(environment)
    if any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or not key
        or "=" in key
        or "\0" in key
        or "\0" in value
        for key, value in validated.items()
    ):
        raise ProbeProcessError("invalid_environment")
    return validated


class _Process(Protocol):
    pid: int
    stdin: Any
    stdout: Any
    stderr: Any
    returncode: int | None
    def wait(self, timeout: float) -> int: ...
    def kill(self) -> bool | None: ...


class _PosixProcess:
    """Observe leader exit without reaping it until group containment completes."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self.pid = process.pid
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
        self.returncode: int | None = None
        self._reaped = False
        self._kqueue: Any | None = None
        self._kqueue_exit_flags = 0
        if not callable(getattr(os, "waitid", None)):
            if sys.platform != "darwin" or not hasattr(select, "kqueue"):
                raise RuntimeError("non-reaping process observation unavailable")
            self._kqueue = select.kqueue()
            note_exit_status = getattr(select, "KQ_NOTE_EXITSTATUS", 0x04000000)
            self._kqueue_exit_flags = select.KQ_NOTE_EXIT | note_exit_status
            event = select.kevent(
                self.pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
                fflags=self._kqueue_exit_flags,
            )
            self._kqueue.control([event], 0, 0)

    def _observe(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if self._kqueue is not None:
            events = self._kqueue.control(None, 1, 0)
            if not events:
                return None
            if events[0].fflags & self._kqueue_exit_flags != self._kqueue_exit_flags:
                raise OSError("kqueue exit status unavailable")
            status = events[0].data
            signal_number = status & 0x7F
            self.returncode = -signal_number if signal_number else (status >> 8) & 0xFF
        else:
            result = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if result is None:
                return None
            self.returncode = result.si_status if result.si_code == os.CLD_EXITED else -result.si_status
        return self.returncode

    def poll(self) -> int | None:
        return self._observe()

    def wait(self, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        while True:
            result = self._observe()
            if result is not None:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired([], timeout)
            time.sleep(min(0.01, remaining))

    def kill(self) -> bool:
        try:
            os.kill(self.pid, signal.SIGKILL)
            return True
        except ProcessLookupError:
            return True
        except OSError:
            return False

    def reap(self, deadline: float) -> bool:
        if self._reaped:
            return True
        while True:
            try:
                waited_pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                return False
            if waited_pid == self.pid:
                self._reaped = True
                self._process.returncode = os.waitstatus_to_exitcode(status)
                self.returncode = self._process.returncode
                if self._kqueue is not None:
                    self._kqueue.close()
                    self._kqueue = None
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))


def _launch_process(
    argv: list[str],
    *,
    cwd: Path,
    input_data: bytes | None,
    environment: Mapping[str, str],
) -> _Process:
    if os.name == "nt":
        from .windows_job import launch

        return launch(argv, cwd=cwd, environment=environment, with_stdin=input_data is not None)
    if not callable(getattr(os, "waitid", None)) and (sys.platform != "darwin" or not hasattr(select, "kqueue")):
        raise RuntimeError("non-reaping process observation unavailable")
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=environment,
        shell=False,
        stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        return _PosixProcess(process)
    except Exception:
        process.kill()
        try:
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        raise


def run_bounded_process(
    argv: list[str],
    *,
    cwd: Path,
    stdin: bytes | str | None = None,
    timeout_seconds: float,
    max_output_bytes: int = OUTPUT_LIMIT_BYTES,
    max_input_bytes: int = INPUT_LIMIT_BYTES,
    check: bool = True,
    environment: Mapping[str, str] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ProbeProcessResult:
    """Run one isolated process tree under a single hard transport deadline.

    ``environment=None`` selects the sanitized probe default. An explicit mapping is the
    complete, non-inheriting child environment, including when the mapping is empty.
    """
    if (
        not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or max_output_bytes <= 0
        or isinstance(max_input_bytes, bool)
        or not isinstance(max_input_bytes, int)
        or max_input_bytes <= 0
        or max_input_bytes > 4 * 1024 * 1024
    ):
        raise ProbeProcessError("invalid_timeout")
    if isinstance(stdin, str):
        try:
            input_data = stdin.encode("utf-8")
        except UnicodeEncodeError:
            raise ProbeProcessError("input_limit") from None
    else:
        input_data = stdin
    if input_data is not None and len(input_data) > max_input_bytes:
        raise ProbeProcessError("input_limit")

    cancellation = cancelled or (lambda: False)
    try:
        if cancellation():
            raise ProbeProcessError("cancelled")
    except ProbeProcessError:
        raise
    except Exception:
        raise ProbeProcessError("io_failed") from None

    started = time.monotonic()
    deadline = started + timeout_seconds
    try:
        validated_environment = _validated_environment(environment)
    except ProbeProcessError:
        raise
    except Exception:
        raise ProbeProcessError("launch_failed") from None
    try:
        process = _launch_process(argv, cwd=cwd, input_data=input_data, environment=validated_environment)
    except Exception:
        raise ProbeProcessError("launch_failed") from None

    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    overflow: threading.Event | None = None
    io_failed: threading.Event | None = None
    writer_failed: threading.Event | None = None
    cleanup_started: threading.Event | None = None
    workers: list[threading.Thread] = []
    worker_pipes: list[tuple[threading.Thread, Any]] = []
    failure_code: str | None = None

    def read_pipe(name: str, pipe: Any) -> None:
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                remaining = max_output_bytes + 1 - len(outputs[name])
                if remaining > 0:
                    outputs[name].extend(chunk[:remaining])
                if len(outputs[name]) > max_output_bytes:
                    overflow.set()
                    break
        except (OSError, ValueError):
            if cleanup_started is not None and not cleanup_started.is_set() and io_failed is not None:
                io_failed.set()

    def write_input() -> None:
        if input_data is None or process.stdin is None:
            return
        try:
            process.stdin.write(input_data)
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            if writer_failed is not None:
                writer_failed.set()
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    try:
        overflow = threading.Event()
        io_failed = threading.Event()
        writer_failed = threading.Event()
        cleanup_started = threading.Event()
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            thread = threading.Thread(target=read_pipe, args=(name, pipe), daemon=True)
            thread.start()
            workers.append(thread)
            worker_pipes.append((thread, pipe))
        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()
        workers.append(writer)
        worker_pipes.append((writer, process.stdin))
        cleanup_reserve = min(_CLEANUP_SECONDS, max(0.05, timeout_seconds * 0.4))
        execution_deadline = deadline - cleanup_reserve
        while failure_code is None:
            try:
                if cancellation():
                    failure_code = "cancelled"
                    break
            except Exception:
                failure_code = "io_failed"
                break
            if overflow.is_set():
                failure_code = "output_limit"
                break
            if writer_failed.is_set():
                failure_code = "input_failed"
                break
            remaining = execution_deadline - time.monotonic()
            if remaining <= 0:
                failure_code = "timeout"
                break
            try:
                process.wait(timeout=min(0.05, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
            except (OSError, ValueError):
                failure_code = "io_failed"
                break
    except Exception:
        failure_code = "io_failed"
    finally:
        if cleanup_started is not None:
            cleanup_started.set()
        if not _cleanup_process_transport(process, workers, worker_pipes, deadline):
            failure_code = "cleanup_failed"

    # These events can be set after the direct child exits, so recheck only
    # after cleanup and every bounded join has completed.
    if failure_code is None and writer_failed is not None and writer_failed.is_set():
        failure_code = "input_failed"
    elif failure_code is None and overflow is not None and overflow.is_set():
        failure_code = "output_limit"
    elif failure_code is None and io_failed is not None and io_failed.is_set():
        failure_code = "io_failed"
    elif failure_code is None and any(thread.is_alive() for thread in workers):
        failure_code = "timeout"
    if failure_code is not None:
        raise ProbeProcessError(failure_code)
    if process.returncode is None or (check and process.returncode != 0):
        raise ProbeProcessError("nonzero")
    return ProbeProcessResult(
        process.returncode,
        bytes(outputs["stdout"]),
        bytes(outputs["stderr"]),
        time.monotonic() - started,
    )


def _cleanup_process_transport(
    process: _Process,
    workers: list[threading.Thread],
    worker_pipes: list[tuple[threading.Thread, Any]],
    deadline: float,
) -> bool:
    cleanup_ok = _terminate_process_tree(process, deadline) is not False
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except (subprocess.TimeoutExpired, OSError, ValueError):
        try:
            if process.kill() is False:
                cleanup_ok = False
        except (OSError, ValueError):
            cleanup_ok = False
    for thread in workers:
        if thread.is_alive():
            _cancel_synchronous_io(thread)
        thread.join(min(0.2, max(0.0, deadline - time.monotonic())))
        if thread.is_alive():
            _cancel_synchronous_io(thread)
            thread.join(min(0.2, max(0.0, deadline - time.monotonic())))
    active_pipes = {id(pipe) for thread, pipe in worker_pipes if pipe is not None and thread.is_alive()}
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None and id(pipe) not in active_pipes:
            try:
                pipe.close()
            except (OSError, ValueError):
                cleanup_ok = False
    close_handles = getattr(process, "close_handles", None)
    if close_handles is not None:
        try:
            if close_handles() is False:
                cleanup_ok = False
        except (OSError, ValueError):
            cleanup_ok = False
    reap = getattr(process, "reap", None)
    if reap is not None:
        try:
            if reap(deadline) is False:
                cleanup_ok = False
        except (OSError, ValueError):
            cleanup_ok = False
    return cleanup_ok


def _cancel_synchronous_io(thread: threading.Thread) -> None:
    if os.name != "nt" or thread.native_id is None:
        return
    try:
        import ctypes

        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.CancelSynchronousIo.argtypes = (wintypes.HANDLE,)
        kernel32.CancelSynchronousIo.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenThread(0x0001, False, thread.native_id)
        if handle:
            try:
                kernel32.CancelSynchronousIo(handle)
            finally:
                kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        pass


def _gracefully_signal_process_tree(process: _Process) -> bool:
    try:
        if os.name == "nt":
            if getattr(process, "poll", lambda: process.returncode)() is not None:
                return False
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
        return True
    except (AttributeError, OSError, ValueError):
        return False


def _force_kill_process_tree(process: _Process, deadline: float) -> bool:
    cleanup_ok = True
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except (OSError, ValueError):
            cleanup_ok = False
    elif hasattr(process, "terminate_tree"):
        try:
            if process.terminate_tree() is False:
                cleanup_ok = False
        except (OSError, ValueError):
            cleanup_ok = False
    if process.returncode is None:
        try:
            if process.kill() is False:
                cleanup_ok = False
        except (OSError, ValueError):
            cleanup_ok = False
    return cleanup_ok


def _posix_process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Unknown probe failures must fail safe: assume the group still exists.
        return True


def _terminate_process_tree(process: _Process, deadline: float) -> bool:
    """Gracefully signal the process group, then force-kill the tree within the deadline."""
    signaled = _gracefully_signal_process_tree(process)
    if signaled and os.name != "nt":
        grace_deadline = min(deadline, time.monotonic() + _GRACEFUL_CLEANUP_SECONDS)
        group_exists = _posix_process_group_exists(process.pid)
        while group_exists:
            remaining = grace_deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.01, remaining))
            group_exists = _posix_process_group_exists(process.pid)
        if not group_exists:
            return True
    elif signaled:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining > 0:
            try:
                process.wait(timeout=min(_GRACEFUL_CLEANUP_SECONDS, remaining))
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
    return _force_kill_process_tree(process, deadline)
