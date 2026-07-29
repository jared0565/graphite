"""Wiring `hookinstall.install_hooks` into `graphite init`.

Mirrors the fixture style of tests/test_hookinstall.py (Task 3), but goes
through `init_project` / `cmd_init` rather than calling `install_hooks`
directly -- this is the layer that decides the default and the CLI opt-out,
not hook rendering or relocation itself (already covered by
tests/test_hookinstall.py and tests/test_hookshim.py).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from graphite.cli import main
from graphite.hookshim import MARKER_START, TRIGGERS
from graphite.init import init_project


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
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


def test_init_installs_hooks_by_default(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    result = init_project(root, platforms=["claude"]).to_dict()

    for hook in TRIGGERS:
        shim = root / ".githooks" / hook
        assert shim.exists()
        assert MARKER_START.encode() in shim.read_bytes()
    assert _config(root, "core.hooksPath") == ".githooks"
    assert result["hooks"]["action"] == "installed"
    # NOT asserted: relocated == []. This machine's `init.templateDir`
    # (aramid) seeds every fresh `git init` with real pre-commit/pre-push
    # hooks, so relocation is expected here too -- byte-identical relocation
    # itself is test_hookinstall.py's contract, not this file's.
    assert isinstance(result["hooks"]["relocated"], list)


def test_init_no_hooks_flag_skips_installation(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    result = main([
        "init", str(root),
        "--platform", "claude",
        "--no-hooks", "--no-build", "--no-validate", "--json",
    ])

    assert result == 0
    assert not (root / ".githooks").exists()
    assert _config(root, "core.hooksPath") == ""


def test_init_is_idempotent_over_hooks(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    init_project(root, platforms=["claude"])
    first = (root / ".githooks" / "post-commit").read_bytes()

    second = init_project(root, platforms=["claude"]).to_dict()

    assert (root / ".githooks" / "post-commit").read_bytes() == first
    assert not (root / ".githooks" / "post-commit.local").exists()
    assert second["hooks"]["action"] == "installed"


def test_init_reports_what_it_relocated(tmp_path: Path, capsys) -> None:
    root = _repo(tmp_path)
    body = "#!/bin/sh\n# >>> aramid managed >>>\nexit 0\n"
    _write_hook(root, "pre-push", body)

    result = main(["init", str(root), "--platform", "claude", "--no-build", "--no-validate"])
    out = capsys.readouterr().out

    assert result == 0
    assert "relocated" in out
    assert "pre-push" in out
    # Relocation itself stays byte-identical -- that contract is
    # test_hookinstall.py's job; here we only need proof it was reported.
    assert (root / ".githooks" / "pre-push").read_bytes() == body.encode()


def test_init_on_a_non_git_directory_does_not_claim_hooks_are_installed(tmp_path: Path) -> None:
    """`hookinstall.install_hooks` would happily write trampolines into
    `.githooks/` even with no `.git` for `core.hooksPath` to live in -- git
    would then never dispatch to them. Reporting `action: "installed"` in
    that case is exactly the confident-silent-wrong shape to avoid: no `git
    init` has run here, so init must report a skip, not a claimed install,
    and must not leave inert files behind that look like they did something.
    """
    result = init_project(tmp_path, platforms=["claude"]).to_dict()

    assert result["hooks"]["action"] == "skipped"
    assert result["hooks"]["reason"] == "not a git repository"
    assert not (tmp_path / ".githooks").exists()
