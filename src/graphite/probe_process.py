"""Bounded, environment-sanitized subprocess transport for readiness probes."""
from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
        "invalid_timeout",
        "input_limit",
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


def run_bounded_process(
    argv: list[str],
    *,
    cwd: Path,
    stdin: bytes | str | None = None,
    timeout_seconds: float,
    max_output_bytes: int = OUTPUT_LIMIT_BYTES,
    check: bool = True,
) -> ProbeProcessResult:
    """Run one isolated process tree under a single hard transport deadline."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or max_output_bytes <= 0:
        raise ProbeProcessError("invalid_timeout")
    if isinstance(stdin, str):
        try:
            input_data = stdin.encode("utf-8")
        except UnicodeEncodeError:
            raise ProbeProcessError("input_limit") from None
    else:
        input_data = stdin
    if input_data is not None and len(input_data) > INPUT_LIMIT_BYTES:
        raise ProbeProcessError("input_limit")

    started = time.monotonic()
    deadline = started + timeout_seconds
    process_options: dict[str, Any] = {}
    if os.name == "nt":
        process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=sanitized_probe_environment(),
            shell=False,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **process_options,
        )
    except (OSError, ValueError):
        raise ProbeProcessError("launch_failed") from None

    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()
    io_failed = threading.Event()
    writer_failed = threading.Event()
    cleanup_started = threading.Event()

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
            if not cleanup_started.is_set():
                io_failed.set()

    readers = [
        threading.Thread(target=read_pipe, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read_pipe, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in readers:
        thread.start()

    def write_input() -> None:
        if input_data is None or process.stdin is None:
            return
        try:
            process.stdin.write(input_data)
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            writer_failed.set()
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    writer = threading.Thread(target=write_input, daemon=True)
    writer.start()
    failure_code: str | None = None
    try:
        cleanup_reserve = min(_CLEANUP_SECONDS, max(0.05, timeout_seconds * 0.4))
        execution_deadline = deadline - cleanup_reserve
        while failure_code is None:
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
    finally:
        cleanup_started.set()
        workers = [writer, *readers]
        if failure_code is not None:
            _terminate_process_tree(process, deadline)
        else:
            for thread in workers:
                thread.join(min(0.05, max(0.0, deadline - time.monotonic())))
            if any(thread.is_alive() for thread in workers):
                _terminate_process_tree(process, deadline)
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except (subprocess.TimeoutExpired, OSError, ValueError):
            try:
                process.kill()
            except (OSError, ValueError):
                pass
        for thread in workers:
            if thread.is_alive():
                _cancel_synchronous_io(thread)
        for thread in workers:
            thread.join(min(0.2, max(0.0, deadline - time.monotonic())))
            if thread.is_alive():
                _cancel_synchronous_io(thread)
                thread.join(min(0.2, max(0.0, deadline - time.monotonic())))
        for thread, pipe in zip((writer, *readers), (process.stdin, process.stdout, process.stderr)):
            if not thread.is_alive() and pipe is not None:
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass

    # These events can be set after the direct child exits, so recheck only
    # after cleanup and every bounded join has completed.
    if failure_code is None and writer_failed.is_set():
        failure_code = "input_failed"
    elif failure_code is None and overflow.is_set():
        failure_code = "output_limit"
    elif failure_code is None and io_failed.is_set():
        failure_code = "io_failed"
    elif failure_code is None and any(thread.is_alive() for thread in (writer, *readers)):
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


def _cancel_synchronous_io(thread: threading.Thread) -> None:
    if os.name != "nt" or thread.native_id is None:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenThread(0x0001, False, thread.native_id)
        if handle:
            try:
                kernel32.CancelSynchronousIo(handle)
            finally:
                kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        pass


def _trusted_taskkill() -> Path | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
        if length <= 0 or length >= len(buffer):
            return None
        system_directory = Path(buffer.value).resolve(strict=True)
        resolved = (system_directory / "taskkill.exe").resolve(strict=True)
    except (AttributeError, OSError, ValueError):
        return None
    if not resolved.is_absolute() or not resolved.is_file():
        return None
    if resolved.name.lower() != "taskkill.exe" or resolved.parent != system_directory:
        return None
    return resolved


def _gracefully_signal_process_tree(process: subprocess.Popen[bytes]) -> bool:
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
        return True
    except (AttributeError, OSError, ValueError):
        return False


def _force_kill_process_tree(process: subprocess.Popen[bytes], deadline: float) -> None:
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ValueError):
            pass
    else:
        executable = _trusted_taskkill()
        if executable is not None and deadline > time.monotonic():
            try:
                killer = subprocess.Popen(
                    [str(executable), "/PID", str(process.pid), "/T", "/F"],
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=sanitized_probe_environment(),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                try:
                    killer.wait(timeout=min(0.5, max(0.0, deadline - time.monotonic())))
                except (subprocess.TimeoutExpired, OSError, ValueError):
                    try:
                        killer.kill()
                    except (OSError, ValueError):
                        pass
                    try:
                        killer.wait(timeout=max(0.0, deadline - time.monotonic()))
                    except (subprocess.TimeoutExpired, OSError, ValueError):
                        pass
            except (OSError, ValueError):
                pass
    try:
        process.kill()
    except (OSError, ValueError):
        pass


def _terminate_process_tree(process: subprocess.Popen[bytes], deadline: float) -> None:
    """Gracefully signal the process group, then force-kill the tree within the deadline."""
    signaled = _gracefully_signal_process_tree(process)
    if signaled:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining > 0:
            try:
                process.wait(timeout=min(_GRACEFUL_CLEANUP_SECONDS, remaining))
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
    _force_kill_process_tree(process, deadline)
