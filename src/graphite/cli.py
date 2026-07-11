"""Graphite command-line interface."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from .analyze import analyze
from .cache import Cache
from .bootstrap import bootstrap_project
from .cluster import detect_communities
from .config import Config, default_projects_root
from .context import build_context, format_context_markdown
from .daemon import DaemonOptions, read_daemon_status, run_daemon
from .daemon_health import HealthOptions, evaluate_daemon_health, format_health_text
from .export.html import to_html as export_html
from .export.json import build_bundle, to_json as export_json
from .export.md import to_markdown as export_md
from .extract.ast import extract_all
from .graph import build_graph, graph_from_json, graph_to_json
from .ingest import collect_files
from .init import init_project, platform_choices, resolve_platform_selection
from .io import atomic_write_json
from .llm import enrich_report
from .query import _find_node, annotate_communities, query
from .replacement_audit import audit_replacement, format_replacement_audit
from .review import (
    ReviewError,
    build_review_packet,
    discover_git_changes,
    format_review_markdown,
    normalize_explicit_changes,
)
from .validation import assert_valid_graph_bundle, validate_graph_bundle
from .watch import WatchChange, WatchOptions, watch_loop
from .windows_task import (
    DEFAULT_TASK_NAME,
    create_daemon_task,
    daemon_task_command,
    delete_daemon_task,
    query_daemon_task,
)
from .windows_startup import install_startup_launcher, startup_status, uninstall_startup_launcher

_TEST_SUFFIXES = (
    ".test.ts",
    ".spec.ts",
    ".test.tsx",
    ".spec.tsx",
    ".test.js",
    ".spec.js",
    ".test.py",
    ".spec.py",
)


def _config_from_args(args: argparse.Namespace) -> Config:
    """Build config from defaults + CLI args + env."""
    base = Config.from_env()
    kwargs: dict[str, Any] = {**base.to_dict()}
    if getattr(args, "output_dir", None) is not None:
        kwargs["output_dir"] = Path(args.output_dir)
    if getattr(args, "cache_dir", None) is not None:
        kwargs["cache_dir"] = Path(args.cache_dir)
    if getattr(args, "workers", None) is not None:
        kwargs["workers"] = int(args.workers)
    if getattr(args, "verbose", False):
        kwargs["verbose"] = True
    if getattr(args, "no_typescript_symbol_references", False):
        kwargs["typescript_symbol_references"] = False
    for arg_name, cfg_name in (
        ("typescript_resolver", "typescript_resolver"),
        ("typescript_resolver_timeout", "typescript_resolver_timeout_seconds"),
        ("llm", "llm_mode"),
        ("llm_provider", "llm_provider"),
        ("llm_model", "llm_model"),
        ("llm_base_url", "llm_base_url"),
        ("llm_api_key", "llm_api_key"),
        ("llm_timeout", "llm_timeout_seconds"),
        ("llm_max_input_chars", "llm_max_input_chars"),
    ):
        value = getattr(args, arg_name, None)
        if value is not None:
            kwargs[cfg_name] = value
    return Config(**kwargs)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_json(path, data, indent=2)


def _scan(args: argparse.Namespace, cfg: Config) -> tuple[dict[str, Any], list[Any]]:
    root = Path(args.path).resolve()
    if not root.exists():
        print(f"[graphite] path not found: {root}", file=sys.stderr)
        raise SystemExit(1)

    start = time.time()
    entries = collect_files(root, cfg)
    manifest = {
        "root": root.name,
        "file_count": len(entries),
        "files": [
            {"rel_path": e.rel_path, "language": e.language, "size": e.size, "hash": e.content_hash}
            for e in entries
        ],
    }
    if cfg.verbose:
        print(f"[graphite] scanned {len(entries)} files in {time.time() - start:.2f}s")
    return manifest, entries


def _build(
    args: argparse.Namespace,
    cfg: Config,
    manifest: dict[str, Any],
    entries: list[Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    cache = Cache(cfg.cache_dir, cfg.cache_version)
    start = time.time()
    extraction = extract_all(entries, cfg, cache)
    if cfg.verbose:
        print(
            f"[graphite] extracted {len(extraction.nodes)} nodes / {len(extraction.edges)} edges "
            f"in {time.time() - start:.2f}s"
        )

    start = time.time()
    g = build_graph(extraction.nodes, extraction.edges)
    graph_data = graph_to_json(g)
    if cfg.verbose:
        print(f"[graphite] built graph in {time.time() - start:.2f}s")

    clusters = detect_communities(g, seed=cfg.seed)
    if cfg.verbose:
        print(f"[graphite] detected {clusters['count']} communities")

    annotate_communities(g, clusters["node_to_community"])
    graph_data = graph_to_json(g)
    analysis = analyze(g)
    llm = enrich_report(graph_data, clusters, analysis, cfg)
    analysis["llm"] = llm
    if cfg.verbose and llm.get("enabled"):
        status = llm.get("status", "unknown")
        provider = llm.get("provider", cfg.llm_provider)
        print(f"[graphite] llm enrichment {status} via {provider}")

    return graph_data, clusters, analysis


def _report(
    cfg: Config,
    manifest: dict[str, Any],
    graph_data: dict[str, Any],
    clusters: dict[str, Any],
    analysis: dict[str, Any],
) -> None:
    out = cfg.output_dir
    out.mkdir(parents=True, exist_ok=True)

    llm = analysis.get("llm", {})
    public_manifest = {
        **manifest,
        "llm_mode": cfg.llm_mode,
        "llm_provider": llm.get("provider") or cfg.llm_provider,
        "llm_model": llm.get("model") or cfg.llm_model,
        "llm_status": llm.get("status", "disabled" if not llm.get("enabled") else "unknown"),
        "llm_effective_mode": llm.get("effective_mode"),
        "llm_reason": llm.get("reason"),
        "llm_auto": llm.get("auto"),
        "llm_tokens": llm.get("tokens", 0),
        "typescript_resolver": cfg.typescript_resolver,
    }

    bundle = build_bundle(graph_data, clusters, analysis, public_manifest)
    validation = assert_valid_graph_bundle(bundle)

    _write_json(out / ".graphite_manifest.json", public_manifest)
    _write_json(out / ".graphite_graph.json", graph_data)
    _write_json(out / ".graphite_clusters.json", clusters)
    _write_json(out / ".graphite_analysis.json", analysis)
    _write_json(out / ".graphite_validation.json", validation)

    export_json(graph_data, clusters, analysis, public_manifest, out / "graph.json")
    export_html(graph_data, clusters, analysis, public_manifest, out / "graph.html")
    export_md(graph_data, clusters, analysis, public_manifest, out / "GRAPH_REPORT.md")

    print(f"[graphite] report written to {out}/")
    print("  - GRAPH_REPORT.md")
    print("  - graph.json")
    print("  - graph.html")


def _build_project(path: Path, cfg: Config) -> None:
    args = argparse.Namespace(path=str(path))
    manifest, entries = _scan(args, cfg)
    graph_data, clusters, analysis = _build(args, cfg, manifest, entries)
    _report(cfg, manifest, graph_data, clusters, analysis)


def _manifest_map(manifest: dict[str, Any]) -> dict[str, str]:
    return {f["rel_path"]: f.get("hash", "") for f in manifest.get("files", []) if "rel_path" in f}


def _check_status(root: Path, cfg: Config) -> dict[str, Any]:
    manifest_path = cfg.output_dir / ".graphite_manifest.json"
    if not manifest_path.exists():
        return {
            "stale": True,
            "reason": "missing manifest",
            "manifest": manifest_path.as_posix(),
            "added": [],
            "changed": [],
            "removed": [],
        }
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "stale": True,
            "reason": f"unreadable manifest: {exc}",
            "added": [],
            "changed": [],
            "removed": [],
        }

    entries = collect_files(root, cfg)
    current = {e.rel_path: e.content_hash for e in entries}
    old = _manifest_map(previous)
    added = sorted(set(current) - set(old))
    removed = sorted(set(old) - set(current))
    changed = sorted(p for p in set(current).intersection(old) if current[p] != old[p])
    stale = bool(added or removed or changed)
    return {
        "stale": stale,
        "file_count": len(current),
        "manifest_file_count": len(old),
        "added": added,
        "changed": changed,
        "removed": removed,
    }


def _load_graph(path: Path) -> Any:
    if not path.exists():
        print(f"[graphite] graph not found: {path}", file=sys.stderr)
        print("Run `graphite build .` first.", file=sys.stderr)
        raise SystemExit(1)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return graph_from_json(data)


def _is_test_file(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return "/tests/" in f"/{normalized}" or normalized.endswith(_TEST_SUFFIXES)


def _impact(g: Any, changes: list[str], depth: int) -> dict[str, Any]:
    start_nodes: list[str] = []
    missing: list[str] = []
    for change in changes:
        node = _find_node(g, change)
        if node:
            start_nodes.append(node)
        else:
            missing.append(change)

    visited: set[str] = set(start_nodes)
    queue: deque[tuple[str, int]] = deque((n, 0) for n in start_nodes)
    impacted_nodes: set[str] = set()
    while queue:
        node, dist = queue.popleft()
        if dist >= depth:
            continue
        for pred in sorted(g.predecessors(node)):
            if pred in visited:
                continue
            visited.add(pred)
            impacted_nodes.add(pred)
            queue.append((pred, dist + 1))

    impacted_files: set[str] = set()
    likely_tests: set[str] = set()
    for node in impacted_nodes.union(start_nodes):
        sf = g.nodes[node].get("source_file")
        if not sf:
            continue
        if _is_test_file(sf):
            likely_tests.add(sf)
        elif node not in start_nodes:
            impacted_files.add(sf)

    return {
        "changed": changes,
        "matched_nodes": sorted(start_nodes),
        "missing": missing,
        "depth": depth,
        "impacted_files": sorted(impacted_files),
        "likely_tests": sorted(likely_tests),
    }


def _print_watch_change(change: WatchChange) -> None:
    print("[graphite] change detected")
    for label, values in (
        ("added", change.added),
        ("changed", change.changed),
        ("removed", change.removed),
    ):
        if values:
            shown = ", ".join(values[:8])
            suffix = " ..." if len(values) > 8 else ""
            print(f"  {label}: {shown}{suffix}")


def _print_watch_impact(root: Path, cfg: Config, change: WatchChange, depth: int) -> None:
    graph_path = cfg.output_dir / "graph.json"
    impact_inputs = list(change.changed or change.removed)
    if not impact_inputs or not graph_path.exists():
        return

    try:
        g = _load_graph(graph_path)
        result = _impact(g, impact_inputs, depth)
    except Exception as exc:
        print(f"[graphite] impact skipped: {exc}", file=sys.stderr)
        return

    if result["impacted_files"]:
        print("[graphite] impacted files:")
        for path in result["impacted_files"][:20]:
            print(f"  - {path}")
    if result["likely_tests"]:
        print("[graphite] likely tests:")
        for path in result["likely_tests"][:30]:
            print(f"  - {path}")


def cmd_scan(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args)
    manifest, _ = _scan(args, cfg)
    _write_json(cfg.output_dir / ".graphite_manifest.json", manifest)
    print(f"[graphite] manifest written: {cfg.output_dir / '.graphite_manifest.json'}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args)
    _build_project(Path(args.path).resolve(), cfg)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    return cmd_build(args)


def cmd_check(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args)
    status = _check_status(Path(args.path).resolve(), cfg)
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    elif status["stale"]:
        print("[graphite] graph is stale")
        for key in ("added", "changed", "removed"):
            if status.get(key):
                print(f"  {key}: {', '.join(status[key])}")
    else:
        print("[graphite] graph is fresh")
    return 1 if status["stale"] else 0


def _project_scoped_config(args: argparse.Namespace, root: Path) -> Config:
    cfg = _config_from_args(args)
    data = cfg.to_dict()
    if not Path(data["output_dir"]).is_absolute():
        data["output_dir"] = root / data["output_dir"]
    if not Path(data["cache_dir"]).is_absolute():
        data["cache_dir"] = root / data["cache_dir"]
    return Config(**data)


def cmd_bootstrap(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    daemon_base = Path(args.daemon_base).resolve() if args.daemon_base else None
    result = bootstrap_project(root, daemon_base=daemon_base).to_dict()
    cfg = _project_scoped_config(args, root)
    build: dict[str, Any] = {"requested": not args.no_build, "ok": None}
    validation: dict[str, Any] = {"requested": not args.no_validate, "ok": None}

    if not args.no_build:
        if args.json:
            with contextlib.redirect_stdout(sys.stderr):
                _build_project(root, cfg)
        else:
            _build_project(root, cfg)
        build["ok"] = True

    if not args.no_validate:
        graph_path = cfg.output_dir / "graph.json"
        if graph_path.exists():
            with open(graph_path, "r", encoding="utf-8") as f:
                validation_report = validate_graph_bundle(json.load(f))
            validation.update(validation_report)
        else:
            validation.update({"ok": False, "error": f"graph not found: {graph_path}"})

    payload = {**result, "build": build, "validation": validation}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[graphite] bootstrapped: {root}")
        for key in ("gitignore", "agents"):
            item = result[key]
            action = "updated" if item.get("changed") else "already current"
            print(f"  - {key}: {action} ({item.get('path')})")
        daemon = result["daemon"]
        daemon_note = "listed" if daemon.get("project_listed") else "not listed yet"
        print(f"  - daemon: {daemon_note} ({daemon.get('status_path')})")
        if build["requested"]:
            print(f"  - build: {'ok' if build['ok'] else 'failed'}")
        if validation["requested"]:
            print(f"  - validation: {'ok' if validation.get('ok') else 'failed'}")
    return 0 if (validation.get("ok") is not False) else 1



def cmd_init(args: argparse.Namespace) -> int:
    if args.list_platforms:
        choices = platform_choices()
        if args.json:
            print(json.dumps({"platforms": choices}, ensure_ascii=False, indent=2))
        else:
            for item in choices:
                print(f"{item['key']}: {item['label']}")
        return 0
    root = Path(args.path).resolve()
    interactive = not args.platform and not args.all and not args.yes and sys.stdin.isatty()
    requested = ["all"] if args.all else (args.platform or [])
    platforms = resolve_platform_selection(requested, interactive=interactive)
    daemon_base = Path(args.daemon_base).resolve() if args.daemon_base else None
    result = init_project(root, platforms=platforms, daemon_base=daemon_base).to_dict()
    cfg = _project_scoped_config(args, root)
    build: dict[str, Any] = {"requested": not args.no_build, "ok": None}
    validation: dict[str, Any] = {"requested": not args.no_validate, "ok": None}

    if not args.no_build:
        if args.json:
            with contextlib.redirect_stdout(sys.stderr):
                _build_project(root, cfg)
        else:
            _build_project(root, cfg)
        build["ok"] = True

    if not args.no_validate:
        graph_path = cfg.output_dir / "graph.json"
        if graph_path.exists():
            with open(graph_path, "r", encoding="utf-8") as f:
                validation_report = validate_graph_bundle(json.load(f))
            validation.update(validation_report)
        else:
            validation.update({"ok": False, "error": f"graph not found: {graph_path}"})

    payload = {**result, "build": build, "validation": validation}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[graphite] initialized: {root}")
        print(f"  - platforms: {', '.join(result['platforms'])}")
        doc = result["graphite_doc"]
        print(f"  - graphite_doc: {'updated' if doc.get('changed') else 'already current'} ({doc.get('path')})")
        for item in result["platform_files"]:
            action = "updated" if item.get("changed") else "already current"
            print(f"  - {item.get('platform')}: {action} ({item.get('path')})")
        allowlist = result["allowlist"]
        if allowlist.get("changed"):
            print(f"  - gitignore allowlist: added {', '.join(allowlist.get('added', []))}")
        daemon = result["daemon"]
        daemon_note = "listed" if daemon.get("project_listed") else "not listed yet"
        print(f"  - daemon: {daemon_note} ({daemon.get('status_path')})")
        if build["requested"]:
            print(f"  - build: {'ok' if build['ok'] else 'failed'}")
        if validation["requested"]:
            print(f"  - validation: {'ok' if validation.get('ok') else 'failed'}")
    return 0 if (validation.get("ok") is not False) else 1

def cmd_audit_replacement(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    daemon_base = Path(args.daemon_base).resolve() if args.daemon_base else None
    cfg = _project_scoped_config(args, root)
    report = audit_replacement(root, daemon_base=daemon_base, cfg=cfg)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_replacement_audit(report), end="")
    return 1 if args.fail_on_blocker and not report["ok"] else 0


def cmd_validate(args: argparse.Namespace) -> int:
    graph_path = Path(args.graph_json)
    if not graph_path.exists():
        print(f"[graphite] graph not found: {graph_path}", file=sys.stderr)
        return 1
    try:
        with open(graph_path, "r", encoding="utf-8") as f:
            bundle = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[graphite] invalid graph json: {exc}", file=sys.stderr)
        return 1
    report = validate_graph_bundle(bundle)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if report["ok"]:
            print(
                f"[graphite] graph valid "
                f"({report['node_count']} nodes / {report['edge_count']} edges, "
                f"{report['warning_count']} warnings)"
            )
        else:
            print(f"[graphite] graph invalid ({report['error_count']} errors, {report['warning_count']} warnings)")
            for issue in report["errors"][:10]:
                print(f"  - {issue['code']}: {issue['message']} [{issue['path']}]")
    return 0 if report["ok"] else 1

def cmd_query(args: argparse.Namespace) -> int:
    g = _load_graph(Path(args.graph_json))
    result = query(g, args.query)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_impact(args: argparse.Namespace) -> int:
    g = _load_graph(Path(args.graph_json))
    result = _impact(g, args.files, args.depth)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Impacted files:")
        for path in result["impacted_files"]:
            print(f"  - {path}")
        print("Likely tests:")
        for path in result["likely_tests"]:
            print(f"  - {path}")
        if result["missing"]:
            print("Missing inputs:")
            for item in result["missing"]:
                print(f"  - {item}")
    return 0 if not result["missing"] else 1


def cmd_review_changes(args: argparse.Namespace) -> int:
    """Build deterministic change-review evidence without invoking an LLM."""
    root = Path(args.path).resolve()
    if not root.is_dir():
        raise ReviewError("project path is not a directory")
    if args.depth < 0:
        raise ReviewError("review depth must be zero or greater")
    if args.git_timeout <= 0:
        raise ReviewError("Git status timeout must be greater than zero")

    if args.files:
        changes = normalize_explicit_changes(root, args.files)
        discovery = "explicit"
    else:
        changes = discover_git_changes(root, timeout_seconds=args.git_timeout)
        discovery = "git"

    cfg = _project_scoped_config(args, root)
    graph_path = Path(args.graph_json) if args.graph_json is not None else cfg.output_dir / "graph.json"
    if not graph_path.is_absolute():
        graph_path = root / graph_path

    graph_bundle: Any = None
    graph_error: str | None = None
    try:
        with open(graph_path, "r", encoding="utf-8") as graph_file:
            graph_bundle = json.load(graph_file)
    except (OSError, UnicodeError, json.JSONDecodeError):
        graph_error = "dependency graph is unavailable"

    packet = build_review_packet(
        root_name=root.name,
        changes=changes,
        discovery=discovery,
        graph_bundle=graph_bundle,
        graph_status=_check_status(root, cfg),
        depth=args.depth,
        graph_error=graph_error,
    )
    if args.json:
        print(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_review_markdown(packet), end="")
    return 1 if args.fail_on_blocker and packet["blockers"] else 0


def cmd_context(args: argparse.Namespace) -> int:
    g = _load_graph(Path(args.graph_json))
    result = build_context(g, args.files, depth=args.depth, neighbor_limit=args.neighbor_limit)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_context_markdown(result))
    return 0 if not result["missing"] else 1


def cmd_watch(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args)
    root = Path(args.path).resolve()
    options = WatchOptions(
        interval_seconds=args.interval,
        debounce_seconds=args.debounce,
        max_cycles=args.max_cycles,
        build_now=not args.no_initial_build,
        once=args.once,
    )

    def on_change(change: WatchChange) -> bool:
        initial = len(change.added) > 0 and not change.changed and not change.removed
        if initial and options.build_now:
            print(f"[graphite] initial build for {root}")
        else:
            _print_watch_change(change)
            if args.impact:
                _print_watch_impact(root, cfg, change, args.impact_depth)
        try:
            _build_project(root, cfg)
            return True
        except Exception as exc:
            print(f"[graphite] rebuild failed: {exc}", file=sys.stderr)
            return False

    def on_error(exc: Exception) -> None:
        print(f"[graphite] watcher error: {exc}", file=sys.stderr)

    print(
        f"[graphite] watching {root} "
        f"(interval={options.interval_seconds}s, debounce={options.debounce_seconds}s)"
    )
    if cfg.llm_mode != "none":
        print("[graphite] warning: LLM enrichment is enabled for watch rebuilds")
    processed = watch_loop(root, cfg, on_change, options, on_error=on_error)
    if args.once:
        print(f"[graphite] watch once complete ({processed} rebuilds)")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args)
    base = Path(args.base_path).resolve()
    options = DaemonOptions(
        scan_interval_seconds=args.scan_interval,
        discover_interval_seconds=args.discover_interval,
        debounce_seconds=args.debounce,
        max_depth=args.max_depth,
        max_projects=args.max_projects,
        max_files_per_project=args.max_files_per_project,
        max_builds_per_cycle=args.max_builds_per_cycle,
        build_timeout_seconds=args.build_timeout,
        build_now=not args.no_initial_build,
        once=args.once,
        max_cycles=args.max_cycles,
        state_dir=Path(args.state_dir).resolve() if args.state_dir else None,
    )
    if cfg.llm_mode != "none":
        print("[graphite] warning: LLM enrichment is enabled for daemon rebuilds")
    status = run_daemon(base, cfg, options)
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        state_dir = options.state_dir or (base / ".graphite-daemon")
        print(
            f"[graphite] daemon status: {status.get('status')} "
            f"({status.get('project_count')} projects, {status.get('failing_projects')} failing)"
        )
        print(f"[graphite] status file: {state_dir / 'status.json'}")
        print(f"[graphite] log file: {state_dir / 'graphite-daemon.log'}")
    return 0


def cmd_daemon_status(args: argparse.Namespace) -> int:
    base = Path(args.base_path).resolve()
    state_dir = Path(args.state_dir).resolve() if args.state_dir else None
    try:
        status = read_daemon_status(base, state_dir)
    except FileNotFoundError:
        path = (state_dir or (base / ".graphite-daemon")) / "status.json"
        print(f"[graphite] daemon status not found: {path}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print(
            f"[graphite] daemon status: {status.get('status')} "
            f"({status.get('project_count')} projects, {status.get('failing_projects')} failing, "
            f"{status.get('pending_projects')} pending)"
        )
        print(f"[graphite] updated: {status.get('updated_at')}")
        for project in status.get("projects", [])[:20]:
            print(
                f"  - {project.get('root')} | builds={project.get('build_count')} "
                f"failures={project.get('failure_count')} files={project.get('file_count')}"
            )
    return 0


def cmd_daemon_health(args: argparse.Namespace) -> int:
    base = Path(args.base_path).resolve()
    state_dir = Path(args.state_dir).resolve() if args.state_dir else None
    options = HealthOptions(
        max_status_age_seconds=args.max_status_age,
        max_project_success_age_seconds=args.max_project_success_age,
        require_process=not args.no_process_check,
        require_startup=not args.no_startup_check,
        startup_name=args.startup_name,
    )
    report = evaluate_daemon_health(base, state_dir=state_dir, options=options)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_health_text(report), end="")
    return 1 if args.fail_on_error and not report["ok"] else 0


def _daemon_task_command_from_args(args: argparse.Namespace) -> Any:
    return daemon_task_command(
        Path(args.base_path),
        graphite_executable=args.graphite_executable,
        scan_interval=args.scan_interval,
        discover_interval=args.discover_interval,
        max_projects=args.max_projects,
        max_depth=args.max_depth,
        max_builds_per_cycle=args.max_builds_per_cycle,
        build_timeout=args.build_timeout,
        debounce=args.debounce,
    )


def cmd_daemon_install_windows(args: argparse.Namespace) -> int:
    command = _daemon_task_command_from_args(args)
    result = create_daemon_task(args.task_name, command, force=not args.no_force, start_now=args.start_now)
    if args.json:
        print(json.dumps({"task_name": args.task_name, "task_run": command.task_run, **result}, ensure_ascii=False, indent=2))
    elif result["ok"]:
        print(f"[graphite] scheduled task installed: {args.task_name}")
        print(f"[graphite] task command: {command.task_run}")
        if args.start_now:
            started = result.get("started", {})
            print(f"[graphite] task start requested: {started.get('ok')}")
    else:
        print(f"[graphite] failed to install scheduled task: {args.task_name}", file=sys.stderr)
        if result.get("stderr"):
            print(result["stderr"], file=sys.stderr)
        if result.get("stdout"):
            print(result["stdout"], file=sys.stderr)
    return 0 if result["ok"] else 1


def cmd_daemon_task_status(args: argparse.Namespace) -> int:
    result = query_daemon_task(args.task_name)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result.get("exists"):
        task = result.get("task", {})
        print(f"[graphite] scheduled task exists: {args.task_name}")
        for key in ("TaskName", "Status", "Task To Run", "Schedule Type", "Last Run Time", "Last Result", "Next Run Time"):
            if isinstance(task, dict) and task.get(key):
                print(f"  {key}: {task[key]}")
    else:
        print(f"[graphite] scheduled task not found: {args.task_name}")
    return 0 if result.get("exists") else 1


def cmd_daemon_uninstall_windows(args: argparse.Namespace) -> int:
    result = delete_daemon_task(args.task_name)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        print(f"[graphite] scheduled task removed: {args.task_name}")
    else:
        print(f"[graphite] failed to remove scheduled task: {args.task_name}", file=sys.stderr)
        if result.get("stderr"):
            print(result["stderr"], file=sys.stderr)
        if result.get("stdout"):
            print(result["stdout"], file=sys.stderr)
    return 0 if result["ok"] else 1


def cmd_daemon_install_startup_windows(args: argparse.Namespace) -> int:
    result = install_startup_launcher(
        Path(args.base_path),
        name=args.name,
        graphite_executable=args.graphite_executable,
        scan_interval=args.scan_interval,
        discover_interval=args.discover_interval,
        max_projects=args.max_projects,
        max_depth=args.max_depth,
        max_builds_per_cycle=args.max_builds_per_cycle,
        build_timeout=args.build_timeout,
        debounce=args.debounce,
    )
    payload = {
        "name": result.name,
        "installed": True,
        "base_path": str(result.base_path),
        "script_path": str(result.script_path),
        "launcher_path": str(result.launcher_path),
        "command_line": result.command_line,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[graphite] startup launcher installed: {result.name}")
        print(f"[graphite] launcher: {result.launcher_path}")
        print(f"[graphite] script: {result.script_path}")
    return 0


def cmd_daemon_startup_status(args: argparse.Namespace) -> int:
    result = startup_status(Path(args.base_path), name=args.name)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["installed"]:
        print(f"[graphite] startup launcher installed: {args.name}")
        print(f"[graphite] launcher: {result['launcher_path']}")
        print(f"[graphite] script: {result['script_path']}")
    else:
        print(f"[graphite] startup launcher not installed: {args.name}")
        print(f"[graphite] launcher: {result['launcher_path']}")
        print(f"[graphite] script: {result['script_path']}")
    return 0 if result["installed"] else 1


def cmd_daemon_uninstall_startup_windows(args: argparse.Namespace) -> int:
    result = uninstall_startup_launcher(Path(args.base_path), name=args.name)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"[graphite] startup launcher removed: {args.name}")
        for path in result["removed"]:
            print(f"  - {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graphite", description="Local-first code knowledge graph.")
    parser.add_argument("--output-dir", default=None, help="Output directory (default: graph-out)")
    parser.add_argument("--cache-dir", default=None, help="Cache directory (default: .cache/graphite)")
    parser.add_argument("--workers", type=int, default=None, help="Parallel workers")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument("--typescript-resolver", choices=["auto", "compiler", "heuristic", "disabled"], default=None, help="TypeScript resolver mode (default: auto)")
    parser.add_argument("--typescript-resolver-timeout", type=float, default=None, help="TypeScript compiler resolver timeout in seconds")
    parser.add_argument("--no-typescript-symbol-references", action="store_true", help="Disable TypeScript compiler symbol-reference edges")
    parser.add_argument("--llm", choices=["none", "auto", "local", "cloud"], default=None, help="Optional LLM enrichment mode")
    parser.add_argument("--llm-provider", default=None, help="LLM provider: ollama, openai, openai-compatible, lmstudio, vllm, openrouter, groq")
    parser.add_argument("--llm-model", default=None, help="LLM model name")
    parser.add_argument("--llm-base-url", default=None, help="LLM provider base URL")
    parser.add_argument("--llm-api-key", default=None, help="LLM API key; prefer GRAPHITE_LLM_API_KEY for shells/history")
    parser.add_argument("--llm-timeout", type=float, default=None, help="LLM request timeout in seconds")
    parser.add_argument("--llm-max-input-chars", type=int, default=None, help="Maximum graph-summary prompt characters")

    sub = parser.add_subparsers(dest="command")

    p_scan = sub.add_parser("scan", help="Scan files and write manifest")
    p_scan.add_argument("path", help="Repository path")
    p_scan.set_defaults(func=cmd_scan)

    p_build = sub.add_parser("build", help="Scan + extract + build graph + report")
    p_build.add_argument("path", help="Repository path")
    p_build.set_defaults(func=cmd_build)

    p_report = sub.add_parser("report", help="Alias for build")
    p_report.add_argument("path", help="Repository path")
    p_report.set_defaults(func=cmd_report)

    p_check = sub.add_parser("check", help="Check whether graph-out is stale")
    p_check.add_argument("path", help="Repository path")
    p_check.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_check.set_defaults(func=cmd_check)


    p_init = sub.add_parser("init", aliases=["Init"], help="Initialize Graphite instructions for AI coding platforms")
    p_init.add_argument("path", nargs="?", default=".", help="Project path (default: current directory)")
    p_init.add_argument("--platform", action="append", default=[], help="Platform to configure: codex, claude, antigravity, visual-studio, cursor, windsurf, or all. Can be repeated or comma-separated.")
    p_init.add_argument("--all", action="store_true", help="Configure every supported platform")
    p_init.add_argument("--yes", action="store_true", help="Use default platforms without prompting when --platform is omitted")
    p_init.add_argument("--daemon-base", default=None, help="Daemon base folder for visibility check, default auto-detects F:/Projects")
    p_init.add_argument("--no-build", action="store_true", help="Only update instruction files; do not build graph")
    p_init.add_argument("--no-validate", action="store_true", help="Skip graph validation after init")
    p_init.add_argument("--list-platforms", action="store_true", help="Print supported platform keys and exit")
    p_init.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_init.set_defaults(func=cmd_init)

    p_bootstrap = sub.add_parser("bootstrap", help="Make a project Graphite-ready and optionally build its graph")
    p_bootstrap.add_argument("path", help="Project path")
    p_bootstrap.add_argument("--daemon-base", default=None, help="Daemon base folder for visibility check, default auto-detects F:/Projects")
    p_bootstrap.add_argument("--no-build", action="store_true", help="Only update project workflow files; do not build graph")
    p_bootstrap.add_argument("--no-validate", action="store_true", help="Skip graph validation after bootstrap")
    p_bootstrap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_bootstrap.set_defaults(func=cmd_bootstrap)

    p_audit_replacement = sub.add_parser("audit-replacement", help="Audit whether Graphite is ready to replace Graphify in a project")
    p_audit_replacement.add_argument("path", help="Project path")
    p_audit_replacement.add_argument("--daemon-base", default=None, help="Daemon base folder for daemon and health checks")
    p_audit_replacement.add_argument("--fail-on-blocker", action="store_true", help="Return non-zero when replacement blockers are found")
    p_audit_replacement.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_audit_replacement.set_defaults(func=cmd_audit_replacement)

    p_validate = sub.add_parser("validate", help="Validate graph.json integrity")
    p_validate.add_argument("--graph-json", default="graph-out/graph.json", help="Path to graph.json")
    p_validate.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_validate.set_defaults(func=cmd_validate)
    p_query = sub.add_parser("query", help="Query an existing graph.json")
    p_query.add_argument("query", help="Query string, e.g. 'depends-on db.ts'")
    p_query.add_argument("--graph-json", default="graph-out/graph.json", help="Path to graph.json")
    p_query.set_defaults(func=cmd_query)

    p_impact = sub.add_parser("impact", help="Suggest impacted files and tests for changed files")
    p_impact.add_argument("files", nargs="+", help="Changed file paths or graph node fragments")
    p_impact.add_argument("--graph-json", default="graph-out/graph.json", help="Path to graph.json")
    p_impact.add_argument("--depth", type=int, default=2, help="Reverse dependency traversal depth")
    p_impact.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_impact.set_defaults(func=cmd_impact)

    p_review = sub.add_parser(
        "review-changes",
        help="Build deterministic review evidence for explicit or Git changes",
    )
    p_review.add_argument(
        "path", nargs="?", default=".", help="Project path (default: current directory)"
    )
    p_review.add_argument(
        "files",
        nargs="*",
        help="Changed paths; omit to discover changes from Git",
    )
    p_review.add_argument(
        "--graph-json",
        default=None,
        help="Graph JSON path; relative to the project root",
    )
    p_review.add_argument(
        "--depth", type=int, default=2, help="Reverse dependency traversal depth"
    )
    p_review.add_argument(
        "--git-timeout", type=float, default=5.0, help="Git discovery timeout in seconds"
    )
    p_review.add_argument(
        "--fail-on-blocker",
        action="store_true",
        help="Return non-zero when review blockers are found",
    )
    p_review.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_review.set_defaults(func=cmd_review_changes)

    p_context = sub.add_parser("context", help="Print compact graph context for files or nodes")
    p_context.add_argument("files", nargs="+", help="File paths, node ids, or graph node fragments")
    p_context.add_argument("--graph-json", default="graph-out/graph.json", help="Path to graph.json")
    p_context.add_argument("--depth", type=int, default=2, help="Reverse dependency impact depth")
    p_context.add_argument("--neighbor-limit", type=int, default=20, help="Maximum direct neighbors and community peers per matched node")
    p_context.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_context.set_defaults(func=cmd_context)

    p_watch = sub.add_parser("watch", help="Watch a repo and rebuild graph-out after stable file changes")
    p_watch.add_argument("path", help="Repository path")
    p_watch.add_argument("--interval", type=float, default=1.5, help="Polling interval in seconds")
    p_watch.add_argument("--debounce", type=float, default=0.75, help="Stable-change debounce seconds")
    p_watch.add_argument("--impact", action="store_true", help="Print impacted files/tests before rebuild")
    p_watch.add_argument("--impact-depth", type=int, default=2, help="Reverse dependency impact depth")
    p_watch.add_argument("--no-initial-build", action="store_true", help="Do not build immediately on start")
    p_watch.add_argument("--once", action="store_true", help="Run initial build and one poll cycle, then exit")
    p_watch.add_argument("--max-cycles", type=int, default=None, help="Stop after this many poll cycles")
    p_watch.set_defaults(func=cmd_watch)

    default_root = str(default_projects_root())
    p_daemon = sub.add_parser("daemon", help="Watch all discovered projects under a base folder")
    p_daemon.add_argument("base_path", nargs="?", default=default_root, help="Base folder to discover projects under")
    p_daemon.add_argument("--scan-interval", type=float, default=10.0, help="Polling interval in seconds")
    p_daemon.add_argument("--discover-interval", type=float, default=60.0, help="Project rediscovery interval in seconds")
    p_daemon.add_argument("--debounce", type=float, default=1.0, help="Stable-change debounce seconds")
    p_daemon.add_argument("--max-depth", type=int, default=6, help="Maximum discovery depth below base folder")
    p_daemon.add_argument("--max-projects", type=int, default=128, help="Maximum projects to supervise")
    p_daemon.add_argument("--max-files-per-project", type=int, default=10000, help="Maximum files to ingest per project")
    p_daemon.add_argument("--max-builds-per-cycle", type=int, default=2, help="Maximum project rebuilds per daemon cycle")
    p_daemon.add_argument("--build-timeout", type=float, default=300.0, help="Per-project build timeout in seconds")
    p_daemon.add_argument("--state-dir", default=None, help="Daemon state directory (default: <base>/.graphite-daemon)")
    p_daemon.add_argument("--no-initial-build", action="store_true", help="Discover projects without building immediately")
    p_daemon.add_argument("--once", action="store_true", help="Run one daemon cycle and exit")
    p_daemon.add_argument("--max-cycles", type=int, default=None, help="Stop after this many daemon cycles")
    p_daemon.add_argument("--json", action="store_true", help="Emit final status as JSON")
    p_daemon.set_defaults(func=cmd_daemon)

    p_daemon_status = sub.add_parser("daemon-status", help="Read the latest Graphite daemon status")
    p_daemon_status.add_argument("base_path", nargs="?", default=default_root, help="Base folder used by the daemon")
    p_daemon_status.add_argument("--state-dir", default=None, help="Daemon state directory (default: <base>/.graphite-daemon)")
    p_daemon_status.add_argument("--json", action="store_true", help="Emit status as JSON")
    p_daemon_status.set_defaults(func=cmd_daemon_status)

    p_daemon_health = sub.add_parser("daemon-health", help="Run operational health checks for the Graphite daemon")
    p_daemon_health.add_argument("base_path", nargs="?", default=default_root, help="Base folder used by the daemon")
    p_daemon_health.add_argument("--state-dir", default=None, help="Daemon state directory (default: <base>/.graphite-daemon)")
    p_daemon_health.add_argument("--max-status-age", type=float, default=180.0, help="Maximum acceptable status age in seconds")
    p_daemon_health.add_argument("--max-project-success-age", type=float, default=86400.0, help="Warn when a project has not built successfully within this many seconds")
    p_daemon_health.add_argument("--startup-name", default=DEFAULT_TASK_NAME, help="Startup launcher name")
    p_daemon_health.add_argument("--no-process-check", action="store_true", help="Skip local daemon process check")
    p_daemon_health.add_argument("--no-startup-check", action="store_true", help="Skip Windows startup launcher check")
    p_daemon_health.add_argument("--fail-on-error", action="store_true", help="Return non-zero when health errors are present")
    p_daemon_health.add_argument("--json", action="store_true", help="Emit health report as JSON")
    p_daemon_health.set_defaults(func=cmd_daemon_health)

    p_daemon_install = sub.add_parser("daemon-install-windows", help="Install the Graphite daemon as a Windows Scheduled Task")
    p_daemon_install.add_argument("base_path", nargs="?", default=default_root, help="Base folder to supervise")
    p_daemon_install.add_argument("--task-name", default=DEFAULT_TASK_NAME, help="Windows Scheduled Task name")
    p_daemon_install.add_argument("--graphite-executable", default=None, help="Path to graphite executable or shim")
    p_daemon_install.add_argument("--scan-interval", type=float, default=15.0, help="Polling interval in seconds")
    p_daemon_install.add_argument("--discover-interval", type=float, default=90.0, help="Project rediscovery interval in seconds")
    p_daemon_install.add_argument("--debounce", type=float, default=1.0, help="Stable-change debounce seconds")
    p_daemon_install.add_argument("--max-depth", type=int, default=6, help="Maximum discovery depth below base folder")
    p_daemon_install.add_argument("--max-projects", type=int, default=128, help="Maximum projects to supervise")
    p_daemon_install.add_argument("--max-builds-per-cycle", type=int, default=1, help="Maximum project rebuilds per daemon cycle")
    p_daemon_install.add_argument("--build-timeout", type=float, default=240.0, help="Per-project build timeout in seconds")
    p_daemon_install.add_argument("--start-now", action="store_true", help="Start the task immediately after installation")
    p_daemon_install.add_argument("--no-force", action="store_true", help="Do not overwrite an existing task")
    p_daemon_install.add_argument("--json", action="store_true", help="Emit installation result as JSON")
    p_daemon_install.set_defaults(func=cmd_daemon_install_windows)

    p_daemon_task_status = sub.add_parser("daemon-task-status", help="Read the Windows Scheduled Task status for Graphite daemon")
    p_daemon_task_status.add_argument("--task-name", default=DEFAULT_TASK_NAME, help="Windows Scheduled Task name")
    p_daemon_task_status.add_argument("--json", action="store_true", help="Emit task status as JSON")
    p_daemon_task_status.set_defaults(func=cmd_daemon_task_status)

    p_daemon_uninstall = sub.add_parser("daemon-uninstall-windows", help="Remove the Graphite daemon Windows Scheduled Task")
    p_daemon_uninstall.add_argument("--task-name", default=DEFAULT_TASK_NAME, help="Windows Scheduled Task name")
    p_daemon_uninstall.add_argument("--json", action="store_true", help="Emit removal result as JSON")
    p_daemon_uninstall.set_defaults(func=cmd_daemon_uninstall_windows)

    p_startup_install = sub.add_parser("daemon-install-startup-windows", help="Install hidden Windows Startup-folder launcher for Graphite daemon")
    p_startup_install.add_argument("base_path", nargs="?", default=default_root, help="Base folder to supervise")
    p_startup_install.add_argument("--name", default=DEFAULT_TASK_NAME, help="Startup launcher name")
    p_startup_install.add_argument("--graphite-executable", default=None, help="Path to graphite executable or shim")
    p_startup_install.add_argument("--scan-interval", type=float, default=15.0, help="Polling interval in seconds")
    p_startup_install.add_argument("--discover-interval", type=float, default=90.0, help="Project rediscovery interval in seconds")
    p_startup_install.add_argument("--debounce", type=float, default=1.0, help="Stable-change debounce seconds")
    p_startup_install.add_argument("--max-depth", type=int, default=6, help="Maximum discovery depth below base folder")
    p_startup_install.add_argument("--max-projects", type=int, default=128, help="Maximum projects to supervise")
    p_startup_install.add_argument("--max-builds-per-cycle", type=int, default=1, help="Maximum project rebuilds per daemon cycle")
    p_startup_install.add_argument("--build-timeout", type=float, default=240.0, help="Per-project build timeout in seconds")
    p_startup_install.add_argument("--json", action="store_true", help="Emit installation result as JSON")
    p_startup_install.set_defaults(func=cmd_daemon_install_startup_windows)

    p_startup_status = sub.add_parser("daemon-startup-status", help="Read Windows Startup-folder launcher status for Graphite daemon")
    p_startup_status.add_argument("base_path", nargs="?", default=default_root, help="Base folder supervised by the launcher")
    p_startup_status.add_argument("--name", default=DEFAULT_TASK_NAME, help="Startup launcher name")
    p_startup_status.add_argument("--json", action="store_true", help="Emit startup status as JSON")
    p_startup_status.set_defaults(func=cmd_daemon_startup_status)

    p_startup_uninstall = sub.add_parser("daemon-uninstall-startup-windows", help="Remove hidden Windows Startup-folder launcher for Graphite daemon")
    p_startup_uninstall.add_argument("base_path", nargs="?", default=default_root, help="Base folder supervised by the launcher")
    p_startup_uninstall.add_argument("--name", default=DEFAULT_TASK_NAME, help="Startup launcher name")
    p_startup_uninstall.add_argument("--json", action="store_true", help="Emit removal result as JSON")
    p_startup_uninstall.set_defaults(func=cmd_daemon_uninstall_startup_windows)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    try:
        return int(args.func(args) or 0)
    except Exception as e:
        print(f"[graphite] error: {e}", file=sys.stderr)
        if os.environ.get("GRAPHITE_DEBUG"):
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())













