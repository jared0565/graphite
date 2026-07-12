"""Isolated, bounded deep readiness probes."""
from __future__ import annotations

import json
import os
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
_CLEANUP_SECONDS = 0.5
_SAFE_ERROR_CODES = frozenset(
    {
        "timeout",
        "output_limit",
        "nonzero",
        "launch_failed",
        "io_failed",
        "invalid_timeout",
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
    stdin: bytes | None = None,
    timeout_seconds: float,
    max_output_bytes: int = _OUTPUT_LIMIT,
) -> ProbeProcessResult:
    """Run a process with independent bounded 32 KiB stdout and stderr buffers."""
    if timeout_seconds <= 0 or max_output_bytes <= 0:
        raise DoctorProbeError("invalid_timeout")
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=_minimal_environment(),
            shell=False,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, ValueError):
        raise DoctorProbeError("launch_failed") from None

    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()
    io_failed = threading.Event()

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
            io_failed.set()

    threads = [
        threading.Thread(target=read_pipe, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read_pipe, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    failure_code: str | None = None
    try:
        if stdin is not None and process.stdin is not None:
            try:
                process.stdin.write(stdin)
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                failure_code = "io_failed"
        deadline = started + timeout_seconds
        while failure_code is None:
            if overflow.is_set():
                failure_code = "output_limit"
                break
            remaining = deadline - time.monotonic()
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
        if failure_code is not None or process.returncode is None:
            try:
                process.kill()
            except (OSError, ValueError):
                pass
        try:
            process.wait(timeout=_CLEANUP_SECONDS)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        if process.stdin is not None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
        # A descendant may inherit a pipe after the direct child exits. Never let
        # closing that foreign-held pipe make doctor cleanup unbounded.
        for thread, pipe in zip(threads, (process.stdout, process.stderr)):
            thread.join(_CLEANUP_SECONDS)
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
    del selected  # The selected repository is an invariant input and is never a process cwd.

    result: DoctorCheck | None = None
    try:
        temp_root = (_temp_root_resolver() if _temp_root_resolver else Path(tempfile.gettempdir())).resolve()
        factory = _temp_factory or tempfile.TemporaryDirectory
        with factory(prefix="graphite-doctor-") as temporary:
            work = Path(temporary).resolve(strict=True)
            try:
                work.relative_to(temp_root)
            except ValueError:
                return _blocked("isolation", "unsafe_temp_path")

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
                    if validation.get("ok") is not True or validation.get("error_count") != 0 or validation.get("errors") not in (None, []):
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
