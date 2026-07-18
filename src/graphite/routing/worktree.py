"""Approval-bound, isolated Git worktree preparation."""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from graphite.git import (
    GitError,
    GitOutputLimitError,
    GitResult,
    GitRunner,
    GitTimeoutError,
)

_TASK_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT: Final = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
GIT_TIMEOUT_SECONDS: Final = 15.0
MAX_GIT_METADATA_BYTES: Final = 8 * 1024 * 1024


class WorktreeError(RuntimeError):
    """Stable, path-free worktree preparation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TaskWorktree:
    worktree_id: str
    root: Path
    git_common_dir: Path
    baseline_commit: str
    status: str = "prepared"


_CLEANUP_TERMINAL_STATES: Final = frozenset({"accepted", "rejected", "abandoned"})


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _canonical_directory(path: Path, code: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise WorktreeError(code)
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise WorktreeError(code) from None
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise WorktreeError(code)
    return resolved


def _secure_directory(path: Path, code: str) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        raise WorktreeError(code) from None
    resolved = _canonical_directory(path, code)
    if os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError:
            raise WorktreeError(code) from None
    return resolved


def _run(runner: GitRunner, arguments: list[str], *, maximum: int = MAX_GIT_METADATA_BYTES) -> GitResult:
    try:
        return runner.run(
            arguments,
            timeout_seconds=GIT_TIMEOUT_SECONDS,
            max_stdout_bytes=maximum,
        )
    except GitTimeoutError:
        raise WorktreeError("git_timeout") from None
    except GitOutputLimitError:
        raise WorktreeError("git_output_limit") from None
    except GitError:
        raise WorktreeError("git_unavailable") from None


def _decode_line(output: bytes, code: str) -> str:
    try:
        value = output.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        raise WorktreeError(code) from None
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise WorktreeError(code)
    return value


def _resolve_git_path(root: Path, raw: bytes, code: str) -> Path:
    value = _decode_line(raw, code)
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return candidate.resolve(strict=True)
    except OSError:
        raise WorktreeError(code) from None


def _validate_index(output: bytes) -> None:
    if output and not output.endswith(b"\0"):
        raise WorktreeError("git_protocol")
    folded: dict[str, str] = {}
    for record in output[:-1].split(b"\0") if output else ():
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, _object_id, stage = header.split(b" ")
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise WorktreeError("git_protocol") from None
        if stage != b"0" or mode in {b"120000", b"160000"}:
            raise WorktreeError("source_special_file")
        if not path or "\\" in path or "\x00" in path:
            raise WorktreeError("git_protocol")
        collision = path.casefold()
        if collision in folded and folded[collision] != path:
            raise WorktreeError("source_path_collision")
        folded[collision] = path


def create_task_worktree(
    *,
    source_root: Path,
    state_root: Path,
    task_id: str,
    approved_commit: str,
) -> TaskWorktree:
    """Create one detached worktree only from an exact clean approved commit."""
    if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
        raise WorktreeError("task_id_invalid")
    if not isinstance(approved_commit, str) or _COMMIT.fullmatch(approved_commit) is None:
        raise WorktreeError("commit_invalid")
    source = _canonical_directory(source_root, "source_root_invalid")
    state_candidate = state_root.resolve(strict=False)
    try:
        state_candidate.relative_to(source)
    except ValueError:
        pass
    else:
        raise WorktreeError("state_root_invalid")
    state = _secure_directory(state_root, "state_root_invalid")
    tasks = _secure_directory(state / "tasks", "state_root_invalid")
    target = tasks / task_id
    if target.exists() or target.is_symlink():
        raise WorktreeError("worktree_exists")

    runner = GitRunner(source)
    top = _run(runner, ["rev-parse", "--show-toplevel"])
    if top.returncode != 0 or _resolve_git_path(source, top.stdout, "source_root_invalid") != source:
        raise WorktreeError("source_root_invalid")
    head = _run(runner, ["rev-parse", "HEAD"])
    if head.returncode != 0 or _decode_line(head.stdout, "git_protocol") != approved_commit:
        raise WorktreeError("source_commit_drift")
    status_result = _run(runner, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if status_result.returncode != 0:
        raise WorktreeError("source_root_invalid")
    if status_result.stdout:
        raise WorktreeError("source_dirty")
    index = _run(runner, ["ls-files", "-s", "-z"])
    if index.returncode != 0:
        raise WorktreeError("source_root_invalid")
    _validate_index(index.stdout)
    source_common_result = _run(runner, ["rev-parse", "--git-common-dir"])
    if source_common_result.returncode != 0:
        raise WorktreeError("source_root_invalid")
    source_common = _resolve_git_path(source, source_common_result.stdout, "source_root_invalid")

    added = _run(
        runner,
        ["worktree", "add", "--detach", "--no-checkout", str(target), approved_commit],
    )
    if added.returncode != 0:
        raise WorktreeError("worktree_create_failed")
    checkout_runner = GitRunner(target)
    checkout = _run(checkout_runner, ["checkout", "--detach", approved_commit])
    if checkout.returncode != 0:
        raise WorktreeError("worktree_quarantined")
    prepared_root = _canonical_directory(target, "worktree_quarantined")
    prepared_head = _run(checkout_runner, ["rev-parse", "HEAD"])
    if prepared_head.returncode != 0 or _decode_line(prepared_head.stdout, "git_protocol") != approved_commit:
        raise WorktreeError("worktree_quarantined")
    symbolic = _run(checkout_runner, ["symbolic-ref", "-q", "HEAD"])
    if symbolic.returncode == 0:
        raise WorktreeError("worktree_quarantined")
    common_result = _run(checkout_runner, ["rev-parse", "--git-common-dir"])
    if common_result.returncode != 0:
        raise WorktreeError("worktree_quarantined")
    common = _resolve_git_path(prepared_root, common_result.stdout, "worktree_quarantined")
    if common != source_common:
        raise WorktreeError("worktree_quarantined")
    final_status = _run(
        checkout_runner, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    )
    if final_status.returncode != 0 or final_status.stdout:
        raise WorktreeError("worktree_quarantined")
    return TaskWorktree(task_id, prepared_root, common, approved_commit)


def cleanup_task_worktree(
    worktree: TaskWorktree,
    *,
    state_root: Path,
    task_id: str,
    terminal_status: str,
    authority_granted: bool,
) -> None:
    """Remove a terminal task worktree only under explicit, rebound authority."""
    if authority_granted is not True:
        raise WorktreeError("cleanup_authority_required")
    if (
        not isinstance(worktree, TaskWorktree)
        or task_id != worktree.worktree_id
        or terminal_status not in _CLEANUP_TERMINAL_STATES
    ):
        raise WorktreeError("cleanup_state_invalid")
    state = _canonical_directory(state_root, "state_root_invalid")
    expected = state / "tasks" / task_id
    root = _canonical_directory(worktree.root, "cleanup_containment_invalid")
    try:
        expected_resolved = expected.resolve(strict=True)
    except OSError:
        raise WorktreeError("cleanup_containment_invalid") from None
    if root != expected_resolved or root != worktree.root:
        raise WorktreeError("cleanup_containment_invalid")
    checkout_runner = GitRunner(root)
    common_result = _run(checkout_runner, ["rev-parse", "--git-common-dir"])
    if common_result.returncode != 0:
        raise WorktreeError("cleanup_containment_invalid")
    common = _resolve_git_path(root, common_result.stdout, "cleanup_containment_invalid")
    if common != worktree.git_common_dir or common.name != ".git":
        raise WorktreeError("cleanup_containment_invalid")
    source = _canonical_directory(common.parent, "cleanup_containment_invalid")
    source_runner = GitRunner(source)
    removed = _run(source_runner, ["worktree", "remove", "--force", str(root)])
    if removed.returncode != 0 or root.exists() or root.is_symlink():
        raise WorktreeError("cleanup_failed")
