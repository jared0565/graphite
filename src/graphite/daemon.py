"""Multi-project Graphite daemon for keeping local graphs fresh."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Config
from .incident_ledger import record_incident, repo_ledger_dir
from .io import atomic_write_json
from .provider_observer import ProviderObservationSummary
from .watch import Snapshot, WatchChange, diff_snapshots, snapshot, wait_for_stable_snapshot

IGNORE_MARKER = ".graphite-ignore"

PROJECT_MARKERS: tuple[str, ...] = (
    ".git",
    "package.json",
    "pyproject.toml",
    "wrangler.toml",
    "Cargo.toml",
    "go.mod",
    "deno.json",
    "vite.config.ts",
    "next.config.js",
    "next.config.mjs",
)

MAX_DAEMON_STATUS_BYTES = 4 * 1024 * 1024
_PROVIDER_ENV_PREFIXES = (
    "GRAPHITE_LLM",
    "GRAPHITE_PROVIDER_",
    "GRAPHITE_ROUTE_",
    "ANTHROPIC_",
    "CLAUDE_",
    "CODEX_",
    "GROQ_",
    "LMSTUDIO_",
    "OLLAMA_",
    "OPENAI_",
    "OPENROUTER_",
    "VLLM_",
)


class DaemonStatusTooLargeError(OSError):
    """Raised when a daemon status snapshot exceeds the parsing limit."""


class DaemonStatusInvalidError(ValueError):
    """Raised when a bounded daemon status snapshot cannot be parsed safely."""


DISCOVERY_SKIP_DIRS: frozenset[str] = frozenset({
    ".cache",
    ".git",
    ".graphite-daemon",
    ".next",
    ".open-next",
    ".pytest_cache",
    ".venv",
    ".wrangler",
    "__pycache__",
    "_tools",
    "build",
    "coverage",
    "dist",
    "graph-out",
    "graphify-out",
    "node_modules",
    "out",
    "vendor",
    "venv",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tail(text: str, limit: int = 4000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def summarize_change(change: WatchChange, sample_limit: int = 20) -> dict[str, object]:
    return {
        "added_count": len(change.added),
        "changed_count": len(change.changed),
        "removed_count": len(change.removed),
        "sample": list(change.paths[:sample_limit]),
    }


@dataclass(frozen=True)
class DaemonOptions:
    """Operational limits for the multi-project daemon."""

    scan_interval_seconds: float = 10.0
    discover_interval_seconds: float = 60.0
    debounce_seconds: float = 1.0
    max_depth: int = 6
    max_projects: int = 128
    max_files_per_project: int | None = 10_000
    max_builds_per_cycle: int = 2
    build_timeout_seconds: float = 300.0
    build_now: bool = True
    once: bool = False
    max_cycles: int | None = None
    state_dir: Path | None = None
    log_max_bytes: int = 5_000_000

    def validate(self) -> None:
        if self.scan_interval_seconds <= 0:
            raise ValueError("daemon scan interval must be greater than zero")
        if self.discover_interval_seconds <= 0:
            raise ValueError("daemon discover interval must be greater than zero")
        if self.debounce_seconds < 0:
            raise ValueError("daemon debounce must be zero or greater")
        if self.max_depth < 0:
            raise ValueError("daemon max depth must be zero or greater")
        if self.max_projects <= 0:
            raise ValueError("daemon max projects must be greater than zero")
        if self.max_files_per_project is not None and self.max_files_per_project <= 0:
            raise ValueError("daemon max files per project must be greater than zero")
        if self.max_builds_per_cycle <= 0:
            raise ValueError("daemon max builds per cycle must be greater than zero")
        if self.build_timeout_seconds <= 0:
            raise ValueError("daemon build timeout must be greater than zero")
        if self.max_cycles is not None and self.max_cycles <= 0:
            raise ValueError("daemon max cycles must be greater than zero")
        if self.log_max_bytes <= 0:
            raise ValueError("daemon log max bytes must be greater than zero")


@dataclass(frozen=True)
class BuildResult:
    success: bool
    returncode: int | None
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


@dataclass
class ProjectRuntime:
    root: Path
    snapshot: Snapshot
    discovered_at: str = field(default_factory=utc_now)
    last_seen_at: str = field(default_factory=utc_now)
    last_build_started_at: str | None = None
    last_build_finished_at: str | None = None
    last_build_seconds: float | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    build_count: int = 0
    failure_count: int = 0
    needs_initial_build: bool = True
    last_change: dict[str, object] | None = None

    def to_status(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "file_count": len(self.snapshot),
            "discovered_at": self.discovered_at,
            "last_seen_at": self.last_seen_at,
            "last_build_started_at": self.last_build_started_at,
            "last_build_finished_at": self.last_build_finished_at,
            "last_build_seconds": self.last_build_seconds,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "build_count": self.build_count,
            "failure_count": self.failure_count,
            "needs_initial_build": self.needs_initial_build,
            "last_change": self.last_change,
        }


class DaemonLogger:
    def __init__(self, state_dir: Path, max_bytes: int) -> None:
        self.state_dir = state_dir
        self.max_bytes = max_bytes
        self.log_path = state_dir / "graphite-daemon.log"
        self._lock = threading.Lock()
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def event(self, event: str, **fields: object) -> None:
        with self._lock:
            self._rotate_if_needed()
            payload = {"ts": utc_now(), "event": event, **fields}
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def _rotate_if_needed(self) -> None:
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > self.max_bytes:
                rotated = self.log_path.with_suffix(".log.1")
                if rotated.exists():
                    rotated.unlink()
                self.log_path.rename(rotated)
        except OSError:
            return


def is_project_root(path: Path) -> bool:
    return any((path / marker).exists() for marker in PROJECT_MARKERS)


def discover_projects(base: Path, *, max_depth: int = 6, max_projects: int = 128) -> list[Path]:
    """Discover likely project roots under a base directory without entering heavy build folders."""
    base = base.resolve()
    if not base.exists():
        raise FileNotFoundError(base)
    if not base.is_dir():
        raise NotADirectoryError(base)

    projects: list[Path] = []
    for dirpath, dirnames, _ in os.walk(base):
        current = Path(dirpath)
        try:
            rel = current.relative_to(base)
        except ValueError:
            continue
        depth = 0 if rel == Path(".") else len(rel.parts)
        dirnames[:] = [
            d for d in dirnames
            if d not in DISCOVERY_SKIP_DIRS and not d.startswith(".")
        ]

        if depth > max_depth:
            dirnames[:] = []
            continue

        if (current / IGNORE_MARKER).exists():
            dirnames[:] = []
            continue

        if is_project_root(current):
            projects.append(current)
            if len(projects) >= max_projects:
                break
            # A project's graph covers its whole subtree, so do not descend
            # into it looking for more projects. Without this, every monorepo
            # workspace (package.json / wrangler.toml markers) became its own
            # supervised project with a duplicate — and cross-workspace-blind —
            # graph next to the root one.
            dirnames[:] = []
            continue

        if depth >= max_depth:
            dirnames[:] = []

    return sorted(projects, key=lambda p: str(p).lower())


def daemon_config_for_project(cfg: Config, root: Path, options: DaemonOptions) -> Config:
    data = cfg.canonical_graph().to_dict()
    if not Path(data["output_dir"]).is_absolute():
        data["output_dir"] = root / data["output_dir"]
    if not Path(data["cache_dir"]).is_absolute():
        data["cache_dir"] = root / data["cache_dir"]
    if options.max_files_per_project is not None:
        current_max = data.get("max_files")
        data["max_files"] = min(current_max, options.max_files_per_project) if current_max else options.max_files_per_project
    return Config(**data)


def _build_command(cfg: Config, root: Path) -> tuple[list[str], dict[str, str]]:
    cfg = cfg.canonical_graph()
    cmd = [
        sys.executable,
        "-B",
        "-m",
        "graphite",
        "--output-dir",
        str(cfg.output_dir),
        "--cache-dir",
        str(cfg.cache_dir),
        "--workers",
        str(cfg.workers),
        "--typescript-resolver",
        cfg.typescript_resolver,
        "--typescript-resolver-timeout",
        str(cfg.typescript_resolver_timeout_seconds),
    ]
    if not cfg.typescript_symbol_references:
        cmd.append("--no-typescript-symbol-references")
    cmd.extend(["--llm", "none", "build", str(root)])

    env: dict[str, str] = {}
    for key in os.environ:
        if key.upper().startswith(_PROVIDER_ENV_PREFIXES):
            continue
        env[key] = os.environ[key]
    src_root = str(Path(__file__).resolve().parents[1])
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src_root if not existing_pythonpath else f"{src_root}{os.pathsep}{existing_pythonpath}"
    return cmd, env


def run_graphite_build(root: Path, cfg: Config, timeout_seconds: float) -> BuildResult:
    """Build one project in an isolated child process with bounded runtime."""
    start = time.time()
    cmd, env = _build_command(cfg, root)
    try:
        result = subprocess.run(
            cmd,
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            env=env,
        )
        return BuildResult(
            success=result.returncode == 0,
            returncode=result.returncode,
            duration_seconds=time.time() - start,
            stdout=_tail(result.stdout),
            stderr=_tail(result.stderr),
        )
    except subprocess.TimeoutExpired as exc:
        return BuildResult(
            success=False,
            returncode=None,
            duration_seconds=time.time() - start,
            stdout=_tail(exc.stdout or ""),
            stderr=_tail(exc.stderr or ""),
            error=f"build timed out after {timeout_seconds:.1f}s",
        )
    except OSError as exc:
        return BuildResult(
            success=False,
            returncode=None,
            duration_seconds=time.time() - start,
            error=str(exc),
        )


BuildProject = Callable[[Path, Config, float], BuildResult]
SleepFn = Callable[[float], None]
ProviderObservationCycle = Callable[[], ProviderObservationSummary]


class _ProviderObservationWorker:
    """Run bounded provider work outside the graph scheduling thread."""

    def __init__(
        self,
        cycle: ProviderObservationCycle,
        logger: DaemonLogger,
        interval_seconds: float,
    ) -> None:
        self._cycle = cycle
        self._logger = logger
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status: dict[str, object] = {
            "status": "observing",
            "attempted": 0,
            "deferred": 0,
            "succeeded": 0,
            "failed": 0,
            "state_counts": {},
            "reason_counts": {},
        }
        self._thread = threading.Thread(
            target=self._run,
            name="graphite-provider-observer",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                **self._status,
                "state_counts": dict(self._status["state_counts"]),
                "reason_counts": dict(self._status["reason_counts"]),
            }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self._cycle()
                if not isinstance(result, ProviderObservationSummary):
                    raise TypeError("provider_observation_result_invalid")
                status = result.to_status()
                status["status"] = "degraded" if result.failed else "ok"
            except Exception:
                status = {
                    "status": "degraded",
                    "attempted": 1,
                    "deferred": 0,
                    "succeeded": 0,
                    "failed": 1,
                    "state_counts": {},
                    "reason_counts": {"observer_cycle_failed": 1},
                }
                record_incident(
                    self._logger.state_dir,
                    klass="daemon",
                    code="provider_probe_failed",
                    subject="daemon",
                    detail="observer_cycle_failed",
                )
            with self._lock:
                self._status = status
            self._logger.event("provider_observation_cycle", **status)
            if self._stop.wait(self._interval_seconds):
                return


def _write_status(
    state_dir: Path,
    base: Path,
    states: dict[Path, ProjectRuntime],
    options: DaemonOptions,
    cycle: int,
    provider_lifecycle: dict[str, object] | None = None,
) -> dict[str, object]:
    projects = [state.to_status() for _, state in sorted(states.items(), key=lambda item: str(item[0]).lower())]
    healthy = sum(1 for state in states.values() if state.last_error is None and not state.needs_initial_build)
    failing = sum(1 for state in states.values() if state.last_error is not None)
    pending = sum(1 for state in states.values() if state.needs_initial_build)
    payload: dict[str, object] = {
        "status": "ok" if failing == 0 else "degraded",
        "updated_at": utc_now(),
        "base_path": str(base),
        "cycle": cycle,
        "project_count": len(projects),
        "healthy_projects": healthy,
        "failing_projects": failing,
        "pending_projects": pending,
        "limits": {
            "scan_interval_seconds": options.scan_interval_seconds,
            "discover_interval_seconds": options.discover_interval_seconds,
            "debounce_seconds": options.debounce_seconds,
            "max_depth": options.max_depth,
            "max_projects": options.max_projects,
            "max_files_per_project": options.max_files_per_project,
            "max_builds_per_cycle": options.max_builds_per_cycle,
            "build_timeout_seconds": options.build_timeout_seconds,
        },
        "projects": projects,
    }
    if provider_lifecycle is not None:
        payload["provider_lifecycle"] = provider_lifecycle
    atomic_write_json(state_dir / "status.json", payload, indent=2)
    return payload


def read_daemon_status(base: Path, state_dir: Path | None = None) -> dict[str, object]:
    base = base.resolve()
    path = (state_dir or (base / ".graphite-daemon")) / "status.json"
    with open(path, "rb") as f:
        payload = f.read(MAX_DAEMON_STATUS_BYTES + 1)
    if len(payload) > MAX_DAEMON_STATUS_BYTES:
        raise DaemonStatusTooLargeError("daemon status exceeds size limit")
    decoded = payload.decode("utf-8", errors="strict")
    try:
        return json.loads(decoded)
    except json.JSONDecodeError:
        raise
    except ValueError:
        raise DaemonStatusInvalidError("daemon status is invalid") from None


def _record_build_result(state: ProjectRuntime, change: WatchChange, result: BuildResult) -> None:
    state.last_build_finished_at = utc_now()
    state.last_build_seconds = round(result.duration_seconds, 3)
    state.build_count += 1
    state.last_change = summarize_change(change)
    if result.success:
        state.last_success_at = state.last_build_finished_at
        state.last_error = None
        state.needs_initial_build = False
    else:
        state.failure_count += 1
        state.last_error = result.error or result.stderr or result.stdout or f"build failed with code {result.returncode}"
        record_incident(
            repo_ledger_dir(state.root),
            klass="daemon",
            code="daemon_build_failed",
            subject=str(state.root),
            detail=state.last_error or "build failed",
        )


def run_daemon(
    base: Path,
    cfg: Config,
    options: DaemonOptions,
    *,
    build_project: BuildProject = run_graphite_build,
    sleep: SleepFn = time.sleep,
    provider_observation_cycle: ProviderObservationCycle | None = None,
) -> dict[str, object]:
    """Run the multi-project daemon loop and return the last written status."""
    options.validate()
    base = base.resolve()
    if not base.exists():
        raise FileNotFoundError(base)
    state_dir = (options.state_dir or (base / ".graphite-daemon")).resolve()
    logger = DaemonLogger(state_dir, options.log_max_bytes)
    logger.event("daemon_start", base_path=str(base), once=options.once)
    provider_worker = None
    if provider_observation_cycle is not None:
        provider_options = cfg.provider_observer_options()
        provider_worker = _ProviderObservationWorker(
            provider_observation_cycle,
            logger,
            provider_options.interval_seconds,
        )
        provider_worker.start()

    states: dict[Path, ProjectRuntime] = {}
    last_discovery = 0.0
    last_status: dict[str, object] = {}
    cycle = 0

    while True:
        now = time.time()
        if cycle == 0 or now - last_discovery >= options.discover_interval_seconds:
            projects = discover_projects(base, max_depth=options.max_depth, max_projects=options.max_projects)
            discovered = set(projects)
            for project in projects:
                if project not in states:
                    try:
                        project_cfg = daemon_config_for_project(cfg, project, options)
                        snap = snapshot(project, project_cfg)
                    except Exception as exc:
                        logger.event("project_snapshot_failed", project=str(project), error=str(exc))
                        continue
                    states[project] = ProjectRuntime(
                        root=project,
                        snapshot=snap,
                        needs_initial_build=options.build_now,
                    )
                    logger.event("project_discovered", project=str(project), file_count=len(snap))
                else:
                    states[project].last_seen_at = utc_now()
            for project in list(states):
                if project not in discovered:
                    logger.event("project_removed", project=str(project))
                    del states[project]
            last_discovery = now

        builds_this_cycle = 0
        for project, state in sorted(states.items(), key=lambda item: str(item[0]).lower()):
            if builds_this_cycle >= options.max_builds_per_cycle:
                break
            project_cfg = daemon_config_for_project(cfg, project, options)
            try:
                if state.needs_initial_build:
                    change = WatchChange(added=tuple(sorted(state.snapshot)))
                else:
                    current = snapshot(project, project_cfg)
                    change = diff_snapshots(state.snapshot, current)
                    if not change.has_changes:
                        continue
                    stable = wait_for_stable_snapshot(project, project_cfg, current, options.debounce_seconds)
                    change = diff_snapshots(state.snapshot, stable)
                    if not change.has_changes:
                        state.snapshot = stable
                        continue
                    current = stable
            except Exception as exc:
                state.last_error = str(exc)
                state.failure_count += 1
                logger.event("project_scan_failed", project=str(project), error=str(exc))
                continue

            was_initial_build = state.needs_initial_build
            state.last_build_started_at = utc_now()
            logger.event("build_started", project=str(project), change=summarize_change(change))
            result = build_project(project, project_cfg, options.build_timeout_seconds)
            _record_build_result(state, change, result)
            if result.success:
                if not was_initial_build:
                    state.snapshot = current
                logger.event("build_succeeded", project=str(project), seconds=state.last_build_seconds)
            else:
                logger.event("build_failed", project=str(project), seconds=state.last_build_seconds, error=state.last_error)
            builds_this_cycle += 1

        last_status = _write_status(
            state_dir,
            base,
            states,
            options,
            cycle,
            None if provider_worker is None else provider_worker.snapshot(),
        )
        if options.once:
            if provider_worker is not None:
                provider_worker.stop()
            logger.event("daemon_stop", reason="once", cycle=cycle)
            return last_status
        cycle += 1
        if options.max_cycles is not None and cycle >= options.max_cycles:
            if provider_worker is not None:
                provider_worker.stop()
            logger.event("daemon_stop", reason="max_cycles", cycle=cycle)
            return last_status
        sleep(options.scan_interval_seconds)
