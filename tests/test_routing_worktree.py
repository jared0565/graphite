from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import graphite.routing.worktree as worktree_module
from graphite.routing.worktree import (
    WorktreeError,
    cleanup_task_worktree,
    create_task_worktree,
)
from graphite.routing.diff_policy import DiffPolicyError, collect_diff_evidence


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Graphite Tests")
    (root / "app.py").write_text("print('baseline')\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-m", "baseline")
    return root, _git(root, "rev-parse", "HEAD")


def test_create_task_worktree_is_detached_at_bound_commit_outside_source(tmp_path: Path) -> None:
    source, commit = _repository(tmp_path)
    state = tmp_path / "private-state"

    prepared = create_task_worktree(
        source_root=source,
        state_root=state,
        task_id="task-123",
        approved_commit=commit,
    )

    assert prepared.root == (state / "tasks" / "task-123").resolve()
    assert prepared.baseline_commit == commit
    assert prepared.worktree_id == "task-123"
    assert prepared.status == "prepared"
    assert _git(prepared.root, "rev-parse", "HEAD") == commit
    detached = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=prepared.root, check=False
    )
    assert detached.returncode == 1
    assert prepared.git_common_dir == (source / ".git").resolve()


def test_create_task_worktree_rejects_dirty_source_without_mutating_state(tmp_path: Path) -> None:
    source, commit = _repository(tmp_path)
    (source / "app.py").write_text("dirty\n", encoding="utf-8")
    state = tmp_path / "private-state"

    with pytest.raises(WorktreeError, match="^source_dirty$"):
        create_task_worktree(
            source_root=source,
            state_root=state,
            task_id="task-123",
            approved_commit=commit,
        )
    assert not (state / "tasks" / "task-123").exists()


def test_create_task_worktree_rejects_commit_drift_and_state_inside_repository(
    tmp_path: Path,
) -> None:
    source, commit = _repository(tmp_path)
    (source / "later.py").write_text("later = True\n", encoding="utf-8")
    _git(source, "add", "later.py")
    _git(source, "commit", "-m", "later")

    with pytest.raises(WorktreeError, match="^source_commit_drift$"):
        create_task_worktree(
            source_root=source,
            state_root=tmp_path / "state",
            task_id="task-123",
            approved_commit=commit,
        )
    with pytest.raises(WorktreeError, match="^state_root_invalid$"):
        create_task_worktree(
            source_root=source,
            state_root=source / ".graphite" / "tasks",
            task_id="task-124",
            approved_commit=_git(source, "rev-parse", "HEAD"),
        )


def test_create_task_worktree_rejects_symlinked_tracked_content(tmp_path: Path) -> None:
    source, _ = _repository(tmp_path)
    target = source / "target.txt"
    target.write_text("target\n", encoding="utf-8")
    link = source / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    _git(source, "add", "target.txt", "linked.txt")
    _git(source, "commit", "-m", "symlink")
    commit = _git(source, "rev-parse", "HEAD")

    with pytest.raises(WorktreeError, match="^source_special_file$"):
        create_task_worktree(
            source_root=source,
            state_root=tmp_path / "state",
            task_id="task-link",
            approved_commit=commit,
        )


def test_collect_real_diff_binds_tracked_and_untracked_content(tmp_path: Path) -> None:
    source, commit = _repository(tmp_path)
    prepared = create_task_worktree(
        source_root=source,
        state_root=tmp_path / "state",
        task_id="task-diff",
        approved_commit=commit,
    )
    (prepared.root / "app.py").write_text("print('changed')\n", encoding="utf-8")
    (prepared.root / "new.py").write_text("new = True\n", encoding="utf-8")

    first = collect_diff_evidence(prepared, max_files=8, max_bytes=32_768)
    assert first.changed_files == 2
    assert first.changed_bytes > 0

    (prepared.root / "new.py").write_text("new = False\n", encoding="utf-8")
    second = collect_diff_evidence(prepared, max_files=8, max_bytes=32_768)
    assert first.diff_sha256 != second.diff_sha256


def test_cleanup_requires_explicit_authority_terminal_state_and_matching_identity(
    tmp_path: Path,
) -> None:
    source, commit = _repository(tmp_path)
    state = tmp_path / "state"
    prepared = create_task_worktree(
        source_root=source,
        state_root=state,
        task_id="task-cleanup",
        approved_commit=commit,
    )
    with pytest.raises(WorktreeError, match="^cleanup_authority_required$"):
        cleanup_task_worktree(
            prepared,
            state_root=state,
            task_id="task-cleanup",
            terminal_status="accepted",
            authority_granted=False,
        )
    assert prepared.root.exists()
    with pytest.raises(WorktreeError, match="^cleanup_state_invalid$"):
        cleanup_task_worktree(
            prepared,
            state_root=state,
            task_id="wrong-task",
            terminal_status="accepted",
            authority_granted=True,
        )
    assert prepared.root.exists()

    cleanup_task_worktree(
        prepared,
        state_root=state,
        task_id="task-cleanup",
        terminal_status="accepted",
        authority_granted=True,
    )
    assert not prepared.root.exists()


def test_collect_diff_does_not_hide_ignored_sensitive_files(tmp_path: Path) -> None:
    source, _ = _repository(tmp_path)
    (source / ".gitignore").write_text(".env\n", encoding="utf-8")
    _git(source, "add", ".gitignore")
    _git(source, "commit", "-m", "ignore environment")
    commit = _git(source, "rev-parse", "HEAD")
    prepared = create_task_worktree(
        source_root=source,
        state_root=tmp_path / "state",
        task_id="task-ignored",
        approved_commit=commit,
    )
    (prepared.root / ".env").write_text("TOKEN=must-not-survive\n", encoding="utf-8")

    with pytest.raises(DiffPolicyError, match="^sensitive_path$"):
        collect_diff_evidence(prepared, max_files=8, max_bytes=32_768)


def test_create_rejects_simulated_source_reparse_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, commit = _repository(tmp_path)
    monkeypatch.setattr(worktree_module, "_is_reparse", lambda _metadata: True)
    with pytest.raises(WorktreeError, match="^source_root_invalid$"):
        create_task_worktree(
            source_root=source,
            state_root=tmp_path / "state",
            task_id="task-reparse",
            approved_commit=commit,
        )


def test_collect_rejects_nested_untracked_repository(tmp_path: Path) -> None:
    source, commit = _repository(tmp_path)
    prepared = create_task_worktree(
        source_root=source,
        state_root=tmp_path / "state",
        task_id="task-nested",
        approved_commit=commit,
    )
    nested = prepared.root / "nested"
    nested.mkdir()
    _git(nested, "init")
    with pytest.raises(DiffPolicyError, match="^special_file_change$"):
        collect_diff_evidence(prepared, max_files=8, max_bytes=32_768)
