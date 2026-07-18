"""Fail-closed inspection of untrusted model-generated worktree changes."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final

from graphite.git import GitError, GitOutputLimitError, GitRunner, GitTimeoutError

from .worktree import TaskWorktree

_COMMIT: Final = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SENSITIVE_COMPONENTS: Final = frozenset({"secret", "secrets", "credential", "credentials"})
_SENSITIVE_NAMES: Final = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "id_rsa",
        "id_ed25519",
        "service-account.json",
    }
)
_SENSITIVE_SUFFIXES: Final = (".pem", ".key", ".p12", ".pfx")
_REJECTED_CI_PREFIXES: Final = (".github/workflows/", ".gitlab/")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
GIT_TIMEOUT_SECONDS: Final = 15.0
MAX_METADATA_BYTES: Final = 8 * 1024 * 1024


class DiffPolicyError(RuntimeError):
    """Stable unsafe-diff failure with no changed path in its text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class DiffEvidence:
    baseline_commit: str
    changed_files: int
    changed_bytes: int
    diff_sha256: str
    changed_paths: tuple[str, ...] = field(repr=False)


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _records(output: bytes) -> list[bytes]:
    if not isinstance(output, bytes) or (output and not output.endswith(b"\0")):
        raise DiffPolicyError("git_protocol")
    return output[:-1].split(b"\0") if output else []


def _path(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise DiffPolicyError("path_invalid") from None
    candidate = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or candidate.is_absolute()
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise DiffPolicyError("path_invalid")
    folded = value.casefold()
    parts = tuple(part.casefold() for part in candidate.parts)
    name = parts[-1]
    if parts[0] == ".git" or name == ".git" or folded.startswith(".git/"):
        raise DiffPolicyError("git_control_path")
    if (
        name in _SENSITIVE_NAMES
        or name.startswith(".env.")
        or name.startswith("secret.")
        or name.startswith("secrets.")
        or name.startswith("credential.")
        or name.startswith("credentials.")
        or name.endswith(_SENSITIVE_SUFFIXES)
        or any(part in _SENSITIVE_COMPONENTS for part in parts)
        or any(folded.startswith(prefix) for prefix in _REJECTED_CI_PREFIXES)
    ):
        raise DiffPolicyError("sensitive_path")
    return value


def _status_paths(output: bytes) -> set[str]:
    records = _records(output)
    paths: set[str] = set()
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) < 4 or record[2:3] != b" " or record[:2] == b"  ":
            raise DiffPolicyError("git_protocol")
        if b"U" in record[:2]:
            raise DiffPolicyError("unmerged_change")
        paths.add(_path(record[3:]))
        if b"R" in record[:2] or b"C" in record[:2]:
            index += 1
            if index >= len(records):
                raise DiffPolicyError("git_protocol")
            paths.add(_path(records[index]))
        index += 1
    return paths


def _name_status_paths(output: bytes) -> set[str]:
    records = _records(output)
    paths: set[str] = set()
    index = 0
    while index < len(records):
        status = records[index]
        index += 1
        if not status or status[:1] not in b"ACDMRTUXB":
            raise DiffPolicyError("git_protocol")
        count = 2 if status[:1] in {b"R", b"C"} else 1
        if index + count > len(records):
            raise DiffPolicyError("git_protocol")
        for raw in records[index : index + count]:
            paths.add(_path(raw))
        index += count
    return paths


def _numstat(output: bytes) -> tuple[set[str], int]:
    paths: set[str] = set()
    churn = 0
    records = _records(output)
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        try:
            added, deleted, raw_path = record.split(b"\t", 2)
        except ValueError:
            raise DiffPolicyError("git_protocol") from None
        if added == b"-" or deleted == b"-":
            raise DiffPolicyError("binary_change")
        try:
            additions = int(added)
            deletions = int(deleted)
        except ValueError:
            raise DiffPolicyError("git_protocol") from None
        if additions < 0 or deletions < 0:
            raise DiffPolicyError("git_protocol")
        churn += additions + deletions
        if raw_path:
            paths.add(_path(raw_path))
        else:
            if index + 2 > len(records):
                raise DiffPolicyError("git_protocol")
            paths.add(_path(records[index]))
            paths.add(_path(records[index + 1]))
            index += 2
    return paths, churn


def _raw_paths_and_modes(output: bytes) -> set[str]:
    records = _records(output)
    paths: set[str] = set()
    index = 0
    while index < len(records):
        header = records[index]
        index += 1
        match = re.fullmatch(
            rb":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40}|[0-9a-f]{64}) ([0-9a-f]{40}|[0-9a-f]{64}) ([A-Z][0-9]{0,3})",
            header,
        )
        if match is None:
            raise DiffPolicyError("git_protocol")
        old_mode, new_mode, _old_hash, _new_hash, status = match.groups()
        if status[:1] == b"U":
            raise DiffPolicyError("unmerged_change")
        if b"160000" in {old_mode, new_mode}:
            raise DiffPolicyError("submodule_change")
        if b"120000" in {old_mode, new_mode}:
            raise DiffPolicyError("special_file_change")
        if b"100755" in {old_mode, new_mode}:
            raise DiffPolicyError("executable_change")
        count = 2 if status[:1] in {b"R", b"C"} else 1
        if index + count > len(records):
            raise DiffPolicyError("git_protocol")
        for raw in records[index : index + count]:
            paths.add(_path(raw))
        index += count
    return paths


def _validate_filesystem(
    root: Path, paths: set[str], untracked_paths: set[str], *, max_bytes: int
) -> tuple[int, bytes]:
    untracked_bytes = 0
    manifest = hashlib.sha256()
    for relative in paths:
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        parent = candidate.parent
        while parent != root:
            try:
                metadata = parent.lstat()
            except OSError:
                raise DiffPolicyError("containment_uncertain") from None
            if stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE):
                raise DiffPolicyError("containment_uncertain")
            parent = parent.parent
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise DiffPolicyError("containment_uncertain") from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)
            or not stat.S_ISREG(metadata.st_mode)
        ):
            raise DiffPolicyError("special_file_change")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            raise DiffPolicyError("containment_uncertain") from None
        if relative in untracked_paths:
            if metadata.st_size > max_bytes - untracked_bytes:
                raise DiffPolicyError("byte_limit")
            content_digest = hashlib.sha256()
            try:
                with candidate.open("rb") as handle:
                    while chunk := handle.read(64 * 1024):
                        content_digest.update(chunk)
                after = candidate.lstat()
            except OSError:
                raise DiffPolicyError("containment_uncertain") from None
            if (
                after.st_size != metadata.st_size
                or after.st_mtime_ns != metadata.st_mtime_ns
                or getattr(after, "st_ino", None) != getattr(metadata, "st_ino", None)
            ):
                raise DiffPolicyError("concurrent_mutation")
            encoded_path = relative.encode("utf-8")
            manifest.update(len(encoded_path).to_bytes(4, "big"))
            manifest.update(encoded_path)
            manifest.update(metadata.st_size.to_bytes(8, "big"))
            manifest.update(content_digest.digest())
            untracked_bytes += metadata.st_size
    return untracked_bytes, manifest.digest()


def inspect_diff_evidence(
    *,
    worktree_root: Path,
    baseline_commit: str,
    status_output: bytes,
    name_status_output: bytes,
    numstat_output: bytes,
    raw_output: bytes,
    patch_output: bytes,
    max_files: int,
    max_bytes: int,
) -> DiffEvidence:
    """Validate bounded machine-readable Git evidence and return only a digest."""
    if not isinstance(baseline_commit, str) or _COMMIT.fullmatch(baseline_commit) is None:
        raise DiffPolicyError("baseline_invalid")
    if (
        isinstance(max_files, bool)
        or not isinstance(max_files, int)
        or not 1 <= max_files <= 10_000
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= 100 * 1024 * 1024
        or not isinstance(patch_output, bytes)
    ):
        raise DiffPolicyError("policy_invalid")
    try:
        root_metadata = worktree_root.lstat()
        root = worktree_root.resolve(strict=True)
    except OSError:
        raise DiffPolicyError("worktree_invalid") from None
    if stat.S_ISLNK(root_metadata.st_mode) or _is_reparse(root_metadata) or not stat.S_ISDIR(root_metadata.st_mode):
        raise DiffPolicyError("worktree_invalid")
    status_paths = _status_paths(status_output)
    tracked_paths = _name_status_paths(name_status_output)
    numstat_paths, churn = _numstat(numstat_output)
    raw_paths = _raw_paths_and_modes(raw_output)
    if tracked_paths != numstat_paths or tracked_paths != raw_paths:
        if tracked_paths or numstat_paths or raw_paths:
            raise DiffPolicyError("git_evidence_mismatch")
    if not tracked_paths <= status_paths:
        raise DiffPolicyError("git_evidence_mismatch")
    folded: dict[str, str] = {}
    for path in status_paths:
        key = path.casefold()
        if key in folded and folded[key] != path:
            raise DiffPolicyError("path_collision")
        folded[key] = path
    if len(status_paths) > max_files:
        raise DiffPolicyError("file_limit")
    if b"GIT binary patch" in patch_output or b"Binary files " in patch_output:
        raise DiffPolicyError("binary_change")
    untracked_paths = status_paths - tracked_paths
    filesystem_bytes, untracked_digest = _validate_filesystem(
        root, status_paths, untracked_paths, max_bytes=max_bytes
    )
    changed_bytes = len(patch_output) + filesystem_bytes
    if changed_bytes > max_bytes or churn > max_bytes:
        raise DiffPolicyError("byte_limit")
    digest = hashlib.sha256()
    digest.update(b"graphite-diff-v1\0")
    digest.update(baseline_commit.encode("ascii"))
    for output in (status_output, name_status_output, numstat_output, raw_output, patch_output):
        digest.update(len(output).to_bytes(8, "big"))
        digest.update(output)
    digest.update(untracked_digest)
    return DiffEvidence(
        baseline_commit,
        len(status_paths),
        changed_bytes,
        digest.hexdigest(),
        tuple(sorted(status_paths)),
    )


def _run_git(
    runner: GitRunner, arguments: list[str], *, maximum: int
) -> bytes:
    try:
        result = runner.run(
            arguments,
            timeout_seconds=GIT_TIMEOUT_SECONDS,
            max_stdout_bytes=maximum,
        )
    except GitTimeoutError:
        raise DiffPolicyError("git_timeout") from None
    except GitOutputLimitError:
        raise DiffPolicyError("byte_limit") from None
    except GitError:
        raise DiffPolicyError("git_unavailable") from None
    if result.returncode != 0:
        raise DiffPolicyError("git_failed")
    return result.stdout


def _decode_git_path(root: Path, output: bytes) -> Path:
    try:
        raw = output.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        raise DiffPolicyError("git_protocol") from None
    if not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise DiffPolicyError("git_protocol")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return candidate.resolve(strict=True)
    except OSError:
        raise DiffPolicyError("containment_uncertain") from None


def collect_diff_evidence(
    worktree: TaskWorktree,
    *,
    max_files: int,
    max_bytes: int,
) -> DiffEvidence:
    """Collect bounded Git evidence twice and reject concurrent or identity drift."""
    if not isinstance(worktree, TaskWorktree) or worktree.status != "prepared":
        raise DiffPolicyError("worktree_invalid")
    try:
        root = worktree.root.resolve(strict=True)
    except OSError:
        raise DiffPolicyError("worktree_invalid") from None
    if root != worktree.root:
        raise DiffPolicyError("worktree_invalid")
    runner = GitRunner(root)
    head = _run_git(runner, ["rev-parse", "HEAD"], maximum=256)
    try:
        current_head = head.decode("ascii").strip()
    except UnicodeDecodeError:
        raise DiffPolicyError("git_protocol") from None
    if current_head != worktree.baseline_commit:
        raise DiffPolicyError("commit_drift")
    common = _run_git(runner, ["rev-parse", "--git-common-dir"], maximum=4_096)
    if _decode_git_path(root, common) != worktree.git_common_dir:
        raise DiffPolicyError("worktree_identity_drift")
    status_arguments = [
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
    ]
    first_status = _run_git(runner, status_arguments, maximum=MAX_METADATA_BYTES)
    common_diff = ["diff", "--no-ext-diff", "--no-renames", worktree.baseline_commit, "--"]
    names = _run_git(
        runner,
        ["diff", "--name-status", "-z", "--no-ext-diff", "--no-renames", worktree.baseline_commit, "--"],
        maximum=MAX_METADATA_BYTES,
    )
    numstat = _run_git(
        runner,
        ["diff", "--numstat", "-z", "--no-ext-diff", "--no-renames", worktree.baseline_commit, "--"],
        maximum=MAX_METADATA_BYTES,
    )
    raw = _run_git(
        runner,
        [
            "diff",
            "--raw",
            "-z",
            "--abbrev=64",
            "--no-ext-diff",
            "--no-renames",
            worktree.baseline_commit,
            "--",
        ],
        maximum=MAX_METADATA_BYTES,
    )
    patch = _run_git(
        runner,
        [*common_diff[:2], "--no-color", "--binary", "--full-index", *common_diff[2:]],
        maximum=max_bytes + 1,
    )
    evidence = inspect_diff_evidence(
        worktree_root=root,
        baseline_commit=worktree.baseline_commit,
        status_output=first_status,
        name_status_output=names,
        numstat_output=numstat,
        raw_output=raw,
        patch_output=patch,
        max_files=max_files,
        max_bytes=max_bytes,
    )
    second_status = _run_git(runner, status_arguments, maximum=MAX_METADATA_BYTES)
    if second_status != first_status:
        raise DiffPolicyError("concurrent_mutation")
    return evidence
