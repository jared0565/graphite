"""Security invariants for the deep-probe workspace lease."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest


class _CountingLease:
    def __init__(self, lease: object) -> None:
        self._lease = lease
        self.path = lease.path
        self.temp_root = lease.temp_root
        self.validations = 0
        self.cleaned = False

    def validate(self) -> None:
        self.validations += 1
        self._lease.validate()

    def cleanup(self) -> None:
        self.cleaned = True
        self._lease.cleanup()


def test_workspace_lease_rejects_initial_symlink_or_windows_reparse_without_writing(tmp_path: Path) -> None:
    from graphite.probe_workspace import ProbeWorkspaceLease, WorkspaceLeaseError

    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    sentinel = tmp_path / "outside"
    sentinel.mkdir()
    marker = sentinel / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    def malicious_parent_factory(**kwargs: object) -> str:
        parent = Path(os.path.join(str(kwargs["dir"]), "attacker-parent"))
        parent.mkdir(mode=0o700)
        try:
            (parent / "workspace").symlink_to(sentinel, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")
        return str(parent)

    with pytest.raises(WorkspaceLeaseError):
        ProbeWorkspaceLease.acquire(temp_root=temp_root, _parent_factory=malicious_parent_factory)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert list(sentinel.iterdir()) == [marker]


def test_workspace_lease_never_mutates_or_removes_factory_path_outside_temp_root(tmp_path: Path) -> None:
    from graphite.probe_workspace import ProbeWorkspaceLease, WorkspaceLeaseError

    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    outside = tmp_path / "existing-empty-sibling"
    outside.mkdir(mode=0o755)
    before = outside.lstat()

    with pytest.raises(WorkspaceLeaseError):
        ProbeWorkspaceLease.acquire(
            temp_root=temp_root,
            _parent_factory=lambda **kwargs: str(outside),
        )

    after = outside.lstat()
    assert list(outside.iterdir()) == []
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    )


def test_workspace_lease_rejects_preexisting_in_root_factory_path_unchanged(tmp_path: Path) -> None:
    from graphite.probe_workspace import ProbeWorkspaceLease, WorkspaceLeaseError

    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    preexisting = temp_root / "preexisting"
    preexisting.mkdir(mode=0o755)
    before = preexisting.lstat()

    try:
        lease = ProbeWorkspaceLease.acquire(
            temp_root=temp_root,
            _parent_factory=lambda **kwargs: str(preexisting),
        )
    except WorkspaceLeaseError:
        pass
    else:
        lease.close()
        pytest.fail("a factory-returned pre-existing directory must not be acquired")

    after = preexisting.lstat()
    assert list(preexisting.iterdir()) == []
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    )


def test_workspace_lease_does_not_remove_parent_swapped_after_owned_creation(tmp_path: Path) -> None:
    from graphite.probe_workspace import ProbeWorkspaceLease, WorkspaceLeaseError

    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    replacement: Path | None = None
    original: Path | None = None

    def swapped_creator(root: Path) -> tuple[Path, tuple[int, int]]:
        nonlocal replacement, original
        candidate = root / "owned-candidate"
        candidate.mkdir(mode=0o700)
        info = candidate.lstat()
        original = root / "moved-original"
        candidate.rename(original)
        candidate.mkdir(mode=0o700)
        replacement = candidate
        (candidate / "sentinel.txt").write_text("keep", encoding="utf-8")
        return candidate, (info.st_dev, info.st_ino)

    with pytest.raises(WorkspaceLeaseError):
        ProbeWorkspaceLease.acquire(
            temp_root=temp_root,
            _owned_parent_factory=swapped_creator,
        )

    assert original is not None and original.exists()
    assert replacement is not None
    assert (replacement / "sentinel.txt").read_text(encoding="utf-8") == "keep"


def test_workspace_lease_cleans_still_owned_parent_after_partial_creation_failure(tmp_path: Path) -> None:
    from graphite.probe_workspace import ProbeWorkspaceLease, WorkspaceLeaseError

    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    created: list[Path] = []

    def parent_creator(root: Path) -> tuple[Path, tuple[int, int]]:
        candidate = root / "partial-parent"
        candidate.mkdir(mode=0o700)
        created.append(candidate)
        info = candidate.lstat()
        return candidate, (info.st_dev, info.st_ino)

    def workspace_failure(parent: Path) -> tuple[Path, tuple[int, int]]:
        raise OSError("injected workspace creation failure")

    with pytest.raises(WorkspaceLeaseError):
        ProbeWorkspaceLease.acquire(
            temp_root=temp_root,
            _owned_parent_factory=parent_creator,
            _workspace_factory=workspace_failure,
        )

    assert created and not created[0].exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission invariant")
def test_workspace_lease_uses_private_posix_modes(tmp_path: Path) -> None:
    from graphite.probe_workspace import ProbeWorkspaceLease

    lease = ProbeWorkspaceLease.acquire(temp_root=tmp_path)
    try:
        assert stat.S_IMODE(lease.parent_path.lstat().st_mode) == 0o700
        assert stat.S_IMODE(lease.path.lstat().st_mode) == 0o700
    finally:
        lease.cleanup()


@pytest.mark.skipif(os.name == "nt", reason="Windows handles prevent the substitution")
def test_workspace_lease_identity_change_blocks_cleanup_of_replacement(tmp_path: Path) -> None:
    from graphite.probe_workspace import ProbeWorkspaceLease, WorkspaceLeaseError

    lease = ProbeWorkspaceLease.acquire(temp_root=tmp_path)
    original = lease.parent_path / "original-workspace"
    lease.path.rename(original)
    lease.path.mkdir()
    sentinel = lease.path / "outside-sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(WorkspaceLeaseError, match="workspace_isolation_changed"):
        lease.validate()
    with pytest.raises(WorkspaceLeaseError, match="workspace_cleanup_blocked"):
        lease.cleanup()

    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "nt", reason="Windows handle pinning invariant")
def test_windows_workspace_handle_blocks_top_level_rename_and_close_is_idempotent(tmp_path: Path) -> None:
    from graphite.probe_workspace import ProbeWorkspaceLease

    lease = ProbeWorkspaceLease.acquire(temp_root=tmp_path)
    replacement = lease.parent_path / "renamed-workspace"
    with pytest.raises(OSError):
        lease.path.rename(replacement)
    lease.cleanup()
    lease.close()
    lease.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows atomic ACL creation invariant")
def test_windows_owned_creators_use_private_directory_creation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import graphite.probe_workspace as workspace

    calls: list[str | None] = []

    def private_create(parent: Path, name: str | None = None) -> Path:
        calls.append(name)
        path = parent / (name or "random-private-parent")
        path.mkdir(mode=0o700)
        return path

    monkeypatch.setattr(
        workspace,
        "_create_private_windows_directory",
        private_create,
        raising=False,
    )

    parent, _ = workspace._create_owned_parent(tmp_path)
    child, _ = workspace._create_owned_workspace(parent)

    assert calls == [None, "workspace"]
    assert child == parent / "workspace"


def test_core_probe_blocks_identity_change_before_next_phase_and_preserves_replacement(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows lease handles prevent this substitution; covered separately")

    import graphite.doctor_probes as probes
    from graphite.probe_workspace import ProbeWorkspaceLease

    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "keep.txt").write_text("selected", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in selected.iterdir()}
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    lease = ProbeWorkspaceLease.acquire(temp_root=temp_root)
    calls = 0
    sentinel: Path | None = None

    def run(*args: object, **kwargs: object) -> probes.ProbeProcessResult:
        nonlocal calls, sentinel
        calls += 1
        original = lease.parent_path / "original-workspace"
        lease.path.rename(original)
        lease.path.mkdir()
        sentinel = lease.path / "outside-sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        return probes.ProbeProcessResult(0, b"", b"", 0.01)

    check = probes.probe_core_pipeline(
        selected,
        _runner=run,
        _workspace_factory=lambda: lease,
    )

    assert check.status == "blocked"
    assert check.details == {"error_type": "cleanup", "code": "cleanup_blocked"}
    assert calls == 1
    assert sentinel is not None and sentinel.read_text(encoding="utf-8") == "keep"
    assert {path.name: path.read_bytes() for path in selected.iterdir()} == before
    assert str(sentinel) not in json.dumps(check.to_dict())


def test_core_probe_uses_injected_lease_and_revalidates_all_boundaries(tmp_path: Path) -> None:
    import graphite.doctor_probes as probes
    from graphite.probe_workspace import ProbeWorkspaceLease

    selected = tmp_path / "selected"
    selected.mkdir()
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    counting = _CountingLease(ProbeWorkspaceLease.acquire(temp_root=temp_root))
    outputs = iter(
        [
            b"",
            b'{"ok":true,"error_count":0,"errors":[],"node_count":2,"edge_count":1}',
            b'{"node_count":2,"edge_count":1}',
        ]
    )

    check = probes.probe_core_pipeline(
        selected,
        _runner=lambda *args, **kwargs: probes.ProbeProcessResult(0, next(outputs), b"", 0.01),
        _workspace_factory=lambda: counting,
    )

    assert check.status == "ready", check.to_dict()
    assert counting.validations >= 12
    assert counting.cleaned is True
