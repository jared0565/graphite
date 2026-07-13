"""Deadline-aware orchestration for isolated deep readiness probes."""
from __future__ import annotations

import json
import math
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config
from .doctor import DoctorCheck
from .probe_process import ProbeProcessError, ProbeProcessResult, run_bounded_process

_CLEANUP_RESERVE_SECONDS = 1.0


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
        raise ProbeProcessError("unexpected") from None
    if not isinstance(value, dict):
        raise ProbeProcessError("unexpected")
    return value


def _count(value: object, *, positive: bool = False) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if value < (1 if positive else 0):
        return None
    return value


def _check_deadline(limit: float, clock: Callable[[], float]) -> None:
    if clock() >= limit:
        raise ProbeProcessError("timeout")


def _cleanup_verified_temp(
    path: Path,
    cleanup: Callable[[Path], None],
    deadline: float,
    clock: Callable[[], float],
) -> str | None:
    """Clean only a previously verified target, waiting no longer than the total deadline."""
    failed = threading.Event()

    def perform() -> None:
        try:
            cleanup(path)
        except Exception:
            failed.set()

    thread = threading.Thread(target=perform, daemon=True)
    thread.start()
    thread.join(max(0.0, deadline - clock()))
    if thread.is_alive():
        return "cleanup_timeout"
    if failed.is_set():
        return "cleanup_failed"
    try:
        if path.exists():
            return "cleanup_failed"
    except OSError:
        return "cleanup_failed"
    return None


def probe_core_pipeline(
    selected_root: Path,
    python_executable: str = sys.executable,
    timeout_seconds: float = 30,
    *,
    _runner: Callable[..., ProbeProcessResult] = run_bounded_process,
    _temp_factory: Callable[..., str] | None = None,
    _temp_cleanup: Callable[[Path], None] | None = None,
    _temp_root_resolver: Callable[[], Path] | None = None,
    _clock: Callable[[], float] = time.monotonic,
) -> DoctorCheck:
    """Exercise build/validate/query under one checked end-to-end deadline.

    Subprocess transport is hard-cancelled. Filesystem calls cannot be safely
    preempted, so their deadline is checked immediately before and after each
    synchronous phase. Verified cleanup runs in a bounded daemon thread.
    """
    started = _clock()
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        return _blocked("timeout", "invalid_timeout")
    deadline = started + timeout_seconds
    cleanup_reserve = min(_CLEANUP_RESERVE_SECONDS, max(0.05, timeout_seconds * 0.2))
    phase_deadline = deadline - cleanup_reserve
    work: Path | None = None
    verified_work: Path | None = None
    candidate: DoctorCheck | None = None

    try:
        _check_deadline(phase_deadline, _clock)
        selected = selected_root.resolve(strict=True)
        if not selected.is_dir():
            return _blocked("invariant", "invalid_selected_root")
        _check_deadline(phase_deadline, _clock)

        temp_root = (_temp_root_resolver() if _temp_root_resolver else Path(tempfile.gettempdir())).resolve()
        _check_deadline(phase_deadline, _clock)
        try:
            temp_root.relative_to(selected)
        except ValueError:
            pass
        else:
            return _blocked("isolation", "selected_contains_temp")

        factory = _temp_factory or tempfile.mkdtemp
        _check_deadline(phase_deadline, _clock)
        work = Path(factory(prefix="graphite-doctor-")).resolve(strict=True)
        creation_exceeded_deadline = _clock() >= phase_deadline
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
        verified_work = work
        if creation_exceeded_deadline:
            raise ProbeProcessError("timeout")
        _check_deadline(phase_deadline, _clock)

        repo = work / "repo"
        source = repo / "src"
        source.mkdir(parents=True)
        (source / "lib.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
        (repo / "app.py").write_text("from src.lib import answer\nVALUE = answer()\n", encoding="utf-8")
        _check_deadline(phase_deadline, _clock)

        out = work / "out"
        cache = work / "cache"
        graph = out / "graph.json"
        commands = [
            [python_executable, "-B", "-m", "graphite", "--output-dir", str(out), "--cache-dir", str(cache), "--llm", "none", "build", str(repo)],
            [python_executable, "-B", "-m", "graphite", "validate", "--graph-json", str(graph), "--json"],
            [python_executable, "-B", "-m", "graphite", "query", "stats", "--graph-json", str(graph)],
        ]
        outputs: list[ProbeProcessResult] = []
        validation_nodes: int | None = None
        validation_edges: int | None = None
        for index, command in enumerate(commands):
            _check_deadline(phase_deadline, _clock)
            outputs.append(_runner(command, cwd=work, timeout_seconds=phase_deadline - _clock()))
            _check_deadline(phase_deadline, _clock)
            if index == 1:
                validation = _json_object(outputs[1].stdout)
                _check_deadline(phase_deadline, _clock)
                validation_nodes = _count(validation.get("node_count"), positive=True)
                validation_edges = _count(validation.get("edge_count"))
                if (
                    validation.get("ok") is not True
                    or validation.get("valid", True) is not True
                    or validation.get("error_count") != 0
                    or validation.get("errors") != []
                    or validation_nodes is None
                    or validation_edges is None
                ):
                    candidate = _blocked("validation", "validation_failed")
                    break

        if candidate is None:
            stats = _json_object(outputs[2].stdout)
            _check_deadline(phase_deadline, _clock)
            stats_nodes = _count(stats.get("node_count"), positive=True)
            stats_edges = _count(stats.get("edge_count"))
            if stats_nodes is None or stats_edges is None:
                candidate = _blocked("response", "invalid_counts")
            elif stats_nodes != validation_nodes or stats_edges != validation_edges:
                candidate = _blocked("response", "count_mismatch")
            else:
                _check_deadline(phase_deadline, _clock)
                candidate = DoctorCheck(
                    "deep_core",
                    "Deterministic pipeline",
                    "ready",
                    "The isolated deterministic build, validation, and query pipeline is ready.",
                    {
                        "node_count": stats_nodes,
                        "edge_count": stats_edges,
                        "duration_ms": max(0, round((_clock() - started) * 1000)),
                        "commands_completed": 3,
                    },
                )
    except ProbeProcessError as exc:
        if exc.code == "unexpected":
            candidate = _blocked("response", "malformed_output")
        else:
            candidate = _blocked(_process_error_type(exc.code), exc.code)
    except (OSError, RuntimeError):
        candidate = _blocked("isolation", "temporary_directory_failed")
    except Exception:
        candidate = _blocked("unexpected", "probe_failed")
    finally:
        if verified_work is not None:
            cleanup_error = _cleanup_verified_temp(
                verified_work,
                _temp_cleanup or shutil.rmtree,
                deadline,
                _clock,
            )
            if cleanup_error is not None:
                candidate = _blocked("cleanup", cleanup_error)

    if candidate is None:
        return _blocked("unexpected", "probe_failed")
    if _clock() >= deadline and candidate.details.get("error_type") != "cleanup":
        return _blocked("timeout", "timeout")
    return candidate


def run_deep_probes(root: Path, *, cfg: Config, include_llm: bool) -> list[DoctorCheck]:
    """Return the deep capabilities implemented in this release."""
    del cfg, include_llm
    return [probe_core_pipeline(root)]
