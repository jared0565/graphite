"""A typed, call-time view of the Windows-only part of :mod:`ctypes`.

`ctypes.WinDLL`, `ctypes.WinError` and `ctypes.get_last_error` exist only on
Windows, and typeshed declares them that way -- so a module that names them
unguarded fails the type gate on the ubuntu lint runner even though every
call site is behind an `os.name == "nt"` check at runtime.

A `sys.platform` guard is the textbook answer and is deliberately NOT used
here: the suite reaches these branches on Windows by patching `os.name`, and
mypy's `sys.platform` narrowing would make them unreachable under that
patching. This view keeps the runtime exactly as it was -- the same module
object, every attribute looked up at the call -- while giving mypy a
declaration it accepts on any platform. Tests that monkeypatch
`ctypes.WinDLL` therefore still take effect.

`_cleanup_worker.py` carries a private copy of this shape on purpose: it is
executed as an isolated script under `python -I` and must not import from
the package.
"""
from __future__ import annotations

import ctypes
from typing import Protocol, cast


class Win32Ctypes(Protocol):
    def WinDLL(self, name: str, *, use_last_error: bool = ...) -> ctypes.CDLL: ...

    def WinError(self, code: int | None = ..., descr: str | None = ...) -> OSError: ...

    def get_last_error(self) -> int: ...


def win32() -> Win32Ctypes:
    """The `ctypes` module, typed as its Windows surface. Call-time lookup."""
    return cast(Win32Ctypes, ctypes)
