"""Tests for `graphite._win32_ctypes`, the typed call-time view of `ctypes`.

Named after the module so aramid's mutation stage 1 (`tests/test_<module>.py`)
has something to run; `-k _win32_ctypes` selected nothing before this file.

The module's one promise is that the view is the real `ctypes` module looked
up at call time, so a test that monkeypatches `ctypes.WinDLL` still takes
effect through it. A cached or copied view would break the doctor and probe
suites on Windows silently, so that promise is pinned here on every platform.
"""
from __future__ import annotations

import ctypes

import pytest

from graphite._win32_ctypes import win32


def test_view_is_the_ctypes_module_itself() -> None:
    assert win32() is ctypes


def test_attributes_are_looked_up_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    view = win32()
    sentinel = object()
    # Set rather than patch-existing: WinDLL only exists on Windows, and this
    # test must exercise the same path everywhere.
    monkeypatch.setattr(ctypes, "WinDLL", sentinel, raising=False)
    assert view.WinDLL is sentinel  # type: ignore[comparison-overlap]
    assert win32().WinDLL is sentinel  # type: ignore[comparison-overlap]


def test_patched_get_last_error_is_visible_through_the_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 1234, raising=False)
    assert win32().get_last_error() == 1234
