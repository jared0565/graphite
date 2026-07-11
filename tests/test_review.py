from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import graphite.review as review_module

from graphite.review import (
    Change,
    ReviewError,
    _parse_porcelain,
    build_review_packet,
    discover_git_changes,
    format_review_markdown,
    normalize_explicit_changes,
)


def _review_bundle(*, dependent_count: int = 1) -> dict[str, object]:
    nodes: list[dict[str, str]] = [
        {"id": "store", "kind": "module", "name": "store", "source_file": "src/store.py"},
        {
            "id": "store_test",
            "kind": "module",
            "name": "test_store",
            "source_file": "tests/test_store.py",
        },
    ]
    edges: list[dict[str, str]] = [
        {"source": "store_test", "target": "store", "relation": "imports"}
    ]
    for index in range(dependent_count):
        node_id = f"api_{index}"
        source_file = "src/api.py" if index == 0 else f"src/api_{index}.py"
        nodes.append(
            {"id": node_id, "kind": "module", "name": node_id, "source_file": source_file}
        )
        edges.append({"source": node_id, "target": "store", "relation": "imports"})
    return {
        "nodes": nodes,
        "edges": edges,
        "metadata": {"node_count": len(nodes), "edge_count": len(edges)},
    }


def _packet(
    changes: list[Change],
    *,
    bundle: dict[str, object] | None = None,
    graph_status: dict[str, object] | None = None,
) -> dict[str, object]:
    return build_review_packet(
        root_name="sample",
        changes=changes,
        discovery="git",
        graph_bundle=_review_bundle() if bundle is None else bundle,
        graph_status={} if graph_status is None else graph_status,
        depth=2,
    )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_change_is_ordered_and_serializes_to_a_dictionary() -> None:
    assert Change("b.py", "modified") > Change("a.py", "modified")
    assert Change("a.py", "explicit").to_dict() == {"path": "a.py", "status": "explicit"}

    with pytest.raises(AttributeError):
        Change("a.py", "explicit").path = "changed.py"


def test_normalize_explicit_changes_is_unique_sorted_and_project_relative(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    second = nested / "b.py"

    assert normalize_explicit_changes(
        tmp_path,
        [str(second), "a.py", "nested/b.py"],
    ) == [
        Change("a.py", "explicit"),
        Change("nested/b.py", "explicit"),
    ]


def test_normalize_explicit_changes_rejects_paths_outside_project_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    with pytest.raises(ReviewError, match="outside project root"):
        normalize_explicit_changes(root, ["../secrets.txt"])


def test_outside_root_error_is_bounded_and_does_not_echo_input(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    injected = "private-" + ("x" * 300) + "\nINJECTED"

    with pytest.raises(ReviewError) as error:
        normalize_explicit_changes(root, [str(tmp_path / injected)])

    assert str(error.value) == "path is outside project root"
    assert len(str(error.value)) <= 200
    assert "INJECTED" not in str(error.value)


def test_normalize_explicit_changes_rejects_project_root(tmp_path: Path) -> None:
    with pytest.raises(ReviewError, match="project root"):
        normalize_explicit_changes(tmp_path, ["."])


def test_discover_git_changes_collects_and_classifies_worktree_evidence(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "review@example.test")
    _git(tmp_path, "config", "user.name", "Review Test")

    for name in ("before.py", "deleted.py", "staged.py", "unstaged.py"):
        _write(tmp_path / name, "baseline\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "baseline")

    _git(tmp_path, "mv", "before.py", "after.py")
    (tmp_path / "deleted.py").unlink()
    _write(tmp_path / "staged.py", "staged change\n")
    _git(tmp_path, "add", "staged.py")
    _write(tmp_path / "unstaged.py", "unstaged change\n")
    _write(tmp_path / "untracked.py", "untracked\n")

    assert discover_git_changes(tmp_path) == [
        Change("after.py", "renamed"),
        Change("deleted.py", "deleted"),
        Change("staged.py", "modified"),
        Change("unstaged.py", "modified"),
        Change("untracked.py", "untracked"),
    ]


def test_discover_git_changes_rejects_non_git_directory(tmp_path: Path) -> None:
    with pytest.raises(ReviewError, match="not a Git worktree"):
        discover_git_changes(tmp_path)


def test_discover_git_changes_rejects_nested_worktree_path(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    nested = tmp_path / "nested"
    nested.mkdir()

    with pytest.raises(ReviewError, match="^project path must be the Git worktree root$"):
        discover_git_changes(nested)


def test_discover_git_changes_does_not_expose_git_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_git(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            ["git"], 128, stdout=b"", stderr=b"C:\\private\\repo\nINJECTED"
        )

    monkeypatch.setattr(review_module.subprocess, "run", failed_git)

    with pytest.raises(ReviewError) as error:
        discover_git_changes(tmp_path)

    assert str(error.value) == "not a Git worktree"
    assert len(str(error.value)) <= 200
    assert "private" not in str(error.value)
    assert "\n" not in str(error.value)


def test_discover_git_changes_sanitizes_os_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_git(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise OSError("C:\\private\\repo\nINJECTED")

    monkeypatch.setattr(review_module.subprocess, "run", broken_git)

    with pytest.raises(ReviewError) as error:
        discover_git_changes(tmp_path)

    assert str(error.value) == "unable to run Git command"
    assert len(str(error.value)) <= 200
    assert "private" not in str(error.value)
    assert "\n" not in str(error.value)


def test_discover_git_changes_rejects_invalid_utf8_worktree_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invalid_root(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(["git"], 0, stdout=b"\xff\n", stderr=b"")

    monkeypatch.setattr(review_module.subprocess, "run", invalid_root)

    with pytest.raises(ReviewError) as error:
        discover_git_changes(tmp_path)

    assert str(error.value) == "unable to decode Git worktree root"
    assert "\\udc" not in str(error.value)


def test_discover_git_changes_rejects_non_positive_timeout(tmp_path: Path) -> None:
    with pytest.raises(ReviewError, match="timeout"):
        discover_git_changes(tmp_path, timeout_seconds=0)


def test_parse_porcelain_maps_type_change_to_modified() -> None:
    assert _parse_porcelain(b"T  changed.bin\0") == [Change("changed.bin", "modified")]


def test_parse_porcelain_maps_added_and_copied_states() -> None:
    assert _parse_porcelain(b"A  added.py\0C  copy.py\0source.py\0") == [
        Change("added.py", "added"),
        Change("copy.py", "renamed"),
    ]


def test_parse_porcelain_preserves_literal_backslashes() -> None:
    path = "literal\\backslash.py"

    assert _parse_porcelain(b"?? " + os.fsencode(path) + b"\0") == [Change(path, "untracked")]


def test_parse_porcelain_rejects_invalid_utf8_path() -> None:
    with pytest.raises(ReviewError) as error:
        _parse_porcelain(b"?? invalid-\xff.py\0")

    assert str(error.value) == "malformed Git status output: invalid path encoding"
    assert "\\udc" not in str(error.value)


@pytest.mark.parametrize(
    "output",
    [
        b"?? ../escape.py\0",
        b"?? nested/../../escape.py\0",
        b"?? /absolute.py\0",
        b"?? .\0",
        b"?? \0",
    ],
)
def test_parse_porcelain_rejects_unsafe_paths(output: bytes) -> None:
    with pytest.raises(ReviewError, match="Git status output"):
        _parse_porcelain(output)


def test_parse_porcelain_deduplicates_identical_status_data() -> None:
    assert _parse_porcelain(b" M same.py\0 M same.py\0") == [Change("same.py", "modified")]


@pytest.mark.parametrize(
    "output",
    [b" M same.py\0D  same.py\0", b"D  same.py\0 M same.py\0"],
)
def test_parse_porcelain_rejects_conflicting_status_data(output: bytes) -> None:
    with pytest.raises(ReviewError, match="^Git returned conflicting status data$"):
        _parse_porcelain(output)


def test_parse_porcelain_rejects_malformed_status() -> None:
    with pytest.raises(ReviewError, match="malformed Git status output"):
        _parse_porcelain(b"Z  bad.py\0")


@pytest.mark.parametrize("status", [b"?M", b"!!", b"  "])
def test_parse_porcelain_rejects_invalid_xy_grammar(status: bytes) -> None:
    with pytest.raises(ReviewError, match="invalid status"):
        _parse_porcelain(status + b" invalid.py\0")


@pytest.mark.parametrize(
    ("status", "expected"),
    [(b"UU", "modified"), (b"AA", "added"), (b"UD", "deleted")],
)
def test_parse_porcelain_accepts_valid_unmerged_states(status: bytes, expected: str) -> None:
    assert _parse_porcelain(status + b" conflict.py\0") == [Change("conflict.py", expected)]


def test_parse_porcelain_rejects_incomplete_rename() -> None:
    with pytest.raises(ReviewError, match="missing rename source"):
        _parse_porcelain(b"R  renamed.py\0")


def test_build_review_packet_derives_deterministic_graph_evidence() -> None:
    changes = [Change("src/store.py", "modified")]

    first = _packet(changes)
    second = _packet(changes)

    assert first == second
    assert first["schema_version"] == 1
    assert first["project"] == "sample"
    assert first["changes"] == [{"path": "src/store.py", "status": "modified"}]
    assert first["impact"] == {
        "matched_nodes": ["store"],
        "missing": [],
        "impacted_files": ["src/api.py"],
        "likely_tests": ["tests/test_store.py"],
    }
    assert first["risk"] == {"level": "low", "signals": []}
    assert [item["id"] for item in first["acceptance_criteria"]] == [
        "REVIEW_SCOPE",
        "REVIEW_IMPACT",
        "RUN_LIKELY_TESTS",
    ]


def test_build_review_packet_combines_stale_deleted_sensitive_and_missing_risks() -> None:
    packet = _packet(
        [
            Change("pyproject.toml", "modified"),
            Change("src/missing.py", "deleted"),
        ],
        graph_status={"stale": True},
    )

    assert packet["risk"] == {
        "level": "high",
        "signals": [
            "GRAPH_STALE",
            "DELETED_FILES",
            "SENSITIVE_CONFIG",
            "MISSING_GRAPH_MATCHES",
            "NO_LIKELY_TESTS",
        ],
    }
    assert [item["id"] for item in packet["acceptance_criteria"]] == [
        "REVIEW_SCOPE",
        "REFRESH_GRAPH",
        "ADD_TEST_PLAN",
        "REVIEW_DELETIONS",
        "VERIFY_CONFIG",
    ]
    assert [item["code"] for item in packet["warnings"]] == [
        "GRAPH_STALE",
        "MISSING_GRAPH_MATCHES",
    ]


def test_build_review_packet_blocks_missing_graph_without_exposing_error() -> None:
    packet = build_review_packet(
        root_name="sample",
        changes=[Change("src/store.py", "modified")],
        discovery="git",
        graph_bundle=None,
        graph_status={},
        depth=2,
        graph_error="C:\\private\\graph.json\nINJECTED",
    )

    assert [item["code"] for item in packet["blockers"]] == ["MISSING_GRAPH"]
    assert packet["impact"] == {
        "matched_nodes": [],
        "missing": [],
        "impacted_files": [],
        "likely_tests": [],
    }
    assert packet["risk"] == {"level": "low", "signals": []}
    assert packet["warnings"] == []
    assert packet["blockers"] == [
        {
            "code": "MISSING_GRAPH",
            "message": "Dependency graph evidence is unavailable; build the graph before review.",
        }
    ]
    assert "private" not in repr(packet)
    assert "INJECTED" not in repr(packet)


def test_build_review_packet_blocks_invalid_graph_with_safe_validation_summary() -> None:
    bundle = _review_bundle()
    assert isinstance(bundle["edges"], list)
    bundle["edges"].append(
        {"source": "store", "target": "unknown", "relation": "imports"}
    )
    assert isinstance(bundle["metadata"], dict)
    bundle["metadata"]["edge_count"] = len(bundle["edges"])

    packet = _packet([Change("src/store.py", "modified")], bundle=bundle)

    assert [item["code"] for item in packet["blockers"]] == ["INVALID_GRAPH"]
    assert packet["graph"]["validation"]["ok"] is False
    assert packet["graph"]["validation"]["error_codes"] == ["edge_target_unknown"]
    assert "edge target does not exist" not in repr(packet["graph"]["validation"])
    assert "'target': 'unknown'" not in repr(packet["graph"]["validation"])
    assert packet["impact"] == {
        "matched_nodes": [],
        "missing": [],
        "impacted_files": [],
        "likely_tests": [],
    }
    assert packet["risk"] == {"level": "low", "signals": []}
    assert packet["warnings"] == []
    assert packet["blockers"] == [
        {
            "code": "INVALID_GRAPH",
            "message": "Dependency graph validation failed; rebuild a valid graph before review.",
        }
    ]


def test_build_review_packet_empty_changes_only_confirms_clean_state() -> None:
    packet = _packet([])

    assert packet["risk"] == {"level": "low", "signals": []}
    assert [item["id"] for item in packet["acceptance_criteria"]] == ["CONFIRM_CLEAN"]


@pytest.mark.parametrize(
    ("dependent_count", "has_broad_impact"),
    [(9, False), (10, True)],
)
def test_build_review_packet_broad_impact_threshold(
    dependent_count: int, has_broad_impact: bool
) -> None:
    packet = _packet(
        [Change("src/store.py", "modified")],
        bundle=_review_bundle(dependent_count=dependent_count),
    )

    assert ("BROAD_IMPACT" in packet["risk"]["signals"]) is has_broad_impact
    assert packet["risk"]["level"] == ("high" if has_broad_impact else "low")


@pytest.mark.parametrize(
    "path",
    ["PYPROJECT.TOML", "Package-Lock.JSON", "DOCKERFILE", ".GITHUB/WORKFLOWS/CI.YML"],
)
def test_build_review_packet_sensitive_paths_are_case_insensitive(path: str) -> None:
    packet = _packet([Change(path, "modified")])

    assert "SENSITIVE_CONFIG" in packet["risk"]["signals"]
    assert "VERIFY_CONFIG" in [item["id"] for item in packet["acceptance_criteria"]]


def test_build_review_packet_rejects_negative_depth() -> None:
    with pytest.raises(ReviewError, match="depth"):
        build_review_packet(
            root_name="sample",
            changes=[],
            discovery="git",
            graph_bundle=_review_bundle(),
            graph_status={},
            depth=-1,
        )


@pytest.mark.parametrize("path", ["src/bad\nname.py", "src/bad\x1b[31m.py", "src/bad\x00.py"])
def test_build_review_packet_rejects_control_characters_in_change_paths(path: str) -> None:
    with pytest.raises(ReviewError) as error:
        _packet([Change(path, "modified")])

    assert str(error.value) == "change path contains unsafe characters"
    assert path not in str(error.value)


def test_build_review_packet_sanitizes_graph_status() -> None:
    packet = _packet(
        [Change("src/store.py", "modified")],
        graph_status={
            "stale": True,
            "node_count": 3,
            "edge_count": 2,
            "file_count": 8,
            "manifest_file_count": 7,
            "added": ["src/z.py", "src/a.py", "src/a.py"],
            "changed": ["src/c.py"],
            "removed": ["src/r.py"],
            "manifest": "C:\\private\\manifest.json",
            "error": "RuntimeError: C:\\private\\repo\nINJECTED\x1b[31m",
            "reason": "manifest missing at C:\\private\\manifest.json\nINJECTED",
        },
    )

    assert packet["graph"]["status"] == {
        "stale": True,
        "node_count": 3,
        "edge_count": 2,
        "file_count": 8,
        "manifest_file_count": 7,
        "added": ["src/a.py", "src/z.py"],
        "changed": ["src/c.py"],
        "removed": ["src/r.py"],
        "reason": "missing",
    }
    serialized = repr(packet)
    assert "private" not in serialized
    assert "INJECTED" not in serialized
    assert "RuntimeError" not in serialized
    assert "\x1b" not in serialized


def test_format_review_markdown_renders_packet_evidence() -> None:
    packet = _packet([Change("src/store.py", "modified")])

    markdown = format_review_markdown(packet)

    assert markdown.startswith("# Graphite Change Review\n")
    assert "## Changes" in markdown
    assert "`src/store.py`" in markdown
    assert "## Impact" in markdown
    assert "`src/api.py`" in markdown
    assert "`tests/test_store.py`" in markdown
    assert "## Risk Signals" in markdown
    assert "## Acceptance Criteria" in markdown
    assert "- [ ] **REVIEW_SCOPE**" in markdown
    assert "- [ ] **REVIEW_IMPACT**" in markdown
    assert "- [ ] **RUN_LIKELY_TESTS**" in markdown
    assert "## Warnings" not in markdown
    assert "## Blockers" not in markdown
    assert markdown.endswith("\n")


def test_format_review_markdown_includes_optional_blockers() -> None:
    packet = build_review_packet(
        root_name="sample",
        changes=[Change("src/store.py", "modified")],
        discovery="explicit",
        graph_bundle=None,
        graph_status={},
        depth=1,
    )

    markdown = format_review_markdown(packet)

    assert "## Blockers" in markdown
    assert "MISSING_GRAPH" in markdown


def test_format_review_markdown_includes_only_applicable_warning_section() -> None:
    warning_packet = _packet(
        [Change("src/store.py", "modified")], graph_status={"stale": True}
    )
    no_warning_packet = _packet([Change("src/store.py", "modified")])

    warning_markdown = format_review_markdown(warning_packet)
    no_warning_markdown = format_review_markdown(no_warning_packet)

    assert "## Warnings" in warning_markdown
    assert "GRAPH_STALE" in warning_markdown
    assert "Dependency graph evidence may be stale and should be refreshed." in warning_markdown
    assert "## Warnings" not in no_warning_markdown


def test_format_review_markdown_bounds_inline_code_containing_backticks() -> None:
    packet = _packet([Change("src/a``b.py", "modified")])

    markdown = format_review_markdown(packet)

    assert "```src/a``b.py```" in markdown
