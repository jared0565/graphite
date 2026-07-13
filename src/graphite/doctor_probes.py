"""Isolated, bounded deep readiness probes."""
from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .doctor import DoctorCheck

_OUTPUT_LIMIT = 32 * 1024
_INPUT_LIMIT = 1024 * 1024
_CLEANUP_SECONDS = 1.0
_SAFE_ERROR_CODES = frozenset(
    {
        "timeout",
        "output_limit",
        "nonzero",
        "launch_failed",
        "io_failed",
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


class DoctorProbeError(Exception):
    """A classified probe failure that never includes process or environment data."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _SAFE_ERROR_CODES else "unexpected"
        super().__init__(f"doctor probe failed: {self.code}")


@dataclass(frozen=True)
class ProbeProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float


def _minimal_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return OS essentials only; all provider, credential, Git, and Node inputs are dropped."""
    ambient = os.environ if source is None else source
    env = {name: ambient[name] for name in _ESSENTIAL_ENV if name in ambient}
    # Ensure the probe imports this exact installed/worktree package, never selected-root code.
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _run_bounded(
    argv: list[str],
    *,
    cwd: Path,
    stdin: bytes | str | None = None,
    timeout_seconds: float,
    max_output_bytes: int = _OUTPUT_LIMIT,
) -> ProbeProcessResult:
    """Run a process with independent bounded 32 KiB stdout and stderr buffers."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or max_output_bytes <= 0:
        raise DoctorProbeError("invalid_timeout")
    if isinstance(stdin, str):
        try:
            input_data = stdin.encode("utf-8")
        except UnicodeEncodeError:
            raise DoctorProbeError("input_limit") from None
    else:
        input_data = stdin
    if input_data is not None and len(input_data) > _INPUT_LIMIT:
        raise DoctorProbeError("input_limit")
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
            env=_minimal_environment(),
            shell=False,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **process_options,
        )
    except (OSError, ValueError):
        raise DoctorProbeError("launch_failed") from None

    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()
    io_failed = threading.Event()
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

    writer_failed = threading.Event()

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
                failure_code = "io_failed"
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
        worker_threads = [writer, *readers]
        if failure_code is not None:
            _terminate_process_tree(process, deadline)
        else:
            for thread in worker_threads:
                thread.join(min(0.05, max(0.0, deadline - time.monotonic())))
            if any(thread.is_alive() for thread in worker_threads):
                _terminate_process_tree(process, deadline)
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except (subprocess.TimeoutExpired, OSError, ValueError):
            try:
                process.kill()
            except (OSError, ValueError):
                pass
        for thread in worker_threads:
            if thread.is_alive():
                _cancel_synchronous_io(thread)
        for thread in worker_threads:
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

    if failure_code is None and overflow.is_set():
        failure_code = "output_limit"
    if failure_code is None and io_failed.is_set():
        failure_code = "io_failed"
    if failure_code is not None:
        raise DoctorProbeError(failure_code)
    returncode = process.returncode
    if returncode is None or returncode != 0:
        raise DoctorProbeError("nonzero")
    return ProbeProcessResult(
        returncode,
        bytes(outputs["stdout"]),
        bytes(outputs["stderr"]),
        time.monotonic() - started,
    )


def _cancel_synchronous_io(thread: threading.Thread) -> None:
    """Best-effort cancellation for a Windows thread blocked in pipe I/O."""
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
        candidate = system_directory / "taskkill.exe"
        resolved = candidate.resolve(strict=True)
    except (AttributeError, OSError, ValueError):
        return None
    if not resolved.is_absolute() or not resolved.is_file():
        return None
    if resolved.name.lower() != "taskkill.exe" or resolved.parent != system_directory:
        return None
    return resolved


def _terminate_process_tree(process: subprocess.Popen[bytes], deadline: float) -> None:
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError, ValueError):
            pass
    else:
        executable = _trusted_taskkill()
        remaining = deadline - time.monotonic()
        if executable is not None and remaining > 0:
            try:
                killer = subprocess.Popen(
                    [str(executable), "/PID", str(process.pid), "/T", "/F"],
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=_minimal_environment(),
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


def _blocked(error_type: str, code: str) -> DoctorCheck:
    return DoctorCheck(
        "deep_core",
        "Deterministic pipeline",
        "blocked",
        "The isolated deterministic pipeline probe failed safely.",
        {"error_type": error_type, "code": code},
        ("Run the Graphite build, validate, and query commands locally and inspect their diagnostics.",),
    )


def _process_error_type(code: str) -> str:
    if code in {"timeout", "output_limit"}:
        return code
    return "process"


def _json_object(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DoctorProbeError("unexpected") from None
    if not isinstance(value, dict):
        raise DoctorProbeError("unexpected")
    return value


def probe_core_pipeline(
    selected_root: Path,
    python_executable: str = sys.executable,
    timeout_seconds: float = 30,
    *,
    _runner: Callable[..., ProbeProcessResult] = _run_bounded,
    _temp_factory: Callable[..., Any] | None = None,
    _temp_root_resolver: Callable[[], Path] | None = None,
) -> DoctorCheck:
    """Exercise build/validate/query in OS temp under one total timeout budget."""
    started = time.monotonic()
    try:
        selected = selected_root.resolve(strict=True)
        if not selected.is_dir():
            return _blocked("invariant", "invalid_selected_root")
    except (OSError, RuntimeError):
        return _blocked("invariant", "invalid_selected_root")

    result: DoctorCheck | None = None
    try:
        temp_root = (_temp_root_resolver() if _temp_root_resolver else Path(tempfile.gettempdir())).resolve()
        try:
            temp_root.relative_to(selected)
        except ValueError:
            pass
        else:
            return _blocked("isolation", "selected_contains_temp")
        factory = _temp_factory or tempfile.TemporaryDirectory
        with factory(prefix="graphite-doctor-") as temporary:
            work = Path(temporary).resolve(strict=True)
            try:
                work.relative_to(temp_root)
            except ValueError:
                return _blocked("isolation", "unsafe_temp_path")
            try:
                work.relative_to(selected)
            except ValueError:
                pass
            else:
                return _blocked("isolation", "overlapping_temp_path")
            try:
                selected.relative_to(work)
            except ValueError:
                pass
            else:
                return _blocked("isolation", "overlapping_temp_path")

            repo = work / "repo"
            source = repo / "src"
            source.mkdir(parents=True)
            (source / "lib.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
            (repo / "app.py").write_text("from src.lib import answer\nVALUE = answer()\n", encoding="utf-8")
            out = work / "out"
            cache = work / "cache"
            graph = out / "graph.json"
            commands = [
                [python_executable, "-B", "-m", "graphite", "--output-dir", str(out), "--cache-dir", str(cache), "--llm", "none", "build", str(repo)],
                [python_executable, "-B", "-m", "graphite", "validate", "--graph-json", str(graph), "--json"],
                [python_executable, "-B", "-m", "graphite", "query", "stats", "--graph-json", str(graph)],
            ]
            outputs: list[ProbeProcessResult] = []
            for index, command in enumerate(commands):
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise DoctorProbeError("timeout")
                outputs.append(_runner(command, cwd=work, timeout_seconds=remaining))
                if index == 1:
                    try:
                        validation = _json_object(outputs[1].stdout)
                    except DoctorProbeError:
                        result = _blocked("response", "malformed_output")
                        break
                    validation_nodes = validation.get("node_count")
                    if (
                        validation.get("ok") is not True
                        or validation.get("valid", True) is not True
                        or validation.get("error_count") != 0
                        or validation.get("errors") not in (None, [])
                        or not isinstance(validation_nodes, int)
                        or isinstance(validation_nodes, bool)
                        or validation_nodes <= 0
                    ):
                        result = _blocked("validation", "validation_failed")
                        break

            if result is None:
                try:
                    stats = _json_object(outputs[2].stdout)
                except DoctorProbeError:
                    result = _blocked("response", "malformed_output")
                else:
                    node_count = stats.get("node_count")
                    edge_count = stats.get("edge_count")
                    if not isinstance(node_count, int) or isinstance(node_count, bool) or node_count <= 0 or not isinstance(edge_count, int) or isinstance(edge_count, bool) or edge_count < 0:
                        result = _blocked("response", "invalid_counts")
                    else:
                        result = DoctorCheck(
                            "deep_core",
                            "Deterministic pipeline",
                            "ready",
                            "The isolated deterministic build, validation, and query pipeline is ready.",
                            {
                                "node_count": node_count,
                                "edge_count": edge_count,
                                "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
                                "commands_completed": 3,
                            },
                        )
    except DoctorProbeError as exc:
        result = _blocked(_process_error_type(exc.code), exc.code)
    except (OSError, RuntimeError):
        result = _blocked("cleanup", "temporary_directory_failed")
    except Exception:
        result = _blocked("unexpected", "probe_failed")
    return result or _blocked("unexpected", "probe_failed")


def run_deep_probes(root: Path, *, cfg: Config, include_llm: bool) -> list[DoctorCheck]:
    """Return the deep capabilities implemented in this release."""
    del cfg, include_llm
    return [probe_core_pipeline(root)]
