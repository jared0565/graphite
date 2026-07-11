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
    discover_git_changes,
    normalize_explicit_changes,
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


def test_parse_porcelain_rejects_incomplete_rename() -> None:
    with pytest.raises(ReviewError, match="missing rename source"):
        _parse_porcelain(b"R  renamed.py\0")
