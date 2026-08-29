"""Private, identity-bound workspace leases for destructive readiness probes.

The local OS user and same-user process namespace remain a trust boundary.  The
lease provides best-effort identity checks and Windows rename pinning; it does
not claim complete protection from a malicious process running as that user.
"""
from __future__ import annotations

import ctypes
import os
import secrets
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ._win32_ctypes import win32 as _win32


class WorkspaceLeaseError(RuntimeError):
    """A workspace could not be acquired, revalidated, or safely cleaned."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _PathSnapshot:
    lexical: Path
    canonical: Path
    identity: tuple[int, int]
    size: int
    mtime_ns: int
    mode: int
    uid: int | None
    gid: int | None


ParentFactory = Callable[..., str]
OwnedDirectoryFactory = Callable[[Path], tuple[Path, tuple[int, int]]]


class ProbeWorkspaceLease:
    """Own a private parent and fixed child workspace until bounded cleanup."""

    def __init__(
        self,
        *,
        temp_root: Path,
        parent: _PathSnapshot,
        workspace: _PathSnapshot,
        temp_root_handle: int | None,
        parent_handle: int,
        workspace_handle: int,
        windows_handle_identities: tuple[tuple[int, int, int], tuple[int, int, int]] | None,
    ) -> None:
        self.temp_root = temp_root
        self._parent = parent
        self._workspace = workspace
        self._temp_root_handle: int | None = temp_root_handle
        self._parent_handle: int | None = parent_handle
        self._workspace_handle: int | None = workspace_handle
        self._windows_handle_identities = windows_handle_identities
        self._closed = False
        self._cleaned = False

    @property
    def parent_path(self) -> Path:
        return self._parent.lexical

    @property
    def path(self) -> Path:
        return self._workspace.lexical

    @classmethod
    def acquire(
        cls,
        *,
        temp_root: Path | None = None,
        _parent_factory: ParentFactory | None = None,
        _owned_parent_factory: OwnedDirectoryFactory | None = None,
        _workspace_factory: OwnedDirectoryFactory | None = None,
    ) -> ProbeWorkspaceLease:
        """Create and pin a private randomized parent with a fixed workspace child."""
        try:
            root = (temp_root or Path(tempfile.gettempdir())).resolve(strict=True)
            if not root.is_dir():
                raise WorkspaceLeaseError("workspace_isolation_failed")
        except (OSError, RuntimeError) as exc:
            raise WorkspaceLeaseError("workspace_isolation_failed") from exc

        parent_path: Path | None = None
        workspace_path: Path | None = None
        parent_handle: int | None = None
        workspace_handle: int | None = None
        temp_root_handle: int | None = None
        parent_owned: _PathSnapshot | None = None
        workspace_owned: _PathSnapshot | None = None
        try:
            if os.name != "nt":
                root_snapshot = _snapshot(root, root)
                temp_root_handle, root_handle_identity = _open_directory_handle(root)
                _assert_handle_matches_snapshot(
                    temp_root_handle,
                    root_handle_identity,
                    root_snapshot,
                )
            if _parent_factory is not None:
                if _owned_parent_factory is not None:
                    raise WorkspaceLeaseError("workspace_isolation_failed")
                parent_path = Path(
                    _parent_factory(prefix="graphite-doctor-", dir=str(root))
                )
                parent_path = Path(os.path.abspath(parent_path))
                _snapshot(parent_path, root)
                # A path-only factory provides no proof that this acquisition
                # created the candidate.  It is safe to inspect, never to adopt.
                raise WorkspaceLeaseError("workspace_isolation_failed")
            parent_creator = _owned_parent_factory or _create_owned_parent
            parent_path, parent_identity = parent_creator(root)
            parent_path = Path(os.path.abspath(parent_path))
            parent_before = _snapshot(parent_path, root)
            if parent_before.identity != parent_identity:
                raise WorkspaceLeaseError("workspace_isolation_failed")
            parent_owned = parent_before
            if os.name != "nt" and parent_before.mode != 0o700:
                raise WorkspaceLeaseError("workspace_isolation_failed")
            if os.name == "nt" and _owned_parent_factory is not None:
                _set_private_windows_dacl(parent_path)
            parent_handle, parent_handle_identity = _open_directory_handle(parent_path)
            _assert_handle_matches_snapshot(
                parent_handle,
                parent_handle_identity,
                parent_before,
            )

            workspace_path = parent_path / "workspace"
            workspace_creator = _workspace_factory or _create_owned_workspace
            if _workspace_factory is None and os.name != "nt":
                created_workspace_path, workspace_identity = _create_owned_workspace(
                    parent_path,
                    parent_handle,
                )
            else:
                created_workspace_path, workspace_identity = workspace_creator(parent_path)
            created_workspace_path = Path(os.path.abspath(created_workspace_path))
            if created_workspace_path != workspace_path:
                raise WorkspaceLeaseError("workspace_isolation_failed")
            workspace_owned = _snapshot(created_workspace_path, root)
            if workspace_owned.identity != workspace_identity:
                workspace_owned = None
                raise WorkspaceLeaseError("workspace_isolation_failed")
            if os.name != "nt" and workspace_owned.mode != 0o700:
                raise WorkspaceLeaseError("workspace_isolation_failed")
            if os.name == "nt" and _workspace_factory is not None:
                _set_private_windows_dacl(workspace_path)
            workspace_snapshot = _snapshot(workspace_path, root)
            workspace_handle, workspace_handle_identity = _open_directory_handle(workspace_path)
            _assert_handle_matches_snapshot(
                workspace_handle,
                workspace_handle_identity,
                workspace_snapshot,
            )
            parent_snapshot = _snapshot(parent_path, root)
            if parent_snapshot.identity != parent_before.identity:
                raise WorkspaceLeaseError("workspace_isolation_failed")
            _assert_handle_matches_snapshot(
                parent_handle,
                parent_handle_identity,
                parent_snapshot,
            )
            _assert_handle_matches_snapshot(
                workspace_handle,
                workspace_handle_identity,
                workspace_snapshot,
            )
            return cls(
                temp_root=root,
                parent=parent_snapshot,
                workspace=workspace_snapshot,
                temp_root_handle=temp_root_handle,
                parent_handle=parent_handle,
                workspace_handle=workspace_handle,
                windows_handle_identities=(parent_handle_identity, workspace_handle_identity)
                if os.name == "nt"
                else None,
            )
        except WorkspaceLeaseError:
            _close_raw_handle(workspace_handle)
            _close_raw_handle(parent_handle)
            _close_raw_handle(temp_root_handle)
            _initial_cleanup(root, parent_owned, workspace_owned)
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            _close_raw_handle(workspace_handle)
            _close_raw_handle(parent_handle)
            _close_raw_handle(temp_root_handle)
            _initial_cleanup(root, parent_owned, workspace_owned)
            raise WorkspaceLeaseError("workspace_isolation_failed") from exc

    def validate(self) -> None:
        """Revalidate containment, type, identity, ownership, and pinned handles."""
        if self._closed or self._cleaned:
            raise WorkspaceLeaseError("workspace_isolation_changed")
        try:
            parent = _snapshot(self.parent_path, self.temp_root)
            workspace = _snapshot(self.path, self.temp_root)
            if not _same_binding(parent, self._parent) or not _same_binding(workspace, self._workspace):
                raise WorkspaceLeaseError("workspace_isolation_changed")
            if os.name == "nt":
                assert self._windows_handle_identities is not None
                current_parent = _windows_handle_identity(self._required_handle(self._parent_handle))
                current_workspace = _windows_handle_identity(self._required_handle(self._workspace_handle))
                if (current_parent, current_workspace) != self._windows_handle_identities:
                    raise WorkspaceLeaseError("workspace_isolation_changed")
                _assert_handle_matches_snapshot(
                    self._required_handle(self._parent_handle),
                    current_parent,
                    self._parent,
                )
                _assert_handle_matches_snapshot(
                    self._required_handle(self._workspace_handle),
                    current_workspace,
                    self._workspace,
                )
            else:
                root_stat = os.fstat(self._required_handle(self._temp_root_handle))
                parent_stat = os.fstat(self._required_handle(self._parent_handle))
                workspace_stat = os.fstat(self._required_handle(self._workspace_handle))
                root_path_stat = self.temp_root.lstat()
                if (root_stat.st_dev, root_stat.st_ino) != (
                    root_path_stat.st_dev,
                    root_path_stat.st_ino,
                ):
                    raise WorkspaceLeaseError("workspace_isolation_changed")
                if (parent_stat.st_dev, parent_stat.st_ino) != self._parent.identity:
                    raise WorkspaceLeaseError("workspace_isolation_changed")
                if (workspace_stat.st_dev, workspace_stat.st_ino) != self._workspace.identity:
                    raise WorkspaceLeaseError("workspace_isolation_changed")
        except WorkspaceLeaseError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspaceLeaseError("workspace_isolation_changed") from exc

    def cleanup(self) -> None:
        """Delete only the still-bound lease target, otherwise deliberately leak it."""
        if self._cleaned:
            self.close()
            return
        try:
            self.validate()
        except WorkspaceLeaseError as exc:
            self.close()
            raise WorkspaceLeaseError("workspace_cleanup_blocked") from exc

        try:
            if os.name == "nt":
                self._cleanup_windows()
            else:
                if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
                    raise WorkspaceLeaseError("workspace_cleanup_blocked")
                if os.rmdir not in os.supports_dir_fd:
                    raise WorkspaceLeaseError("workspace_cleanup_blocked")
                hook = getattr(self, "_before_cleanup_delete", None)
                if hook is not None:
                    hook()
                try:
                    self.validate()
                except WorkspaceLeaseError as exc:
                    raise WorkspaceLeaseError("workspace_cleanup_blocked") from exc
                if self.parent_path.parent != self.temp_root or self.path.name != "workspace":
                    raise WorkspaceLeaseError("workspace_cleanup_blocked")
                shutil.rmtree(
                    self.path.name,
                    dir_fd=self._required_handle(self._parent_handle),
                )
                self._validate_posix_parent_binding()
                os.rmdir(
                    self.parent_path.name,
                    dir_fd=self._required_handle(self._temp_root_handle),
                )
                self._cleaned = True
                self.close()
        except WorkspaceLeaseError:
            self.close()
            raise
        except OSError as exc:
            self.close()
            raise WorkspaceLeaseError("workspace_cleanup_failed") from exc

    def _cleanup_windows(self) -> None:
        for entry in os.scandir(self.path):
            entry_path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry_path)
            else:
                entry_path.unlink()
        self.validate()
        _close_raw_handle(self._workspace_handle)
        self._workspace_handle = None
        os.rmdir(self.path)
        parent = _snapshot(self.parent_path, self.temp_root)
        if not _same_binding(parent, self._parent):
            raise WorkspaceLeaseError("workspace_cleanup_blocked")
        if any(os.scandir(self.parent_path)):
            raise WorkspaceLeaseError("workspace_cleanup_failed")
        _close_raw_handle(self._parent_handle)
        self._parent_handle = None
        os.rmdir(self.parent_path)
        self._cleaned = True
        self.close()

    def close(self) -> None:
        """Release lease handles; safe to call repeatedly."""
        if self._closed:
            return
        _close_raw_handle(self._workspace_handle)
        _close_raw_handle(self._parent_handle)
        _close_raw_handle(self._temp_root_handle)
        self._workspace_handle = None
        self._parent_handle = None
        self._temp_root_handle = None
        self._closed = True

    def _validate_posix_parent_binding(self) -> None:
        parent = _snapshot(self.parent_path, self.temp_root)
        if not _same_binding(parent, self._parent):
            raise WorkspaceLeaseError("workspace_cleanup_blocked")
        root_stat = os.fstat(self._required_handle(self._temp_root_handle))
        path_root_stat = self.temp_root.lstat()
        if (root_stat.st_dev, root_stat.st_ino) != (
            path_root_stat.st_dev,
            path_root_stat.st_ino,
        ):
            raise WorkspaceLeaseError("workspace_cleanup_blocked")
        parent_stat = os.fstat(self._required_handle(self._parent_handle))
        if (parent_stat.st_dev, parent_stat.st_ino) != self._parent.identity:
            raise WorkspaceLeaseError("workspace_cleanup_blocked")

    @staticmethod
    def _required_handle(handle: int | None) -> int:
        if handle is None:
            raise WorkspaceLeaseError("workspace_isolation_changed")
        return handle


def _snapshot(path: Path, root: Path) -> _PathSnapshot:
    lexical = Path(os.path.abspath(path))
    info = lexical.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or _path_is_reparse(info):
        raise WorkspaceLeaseError("workspace_isolation_failed")
    canonical = lexical.resolve(strict=True)
    try:
        canonical.relative_to(root)
    except ValueError as exc:
        raise WorkspaceLeaseError("workspace_isolation_failed") from exc
    if os.name == "nt" and _windows_path_is_reparse(lexical):
        raise WorkspaceLeaseError("workspace_isolation_failed")
    return _PathSnapshot(
        lexical=lexical,
        canonical=canonical,
        identity=(info.st_dev, info.st_ino),
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        mode=stat.S_IMODE(info.st_mode),
        uid=getattr(info, "st_uid", None),
        gid=getattr(info, "st_gid", None),
    )


def _same_binding(current: _PathSnapshot, expected: _PathSnapshot) -> bool:
    return (
        current.lexical == expected.lexical
        and current.canonical == expected.canonical
        and current.identity == expected.identity
        and current.mode == expected.mode
        and current.uid == expected.uid
        and current.gid == expected.gid
    )


def _path_is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _create_owned_parent(root: Path) -> tuple[Path, tuple[int, int]]:
    if os.name == "nt":
        path = _create_private_windows_directory(root)
    else:
        path = Path(tempfile.mkdtemp(prefix="graphite-doctor-", dir=str(root)))
    info = path.lstat()
    return path, (info.st_dev, info.st_ino)


def _create_owned_workspace(
    parent: Path,
    parent_handle: int | None = None,
) -> tuple[Path, tuple[int, int]]:
    if os.name == "nt":
        path = _create_private_windows_directory(parent, "workspace")
    else:
        path = parent / "workspace"
        if parent_handle is None:
            os.mkdir(path, 0o700)
            info = path.lstat()
            return path, (info.st_dev, info.st_ino)
        os.mkdir(path.name, 0o700, dir_fd=parent_handle)
        info = os.stat(path.name, dir_fd=parent_handle, follow_symlinks=False)
        return path, (info.st_dev, info.st_ino)
    info = path.lstat()
    return path, (info.st_dev, info.st_ino)


def _initial_cleanup(
    root: Path,
    parent: _PathSnapshot | None,
    workspace: _PathSnapshot | None,
) -> None:
    for owned in (workspace, parent):
        if owned is None:
            continue
        try:
            current = _snapshot(owned.lexical, root)
            if _same_binding(current, owned):
                os.rmdir(owned.lexical)
        except (OSError, WorkspaceLeaseError):
            # Acquisition failure cleanup is deliberately best effort.  An
            # uncertain binding is leaked rather than pathname-deleted.
            pass


def _open_directory_handle(path: Path) -> tuple[int, tuple[int, int, int]]:
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        handle = os.open(path, flags)
        os.set_inheritable(handle, False)
        info = os.fstat(handle)
        return handle, (info.st_dev, 0, info.st_ino)

    from ctypes import wintypes

    kernel32 = _win32().WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x80000000 | 0x80,
        0x1 | 0x2,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise _win32().WinError(_win32().get_last_error())
    raw_handle = int(handle)
    set_handle_information = kernel32.SetHandleInformation
    set_handle_information.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    set_handle_information.restype = wintypes.BOOL
    if not set_handle_information(raw_handle, 0x1, 0):
        error = _win32().get_last_error()
        _close_raw_handle(raw_handle)
        raise _win32().WinError(error)
    try:
        identity = _windows_handle_identity(raw_handle)
        _reject_windows_handle_reparse(raw_handle)
        return raw_handle, identity
    except Exception:
        _close_raw_handle(raw_handle)
        raise


def _close_raw_handle(handle: int | None) -> None:
    if handle is None:
        return
    if os.name != "nt":
        try:
            os.close(handle)
        except OSError:
            pass
        return
    from ctypes import wintypes

    kernel32 = _win32().WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


def _windows_handle_identity(handle: int) -> tuple[int, int, int]:
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = _win32().WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFileInformationByHandle
    function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    function.restype = wintypes.BOOL
    information = ByHandleFileInformation()
    if not function(handle, ctypes.byref(information)):
        raise _win32().WinError(_win32().get_last_error())
    return (
        information.volume_serial_number,
        information.file_index_high,
        information.file_index_low,
    )


def _windows_handle_final_path(handle: int) -> Path:
    from ctypes import wintypes

    kernel32 = _win32().WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFinalPathNameByHandleW
    function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    function.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = function(handle, buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise _win32().WinError(_win32().get_last_error())
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value).resolve(strict=True)


def _assert_handle_matches_snapshot(
    handle: int,
    handle_identity: tuple[int, int, int],
    snapshot: _PathSnapshot,
) -> None:
    if os.name == "nt":
        file_index = (handle_identity[1] << 32) | handle_identity[2]
        if file_index != snapshot.identity[1]:
            raise WorkspaceLeaseError("workspace_isolation_failed")
        final_path = _windows_handle_final_path(handle)
        if os.path.normcase(str(final_path)) != os.path.normcase(str(snapshot.canonical)):
            raise WorkspaceLeaseError("workspace_isolation_failed")
        return
    info = os.fstat(handle)
    if (info.st_dev, info.st_ino) != snapshot.identity:
        raise WorkspaceLeaseError("workspace_isolation_failed")


def _reject_windows_handle_reparse(handle: int) -> None:
    from ctypes import wintypes

    class FileAttributeTagInformation(ctypes.Structure):
        _fields_ = [("file_attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]

    kernel32 = _win32().WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFileInformationByHandleEx
    function.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    function.restype = wintypes.BOOL
    information = FileAttributeTagInformation()
    if not function(handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
        raise _win32().WinError(_win32().get_last_error())
    if information.file_attributes & 0x400 or information.reparse_tag:
        raise WorkspaceLeaseError("workspace_isolation_failed")


def _windows_path_is_reparse(path: Path) -> bool:
    if os.name != "nt":
        return False
    from ctypes import wintypes

    kernel32 = _win32().WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFileAttributesW
    function.argtypes = [wintypes.LPCWSTR]
    function.restype = wintypes.DWORD
    attributes = function(str(path))
    if attributes == 0xFFFFFFFF:
        raise _win32().WinError(_win32().get_last_error())
    return bool(attributes & 0x400)


def _windows_private_security_descriptor() -> int:
    """Allocate a protected DACL granting full control only to the current user."""
    from ctypes import wintypes

    advapi32 = _win32().WinDLL("advapi32", use_last_error=True)
    kernel32 = _win32().WinDLL("kernel32", use_last_error=True)
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_token_information.restype = wintypes.BOOL
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    convert_sid.restype = wintypes.BOOL
    convert_descriptor = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert_descriptor.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert_descriptor.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE

    token = wintypes.HANDLE()
    if not open_process_token(get_current_process(), 0x0008, ctypes.byref(token)):
        raise _win32().WinError(_win32().get_last_error())
    sid_text = wintypes.LPWSTR()
    descriptor = wintypes.LPVOID()
    try:
        needed = wintypes.DWORD()
        get_token_information(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise _win32().WinError(_win32().get_last_error())
        buffer = ctypes.create_string_buffer(needed.value)
        if not get_token_information(token, 1, buffer, needed, ctypes.byref(needed)):
            raise _win32().WinError(_win32().get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
        if not convert_sid(sid, ctypes.byref(sid_text)):
            raise _win32().WinError(_win32().get_last_error())
        sddl = f"D:P(A;OICI;FA;;;{sid_text.value})"
        if not convert_descriptor(sddl, 1, ctypes.byref(descriptor), None):
            raise _win32().WinError(_win32().get_last_error())
        if descriptor.value is None:
            # The conversion reported success, so the out-pointer was filled.
            raise RuntimeError("security descriptor conversion succeeded without a descriptor")
        result = int(descriptor.value)
        descriptor = wintypes.LPVOID()
        return result
    finally:
        if descriptor:
            local_free(descriptor)
        if sid_text:
            local_free(sid_text)
        _close_raw_handle(int(token.value) if token.value else None)


def _free_windows_security_descriptor(descriptor: int) -> None:
    from ctypes import wintypes

    kernel32 = _win32().WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL
    local_free(descriptor)


def _create_private_windows_directory(parent: Path, name: str | None = None) -> Path:
    """Create a Windows directory with its protected DACL applied atomically."""
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("security_descriptor", wintypes.LPVOID),
            ("inherit_handle", wintypes.BOOL),
        ]

    kernel32 = _win32().WinDLL("kernel32", use_last_error=True)
    create_directory = kernel32.CreateDirectoryW
    create_directory.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
    create_directory.restype = wintypes.BOOL
    descriptor = _windows_private_security_descriptor()
    attributes = SecurityAttributes(
        ctypes.sizeof(SecurityAttributes),
        ctypes.c_void_p(descriptor),
        False,
    )
    try:
        for _ in range(64):
            child_name = name or f"graphite-doctor-{secrets.token_hex(16)}"
            path = parent / child_name
            if create_directory(str(path), ctypes.byref(attributes)):
                return path
            error = _win32().get_last_error()
            if name is not None or error != 183:
                raise _win32().WinError(error)
        raise FileExistsError("unable to allocate a unique probe workspace parent")
    finally:
        _free_windows_security_descriptor(descriptor)


def _set_private_windows_dacl(path: Path) -> None:
    """Apply the protected DACL to an already identity-verified injected path."""
    if os.name != "nt":
        return
    from ctypes import wintypes

    advapi32 = _win32().WinDLL("advapi32", use_last_error=True)
    set_file_security = advapi32.SetFileSecurityW
    set_file_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
    set_file_security.restype = wintypes.BOOL
    descriptor = _windows_private_security_descriptor()
    try:
        if not set_file_security(str(path), 0x80000004, descriptor):
            raise _win32().WinError(_win32().get_last_error())
    finally:
        _free_windows_security_descriptor(descriptor)
