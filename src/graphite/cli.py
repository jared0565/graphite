"""Graphite command-line interface."""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Any, TextIO

from .analyze import analyze
from .cache import Cache
from .bootstrap import bootstrap_project
from .cluster import detect_communities
from .config import Config, default_projects_root
from .context import build_context, format_context_markdown
from .daemon import DaemonOptions, read_daemon_status, run_daemon
from .daemon_health import HealthOptions, evaluate_daemon_health, format_health_text
from .doctor import format_doctor_text, run_doctor
from .engine_identity import engine_identity
from .export.html import to_html as export_html
from .export.json import build_bundle, to_json as export_json
from .export.md import to_markdown as export_md
from .extract.ast import extract_all
from .freshness import check_graph_freshness
from .graph import build_graph, graph_to_json
from .graph_io import MAX_GRAPH_BYTES, GraphReadError, load_validated_graph_bundle
from .ingest import collect_files
from .init import init_project, platform_choices, resolve_platform_selection
from .io import atomic_write_json
from .llm import CANONICAL_ENRICHMENT_MIGRATION_MESSAGE
from .overlays import OverlayError, OverlayRequest, build_overlay
from .routing.approval import approval_prompt
from .routing.contracts import Effort
from .routing.lifecycle_operator import LifecycleOperator, LifecycleOperatorError
from .routing.service import RoutingService, RoutingServiceError
from .routing.storage import DEFAULT_RECOVERY_PAGE_SIZE, StorageError
from .natural_query import answer_natural, natural_catalog, translate_natural
from .query import (
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    _find_node,
    annotate_communities,
    build_plan,
    plan_preview,
    query,
    search_graph,
    verb_catalog,
)
from .query_plan import DEFAULT_MAX_DEPTH, DEFAULT_MAX_RESULTS, PLAN_VERSION
from .replacement_audit import audit_replacement, format_replacement_audit
from .review import (
    ReviewError,
    build_review_packet,
    discover_git_changes,
    format_review_markdown,
    normalize_explicit_changes,
)
from .validation import assert_valid_graph_bundle, validate_graph_bundle
from .typescript_activation import (
    ActivationOutcome,
    ActivationRequest,
    ActivationResult,
    activate_typescript,
)
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

# Review graph reads are bounded to prevent untrusted artifacts exhausting memory.
_MAX_REVIEW_GRAPH_BYTES = 128 * 1024 * 1024
_CANONICAL_COMMANDS = frozenset(
    {
        "scan",
        "build",
        "report",
        "check",
        "validate",
        "query",
        "search",
        "capabilities",
        "impact",
        "context",
        "watch",
        "daemon",
    }
)
# Hook endpoints are inference-free like canonical commands but are not part of
# the agent-facing query surface, so they stay out of `capabilities` output.
_INFERENCE_FREE_EXTRA_COMMANDS = frozenset({"agent-hook"})
_LLM_GATED_COMMANDS = _CANONICAL_COMMANDS | _INFERENCE_FREE_EXTRA_COMMANDS
_LEGACY_LLM_ARGUMENTS = (
    "llm_provider",
    "llm_model",
    "llm_base_url",
    "llm_api_key",
    "llm_timeout",
    "llm_max_input_chars",
    "llm_max_output_tokens",
)


def _config_from_args(args: argparse.Namespace, *, canonical: bool = False) -> Config:
    """Build config from defaults + CLI args + env."""
    base = Config.from_env(include_llm=not canonical)
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
    configurable = (
        ("typescript_resolver", "typescript_resolver"),
        ("typescript_resolver_timeout", "typescript_resolver_timeout_seconds"),
    )
    if not canonical:
        configurable += (
            ("llm", "llm_mode"),
            ("llm_provider", "llm_provider"),
            ("llm_model", "llm_model"),
            ("llm_base_url", "llm_base_url"),
            ("llm_api_key", "llm_api_key"),
            ("llm_timeout", "llm_timeout_seconds"),
            ("llm_max_input_chars", "llm_max_input_chars"),
            ("llm_max_output_tokens", "llm_max_output_tokens"),
        )
    for arg_name, cfg_name in configurable:
        value = getattr(args, arg_name, None)
        if value is not None:
            kwargs[cfg_name] = value
    cfg = Config(**kwargs)
    return cfg.canonical_graph() if canonical else cfg


def _write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_json(path, data, indent=2)


def _scan(args: argparse.Namespace, cfg: Config) -> tuple[dict[str, Any], list[Any]]:
    cfg = cfg.canonical_graph()
    root = Path(args.path).resolve()
    if not root.exists():
        print(f"[graphite] path not found: {root}", file=sys.stderr)
        raise SystemExit(1)

    start = time.time()
    entries = collect_files(root, cfg)
    manifest = {
        "root": root.name,
        "file_count": len(entries),
        "engine": engine_identity(cfg.cache_version),
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
    cfg = cfg.canonical_graph()
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

    return graph_data, clusters, analysis


def _report(
    cfg: Config,
    manifest: dict[str, Any],
    graph_data: dict[str, Any],
    clusters: dict[str, Any],
    analysis: dict[str, Any],
) -> None:
    cfg = cfg.canonical_graph()
    out = cfg.output_dir
    out.mkdir(parents=True, exist_ok=True)

    public_manifest = {
        **manifest,
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
    cfg = cfg.canonical_graph()
    args = argparse.Namespace(path=str(path))
    manifest, entries = _scan(args, cfg)
    graph_data, clusters, analysis = _build(args, cfg, manifest, entries)
    _report(cfg, manifest, graph_data, clusters, analysis)


def _load_graph(path: Path, *, root: Path | None = None) -> Any:
    selected_root = (root or Path.cwd()).resolve()
    try:
        _, graph = load_validated_graph_bundle(path, root=selected_root)
    except GraphReadError as exc:
        raise ValueError(f"graph unavailable: {exc.code}") from None
    return graph


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
        g = _load_graph(graph_path, root=root)
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
    cfg = _config_from_args(args, canonical=True)
    manifest, _ = _scan(args, cfg)
    _write_json(cfg.output_dir / ".graphite_manifest.json", manifest)
    print(f"[graphite] manifest written: {cfg.output_dir / '.graphite_manifest.json'}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args, canonical=True)
    _build_project(Path(args.path).resolve(), cfg)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    return cmd_build(args)


def cmd_check(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args, canonical=True)
    status = check_graph_freshness(
        Path(args.path).resolve(), cfg, ignore_engine=args.ignore_engine
    )
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    elif status["stale"]:
        reason = status.get("reason", "source changes")
        print(f"[graphite] graph is stale ({reason})")
        if reason == "engine_changed":
            print("  graphite was updated since this graph was built; rebuild to refresh")
        for key in ("added", "changed", "removed"):
            if status.get(key):
                print(f"  {key}: {', '.join(status[key])}")
    else:
        print("[graphite] graph is fresh")
    return 1 if status["stale"] else 0


def cmd_doctor(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        raise ValueError("doctor path must be an existing directory")
    cfg = _project_scoped_config(args, root)
    report = run_doctor(
        root,
        cfg=cfg,
        daemon_base=Path(args.daemon_base).resolve() if args.daemon_base else None,
        deep=args.deep,
        include_llm=args.include_llm,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_doctor_text(report), end="")
    return int(report["exit_code"])


def _print_overlay_result(payload: dict[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    outcome = payload.get("outcome_category", "failed")
    provider = payload.get("provider", "unknown")
    identity = payload.get("overlay_identity_digest", "unknown")
    print(f"[graphite] overlay {outcome}: provider={provider} identity={identity}")
    if payload.get("failure_category"):
        print(f"  - failure_category: {payload['failure_category']}")


def _overlay_error(code: str, *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps({"error": code}, sort_keys=True))
    else:
        print(f"[graphite] overlay error: {code}", file=sys.stderr)


def cmd_overlay_build(args: argparse.Namespace) -> int:
    """Run one explicit non-authoritative enrichment operation."""
    if args.llm_api_key is not None:
        _overlay_error("overlay_credential_argv_forbidden", json_mode=args.json)
        return 2
    root = Path(args.path).resolve()
    cfg = _project_scoped_config(args, root)
    try:
        request = OverlayRequest(
            repository_root=root,
            output_dir=cfg.output_dir,
            provider=cfg.llm_provider.strip().casefold().replace("_", "-"),
            provider_lifecycle_identity_digest=args.provider_identity_digest,
            model_identity_digest=args.model_identity_digest,
            routing_policy_digest=args.routing_policy_digest,
            created_at=int(time.time()),
        )
        payload = build_overlay(request, cfg)
    except ValueError:
        _overlay_error("overlay_request_invalid", json_mode=args.json)
        return 2
    except OverlayError as exc:
        code = str(exc)
        _overlay_error(code, json_mode=args.json)
        return 3 if code.startswith("canonical_") else 2
    _print_overlay_result(payload, json_mode=args.json)
    return 0 if payload.get("outcome_category") == "succeeded" else 4


def _project_scoped_config(
    args: argparse.Namespace, root: Path, *, canonical: bool = False
) -> Config:
    cfg = _config_from_args(args, canonical=canonical)
    data = cfg.to_dict()
    if not Path(data["output_dir"]).is_absolute():
        data["output_dir"] = root / data["output_dir"]
    if not Path(data["cache_dir"]).is_absolute():
        data["cache_dir"] = root / data["cache_dir"]
    return Config(**data)


def _activate_typescript_for_onboarding(
    args: argparse.Namespace, root: Path, cfg: Config
) -> ActivationResult:
    interactive = _onboarding_is_interactive(args)
    try:
        return activate_typescript(
            ActivationRequest(
                root=root,
                cfg=cfg,
                stdin_is_tty=interactive,
                stdout_is_tty=interactive,
                assume_yes=bool(getattr(args, "yes", False)),
                json_mode=bool(getattr(args, "json", False)),
            )
        )
    except Exception:
        return ActivationResult(
            ActivationOutcome.INSTALLATION_FAILED,
            None,
            "dependency_failed",
        )


def _onboarding_is_interactive(args: argparse.Namespace) -> bool:
    if (
        bool(getattr(args, "json", False))
        or bool(getattr(args, "yes", False))
        or bool(os.environ.get("CI"))
    ):
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _print_typescript_activation(activation: ActivationResult) -> None:
    manager = activation.manager.value if activation.manager else "none"
    changed = ", ".join(activation.changed_files) if activation.changed_files else "none"
    print(
        "  - TypeScript activation: "
        f"{activation.outcome.value} (manager={manager}, "
        f"reason={activation.reason}, changed={changed})"
    )
    if activation.outcome is ActivationOutcome.GUIDANCE_ONLY:
        print("    1. Set GRAPHITE_PACKAGE_VALIDATOR=<absolute-validator-path>.")
        print(
            "    2. Fail closed if GRAPHITE_PACKAGE_VALIDATOR is unset, relative, "
            "missing, or not a regular file."
        )
        print("    3. Run: node <absolute-validator-path> typescript")
        print("    4. With <project-manager>, add local dev dependency typescript with scripts disabled.")
        print("    5. Rerun graphite doctor or onboarding to confirm detection.")


def cmd_bootstrap(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    daemon_base = Path(args.daemon_base).resolve() if args.daemon_base else None
    result = bootstrap_project(root, daemon_base=daemon_base).to_dict()
    cfg = _project_scoped_config(args, root, canonical=True)
    activation = _activate_typescript_for_onboarding(args, root, cfg)
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

    payload = {
        **result,
        "typescript_activation": activation.to_dict(),
        "build": build,
        "validation": validation,
    }
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
        _print_typescript_activation(activation)
        if build["requested"]:
            print(f"  - build: {'ok' if build['ok'] else 'failed'}")
        if validation["requested"]:
            print(f"  - validation: {'ok' if validation.get('ok') else 'failed'}")
    return 1 if activation.fatal or validation.get("ok") is False else 0



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
    interactive = not args.platform and not args.all and _onboarding_is_interactive(args)
    requested = ["all"] if args.all else (args.platform or [])
    platforms = resolve_platform_selection(requested, interactive=interactive)
    daemon_base = Path(args.daemon_base).resolve() if args.daemon_base else None
    agent_hooks_mode = "strict" if args.strict else ("remind" if args.remind else None)
    result = init_project(
        root,
        platforms=platforms,
        daemon_base=daemon_base,
        agent_hooks_mode=agent_hooks_mode,
        install_agent_hooks=not args.no_agent_hooks,
    ).to_dict()
    cfg = _project_scoped_config(args, root, canonical=True)
    activation = _activate_typescript_for_onboarding(args, root, cfg)
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

    payload = {
        **result,
        "typescript_activation": activation.to_dict(),
        "build": build,
        "validation": validation,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[graphite] initialized: {root}")
        print(f"  - platforms: {', '.join(result['platforms'])}")
        doc = result["graphite_doc"]
        doc_action = doc.get("action") or ("updated" if doc.get("changed") else "already current")
        print(f"  - graphite_doc: {doc_action} ({doc.get('path')})")
        for item in result["platform_files"]:
            action = item.get("action") or ("updated" if item.get("changed") else "already current")
            print(f"  - {item.get('platform')}: {action} ({item.get('path')})")
        agent_hooks = result["agent_hooks"]
        print(
            f"  - agent_hooks: {agent_hooks.get('action')} "
            f"({agent_hooks.get('path')}, mode={agent_hooks.get('mode')})"
        )
        allowlist = result["allowlist"]
        if allowlist.get("changed"):
            print(f"  - gitignore allowlist: added {', '.join(allowlist.get('added', []))}")
        daemon = result["daemon"]
        daemon_note = "listed" if daemon.get("project_listed") else "not listed yet"
        print(f"  - daemon: {daemon_note} ({daemon.get('status_path')})")
        _print_typescript_activation(activation)
        if build["requested"]:
            print(f"  - build: {'ok' if build['ok'] else 'failed'}")
        if validation["requested"]:
            print(f"  - validation: {'ok' if validation.get('ok') else 'failed'}")
    return 1 if activation.fatal or validation.get("ok") is False else 0

def cmd_audit_replacement(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    daemon_base = Path(args.daemon_base).resolve() if args.daemon_base else None
    cfg = _project_scoped_config(args, root, canonical=True)
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
    if args.natural:
        translated = translate_natural(args.query)
        needs_graph = "plan" in translated or (
            "natural" in translated and translated["natural"]["intent"] == "search"
        )
        if args.plan_only or not needs_graph:
            print(json.dumps(translated, ensure_ascii=False, indent=2))
            return 0
        g = _load_graph(Path(args.graph_json), root=Path.cwd())
        print(json.dumps(answer_natural(g, args.query), ensure_ascii=False, indent=2))
        return 0
    if args.plan_only:
        print(json.dumps(plan_preview(args.query), ensure_ascii=False, indent=2))
        return 0
    g = _load_graph(Path(args.graph_json), root=Path.cwd())
    result = query(g, args.query)
    if args.show_plan:
        plan = build_plan(args.query)
        if "error" not in plan:
            result = {**result, "plan": plan}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    g = _load_graph(Path(args.graph_json), root=Path.cwd())
    result = search_graph(g, args.text, limit=args.limit)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif not result.get("ok"):
        print(f"[graphite] search error: {result.get('error')}")
    elif not result["results"]:
        print(f"[graphite] no matches for: {result['query']}")
    else:
        suffix = " (truncated)" if result["truncated"] else ""
        print(f"[graphite] {result['count']} match(es) for: {result['query']}{suffix}")
        for item in result["results"]:
            location = f" ({item['source_file']})" if item["source_file"] else ""
            print(f"  - {item['id']} [{item['kind']}, {item['match_type']}]{location}")
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    payload = {
        "ok": True,
        "schema_version": 1,
        "commands": sorted(_CANONICAL_COMMANDS),
        "query_verbs": verb_catalog(),
        "search": {"default_limit": DEFAULT_SEARCH_LIMIT, "max_limit": MAX_SEARCH_LIMIT},
        "query_limits": {
            "default_max_depth": DEFAULT_MAX_DEPTH,
            "default_max_results": DEFAULT_MAX_RESULTS,
        },
        "query_plans": {"plan_version": PLAN_VERSION, "flags": ["--plan-only", "--show-plan"]},
        "natural_language": {
            "available": True,
            "mode": "deterministic-grammar",
            "flag": "--natural",
            "providers": False,
            "intents": natural_catalog(),
        },
        "node_kinds": ["class", "file", "function", "unknown"],
        "edge_relations": ["calls", "contains", "imports", "inherits", "references", "type_references"],
        "limits": {"max_graph_bytes": MAX_GRAPH_BYTES},
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("[graphite] canonical commands: " + ", ".join(payload["commands"]))
        print("[graphite] query verbs:")
        for verb in payload["query_verbs"]:
            arguments = f" {verb['arguments']}" if verb["arguments"] else ""
            aliases = f" (aliases: {', '.join(verb['aliases'])})" if verb["aliases"] else ""
            print(f"  - {verb['name']}{arguments}{aliases}: {verb['description']}")
        print(f"[graphite] search: default limit {DEFAULT_SEARCH_LIMIT}, max {MAX_SEARCH_LIMIT}")
        print(
            f"[graphite] query limits: max_depth {DEFAULT_MAX_DEPTH} (path/reaches), "
            f"max_results {DEFAULT_MAX_RESULTS} (neighbor listings)"
        )
        print(f"[graphite] query plans: v{PLAN_VERSION} (--show-plan, --plan-only)")
        pattern_count = sum(len(entry["templates"]) for entry in natural_catalog())
        print(
            f"[graphite] natural language: deterministic grammar via query --natural "
            f"({pattern_count} patterns, no inference)"
        )
    return 0


def cmd_agent_hook(args: argparse.Namespace) -> int:
    from .agent_hooks import handle_pre_tool_use, handle_session_start

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
        if args.event == "session-start":
            out = handle_session_start(payload)
        else:
            out = handle_pre_tool_use(payload, args.mode)
        if out is not None:
            print(json.dumps(out, ensure_ascii=False))
    except Exception:
        pass  # fail-open: a hook problem must never break a tool call
    return 0


def cmd_impact(args: argparse.Namespace) -> int:
    g = _load_graph(Path(args.graph_json), root=Path.cwd())
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


def _review_depth(value: str) -> int:
    try:
        depth = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be zero or greater") from None
    if depth < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return depth


def _review_git_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be finite and greater than zero") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return timeout


def _resolve_review_graph_path(
    root: Path, cfg: Config, explicit_path: str | None
) -> Path:
    candidate = Path(explicit_path) if explicit_path is not None else cfg.output_dir / "graph.json"
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        raise ReviewError("graph path must be within project root") from None
    return resolved


def _load_review_graph(path: Path, root: Path | None = None) -> tuple[Any, str | None]:
    try:
        selected_root = root or path.parent.parent
        bundle, _ = load_validated_graph_bundle(
            path,
            root=selected_root,
            max_bytes=_MAX_REVIEW_GRAPH_BYTES,
        )
        return bundle, None
    except GraphReadError as exc:
        if exc.code == "graph_invalid":
            return {}, None
        return None, "dependency graph is unavailable"


def _review_graph_status(
    root: Path,
    cfg: Config,
    graph_path: Path,
    *,
    custom_graph: bool,
) -> dict[str, Any]:
    if not custom_graph:
        return check_graph_freshness(root, cfg)
    data = cfg.to_dict()
    data["output_dir"] = graph_path.parent
    return check_graph_freshness(root, Config(**data))


def cmd_review_changes(args: argparse.Namespace) -> int:
    """Build deterministic change-review evidence without invoking an LLM."""
    root = Path(args.path).resolve()
    if not root.is_dir():
        raise ReviewError("project path is not a directory")
    if args.files:
        changes = normalize_explicit_changes(root, args.files)
        discovery = "explicit"
    else:
        changes = discover_git_changes(root, timeout_seconds=args.git_timeout)
        discovery = "git"

    cfg = _project_scoped_config(args, root, canonical=True)
    custom_graph = args.graph_json is not None
    graph_path = _resolve_review_graph_path(root, cfg, args.graph_json)
    graph_bundle, graph_error = _load_review_graph(graph_path, root)

    packet = build_review_packet(
        root_name=root.name,
        changes=changes,
        discovery=discovery,
        graph_bundle=graph_bundle,
        graph_status=_review_graph_status(
            root, cfg, graph_path, custom_graph=custom_graph
        ),
        depth=args.depth,
        graph_error=graph_error,
    )
    if args.json:
        print(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_review_markdown(packet), end="")
    return 1 if args.fail_on_blocker and packet["blockers"] else 0


def cmd_context(args: argparse.Namespace) -> int:
    g = _load_graph(Path(args.graph_json), root=Path.cwd())
    result = build_context(g, args.files, depth=args.depth, neighbor_limit=args.neighbor_limit)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_context_markdown(result))
    return 0 if not result["missing"] else 1


def cmd_watch(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args, canonical=True)
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
    processed = watch_loop(root, cfg, on_change, options, on_error=on_error)
    if args.once:
        print(f"[graphite] watch once complete ({processed} rebuilds)")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args, canonical=True)
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


def _route_print(payload: dict[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


_ROUTE_RECOVERY_ERROR_CODES = frozenset({
    "attempt_id_invalid",
    "execution_attempt_conflict",
    "execution_attempt_missing",
    "legacy_attempt_bindings_missing",
    "legacy_attempt_digest_missing",
    "recovery_cursor_invalid",
    "recovery_limit_invalid",
    "repository_root_invalid",
    "storage_corrupt",
    "storage_locked",
    "storage_path_invalid",
    "storage_schema_unsupported",
    "storage_unavailable",
})


def _route_recovery_error(
    error: StorageError | RoutingServiceError | ValueError | OSError, *, json_mode: bool
) -> int:
    if isinstance(error, (StorageError, RoutingServiceError)):
        candidate = error.code
    elif isinstance(error, FileNotFoundError):
        candidate = "repository_root_invalid"
    elif isinstance(error, OSError):
        candidate = "storage_unavailable"
    elif isinstance(error, ValueError):
        candidate = str(error)
        if candidate not in _ROUTE_RECOVERY_ERROR_CODES:
            raise error
    else:
        raise error
    code = (
        candidate
        if candidate in _ROUTE_RECOVERY_ERROR_CODES
        else "route_recovery_failed"
    )
    if json_mode:
        print(json.dumps({"error": {"code": code}}, sort_keys=True), file=sys.stderr)
    else:
        print(f"[graphite] route recovery error: {code}", file=sys.stderr)
    return 1


_MODEL_OUTPUT_BEGIN = "----- BEGIN GRAPHITE MODEL OUTPUT -----"
_MODEL_OUTPUT_END = "----- END GRAPHITE MODEL OUTPUT -----"


def _escaped_terminal_text(text: str) -> str:
    characters: list[str] = []
    for character in text:
        codepoint = ord(character)
        category = unicodedata.category(character)
        if character == "\n":
            characters.append(character)
        elif category in {"Cc", "Cf", "Zl", "Zp"} or codepoint == 0x7F:
            characters.append(
                f"\\x{codepoint:02x}" if codepoint <= 0xFF else f"\\u{codepoint:04x}"
            )
        else:
            characters.append(character)
    escaped = "".join(characters)
    return escaped.replace(_MODEL_OUTPUT_BEGIN, "[escaped model delimiter]").replace(
        _MODEL_OUTPUT_END, "[escaped model delimiter]"
    )


def _render_model_output(text: str, *, stdout: TextIO) -> None:
    """Render untrusted provider text without granting terminal control."""
    safe = _escaped_terminal_text(text)
    quoted = "\n".join(f"| {line}" for line in safe.split("\n"))
    framed = f"{_MODEL_OUTPUT_BEGIN}\n{quoted}\n{_MODEL_OUTPUT_END}\n"
    encoding = getattr(stdout, "encoding", None) or "utf-8"
    try:
        framed = framed.encode(encoding, errors="backslashreplace").decode(encoding)
    except LookupError:
        framed = framed.encode("utf-8", errors="backslashreplace").decode("utf-8")
    stdout.write(framed)
    stdout.flush()


def cmd_route_recommend(args: argparse.Namespace) -> int:
    service = RoutingService(args.path)
    recommendation = service.recommend(
        objective=args.objective,
        targets=tuple(args.target or ()),
    )
    _route_print(recommendation.to_dict(), json_mode=args.json)
    return 3 if recommendation.manual_handoff else 0


def cmd_route_run(args: argparse.Namespace) -> int:
    service = RoutingService(args.path)
    recommendation = service.recommend(
        objective=args.objective,
        targets=tuple(args.target or ()),
    )
    public = recommendation.to_dict()
    _route_print(public, json_mode=args.json)
    if recommendation.manual_handoff:
        return 3
    try:
        prepared = service.prepare(recommendation)
    except RoutingServiceError as exc:
        _route_print({"error": {"code": exc.code}}, json_mode=args.json)
        return 1
    _route_print(prepared.to_dict(), json_mode=args.json)
    approved = approval_prompt(
        stdin=sys.stdin,
        stdout=sys.stdout,
        stdin_is_tty=sys.stdin.isatty(),
        stdout_is_tty=sys.stdout.isatty(),
        json_mode=args.json,
        assume_yes=args.yes,
        ci=bool(os.environ.get("CI")),
    )
    if not approved:
        service.decline(prepared)
        return 2
    result = service.run_approved(prepared, approval_granted=True)
    _render_model_output(result.text, stdout=sys.stdout)
    _route_print(result.to_public_dict(), json_mode=False)
    return 0


def _route_terminal_action(args: argparse.Namespace, action: str) -> int:
    approved = approval_prompt(
        stdin=sys.stdin,
        stdout=sys.stdout,
        stdin_is_tty=sys.stdin.isatty(),
        stdout_is_tty=sys.stdout.isatty(),
        json_mode=args.json,
        assume_yes=args.yes,
        ci=bool(os.environ.get("CI")),
    )
    if not approved:
        return 2
    service = RoutingService(args.path)
    try:
        payload = getattr(service, action)(args.task_id, authority_granted=True)
    except RoutingServiceError as exc:
        _route_print({"error": {"code": exc.code}}, json_mode=args.json)
        return 1
    _route_print(payload, json_mode=args.json)
    return 0


def cmd_route_accept(args: argparse.Namespace) -> int:
    return _route_terminal_action(args, "accept")


def cmd_route_reject(args: argparse.Namespace) -> int:
    return _route_terminal_action(args, "reject")


def cmd_route_cleanup(args: argparse.Namespace) -> int:
    return _route_terminal_action(args, "cleanup")


def cmd_route_review(args: argparse.Namespace) -> int:
    service = RoutingService(args.path)
    try:
        prepared = service.prepare_review(args.task_id)
    except RoutingServiceError as exc:
        _route_print({"error": {"code": exc.code}}, json_mode=args.json)
        return 1
    _route_print(prepared.to_dict(), json_mode=args.json)
    approved = approval_prompt(
        stdin=sys.stdin,
        stdout=sys.stdout,
        stdin_is_tty=sys.stdin.isatty(),
        stdout_is_tty=sys.stdout.isatty(),
        json_mode=args.json,
        assume_yes=args.yes,
        ci=bool(os.environ.get("CI")),
    )
    if not approved:
        service.decline(prepared)
        return 2
    result = service.run_review_approved(prepared, approval_granted=True)
    _render_model_output(result.text, stdout=sys.stdout)
    _route_print(result.to_public_dict(), json_mode=False)
    return 0


def cmd_route_status(args: argparse.Namespace) -> int:
    _route_print(RoutingService(args.path).status(), json_mode=args.json)
    return 0


def cmd_route_recoverable(args: argparse.Namespace) -> int:
    try:
        page = RoutingService(args.path).recoverable_attempts(
            limit=args.limit, after=args.after
        )
    except (StorageError, RoutingServiceError, ValueError, OSError) as exc:
        return _route_recovery_error(exc, json_mode=args.json)
    _route_print(page.to_dict(), json_mode=args.json)
    return 0


def cmd_route_reconcile(args: argparse.Namespace) -> int:
    try:
        payload = RoutingService(args.path).reconcile_execution(args.attempt_id)
    except (StorageError, RoutingServiceError, ValueError, OSError) as exc:
        return _route_recovery_error(exc, json_mode=args.json)
    _route_print(payload, json_mode=args.json)
    return 0


def cmd_route_policy(args: argparse.Namespace) -> int:
    authority_granted = False
    if args.promote or args.rollback:
        authority_granted = approval_prompt(
            stdin=sys.stdin,
            stdout=sys.stdout,
            stdin_is_tty=sys.stdin.isatty(),
            stdout_is_tty=sys.stdout.isatty(),
            json_mode=args.json,
            assume_yes=False,
            ci=bool(os.environ.get("CI")),
        )
        if not authority_granted:
            return 2
    try:
        payload = RoutingService(args.path).policy(
            promote=args.promote,
            rollback=args.rollback,
            authority_granted=authority_granted,
        )
    except (StorageError, RoutingServiceError, ValueError, OSError) as exc:
        return _route_recovery_error(exc, json_mode=args.json)
    _route_print(payload, json_mode=args.json)
    return 0


def cmd_route_record_outcome(args: argparse.Namespace) -> int:
    if args.provenance in {"machine_verified", "ci_imported"} and not args.evidence_file:
        print("[graphite] supported evidence import required", file=sys.stderr)
        return 6
    payload = RoutingService(args.path).record_outcome(
        execution_id=args.execution_id,
        provenance=args.provenance,
        accepted=args.accepted,
        evidence_file=args.evidence_file,
    )
    _route_print(payload, json_mode=args.json)
    return 0


def _lifecycle_result(args: argparse.Namespace, operation: str, **kwargs: Any) -> int:
    try:
        payload = getattr(LifecycleOperator(args.path), operation)(**kwargs)
    except (LifecycleOperatorError, ValueError, OSError) as exc:
        code = getattr(exc, "code", "lifecycle_operator_invalid")
        _route_print({"error": {"code": code}}, json_mode=args.json)
        return 1
    _route_print(payload, json_mode=args.json)
    return 0


def cmd_lifecycle_list(args: argparse.Namespace) -> int:
    return _lifecycle_result(args, "list_observations", limit=args.limit)


def cmd_lifecycle_status(args: argparse.Namespace) -> int:
    return _lifecycle_result(args, "status", boundary_digest=args.boundary_digest)


def cmd_lifecycle_history(args: argparse.Namespace) -> int:
    return _lifecycle_result(
        args, "history", boundary_digest=args.boundary_digest, limit=args.limit
    )


def cmd_lifecycle_policy_inspect(args: argparse.Namespace) -> int:
    return _lifecycle_result(
        args, "inspect_policy", boundary_digest=args.boundary_digest
    )


def cmd_lifecycle_policy_prepare(args: argparse.Namespace) -> int:
    return _lifecycle_result(
        args,
        "prepare_policy_promotion",
        boundary_digest=args.boundary_digest,
        lifecycle_identity_digest=args.lifecycle_identity_digest,
        proposed_policy_version=args.proposed_policy_version,
        minimum_version=args.minimum_version,
        maximum_version_exclusive=args.maximum_version_exclusive,
        required_capabilities=tuple(args.required_capability),
        prepared_at=args.prepared_at,
    )


def cmd_lifecycle_verification_prepare(args: argparse.Namespace) -> int:
    return _lifecycle_result(
        args,
        "prepare_verification_manifest",
        boundary_digest=args.boundary_digest,
        lifecycle_identity_digest=args.lifecycle_identity_digest,
        requested_model=args.requested_model,
        expected_effective_model=args.expected_effective_model,
        effort=Effort(args.effort),
        max_input_tokens=args.max_input_tokens,
        max_output_tokens=args.max_output_tokens,
        timeout_seconds=args.timeout_seconds,
        expires_at=args.expires_at,
        fixture_repository_commit=args.fixture_repository_commit,
        graph_fingerprint=args.graph_fingerprint,
        prompt_contract_hash=args.prompt_contract_hash,
        response_contract_hash=args.response_contract_hash,
        max_cost_microunits=args.max_cost_microunits,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graphite", description="Local-first code knowledge graph.")
    parser.add_argument("--output-dir", default=None, help="Output directory (default: graph-out)")
    parser.add_argument("--cache-dir", default=None, help="Cache directory (default: .cache/graphite)")
    parser.add_argument("--workers", type=int, default=None, help="Parallel workers")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument("--typescript-resolver", choices=["auto", "compiler", "heuristic", "disabled"], default=None, help="TypeScript resolver mode (default: auto)")
    parser.add_argument("--typescript-resolver-timeout", type=float, default=None, help="TypeScript compiler resolver timeout in seconds")
    parser.add_argument("--no-typescript-symbol-references", action="store_true", help="Disable TypeScript compiler symbol-reference edges")
    parser.add_argument("--llm", choices=["none", "auto", "local", "cloud"], default=None, help="Optional integration mode; canonical graph commands accept only none")
    parser.add_argument("--llm-provider", default=None, help="Provider for explicit doctor/overlay operations; rejected by canonical commands")
    parser.add_argument("--llm-model", default=None, help="Model for explicit doctor/overlay operations")
    parser.add_argument("--llm-base-url", default=None, help="Provider base URL for explicit doctor/overlay operations")
    parser.add_argument("--llm-api-key", default=None, help="Provider key for explicit doctor/overlay operations; prefer a session-scoped environment value")
    parser.add_argument("--llm-timeout", type=float, default=None, help="Provider timeout for explicit doctor/overlay operations")
    parser.add_argument("--llm-max-input-chars", type=int, default=None, help="Maximum explicit overlay input characters")
    parser.add_argument("--llm-max-output-tokens", type=int, default=None, help="Maximum explicit overlay output tokens")

    sub = parser.add_subparsers(dest="command")

    p_lifecycle = sub.add_parser(
        "lifecycle", help="Inspect provider lifecycle authority and prepare bounded candidates"
    )
    lifecycle_sub = p_lifecycle.add_subparsers(
        dest="lifecycle_command", required=True
    )

    p_lifecycle_list = lifecycle_sub.add_parser(
        "list", help="List bounded current lifecycle observations"
    )
    p_lifecycle_list.add_argument("path", help="Repository path")
    p_lifecycle_list.add_argument("--limit", type=int, default=50)
    p_lifecycle_list.add_argument("--json", action="store_true")
    p_lifecycle_list.set_defaults(func=cmd_lifecycle_list)

    for name, handler in (
        ("status", cmd_lifecycle_status),
        ("history", cmd_lifecycle_history),
    ):
        lifecycle_read = lifecycle_sub.add_parser(
            name, help=f"Read lifecycle {name}"
        )
        lifecycle_read.add_argument("path", help="Repository path")
        lifecycle_read.add_argument("--boundary-digest", required=True)
        if name == "history":
            lifecycle_read.add_argument("--limit", type=int, default=50)
        lifecycle_read.add_argument("--json", action="store_true")
        lifecycle_read.set_defaults(func=handler)

    p_lifecycle_policy = lifecycle_sub.add_parser(
        "policy", help="Inspect policy binding or prepare a non-activating promotion"
    )
    lifecycle_policy_sub = p_lifecycle_policy.add_subparsers(
        dest="lifecycle_policy_command", required=True
    )
    p_lifecycle_policy_inspect = lifecycle_policy_sub.add_parser(
        "inspect", help="Inspect persisted policy binding"
    )
    p_lifecycle_policy_inspect.add_argument("path", help="Repository path")
    p_lifecycle_policy_inspect.add_argument("--boundary-digest", required=True)
    p_lifecycle_policy_inspect.add_argument("--json", action="store_true")
    p_lifecycle_policy_inspect.set_defaults(func=cmd_lifecycle_policy_inspect)

    p_lifecycle_policy_prepare = lifecycle_policy_sub.add_parser(
        "prepare", help="Prepare a policy promotion candidate without activating it"
    )
    p_lifecycle_policy_prepare.add_argument("path", help="Repository path")
    p_lifecycle_policy_prepare.add_argument("--boundary-digest", required=True)
    p_lifecycle_policy_prepare.add_argument(
        "--lifecycle-identity-digest", required=True
    )
    p_lifecycle_policy_prepare.add_argument(
        "--proposed-policy-version", required=True
    )
    p_lifecycle_policy_prepare.add_argument("--minimum-version", required=True)
    p_lifecycle_policy_prepare.add_argument(
        "--maximum-version-exclusive", required=True
    )
    p_lifecycle_policy_prepare.add_argument(
        "--required-capability", action="append", required=True
    )
    p_lifecycle_policy_prepare.add_argument("--prepared-at", type=int, required=True)
    p_lifecycle_policy_prepare.add_argument("--json", action="store_true")
    p_lifecycle_policy_prepare.set_defaults(func=cmd_lifecycle_policy_prepare)

    p_lifecycle_verification = lifecycle_sub.add_parser(
        "verification", help="Prepare an exact verification manifest"
    )
    lifecycle_verification_sub = p_lifecycle_verification.add_subparsers(
        dest="lifecycle_verification_command", required=True
    )
    p_lifecycle_verification_prepare = lifecycle_verification_sub.add_parser(
        "prepare", help="Prepare a manifest without invoking a provider"
    )
    p_lifecycle_verification_prepare.add_argument("path", help="Repository path")
    p_lifecycle_verification_prepare.add_argument("--boundary-digest", required=True)
    p_lifecycle_verification_prepare.add_argument(
        "--lifecycle-identity-digest", required=True
    )
    p_lifecycle_verification_prepare.add_argument("--requested-model", required=True)
    p_lifecycle_verification_prepare.add_argument(
        "--expected-effective-model", required=True
    )
    p_lifecycle_verification_prepare.add_argument(
        "--effort", choices=[value.value for value in Effort], required=True
    )
    for option in (
        "max-input-tokens", "max-output-tokens", "timeout-seconds", "expires-at"
    ):
        p_lifecycle_verification_prepare.add_argument(
            f"--{option}", type=int, required=True
        )
    p_lifecycle_verification_prepare.add_argument(
        "--fixture-repository-commit", required=True
    )
    p_lifecycle_verification_prepare.add_argument("--graph-fingerprint", required=True)
    p_lifecycle_verification_prepare.add_argument(
        "--prompt-contract-hash", required=True
    )
    p_lifecycle_verification_prepare.add_argument(
        "--response-contract-hash", required=True
    )
    p_lifecycle_verification_prepare.add_argument(
        "--max-cost-microunits", type=int, default=None
    )
    p_lifecycle_verification_prepare.add_argument("--json", action="store_true")
    p_lifecycle_verification_prepare.set_defaults(
        func=cmd_lifecycle_verification_prepare
    )

    p_route = sub.add_parser("route", help="Recommend or run approval-gated model routing")
    route_sub = p_route.add_subparsers(dest="route_command", required=True)

    p_route_recommend = route_sub.add_parser("recommend", help="Compute an offline recommendation")
    p_route_recommend.add_argument("path", help="Repository path")
    p_route_recommend.add_argument("--objective", required=True, help="Bounded task objective")
    p_route_recommend.add_argument("--target", action="append", default=[], help="Repository-relative target")
    p_route_recommend.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_route_recommend.set_defaults(func=cmd_route_recommend)

    p_route_run = route_sub.add_parser(
        "run", help="Prepare and run one separately approved authenticated CLI task"
    )
    p_route_run.add_argument("path", help="Repository path")
    p_route_run.add_argument("--objective", required=True, help="Bounded task objective")
    p_route_run.add_argument("--target", action="append", default=[], help="Repository-relative target")
    p_route_run.add_argument("--yes", action="store_true", help="Never grants routing consent; interactive approval is still required")
    p_route_run.add_argument("--json", action="store_true", help="Non-interactive output; execution is disabled")
    p_route_run.set_defaults(func=cmd_route_run)

    for action, handler in (
        ("accept", cmd_route_accept),
        ("reject", cmd_route_reject),
        ("cleanup", cmd_route_cleanup),
    ):
        action_parser = route_sub.add_parser(
            action, help=f"Explicitly {action} one prepared routing task"
        )
        action_parser.add_argument("path", help="Repository path")
        action_parser.add_argument("--task-id", required=True)
        action_parser.add_argument(
            "--yes",
            action="store_true",
            help="Never grants consent; interactive approval is still required",
        )
        action_parser.add_argument(
            "--json", action="store_true", help="Non-interactive output; action is disabled"
        )
        action_parser.set_defaults(func=handler)

    p_route_review = route_sub.add_parser(
        "review", help="Run a separately approved read-only other-provider review"
    )
    p_route_review.add_argument("path", help="Repository path")
    p_route_review.add_argument("--task-id", required=True)
    p_route_review.add_argument(
        "--yes",
        action="store_true",
        help="Never grants consent; interactive approval is still required",
    )
    p_route_review.add_argument(
        "--json", action="store_true", help="Non-interactive output; review is disabled"
    )
    p_route_review.set_defaults(func=cmd_route_review)

    p_route_outcome = route_sub.add_parser("record-outcome", help="Append supported outcome evidence")
    p_route_outcome.add_argument("path", help="Repository path")
    p_route_outcome.add_argument("--execution-id", required=True)
    p_route_outcome.add_argument(
        "--provenance", required=True,
        choices=["machine_verified", "ci_imported", "human", "pairwise", "reversion", "ambiguous"],
    )
    p_route_outcome.add_argument("--accepted", action="store_true")
    p_route_outcome.add_argument("--evidence-file", default=None)
    p_route_outcome.add_argument("--json", action="store_true")
    p_route_outcome.set_defaults(func=cmd_route_record_outcome)

    p_route_status = route_sub.add_parser("status", help="Read local routing readiness")
    p_route_status.add_argument("path", help="Repository path")
    p_route_status.add_argument("--json", action="store_true")
    p_route_status.set_defaults(func=cmd_route_status)

    p_route_recoverable = route_sub.add_parser(
        "recoverable", help="List staged execution attempts eligible for reconciliation"
    )
    p_route_recoverable.add_argument("path", help="Repository path")
    p_route_recoverable.add_argument(
        "--limit", type=int, default=DEFAULT_RECOVERY_PAGE_SIZE,
        help="Page size from 1 to 100 (default: 50)",
    )
    p_route_recoverable.add_argument(
        "--after", default=None, help="Validated attempt ID cursor from next_cursor"
    )
    p_route_recoverable.add_argument("--json", action="store_true")
    p_route_recoverable.set_defaults(func=cmd_route_recoverable)

    p_route_reconcile = route_sub.add_parser(
        "reconcile", help="Finalize one staged receipt without another provider call"
    )
    p_route_reconcile.add_argument("path", help="Repository path")
    p_route_reconcile.add_argument("--attempt-id", required=True)
    p_route_reconcile.add_argument("--json", action="store_true")
    p_route_reconcile.set_defaults(func=cmd_route_reconcile)

    p_route_policy = route_sub.add_parser("policy", help="Inspect or explicitly manage recommendation policy")
    p_route_policy.add_argument("path", help="Repository path")
    p_route_policy.add_argument("--promote", default=None)
    p_route_policy.add_argument("--rollback", default=None)
    p_route_policy.add_argument("--json", action="store_true")
    p_route_policy.set_defaults(func=cmd_route_policy)

    p_overlay = sub.add_parser(
        "overlay",
        help="Manage explicit non-authoritative model overlays",
    )
    overlay_sub = p_overlay.add_subparsers(dest="overlay_command", required=True)
    p_overlay_build = overlay_sub.add_parser(
        "build",
        help="Build one identity-bound overlay from a fresh canonical graph",
    )
    p_overlay_build.add_argument("path", help="Repository path")
    p_overlay_build.add_argument(
        "--provider-identity-digest",
        required=True,
        help="Exact current provider lifecycle identity SHA-256",
    )
    p_overlay_build.add_argument(
        "--model-identity-digest",
        required=True,
        help="Exact current model identity SHA-256",
    )
    p_overlay_build.add_argument(
        "--routing-policy-digest",
        default=None,
        help="Exact OpenRouter routing-policy SHA-256",
    )
    p_overlay_build.add_argument(
        "--json", action="store_true", help="Emit sanitized machine-readable output"
    )
    p_overlay_build.set_defaults(func=cmd_overlay_build)

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
    p_check.add_argument(
        "--ignore-engine",
        action="store_true",
        help="Report source drift only; treat graphite engine updates as fresh",
    )
    p_check.set_defaults(func=cmd_check)

    p_doctor = sub.add_parser(
        "doctor",
        help="Check Graphite core and optional integration readiness",
        description="Check Graphite core and optional integration readiness",
    )
    p_doctor.add_argument(
        "path", nargs="?", default=".", help="Project path (default: current directory)"
    )
    p_doctor.add_argument(
        "--daemon-base", default=None, help="Daemon base folder (default: auto-detect)"
    )
    p_doctor.add_argument(
        "--deep", action="store_true", help="Run bounded functional probes in temporary storage"
    )
    p_doctor.add_argument(
        "--include-llm",
        action="store_true",
        help="With --deep, run one synthetic LLM connectivity probe",
    )
    p_doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_doctor.set_defaults(func=cmd_doctor)


    p_init = sub.add_parser("init", aliases=["Init"], help="Initialize Graphite instructions for AI coding platforms")
    p_init.add_argument("path", nargs="?", default=".", help="Project path (default: current directory)")
    p_init.add_argument("--platform", action="append", default=[], help="Platform to configure: codex, claude, antigravity, visual-studio, cursor, windsurf, or all. Can be repeated or comma-separated.")
    p_init.add_argument("--all", action="store_true", help="Configure every supported platform")
    p_init.add_argument("--yes", action="store_true", help="Use default platforms and suppress optional dependency prompts")
    p_init.add_argument("--daemon-base", default=None, help="Daemon base folder for visibility check, default auto-detects F:/Projects")
    p_init.add_argument("--no-build", action="store_true", help="Only update instruction files; do not build graph")
    p_init.add_argument("--no-validate", action="store_true", help="Skip graph validation after init")
    p_init.add_argument("--list-platforms", action="store_true", help="Print supported platform keys and exit")
    p_init.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_init_mode = p_init.add_mutually_exclusive_group()
    p_init_mode.add_argument("--strict", action="store_true", help="Write strict-mode graphite-first hook wiring (denies provable relationship greps)")
    p_init_mode.add_argument("--remind", action="store_true", help="Write remind-mode hook wiring (non-blocking reminders; default for first-time wiring)")
    p_init.add_argument("--no-agent-hooks", action="store_true", help="Skip Claude Code hook wiring in .claude/settings.json")
    p_init.set_defaults(func=cmd_init)

    p_bootstrap = sub.add_parser("bootstrap", help="Make a project Graphite-ready and optionally build its graph")
    p_bootstrap.add_argument("path", help="Project path")
    p_bootstrap.add_argument("--daemon-base", default=None, help="Daemon base folder for visibility check, default auto-detects F:/Projects")
    p_bootstrap.add_argument("--no-build", action="store_true", help="Only update project workflow files; do not build graph")
    p_bootstrap.add_argument("--no-validate", action="store_true", help="Skip graph validation after bootstrap")
    p_bootstrap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_bootstrap.add_argument("--yes", action="store_true", help="Run non-interactively and never offer dependency installation")
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
    verb_lines = []
    for verb in verb_catalog():
        arguments = f" {verb['arguments']}" if verb["arguments"] else ""
        aliases = f" (aliases: {', '.join(verb['aliases'])})" if verb["aliases"] else ""
        verb_lines.append(f"  {verb['name']}{arguments}{aliases}: {verb['description']}")
    p_query = sub.add_parser(
        "query",
        help="Query an existing graph.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="supported verbs:\n"
        + "\n".join(verb_lines)
        + '\n\nfor free-text lookup use: graphite search "<symbol, path, or concept>"'
        + '\nfor questions use: graphite query --natural "who calls X"'
        + " (fixed deterministic grammar; list it via graphite capabilities --json)",
    )
    p_query.add_argument("query", help="Query string, e.g. 'depends-on db.ts'")
    p_query.add_argument("--graph-json", default="graph-out/graph.json", help="Path to graph.json")
    p_query.add_argument(
        "--show-plan", action="store_true",
        help="Include the canonical query plan in the JSON output",
    )
    p_query.add_argument(
        "--plan-only", action="store_true",
        help="Validate and print the query plan without loading the graph or executing",
    )
    p_query.add_argument(
        "--natural", action="store_true",
        help="Interpret the query as a question via a fixed deterministic grammar (no inference; see capabilities)",
    )
    p_query.set_defaults(func=cmd_query)

    p_search = sub.add_parser("search", help="Deterministic ranked node search by symbol, path, or concept")
    p_search.add_argument("text", help="Symbol, path, or concept to search for")
    p_search.add_argument("--graph-json", default="graph-out/graph.json", help="Path to graph.json")
    p_search.add_argument("--limit", type=int, default=DEFAULT_SEARCH_LIMIT, help="Maximum results")
    p_search.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_search.set_defaults(func=cmd_search)

    p_capabilities = sub.add_parser("capabilities", help="List supported operations, query verbs, and limits")
    p_capabilities.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_capabilities.set_defaults(func=cmd_capabilities)

    p_agent_hook = sub.add_parser(
        "agent-hook",
        help="Claude Code hook endpoint for graphite-first enforcement (reads hook JSON on stdin; always exits 0)",
    )
    p_agent_hook.add_argument("event", choices=["session-start", "pre-tool-use"], help="Hook event to handle")
    p_agent_hook.add_argument("--mode", choices=["remind", "strict"], default="remind", help="pre-tool-use enforcement mode")
    p_agent_hook.set_defaults(func=cmd_agent_hook)

    p_impact = sub.add_parser("impact", help="Suggest impacted files and tests for changed files")
    p_impact.add_argument("files", nargs="+", help="Changed file paths or graph node fragments")
    p_impact.add_argument("--graph-json", default="graph-out/graph.json", help="Path to graph.json")
    p_impact.add_argument("--depth", type=int, default=2, help="Reverse dependency traversal depth")
    p_impact.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_impact.set_defaults(func=cmd_impact)

    p_review = sub.add_parser(
        "review-changes",
        help="Produce deterministic review evidence and acceptance criteria",
        description="Produce deterministic review evidence and acceptance criteria",
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
        "--depth", type=_review_depth, default=2, help="Reverse dependency traversal depth"
    )
    p_review.add_argument(
        "--git-timeout",
        type=_review_git_timeout,
        default=5.0,
        help="Git discovery timeout in seconds",
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
    if args.command == "doctor" and args.include_llm and not args.deep:
        p_doctor.error("--include-llm requires --deep")
    if args.command in _LLM_GATED_COMMANDS and (
        getattr(args, "llm", None) not in (None, "none")
        or any(getattr(args, name, None) is not None for name in _LEGACY_LLM_ARGUMENTS)
    ):
        print(CANONICAL_ENRICHMENT_MIGRATION_MESSAGE, file=sys.stderr)
        return 2

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
