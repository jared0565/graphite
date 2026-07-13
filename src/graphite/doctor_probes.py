"""Deadline-aware orchestration for isolated deep readiness probes."""
from __future__ import annotations

import json
import math
import os
import site
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
_MCP_OUTPUT_LIMIT_BYTES = 1024 * 1024
_MCP_LINE_LIMIT = 64
_MCP_RESPONSE_LIMIT = 8
_MCP_NESTING_LIMIT = 32
_MCP_PROTOCOL_VERSION = "2024-11-05"
_REQUIRED_MCP_TOOLS = frozenset(
    {"graphite_query", "graphite_summary", "graphite_community", "graphite_refresh"}
)
_TYPESCRIPT_SCRIPT = (
    "try{require.resolve('typescript')}catch(error){"
    "if(error&&error.code==='MODULE_NOT_FOUND'){"
    "process.stdout.write(JSON.stringify({missing_module:'typescript'}));process.exit(0)}"
    "process.exit(4)}"
    "process.stdout.write(JSON.stringify({detected:true}));"
)
_MCP_BOOTSTRAP = """\
import importlib.util
import pathlib
import runpy
import sys

trusted = pathlib.Path(sys.argv[1]).resolve(strict=True)
selected = pathlib.Path(sys.argv[2]).resolve(strict=True)
if not trusted.is_dir() or not selected.is_dir():
    raise SystemExit(70)
allowed = []
for raw in sys.argv[3:]:
    candidate = pathlib.Path(raw)
    if not candidate.is_absolute():
        raise SystemExit(70)
    candidate = candidate.resolve(strict=True)
    if not candidate.is_dir():
        raise SystemExit(70)
    try:
        candidate.relative_to(selected)
    except ValueError:
        pass
    else:
        raise SystemExit(70)
    try:
        selected.relative_to(candidate)
    except ValueError:
        pass
    else:
        raise SystemExit(70)
    allowed.append(str(candidate))
sys.path[:] = [str(trusted), *allowed]
spec = importlib.util.find_spec("graphite.mcp")
if spec is None or spec.origin is None:
    raise SystemExit(70)
origin = pathlib.Path(spec.origin).resolve(strict=True)
try:
    origin.relative_to(trusted)
except ValueError:
    raise SystemExit(70) from None
runpy.run_module("graphite.mcp", run_name="__main__")
"""
_TYPESCRIPT_REMEDIATION = (
    "Validate package: node C:/Users/fbmac/atlas/Codex/.codex_state/user_home/scripts/validate-packages.cjs typescript",
    "Then add typescript with the target project's existing package manager.",
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


def _degraded_probe(code: str, label: str, failure: str) -> DoctorCheck:
    return DoctorCheck(
        code,
        label,
        "degraded",
        f"The {label} deep probe {failure}.",
        {"code": failure},
    )


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _mcp_import_roots(selected_root: Path) -> tuple[Path, Path, list[Path]]:
    """Build a parent-validated import allowlist without repository-local paths."""
    selected = selected_root.resolve(strict=True)
    if not selected.is_dir():
        raise OSError
    trusted = Path(__file__).resolve(strict=True).parent.parent
    runtime_prefixes: list[Path] = []
    for raw_prefix in {sys.base_prefix, sys.prefix}:
        try:
            runtime_prefixes.append(Path(raw_prefix).resolve(strict=True))
        except OSError:
            continue
    site_roots: set[Path] = set()
    try:
        raw_site_roots = [*site.getsitepackages(), site.getusersitepackages()]
    except (AttributeError, OSError):
        raw_site_roots = []
    for raw_site_root in raw_site_roots:
        try:
            site_root = Path(raw_site_root).resolve(strict=True)
        except OSError:
            continue
        if site_root.is_dir():
            site_roots.add(site_root)
    roots: list[Path] = []
    seen = {os.path.normcase(str(trusted))}
    for raw in sys.path:
        if not raw:
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            continue
        try:
            candidate = candidate.resolve(strict=True)
        except OSError:
            continue
        is_runtime_root = any(_path_is_within(candidate, prefix) for prefix in runtime_prefixes)
        is_site_root = any(_path_is_within(candidate, root) for root in site_roots)
        if (
            not candidate.is_dir()
            or _paths_overlap(candidate, selected)
            or (not is_runtime_root and not is_site_root)
        ):
            continue
        normalized = os.path.normcase(str(candidate))
        if normalized not in seen:
            seen.add(normalized)
            roots.append(candidate)
    return trusted, selected, roots


def _validate_json_nesting(text: str) -> None:
    """Reject excessive or mismatched JSON nesting without interpreting string data."""
    stack: list[str] = []
    in_string = False
    escaped = False
    pairs = {"}": "{", "]": "["}
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            stack.append(character)
            if len(stack) > _MCP_NESTING_LIMIT:
                raise ValueError
        elif character in "]}":
            if not stack or stack.pop() != pairs[character]:
                raise ValueError
    if in_string or stack:
        raise ValueError


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError


def _json_object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _parse_mcp_responses(output: bytes, stderr: bytes) -> dict[int, dict[str, Any]]:
    if len(output) + len(stderr) > _MCP_OUTPUT_LIMIT_BYTES:
        raise ValueError
    raw_lines = output.splitlines()
    if not raw_lines or len(raw_lines) > _MCP_LINE_LIMIT:
        raise ValueError
    responses: dict[int, dict[str, Any]] = {}
    response_count = 0
    for raw_line in raw_lines:
        text = raw_line.decode("utf-8")
        _validate_json_nesting(text)
        envelope = json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_json_object_without_duplicate_keys,
        )
        if not isinstance(envelope, dict) or envelope.get("jsonrpc") != "2.0":
            raise ValueError
        if "id" not in envelope:
            if not set(envelope).issubset({"jsonrpc", "method", "params"}):
                raise ValueError
            method = envelope.get("method")
            params = envelope.get("params", {})
            if not isinstance(method, str) or not method or not isinstance(params, (dict, list)):
                raise ValueError
            continue
        response_count += 1
        if response_count > _MCP_RESPONSE_LIMIT:
            raise ValueError
        if not set(envelope).issubset({"jsonrpc", "id", "result", "error"}):
            raise ValueError
        response_id = envelope["id"]
        if not isinstance(response_id, int) or isinstance(response_id, bool):
            raise ValueError
        if response_id not in {1, 2} or response_id in responses:
            raise ValueError
        if "result" not in envelope or "error" in envelope:
            raise ValueError
        responses[response_id] = envelope
    if set(responses) != {1, 2} or response_count != 2:
        raise ValueError
    return responses


def probe_mcp(
    root: Path,
    *,
    python_executable: str = sys.executable,
    timeout_seconds: float = 10.0,
    _runner: Callable[..., ProbeProcessResult] = run_bounded_process,
) -> DoctorCheck:
    """Initialize the MCP server and inspect its read-only tool inventory."""
    requests = (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "graphite-doctor", "version": "1.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    stdin = b"".join(
        json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
        for request in requests
    )
    try:
        trusted, selected, import_roots = _mcp_import_roots(root)
        result = _runner(
            [
                python_executable,
                "-I",
                "-S",
                "-B",
                "-c",
                _MCP_BOOTSTRAP,
                str(trusted),
                str(selected),
                *(str(path) for path in import_roots),
            ],
            cwd=root,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            max_output_bytes=_MCP_OUTPUT_LIMIT_BYTES,
        )
    except ProbeProcessError as exc:
        return _degraded_probe("deep_mcp", "MCP", exc.code)
    except Exception:
        return _degraded_probe("deep_mcp", "MCP", "probe_failed")

    try:
        responses = _parse_mcp_responses(result.stdout, result.stderr)
        initialize = responses[1]["result"]
        tools_result = responses[2]["result"]
        if not isinstance(initialize, dict) or not isinstance(tools_result, dict):
            raise ValueError
        server_info = initialize.get("serverInfo")
        tools = tools_result.get("tools")
        if not isinstance(server_info, dict) or server_info.get("name") != "graphite":
            raise ValueError
        if not isinstance(tools, list):
            raise ValueError
        tool_names = {
            tool.get("name")
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        }
        if not _REQUIRED_MCP_TOOLS.issubset(tool_names):
            raise ValueError
    except Exception:
        return _degraded_probe("deep_mcp", "MCP", "invalid_response")
    return DoctorCheck(
        "deep_mcp",
        "MCP",
        "ready",
        "The MCP server initializes and exposes the required tools.",
        {"server_name": "graphite", "tool_count": len(tool_names)},
    )


def _resolve_external_node(root: Path) -> Path | None:
    """Resolve executable Node from absolute PATH entries outside the selected project."""
    name = "node.exe" if os.name == "nt" else "node"
    try:
        selected = root.resolve()
    except OSError:
        return None
    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(raw_directory)
        if not raw_directory or not directory.is_absolute():
            continue
        try:
            candidate = (directory / name).resolve(strict=True)
            if not candidate.is_file() or (os.name != "nt" and not os.access(candidate, os.X_OK)):
                continue
            if not _paths_overlap(candidate.parent, selected):
                return candidate
        except OSError:
            continue
    return None


def probe_typescript(
    root: Path,
    *,
    timeout_seconds: float = 10.0,
    _node_resolver: Callable[[Path], Path | None] = _resolve_external_node,
    _runner: Callable[..., ProbeProcessResult] = run_bounded_process,
) -> DoctorCheck:
    """Detect TypeScript statically without executing project-controlled JavaScript."""
    node = _node_resolver(root)
    if node is None:
        return DoctorCheck(
            "deep_typescript",
            "TypeScript",
            "optional",
            "Node.js is unavailable; the TypeScript deep probe is optional.",
        )
    try:
        result = _runner(
            [str(node), "-e", _TYPESCRIPT_SCRIPT],
            cwd=root,
            timeout_seconds=timeout_seconds,
            check=False,
        )
    except ProbeProcessError as exc:
        return _degraded_probe("deep_typescript", "TypeScript", exc.code)
    except Exception:
        return _degraded_probe("deep_typescript", "TypeScript", "probe_failed")
    if result.returncode != 0:
        return _degraded_probe("deep_typescript", "TypeScript", "invalid_result")
    try:
        payload = _json_object(result.stdout)
    except Exception:
        return _degraded_probe("deep_typescript", "TypeScript", "invalid_result")
    if payload == {"missing_module": "typescript"}:
        return DoctorCheck(
            "deep_typescript",
            "TypeScript",
            "optional",
            "The TypeScript compiler module is unavailable.",
            remediation=_TYPESCRIPT_REMEDIATION,
        )
    if set(payload) != {"detected"} or payload["detected"] is not True:
        return _degraded_probe("deep_typescript", "TypeScript", "invalid_result")
    return DoctorCheck(
        "deep_typescript",
        "TypeScript",
        "optional",
        "TypeScript was detected but intentionally not executed because project dependencies "
        "are outside the doctor trust boundary.",
    )


def run_deep_probes(root: Path, *, cfg: Config, include_llm: bool) -> list[DoctorCheck]:
    """Return the deep capabilities implemented in this release."""
    del cfg, include_llm
    probes: tuple[tuple[Callable[[Path], DoctorCheck], Callable[[], DoctorCheck]], ...] = (
        (probe_core_pipeline, lambda: _blocked("unexpected", "probe_failed")),
        (probe_mcp, lambda: _degraded_probe("deep_mcp", "MCP", "probe_failed")),
        (
            probe_typescript,
            lambda: _degraded_probe("deep_typescript", "TypeScript", "probe_failed"),
        ),
    )
    checks: list[DoctorCheck] = []
    for probe, fallback in probes:
        try:
            checks.append(probe(root))
        except Exception:
            checks.append(fallback())
    return checks
