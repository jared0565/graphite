from __future__ import annotations

from pathlib import Path

import pytest

from graphite.routing.diff_policy import DiffPolicyError, inspect_diff_evidence
from graphite.routing.worktree import WorktreeError, _validate_index


BASELINE = "a" * 40


def _inspect(
    tmp_path: Path,
    *,
    status: bytes,
    names: bytes,
    numstat: bytes,
    raw: bytes,
    patch: bytes = b"patch",
    max_files: int = 64,
    max_bytes: int = 1_048_576,
):
    root = tmp_path / "worktree"
    root.mkdir(parents=True)
    return inspect_diff_evidence(
        worktree_root=root,
        baseline_commit=BASELINE,
        status_output=status,
        name_status_output=names,
        numstat_output=numstat,
        raw_output=raw,
        patch_output=patch,
        max_files=max_files,
        max_bytes=max_bytes,
    )


def test_inspect_diff_accepts_bounded_text_change_and_hashes_without_content(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('safe')\n", encoding="utf-8")
    evidence = inspect_diff_evidence(
        worktree_root=root,
        baseline_commit=BASELINE,
        status_output=b" M src/app.py\0",
        name_status_output=b"M\0src/app.py\0",
        numstat_output=b"1\t1\tsrc/app.py\0",
        raw_output=b":100644 100644 " + b"1" * 40 + b" " + b"2" * 40 + b" M\0src/app.py\0",
        patch_output=b"diff --git a/src/app.py b/src/app.py\n-safe\n+safe2\n",
        max_files=64,
        max_bytes=1024,
    )
    assert evidence.changed_files == 1
    assert 0 < evidence.changed_bytes <= 1024
    assert len(evidence.diff_sha256) == 64
    assert "safe2" not in repr(evidence)


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("../escape.py", "path_invalid"),
        (".env", "sensitive_path"),
        ("config/secrets.json", "sensitive_path"),
        (".git/config", "git_control_path"),
        ("A\\B.py", "path_invalid"),
    ],
)
def test_inspect_diff_rejects_unsafe_paths(tmp_path: Path, path: str, code: str) -> None:
    with pytest.raises(DiffPolicyError, match=f"^{code}$"):
        _inspect(
            tmp_path,
            status=f"?? {path}\0".encode(),
            names=b"",
            numstat=b"",
            raw=b"",
        )


def test_inspect_diff_rejects_binary_submodule_symlink_and_executable_modes(tmp_path: Path) -> None:
    cases = [
        (b"-\t-\tasset.bin\0", b"", "binary_change"),
        (
            b"1\t0\tmodule\0",
            b":160000 160000 " + b"1" * 40 + b" " + b"2" * 40 + b" M\0module\0",
            "submodule_change",
        ),
        (
            b"1\t0\tlink\0",
            b":100644 120000 " + b"1" * 40 + b" " + b"2" * 40 + b" M\0link\0",
            "special_file_change",
        ),
        (
            b"1\t0\tscript\0",
            b":100644 100755 " + b"1" * 40 + b" " + b"2" * 40 + b" M\0script\0",
            "executable_change",
        ),
    ]
    for numstat, raw, code in cases:
        with pytest.raises(DiffPolicyError, match=f"^{code}$"):
            _inspect(
                tmp_path / code,
                status=b" M module\0",
                names=b"M\0module\0",
                numstat=numstat,
                raw=raw,
            )


def test_inspect_diff_rejects_case_collisions_and_limits(tmp_path: Path) -> None:
    with pytest.raises(DiffPolicyError, match="^path_collision$"):
        _inspect(
            tmp_path / "collision",
            status=b"?? A.py\0?? a.py\0",
            names=b"",
            numstat=b"",
            raw=b"",
        )
    with pytest.raises(DiffPolicyError, match="^file_limit$"):
        _inspect(
            tmp_path / "files",
            status=b"?? a.py\0?? b.py\0",
            names=b"",
            numstat=b"",
            raw=b"",
            max_files=1,
        )
    with pytest.raises(DiffPolicyError, match="^byte_limit$"):
        _inspect(
            tmp_path / "bytes",
            status=b" M a.py\0",
            names=b"M\0a.py\0",
            numstat=b"100\t100\ta.py\0",
            raw=b":100644 100644 "
            + b"1" * 40
            + b" "
            + b"2" * 40
            + b" M\0a.py\0",
            max_bytes=10,
        )


def test_index_gitlink_is_rejected_as_a_submodule() -> None:
    with pytest.raises(WorktreeError, match="^source_special_file$"):
        _validate_index(b"160000 " + b"1" * 40 + b" 0\tmodule\0")
