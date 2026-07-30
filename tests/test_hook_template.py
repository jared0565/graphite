"""Machine-wide git template for fresh clones (`init.templateDir`).

Git copies `<templateDir>/hooks/<name>` into `.git/hooks/<name>` on every
future `git init`/`git clone` **on this machine**, once a human points
`init.templateDir` there -- so a hook placed here runs for every repo on the
machine, onboarded or not. `install_template` therefore reuses
`render_trigger_shim` unchanged: `hook_entry.main()` already fails open
(returns 0, silently) when no `GRAPHITE.md` is found walking up from cwd,
which is exactly the guard a machine-wide template hook needs. A different or
"simpler" shim was deliberately not written for this case.

**Isolation:** every test here confines itself to `tmp_path`. None reads or
writes this machine's real global git config, and none calls
`install_template` against a real, non-`tmp_path` template directory.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from graphite.hookinstall import install_template
from graphite.hookshim import MARKER_START, TRIGGERS, render_trigger_shim

INTERP = Path("C:/Python314/python.exe")

# Resolved once at import time so the skip reason is stable across the file.
# The hook shim is `#!/bin/sh` by construction (hookshim.render_trigger_shim)
# -- if `sh` is not on PATH, graphite's whole hook mechanism cannot fire on
# this machine regardless of what this test does, so skipping (not failing)
# is the honest outcome.
SH = shutil.which("sh")


def test_template_writes_into_hooks_subdirectory(tmp_path: Path) -> None:
    """Git only ever copies `<templateDir>/hooks/<name>` -- writing straight
    into `template_root` would be invisible to `init.templateDir`."""
    template_root = tmp_path / "template"

    written = install_template(template_root, INTERP)

    hooks_subdir = template_root / "hooks"
    assert hooks_subdir.is_dir()
    for hook in TRIGGERS:
        path = hooks_subdir / hook
        assert path.exists()
        assert path in written
        assert MARKER_START.encode() in path.read_bytes()
    # Nothing lives directly under template_root besides the hooks/ dir git
    # actually copies from.
    assert [p.name for p in template_root.iterdir()] == ["hooks"]


@pytest.mark.skipif(SH is None, reason="sh not found on PATH -- needed to execute the templated shim for real")
def test_templated_shim_noops_without_graphite_md(tmp_path: Path) -> None:
    """The whole point of this task: this hook runs in EVERY new clone on the
    machine, onboarded or not. A repo with no GRAPHITE.md must see a silent
    no-op, never an error -- verified by actually running the shim in a
    fresh, non-onboarded repo, not by reading its source.

    exit-0-and-silent alone would also be produced by a *crashing*
    hook_entry, since the shim's own `>/dev/null 2>&1 || true` plus trailing
    `exit 0` swallow everything. The discriminating check is the absence of
    `.graphite-cache`: that directory is only ever created by hook_entry's
    `build_lock`, which only runs once `_repo_root` has found a GRAPHITE.md.
    Its absence proves the guard fired before build_lock was ever reached,
    not merely that failures were silenced."""
    template_root = tmp_path / "template"
    install_template(template_root, Path(sys.executable))

    repo = tmp_path / "fresh-clone"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    # Deliberately no GRAPHITE.md: this repo was never onboarded.
    assert not (repo / "GRAPHITE.md").exists()

    hook = template_root / "hooks" / "post-commit"
    result = subprocess.run(
        [SH, str(hook)], cwd=str(repo), capture_output=True, text=True
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout == ""
    assert result.stderr == ""
    assert not (repo / ".graphite-cache").exists()


def test_templated_shim_is_lf_only(tmp_path: Path) -> None:
    template_root = tmp_path / "template"

    install_template(template_root, INTERP)

    for hook in TRIGGERS:
        assert b"\r" not in (template_root / "hooks" / hook).read_bytes()


def test_install_template_is_idempotent(tmp_path: Path) -> None:
    template_root = tmp_path / "template"
    install_template(template_root, INTERP)
    first = {
        hook: (template_root / "hooks" / hook).read_bytes() for hook in TRIGGERS
    }

    install_template(template_root, INTERP)

    for hook in TRIGGERS:
        assert (template_root / "hooks" / hook).read_bytes() == first[hook]


def test_template_shim_bytes_reuse_render_trigger_shim(tmp_path: Path) -> None:
    """No separate rendering path for the template case -- the brief is
    explicit that reusing `render_trigger_shim` directly is what makes the
    fail-open guarantee hold here."""
    template_root = tmp_path / "template"

    install_template(template_root, INTERP)

    for hook in TRIGGERS:
        expected = render_trigger_shim(hook, INTERP)
        assert (template_root / "hooks" / hook).read_bytes() == expected


def test_hooks_cli_requires_install_template_flag(tmp_path: Path, monkeypatch, capsys) -> None:
    """No flag, no side effect -- this must not touch the real default
    template root, so a bare `graphite hooks` has to bail out before ever
    resolving it.

    `main()`'s catch-all (cli.py `except Exception`) also returns 1, so
    `rc == 1` alone cannot tell a legitimate early bail apart from "resolved
    the default root, then blew up" -- both would satisfy that assertion.
    The stderr text is what discriminates: only the early-bail path prints
    `cmd_hooks`'s own message; the tripwire's `AssertionError`, if it fired,
    would surface instead as `[graphite] error: ...`."""
    import graphite.cli as cli

    monkeypatch.setattr(
        cli.hookinstall,
        "default_template_root",
        lambda: (_ for _ in ()).throw(AssertionError("should not resolve a default root")),
    )

    rc = cli.main(["hooks"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "pass --install-template" in err
    assert "should not resolve a default root" not in err


def test_hooks_cli_prints_activation_command_and_never_runs_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`graphite hooks --install-template` must resolve its default root the
    same way daemon state does (see `hookinstall.default_template_root`,
    mocked here so the test never touches this machine's real default), write
    the shims, and print the `git config --global init.templateDir` command
    for a human to run -- never execute it. Guarded with a `subprocess.run`
    tripwire: if graphite's code path ever shells out here, this test fails
    loudly instead of passing by accident."""
    import graphite.cli as cli

    fake_root = tmp_path / "machine-template"
    monkeypatch.setattr(cli.hookinstall, "default_template_root", lambda: fake_root)

    def _guard(*args, **kwargs):
        raise AssertionError(f"unexpected subprocess.run call: {args!r} {kwargs!r}")

    monkeypatch.setattr(subprocess, "run", _guard)

    rc = cli.main(["hooks", "--install-template"])

    assert rc == 0
    for hook in TRIGGERS:
        assert (fake_root / "hooks" / hook).exists()
    out = capsys.readouterr().out
    assert "git config --global init.templateDir" in out
    assert str(fake_root) in out
