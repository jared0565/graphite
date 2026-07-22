from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest
import graphite.cli as cli_module
import graphite.git as git_module
import graphite.review as review_module

from graphite.cache import file_hash
from graphite.cli import main
from graphite.config import Config
from graphite.engine_identity import engine_identity
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class _FakeGitProcess:
    def __init__(self, output: bytes, returncode: int = 0) -> None:
        self.stdout = io.BytesIO(output)
        self.returncode = returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        pass


def _collect_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {
            str(key).casefold()
            for key in value
        } | {
            nested_key
            for nested_value in value.values()
            for nested_key in _collect_keys(nested_value)
        }
    if isinstance(value, list):
        return {key for item in value for key in _collect_keys(item)}
    return set()


def _write_review_project(
    root: Path,
    *,
    bundle: object | None = None,
    graph_path: str = "graph-out/graph.json",
) -> None:
    _write(root / "src/store.py", "value = 1\n")
    _write(root / "tests/test_store.py", "def test_store(): pass\n")
    selected_bundle = _review_bundle() if bundle is None else bundle
    _write(root / graph_path, json.dumps(selected_bundle))
    manifest = {
        "root": root.name,
        "file_count": 2,
        "engine": engine_identity(Config().cache_version),
        "files": [
            {
                "rel_path": relative,
                "language": "python",
                "size": (root / relative).stat().st_size,
                "hash": file_hash(root / relative),
            }
            for relative in ("src/store.py", "tests/test_store.py")
        ],
    }
    _write(root / "graph-out/.graphite_manifest.json", json.dumps(manifest))


def _write_review_manifest(root: Path, output_dir: str, relative_files: tuple[str, ...]) -> None:
    manifest = {
        "root": root.name,
        "file_count": len(relative_files),
        "engine": engine_identity(Config().cache_version),
        "files": [
            {
                "rel_path": relative,
                "language": "python",
                "size": (root / relative).stat().st_size,
                "hash": file_hash(root / relative),
            }
            for relative in relative_files
        ],
    }
    _write(root / output_dir / ".graphite_manifest.json", json.dumps(manifest))


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


def test_resolve_trusted_git_ignores_unsafe_path_entries_and_cwd_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "untrusted repository"
    repository_tools = repository / "tools"
    trusted_bin = tmp_path / "trusted tools"
    repository_tools.mkdir(parents=True)
    trusted_bin.mkdir()
    _write(repository / "git.exe", "malicious")
    _write(repository_tools / "git.exe", "also malicious")
    _write(trusted_bin / "git.exe", "trusted")
    monkeypatch.chdir(repository)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            ("", ".", "relative tools", str(repository_tools), str(trusted_bin))
        ),
    )

    executable = git_module._resolve_git_executable(
        repository.resolve(), platform_name="nt"
    )

    assert executable == trusted_bin.resolve() / "git.exe"
    assert executable.is_absolute()
    assert executable.name == "git.exe"
    assert executable.parent != repository.resolve()


def test_resolve_trusted_git_rejects_only_project_contained_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "private repository"
    repository_tools = repository / "nested tools"
    repository_tools.mkdir(parents=True)
    _write(repository_tools / "git.exe", "malicious")
    monkeypatch.setenv("PATH", str(repository_tools))

    with pytest.raises(git_module.GitUnavailableError) as error:
        git_module._resolve_git_executable(
            repository.resolve(), platform_name="nt"
        )

    assert str(error.value) == "Git executable was not found"
    assert str(repository) not in str(error.value)


def test_resolve_trusted_git_failure_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    private_path = tmp_path / ("private-" + "x" * 80)
    monkeypatch.setenv("PATH", os.pathsep.join(("", ".", "relative", str(private_path))))

    with pytest.raises(git_module.GitUnavailableError) as error:
        git_module._resolve_git_executable(
            repository.resolve(), platform_name="nt"
        )

    assert str(error.value) == "Git executable was not found"
    assert str(private_path) not in str(error.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permission contract")
def test_resolve_trusted_git_skips_non_executable_posix_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    first_bin = tmp_path / "first bin"
    trusted_bin = tmp_path / "trusted bin"
    repository.mkdir()
    first_bin.mkdir()
    trusted_bin.mkdir()
    _write(first_bin / "git", "not executable")
    _write(trusted_bin / "git", "#!/bin/sh\nexit 0\n")
    (first_bin / "git").chmod(0o600)
    (trusted_bin / "git").chmod(0o700)
    monkeypatch.setenv("PATH", os.pathsep.join((str(first_bin), str(trusted_bin))))

    executable = git_module._resolve_git_executable(
        repository.resolve(), platform_name="posix"
    )

    assert executable == (trusted_bin / "git").resolve()


def test_discover_git_changes_uses_hardened_absolute_git_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted_git = (tmp_path / "trusted tools" / "git.exe").resolve()
    calls: list[tuple[list[str], dict[str, object]]] = []
    hostile_git_environment = {
        "GiT_DiR": "private-dir",
        "git_work_tree": "private-tree",
        "GIT_INDEX_FILE": "private-index",
        "Git_Common_Dir": "private-common",
        "GIT_OBJECT_DIRECTORY": "private-objects",
        "git_exec_path": "private-exec",
        "GIT_CONFIG_COUNT": "99",
        "Git_Trace": "private-trace",
    }
    for name, value in hostile_git_environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GRAPHITE_REVIEW_TEST", "retained")
    source_environment = dict(os.environ)

    def fake_popen(command: list[str], **kwargs: object) -> _FakeGitProcess:
        calls.append((command, kwargs))
        if command[1:] == ["--version"]:
            return _FakeGitProcess(b"git version 2.38.0\n")
        if command[-2:] == ["rev-parse", "--show-toplevel"]:
            return _FakeGitProcess(f"{tmp_path.resolve()}\n".encode())
        return _FakeGitProcess(b"?? path with spaces.py\0")

    monkeypatch.setattr(
        git_module,
        "_resolve_git_executable",
        lambda _root, **_kwargs: trusted_git,
    )
    monkeypatch.setattr(git_module.subprocess, "Popen", fake_popen)

    assert discover_git_changes(tmp_path) == [Change("path with spaces.py", "untracked")]
    assert len(calls) == 3
    assert calls[0][0] == [str(trusted_git), "--version"]
    for command, kwargs in calls[1:]:
        assert command[0] == str(trusted_git)
        assert command[1:4] == ["--no-optional-locks", "-c", "core.fsmonitor=false"]
        assert kwargs["shell"] is False
        assert kwargs["stdin"] is subprocess.DEVNULL
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["PATH"] == source_environment["PATH"]
        assert environment["GRAPHITE_REVIEW_TEST"] == "retained"
        assert environment["GIT_OPTIONAL_LOCKS"] == "0"
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert {
            name for name in environment if name.casefold().startswith("git_")
        } == {"GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT"}
    assert dict(os.environ) == source_environment
    assert calls[1][0][6:] == ["rev-parse", "--show-toplevel"]
    assert calls[2][0][6:] == [
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ]


def test_discover_git_changes_rejects_non_git_directory(tmp_path: Path) -> None:
    with pytest.raises(ReviewError, match="not a Git worktree"):
        discover_git_changes(tmp_path)


def test_discover_git_changes_maps_unsupported_git_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnsupportedGitRunner:
        def run(self, arguments: list[str], *, timeout_seconds: float) -> None:
            raise git_module.GitUnsupportedVersionError("private raw version output")

    monkeypatch.setattr(
        review_module, "_review_git_runner", lambda _root: UnsupportedGitRunner()
    )

    with pytest.raises(ReviewError) as error:
        discover_git_changes(tmp_path)

    assert str(error.value) == "Git 2.38 or newer is required"
    assert error.value.__cause__ is None


def test_review_changes_cli_maps_unsupported_git_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class UnsupportedGitRunner:
        def run(self, arguments: list[str], *, timeout_seconds: float) -> None:
            raise git_module.GitUnsupportedVersionError("private raw version output")

    monkeypatch.setattr(
        review_module, "_review_git_runner", lambda _root: UnsupportedGitRunner()
    )

    assert main(["review-changes", str(tmp_path), "--json"]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "[graphite] error: Git 2.38 or newer is required\n"
    assert "private" not in output.err


def test_discover_git_changes_rejects_nested_worktree_path(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    nested = tmp_path / "nested"
    nested.mkdir()

    with pytest.raises(ReviewError, match="^project path must be the Git worktree root$"):
        discover_git_changes(nested)


def test_discover_git_changes_does_not_expose_git_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_git(command: list[str], **kwargs: object) -> _FakeGitProcess:
        if command[1:] == ["--version"]:
            return _FakeGitProcess(b"git version 2.38.0\n")
        return _FakeGitProcess(b"", returncode=128)

    monkeypatch.setattr(git_module.subprocess, "Popen", failed_git)

    with pytest.raises(ReviewError) as error:
        discover_git_changes(tmp_path)

    assert str(error.value) == "not a Git worktree"
    assert len(str(error.value)) <= 200
    assert "private" not in str(error.value)
    assert "\n" not in str(error.value)


def test_discover_git_changes_sanitizes_os_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_git(command: list[str], **kwargs: object) -> _FakeGitProcess:
        if command[1:] == ["--version"]:
            return _FakeGitProcess(b"git version 2.38.0\n")
        raise OSError("C:\\private\\repo\nINJECTED")

    monkeypatch.setattr(git_module.subprocess, "Popen", broken_git)

    with pytest.raises(ReviewError) as error:
        discover_git_changes(tmp_path)

    assert str(error.value) == "unable to run Git command"
    assert len(str(error.value)) <= 200
    assert "private" not in str(error.value)
    assert "\n" not in str(error.value)


def test_discover_git_changes_rejects_invalid_utf8_worktree_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invalid_root(command: list[str], **kwargs: object) -> _FakeGitProcess:
        if command[1:] == ["--version"]:
            return _FakeGitProcess(b"git version 2.38.0\n")
        return _FakeGitProcess(b"\xff\n")

    monkeypatch.setattr(git_module.subprocess, "Popen", invalid_root)

    with pytest.raises(ReviewError) as error:
        discover_git_changes(tmp_path)

    assert str(error.value) == "unable to decode Git worktree root"
    assert "\\udc" not in str(error.value)


def test_discover_git_changes_rejects_non_positive_timeout(tmp_path: Path) -> None:
    with pytest.raises(ReviewError, match="timeout"):
        discover_git_changes(tmp_path, timeout_seconds=0)


def test_parse_porcelain_maps_type_change_to_modified() -> None:
    assert _parse_porcelain(b"T  changed.bin\0") == [Change("changed.bin", "modified")]


def test_parse_porcelain_rejects_record_count_above_limit() -> None:
    output = b"?? one.py\0?? two.py\0?? three.py\0"

    with pytest.raises(ReviewError, match="Git status record limit exceeded"):
        _parse_porcelain(output, max_records=2)


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


def test_build_review_packet_blocks_non_object_graph_bundle() -> None:
    packet = build_review_packet(
        root_name="sample",
        changes=[Change("src/store.py", "modified")],
        discovery="git",
        graph_bundle=[],  # type: ignore[arg-type]
        graph_status={},
        depth=2,
    )

    assert packet["impact"] == {
        "matched_nodes": [],
        "missing": [],
        "impacted_files": [],
        "likely_tests": [],
    }
    assert packet["graph"]["validation"] == {
        "ok": False,
        "error_count": 1,
        "warning_count": 0,
        "node_count": 0,
        "edge_count": 0,
        "error_codes": ["bundle_type"],
        "warning_codes": [],
    }
    assert [item["code"] for item in packet["blockers"]] == ["INVALID_GRAPH"]


@pytest.mark.parametrize("unsafe_source_file", [123, "src/api.py\u202e"])
def test_build_review_packet_blocks_unsafe_graph_source_files(
    unsafe_source_file: object,
) -> None:
    bundle = _review_bundle()
    assert isinstance(bundle["nodes"], list)
    target = "store" if isinstance(unsafe_source_file, int) else "api_0"
    for node in bundle["nodes"]:
        if isinstance(node, dict) and node.get("id") == target:
            node["source_file"] = unsafe_source_file

    packet = _packet([Change("src/store.py", "modified")], bundle=bundle)

    assert packet["impact"] == {
        "matched_nodes": [],
        "missing": [],
        "impacted_files": [],
        "likely_tests": [],
    }
    assert packet["graph"]["validation"]["ok"] is False
    assert packet["graph"]["validation"]["error_codes"] == ["graph_processing_error"]
    assert [item["code"] for item in packet["blockers"]] == ["INVALID_GRAPH"]
    assert "\u202e" not in repr(packet)
    assert "AttributeError" not in repr(packet)


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


from graphite.review import MAX_REVIEW_PACKET_ITEMS


def test_review_packet_truncates_changes_and_flags_output():
    changes = [Change(f"a/f{index:06d}.py", "modified") for index in range(MAX_REVIEW_PACKET_ITEMS + 1)]
    packet = _packet(changes)
    assert len(packet["changes"]) == MAX_REVIEW_PACKET_ITEMS
    assert {"code": "OUTPUT_TRUNCATED", "message": "Review output was truncated to a bounded size; some entries are not shown."} in packet["warnings"]


def test_review_packet_within_cap_is_not_flagged():
    packet = _packet([Change("src/store.py", "modified")])
    assert len(packet["changes"]) == 1
    assert all(warning.get("code") != "OUTPUT_TRUNCATED" for warning in packet["warnings"])


def test_review_packet_detects_sensitive_change_beyond_cap():
    # A sensitive change sorted BEYOND the cap index is dropped from the serialized
    # list but MUST still raise its risk signal — detection runs over the full set.
    bulk = [Change(f"a/f{index:06d}.py", "modified") for index in range(MAX_REVIEW_PACKET_ITEMS + 1)]
    changes = bulk + [Change("z/pyproject.toml", "modified")]  # basename => sensitive; sorts last
    packet = _packet(changes)
    serialized_paths = {change["path"] for change in packet["changes"]}
    assert "z/pyproject.toml" not in serialized_paths  # truncated out of the visible list
    assert "SENSITIVE_CONFIG" in packet["risk"]["signals"]  # but detected over the full set
    assert len(packet["changes"]) == MAX_REVIEW_PACKET_ITEMS


def test_review_packet_truncates_impact_lists():
    bundle = _review_bundle(dependent_count=MAX_REVIEW_PACKET_ITEMS + 1)
    packet = _packet([Change("src/store.py", "modified")], bundle=bundle)
    assert len(packet["impact"]["impacted_files"]) == MAX_REVIEW_PACKET_ITEMS
    assert {"code": "OUTPUT_TRUNCATED", "message": "Review output was truncated to a bounded size; some entries are not shown."} in packet["warnings"]


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


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.py",
        "C:\\private\\absolute.py",
        "../escape.py",
        "src/../escape.py",
        "src/bad\nname.py",
        "src/bad\x1b[31m.py",
        "src/bad\x85name.py",
        "src/bad\u202ename.py",
        "src/bad\u2028name.py",
        "src/bad\u2029name.py",
        "src/bad\x00.py",
    ],
)
def test_build_review_packet_rejects_unsafe_change_paths(path: str) -> None:
    with pytest.raises(ReviewError) as error:
        _packet([Change(path, "modified")])

    assert str(error.value) == "change path is invalid"
    assert path not in str(error.value)


@pytest.mark.parametrize("status", ["copied", None, []])
def test_build_review_packet_rejects_unknown_change_status(status: object) -> None:
    with pytest.raises(ReviewError, match="^change status is invalid$"):
        _packet([Change("src/store.py", status)])  # type: ignore[arg-type]


@pytest.mark.parametrize("discovery", ["automatic", None, []])
def test_build_review_packet_rejects_unknown_discovery_mode(discovery: object) -> None:
    with pytest.raises(ReviewError, match="^review discovery is invalid$"):
        build_review_packet(
            root_name="sample",
            changes=[],
            discovery=discovery,  # type: ignore[arg-type]
            graph_bundle=_review_bundle(),
            graph_status={},
            depth=1,
        )


@pytest.mark.parametrize(
    "root_name", ["", "   ", "nested/project", "nested\\project", "bad\nname", "bad\u202ename"]
)
def test_build_review_packet_rejects_unsafe_project_label(root_name: str) -> None:
    with pytest.raises(ReviewError, match="^project label is invalid$"):
        build_review_packet(
            root_name=root_name,
            changes=[],
            discovery="git",
            graph_bundle=_review_bundle(),
            graph_status={},
            depth=1,
        )


@pytest.mark.parametrize("graph_status", [None, []])
def test_build_review_packet_rejects_malformed_graph_status(graph_status: object) -> None:
    with pytest.raises(ReviewError, match="^graph status is invalid$"):
        build_review_packet(
            root_name="sample",
            changes=[],
            discovery="git",
            graph_bundle=_review_bundle(),
            graph_status=graph_status,  # type: ignore[arg-type]
            depth=1,
        )


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


@pytest.mark.parametrize(
    "packet",
    [
        None,
        [],
        {"graph": []},
        {"changes": [None]},
        {"impact": []},
        {"risk": []},
    ],
)
def test_format_review_markdown_rejects_malformed_packets(packet: object) -> None:
    with pytest.raises(ReviewError) as error:
        format_review_markdown(packet)  # type: ignore[arg-type]

    assert str(error.value) == "review packet is invalid"


def test_format_review_markdown_drops_unicode_format_characters_defensively() -> None:
    markdown = format_review_markdown({"project": "safe\u202eunsafe"})

    assert "\u202e" not in markdown


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_format_review_markdown_drops_unicode_line_separators(separator: str) -> None:
    markdown = format_review_markdown(
        {"changes": [{"path": f"src/safe{separator}unsafe.py", "status": "modified"}]}
    )

    assert separator not in markdown


def test_private_use_filename_is_preserved_in_packet_and_markdown() -> None:
    path = "src/private\ue000.py"

    packet = _packet([Change(path, "modified")])
    markdown = format_review_markdown(packet)

    assert packet["changes"] == [{"path": path, "status": "modified"}]
    assert path in markdown


def test_review_changes_cli_explicit_json_is_deterministic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "sample-project"
    _write_review_project(root)
    argv = ["review-changes", str(root), str(root / "src/store.py"), "--json"]

    assert main(argv) == 0
    first = capsys.readouterr()
    assert main(argv) == 0
    second = capsys.readouterr()

    assert first.out == second.out
    assert first.err == second.err == ""
    packet = json.loads(first.out)
    assert packet["schema_version"] == 1
    assert packet["project"] == root.name
    assert packet["discovery"] == "explicit"
    assert packet["impact"]["impacted_files"] == ["src/api.py"]
    assert packet["impact"]["likely_tests"] == ["tests/test_store.py"]
    forbidden_keys = {"llm", "model", "timestamp", "created_at", "generated_at", "datetime"}
    assert _collect_keys(packet).isdisjoint(forbidden_keys)
    serialized = json.dumps(packet)
    assert str(root.resolve()) not in serialized


def test_review_explicit_freshness_uses_only_shared_hardened_git_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "sample-project"
    repository_bin = root / "repo-bin"
    trusted_bin = tmp_path / "trusted-bin"
    _write_review_project(root)
    (root / ".git").mkdir()
    repository_bin.mkdir()
    trusted_bin.mkdir()
    executable_name = "git.exe" if os.name == "nt" else "git"
    _write(repository_bin / executable_name, "malicious")
    _write(trusted_bin / executable_name, "trusted")
    if os.name != "nt":
        (repository_bin / executable_name).chmod(0o700)
        (trusted_bin / executable_name).chmod(0o700)
    monkeypatch.setenv("PATH", os.pathsep.join((str(repository_bin), str(trusted_bin))))
    for name in ("GiT_DiR", "GIT_INDEX_FILE", "git_exec_path"):
        monkeypatch.setenv(name, "private")
    source_environment = dict(os.environ)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(command: list[str], **kwargs: object) -> _FakeGitProcess:
        calls.append((command, kwargs))
        if command[1:] == ["--version"]:
            return _FakeGitProcess(b"git version 2.38.0\n")
        return _FakeGitProcess(b"src/store.py\0tests/test_store.py\0")

    monkeypatch.setattr(git_module.subprocess, "Popen", fake_popen)

    assert main(["review-changes", str(root), "src/store.py", "--json"]) == 0

    packet = json.loads(capsys.readouterr().out)
    assert packet["graph"]["status"]["stale"] is False
    assert len(calls) == 2
    assert calls[0][0][1:] == ["--version"]
    command, kwargs = calls[1]
    assert Path(command[0]) == (trusted_bin / executable_name).resolve()
    assert command[1:4] == ["--no-optional-locks", "-c", "core.fsmonitor=false"]
    assert command[4:6] == ["-c", f"safe.directory={root.resolve()}"]
    assert command[6:] == ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert {name for name in kwargs["env"] if name.casefold().startswith("git_")} == {
        "GIT_OPTIONAL_LOCKS",
        "GIT_TERMINAL_PROMPT",
    }
    assert dict(os.environ) == source_environment


def test_review_changes_cli_markdown_contains_evidence(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "markdown-project"
    _write_review_project(root)

    assert main(["review-changes", str(root), "src/store.py"]) == 0

    output = capsys.readouterr()
    assert output.err == ""
    assert "# Graphite Change Review" in output.out
    assert "## Changes" in output.out
    assert "## Impact" in output.out
    assert "`src/store.py`" in output.out
    assert "`tests/test_store.py`" in output.out


def test_review_changes_cli_missing_graph_only_fails_when_requested(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "missing-graph"
    root.mkdir()

    assert main(["review-changes", str(root), "src/store.py", "--json"]) == 0
    packet = json.loads(capsys.readouterr().out)
    assert [item["code"] for item in packet["blockers"]] == ["MISSING_GRAPH"]

    assert main(["review-changes", str(root), "src/store.py", "--json", "--fail-on-blocker"]) == 1
    packet = json.loads(capsys.readouterr().out)
    assert [item["code"] for item in packet["blockers"]] == ["MISSING_GRAPH"]


@pytest.mark.parametrize("graph_content, expected", [("{not-json", "MISSING_GRAPH"), ("[]", "INVALID_GRAPH")])
def test_review_changes_cli_invalid_graph_is_safe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    graph_content: str,
    expected: str,
) -> None:
    root = tmp_path / ("private-" + "x" * 40)
    _write(root / "graph-out/graph.json", graph_content)

    assert main(["review-changes", str(root), "src/store.py", "--json"]) == 0

    output = capsys.readouterr()
    packet = json.loads(output.out)
    assert [item["code"] for item in packet["blockers"]] == [expected]
    assert output.err == ""
    assert str(root) not in output.out
    assert "JSONDecodeError" not in output.out


@pytest.mark.parametrize("graph_argument", ["../outside.json", "{outside}"])
def test_review_changes_cli_rejects_graph_paths_outside_project(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    graph_argument: str,
) -> None:
    root = tmp_path / "project"
    outside = tmp_path / ("private-" + "x" * 60 + ".json")
    _write_review_project(root)
    _write(outside, json.dumps(_review_bundle()))
    argument = graph_argument.format(outside=outside)

    assert main(["review-changes", str(root), "src/store.py", "--graph-json", argument, "--json"]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "[graphite] error: graph path must be within project root\n"
    assert str(outside) not in output.err


def test_review_changes_cli_rejects_graph_symlink_escape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    outside = tmp_path / "outside.json"
    link = root / "custom/graph.json"
    _write_review_project(root)
    _write(outside, json.dumps(_review_bundle()))
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    assert main(["review-changes", str(root), "src/store.py", "--graph-json", "custom/graph.json", "--json"]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "[graphite] error: graph path must be within project root\n"
    assert str(outside) not in output.err


def test_review_changes_cli_bounds_graph_input_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "oversize-private-project"
    _write_review_project(root)
    monkeypatch.setattr(cli_module, "_MAX_REVIEW_GRAPH_BYTES", 32, raising=False)

    assert main(["review-changes", str(root), "src/store.py", "--json"]) == 0

    output = capsys.readouterr()
    packet = json.loads(output.out)
    assert [item["code"] for item in packet["blockers"]] == ["MISSING_GRAPH"]
    assert output.err == ""
    assert str(root) not in output.out
    assert "too large" not in output.out.casefold()


@pytest.mark.parametrize(
    ("graph_bytes", "raw_error", "expected_blockers"),
    [
        (b"\xff\xfeprivate", "UnicodeDecodeError", {"MISSING_GRAPH"}),
        (
            b"[" * 2000 + b"0" + b"]" * 2000,
            "RecursionError",
            {"MISSING_GRAPH", "INVALID_GRAPH"},
        ),
        (b'{"nodes":', "JSONDecodeError", {"MISSING_GRAPH"}),
    ],
)
def test_review_changes_cli_sanitizes_unreadable_graph_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    graph_bytes: bytes,
    raw_error: str,
    expected_blockers: set[str],
) -> None:
    root = tmp_path / "unreadable-private-project"
    _write_review_project(root)
    (root / "graph-out/graph.json").write_bytes(graph_bytes)

    assert main(["review-changes", str(root), "src/store.py", "--json"]) == 0

    output = capsys.readouterr()
    packet = json.loads(output.out)
    assert len(packet["blockers"]) == 1
    assert packet["blockers"][0]["code"] in expected_blockers
    assert output.err == ""
    assert str(root) not in output.out
    assert raw_error not in output.out


def test_review_changes_cli_sanitizes_graph_parser_recursion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "recursion-private-project"
    _write_review_project(root)

    def recursive_loads(_value: object) -> object:
        raise RecursionError("private parser detail")

    monkeypatch.setattr(cli_module.json, "loads", recursive_loads)

    bundle, error = cli_module._load_review_graph(root / "graph-out/graph.json")

    assert bundle is None
    assert error == "dependency graph is unavailable"
    assert "private parser detail" not in error


def test_review_changes_cli_high_risk_without_blocker_does_not_fail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "broad-impact"
    _write_review_project(root, bundle=_review_bundle(dependent_count=10))

    assert main(["review-changes", str(root), "src/store.py", "--json", "--fail-on-blocker"]) == 0

    packet = json.loads(capsys.readouterr().out)
    assert packet["risk"]["level"] == "high"
    assert packet["blockers"] == []


def test_review_changes_cli_discovers_real_git_changes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "git-project"
    _write_review_project(root)
    _git(root, "init")
    _git(root, "config", "user.email", "review@example.test")
    _git(root, "config", "user.name", "Review Tests")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    _write(root / "src/store.py", "value = 2\n")

    assert main(["review-changes", str(root), "--json"]) == 0

    packet = json.loads(capsys.readouterr().out)
    assert packet["discovery"] == "git"
    assert packet["changes"] == [{"path": "src/store.py", "status": "modified"}]


def test_review_changes_cli_non_git_failure_is_sanitized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / ("secret-" + "x" * 80)
    root.mkdir()

    assert main(["review-changes", str(root), "--json"]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "[graphite] error: not a Git worktree\n"
    assert str(root) not in output.err


def test_review_changes_cli_relative_graph_is_project_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "project"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write_review_project(root, graph_path="custom/graph.json")
    monkeypatch.chdir(elsewhere)

    assert main(["review-changes", str(root), "src/store.py", "--graph-json", "custom/graph.json", "--json"]) == 0

    packet = json.loads(capsys.readouterr().out)
    assert packet["blockers"] == []
    assert packet["impact"]["likely_tests"] == ["tests/test_store.py"]


def test_review_changes_cli_custom_graph_uses_sibling_manifest_for_freshness(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "custom-freshness"
    _write_review_project(root, graph_path="custom/graph.json")
    _write_review_manifest(root, "graph-out", ())
    _write_review_manifest(
        root,
        "custom",
        ("src/store.py", "tests/test_store.py"),
    )

    assert main(["review-changes", str(root), "src/store.py", "--graph-json", "custom/graph.json", "--json"]) == 0

    packet = json.loads(capsys.readouterr().out)
    assert packet["graph"]["status"]["stale"] is False


def test_review_changes_cli_missing_custom_manifest_is_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "custom-missing-manifest"
    _write_review_project(root, graph_path="custom/graph.json")
    _write_review_manifest(
        root,
        "graph-out",
        ("custom/graph.json", "src/store.py", "tests/test_store.py"),
    )

    assert main(["review-changes", str(root), "src/store.py", "--graph-json", "custom/graph.json", "--json"]) == 0

    packet = json.loads(capsys.readouterr().out)
    assert packet["graph"]["status"] == {
        "added": [],
        "changed": [],
        "reason": "missing",
        "removed": [],
        "stale": True,
    }


def test_review_changes_cli_help_documents_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="0"):
        main(["review-changes", "--help"])

    output = capsys.readouterr().out
    assert "Produce deterministic review evidence and acceptance criteria" in output
    for option in ("--graph-json", "--depth", "--git-timeout", "--fail-on-blocker", "--json"):
        assert option in output
    assert "relative to the project root" in output


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("--depth", "-1", "must be zero or greater"),
        ("--git-timeout", "0", "must be finite and greater than zero"),
        ("--git-timeout", "-1", "must be finite and greater than zero"),
        ("--git-timeout", "nan", "must be finite and greater than zero"),
        ("--git-timeout", "inf", "must be finite and greater than zero"),
        ("--git-timeout", "-inf", "must be finite and greater than zero"),
    ],
)
def test_review_changes_cli_rejects_invalid_limits_with_argparse(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    option: str,
    value: str,
    message: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()

    with pytest.raises(SystemExit) as error:
        main(["review-changes", str(root), "src/store.py", f"{option}={value}", "--json"])

    assert error.value.code == 2
    output = capsys.readouterr().err
    assert f"argument {option}" in output
    assert message in output


def test_review_changes_cli_rejects_nonexistent_root_safely(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / ("missing-secret-" + "x" * 80)

    assert main(["review-changes", str(missing), "src/store.py", "--json"]) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert str(missing) not in output.err
    assert len(output.err) < 200
