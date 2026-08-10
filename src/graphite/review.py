"""Safe collection of explicit and Git-reported review changes."""

from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .context import build_context
from .graph import graph_from_json
from .git import (
    GitLaunchError,
    GitOutputLimitError,
    GitRunner,
    GitTimeoutError,
    GitUnavailableError,
    GitUnsupportedVersionError,
    git_version_failure_message,
)
from .validation import validate_graph_bundle


class ReviewError(ValueError):
    """Raised when review change evidence cannot be collected safely."""


MAX_GIT_STATUS_RECORDS = 100_000
MAX_REVIEW_PACKET_ITEMS = 10_000


@dataclass(frozen=True, order=True)
class Change:
    """A project-relative changed path and its normalized status."""

    path: str
    status: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def normalize_explicit_changes(root: Path, paths: Iterable[str]) -> list[Change]:
    """Normalize user-supplied paths while containing them within *root*."""
    resolved_root = root.resolve()
    normalized: set[str] = set()

    for raw_path in paths:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = resolved_root / candidate
        resolved_path = candidate.resolve()

        try:
            relative_path = resolved_path.relative_to(resolved_root)
        except ValueError as exc:
            raise ReviewError("path is outside project root") from exc

        if relative_path == Path("."):
            raise ReviewError("a change path cannot be the project root")
        normalized.add(relative_path.as_posix())

    return [Change(path, "explicit") for path in sorted(normalized)]


def discover_git_changes(root: Path, *, timeout_seconds: float = 5.0) -> list[Change]:
    """Collect normalized changes from Git porcelain output."""
    if timeout_seconds <= 0:
        raise ReviewError("Git status timeout must be greater than zero")

    try:
        resolved_root = root.resolve()
    except OSError as exc:
        raise ReviewError("unable to resolve project path") from exc

    runner = _review_git_runner(resolved_root)
    top_level_result = _run_review_git(
        runner, ["rev-parse", "--show-toplevel"], timeout_seconds
    )
    if top_level_result.returncode != 0:
        raise ReviewError("not a Git worktree")

    try:
        decoded_git_root = top_level_result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewError("unable to decode Git worktree root") from exc
    try:
        git_root = Path(decoded_git_root.rstrip("\r\n")).resolve()
    except OSError as exc:
        raise ReviewError("unable to resolve Git worktree root") from exc
    if not top_level_result.stdout or git_root != resolved_root:
        raise ReviewError("project path must be the Git worktree root")

    status_result = _run_review_git(
        runner,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        timeout_seconds,
    )
    if status_result.returncode != 0:
        raise ReviewError("not a Git worktree")

    return _parse_porcelain(status_result.stdout)


def _review_git_runner(root: Path) -> GitRunner:
    try:
        return GitRunner(root)
    except GitUnavailableError as exc:
        raise ReviewError("Git executable was not found") from exc
    except GitLaunchError as exc:
        raise ReviewError("unable to run Git command") from exc


def _run_review_git(
    runner: GitRunner, arguments: list[str], timeout_seconds: float
):
    try:
        return runner.run(arguments, timeout_seconds=timeout_seconds)
    except GitUnsupportedVersionError as exc:
        # Looked up from `reason`, never taken from `str(exc)`: the message on
        # the exception could carry Git's own output, and keeping that off a
        # user's terminal is what the hardcoded literal was protecting. The
        # literal also asserted a version requirement for two causes where no
        # version was ever read -- sanitized and wrong at the same time.
        raise ReviewError(git_version_failure_message(exc.reason)) from None
    except GitUnavailableError as exc:
        raise ReviewError("Git executable was not found") from exc
    except GitTimeoutError as exc:
        raise ReviewError("Git command timeout") from exc
    except GitOutputLimitError as exc:
        raise ReviewError("Git command output limit exceeded") from exc
    except GitLaunchError as exc:
        raise ReviewError("unable to run Git command") from exc


def _parse_porcelain(
    output: bytes, *, max_records: int = MAX_GIT_STATUS_RECORDS
) -> list[Change]:
    if not output:
        return []
    if not output.endswith(b"\0"):
        raise ReviewError("malformed Git status output: missing NUL terminator")
    if output.count(b"\0") > max_records:
        raise ReviewError("Git status record limit exceeded")

    records = output[:-1].split(b"\0")
    changes: dict[str, Change] = {}
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) < 4 or record[2:3] != b" " or not record[3:]:
            raise ReviewError("malformed Git status output")

        status_bytes = record[:2]
        if status_bytes != b"??" and (
            status_bytes == b"  " or any(value not in b" MADRCUT" for value in status_bytes)
        ):
            raise ReviewError("malformed Git status output: invalid status")

        is_rename = b"R" in status_bytes or b"C" in status_bytes
        if is_rename:
            index += 1
            if index >= len(records) or not records[index]:
                raise ReviewError("malformed Git status output: missing rename source")
            _decode_git_path(records[index])

        path = _decode_git_path(record[3:])
        status = _normalize_status(status_bytes)
        change = Change(path, status)
        previous = changes.get(path)
        if previous is not None and previous != change:
            raise ReviewError("Git returned conflicting status data")
        changes[path] = change
        index += 1

    return sorted(changes.values())


def _normalize_status(status: bytes) -> str:
    if status == b"??":
        return "untracked"
    if b"D" in status:
        return "deleted"
    if b"A" in status:
        return "added"
    if b"R" in status or b"C" in status:
        return "renamed"
    return "modified"


def _decode_git_path(value: bytes) -> str:
    try:
        path = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewError("malformed Git status output: invalid path encoding") from exc

    posix_path = PurePosixPath(path)
    if not path or path == "." or posix_path.is_absolute() or ".." in posix_path.parts:
        raise ReviewError("unsafe path in Git status output")
    return path


_RISK_ORDER = (
    "GRAPH_STALE",
    "DELETED_FILES",
    "SENSITIVE_CONFIG",
    "BROAD_IMPACT",
    "MISSING_GRAPH_MATCHES",
    "NO_LIKELY_TESTS",
)

_CHANGE_STATUSES = frozenset(
    {"explicit", "untracked", "deleted", "added", "renamed", "modified"}
)
_DISCOVERY_MODES = frozenset({"explicit", "git"})
_UNSAFE_UNICODE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

_CRITERIA = {
    "CONFIRM_CLEAN": {
        "description": "Confirm that there are no changes in the requested review scope.",
        "verify": "Re-run change discovery and confirm the result is empty.",
    },
    "REVIEW_SCOPE": {
        "description": "Review every changed path in the requested scope.",
        "verify": "Confirm each listed change has been inspected.",
    },
    "REFRESH_GRAPH": {
        "description": "Refresh the stale dependency graph before relying on graph evidence.",
        "verify": "Rebuild the graph and confirm its status is current.",
    },
    "REVIEW_IMPACT": {
        "description": "Review the files identified as potentially impacted.",
        "verify": "Confirm the impacted files remain compatible with the changes.",
    },
    "RUN_LIKELY_TESTS": {
        "description": "Run the tests identified by dependency evidence.",
        "verify": "Record successful results for every listed likely test.",
    },
    "ADD_TEST_PLAN": {
        "description": "Define test coverage because no likely tests were identified.",
        "verify": "Document and execute an appropriate test plan for the changes.",
    },
    "REVIEW_DELETIONS": {
        "description": "Review deleted files and their consumers for intentional removal.",
        "verify": "Confirm each deletion is intentional and leaves no broken references.",
    },
    "VERIFY_CONFIG": {
        "description": "Verify changes to sensitive build, package, or workflow configuration.",
        "verify": "Validate configuration and dependency integrity in a clean environment.",
    },
}


def build_review_packet(
    *,
    root_name: str,
    changes: list[Change],
    discovery: str,
    graph_bundle: dict[str, Any] | None,
    graph_status: dict[str, Any],
    depth: int,
    graph_error: str | None = None,
) -> dict[str, Any]:
    """Build deterministic, bounded evidence for a change review."""
    del graph_error  # Exception details are deliberately never copied into review evidence.
    if depth < 0:
        raise ReviewError("review depth must be zero or greater")

    _validate_review_inputs(root_name, changes, discovery, graph_status)

    ordered_changes = sorted(changes, key=lambda item: (item.path, item.status))
    change_paths = sorted({change.path for change in ordered_changes})
    safe_status = _sanitize_graph_status(graph_status)
    blockers: list[dict[str, str]] = []
    validation: dict[str, Any]
    graph_usable = False
    impact = _empty_impact()

    if graph_bundle is None:
        validation = _empty_validation_summary()
        blockers.append(
            {
                "code": "MISSING_GRAPH",
                "message": "Dependency graph evidence is unavailable; build the graph before review.",
            }
        )
    else:
        validation, impact, graph_usable = _derive_graph_evidence(
            graph_bundle, change_paths, depth
        )
        if not graph_usable:
            blockers.append(
                {
                    "code": "INVALID_GRAPH",
                    "message": "Dependency graph validation failed; rebuild a valid graph before review.",
                }
            )

    stale = safe_status.get("stale") is True
    deleted = any(change.status.casefold() == "deleted" for change in ordered_changes)
    sensitive = any(_is_sensitive_path(change.path) for change in ordered_changes)
    risk_flags = {
        "GRAPH_STALE": stale,
        "DELETED_FILES": deleted,
        "SENSITIVE_CONFIG": sensitive,
        "BROAD_IMPACT": len(impact["impacted_files"]) >= 10,
        "MISSING_GRAPH_MATCHES": graph_usable and bool(impact["missing"]),
        "NO_LIKELY_TESTS": graph_usable and bool(ordered_changes) and not impact["likely_tests"],
    }
    signals = [signal for signal in _RISK_ORDER if risk_flags[signal]]
    high_signals = set(_RISK_ORDER[:4])
    level = "high" if high_signals.intersection(signals) else "medium" if signals else "low"

    criterion_ids: list[str]
    if not ordered_changes:
        criterion_ids = ["CONFIRM_CLEAN"]
    else:
        criterion_ids = ["REVIEW_SCOPE"]
        if stale:
            criterion_ids.append("REFRESH_GRAPH")
        if impact["impacted_files"]:
            criterion_ids.append("REVIEW_IMPACT")
        criterion_ids.append("RUN_LIKELY_TESTS" if impact["likely_tests"] else "ADD_TEST_PLAN")
        if deleted:
            criterion_ids.append("REVIEW_DELETIONS")
        if sensitive:
            criterion_ids.append("VERIFY_CONFIG")

    warnings: list[dict[str, str]] = []
    if stale:
        warnings.append(
            {
                "code": "GRAPH_STALE",
                "message": "Dependency graph evidence may be stale and should be refreshed.",
            }
        )
    if impact["missing"]:
        warnings.append(
            {
                "code": "MISSING_GRAPH_MATCHES",
                "message": "Some changed paths were not matched in the dependency graph.",
            }
        )

    change_dicts, changes_truncated = _bounded_list(
        [change.to_dict() for change in ordered_changes]
    )
    bounded_impact: dict[str, list[str]] = {}
    impact_truncated = False
    for impact_field in ("matched_nodes", "missing", "impacted_files", "likely_tests"):
        bounded_values, field_truncated = _bounded_list(impact[impact_field])
        bounded_impact[impact_field] = bounded_values
        impact_truncated = impact_truncated or field_truncated
    if changes_truncated or impact_truncated:
        warnings.append(
            {
                "code": "OUTPUT_TRUNCATED",
                "message": "Review output was truncated to a bounded size; some entries are not shown.",
            }
        )

    return {
        "schema_version": 1,
        "project": root_name,
        "discovery": discovery,
        "changes": change_dicts,
        "graph": {"status": safe_status, "validation": validation},
        "impact": bounded_impact,
        "risk": {"level": level, "signals": signals},
        "acceptance_criteria": [
            {"id": criterion_id, **_CRITERIA[criterion_id]} for criterion_id in criterion_ids
        ],
        "warnings": warnings,
        "blockers": blockers,
    }


def format_review_markdown(packet: dict[str, Any]) -> str:
    """Render a review packet as bounded, control-character-free Markdown."""
    _validate_formatter_packet(packet)
    graph = packet.get("graph", {})
    status = graph.get("status", {})
    validation = graph.get("validation", {})
    lines = [
        "# Graphite Change Review",
        "",
        f"- Schema: {_safe_markdown_text(packet.get('schema_version', 'unknown'))}",
        f"- Project: {_safe_markdown_text(packet.get('project', 'unknown'))}",
        f"- Discovery: {_safe_markdown_text(packet.get('discovery', 'unknown'))}",
        f"- Graph stale: {_safe_markdown_text(status.get('stale', False))}",
        f"- Graph valid: {_safe_markdown_text(validation.get('ok', False))}",
        "",
        "## Changes",
    ]
    changes = packet.get("changes", [])
    if changes:
        for change in changes:
            lines.append(
                f"- {_inline_code(change.get('path', ''))} — "
                f"{_safe_markdown_text(change.get('status', 'unknown'))}"
            )
    else:
        lines.append("- None")

    impact = packet.get("impact", {})
    lines.extend(["", "## Impact", "", "Impacted files:"])
    _append_code_items(lines, impact.get("impacted_files", []))
    lines.extend(["", "Likely tests:"])
    _append_code_items(lines, impact.get("likely_tests", []))
    if impact.get("missing"):
        lines.extend(["", "Unmatched changes:"])
        _append_code_items(lines, impact["missing"])

    risk = packet.get("risk", {})
    lines.extend(
        [
            "",
            "## Risk Signals",
            "",
            f"Level: **{_safe_markdown_text(risk.get('level', 'unknown'))}**",
        ]
    )
    signals = risk.get("signals", [])
    lines.extend(f"- {_safe_markdown_identifier(signal)}" for signal in signals)
    if not signals:
        lines.append("- None")

    lines.extend(["", "## Acceptance Criteria"])
    for criterion in packet.get("acceptance_criteria", []):
        lines.append(
            f"- [ ] **{_safe_markdown_identifier(criterion.get('id', ''))}** — "
            f"{_safe_markdown_text(criterion.get('description', ''))}"
        )
        lines.append(f"  - Verify: {_safe_markdown_text(criterion.get('verify', ''))}")

    _append_notice_section(lines, "Blockers", packet.get("blockers", []))
    _append_notice_section(lines, "Warnings", packet.get("warnings", []))
    return "\n".join(lines) + "\n"


def _validate_review_inputs(
    root_name: Any,
    changes: Any,
    discovery: Any,
    graph_status: Any,
) -> None:
    if (
        not isinstance(root_name, str)
        or not root_name.strip()
        or "/" in root_name
        or "\\" in root_name
        or _contains_unsafe_unicode(root_name)
    ):
        raise ReviewError("project label is invalid")
    if not isinstance(discovery, str) or discovery not in _DISCOVERY_MODES:
        raise ReviewError("review discovery is invalid")
    if not isinstance(changes, list) or not all(isinstance(change, Change) for change in changes):
        raise ReviewError("review changes are invalid")
    for change in changes:
        if _normalize_safe_relative_path(change.path) is None:
            raise ReviewError("change path is invalid")
        if not isinstance(change.status, str) or change.status not in _CHANGE_STATUSES:
            raise ReviewError("change status is invalid")
    if not isinstance(graph_status, dict):
        raise ReviewError("graph status is invalid")


def _derive_graph_evidence(
    graph_bundle: Any,
    change_paths: list[str],
    depth: int,
) -> tuple[dict[str, Any], dict[str, list[str]], bool]:
    empty_impact = _empty_impact()
    if not isinstance(graph_bundle, dict):
        return _invalid_graph_summary("bundle_type"), empty_impact, False

    validation: dict[str, Any] | None = None
    try:
        report = validate_graph_bundle(graph_bundle)
        validation = _validation_summary(report)
        if not report["ok"]:
            return validation, empty_impact, False
        if _bundle_has_unsafe_source_file(graph_bundle):
            return _invalid_graph_summary("graph_processing_error", validation), empty_impact, False

        context = build_context(graph_from_json(graph_bundle), change_paths, depth=depth)
        impact = _validated_context_impact(context)
        return validation, impact, True
    except Exception:
        return _invalid_graph_summary("graph_processing_error", validation), empty_impact, False


def _validated_context_impact(context: Any) -> dict[str, list[str]]:
    if not isinstance(context, dict) or not isinstance(context.get("impact"), dict):
        raise ValueError("invalid graph context")
    context_impact = context["impact"]
    matched_nodes = context_impact.get("matched_nodes")
    if (
        not isinstance(matched_nodes, list)
        or not all(
            isinstance(node, str) and node and not _contains_unsafe_unicode(node)
            for node in matched_nodes
        )
    ):
        raise ValueError("invalid graph context")

    return {
        "matched_nodes": sorted(set(matched_nodes)),
        "missing": _validated_graph_paths(context.get("missing")),
        "impacted_files": _validated_graph_paths(context_impact.get("impacted_files")),
        "likely_tests": _validated_graph_paths(context_impact.get("likely_tests")),
    }


def _validated_graph_paths(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("invalid graph paths")
    normalized: set[str] = set()
    for value in values:
        safe_path = _normalize_safe_relative_path(value)
        if safe_path is None:
            raise ValueError("invalid graph paths")
        normalized.add(safe_path)
    return sorted(normalized)


def _bundle_has_unsafe_source_file(bundle: dict[str, Any]) -> bool:
    nodes = bundle.get("nodes", [])
    if not isinstance(nodes, list):
        return True
    for node in nodes:
        if not isinstance(node, dict):
            continue
        source_file = node.get("source_file")
        if source_file is None or source_file == "":
            continue
        if _normalize_safe_relative_path(source_file) is None:
            return True
    return False


def _bounded_list(values: list[Any]) -> tuple[list[Any], bool]:
    """Return values truncated to MAX_REVIEW_PACKET_ITEMS and whether truncation occurred."""
    if len(values) > MAX_REVIEW_PACKET_ITEMS:
        return values[:MAX_REVIEW_PACKET_ITEMS], True
    return values, False


def _empty_impact() -> dict[str, list[str]]:
    return {
        "matched_nodes": [],
        "missing": [],
        "impacted_files": [],
        "likely_tests": [],
    }


def _invalid_graph_summary(
    code: str, base: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "ok": False,
        "error_count": 1,
        "warning_count": _safe_count(base.get("warning_count")) if base else 0,
        "node_count": _safe_count(base.get("node_count")) if base else 0,
        "edge_count": _safe_count(base.get("edge_count")) if base else 0,
        "error_codes": [code],
        "warning_codes": list(base.get("warning_codes", [])) if base else [],
    }


def _validate_formatter_packet(packet: Any) -> None:
    if not isinstance(packet, dict):
        raise ReviewError("review packet is invalid")

    mapping_fields = ("graph", "impact", "risk")
    for field in mapping_fields:
        if field in packet and not isinstance(packet[field], dict):
            raise ReviewError("review packet is invalid")
    graph = packet.get("graph", {})
    for field in ("status", "validation"):
        if field in graph and not isinstance(graph[field], dict):
            raise ReviewError("review packet is invalid")

    object_list_fields = ("changes", "acceptance_criteria", "blockers", "warnings")
    for field in object_list_fields:
        values = packet.get(field, [])
        if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
            raise ReviewError("review packet is invalid")
    impact = packet.get("impact", {})
    for field in ("matched_nodes", "impacted_files", "likely_tests", "missing"):
        values = impact.get(field, [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ReviewError("review packet is invalid")
    risk = packet.get("risk", {})
    signals = risk.get("signals", [])
    if not isinstance(signals, list) or not all(isinstance(signal, str) for signal in signals):
        raise ReviewError("review packet is invalid")


def _validation_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(report.get("ok", False)),
        "error_count": _safe_count(report.get("error_count")),
        "warning_count": _safe_count(report.get("warning_count")),
        "node_count": _safe_count(report.get("node_count")),
        "edge_count": _safe_count(report.get("edge_count")),
        "error_codes": _sorted_issue_codes(report.get("errors", [])),
        "warning_codes": _sorted_issue_codes(report.get("warnings", [])),
    }


def _empty_validation_summary() -> dict[str, Any]:
    return {
        "ok": False,
        "error_count": 0,
        "warning_count": 0,
        "node_count": 0,
        "edge_count": 0,
        "error_codes": [],
        "warning_codes": [],
    }


def _sanitize_graph_status(status: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    if isinstance(status.get("stale"), bool):
        safe["stale"] = status["stale"]
    for key in (
        "node_count",
        "edge_count",
        "file_count",
        "manifest_file_count",
        "added_count",
        "changed_count",
        "removed_count",
    ):
        value = status.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            safe[key] = value
    for key in ("added", "changed", "removed"):
        values = status.get(key)
        if isinstance(values, list):
            safe[key] = sorted(
                {
                    normalized
                    for value in values
                    if isinstance(value, str)
                    for normalized in [_normalize_safe_relative_path(value)]
                    if normalized is not None
                }
            )
    reason = status.get("reason")
    if isinstance(reason, str):
        lowered = reason.casefold()
        safe["reason"] = next(
            (
                category
                for category in ("missing", "changed", "stale", "invalid", "error")
                if category in lowered
            ),
            "unknown",
        )
    return safe


def _normalize_safe_relative_path(value: Any) -> str | None:
    if not isinstance(value, str) or _contains_unsafe_unicode(value):
        return None
    normalized = value.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return None
    if len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":":
        return None
    path = PurePosixPath(normalized)
    if path == PurePosixPath(".") or ".." in path.parts:
        return None
    return path.as_posix()


def _is_sensitive_path(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    name = normalized.rsplit("/", 1)[-1]
    sensitive_names = {
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
        "requirements.txt",
        "uv.lock",
        "pipfile.lock",
        "poetry.lock",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
        "dockerfile",
    }
    return name in sensitive_names or normalized.startswith(".github/workflows/")


def _sorted_issue_codes(issues: Any) -> list[str]:
    if not isinstance(issues, list):
        return []
    return sorted(
        {
            code
            for issue in issues
            if isinstance(issue, dict)
            for code in [issue.get("code")]
            if isinstance(code, str) and not _contains_unsafe_unicode(code)
        }
    )


def _safe_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _contains_unsafe_unicode(value: str) -> bool:
    return any(_is_unsafe_unicode_character(character) for character in value)


def _is_unsafe_unicode_character(character: str) -> bool:
    return unicodedata.category(character) in _UNSAFE_UNICODE_CATEGORIES


def _safe_markdown_text(value: Any) -> str:
    text = str(value)
    text = "".join(
        character if not _is_unsafe_unicode_character(character) else " "
        for character in text
    )
    for character in "\\`*_{}[]<>#|":
        text = text.replace(character, f"\\{character}")
    return text


def _safe_markdown_identifier(value: Any) -> str:
    return "".join(
        character if character.isalnum() or character in "_-" else "_"
        for character in str(value)
        if not _is_unsafe_unicode_character(character)
    )


def _inline_code(value: Any) -> str:
    text = str(value)
    text = "".join(
        character if not _is_unsafe_unicode_character(character) else " "
        for character in text
    )
    longest_run = 0
    current_run = 0
    for character in text:
        current_run = current_run + 1 if character == "`" else 0
        longest_run = max(longest_run, current_run)
    delimiter = "`" * (longest_run + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{delimiter}{padding}{text}{padding}{delimiter}"


def _append_code_items(lines: list[str], values: Any) -> None:
    if values:
        lines.extend(f"- {_inline_code(value)}" for value in values)
    else:
        lines.append("- None")


def _append_notice_section(lines: list[str], heading: str, notices: Any) -> None:
    if not notices:
        return
    lines.extend(["", f"## {heading}"])
    for notice in notices:
        lines.append(
            f"- **{_safe_markdown_identifier(notice.get('code', ''))}** — "
            f"{_safe_markdown_text(notice.get('message', ''))}"
        )
