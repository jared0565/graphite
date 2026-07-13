"""Race-free Windows process launch inside a kill-on-close Job Object."""
from __future__ import annotations

import ctypes
import io
import os
import signal
from ctypes import wintypes
from pathlib import Path
from typing import BinaryIO, Mapping

from .process_contracts import WINDOWS_PROCESS_CREATION_LOCK, build_windows_environment_block

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
STILL_ACTIVE = 259

ULONG_PTR = wintypes.WPARAM
SIZE_T = ctypes.c_size_t


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = (("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID), ("bInheritHandle", wintypes.BOOL))


class STARTUPINFOW(ctypes.Structure):
    _fields_ = (
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
    )


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = (("StartupInfo", STARTUPINFOW), ("lpAttributeList", wintypes.LPVOID))


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = (("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD))


class IO_COUNTERS(ctypes.Structure):
    _fields_ = tuple((name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
        "WriteTransferCount", "OtherTransferCount",
    ))


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", SIZE_T), ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ULONG_PTR),
        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
    )


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T),
    )


def _kernel32() -> ctypes.WinDLL:
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = (ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.LPCWSTR)
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    api.SetInformationJobObject.restype = wintypes.BOOL
    api.CreatePipe.argtypes = (ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD)
    api.CreatePipe.restype = wintypes.BOOL
    api.SetHandleInformation.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
    api.SetHandleInformation.restype = wintypes.BOOL
    api.InitializeProcThreadAttributeList.argtypes = (wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SIZE_T))
    api.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    api.UpdateProcThreadAttribute.argtypes = (wintypes.LPVOID, wintypes.DWORD, SIZE_T, wintypes.LPVOID, SIZE_T, wintypes.LPVOID, ctypes.POINTER(SIZE_T))
    api.UpdateProcThreadAttribute.restype = wintypes.BOOL
    api.DeleteProcThreadAttributeList.argtypes = (wintypes.LPVOID,)
    api.DeleteProcThreadAttributeList.restype = None
    api.CreateProcessW.argtypes = (
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(SECURITY_ATTRIBUTES), ctypes.POINTER(SECURITY_ATTRIBUTES),
        wintypes.BOOL, wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
    )
    api.CreateProcessW.restype = wintypes.BOOL
    api.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.ResumeThread.argtypes = (wintypes.HANDLE,)
    api.ResumeThread.restype = wintypes.DWORD
    api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    api.GetExitCodeProcess.restype = wintypes.BOOL
    api.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    api.TerminateJobObject.restype = wintypes.BOOL
    api.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    api.TerminateProcess.restype = wintypes.BOOL
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    return api


class JobProcess:
    def __init__(self, api: ctypes.WinDLL, process_handle: int, job_handle: int, pid: int,
                 stdin: BinaryIO | None, stdout: BinaryIO, stderr: BinaryIO) -> None:
        self._api = api
        self._process_handle = process_handle
        self._job_handle = job_handle
        self.pid = pid
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        code = wintypes.DWORD()
        if not self._api.GetExitCodeProcess(self._process_handle, ctypes.byref(code)):
            raise OSError("process query failed")
        if code.value != STILL_ACTIVE:
            self.returncode = code.value
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        import subprocess

        milliseconds = 0xFFFFFFFF if timeout is None else max(0, min(0xFFFFFFFE, int(timeout * 1000 + 0.999)))
        result = self._api.WaitForSingleObject(self._process_handle, milliseconds)
        if result == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired([], timeout)
        if result != WAIT_OBJECT_0:
            raise OSError("process wait failed")
        value = self.poll()
        if value is None:
            raise OSError("process state unavailable")
        return value

    def send_signal(self, signal_number: int) -> None:
        if signal_number != signal.CTRL_BREAK_EVENT:
            raise ValueError("unsupported signal")
        os.kill(self.pid, signal_number)

    def kill(self) -> bool:
        if self._job_handle:
            return bool(self._api.TerminateJobObject(self._job_handle, 1))
        elif self._process_handle:
            return bool(self._api.TerminateProcess(self._process_handle, 1))
        return True

    def terminate_tree(self) -> bool:
        return self.kill()

    def close_handles(self) -> bool:
        closed = True
        if self._job_handle:
            closed = bool(self._api.CloseHandle(self._job_handle)) and closed
            self._job_handle = 0
        if self._process_handle:
            closed = bool(self._api.CloseHandle(self._process_handle)) and closed
            self._process_handle = 0
        return closed


def launch(argv: list[str], *, cwd: Path, environment: Mapping[str, str], with_stdin: bool) -> JobProcess:
    """Launch suspended, assign to a preconfigured Job, then resume."""
    import msvcrt
    import subprocess

    if not argv or any("\0" in part for part in argv) or "\0" in str(cwd):
        raise ValueError("invalid launch data")
    api = _kernel32()
    job = process_handle = thread_handle = 0
    stdin_read = stdin_write = stdout_read = stdout_write = stderr_read = stderr_write = 0
    streams: list[BinaryIO] = []
    attribute_initialized = False
    attribute_buffer = None
    try:
        job = api.CreateJobObjectW(None, None)
        if not job:
            raise OSError("job create failed")
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise OSError("job configure failed")

        security = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, False)
        with WINDOWS_PROCESS_CREATION_LOCK:
            for pipe_index in range(3):
                read_handle, write_handle = wintypes.HANDLE(), wintypes.HANDLE()
                if not api.CreatePipe(ctypes.byref(read_handle), ctypes.byref(write_handle), ctypes.byref(security), 0):
                    raise OSError("pipe create failed")
                if pipe_index == 0:
                    stdin_read, stdin_write = read_handle.value, write_handle.value
                elif pipe_index == 1:
                    stdout_read, stdout_write = read_handle.value, write_handle.value
                else:
                    stderr_read, stderr_write = read_handle.value, write_handle.value
            for parent_handle in (stdin_write, stdout_read, stderr_read):
                if not api.SetHandleInformation(parent_handle, HANDLE_FLAG_INHERIT, 0):
                    raise OSError("pipe configure failed")

            inherited = (wintypes.HANDLE * 3)(stdin_read, stdout_write, stderr_write)
            attribute_size = SIZE_T()
            api.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attribute_size))
            if not attribute_size.value:
                raise OSError("attribute sizing failed")
            attribute_buffer = ctypes.create_string_buffer(attribute_size.value)
            attribute_pointer = ctypes.cast(attribute_buffer, wintypes.LPVOID)
            if not api.InitializeProcThreadAttributeList(attribute_pointer, 1, 0, ctypes.byref(attribute_size)):
                raise OSError("attribute initialize failed")
            attribute_initialized = True
            if not api.UpdateProcThreadAttribute(
                attribute_pointer, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, ctypes.cast(inherited, wintypes.LPVOID),
                ctypes.sizeof(inherited), None, None,
            ):
                raise OSError("attribute update failed")

            startup = STARTUPINFOEXW()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = stdin_read
            startup.StartupInfo.hStdOutput = stdout_write
            startup.StartupInfo.hStdError = stderr_write
            startup.lpAttributeList = attribute_pointer
            process_info = PROCESS_INFORMATION()
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            environment_block = ctypes.create_unicode_buffer(build_windows_environment_block(environment))
            flags = CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT
            child_handles = (stdin_read, stdout_write, stderr_write)
            enabled_handles: list[int] = []
            failed_restores: list[int] = []
            created = False
            assigned = False
            try:
                for child_handle in child_handles:
                    if not api.SetHandleInformation(child_handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT):
                        raise OSError("child handle configure failed")
                    enabled_handles.append(child_handle)
                created = api.CreateProcessW(
                    None, command_line, None, None, True, flags, environment_block, str(cwd),
                    ctypes.cast(ctypes.byref(startup), ctypes.POINTER(STARTUPINFOW)), ctypes.byref(process_info),
                )
            finally:
                restored = True
                for child_handle in enabled_handles:
                    handle_restored = bool(api.SetHandleInformation(child_handle, HANDLE_FLAG_INHERIT, 0))
                    restored = handle_restored and restored
                    if not handle_restored:
                        failed_restores.append(child_handle)
            if not created:
                raise OSError("process create failed")
            process_handle, thread_handle = process_info.hProcess, process_info.hThread
            if not restored:
                assigned = bool(api.AssignProcessToJobObject(job, process_handle))
                for failed_handle in failed_restores:
                    api.CloseHandle(failed_handle)
                    if failed_handle == stdin_read:
                        stdin_read = 0
                    elif failed_handle == stdout_write:
                        stdout_write = 0
                    elif failed_handle == stderr_write:
                        stderr_write = 0
                raise OSError("child handle restore failed")
            if not assigned and not api.AssignProcessToJobObject(job, process_handle):
                raise OSError("job assignment failed")
            if api.ResumeThread(thread_handle) == 0xFFFFFFFF:
                raise OSError("thread resume failed")
            if not api.CloseHandle(thread_handle):
                raise OSError("thread handle close failed")
            thread_handle = 0
            for handle_name in ("stdin_read", "stdout_write", "stderr_write"):
                handle = locals()[handle_name]
                if not api.CloseHandle(handle):
                    raise OSError("child handle close failed")
                if handle_name == "stdin_read":
                    stdin_read = 0
                elif handle_name == "stdout_write":
                    stdout_write = 0
                else:
                    stderr_write = 0

        def stream_from_descriptor(descriptor: int, mode: str) -> BinaryIO:
            try:
                return io.FileIO(descriptor, mode, closefd=True)
            except Exception:
                os.close(descriptor)
                raise

        stdin_descriptor = msvcrt.open_osfhandle(stdin_write, os.O_WRONLY | os.O_BINARY)
        stdin_write = 0
        stdin_stream = stream_from_descriptor(stdin_descriptor, "wb")
        streams.append(stdin_stream)
        stdout_descriptor = msvcrt.open_osfhandle(stdout_read, os.O_RDONLY | os.O_BINARY)
        stdout_read = 0
        stdout_stream = stream_from_descriptor(stdout_descriptor, "rb")
        streams.append(stdout_stream)
        stderr_descriptor = msvcrt.open_osfhandle(stderr_read, os.O_RDONLY | os.O_BINARY)
        stderr_read = 0
        stderr_stream = stream_from_descriptor(stderr_descriptor, "rb")
        streams.append(stderr_stream)
        if not with_stdin:
            stdin_stream.close()
            streams.remove(stdin_stream)
            stdin_value = None
        else:
            stdin_value = stdin_stream
        return JobProcess(api, process_handle, job, process_info.dwProcessId, stdin_value, stdout_stream, stderr_stream)
    except Exception:
        if process_handle:
            api.TerminateProcess(process_handle, 1)
        if job:
            api.TerminateJobObject(job, 1)
        for stream in streams:
            try:
                stream.close()
            except OSError:
                pass
        for handle in (thread_handle, process_handle, stdin_read, stdin_write, stdout_read, stdout_write, stderr_read, stderr_write, job):
            if handle:
                api.CloseHandle(handle)
        raise
    finally:
        if attribute_initialized:
            api.DeleteProcThreadAttributeList(ctypes.cast(attribute_buffer, wintypes.LPVOID))
