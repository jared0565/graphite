"""Rendering of graphite's git-hook trampolines.

Pure byte rendering, so every Windows correctness rule that bit aramid is
cheaply assertable here rather than only observable in a live repo.
"""
from __future__ import annotations

from pathlib import Path

from graphite.hookshim import (
    MARKER_END,
    MARKER_START,
    TRIGGERS,
    render_trigger_shim,
    sh_interpreter_path,
)

INTERP = Path("C:/Python314/python.exe")


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
