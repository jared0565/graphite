"""Who owns a path, asked of the operating system.

From below a repository's root, graphite takes that root for the repository
(`ingest.repository_root`): its graph is read, its ledger written, and the
root registered with the daemon. The root is the nearest directory upward
that holds `.git`, and a directory can be made, and a `.git` put in it, by
whoever may write to its parent. An ancestor is taken only when it belongs
to the user running graphite.

On POSIX that is the effective uid. On Windows it is the owner SID: the
user's own, or the Administrators group while this process runs with an
elevated administrator's token, since a Windows policy can make that group,
not the user, the owner of what such a process creates.

Every question here answers False when it cannot be answered: a path whose
owner cannot be read is nobody's.
"""
from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

ADMINISTRATORS = "S-1-5-32-544"


def owner_is_current_user(owner: str, user: str, *, elevated_admin: bool) -> bool:
    """The Windows decision, apart from the calls that gather its inputs.

    `owner` and `user` are SID strings; an empty one could not be read.
    """
    if not owner or not user:
        return False
    if owner == user:
        return True
    return owner == ADMINISTRATORS and elevated_admin


def owned_by_current_user(path: Path) -> bool:
    """Whether `path` (a directory or a file) belongs to the user running
    this process. A link is asked about itself, never where it leads, on
    every platform: its owner is whoever made it (`ingest.trusted_root`
    asks about both). Never raises."""
    try:
        if sys.platform == "win32":
            return owner_is_current_user(
                _windows_owner_sid(path) or "",
                _windows_user_sid() or "",
                elevated_admin=_windows_is_elevated_admin(),
            )
        return os.lstat(path).st_uid == os.geteuid()
    except Exception:
        return False


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1

    _GetNamedSecurityInfoW = _advapi32.GetNamedSecurityInfoW
    _GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _GetNamedSecurityInfoW.restype = wintypes.DWORD

    _ConvertSidToStringSidW = _advapi32.ConvertSidToStringSidW
    _ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    _ConvertSidToStringSidW.restype = wintypes.BOOL

    _ConvertStringSidToSidW = _advapi32.ConvertStringSidToSidW
    _ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    _ConvertStringSidToSidW.restype = wintypes.BOOL

    _CheckTokenMembership = _advapi32.CheckTokenMembership
    _CheckTokenMembership.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)]
    _CheckTokenMembership.restype = wintypes.BOOL

    _OpenProcessToken = _advapi32.OpenProcessToken
    _OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _OpenProcessToken.restype = wintypes.BOOL

    _GetTokenInformation = _advapi32.GetTokenInformation
    _GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _GetTokenInformation.restype = wintypes.BOOL

    _GetCurrentProcess = _kernel32.GetCurrentProcess
    _GetCurrentProcess.argtypes = []
    _GetCurrentProcess.restype = wintypes.HANDLE

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL

    _LocalFree = _kernel32.LocalFree
    _LocalFree.argtypes = [ctypes.c_void_p]
    _LocalFree.restype = ctypes.c_void_p

    def _sid_string(sid: int | None) -> str | None:
        """The text form of the SID at address `sid`, or None."""
        if not sid:
            return None
        text = ctypes.c_void_p()
        if not _ConvertSidToStringSidW(sid, ctypes.byref(text)) or not text.value:
            return None
        try:
            return ctypes.wstring_at(text.value)
        finally:
            _LocalFree(text)

    def _windows_owner_sid(path: Path) -> str | None:
        """The SID that owns `path`, as text, or None when it cannot be read."""
        owner = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        status = _GetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
        if status != 0:
            return None
        try:
            # `owner` points into `descriptor`, so it is read before the free.
            return _sid_string(owner.value)
        finally:
            if descriptor.value:
                _LocalFree(descriptor)

    @functools.lru_cache(maxsize=1)
    def _windows_user_sid() -> str | None:
        """The SID of the user this process runs as. It cannot change while
        the process lives, so it is read once."""
        token = wintypes.HANDLE()
        if not _OpenProcessToken(_GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            needed = wintypes.DWORD()
            _GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
            if not needed.value:
                return None
            buffer = ctypes.create_string_buffer(needed.value)
            if not _GetTokenInformation(token, _TOKEN_USER, buffer, needed, ctypes.byref(needed)):
                return None
            # TOKEN_USER begins with SID_AND_ATTRIBUTES, which begins with the SID's address.
            return _sid_string(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0])
        finally:
            _CloseHandle(token)

    @functools.lru_cache(maxsize=1)
    def _windows_is_elevated_admin() -> bool:
        """Whether this process's token counts as a member of Administrators.
        A token an administrator gets without elevation holds that group
        for denying only, and is not counted."""
        sid = ctypes.c_void_p()
        if not _ConvertStringSidToSidW(ADMINISTRATORS, ctypes.byref(sid)):
            return False
        try:
            member = wintypes.BOOL()
            if not _CheckTokenMembership(None, sid, ctypes.byref(member)):
                return False
            return bool(member.value)
        finally:
            _LocalFree(sid)
