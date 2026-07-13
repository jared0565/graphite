"""Deadline-aware orchestration for isolated deep readiness probes."""
from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from functools import lru_cache
from importlib import machinery, metadata
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
import json
import pathlib
import runpy
import sys
from importlib import metadata
from importlib.machinery import PathFinder

trusted = pathlib.Path(sys.argv[1]).resolve(strict=True)
selected = pathlib.Path(sys.argv[2]).resolve(strict=True)
if not trusted.is_dir() or not selected.is_dir():
    raise SystemExit(70)
def overlaps_selected(path):
    try:
        path.relative_to(selected)
        return True
    except ValueError:
        pass
    try:
        selected.relative_to(path)
        return True
    except (ValueError, OSError):
        return False
manifest = json.loads(sys.stdin.buffer.readline())
if not isinstance(manifest, dict) or set(manifest) != {"distributions", "files", "packages"}:
    raise SystemExit(70)
raw_files = manifest["files"]
raw_packages = manifest["packages"]
raw_distributions = manifest["distributions"]
if not isinstance(raw_files, list) or not isinstance(raw_packages, dict) or not isinstance(raw_distributions, dict):
    raise SystemExit(70)
allowed_files = set()
for raw in raw_files:
    path = pathlib.Path(raw)
    if not isinstance(raw, str) or not path.is_absolute():
        raise SystemExit(70)
    path = path.resolve(strict=True)
    if not path.is_file() or overlaps_selected(path):
        raise SystemExit(70)
    allowed_files.add(path)
distributions = {}
for name, raw in raw_distributions.items():
    path = pathlib.Path(raw)
    if not isinstance(name, str) or not isinstance(raw, str) or not path.is_absolute():
        raise SystemExit(70)
    path = path.resolve(strict=True)
    if not path.is_dir() or not path.name.endswith(".dist-info") or overlaps_selected(path):
        raise SystemExit(70)
    distribution = metadata.PathDistribution(path)
    declared = distribution.metadata.get("Name")
    normalize = lambda value: "-".join(filter(None, __import__("re").split(r"[-_.]+", value.lower())))
    if not isinstance(declared, str) or normalize(declared) != name:
        raise SystemExit(70)
    distributions[name] = distribution
packages = {}
for name, raw_entry in raw_packages.items():
    if not isinstance(name, str) or not name.isidentifier():
        raise SystemExit(70)
    if not isinstance(raw_entry, dict) or set(raw_entry) != {"origin", "root", "search"}:
        raise SystemExit(70)
    entry = {}
    for field in ("search",):
        raw = raw_entry[field]
        path = pathlib.Path(raw)
        if not isinstance(raw, str) or not path.is_absolute():
            raise SystemExit(70)
        entry[field] = path.resolve(strict=True)
    raw_origin = raw_entry["origin"]
    if raw_origin is not None:
        origin = pathlib.Path(raw_origin)
        if not isinstance(raw_origin, str) or not origin.is_absolute():
            raise SystemExit(70)
        entry["origin"] = origin.resolve(strict=True)
    else:
        entry["origin"] = None
    raw_root = raw_entry["root"]
    if raw_root is not None:
        root = pathlib.Path(raw_root)
        if not isinstance(raw_root, str) or not root.is_absolute():
            raise SystemExit(70)
        entry["root"] = root.resolve(strict=True)
    else:
        entry["root"] = None
    if (
        (entry["origin"] is not None and entry["origin"] not in allowed_files)
        or not entry["search"].is_dir()
        or overlaps_selected(entry["search"])
        or (entry["root"] is not None and overlaps_selected(entry["root"]))
    ):
        raise SystemExit(70)
    packages[name] = entry

class GuardedDistributionFinder:
    @staticmethod
    def find_distributions(context=metadata.DistributionFinder.Context()):
        requested = context.name
        if requested is None:
            return iter(distributions.values())
        normalized = "-".join(filter(None, __import__("re").split(r"[-_.]+", requested.lower())))
        distribution = distributions.get(normalized)
        return iter(()) if distribution is None else iter((distribution,))

    @staticmethod
    def find_spec(fullname, path=None, target=None):
        del target
        top_level = fullname.partition(".")[0]
        entry = packages.get(top_level)
        if entry is None:
            return None
        search = [str(entry["search"])] if fullname == top_level else path
        if search is None:
            raise ModuleNotFoundError(fullname)
        spec = PathFinder.find_spec(fullname, search)
        if spec is None or spec.origin in ("built-in", "frozen"):
            raise ModuleNotFoundError(fullname)
        locations = spec.submodule_search_locations
        origin = None if spec.origin is None else pathlib.Path(spec.origin).resolve(strict=True)
        if origin is not None and origin not in allowed_files:
            raise ModuleNotFoundError(fullname)
        if fullname == top_level and origin != entry["origin"]:
            raise ModuleNotFoundError(fullname)
        if locations is not None:
            resolved = [pathlib.Path(item).resolve(strict=True) for item in locations]
            if len(resolved) != 1:
                raise ModuleNotFoundError(fullname)
            package_root = entry["root"]
            if package_root is None:
                raise ModuleNotFoundError(fullname)
            try:
                resolved[0].relative_to(package_root)
            except ValueError:
                raise ModuleNotFoundError(fullname) from None
        elif origin is None:
            raise ModuleNotFoundError(fullname)
        return spec

stdlib_path = list(sys.path)
sys.path[:] = [str(trusted), *stdlib_path]
sys.meta_path.insert(0, GuardedDistributionFinder)
expected_graphite_mcp = (trusted / "graphite" / "mcp.py").resolve(strict=True)
if not expected_graphite_mcp.is_file():
    raise SystemExit(70)
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


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _marker_value(name: str) -> str:
    values = {
        "platform_python_implementation": platform.python_implementation(),
        "platform_system": platform.system(),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "sys_platform": sys.platform,
    }
    if name not in values:
        raise ValueError
    return values[name]


def _requirement_applies(raw_requirement: str) -> bool:
    _, separator, raw_marker = raw_requirement.partition(";")
    if not separator:
        return True
    marker = raw_marker.strip()
    if re.search(r"\bextra\b", marker):
        return False
    while marker.startswith("(") and marker.endswith(")"):
        marker = marker[1:-1].strip()
    match = re.fullmatch(
        r"(python_version|sys_platform|platform_system|platform_python_implementation)"
        r"\s*(==|!=|<=|>=|<|>)\s*(['\"])([^'\"]+)\3",
        marker,
    )
    if match is None:
        raise ValueError
    actual = _marker_value(match.group(1))
    expected = match.group(4)
    if match.group(1) == "python_version":
        actual_value: object = tuple(int(part) for part in actual.split("."))
        expected_value: object = tuple(int(part) for part in expected.split("."))
    else:
        actual_value = actual
        expected_value = expected
    operator = match.group(2)
    comparisons = {
        "==": actual_value == expected_value,
        "!=": actual_value != expected_value,
        "<": actual_value < expected_value,
        "<=": actual_value <= expected_value,
        ">": actual_value > expected_value,
        ">=": actual_value >= expected_value,
    }
    return comparisons[operator]


def _mcp_distribution_closure() -> dict[str, metadata.Distribution]:
    pending = ["mcp", "networkx"]
    distributions: dict[str, metadata.Distribution] = {}
    while pending:
        requested = pending.pop()
        normalized = _normalized_distribution_name(requested)
        if normalized in distributions:
            continue
        distribution = metadata.distribution(requested)
        declared_name = distribution.metadata.get("Name")
        if not isinstance(declared_name, str) or _normalized_distribution_name(declared_name) != normalized:
            raise ValueError
        distributions[normalized] = distribution
        for requirement in distribution.requires or ():
            if not _requirement_applies(requirement):
                continue
            match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
            if match is None:
                raise ValueError
            pending.append(match.group(0))
    return distributions


@lru_cache(maxsize=1)
def _mcp_import_inventory() -> tuple[
    dict[str, str],
    dict[str, frozenset[Path]],
    dict[str, dict[str, Path]],
    dict[str, frozenset[str]],
    frozenset[Path],
]:
    """Cache immutable installed-distribution records, never ambient import resolution."""
    distributions = _mcp_distribution_closure()
    distribution_paths: dict[str, str] = {}
    for name, distribution in distributions.items():
        raw_path = getattr(distribution, "_path", None)
        if raw_path is None:
            raise ValueError
        path = Path(raw_path).resolve(strict=True)
        if not path.is_dir() or not path.name.endswith(".dist-info"):
            raise ValueError
        distribution_paths[name] = str(path)
    suffixes = tuple(machinery.all_suffixes())
    files_by_distribution: dict[str, set[Path]] = {}
    top_directories: dict[str, dict[str, Path]] = {}
    ownership: dict[str, set[str]] = {}
    all_files: set[Path] = set()
    for name, distribution in distributions.items():
        declared_files: set[Path] = set()
        declared_directories: dict[str, Path] = {}
        if distribution.files is None:
            raise ValueError
        for declared in distribution.files:
            first = str(declared).replace("\\", "/").partition("/")[0]
            top_level = first.partition(".")[0] if first.endswith(suffixes) else first
            if top_level.isidentifier() and first == top_level:
                try:
                    top_path = Path(distribution.locate_file(first)).resolve(strict=True)
                except OSError:
                    pass
                else:
                    if top_path.is_dir():
                        declared_directories[top_level] = top_path
            if not str(declared).endswith(suffixes):
                continue
            try:
                candidate = Path(distribution.locate_file(declared)).resolve(strict=True)
            except OSError:
                continue
            if not candidate.is_file():
                continue
            declared_files.add(candidate)
        if not declared_files:
            raise ValueError
        files_by_distribution[name] = declared_files
        top_directories[name] = declared_directories
        all_files.update(declared_files)
        raw_top_level = distribution.read_text("top_level.txt") or ""
        top_levels: set[str] = set()
        for raw in raw_top_level.splitlines():
            normalized = raw.strip().replace("\\", "/")
            if normalized:
                top_levels.add(normalized.partition("/")[0])
                top_levels.add(normalized.rpartition("/")[2])
        for declared in distribution.files:
            first = str(declared).replace("\\", "/").partition("/")[0]
            if first.endswith(suffixes):
                first = first.partition(".")[0]
            top_levels.add(first)
        for top_level in top_levels:
            if top_level.isidentifier() and top_level != "__pycache__":
                ownership.setdefault(top_level, set()).add(name)

    return (
        distribution_paths,
        {name: frozenset(files) for name, files in files_by_distribution.items()},
        top_directories,
        {name: frozenset(owners) for name, owners in ownership.items()},
        frozenset(all_files),
    )


def _mcp_import_manifest(selected_root: Path) -> dict[str, object]:
    """Describe only import files declared by the verified MCP dependency closure."""
    selected = selected_root.resolve(strict=True)
    if not selected.is_dir():
        raise OSError
    (
        distribution_paths,
        files_by_distribution,
        top_directories,
        ownership,
        all_files,
    ) = _mcp_import_inventory()
    if any(_path_is_within(path, selected) for path in all_files):
        raise ValueError
    if any(_paths_overlap(Path(path), selected) for path in distribution_paths.values()):
        raise ValueError

    packages: dict[str, dict[str, str | None]] = {}
    for top_level, raw_owners in ownership.items():
        if len(raw_owners) != 1:
            raise ValueError
        owner = next(iter(raw_owners))
        spec = machinery.PathFinder.find_spec(top_level, sys.path)
        if spec is None or spec.origin in {"built-in", "frozen"}:
            continue
        raw_locations = spec.submodule_search_locations
        root: Path | None = None
        origin: Path | None = None
        if raw_locations is not None:
            locations = [Path(raw).resolve(strict=True) for raw in raw_locations]
            if len(locations) != 1 or not locations[0].is_dir():
                raise ValueError
            root = locations[0]
            search = root.parent
            if spec.origin is None:
                if top_directories[owner].get(top_level) != root:
                    raise ValueError
        else:
            if spec.origin is None:
                raise ValueError
            origin = Path(spec.origin).resolve(strict=True)
            if origin not in files_by_distribution[owner]:
                raise ValueError
            search = origin.parent
        if spec.origin is not None:
            origin = Path(spec.origin).resolve(strict=True)
            if origin not in files_by_distribution[owner]:
                raise ValueError
        if _paths_overlap(search, selected) or (root is not None and _paths_overlap(root, selected)):
            raise ValueError
        packages[top_level] = {
            "origin": str(origin) if origin is not None else None,
            "root": str(root) if root is not None else None,
            "search": str(search),
        }
    for required in ("mcp", "networkx"):
        if required not in packages:
            raise ValueError
    return {
        "distributions": distribution_paths,
        "files": [str(path) for path in sorted(all_files, key=lambda item: os.path.normcase(str(item)))],
        "packages": packages,
    }


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


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError
    return parsed


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
            parse_float=_parse_finite_json_float,
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
    timeout_seconds: float = 20.0,
    _runner: Callable[..., ProbeProcessResult] = run_bounded_process,
    _manifest_builder: Callable[[Path], dict[str, object]] = _mcp_import_manifest,
) -> DoctorCheck:
    """Initialize the MCP server and inspect its read-only tool inventory."""
    started = time.monotonic()
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        return _degraded_probe("deep_mcp", "MCP", "invalid_timeout")
    deadline = started + timeout_seconds
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
    protocol_input = b"".join(
        json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
        for request in requests
    )
    try:
        trusted = Path(__file__).resolve(strict=True).parent.parent
        selected = root.resolve(strict=True)
        manifest = _manifest_builder(selected)
        stdin = json.dumps(manifest, separators=(",", ":")).encode("utf-8") + b"\n" + protocol_input
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeProcessError("timeout")
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
            ],
            cwd=root,
            stdin=stdin,
            timeout_seconds=remaining,
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
