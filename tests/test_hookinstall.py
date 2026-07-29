"""Hook installation, migration and relocation.

This module carries the aramid interop contract. The load-bearing rule is
**relocate, never trampoline, a hook graphite does not trigger on**: stamping
`# >>> graphite managed >>>` onto `pre-commit`/`pre-push` makes aramid's
`install()` refuse them (aramid 0f24609), which would leave its gates
un-refreshable across every repo graphite migrated.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from graphite.hookinstall import hooks_dir, install_hooks, uninstall_hooks
from graphite.hookshim import MARKER_START, TRIGGERS

INTERP = Path("C:/Python314/python.exe")


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "GRAPHITE.md").write_text("x", encoding="utf-8")
    return tmp_path


def _write_hook(root: Path, name: str, body: str) -> Path:
    p = root / ".git" / "hooks" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body.encode())
    return p


def _config(root: Path, key: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "config", "--get", key],
        capture_output=True, text=True,
    ).stdout.strip()


def test_non_trigger_hook_is_relocated_byte_identically(tmp_path: Path) -> None:
    """The aramid contract. An aramid shim must arrive in .githooks/ unchanged
    and still carrying ITS OWN marker, so `_is_aramid_shim` is true and
    aramid's refusal branch never runs."""
    root = _repo(tmp_path)
    body = "#!/bin/sh\n# >>> aramid managed >>>\nexit 0\n"
    original = _write_hook(root, "pre-push", body).read_bytes()

    install_hooks(root, INTERP)

    moved = root / ".githooks" / "pre-push"
    assert moved.read_bytes() == original
    assert MARKER_START.encode() not in moved.read_bytes()
    assert not (root / ".githooks" / "pre-push.local").exists()


def test_trigger_hook_is_chained_not_relocated(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    body = "#!/bin/sh\necho original\n"
    _write_hook(root, "post-commit", body)

    install_hooks(root, INTERP)

    assert (root / ".githooks" / "post-commit.local").read_bytes() == body.encode()
    assert MARKER_START.encode() in (root / ".githooks" / "post-commit").read_bytes()


def test_trigger_shims_are_written_even_with_nothing_to_chain(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    install_hooks(root, INTERP)
    for hook in TRIGGERS:
        assert (root / ".githooks" / hook).exists()
        assert not (root / ".githooks" / f"{hook}.local").exists()


def test_hooks_path_is_configured(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    install_hooks(root, INTERP)
    assert _config(root, "core.hooksPath") == ".githooks"


def test_hooks_path_is_set_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No window may exist where hooksPath redirects to a directory that has
    no hooks in it yet."""
    import graphite.hookinstall as hi

    root = _repo(tmp_path)
    monkeypatch.setattr(
        hi, "render_trigger_shim",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        install_hooks(root, INTERP)
    assert _config(root, "core.hooksPath") == ""


def test_existing_hookspath_is_not_hijacked(tmp_path: Path) -> None:
    """Someone else owns hook policy here (husky et al.) -- install into their
    directory rather than taking the setting over."""
    root = _repo(tmp_path)
    subprocess.run(["git", "-C", str(root), "config", "core.hooksPath", ".husky"], check=True)

    install_hooks(root, INTERP)

    assert _config(root, "core.hooksPath") == ".husky"
    assert (root / ".husky" / "post-commit").exists()
    assert hooks_dir(root) == root / ".husky"


def test_install_is_idempotent_and_never_self_chains(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    install_hooks(root, INTERP)
    first = (root / ".githooks" / "post-commit").read_bytes()

    install_hooks(root, INTERP)

    assert (root / ".githooks" / "post-commit").read_bytes() == first
    assert not (root / ".githooks" / "post-commit.local").exists()


def test_reinstall_preserves_an_existing_chained_original(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    body = "#!/bin/sh\necho original\n"
    _write_hook(root, "post-commit", body)
    install_hooks(root, INTERP)

    install_hooks(root, INTERP)

    assert (root / ".githooks" / "post-commit.local").read_bytes() == body.encode()


def test_relocation_does_not_reprocess_on_reinstall(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    body = "#!/bin/sh\n# >>> aramid managed >>>\nexit 0\n"
    _write_hook(root, "pre-push", body)
    install_hooks(root, INTERP)
    install_hooks(root, INTERP)
    assert (root / ".githooks" / "pre-push").read_bytes() == body.encode()
    assert not (root / ".githooks" / "pre-push.local").exists()


def test_uninstall_restores_the_chained_original(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    body = "#!/bin/sh\necho original\n"
    _write_hook(root, "post-commit", body)
    install_hooks(root, INTERP)

    uninstall_hooks(root)

    assert (root / ".githooks" / "post-commit").read_bytes() == body.encode()
    assert not (root / ".githooks" / "post-commit.local").exists()


def test_uninstall_removes_a_shim_that_chained_nothing(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    install_hooks(root, INTERP)
    uninstall_hooks(root)
    for hook in TRIGGERS:
        assert not (root / ".githooks" / hook).exists()


def test_uninstall_leaves_relocated_foreign_hooks_alone(tmp_path: Path) -> None:
    """Graphite moved it but never owned it. Removing it on uninstall would
    delete aramid's live gate."""
    root = _repo(tmp_path)
    body = "#!/bin/sh\n# >>> aramid managed >>>\nexit 0\n"
    _write_hook(root, "pre-push", body)
    install_hooks(root, INTERP)

    uninstall_hooks(root)

    assert (root / ".githooks" / "pre-push").read_bytes() == body.encode()
