"""Validation for Graphite graph artifacts."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any

_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    path: str


def validate_graph_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Validate the public graph.json bundle and return a machine-readable report."""
    issues: list[ValidationIssue] = []

    nodes = bundle.get("nodes")
    edges = bundle.get("edges")
    metadata = bundle.get("metadata")
    clusters = bundle.get("clusters", [])
    analysis = bundle.get("analysis", {})

    if not isinstance(nodes, list):
        issues.append(_error("nodes_type", "nodes must be a list", "nodes"))
        nodes = []
    if not isinstance(edges, list):
        issues.append(_error("edges_type", "edges must be a list", "edges"))
        edges = []
    if not isinstance(metadata, dict):
        issues.append(_error("metadata_type", "metadata must be an object", "metadata"))
        metadata = {}
    if not isinstance(clusters, list):
        issues.append(_warn("clusters_type", "clusters should be a list", "clusters"))
        clusters = []
    if not isinstance(analysis, dict):
        issues.append(_warn("analysis_type", "analysis should be an object", "analysis"))

    node_ids: set[str] = set()
    for index, node in enumerate(nodes):
        path = f"nodes[{index}]"
        if not isinstance(node, dict):
            issues.append(_error("node_type", "node must be an object", path))
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            issues.append(_error("node_id_missing", "node id must be a non-empty string", f"{path}.id"))
            continue
        if node_id in node_ids:
            issues.append(_error("node_id_duplicate", f"duplicate node id: {node_id}", f"{path}.id"))
        node_ids.add(node_id)
        source_file = node.get("source_file")
        if isinstance(source_file, str) and _is_absolute_or_unsafe_path(source_file):
            issues.append(_error("absolute_source_file", f"source_file must be project-relative: {source_file}", f"{path}.source_file"))

    seen_edges: set[tuple[str, str, str]] = set()
    for index, edge in enumerate(edges):
        path = f"edges[{index}]"
        if not isinstance(edge, dict):
            issues.append(_error("edge_type", "edge must be an object", path))
            continue
        source = edge.get("source")
        target = edge.get("target")
        relation = edge.get("relation")
        if not isinstance(source, str) or not source:
            issues.append(_error("edge_source_missing", "edge source must be a non-empty string", f"{path}.source"))
            continue
        if not isinstance(target, str) or not target:
            issues.append(_error("edge_target_missing", "edge target must be a non-empty string", f"{path}.target"))
            continue
        if not isinstance(relation, str) or not relation:
            issues.append(_error("edge_relation_missing", "edge relation must be a non-empty string", f"{path}.relation"))
            continue
        if source not in node_ids:
            issues.append(_error("edge_source_unknown", f"edge source does not exist: {source}", f"{path}.source"))
        if target not in node_ids:
            issues.append(_error("edge_target_unknown", f"edge target does not exist: {target}", f"{path}.target"))
        key = (source, target, relation)
        if key in seen_edges:
            issues.append(_warn("edge_duplicate", f"duplicate edge: {source} -> {target} ({relation})", path))
        seen_edges.add(key)
        source_file = edge.get("source_file")
        if isinstance(source_file, str) and _is_absolute_or_unsafe_path(source_file):
            issues.append(_error("absolute_edge_source_file", f"edge source_file must be project-relative: {source_file}", f"{path}.source_file"))

    expected_nodes = metadata.get("node_count")
    expected_edges = metadata.get("edge_count")
    if isinstance(expected_nodes, int) and expected_nodes != len(nodes):
        issues.append(_error("metadata_node_count", f"metadata node_count {expected_nodes} != actual {len(nodes)}", "metadata.node_count"))
    if isinstance(expected_edges, int) and expected_edges != len(edges):
        issues.append(_error("metadata_edge_count", f"metadata edge_count {expected_edges} != actual {len(edges)}", "metadata.edge_count"))

    for cidx, cluster in enumerate(clusters):
        if not isinstance(cluster, dict):
            issues.append(_warn("cluster_type", "cluster should be an object", f"clusters[{cidx}]"))
            continue
        members = cluster.get("members", [])
        if not isinstance(members, list):
            issues.append(_warn("cluster_members_type", "cluster members should be a list", f"clusters[{cidx}].members"))
            continue
        for midx, member in enumerate(members):
            if isinstance(member, str) and member not in node_ids:
                issues.append(_warn("cluster_member_unknown", f"cluster member does not exist: {member}", f"clusters[{cidx}].members[{midx}]"))

    if not nodes:
        issues.append(_warn("empty_graph", "graph contains no nodes", "nodes"))

    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    return {
        "ok": not errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "errors": [asdict(issue) for issue in errors],
        "warnings": [asdict(issue) for issue in warnings],
    }


def assert_valid_graph_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Validate or raise a concise ValueError suitable for build failure."""
    report = validate_graph_bundle(bundle)
    if not report["ok"]:
        first = report["errors"][0]
        raise ValueError(f"graph validation failed: {first['code']}: {first['message']} at {first['path']}")
    return report


def _error(code: str, message: str, path: str) -> ValidationIssue:
    return ValidationIssue("error", code, message, path)


def _warn(code: str, message: str, path: str) -> ValidationIssue:
    return ValidationIssue("warning", code, message, path)


def _is_absolute_or_unsafe_path(value: str) -> bool:
    if _WINDOWS_ABS_RE.match(value) or value.startswith(("/", "\\")):
        return True
    parts = PurePosixPath(value.replace("\\", "/")).parts
    return any(part == ".." for part in parts)
