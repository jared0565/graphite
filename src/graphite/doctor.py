"""Typed, bounded system-readiness diagnostics."""
from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Callable, Iterable, Literal, Mapping

from .config import Config, default_projects_root
from .daemon import read_daemon_status
from .daemon_health import HealthOptions, evaluate_daemon_health
from .freshness import FreshnessLimitError, check_graph_freshness
from .git import GitError, GitRunner
from .hookinstall import DEFAULT_HOOKS_DIRNAME, hook_shim_present, hooks_dir, managed_hook_paths
from .init import gitignored_managed_paths, managed_doc_paths
from .hookshim import TRIGGERS
from .llm import canonical_provider_name
from .validation import validate_graph_bundle

DoctorStatus = Literal["ready", "optional", "degraded", "blocked"]
STATUSES: tuple[DoctorStatus, ...] = ("ready", "optional", "degraded", "blocked")
_RANK = {name: rank for rank, name in enumerate(STATUSES)}
_ARTIFACT_LIMIT = 128 * 1024 * 1024
_TEXT_LIMIT = 500
_MANIFEST_LIMIT = 16 * 1024 * 1024
_FILE_LIMIT = 10_000
_PROCESS_CLEANUP_SECONDS = 0.2
_DAEMON_ISSUES_EXCLUDED_FROM_GLOBAL = {
    "project_failing",
    "project_pending_initial_build",
    "project_not_built_recently",
    "project_nested_repo_unsupervised",
    "daemon_process_check_unavailable",
}


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError("invalid doctor check details")
    if isinstance(value, MappingABC):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("invalid doctor check details")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError("invalid doctor check details")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, MappingABC):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class DoctorCheck:
    code: str
    label: str
    status: DoctorStatus
    summary: str
    details: Mapping[str, Any] = field(default_factory=dict)
    remediation: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in _RANK:
            raise ValueError("invalid doctor check status")
        if not isinstance(self.remediation, tuple) or not all(isinstance(item, str) for item in self.remediation):
            raise ValueError("invalid doctor remediation")
        frozen = _freeze_json(self.details)
        if not isinstance(frozen, MappingABC):
            raise ValueError("invalid doctor check details")
        object.__setattr__(self, "details", frozen)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "label": self.label, "status": self.status, "summary": self.summary, "details": _thaw_json(self.details), "remediation": list(self.remediation)}


def build_report(root: Path, checks: Iterable[DoctorCheck], deep: bool, llm_included: bool) -> dict[str, Any]:
    ordered = sorted(checks, key=lambda check: check.code)
    if any(check.status not in _RANK for check in ordered):
        raise ValueError("invalid doctor check status")
    status = max((check.status for check in ordered), key=_RANK.get, default="ready")
    return {"schema_version": 1, "root": root.resolve().name, "deep": bool(deep), "llm_included": bool(llm_included), "status": status, "exit_code": 1 if status == "blocked" else 0, "checks": [check.to_dict() for check in ordered]}


def format_doctor_text(report: Mapping[str, Any]) -> str:
    lines = [f"[graphite] doctor: {report.get('status', 'ready')}"]
    for item in report.get("checks", []):
        lines.append(f"  [{item['status']}] {item['label']}: {item['summary']}")
        for remediation in item.get("remediation", []):
            lines.append(f"    - {remediation}")
    return "\n".join(lines) + "\n"


def check_python() -> DoctorCheck:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ready = sys.version_info >= (3, 11)
    return DoctorCheck("python", "Python", "ready" if ready else "blocked", f"Python {version} is {'supported' if ready else 'unsupported'}.", {"version": version}, (() if ready else ("Use Python 3.11 or newer.",)))


def check_git(root: Path) -> DoctorCheck:
    try:
        result = GitRunner(root).run(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], timeout_seconds=10.0, max_stdout_bytes=16 * 1024 * 1024)
        if result.returncode != 0:
            raise GitError("Git command failed")
        records = _validated_git_records(root.resolve(), result.stdout)
        count = len(records)
        return DoctorCheck("git", "Git", "ready", "Git repository inventory is readable.", {"record_count": count})
    except (GitError, OSError):
        return DoctorCheck("git", "Git", "blocked", "Git repository inventory is unavailable.", remediation=("Verify Git 2.38+ and repository access.",))


def _validated_git_records(root: Path, output: bytes) -> list[str]:
    try:
        decoded = output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitError("Git returned malformed output") from exc
    if not decoded:
        return []
    if not decoded.endswith("\0"):
        raise GitError("Git returned malformed output")
    records = decoded[:-1].split("\0")
    if any(not record for record in records):
        raise GitError("Git returned malformed output")
    for record in records:
        posix = PurePosixPath(record)
        if record == "." or posix.as_posix() != record or posix.is_absolute() or ".." in posix.parts or PureWindowsPath(record).is_absolute():
            raise GitError("Git returned malformed output")
        try:
            (root / Path(record)).resolve().relative_to(root)
        except (OSError, ValueError) as exc:
            raise GitError("Git returned malformed output") from exc
    return records


def _hooks_path_configured(root: Path) -> bool:
    try:
        result = GitRunner(root).run(["config", "--get", "core.hooksPath"], timeout_seconds=5.0, max_stdout_bytes=4096)
    except (GitError, OSError):
        return False
    if result.returncode != 0:
        return False
    try:
        return bool(result.stdout.decode("utf-8").strip())
    except UnicodeDecodeError:
        return False


def check_hooks(root: Path) -> DoctorCheck:
    """Are graphite's git hooks not just installed, but actually enforced.

    Precedent from aramid's `probe_enforcement`: a repo with no `GRAPHITE.md`
    is deliberately not onboarded, so it is reported `optional` -- not a
    finding -- rather than nagging every repo that never opted in.
    """
    if not (root / "GRAPHITE.md").is_file():
        return DoctorCheck("hooks", "Hooks", "optional", "Repository is not onboarded onto graphite; hook enforcement does not apply.", {"onboarded": False})

    hdir = hooks_dir(root)
    installed = all(hook_shim_present(hdir / hook) for hook in TRIGGERS)
    configured = _hooks_path_configured(root)
    custom_hooks_dir = hdir != root / DEFAULT_HOOKS_DIRNAME
    details = {"onboarded": True, "custom_hooks_dir": custom_hooks_dir, "hookspath_configured": configured, "trampolines_installed": installed}

    if configured and installed:
        return DoctorCheck("hooks", "Hooks", "ready", "Git hooks are installed and enforced.", details)
    if configured and not installed:
        return DoctorCheck("hooks", "Hooks", "degraded", "core.hooksPath is configured but graphite's hook trampolines are missing.", details, ("Run `graphite init` to (re)install the hook trampolines.",))
    if installed and not configured:
        return DoctorCheck("hooks", "Hooks", "degraded", "Graphite's hook trampolines are installed but core.hooksPath is not set, so Git will not run them.", details, ("Run `git config core.hooksPath` to point at the installed hooks directory, or reinstall.",))
    return DoctorCheck("hooks", "Hooks", "degraded", "Git hooks are not installed for this onboarded repository.", details, ("Run `graphite init` to install git hooks.",))


def _scoped_output(root: Path, cfg: Config) -> Path:
    return cfg.output_dir if cfg.output_dir.is_absolute() else root / cfg.output_dir


def _artifact_size(path: Path) -> int:
    return path.stat().st_size


def check_managed_docs(root: Path) -> DoctorCheck:
    """Graphite-managed files that exist on disk but differ from what Git has.

    Why this is a probe and not a comment (aramid interop round 24,
    2026-07-31): *a generated file left uncommitted is worse than a stale one,
    because the staleness is invisible from the machine that generated it.*
    `init` writes these files into the working tree; if nobody commits them the
    author sees a correct repo while every clone and every CI checkout gets the
    previous version. Measured live -- six graphite-managed files sat
    uncommitted in aramid's repo for a day, and graphite had no way to say so.

    Reported `degraded`, never `blocked`: the repo still works, and doctor
    inventing a hard failure that no gate would produce is its own kind of lie
    (aramid's `probe_deps` reasoning). A repo with no `GRAPHITE.md` is
    deliberately not onboarded and is not a finding, matching `check_hooks`.
    """
    code, label = "managed-docs", "Managed docs"
    if not (root / "GRAPHITE.md").is_file():
        return DoctorCheck(code, label, "optional", "Repository is not onboarded onto graphite; no managed files to track.", {"onboarded": False, "uncommitted": []})

    watched = {path.as_posix() for path in managed_doc_paths() if (root / path).is_file()}
    watched |= set(managed_hook_paths(root))
    if not watched:
        return DoctorCheck(code, label, "ready", "No graphite-managed files are present.", {"onboarded": True, "watched": [], "uncommitted": []})

    try:
        result = GitRunner(root).run(["status", "--porcelain", "-z", "--", *sorted(watched)], timeout_seconds=10.0, max_stdout_bytes=1024 * 1024)
        if result.returncode != 0:
            raise GitError("Git command failed")
        decoded = result.stdout.decode("utf-8")
    except (GitError, OSError, UnicodeDecodeError):
        return DoctorCheck(code, label, "optional", "Managed-file commit state could not be read from Git.", {"onboarded": True, "watched": sorted(watched), "uncommitted": []}, ("Verify Git 2.38+ and repository access.",))

    # `-z` records are `XY <path>`, NUL-terminated and never quoted -- which is
    # exactly why `-z` is used rather than parsing the quoted default format. A
    # rename adds a second, bare-path field; intersecting against `watched` is
    # what makes that harmless, since a field that is not a managed path simply
    # never matches.
    dirty = sorted({record[3:] for record in decoded.split("\0") if len(record) > 3} & watched)

    # `git status` omits ignored files ENTIRELY, so everything above is blind to
    # the worse failure: `init` writing a managed file into a path `.gitignore`
    # already denies. Measured live in `BytesAI Learning` (2026-07-31), whose
    # `.gitignore` denies `.claude/` -- the file carrying the graph-first hook
    # was reported fine while its five neighbours were listed as uncommitted.
    #
    # `--ignored=matching` on status does NOT fix this: it still collapses the
    # report to the ignored DIRECTORY (`.claude/`), which never matches the
    # watched path. And `check-ignore` alone is wrong in the other direction --
    # it answers "does a pattern match?", which is true but inert for an
    # already-tracked file (`demo-store2`'s `CLAUDE.md` is both).
    #
    # `ls-files --others --ignored` is the conjunction itself: `--others`
    # restricts to untracked, `--ignored` to ignored. Tracked-and-matched drops
    # out by git's own semantics rather than by a rule reimplemented here.
    # Same helper `init` repairs with, so the detector and the fix cannot drift
    # into disagreeing about what "unreachable" means.
    unreachable = list(gitignored_managed_paths(root, sorted(watched)))

    # Disjoint by construction -- an ignored file never appears in `status`.
    dirty = [path for path in dirty if path not in set(unreachable)]
    details = {"onboarded": True, "watched": sorted(watched), "uncommitted": dirty, "unreachable": unreachable}
    if not dirty and not unreachable:
        return DoctorCheck(code, label, "ready", "Every graphite-managed file matches the committed version.", details)

    # Reported apart from `uncommitted` because the remediation differs and the
    # committing one is actively wrong here: `git add` on an ignored path is a
    # no-op, so an operator who follows it sees nothing change and concludes
    # the probe is broken.
    summaries, steps = [], []
    if unreachable:
        summaries.append(f"{len(unreachable)} graphite-managed file(s) sit in gitignored paths and can never reach a clone")
        steps.append("For details.unreachable, allow the path in .gitignore (e.g. `!/.claude/settings.json`) and then commit it -- `git add` alone is a no-op while the rule stands.")
    if dirty:
        summaries.append(f"{len(dirty)} differ from the committed version; clones and CI get the committed one")
        steps.append("Commit the files listed in details.uncommitted -- a working tree that looks correct does not make them correct for anyone else.")
    return DoctorCheck(code, label, "degraded", "; ".join(summaries) + ".", details, tuple(steps))


def _read_json_bounded(path: Path, limit: int) -> Any:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("artifact too large")
    return json.loads(data.decode("utf-8"))


def check_graph(root: Path, cfg: Config) -> DoctorCheck:
    graph_path = _scoped_output(root, cfg) / "graph.json"
    if not graph_path.exists():
        return DoctorCheck("graph", "Graph", "degraded", "Graph artifact is missing.", remediation=("Run `graphite build .`.",))
    try:
        if _artifact_size(graph_path) > _ARTIFACT_LIMIT:
            return DoctorCheck("graph", "Graph", "blocked", "Graph artifact exceeds the 128 MiB diagnostic limit.", remediation=("Rebuild or reduce the graph artifact.",))
        bundle = _read_json_bounded(graph_path, _ARTIFACT_LIMIT)
        if not isinstance(bundle, dict):
            raise ValueError("invalid graph")
        validation = validate_graph_bundle(bundle)
        if not validation.get("ok"):
            return DoctorCheck("graph", "Graph", "blocked", "Graph artifact validation failed.", {"node_count": validation.get("node_count", 0), "edge_count": validation.get("edge_count", 0), "warning_count": validation.get("warning_count", 0), "stale": False}, ("Rebuild the graph artifact.",))
        configured_max = cfg.max_files
        max_files = _FILE_LIMIT + 1 if configured_max is None else min(configured_max, _FILE_LIMIT + 1)
        scoped = replace(
            cfg,
            output_dir=_scoped_output(root, cfg),
            max_files=max_files,
            max_file_size=min(cfg.max_file_size, Config().max_file_size),
        )
        freshness = check_graph_freshness(root, scoped, max_manifest_bytes=_MANIFEST_LIMIT)
        stale = bool(freshness.get("stale"))
        details = {"node_count": validation.get("node_count", 0), "edge_count": validation.get("edge_count", 0), "warning_count": validation.get("warning_count", 0), "stale": stale}
        return DoctorCheck("graph", "Graph", "degraded" if stale else "ready", "Graph artifact is stale." if stale else "Graph artifact is valid and fresh.", details, (("Run `graphite build .`.",) if stale else ()))
    except FreshnessLimitError:
        return DoctorCheck("graph", "Graph", "blocked", "Graph freshness limit exceeded.", remediation=("Reduce the project or rebuild with bounded inputs.",))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, AttributeError, KeyError):
        return DoctorCheck("graph", "Graph", "blocked", "Graph artifact is unreadable or malformed.", remediation=("Rebuild the graph artifact.",))


def _normalized_root(path: object) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    try:
        return os.path.normcase(str(Path(path).resolve())).casefold()
    except OSError:
        return None


def _category_contains(report: Mapping[str, Any], category: str, selected: str) -> bool:
    projects = report.get("projects", {})
    if not isinstance(projects, MappingABC):
        return False
    items = projects.get(category, [])
    return isinstance(items, list) and any(
        isinstance(item, MappingABC) and _normalized_root(item.get("root")) == selected
        for item in items
    )


def _global_issue_count(report: Mapping[str, Any], key: str) -> int:
    issues = report.get(key, [])
    if not isinstance(issues, list):
        return 1
    return sum(
        1
        for issue in issues
        if not isinstance(issue, MappingABC)
        or issue.get("code") not in _DAEMON_ISSUES_EXCLUDED_FROM_GLOBAL
    )


def check_daemon(root: Path, daemon_base: Path) -> DoctorCheck:
    try:
        report = evaluate_daemon_health(daemon_base, options=HealthOptions())
        status_found = report.get("daemon_status") is not None
        process = report.get("process", {})
        process_checked = isinstance(process, MappingABC) and bool(process.get("checked"))
        process_running = not process_checked or bool(process.get("running"))
        process_observation_available = not (
            process_checked and isinstance(process, MappingABC) and bool(process.get("error"))
        )
        selected = _normalized_root(str(root.resolve()))
        try:
            raw_status = read_daemon_status(daemon_base)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            raw_status = None
        raw_projects = raw_status.get("projects", []) if isinstance(raw_status, MappingABC) else []
        registered = any(
            isinstance(item, MappingABC) and _normalized_root(item.get("root")) == selected
            for item in raw_projects
        )
        failing = bool(selected and _category_contains(report, "failing", selected))
        pending = bool(selected and _category_contains(report, "pending", selected))
        stale = bool(selected and _category_contains(report, "not_built_recently", selected))
        global_error_count = _global_issue_count(report, "errors")
        global_warning_count = _global_issue_count(report, "warnings")
        details = {
            "status_found": status_found,
            "registered": registered,
            "failing": failing,
            "pending": pending,
            "stale": stale,
            "process_checked": process_checked,
            "process_running": process_running,
            "process_observation_available": process_observation_available,
            "startup_checked": bool(report.get("startup", {}).get("checked")),
            "global_error_count": global_error_count,
            "global_warning_count": global_warning_count,
        }
        if not status_found and any(item.get("code") == "status_missing" for item in report.get("errors", [])):
            return DoctorCheck("daemon", "Daemon", "optional", "Daemon status is not present; core operation is unaffected.", details)
        if global_error_count or global_warning_count:
            return DoctorCheck("daemon", "Daemon", "degraded", "Daemon runtime health needs attention.", details)
        if process_observation_available and not process_running:
            return DoctorCheck("daemon", "Daemon", "degraded", "Daemon process is not running.", details)
        if not registered:
            return DoctorCheck("daemon", "Daemon", "optional", "Selected project is not registered with the daemon.", details)
        degraded = failing or pending or stale
        return DoctorCheck("daemon", "Daemon", "degraded" if degraded else "ready", "Selected project daemon state needs attention." if degraded else "Selected project daemon state is healthy.", details)
    except Exception:
        return DoctorCheck("daemon", "Daemon", "degraded", "Daemon health could not be evaluated.")


def check_mcp() -> DoctorCheck:
    package = importlib.util.find_spec("mcp") is not None
    command = shutil.which("graphite-mcp") is not None
    details = {"python_package": package, "command": command}
    if package and command:
        return DoctorCheck("mcp", "MCP", "ready", "MCP integration is available.", details)
    return DoctorCheck("mcp", "MCP", "optional", "MCP integration is not fully activated.", details, ("Install the Graphite MCP extra and ensure `graphite-mcp` is on PATH if MCP is needed.",))


def check_typescript(root: Path, *, timeout_seconds: float = 5.0) -> DoctorCheck:
    executable = _resolve_node_executable(root)
    if executable is None:
        return DoctorCheck("typescript", "TypeScript", "optional", "Node.js is unavailable; TypeScript compiler resolution is optional.")
    script = "try{const p=require('typescript/package.json');process.stdout.write(JSON.stringify({ok:true,version:p.version}))}catch(e){process.stdout.write(JSON.stringify({ok:false,reason:'missing'}))}"
    try:
        returncode, stdout, timed_out = _run_node_probe(executable, root, script, timeout_seconds=timeout_seconds)
        if timed_out:
            return DoctorCheck("typescript", "TypeScript", "degraded", "TypeScript probe timed out.")
        if returncode != 0:
            return DoctorCheck("typescript", "TypeScript", "degraded", "TypeScript probe failed unexpectedly.")
        data = json.loads(stdout.decode("utf-8"))
        if data.get("ok") is True and isinstance(data.get("version"), str):
            return DoctorCheck("typescript", "TypeScript", "ready", "TypeScript compiler is available.", {"version": data["version"][:64]})
        if data.get("reason") == "missing":
            return DoctorCheck("typescript", "TypeScript", "optional", "TypeScript compiler module is unavailable.", remediation=("Add TypeScript to the selected repository if compiler resolution is needed.",))
        return DoctorCheck("typescript", "TypeScript", "degraded", "TypeScript probe returned an invalid response.")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, AttributeError):
        return DoctorCheck("typescript", "TypeScript", "degraded", "TypeScript probe returned an invalid response.")


def _resolve_node_executable(root: Path, *, platform_name: str | None = None) -> Path | None:
    selected_platform = os.name if platform_name is None else platform_name
    name = "node.exe" if selected_platform == "nt" else "node"
    resolved_root = root.resolve()
    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(raw_directory)
        if not raw_directory or not directory.is_absolute():
            continue
        try:
            candidate = (directory / name).resolve(strict=True)
            if not candidate.is_file():
                continue
            if selected_platform != "nt" and not os.access(candidate, os.X_OK):
                continue
            try:
                candidate.relative_to(resolved_root)
            except ValueError:
                return candidate
        except OSError:
            continue
    return None


def _node_environment() -> dict[str, str]:
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"} if os.name == "nt" else {"HOME", "TMPDIR", "LANG"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def _run_node_probe(executable: Path, root: Path, script: str, *, timeout_seconds: float) -> tuple[int, bytes, bool]:
    return _run_bounded_process(
        [str(executable), "-e", script],
        cwd=root,
        env=_node_environment(),
        timeout_seconds=timeout_seconds,
        max_stdout_bytes=_TEXT_LIMIT,
    )


def _close_pipe(process: subprocess.Popen[bytes]) -> bool:
    if process.stdout is None:
        return True
    try:
        process.stdout.close()
    except Exception:
        return False
    return True


def _cleanup_process(process: subprocess.Popen[bytes], reader: threading.Thread) -> bool:
    try:
        process.kill()
    except Exception:
        pass
    closed = _close_pipe(process)
    try:
        process.wait(timeout=_PROCESS_CLEANUP_SECONDS)
    except Exception:
        pass
    reader.join(timeout=_PROCESS_CLEANUP_SECONDS)
    return closed and not reader.is_alive()


def _run_bounded_process(command: list[str], *, cwd: Path, env: Mapping[str, str], timeout_seconds: float, max_stdout_bytes: int) -> tuple[int, bytes, bool]:
    process = subprocess.Popen(command, cwd=cwd, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=dict(env))
    if process.stdout is None:
        raise OSError("bounded process stdout unavailable")
    output: list[bytes] = []
    read_failed = threading.Event()

    def read_bounded() -> None:
        try:
            output.append(process.stdout.read(max_stdout_bytes + 1))
        except Exception:
            read_failed.set()

    reader = threading.Thread(target=read_bounded, daemon=True)
    reader.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except Exception:
        _cleanup_process(process, reader)
        return process.returncode or -1, b"", True
    reader.join(timeout=_PROCESS_CLEANUP_SECONDS)
    if reader.is_alive():
        _cleanup_process(process, reader)
        return returncode, b"", False
    data = output[0] if output else b""
    if read_failed.is_set() or len(data) > max_stdout_bytes or not _close_pipe(process):
        _cleanup_process(process, reader)
        return returncode, b"", False
    return returncode, data, False


def check_llm_config(cfg: Config) -> DoctorCheck:
    mode = cfg.llm_mode.strip().lower()
    normalized_provider = cfg.llm_provider.strip().lower().replace("_", "-")
    provider = canonical_provider_name(cfg.llm_provider)
    credential = bool(cfg.llm_api_key)
    details = {"mode": mode, "provider": provider, "credential_present": credential}
    if mode == "none":
        summary = "LLM enrichment is disabled. An ambient credential is unused; rotate or remove it." if credential else "LLM enrichment is disabled."
        return DoctorCheck("llm", "LLM", "optional", summary, details)
    invalid_mode = mode not in {"auto", "local", "cloud"}
    unsupported_provider = provider == "custom/unknown"
    missing_base_url = normalized_provider in {"openai-compatible", "compatible"} and not cfg.llm_base_url
    missing_cloud_credential = (
        mode == "cloud"
        and normalized_provider in {"openai", "openrouter", "groq"}
        and not credential
    )
    if invalid_mode or unsupported_provider or missing_base_url or missing_cloud_credential:
        return DoctorCheck(
            "llm",
            "LLM",
            "degraded",
            "LLM required configuration is incomplete or invalid.",
            details,
            ("Verify the provider, endpoint, model, and session credential configuration.",),
        )
    return DoctorCheck(
        "llm",
        "LLM",
        "optional",
        "LLM is configured but synthetic connectivity was not requested.",
        details,
        ("Run doctor with --deep --include-llm when network access is approved.",),
    )


def _incidents_check(root: Path) -> DoctorCheck:
    from .incident_ledger import fold_incidents, read_incident_entries, repo_ledger_dir

    entries, skipped = read_incident_entries(repo_ledger_dir(root))
    views = [v for v in fold_incidents(entries) if v.state != "resolved"]
    open_views = [v for v in views if v.state == "open"]
    top = [f"{v.fingerprint} {v.code} {v.subject} x{v.count}" for v in open_views[:10]]
    summary = f"{len(open_views)} open / {len(views) - len(open_views)} acked"
    if skipped:
        summary += f", {skipped} corrupt line(s)"
    status = "degraded" if open_views else "ready"
    return DoctorCheck(
        code="incidents",
        label="Incident ledger",
        status=status,
        summary=summary,
        details={"top_open": top},
        remediation=("graphite incidents list", "graphite incidents ack <fingerprint> -m NOTE")
        if open_views
        else (),
    )


def _fast_checks(root: Path, cfg: Config, daemon_base: Path | None) -> list[DoctorCheck]:
    checks: list[tuple[str, str, Callable[[], DoctorCheck]]] = [("python", "Python", check_python), ("git", "Git", lambda: check_git(root)), ("hooks", "Hooks", lambda: check_hooks(root)), ("managed-docs", "Managed docs", lambda: check_managed_docs(root)), ("graph", "Graph", lambda: check_graph(root, cfg)), ("daemon", "Daemon", lambda: check_daemon(root, daemon_base or root)), ("mcp", "MCP", check_mcp), ("typescript", "TypeScript", lambda: check_typescript(root)), ("llm", "LLM", lambda: check_llm_config(cfg)), ("incidents", "Incident ledger", lambda: _incidents_check(root))]
    results = []
    for code, label, check in checks:
        try:
            results.append(check())
        except Exception:
            results.append(DoctorCheck(code, label, "degraded", "The readiness check failed safely."))
    return results


def run_doctor(root: Path, cfg: Config, daemon_base: Path | None = None, deep: bool = False, include_llm: bool = False, deep_runner: Callable[[Path, Config, bool], Iterable[DoctorCheck]] | None = None) -> dict[str, Any]:
    selected = root.resolve()
    selected_daemon_base = (daemon_base or default_projects_root()).resolve()
    checks = _fast_checks(selected, cfg, selected_daemon_base)
    if deep:
        if deep_runner is None:
            from .doctor_probes import run_deep_probes
            deep_runner = run_deep_probes
        deep_checks = list(deep_runner(selected, cfg=cfg, include_llm=include_llm))
        disabled_llm = isinstance(cfg.llm_mode, str) and cfg.llm_mode.strip().lower() == "none"
        if (
            include_llm
            and not disabled_llm
            and any(check.code == "deep_llm" for check in deep_checks)
        ):
            checks = [check for check in checks if check.code != "llm"]
        checks.extend(deep_checks)
    return build_report(selected, checks, deep, bool(deep and include_llm))
