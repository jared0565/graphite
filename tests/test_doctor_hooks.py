"""Doctor probe: hooks configured but not enforced.

Severity rule (from aramid's `probe_enforcement`): a repo with no
`GRAPHITE.md` is deliberately not onboarded and is NOT a finding -- nagging
every un-onboarded repo is how a real warning gets ignored.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from graphite.doctor import check_hooks
from graphite.hookinstall import hooks_dir, install_hooks

INTERP = Path("C:/Python314/python.exe")


def _repo(tmp_path: Path, *, onboarded: bool = True) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    if onboarded:
        (tmp_path / "GRAPHITE.md").write_text("x", encoding="utf-8")
    return tmp_path


def test_reports_ok_when_hooks_installed_and_hookspath_set(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    install_hooks(root, INTERP)

    check = check_hooks(root)

    assert check.status == "ready"


def test_warns_when_hookspath_set_but_trampoline_missing(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    install_hooks(root, INTERP)
    (hooks_dir(root) / "post-commit").unlink()

    check = check_hooks(root)

    assert check.status == "degraded"


def test_warns_when_trampolines_exist_but_hookspath_unset(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    install_hooks(root, INTERP)
    subprocess.run(
        ["git", "-C", str(root), "config", "--unset", "core.hooksPath"],
        check=True,
    )

    check = check_hooks(root)

    assert check.status == "degraded"


def test_un_onboarded_repo_is_not_a_finding(tmp_path: Path) -> None:
    root = _repo(tmp_path, onboarded=False)

    check = check_hooks(root)

    assert check.status == "optional"
