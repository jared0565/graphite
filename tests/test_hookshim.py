"""Rendering of graphite's git-hook trampolines.

Pure byte rendering, so every Windows correctness rule that bit aramid is
cheaply assertable here rather than only observable in a live repo.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from graphite.hookshim import (
    MARKER_END,
    MARKER_START,
    TRIGGERS,
    render_trigger_shim,
    sh_interpreter_path,
)

INTERP = Path("C:/Python314/python.exe")

SHADOW_MARKER = "SHADOW-EXECUTED"


def _hook_entry_arms(hook: str = "post-commit") -> list[list[str]]:
    """Every rendered line that runs `graphite.hook_entry`, as argv tokens.

    Parsed out of the rendered bytes rather than hard-coded, so a test built on
    this fails when the template changes -- which is the entire point.
    """
    out = render_trigger_shim(hook, INTERP).decode()
    arms = []
    for line in out.splitlines():
        if "graphite.hook_entry" not in line:
            continue
        arms.append(shlex.split(line.split(">")[0]))
    return arms


def _interpreter_flags(arm: list[str]) -> list[str]:
    """Tokens the arm passes to the interpreter before `-m`."""
    return arm[1 : arm.index("-m")]


def _plant_shadow(root: Path, *, package: bool) -> None:
    """A hostile `graphite` importable at the repo root, as the threat model has it."""
    body = f"import sys\nprint({SHADOW_MARKER!r}, file=sys.stderr)\n"
    if package:
        (root / "graphite").mkdir()
        (root / "graphite" / "__init__.py").write_text("", encoding="utf-8")
        (root / "graphite" / "hook_entry.py").write_text(body + "sys.exit(0)\n", encoding="utf-8")
    else:
        (root / "graphite.py").write_text(body, encoding="utf-8")


def _run_hook_entry(cwd: Path, flags: list[str]) -> str:
    result = subprocess.run(  # noqa: S603
        [sys.executable, *flags, "-m", "graphite.hook_entry"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout + result.stderr


def test_triggers_are_the_three_agreed_hooks() -> None:
    # post-checkout is deliberately absent: branch switching is frequent and
    # transient, and hooking it makes `git bisect` expensive.
    assert TRIGGERS == ("post-commit", "post-merge", "post-rewrite")


def test_rendered_bytes_are_lf_only() -> None:
    # Git-for-Windows' `sh` chokes on a bare CR in the exec line, and a
    # text-mode write on Windows silently reintroduces CRLF.
    for hook in TRIGGERS:
        assert b"\r" not in render_trigger_shim(hook, INTERP)


def test_chain_check_uses_if_fi_not_and_shortcircuit() -> None:
    """`[ -f x ] && { ...; }` as a script's final command exits 1 when x is
    absent -- measured -- which would block every commit on a fresh clone.
    aramid's agent reproduced this independently before it shipped."""
    out = render_trigger_shim("post-commit", INTERP).decode()
    assert "if [ -f" in out
    assert "] && {" not in out


def test_shim_ends_with_an_explicit_exit_zero() -> None:
    # A graph refresh must never be able to fail a developer's commit.
    for hook in TRIGGERS:
        assert render_trigger_shim(hook, INTERP).decode().rstrip().endswith("exit 0")


def test_never_invokes_a_bare_python() -> None:
    """This machine exposes several interpreters to hook `sh`, including the
    WindowsApps store stub. The absolute path is baked in, with `py -3` as the
    only fallback."""
    out = render_trigger_shim("post-commit", INTERP).decode()
    for line in out.splitlines():
        stripped = line.strip().lstrip('"')
        assert not stripped.startswith("python "), line


def test_shim_calls_the_fast_path_never_the_cli() -> None:
    # Importing graphite.cli costs ~1.2s; hook_entry costs ~147ms.
    out = render_trigger_shim("post-merge", INTERP).decode()
    assert "graphite.hook_entry" in out
    assert "graphite.cli" not in out


def test_markers_present_and_regeneration_is_byte_stable() -> None:
    first = render_trigger_shim("post-rewrite", INTERP)
    assert MARKER_START.encode() in first
    assert MARKER_END.encode() in first
    assert first == render_trigger_shim("post-rewrite", INTERP)


def test_chain_target_is_the_hooks_own_local_sibling() -> None:
    for hook in TRIGGERS:
        assert f"{hook}.local" in render_trigger_shim(hook, INTERP).decode()


def test_no_chaining_state_is_baked_into_the_bytes() -> None:
    """The chain-check is ALWAYS present whether or not a `.local` exists.
    Baking that state in is what breaks idempotent regeneration -- the lesson
    taken from aramid's `render_shim`."""
    out = render_trigger_shim("post-commit", INTERP).decode()
    assert out.count("if [ -f") >= 1


def test_windows_interpreter_becomes_sh_style_path() -> None:
    assert sh_interpreter_path(Path(r"C:\Python314\python.exe")) == "/c/Python314/python.exe"


# --- shadowing (graphite#43) -------------------------------------------------
#
# `python -m X` puts the CWD at `sys.path[0]`, and git runs hooks from the top
# of the working tree -- so a `graphite.py` or `graphite/` at a managed repo's
# root wins over the installed package. Same defect class as `67aafb2`, but the
# trampoline swallows the evidence: `>/dev/null 2>&1` eats the traceback, `||
# true` eats the status, and all three triggers are `post-*` where git ignores
# it anyway. A hijacked hook is indistinguishable from a working one.


def test_every_hook_entry_invocation_passes_dash_P() -> None:
    """Both arms, or the one without it is the way in.

    Asserting the count too, so an arm cannot be deleted to make this pass.
    """
    arms = _hook_entry_arms()
    assert len(arms) == 2, arms
    for arm in arms:
        assert "-P" in _interpreter_flags(arm), arm


def test_a_package_shadow_cannot_hijack_the_rendered_invocation(tmp_path: Path) -> None:
    """The test that actually encodes the threat.

    Flags come from the rendered template, so dropping `-P` there lands the
    shadow here. The control arm runs the SAME command with no flags and proves
    the shadow is genuinely reachable -- without it a green result could just
    mean the shadow was never wired up.
    """
    _plant_shadow(tmp_path, package=True)
    arm = next(a for a in _hook_entry_arms() if a[0] == "$INTERP")

    assert SHADOW_MARKER in _run_hook_entry(tmp_path, [])  # control: reachable
    assert SHADOW_MARKER not in _run_hook_entry(tmp_path, _interpreter_flags(arm))


def test_a_module_shadow_never_executes_under_the_rendered_invocation(tmp_path: Path) -> None:
    """The nastier shape: importing `graphite.py` RUNS it as a side effect of
    the failed `graphite.hook_entry` lookup, so the shadow's code executes even
    though the arm goes on to raise ModuleNotFoundError -- which `|| true` then
    hides. "It errors out" is not a mitigation."""
    _plant_shadow(tmp_path, package=False)
    arm = next(a for a in _hook_entry_arms() if a[0] == "$INTERP")

    assert SHADOW_MARKER in _run_hook_entry(tmp_path, [])  # control: reachable
    assert SHADOW_MARKER not in _run_hook_entry(tmp_path, _interpreter_flags(arm))
