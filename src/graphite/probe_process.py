"""Bounded subprocess transport with sanitized defaults or exact child environments."""
from __future__ import annotations

import errno
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
# Time left to the child to notice EOF and exit after a deferred stdin close is
# forced. Only reached when the caller's predicate never passes -- the healthy
# path closes as soon as the transcript is complete, long before this.
_STDIN_CLOSE_RESERVE_SECONDS = 2.0
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
    """A classified transport failure containing no process or environment data.

    ``os_error`` is the OS error number behind the classification, when there
    was one. A number is safe to surface where a message is not: it carries no
    path, no argv and no environment, whereas ``strerror`` routinely embeds a
    filename.

    It exists because the code alone cannot distinguish causes that need
    opposite handling -- notably a genuine pipe fault from this module's own
    `_cancel_synchronous_io` aborting a worker's in-flight I/O during cleanup
    (graphite#41). Issue #29 lost several rounds to exactly this: an error
    classified down to a label that discarded the fact which discriminated.

    ``cleanup_failed`` says containment did not complete -- a process may have
    been left running. It is a SEPARATE fact from ``code``, and carrying it
    separately is the point: it used to be written over the code, so a run that
    had already determined why it failed reported `cleanup_failed` instead
    (graphite#46). Both facts are true at once and only the transport knows
    both, so it reports both.
    """

    def __init__(self, code: str, os_error: int | None = None, *, cleanup_failed: bool = False) -> None:
        self.code = code if code in _SAFE_ERROR_CODES else "unexpected"
        # Coerced, never trusted: an OSError subclass can carry anything in
        # `errno`, and only an int may reach the message.
        self.os_error = os_error if isinstance(os_error, int) and not isinstance(os_error, bool) else None
        self.cleanup_failed = bool(cleanup_failed)
        message = "probe input failed" if self.code == "input_failed" else f"probe process failed: {self.code}"
        if self.os_error is not None:
            message = f"{message} (os={self.os_error})"
        # Only when it is not already the code, so the common case does not read
        # "cleanup_failed (cleanup also failed)". A bare boolean is safe to
        # surface where a message is not -- it names no path, pid or argv.
        if self.cleanup_failed and self.code != "cleanup_failed":
            message = f"{message} (cleanup also failed)"
        super().__init__(message)


@dataclass(frozen=True)
class ProbeProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float
    # Input-side evidence. A child may legitimately stop reading before we
    # finish writing (see write_input), which is tolerated -- but that leaves a
    # short stream indistinguishable, from the outside, from a child that read
    # everything and chose to answer less. These two fields keep that
    # distinction observable so a failing probe can say which happened.
    # Defaulted so the many positional constructions in tests stay valid, and
    # so a hand-built result reads as "input delivered in full".
    input_bytes: int = 0
    input_complete: bool = True
    # Seconds between closing the child's stdin and the child exiting.
    #
    # `write_input` closes stdin the moment the payload is written, so a server
    # that reads EOF as end-of-session can tear down while messages it already
    # received are still unprocessed -- answering `initialize`, never answering
    # `tools/list`, and exiting 0 (issue #29). A near-zero interval says the
    # close and the exit are the same event; a larger one says the child died
    # of something else. Without it the two are indistinguishable from outside.
    #
    # -1.0 when there was no stdin to close, so "not measured" cannot be
    # mistaken for "died instantly".
    stdin_close_to_exit_seconds: float = -1.0


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
    stdin_close_when: Callable[[bytes], bool] | None = None,
) -> ProbeProcessResult:
    """Run one isolated process tree under a single hard transport deadline.

    ``environment=None`` selects the sanitized probe default. An explicit mapping is the
    complete, non-inheriting child environment, including when the mapping is empty.

    ``stdin_close_when`` defers closing the child's stdin until the predicate
    accepts the stdout captured so far. Default ``None`` keeps the original
    behaviour -- close as soon as the payload is written -- because for a child
    that reads to EOF before doing anything, deferring would waste the whole
    budget.

    It exists because an immediate close makes EOF arrive while the child is
    still working, and a request/response server reads EOF as end-of-session:
    it tears down the write side with a reply still in flight, answering the
    first request and silently dropping the second (graphite#29). The close is
    still bounded by the run's own deadline, so a predicate that never passes
    degrades to the old timing rather than hanging.
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
    input_truncated: threading.Event | None = None
    cleanup_started: threading.Event | None = None
    workers: list[threading.Thread] = []
    worker_pipes: list[tuple[threading.Thread, Any]] = []
    failure_code: str | None = None
    cleanup_ok = True

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

    # One-slot mailboxes: the writer runs on its own thread, so the timestamp
    # has to cross back without another lock.
    stdin_closed_at: list[float | None] = [None]
    exited_at: list[float | None] = [None]

    # The close can now come from either the writer thread (immediate) or the
    # polling loop (deferred), so it needs to be idempotent and serialized --
    # two closes would race on the timestamp and on the handle.
    stdin_close_lock = threading.Lock()
    stdin_is_closed = [False]
    write_completed = threading.Event()
    # The OS error number behind an `input_failed`, carried back from the writer
    # thread so the classification does not discard it (graphite#41).
    writer_error: list[int | None] = [None]

    def close_stdin() -> None:
        with stdin_close_lock:
            if stdin_is_closed[0]:
                return
            stdin_is_closed[0] = True
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except (OSError, ValueError):
                pass
            # Stamped after the close returns, so the interval measures the
            # child's life beyond EOF rather than our own write time.
            stdin_closed_at[0] = time.monotonic()

    def write_input() -> None:
        if input_data is None or process.stdin is None:
            return
        defer = stdin_close_when is not None
        try:
            process.stdin.write(input_data)
            process.stdin.flush()
        except BrokenPipeError:
            # The child stopped reading -- it exited, or closed stdin -- before
            # we finished writing. That is legitimate rather than a transport
            # fault: `_MCP_BOOTSTRAP` rejects invalid bindings with
            # SystemExit(70) *before* its first stdin read, so a correctly
            # refusing bootstrap never reads the input we are still sending.
            # The child's exit status is the verdict; bytes it declined to read
            # are not evidence of anything. Reporting this as `input_failed`
            # discarded the real return code whenever the write lost that race,
            # which is the intermittent `probe input failed` in issue #29.
            # BrokenPipeError subclasses OSError, so it must be caught first.
            # Tolerating it silently would erase the only signal that the child
            # saw a short stream, so record it as evidence instead.
            defer = False
            if input_truncated is not None:
                input_truncated.set()
        except (OSError, ValueError) as exc:
            defer = False
            # winerror first: on Windows an aborted synchronous I/O reports
            # there, and `errno` is the coarser translation of it.
            writer_error[0] = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
            if writer_error[0] == errno.EINVAL:
                # Windows' other spelling of "the reader is gone". Measured:
                # 3 of 10 CI runs failed as `input_failed (os=22)` across two
                # different tests, while the same write raises BrokenPipeError
                # 40/40 locally -- which is why it never reproduced off CI
                # (graphite#41). Same condition as the branch above, so it gets
                # the same verdict: legitimate, recorded, not a fault.
                #
                # Widened by exactly one number on purpose. An OSError carrying
                # no errno stays a genuine transport fault, which
                # test_probe_transport_rechecks_late_writer_failure pins.
                if input_truncated is not None:
                    input_truncated.set()
            elif writer_failed is not None:
                writer_failed.set()
        finally:
            # Only a fully delivered payload is worth waiting on. If the write
            # broke or failed there is nothing more to send, so hold nothing
            # open -- deferring past a failed write would just delay the
            # child's EOF for a response that is never coming.
            if defer:
                write_completed.set()
            else:
                close_stdin()

    try:
        overflow = threading.Event()
        io_failed = threading.Event()
        writer_failed = threading.Event()
        input_truncated = threading.Event()
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
        # Latest moment a deferred close may still leave the child room to see
        # EOF and exit inside the execution window.
        stdin_close_deadline = execution_deadline - min(
            _STDIN_CLOSE_RESERVE_SECONDS, max(0.05, timeout_seconds * 0.25)
        )
        while failure_code is None:
            if stdin_close_when is not None and write_completed.is_set() and not stdin_is_closed[0]:
                if time.monotonic() >= stdin_close_deadline:
                    close_stdin()
                else:
                    try:
                        # A torn read can only ever yield a prefix of what the
                        # reader has appended, which reads as "not complete
                        # yet" and is retried on the next tick -- so no lock is
                        # needed against the reader thread here.
                        if stdin_close_when(bytes(outputs["stdout"])):
                            close_stdin()
                    except Exception:
                        # A predicate that raises must not strand the child on
                        # an open pipe; fall back to the old timing.
                        close_stdin()
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
                exited_at[0] = time.monotonic()
                break
            except subprocess.TimeoutExpired:
                continue
            except (OSError, ValueError):
                failure_code = "io_failed"
                break
    except Exception:
        failure_code = "io_failed"
    finally:
        # Never leave a deferred close outstanding: a child blocked reading an
        # open pipe would otherwise have to be killed on a path where letting it
        # see EOF and exit on its own is cleaner and faster.
        #
        # Strictly gated on the writer having finished. Closing the handle while
        # that thread is still inside `write` blocks until the write drains --
        # which against a child that never reads means waiting out the full
        # payload, turning a 0.25s timeout into seconds. Non-deferred runs are
        # untouched; their close already happened on the writer thread, and
        # `_cleanup_process_transport` handles a writer still stuck in one.
        if stdin_close_when is not None and write_completed.is_set():
            close_stdin()
        if cleanup_started is not None:
            cleanup_started.set()
        # Recorded, NOT assigned over `failure_code`. Every recheck below is
        # guarded by `failure_code is None`; this one used to be the exception,
        # so a run that already knew it had timed out came back as
        # `cleanup_failed` (graphite#46). The precedence is now explicit and
        # pinned by test: transport failure, then the child's own exit status,
        # and `cleanup_failed` only when there is nothing else to report.
        cleanup_ok = _cleanup_process_transport(process, workers, worker_pipes, deadline)

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
        raise ProbeProcessError(
            failure_code,
            writer_error[0] if failure_code == "input_failed" else None,
            cleanup_failed=not cleanup_ok,
        )
    if process.returncode is None or (check and process.returncode != 0):
        raise ProbeProcessError("nonzero", cleanup_failed=not cleanup_ok)
    # Last, so it never speaks over a diagnosis -- but still raised rather than
    # returned, because a run that may have leaked a process is not a success.
    if not cleanup_ok:
        raise ProbeProcessError("cleanup_failed", cleanup_failed=True)
    closed_at = stdin_closed_at[0]
    finished_at = exited_at[0]
    # Both stamps are required: a child with no stdin, or one reaped on a path
    # that never observed the exit, has no interval to report and must say so
    # rather than claim an instant death.
    # A close that lands AFTER the exit is also "not measured": with a deferred
    # close the child can exit on its own while stdin is still open, and it then
    # never saw the EOF at all. Reporting 0.0 there would read as "died the
    # instant its stdin closed" -- the exact conclusion this field exists to
    # support or refute.
    outlived_close = (
        max(0.0, finished_at - closed_at)
        if closed_at is not None and finished_at is not None and closed_at <= finished_at
        else -1.0
    )
    return ProbeProcessResult(
        process.returncode,
        bytes(outputs["stdout"]),
        bytes(outputs["stderr"]),
        time.monotonic() - started,
        len(input_data) if input_data is not None else 0,
        input_truncated is None or not input_truncated.is_set(),
        outlived_close,
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


def _darwin_group_holds_only_the_zombie_leader(process: _Process, error: OSError) -> bool:
    """Say whether darwin's EPERM means "nothing left to signal" rather than "not allowed".

    MEASURED on macos-latest 3.12.10 against ubuntu-latest as the control, four
    process states, with the errno printed for each:

        leader alive                      killpg -> OK        (both platforms)
        leader exited, unreaped zombie    killpg -> EPERM     darwin
                                          killpg -> SUCCESS   Linux
        leader exited + live DESCENDANT   killpg -> OK        (both platforms)
        after reap                        killpg -> ESRCH     (both platforms)

    This module deliberately holds the exited leader as an unreaped zombie so
    its pgid cannot be recycled under the signals that follow. On darwin that
    makes the group unsignalable, so EVERY successful probe reported a failed
    containment -- 46 of the 62 failures in graphite#46.

    EPERM is genuinely ambiguous on darwin: "forbidden" and "nothing signalable"
    share it. Reading it as the latter is licensed by the third row -- a live
    descendant makes darwin answer OK -- and by the fact that this transport
    CREATES the group itself via `setsid()`, from its own uid and a sanitized
    environment, so a member it may not signal is not reachable. State that
    reasoning wherever this is touched; without it the check is error-swallowing.

    Gated on the leader having exited, which is not decoration: on the timeout
    path `returncode` is None and the group holds a LIVE leader, where EPERM
    cannot mean "only zombies" and stays a failure. Verified at the call site
    rather than inferred -- 0 on the success path, None on the timeout path.
    """
    if sys.platform != "darwin" or not isinstance(error, PermissionError):
        return False
    if process.returncode is None:
        # `returncode` is only set where the run HAPPENED to observe the exit.
        # The success and timeout paths reach `process.wait` and so carry a
        # current value, but `output_limit` and `cancelled` break out of the
        # loop before it -- measured: a child that overflows the limit and exits
        # immediately still reads None here. Trusting the cache would call a
        # contained tree a leak, intermittently, depending on whether the child
        # won the race to exit. `poll` is the same non-reaping observation the
        # run itself uses, and a leader that really is alive still answers None.
        poll = getattr(process, "poll", None)
        if callable(poll):
            try:
                poll()
            except (OSError, ValueError):
                return False
    return process.returncode is not None


def _force_kill_process_tree(process: _Process, deadline: float) -> bool:
    cleanup_ok = True
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except PermissionError as exc:
            if _darwin_group_holds_only_the_zombie_leader(process, exc):
                return True
            cleanup_ok = False
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
