"""Audit whether Graphite is ready to replace legacy Graphify usage in a project."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .bootstrap import GRAPHITE_AGENTS_HEADER, daemon_visibility
from .config import Config
from .daemon_health import HealthOptions, evaluate_daemon_health
from .ingest import collect_files
from .validation import validate_graph_bundle

GRAPHIFY_PATH_PATTERNS: tuple[str, ...] = (
    "graphify",
    "graphify-out",
    "graphify-out/**",
    "graphify/**",
    "scripts/graphify_*.py",
    "**/graphify_*.py",
    "**/*graphify*",
)

GRAPHIFY_TEXT_FILES: tuple[str, ...] = (
    ".gitignore",
    "AGENTS.md",
    "README.md",
    "package.json",
    "pyproject.toml",
)

GRAPHITE_GITIGNORE_REQUIRED: tuple[str, ...] = (
    "graph-out/",
    ".cache/graphite/",
)

HealthChecker = Callable[[Path, Path | None], dict[str, Any]]


def audit_replacement(
    project_root: Path,
    *,
    daemon_base: Path | None = None,
    cfg: Config | None = None,
    health_checker: HealthChecker | None = None,
) -> dict[str, Any]:
    """Return a machine-readable Graphify-to-Graphite replacement audit."""
    root = project_root.resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    runtime_cfg = cfg or Config()
    graph = _graph_status(root, runtime_cfg)
    bootstrap = _bootstrap_status(root)
    daemon = daemon_visibility(root, daemon_base=daemon_base)
    health = _health_status(daemon, daemon_base, health_checker)
    graphify = _graphify_status(root)

    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    recommendations: list[str] = []

    if not bootstrap["agents_configured"]:
        blockers.append({"code": "agents_not_configured", "message": "AGENTS.md does not include the Graphite auto-consult workflow"})
    if not bootstrap["gitignore_configured"]:
        blockers.append({"code": "gitignore_not_configured", "message": "Graphite outputs are not fully ignored in .gitignore"})
    if not graph["exists"]:
        blockers.append({"code": "graph_missing", "message": "graph-out/graph.json does not exist"})
    elif not graph["valid"]:
        blockers.append({"code": "graph_invalid", "message": "graph-out/graph.json failed validation", "validation": graph.get("validation")})
    elif graph["stale"]:
        blockers.append({"code": "graph_stale", "message": "Graphite graph is stale", "changes": graph.get("changes")})
    if not daemon.get("project_listed"):
        blockers.append({"code": "daemon_project_missing", "message": "Graphite daemon status does not list this project"})
    if health.get("checked") and not health.get("ok"):
        blockers.append({"code": "daemon_health_not_ok", "message": "Graphite daemon health check is not ok", "health": health})

    if graphify["existing_paths"]:
        warnings.append({
            "code": "graphify_paths_exist",
            "message": "Graphify files or directories still exist in the project",
            "paths": graphify["existing_paths"],
        })
    if graphify["text_references"]:
        warnings.append({
            "code": "graphify_text_references",
            "message": "Graphify references remain in project text/config files",
            "references": graphify["text_references"],
        })
    if graphify["gitignore_entries"]:
        warnings.append({
            "code": "graphify_gitignore_entries",
            "message": "Legacy Graphify ignore entries remain",
            "entries": graphify["gitignore_entries"],
        })

    if blockers:
        recommendations.append("Resolve blockers before removing Graphify remnants.")
    if graphify["existing_paths"]:
        recommendations.append("Inspect existing Graphify files/directories and migrate any still-useful scripts or reports before deletion.")
    elif graphify["gitignore_entries"]:
        recommendations.append("Graphify ignore entries appear to be legacy-only; remove them after confirming no external workflow still writes Graphify outputs.")
    if not warnings and not blockers:
        recommendations.append("Graphite appears ready to fully replace Graphify for this project.")

    return {
        "ok": not blockers,
        "replacement_ready": not blockers and not graphify["existing_paths"],
        "project_root": str(root),
        "blockers": blockers,
        "warnings": warnings,
        "recommendations": recommendations,
        "graphite": {
            "bootstrap": bootstrap,
            "graph": graph,
            "daemon": daemon,
            "health": health,
        },
        "graphify": graphify,
    }


def format_replacement_audit(report: dict[str, Any]) -> str:
    lines = [
        f"[graphite] replacement audit: {'ready' if report['replacement_ready'] else 'not ready'}",
        f"  project: {report['project_root']}",
        f"  blockers: {len(report['blockers'])}",
        f"  warnings: {len(report['warnings'])}",
    ]
    if report["blockers"]:
        lines.append("Blockers:")
        for item in report["blockers"]:
            lines.append(f"  - {item['code']}: {item['message']}")
    if report["warnings"]:
        lines.append("Warnings:")
        for item in report["warnings"]:
            lines.append(f"  - {item['code']}: {item['message']}")
    graph = report["graphite"]["graph"]
    lines.append("Graphite:")
    lines.append(f"  - graph exists: {graph['exists']}")
    lines.append(f"  - graph valid: {graph['valid']}")
    lines.append(f"  - graph stale: {graph['stale']}")
    daemon = report["graphite"]["daemon"]
    lines.append(f"  - daemon listed: {daemon.get('project_listed')}")
    health = report["graphite"]["health"]
    if health.get("checked"):
        lines.append(f"  - daemon health: {health.get('status')} ({'ok' if health.get('ok') else 'attention required'})")
    graphify = report["graphify"]
    lines.append("Graphify remnants:")
    lines.append(f"  - existing paths: {len(graphify['existing_paths'])}")
    lines.append(f"  - text references: {len(graphify['text_references'])}")
    lines.append(f"  - gitignore entries: {len(graphify['gitignore_entries'])}")
    if report["recommendations"]:
        lines.append("Recommendations:")
        for item in report["recommendations"]:
            lines.append(f"  - {item}")
    return "\n".join(lines) + "\n"


def _bootstrap_status(root: Path) -> dict[str, Any]:
    gitignore = root / ".gitignore"
    agents = root / "AGENTS.md"
    gitignore_text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    agents_text = agents.read_text(encoding="utf-8") if agents.exists() else ""
    missing_gitignore = [entry for entry in GRAPHITE_GITIGNORE_REQUIRED if entry not in gitignore_text]
    return {
        "gitignore_exists": gitignore.exists(),
        "gitignore_configured": not missing_gitignore,
        "gitignore_missing": missing_gitignore,
        "agents_exists": agents.exists(),
        "agents_configured": GRAPHITE_AGENTS_HEADER in agents_text,
    }


def _graph_status(root: Path, cfg: Config) -> dict[str, Any]:
    graph_path = root / cfg.output_dir / "graph.json" if not cfg.output_dir.is_absolute() else cfg.output_dir / "graph.json"
    manifest_path = root / cfg.output_dir / ".graphite_manifest.json" if not cfg.output_dir.is_absolute() else cfg.output_dir / ".graphite_manifest.json"
    payload: dict[str, Any] = {
        "path": str(graph_path),
        "exists": graph_path.exists(),
        "valid": False,
        "stale": True,
    }
    if graph_path.exists():
        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            validation = validate_graph_bundle(graph)
            payload["validation"] = validation
            payload["valid"] = bool(validation.get("ok"))
            payload["node_count"] = validation.get("node_count")
            payload["edge_count"] = validation.get("edge_count")
        except (OSError, json.JSONDecodeError) as exc:
            payload["validation"] = {"ok": False, "error": str(exc)}
    payload.update(_freshness(root, manifest_path, cfg))
    return payload


def _freshness(root: Path, manifest_path: Path, cfg: Config) -> dict[str, Any]:
    if not manifest_path.exists():
        return {"stale": True, "stale_reason": "missing manifest", "changes": {"added": [], "changed": [], "removed": []}}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"stale": True, "stale_reason": f"unreadable manifest: {exc}", "changes": {"added": [], "changed": [], "removed": []}}
    current = {entry.rel_path: entry.content_hash for entry in collect_files(root, cfg)}
    previous = {item["rel_path"]: item.get("hash", "") for item in manifest.get("files", []) if "rel_path" in item}
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    changed = sorted(path for path in set(current) & set(previous) if current[path] != previous[path])
    return {
        "stale": bool(added or removed or changed),
        "changes": {"added": added, "changed": changed, "removed": removed},
    }


def _health_status(daemon: dict[str, Any], daemon_base: Path | None, health_checker: HealthChecker | None) -> dict[str, Any]:
    base_text = daemon.get("base")
    if not base_text:
        return {"checked": False, "ok": None, "reason": "daemon base unavailable"}
    base = daemon_base or Path(str(base_text))
    if health_checker:
        health = health_checker(base, None)
    else:
        health = evaluate_daemon_health(base, options=HealthOptions())
    return {"checked": True, **health}


def _graphify_status(root: Path) -> dict[str, Any]:
    existing_paths = _find_graphify_paths(root)
    text_refs = _find_graphify_text_references(root)
    gitignore_entries = [ref for ref in text_refs if ref["file"] == ".gitignore"]
    return {
        "existing_paths": existing_paths,
        "text_references": text_refs,
        "gitignore_entries": gitignore_entries,
    }


def _find_graphify_paths(root: Path) -> list[str]:
    matches: set[str] = set()
    for pattern in GRAPHIFY_PATH_PATTERNS:
        for path in root.glob(pattern):
            if path == root:
                continue
            rel = path.relative_to(root).as_posix()
            if rel in (".gitignore", "AGENTS.md"):
                continue
            matches.add(rel + ("/" if path.is_dir() else ""))
    return sorted(matches)


def _find_graphify_text_references(root: Path) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for rel in GRAPHIFY_TEXT_FILES:
        path = root / rel
        if not path.exists() or not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            if "graphify" in line.lower():
                refs.append({"file": rel, "line": index, "text": line.strip()})
    return refs

