"""Handle-bound deletion worker for TypeScript activation temporary homes.

This module is executed as an isolated Python script by ``typescript_activation``.
It intentionally has no Graphite imports so ``python -I`` can run the packaged
file without trusting ``PYTHONPATH`` or the selected repository.
"""
from __future__ import annotations

import ctypes
import os
import stat
import sys
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Protocol, cast


class _Win32Ctypes(Protocol):
    """The Windows-only `ctypes` surface, declared so the type gate passes on
    every platform. A private copy of `graphite._win32_ctypes.Win32Ctypes`:
    this file runs under `python -I` and must not import from the package.
    Same module object, attributes looked up at the call."""

    def WinDLL(self, name: str, *, use_last_error: bool = ...) -> ctypes.CDLL: ...

    def WinError(self, code: int | None = ..., descr: str | None = ...) -> OSError: ...

    def get_last_error(self) -> int: ...


def _win32() -> _Win32Ctypes:
    return cast(_Win32Ctypes, ctypes)


EXIT_UNSAFE = 73
EXIT_FAILED = 74
EXIT_POSIX_ROOT_RETAINED = 75
_ENUMERATION_BATCH_SIZE = 64
_WINDOWS_DIRECTORY_BUFFER_BYTES = 64 * 1024


def _same_posix_identity(details: os.stat_result, expected: tuple[int, int]) -> bool:
    return (details.st_dev, details.st_ino) == expected


def _posix_stat_at(parent_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def _posix_open_directory(parent_fd: int, name: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    os.set_inheritable(descriptor, False)
    return descriptor


def _posix_open_nondirectory(parent_fd: int, name: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    os.set_inheritable(descriptor, False)
    return descriptor


def _valid_entry_name(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and "/" not in name and "\0" not in name


def _posix_entry_batch(descriptor: int) -> list[tuple[str, os.stat_result]]:
    with os.scandir(descriptor) as entries:
        return [
            (entry.name, entry.stat(follow_symlinks=False))
            for entry in islice(entries, _ENUMERATION_BATCH_SIZE)
        ]


def _delete_posix_entry(
    descriptor: int, name: str, observed: os.stat_result
) -> None:
    if not _valid_entry_name(name):
        raise OSError("unsafe directory entry")
    expected = (observed.st_dev, observed.st_ino)
    if stat.S_ISDIR(observed.st_mode) and not stat.S_ISLNK(observed.st_mode):
        child_fd = _posix_open_directory(descriptor, name)
        try:
            if not _same_posix_identity(os.fstat(child_fd), expected):
                raise OSError("directory identity changed")
            _delete_posix_directory(child_fd)
            current = _posix_stat_at(descriptor, name)
            if not _same_posix_identity(current, expected) or not stat.S_ISDIR(
                current.st_mode
            ):
                raise OSError("directory binding changed")
            os.rmdir(name, dir_fd=descriptor)
        finally:
            os.close(child_fd)
        return

    if stat.S_ISLNK(observed.st_mode):
        current = _posix_stat_at(descriptor, name)
        if not _same_posix_identity(current, expected) or not stat.S_ISLNK(
            current.st_mode
        ):
            raise OSError("symlink binding changed")
        os.unlink(name, dir_fd=descriptor)
        return

    child_fd = _posix_open_nondirectory(descriptor, name)
    try:
        opened = os.fstat(child_fd)
        if not _same_posix_identity(opened, expected) or stat.S_ISDIR(opened.st_mode):
            raise OSError("file identity changed")
        current = _posix_stat_at(descriptor, name)
        if not _same_posix_identity(current, expected) or stat.S_ISDIR(current.st_mode):
            raise OSError("file binding changed")
        os.unlink(name, dir_fd=descriptor)
    finally:
        os.close(child_fd)


def _delete_posix_directory(descriptor: int) -> None:
    while batch := _posix_entry_batch(descriptor):
        for name, observed in batch:
            _delete_posix_entry(descriptor, name, observed)


def _delete_posix(path: Path, expected: tuple[int, int]) -> None:
    """Empty the identity-bound lease while deliberately retaining its root.

    POSIX ``unlinkat``/``rmdir`` accepts a name relative to a directory file
    descriptor; it cannot remove the directory referenced by an already-open
    descriptor. A final stat-then-rmdir would therefore reopen the root-name
    replacement race. The root was created by Graphite and its contents are
    disposable after the package manager runs, so contents are removed only
    through the held, no-follow descriptor and the empty root is left for OS
    temporary-directory reclamation.

    A hostile process running as the same UID can still race individual names
    inside that disposable root between validation and unlink. It can cause
    cleanup failure or replace disposable contents, but descriptor-relative
    traversal cannot escape the held root and this worker never removes a
    replacement of the root itself or any ancestor.
    """
    parent = path.parent
    name = path.name
    if not path.is_absolute() or not _valid_entry_name(name):
        raise OSError("cleanup path invalid")
    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    parent_fd = os.open(parent, parent_flags)
    os.set_inheritable(parent_fd, False)
    root_fd = -1
    try:
        root_fd = _posix_open_directory(parent_fd, name)
        details = os.fstat(root_fd)
        if not _same_posix_identity(details, expected) or not stat.S_ISDIR(details.st_mode):
            raise OSError("cleanup lease changed")
        _delete_posix_directory(root_fd)
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(parent_fd)


@dataclass(frozen=True)
class _WindowsEntry:
    name: str
    file_id: int
    attributes: int


class _WindowsBackend(Protocol):
    directory_attribute: int
    reparse_attribute: int

    def open(self, path: Path, *, directory: bool) -> int: ...

    def information(self, handle: int) -> tuple[int, int, int]: ...

    def entry_batch(self, handle: int) -> list[_WindowsEntry]: ...

    def mark_delete(self, handle: int) -> None: ...

    def close(self, handle: int) -> None: ...


class _FileIdInformation(ctypes.Structure):
    _fields_ = (
        ("volume_serial", ctypes.c_uint64),
        ("file_id", ctypes.c_ubyte * 16),
    )


class _FileAttributeTagInformation(ctypes.Structure):
    _fields_ = (
        ("file_attributes", ctypes.c_uint32),
        ("reparse_tag", ctypes.c_uint32),
    )


class _FileIdExtdDirectoryInformation(ctypes.Structure):
    _fields_ = (
        ("next_entry_offset", ctypes.c_uint32),
        ("file_index", ctypes.c_uint32),
        ("creation_time", ctypes.c_int64),
        ("last_access_time", ctypes.c_int64),
        ("last_write_time", ctypes.c_int64),
        ("change_time", ctypes.c_int64),
        ("end_of_file", ctypes.c_int64),
        ("allocation_size", ctypes.c_int64),
        ("file_attributes", ctypes.c_uint32),
        ("file_name_length", ctypes.c_uint32),
        ("ea_size", ctypes.c_uint32),
        ("reparse_tag", ctypes.c_uint32),
        ("file_id", ctypes.c_ubyte * 16),
        ("file_name", ctypes.c_uint16 * 1),
    )


class _FileDispositionInformation(ctypes.Structure):
    _fields_ = (("delete_file", ctypes.c_int),)


class _FileDispositionInformationEx(ctypes.Structure):
    _fields_ = (("flags", ctypes.c_uint32),)


class _FileBasicInformation(ctypes.Structure):
    _fields_ = (
        ("creation_time", ctypes.c_int64),
        ("last_access_time", ctypes.c_int64),
        ("last_write_time", ctypes.c_int64),
        ("change_time", ctypes.c_int64),
        ("file_attributes", ctypes.c_uint32),
    )


class _NativeWindowsBackend:
    directory_attribute = 0x10
    reparse_attribute = 0x400
    _readonly_attribute = 0x1
    _invalid_handle = ctypes.c_void_p(-1).value
    _error_no_more_files = 18
    _fallback_errors = {1, 50, 87}

    def __init__(self) -> None:
        from ctypes import wintypes

        self._wintypes = wintypes
        self._api = _win32().WinDLL("kernel32", use_last_error=True)
        self._api.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        self._api.CreateFileW.restype = wintypes.HANDLE
        self._api.GetFileInformationByHandleEx.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        self._api.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self._api.SetFileInformationByHandle.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        self._api.SetFileInformationByHandle.restype = wintypes.BOOL
        self._api.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._api.CloseHandle.restype = wintypes.BOOL

    def open(self, path: Path, *, directory: bool) -> int:
        delete = 0x00010000
        read_attributes = 0x00000080
        write_attributes = 0x00000100
        list_directory = 0x00000001 if directory else 0
        share_read_write = 0x00000001 | 0x00000002
        open_existing = 3
        backup_semantics = 0x02000000
        open_reparse_point = 0x00200000
        handle = self._api.CreateFileW(
            str(path),
            delete | read_attributes | write_attributes | list_directory,
            share_read_write,
            None,
            open_existing,
            backup_semantics | open_reparse_point,
            None,
        )
        if handle == self._invalid_handle:
            raise _win32().WinError(_win32().get_last_error())
        return int(handle)

    def open_identity(self, path: Path) -> int:
        read_attributes = 0x00000080
        share_all = 0x00000001 | 0x00000002 | 0x00000004
        open_existing = 3
        backup_semantics = 0x02000000
        open_reparse_point = 0x00200000
        handle = self._api.CreateFileW(
            str(path),
            read_attributes,
            share_all,
            None,
            open_existing,
            backup_semantics | open_reparse_point,
            None,
        )
        if handle == self._invalid_handle:
            raise _win32().WinError(_win32().get_last_error())
        return int(handle)

    def information(self, handle: int) -> tuple[int, int, int]:
        identity = _FileIdInformation()
        if not self._api.GetFileInformationByHandleEx(
            handle, 18, ctypes.byref(identity), ctypes.sizeof(identity)
        ):
            raise _win32().WinError(_win32().get_last_error())
        attributes = _FileAttributeTagInformation()
        if not self._api.GetFileInformationByHandleEx(
            handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)
        ):
            raise _win32().WinError(_win32().get_last_error())
        file_id = int.from_bytes(bytes(identity.file_id), "little")
        return identity.volume_serial, file_id, attributes.file_attributes

    def entry_batch(self, handle: int) -> list[_WindowsEntry]:
        information_class = 20
        while True:
            result: list[_WindowsEntry] = []
            buffer = ctypes.create_string_buffer(_WINDOWS_DIRECTORY_BUFFER_BYTES)
            if not self._api.GetFileInformationByHandleEx(
                handle, information_class, buffer, len(buffer)
            ):
                error = _win32().get_last_error()
                if error == self._error_no_more_files:
                    return []
                raise _win32().WinError(error)
            information_class = 19
            offset = 0
            while True:
                address = ctypes.addressof(buffer) + offset
                entry = _FileIdExtdDirectoryInformation.from_address(address)
                if entry.file_name_length % 2:
                    raise OSError("directory name encoding invalid")
                name_address = (
                    address + _FileIdExtdDirectoryInformation.file_name.offset
                )
                name_units = entry.file_name_length // 2
                if (
                    name_address + entry.file_name_length
                    > ctypes.addressof(buffer) + len(buffer)
                ):
                    raise OSError("directory name exceeds enumeration buffer")
                raw_name = (ctypes.c_uint16 * name_units).from_address(name_address)
                name = bytes(raw_name).decode("utf-16-le", "strict")
                if name not in {".", ".."}:
                    if not _valid_entry_name(name):
                        raise OSError("unsafe directory entry")
                    result.append(
                        _WindowsEntry(
                            name,
                            int.from_bytes(bytes(entry.file_id), "little"),
                            entry.file_attributes,
                        )
                    )
                if entry.next_entry_offset == 0:
                    break
                if entry.next_entry_offset < _FileIdExtdDirectoryInformation.file_name.offset:
                    raise OSError("directory enumeration offset invalid")
                offset += entry.next_entry_offset
                if offset >= len(buffer):
                    raise OSError("directory enumeration overflow")
            if result:
                return result

    def _set_basic_information(self, handle: int, details: _FileBasicInformation) -> None:
        if not self._api.SetFileInformationByHandle(
            handle, 0, ctypes.byref(details), ctypes.sizeof(details)
        ):
            raise _win32().WinError(_win32().get_last_error())

    def _clear_readonly(self, handle: int) -> None:
        details = _FileBasicInformation()
        if not self._api.GetFileInformationByHandleEx(
            handle, 0, ctypes.byref(details), ctypes.sizeof(details)
        ):
            raise _win32().WinError(_win32().get_last_error())
        if details.file_attributes & self._readonly_attribute:
            details.file_attributes &= ~self._readonly_attribute
            self._set_basic_information(handle, details)

    def mark_delete(self, handle: int) -> None:
        extended = _FileDispositionInformationEx(0x1 | 0x2 | 0x10)
        if self._api.SetFileInformationByHandle(
            handle, 21, ctypes.byref(extended), ctypes.sizeof(extended)
        ):
            return
        error = _win32().get_last_error()
        if error not in self._fallback_errors:
            raise _win32().WinError(error)
        self._clear_readonly(handle)
        fallback = _FileDispositionInformation(1)
        if not self._api.SetFileInformationByHandle(
            handle, 4, ctypes.byref(fallback), ctypes.sizeof(fallback)
        ):
            raise _win32().WinError(_win32().get_last_error())

    def close(self, handle: int) -> None:
        if handle and not self._api.CloseHandle(handle):
            raise _win32().WinError(_win32().get_last_error())


def _delete_windows_handle(
    path: Path,
    handle: int,
    expected_volume: int,
    expected_file_id: int,
    expected_attributes: int,
    backend: _WindowsBackend,
) -> None:
    volume, file_id, attributes = backend.information(handle)
    if (
        volume != expected_volume
        or file_id != expected_file_id
        or bool(attributes & backend.directory_attribute)
        != bool(expected_attributes & backend.directory_attribute)
    ):
        raise OSError("Windows entry identity changed")
    if attributes & backend.reparse_attribute:
        backend.mark_delete(handle)
        return
    if attributes & backend.directory_attribute:
        while batch := backend.entry_batch(handle):
            for entry in batch:
                child_path = path / entry.name
                child_is_directory = bool(
                    entry.attributes & backend.directory_attribute
                    and not entry.attributes & backend.reparse_attribute
                )
                child_handle = backend.open(child_path, directory=child_is_directory)
                try:
                    _delete_windows_handle(
                        child_path,
                        child_handle,
                        expected_volume,
                        entry.file_id,
                        entry.attributes,
                        backend,
                    )
                finally:
                    backend.close(child_handle)
    backend.mark_delete(handle)


def _delete_windows(
    path: Path,
    expected: tuple[int, int],
    backend: _WindowsBackend | None = None,
) -> None:
    if not path.is_absolute() or not _valid_entry_name(path.name):
        raise OSError("cleanup path invalid")
    selected = backend or _NativeWindowsBackend()
    handle = selected.open(path, directory=True)
    try:
        volume, file_id, attributes = selected.information(handle)
        if (
            volume != expected[0]
            or file_id != expected[1]
            or not attributes & selected.directory_attribute
            or attributes & selected.reparse_attribute
        ):
            raise OSError("cleanup lease changed")
        _delete_windows_handle(path, handle, volume, file_id, attributes, selected)
    finally:
        selected.close(handle)


def _windows_directory_identity(path: Path) -> tuple[int, int]:
    """Capture a directory's 64-bit volume and 128-bit FileIdInfo identity."""
    backend = _NativeWindowsBackend()
    handle = backend.open_identity(path)
    try:
        volume, file_id, attributes = backend.information(handle)
        if (
            not attributes & backend.directory_attribute
            or attributes & backend.reparse_attribute
        ):
            raise OSError("Windows cleanup lease is not a plain directory")
        return volume, file_id
    finally:
        backend.close(handle)


def _parse_arguments(argv: list[str]) -> tuple[Path, tuple[int, int]]:
    if len(argv) != 4:
        raise ValueError("cleanup arguments invalid")
    path = Path(argv[1])
    device = int(argv[2], 10)
    inode = int(argv[3], 10)
    if device < 0 or inode < 0 or not path.is_absolute():
        raise ValueError("cleanup arguments invalid")
    return path, (device, inode)


def main(argv: list[str] | None = None) -> int:
    try:
        path, expected = _parse_arguments(sys.argv if argv is None else argv)
        if os.name == "nt":
            _delete_windows(path, expected)
        else:
            _delete_posix(path, expected)
            return EXIT_POSIX_ROOT_RETAINED
        return 0
    except (OSError, RuntimeError, TypeError, ValueError):
        return EXIT_UNSAFE
    except Exception:
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
