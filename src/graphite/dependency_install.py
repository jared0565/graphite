"""Fail-closed primitives for consent-gated local dependency installation.

This module deliberately contains no activation policy or user interaction.  It
only prepares and validates immutable commands, files, environments, and bounded
process results for the higher-level activation service.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .probe_process import ProbeProcessError, ProbeProcessResult, run_bounded_process

TRUSTED_REGISTRY = "https://registry.npmjs.org/"
INSTALL_OUTPUT_LIMIT = 64 * 1024
MAX_CONTROL_FILE_BYTES = 8 * 1024 * 1024
MAX_TRUSTED_FILE_BYTES = 256 * 1024 * 1024
MAX_TRUSTED_LAUNCHER_LINKS = 8
MAX_TRUSTED_LINK_TARGET_BYTES = 4096
MAX_TRUSTED_ROUTE_COMPONENTS = 256
MAX_TRUSTED_EXECUTABLE_PREFIX_BYTES = 256
ACTIVATION_MAX_FILES = 100_000

_VERSION_RE = re.compile(
    r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_LOCALE_ENVIRONMENT = ("LANG", "LC_ALL")
_DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
_FORBIDDEN_MANIFEST_FIELDS = frozenset(
    {"workspaces", "resolutions", "overrides", "pnpm", "installConfig", "publishConfig"}
)
_URI_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://[^\s\"'<>}\]]+")
_LOCKFILE_LINE_LIMIT = 64 * 1024
_MAPPING_LINE_RE = re.compile(
    r"^(?:\"[^\"]+\"|'(?:[^']|'')+'|[A-Za-z0-9_./@+*^~<>=,!|()-]+):(?:\s+(.+)|\s*)$"
)
_SOURCE_FIELD_KEYS = frozenset({"resolved", "tarball", "fetch", "source", "path", "url"})
_LOCAL_TYPESCRIPT_SCRIPT = (
    "const p=require.resolve('typescript/package.json',{paths:[process.cwd()]});"
    "process.stdout.write(JSON.stringify({resolved:p}));"
)


class Manager(StrEnum):
    NPM = "npm"
    PNPM = "pnpm"
    YARN = "yarn"
    BUN = "bun"


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int


@dataclass(frozen=True)
class ManagerAdapter:
    manager: Manager
    lockfiles: tuple[str, ...]
    supported_majors: frozenset[int]
    command_builder: Callable[[str], tuple[str, ...]]
    unsafe_root_files: tuple[str, ...]
    automatic: bool = True

    def supports(self, version: Version | None) -> bool:
        return self.automatic and version is not None and version.major in self.supported_majors

    def argument_tail(self, registry: str) -> tuple[str, ...]:
        return self.command_builder(registry)


@dataclass(frozen=True)
class TrustedPathComponent:
    path: Path
    identity: tuple[int, int]
    mode: int
    owner: int
    link_target: str | None
    link_count: int | None


@dataclass(frozen=True)
class TrustedFile:
    path: Path
    identity: tuple[int, int]
    size: int
    mtime_ns: int
    ctime_ns: int | None
    sha256: str
    launcher_path: Path | None = None
    launcher_route: tuple[TrustedPathComponent, ...] = ()
    prefix: bytes = b""

    def __post_init__(self) -> None:
        if (
            (self.launcher_path is None) != (not self.launcher_route)
            or not isinstance(self.prefix, bytes)
            or len(self.prefix) > MAX_TRUSTED_EXECUTABLE_PREFIX_BYTES
        ):
            raise ValueError("trusted_file_launcher_invalid")

    @property
    def command_path(self) -> Path:
        return self.launcher_path if self.launcher_path is not None else self.path


@dataclass(frozen=True)
class FileSnapshot:
    relative_path: str
    identity: tuple[int, int]
    sha256: str


@dataclass(frozen=True)
class StepResult:
    ok: bool
    reason: str


@dataclass(frozen=True)
class VersionResult:
    ok: bool
    reason: str
    version: Version | None = None


@dataclass(frozen=True)
class TrustedCommand:
    """An argv prefix and every external file on which that prefix depends.

    The first reference is always the OS executable. Remaining references are
    pinned files consumed by it, including a POSIX manager's lexical script
    route or the Windows npm CLI.
    """

    argv: tuple[str, ...]
    references: tuple[TrustedFile, ...]

    def __post_init__(self) -> None:
        reference_arguments = tuple(str(reference.command_path) for reference in self.references)
        if (
            not self.argv
            or not self.references
            or self.argv != reference_arguments
            or (os.name == "nt" and self.references[0].path.suffix.lower() not in {".exe", ".com"})
        ):
            raise ValueError("trusted_command_invalid")


Runner = Callable[..., ProbeProcessResult]


def _npm_tail(registry: str) -> tuple[str, ...]:
    return (
        "install",
        "--save-dev",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        f"--registry={registry}",
        "typescript",
    )


def _pnpm_tail(registry: str) -> tuple[str, ...]:
    return (
        "add",
        "--save-dev",
        "--ignore-scripts",
        "--ignore-workspace-root-check",
        f"--registry={registry}",
        "typescript",
    )


def _yarn_tail(_registry: str) -> tuple[str, ...]:
    return ("add", "--dev", "--mode=skip-build", "typescript")


def _bun_tail(registry: str) -> tuple[str, ...]:
    return ("add", "--dev", "--ignore-scripts", "--registry", registry, "typescript")


_ADAPTERS = {
    Manager.NPM: ManagerAdapter(
        Manager.NPM,
        ("package-lock.json",),
        frozenset(range(8, 12)),
        _npm_tail,
        (".npmrc", "npm-shrinkwrap.json"),
    ),
    Manager.PNPM: ManagerAdapter(
        Manager.PNPM,
        ("pnpm-lock.yaml",),
        frozenset({11}),
        _pnpm_tail,
        (".npmrc", "pnpm-workspace.yaml", ".pnpmfile.cjs", ".pnpmfile.mjs"),
    ),
    Manager.YARN: ManagerAdapter(
        Manager.YARN,
        ("yarn.lock",),
        frozenset(),
        _yarn_tail,
        (".yarnrc.yml", ".yarnrc", ".yarn/plugins"),
        automatic=False,
    ),
    Manager.BUN: ManagerAdapter(
        Manager.BUN, ("bun.lock", "bun.lockb"), frozenset({1}), _bun_tail, (".npmrc", "bunfig.toml")
    ),
}


def adapter_for(manager: Manager) -> ManagerAdapter:
    return _ADAPTERS[Manager(manager)]


def parse_version(value: str | bytes) -> Version | None:
    try:
        text = value.decode("ascii") if isinstance(value, bytes) else value
    except (UnicodeDecodeError, AttributeError):
        return None
    match = _VERSION_RE.fullmatch(text.strip())
    if match is None:
        return None
    return Version(*(int(part) for part in match.groups()[:3]))


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _path_has_symlink(path: Path) -> bool:
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            details = current.lstat()
            if stat.S_ISLNK(details.st_mode) or _is_reparse(details):
                return True
    except OSError:
        return True
    return False


def _trusted_posix_owner(owner: int) -> bool:
    try:
        effective_uid = os.geteuid()
    except AttributeError:
        return False
    return owner in {0, effective_uid}


def _capture_posix_component(path: Path) -> TrustedPathComponent | None:
    try:
        lexical = Path(os.path.abspath(path))
        before = lexical.lstat()
        if _is_reparse(before):
            return None
        link_target = None
        if stat.S_ISLNK(before.st_mode):
            if before.st_size > MAX_TRUSTED_LINK_TARGET_BYTES:
                return None
            link_target = os.readlink(lexical)
            if len(os.fsencode(link_target)) > MAX_TRUSTED_LINK_TARGET_BYTES:
                return None
        after = lexical.lstat()
    except (OSError, RuntimeError, ValueError):
        return None
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
        "st_nlink",
        "st_uid",
    )
    if any(getattr(before, field, None) != getattr(after, field, None) for field in stable_fields):
        return None
    return TrustedPathComponent(
        lexical,
        (before.st_dev, before.st_ino),
        before.st_mode,
        before.st_uid,
        link_target,
        None if stat.S_ISDIR(before.st_mode) else before.st_nlink,
    )


def _trusted_posix_component(
    component: TrustedPathComponent, *, directory: bool
) -> bool:
    if not _trusted_posix_owner(component.owner):
        return False
    if component.link_target is not None:
        return component.link_count == 1
    if directory:
        if not stat.S_ISDIR(component.mode):
            return False
        writable = bool(component.mode & (stat.S_IWGRP | stat.S_IWOTH))
        return not writable or bool(component.mode & stat.S_ISVTX)
    return (
        stat.S_ISREG(component.mode)
        and component.link_count == 1
        and not component.mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def _trusted_posix_route(
    path: Path, root: Path
) -> tuple[Path, tuple[TrustedPathComponent, ...]] | None:
    try:
        lexical = Path(os.path.abspath(path))
        root_path = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not lexical.is_absolute() or _is_under(lexical, root_path):
        return None
    anchor = Path(lexical.anchor)
    anchor_component = _capture_posix_component(anchor)
    if anchor_component is None or not _trusted_posix_component(
        anchor_component, directory=True
    ):
        return None
    route = [anchor_component]
    pending = deque(lexical.parts[1:])
    current = anchor
    seen_links: set[tuple[int, int]] = set()
    followed_links = 0
    while pending:
        part = pending.popleft()
        if part in {"", "."}:
            continue
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        if _is_under(candidate, root_path):
            return None
        component = _capture_posix_component(candidate)
        if component is None or len(route) >= MAX_TRUSTED_ROUTE_COMPONENTS:
            return None
        route.append(component)
        if component.link_target is None:
            if not _trusted_posix_component(component, directory=bool(pending)):
                return None
            current = candidate
            continue
        followed_links += 1
        if (
            followed_links > MAX_TRUSTED_LAUNCHER_LINKS
            or component.identity in seen_links
            or not _trusted_posix_component(component, directory=False)
        ):
            return None
        seen_links.add(component.identity)
        target = Path(component.link_target)
        remaining = tuple(pending)
        if target.is_absolute():
            target_anchor = Path(target.anchor)
            target_anchor_component = _capture_posix_component(target_anchor)
            if (
                target_anchor_component is None
                or len(route) >= MAX_TRUSTED_ROUTE_COMPONENTS
                or not _trusted_posix_component(target_anchor_component, directory=True)
            ):
                return None
            route.append(target_anchor_component)
            current = target_anchor
            target_parts = target.parts[1:]
        else:
            current = candidate.parent
            target_parts = target.parts
        pending = deque((*target_parts, *remaining))
    if _is_under(current, root_path):
        return None
    return current, tuple(route)


def _trusted_posix_launcher(path: Path, root: Path) -> TrustedFile | None:
    try:
        lexical = Path(os.path.abspath(path))
        root_path = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    resolved_route = _trusted_posix_route(lexical, root_path)
    if resolved_route is None:
        return None
    current, route = resolved_route
    target_reference = _trusted_file(current, root_path, executable=True)
    if target_reference is None:
        return None
    final_component = route[-1]
    if (
        target_reference.path != current
        or target_reference.identity != final_component.identity
    ):
        return None
    return TrustedFile(
        path=target_reference.path,
        identity=target_reference.identity,
        size=target_reference.size,
        mtime_ns=target_reference.mtime_ns,
        ctime_ns=target_reference.ctime_ns,
        sha256=target_reference.sha256,
        launcher_path=lexical,
        launcher_route=route,
        prefix=target_reference.prefix,
    )


def _trusted_file(path: Path, root: Path, *, executable: bool) -> TrustedFile | None:
    if not path.is_absolute():
        return None
    try:
        lexical = path.absolute()
        if _path_has_symlink(lexical):
            return None
        canonical = lexical.resolve(strict=True)
        root_path = root.resolve(strict=True)
        initial = canonical.lstat()
    except (OSError, RuntimeError):
        return None
    if (
        _is_under(canonical, root_path)
        or not stat.S_ISREG(initial.st_mode)
        or _is_reparse(initial)
        or initial.st_size > MAX_TRUSTED_FILE_BYTES
    ):
        return None
    if executable:
        if os.name == "nt" and canonical.suffix.lower() not in {".exe", ".com"}:
            return None
        if os.name != "nt" and not os.access(canonical, os.X_OK):
            return None
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(canonical, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or getattr(before, "st_nlink", 1) != 1
            or before.st_size > MAX_TRUSTED_FILE_BYTES
        ):
            return None
        digest = hashlib.sha256()
        prefix = bytearray()
        total_read = 0
        while chunk := os.read(descriptor, 64 * 1024):
            total_read += len(chunk)
            if total_read > MAX_TRUSTED_FILE_BYTES:
                return None
            if len(prefix) < MAX_TRUSTED_EXECUTABLE_PREFIX_BYTES:
                remaining = MAX_TRUSTED_EXECUTABLE_PREFIX_BYTES - len(prefix)
                prefix.extend(chunk[:remaining])
            digest.update(chunk)
        after = os.fstat(descriptor)
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
        "st_nlink",
    )
    if os.name != "nt":
        stable_fields += ("st_ctime_ns",)
    if total_read != before.st_size or any(
        getattr(initial, field, 1) != getattr(before, field, 1) for field in stable_fields
    ) or any(
        getattr(before, field, 1) != getattr(after, field, 1) for field in stable_fields
    ):
        return None
    return TrustedFile(
        path=canonical,
        identity=(before.st_dev, before.st_ino),
        size=before.st_size,
        mtime_ns=before.st_mtime_ns,
        ctime_ns=None if os.name == "nt" else before.st_ctime_ns,
        sha256=digest.hexdigest(),
        prefix=bytes(prefix),
    )


def revalidate_trusted_file(reference: TrustedFile, root: Path, executable: bool) -> bool:
    if reference.launcher_path is not None:
        if os.name == "nt" or not executable:
            return False
        current = _trusted_posix_launcher(reference.launcher_path, root)
    else:
        current = _trusted_file(reference.path, root, executable=executable)
    return current == reference


def resolve_trusted_file(path: Path, root: Path, *, executable: bool) -> TrustedFile | None:
    """Resolve one caller-selected external file into an immutable identity reference."""
    return _trusted_file(path, root, executable=executable)


def _path_entries(path_source: str | Iterable[str | Path]) -> tuple[Path, ...]:
    raw_entries = path_source.split(os.pathsep) if isinstance(path_source, str) else path_source
    entries: list[Path] = []
    for raw in raw_entries:
        entry = Path(raw)
        if not entry.is_absolute():
            continue
        try:
            if _path_has_symlink(entry.absolute()):
                continue
            canonical = entry.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if canonical.is_dir():
            entries.append(canonical)
    return tuple(entries)


def _posix_manager_path_entries(
    path_source: str | Iterable[str | Path],
) -> tuple[Path, ...]:
    raw_entries = path_source.split(os.pathsep) if isinstance(path_source, str) else path_source
    entries: list[Path] = []
    for raw in raw_entries:
        entry = Path(raw)
        if entry.is_absolute():
            entries.append(Path(os.path.abspath(entry)))
    return tuple(entries)


def resolve_trusted_executable(
    name: str,
    root: Path,
    path_source: str | Iterable[str | Path],
    *,
    windows: bool | None = None,
) -> TrustedFile | None:
    use_windows_rules = os.name == "nt" if windows is None else windows
    supplied = Path(name)
    if supplied.name != name or supplied.is_absolute():
        return None
    if use_windows_rules:
        suffix = supplied.suffix.lower()
        names = (name,) if suffix in {".exe", ".com"} else (f"{name}.exe", f"{name}.com")
    else:
        names = (name,)
    posix_manager = (
        os.name != "nt"
        and not use_windows_rules
        and name in {manager.value for manager in Manager}
    )
    directories = (
        _posix_manager_path_entries(path_source)
        if posix_manager
        else _path_entries(path_source)
    )
    for directory in directories:
        for candidate_name in names:
            candidate = directory / candidate_name
            if posix_manager:
                reference = _trusted_posix_launcher(candidate, root)
            else:
                reference = _trusted_file(candidate, root, executable=not use_windows_rules)
            if reference is not None:
                return reference
    return None


def resolve_windows_npm_prefix(
    root: Path, path_source: str | Iterable[str | Path]
) -> TrustedCommand | None:
    for directory in _path_entries(path_source):
        node = _trusted_file(directory / "node.exe", root, executable=True)
        cli = _trusted_file(directory / "node_modules" / "npm" / "bin" / "npm-cli.js", root, executable=False)
        if node is not None and cli is not None:
            return TrustedCommand((str(node.path), str(cli.path)), (node, cli))
    return None


_NODE_SHEBANGS = frozenset(
    {
        b"#!/usr/bin/env node",
        b"#!/usr/bin/node",
        b"#!/bin/node",
    }
)
_POSIX_NATIVE_MAGICS = (
    b"\x7fELF",
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
)


def _posix_manager_execution_kind(reference: TrustedFile) -> str | None:
    prefix = reference.prefix
    if prefix.startswith(b"#!"):
        newline = prefix.find(b"\n")
        if newline < 0:
            return None
        shebang = prefix[:newline]
        if shebang.endswith(b"\r"):
            shebang = shebang[:-1]
        return "node" if shebang in _NODE_SHEBANGS else None
    return "native" if prefix.startswith(_POSIX_NATIVE_MAGICS) else None


def command_for(
    reference: TrustedFile, node: TrustedFile | None = None
) -> TrustedCommand | None:
    if os.name != "nt" and reference.launcher_path is not None:
        execution_kind = _posix_manager_execution_kind(reference)
        if execution_kind == "node":
            if node is None or node.launcher_path is not None:
                return None
            return TrustedCommand(
                (str(node.path), str(reference.command_path)), (node, reference)
            )
        if execution_kind != "native":
            return None
    return TrustedCommand((str(reference.command_path),), (reference,))


def _trusted_windows_system_environment() -> dict[str, str]:
    if os.name != "nt":
        return {}
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
        if length <= 0 or length >= len(buffer):
            raise OSError("windows_directory_unavailable")
        windows = Path(buffer.value).resolve(strict=True)
        system32 = (windows / "System32").resolve(strict=True)
        command = (system32 / "cmd.exe").resolve(strict=True)
        if not system32.is_dir() or not command.is_file():
            raise OSError("windows_system_paths_invalid")
    except (AttributeError, OSError, RuntimeError):
        raise ValueError("windows_system_paths_unavailable") from None
    return {
        "SYSTEMROOT": str(windows),
        "WINDIR": str(windows),
        "COMSPEC": str(command),
        "PATHEXT": ".COM;.EXE",
    }


def build_install_environment(
    manager: Manager,
    isolated_home: Path,
    executable_directories: Iterable[Path],
    registry: str,
    source: Mapping[str, str],
) -> dict[str, str]:
    base = isolated_home.resolve()
    environment = {name: source[name] for name in _LOCALE_ENVIRONMENT if name in source}
    trusted_directories: list[str] = []
    for directory in executable_directories:
        if not directory.is_absolute():
            continue
        try:
            canonical = directory.resolve(strict=True)
        except OSError:
            continue
        if canonical.is_dir() and str(canonical) not in trusted_directories:
            trusted_directories.append(str(canonical))
    if os.name == "nt":
        trusted_windows = _trusted_windows_system_environment()
        environment.update(trusted_windows)
        trusted_directories.append(str(Path(trusted_windows["SYSTEMROOT"]) / "System32"))
    else:
        trusted_directories.extend(("/usr/bin", "/bin"))
    environment["PATH"] = os.pathsep.join(trusted_directories)
    environment.update(
        {
            "HOME": str(base / "home"),
            "USERPROFILE": str(base / "home"),
            "XDG_CONFIG_HOME": str(base / "config"),
            "XDG_CACHE_HOME": str(base / "cache"),
            "TEMP": str(base / "tmp"),
            "TMP": str(base / "tmp"),
            "APPDATA": str(base / "appdata"),
            "LOCALAPPDATA": str(base / "localappdata"),
        }
    )
    selected = Manager(manager)
    if selected in {Manager.NPM, Manager.PNPM}:
        environment.update(
            {
                "NPM_CONFIG_USERCONFIG": str(base / "npm-config" / "user.npmrc"),
                "NPM_CONFIG_GLOBALCONFIG": str(base / "npm-config" / "global.npmrc"),
                "NPM_CONFIG_CACHE": str(base / "npm-cache"),
                "NPM_CONFIG_PREFIX": str(base / "npm-prefix"),
                "NPM_CONFIG_REGISTRY": registry,
                "NPM_CONFIG_IGNORE_SCRIPTS": "true",
                "NPM_CONFIG_AUDIT": "false",
                "NPM_CONFIG_FUND": "false",
                "npm_config_registry": registry,
                "npm_config_ignore_scripts": "true",
            }
        )
        if selected is Manager.PNPM:
            environment.update(
                {
                    "PNPM_HOME": str(base / "pnpm-home"),
                    "PNPM_STORE_DIR": str(base / "pnpm-store"),
                    "PNPM_CONFIG_DIR": str(base / "pnpm-config"),
                    "npm_config_store_dir": str(base / "pnpm-store"),
                }
            )
    elif selected is Manager.YARN:
        environment.update(
            {
                "YARN_NPM_REGISTRY_SERVER": registry,
                "YARN_ENABLE_SCRIPTS": "false",
                "YARN_ENABLE_TELEMETRY": "0",
                "YARN_GLOBAL_FOLDER": str(base / "yarn-global"),
            }
        )
    else:
        environment["BUN_INSTALL_CACHE_DIR"] = str(base / "bun-cache")
    return environment


def _prepare_isolated_install_home(root: Path, isolated_home: Path, manager: Manager) -> bool:
    if not isolated_home.is_absolute():
        return False
    try:
        canonical_root = root.resolve(strict=True)
        lexical_base = isolated_home.absolute()
        if _is_under(lexical_base, canonical_root):
            return False
        parent = lexical_base.parent.resolve(strict=True)
        if _path_has_symlink(parent) or _is_under(parent, canonical_root):
            return False
        if lexical_base.exists() or lexical_base.is_symlink():
            details = lexical_base.lstat()
            if stat.S_ISLNK(details.st_mode) or _is_reparse(details) or not stat.S_ISDIR(details.st_mode):
                return False
        else:
            lexical_base.mkdir(mode=0o700)
        canonical_base = lexical_base.resolve(strict=True)
        if canonical_base != lexical_base or _is_under(canonical_base, canonical_root):
            return False
        directory_names = {
            "home",
            "config",
            "cache",
            "tmp",
            "appdata",
            "localappdata",
        }
        selected = Manager(manager)
        if selected in {Manager.NPM, Manager.PNPM}:
            directory_names.update({"npm-config", "npm-cache", "npm-prefix"})
        if selected is Manager.PNPM:
            directory_names.update({"pnpm-home", "pnpm-store", "pnpm-config"})
        elif selected is Manager.YARN:
            directory_names.add("yarn-global")
        elif selected is Manager.BUN:
            directory_names.add("bun-cache")
        for name in directory_names:
            directory = canonical_base / name
            directory.mkdir(mode=0o700, exist_ok=True)
            details = directory.lstat()
            if (
                directory.resolve(strict=True) != directory
                or not _is_under(directory, canonical_base)
                or stat.S_ISLNK(details.st_mode)
                or _is_reparse(details)
                or not stat.S_ISDIR(details.st_mode)
            ):
                return False
        if selected in {Manager.NPM, Manager.PNPM}:
            for name in ("user.npmrc", "global.npmrc"):
                config = canonical_base / "npm-config" / name
                if config.exists() or config.is_symlink():
                    existing = config.lstat()
                    if (
                        stat.S_ISLNK(existing.st_mode)
                        or _is_reparse(existing)
                        or not stat.S_ISREG(existing.st_mode)
                        or getattr(existing, "st_nlink", 1) != 1
                    ):
                        return False
                flags = (
                    os.O_RDWR
                    | os.O_CREAT
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                descriptor = os.open(config, flags, 0o600)
                try:
                    details = os.fstat(descriptor)
                    if not stat.S_ISREG(details.st_mode) or getattr(details, "st_nlink", 1) != 1:
                        return False
                    os.ftruncate(descriptor, 0)
                finally:
                    os.close(descriptor)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def snapshot_control_file(root: Path, relative_path: str) -> FileSnapshot:
    if (
        not relative_path
        or relative_path in {".", ".."}
        or "/" in relative_path
        or "\\" in relative_path
        or Path(relative_path).is_absolute()
    ):
        raise ValueError("control_file_invalid")
    try:
        canonical_root = root.resolve(strict=True)
        path = canonical_root / relative_path
        initial = path.lstat()
        if stat.S_ISLNK(initial.st_mode) or _is_reparse(initial) or not stat.S_ISREG(initial.st_mode):
            raise ValueError("control_file_invalid")
        canonical = path.resolve(strict=True)
        if not _is_under(canonical, canonical_root) or canonical != path:
            raise ValueError("control_file_invalid")
        if initial.st_size > MAX_CONTROL_FILE_BYTES:
            raise ValueError("control_file_invalid")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            content = os.read(descriptor, MAX_CONTROL_FILE_BYTES + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except ValueError:
        raise
    except (OSError, RuntimeError):
        raise ValueError("control_file_invalid") from None
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if (
        len(content) > MAX_CONTROL_FILE_BYTES
        or len(content) != before.st_size
        or any(getattr(initial, field) != getattr(before, field) for field in stable_fields)
        or any(getattr(before, field) != getattr(after, field) for field in stable_fields)
    ):
        raise ValueError("control_file_changed")
    return FileSnapshot(relative_path, (before.st_dev, before.st_ino), hashlib.sha256(content).hexdigest())


def _dependency_spec_is_safe(specification: str) -> bool:
    value = specification.strip()
    lower = value.lower()
    if not value or value != specification:
        return False
    if lower.startswith(("file:", "link:", "git:", "git+", "ssh:", "http:", "https:", "workspace:")):
        return False
    if lower.endswith((".tgz", ".tar.gz")):
        return False
    if lower.startswith("git@") or any(separator in value for separator in ("/", "\\", ":")):
        return False
    if value.startswith("."):
        return False
    return True


def _is_canonical_registry_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.netloc == "registry.npmjs.org"
        and parsed.path.startswith("/")
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
    )


def _json_source_fields_are_trusted(value: Any) -> bool:
    if isinstance(value, list):
        return all(_json_source_fields_are_trusted(item) for item in value)
    if not isinstance(value, dict):
        return True
    for key, item in value.items():
        lowered = key.lower()
        if lowered in _SOURCE_FIELD_KEYS:
            if not isinstance(item, str) or not _is_canonical_registry_url(item):
                return False
        elif lowered == "resolution" and isinstance(item, dict):
            if any(nested.lower() not in {"integrity", "tarball"} for nested in item):
                return False
        if not _json_source_fields_are_trusted(item):
            return False
    return True


def _text_source_fields_are_trusted(text: str) -> bool:
    for line in text.splitlines():
        content = line.strip()
        mapping = re.fullmatch(
            r"(?P<key>\"[^\"]*\"|'(?:[^']|'')*'|[A-Za-z][A-Za-z0-9_-]*):\s+(?P<value>.+)",
            content,
        )
        if mapping is not None:
            key = _normalize_yaml_mapping_key(mapping.group("key"))
            if key is None:
                return False
            if key.lower() not in _SOURCE_FIELD_KEYS:
                continue
            source = mapping.group("value").strip()
        else:
            classic = re.fullmatch(r"(?i)(resolved|tarball|fetch|source|path|url)\s+(.+)", content)
            if classic is None:
                continue
            source = classic.group(2).strip()
        if source.startswith(("'", '"')) and source.endswith(source[0]) and len(source) >= 2:
            source = source[1:-1]
        if not _is_canonical_registry_url(source):
            return False
    return True


def _normalize_yaml_mapping_key(value: str) -> str | None:
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"') or '"' in value[1:-1]:
            return None
        return value[1:-1]
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            return None
        inner = value[1:-1]
        result: list[str] = []
        index = 0
        while index < len(inner):
            if inner[index] == "'":
                if index + 1 >= len(inner) or inner[index + 1] != "'":
                    return None
                result.append("'")
                index += 2
            else:
                result.append(inner[index])
                index += 1
        return "".join(result)
    return value if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value) else None


def _normalize_text_lockfile_escapes(text: str) -> str | None:
    """Decode a bounded subset of JSON escapes exactly once, rejecting ambiguity."""
    output: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character != "\\":
            output.append(character)
            index += 1
            continue
        if index + 1 >= len(text):
            return None
        escape = text[index + 1]
        if escape in {"/", "\\"}:
            output.append(escape)
            index += 2
            continue
        if escape not in {"u", "U"} or index + 6 > len(text):
            return None
        digits = text[index + 2 : index + 6]
        if re.fullmatch(r"[0-9A-Fa-f]{4}", digits) is None:
            return None
        decoded = chr(int(digits, 16))
        if ord(decoded) < 32 or 0xD800 <= ord(decoded) <= 0xDFFF:
            return None
        output.append(decoded)
        index += 6
    normalized = "".join(output)
    if re.search(r"\\[uU][0-9A-Fa-f]{4}", normalized):
        return None
    return normalized


def _line_has_balanced_structures(content: str) -> bool:
    quote: str | None = None
    brackets: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for character in content:
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character in "([{":
            brackets.append(character)
        elif character in ")]}":
            if not brackets or brackets.pop() != pairs[character]:
                return False
    return quote is None and not brackets


def _mapping_line(content: str) -> tuple[bool, bool]:
    match = _MAPPING_LINE_RE.fullmatch(content)
    if match is None:
        return False, False
    value = match.group(1)
    if value is not None and not _yaml_scalar_is_valid(value):
        return False, False
    return True, value is None


def _yaml_scalar_remainder_is_valid(remainder: str) -> bool:
    return not remainder or (remainder[0].isspace() and remainder.lstrip().startswith("#"))


def _reject_json_constant(_constant: str) -> None:
    raise ValueError("invalid_json_constant")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _parse_bounded_json_int(value: str) -> int:
    if len(value.lstrip("-")) > 1024:
        raise ValueError("json_integer_too_long")
    return int(value)


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("json_float_not_finite")
    return parsed


def _strict_json_loads(value: bytes | str) -> Any:
    return json.loads(
        value,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_int=_parse_bounded_json_int,
        parse_float=_parse_finite_json_float,
    )


def _yaml_scalar_is_valid(value: str) -> bool:
    if not value:
        return False
    if value[0] in {"'", '"'}:
        closing = value.find(value[0], 1)
        return closing >= 1 and _yaml_scalar_remainder_is_valid(value[closing + 1 :])
    if value[0] in "[{":
        pairs = {"]": "[", "}": "{"}
        stack = [value[0]]
        quote: str | None = None
        for index, character in enumerate(value[1:], start=1):
            if quote is not None:
                if character == quote:
                    quote = None
                continue
            if character in {"'", '"'}:
                quote = character
            elif character in "[{":
                stack.append(character)
            elif character in "]}":
                if not stack or stack.pop() != pairs[character]:
                    return False
                if not stack:
                    flow_text = value[: index + 1]
                    try:
                        parsed_flow = _strict_json_loads(flow_text)
                    except (ValueError, RecursionError):
                        return False
                    expected_type = dict if value[0] == "{" else list
                    return (
                        isinstance(parsed_flow, expected_type)
                        and _json_source_fields_are_trusted(parsed_flow)
                        and _yaml_scalar_remainder_is_valid(value[index + 1 :])
                    )
        return False
    plain_value = value
    for index, character in enumerate(value):
        if character == "#" and (index == 0 or value[index - 1].isspace()):
            plain_value = value[:index].rstrip()
            break
    if (
        not plain_value
        or plain_value.endswith(":")
        or any(character in plain_value for character in "[]{}")
    ):
        return False
    return not any(
        character == ":" and index + 1 < len(plain_value) and plain_value[index + 1].isspace()
        for index, character in enumerate(plain_value)
    )


def _validate_mapping_lockfile(lines: list[str], *, berry: bool) -> bool:
    previous_indent = 0
    previous_opens = False
    saw_significant = False
    saw_pnpm_section = False
    saw_metadata_version = False
    saw_package_entry = False
    saw_package_version = False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line[indent:].rstrip()
        if indent % 2 or indent > 40 or not _line_has_balanced_structures(content):
            return False
        if saw_significant and indent > previous_indent:
            if indent != previous_indent + 2 or not previous_opens:
                return False
        valid, opens = _mapping_line(content)
        if not valid:
            return False
        if indent == 0 and content.split(":", 1)[0] in {"importers", "packages", "snapshots"}:
            saw_pnpm_section = True
        if berry:
            if indent == 2 and re.fullmatch(r"version:\s*\d+(?:\.\d+)*", content):
                if not saw_package_entry:
                    saw_metadata_version = True
                else:
                    saw_package_version = True
            if indent == 0 and content != "__metadata:":
                saw_package_entry = True
        previous_indent = indent
        previous_opens = opens
        saw_significant = True
    if berry:
        return saw_metadata_version and saw_package_entry and saw_package_version
    return saw_pnpm_section


def _validate_yarn_classic(lines: list[str]) -> bool:
    previous_indent = 0
    previous_opens = False
    saw_significant = False
    saw_entry = False
    current_entry_has_version = False
    for line in lines[1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line[indent:].rstrip()
        if indent not in {0, 2, 4} or not _line_has_balanced_structures(content):
            return False
        if saw_significant and indent > previous_indent:
            if indent != previous_indent + 2 or not previous_opens:
                return False
        opens = False
        if indent == 0:
            if saw_entry and not current_entry_has_version:
                return False
            valid, opens = _mapping_line(content)
            if not valid or not opens:
                return False
            saw_entry = True
            current_entry_has_version = False
        elif indent == 2:
            block_match = re.fullmatch(r"(?:dependencies|optionalDependencies|peerDependencies):", content)
            field_match = re.fullmatch(r"(?:version|resolved|integrity|uid)\s+\S.*", content)
            if block_match is None and field_match is None:
                return False
            opens = block_match is not None
            if content.startswith("version "):
                current_entry_has_version = True
        elif re.fullmatch(r"(?:\"[^\"]+\"|'[^']+'|[^\s:]+)\s+\S.*", content) is None:
            return False
        previous_indent = indent
        previous_opens = opens
        saw_significant = True
    return saw_entry and current_entry_has_version


def _normalized_lockfile_text(lockfile_bytes: bytes) -> str | None:
    """Return decoded text only for a conservatively recognized lockfile format."""
    try:
        text = lockfile_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(ord(character) < 32 and character not in "\r\n" for character in text):
        return None
    text = text.replace("\r\n", "\n")
    if "\r" in text:
        return None
    stripped = text.lstrip("\ufeff \n")
    if not stripped:
        return None
    if stripped.startswith("{"):
        try:
            parsed = _strict_json_loads(stripped)
        except (UnicodeDecodeError, ValueError, RecursionError):
            return None
        if not isinstance(parsed, dict):
            return None
        if not _json_source_fields_are_trusted(parsed):
            return None
        lockfile_version = parsed.get("lockfileVersion")
        if isinstance(lockfile_version, bool) or not (
            isinstance(lockfile_version, int)
            or (isinstance(lockfile_version, str) and re.fullmatch(r"\d+(?:\.\d+)?", lockfile_version))
        ):
            return None
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    stripped = _normalize_text_lockfile_escapes(stripped)
    if stripped is None:
        return None
    lines = stripped.splitlines()
    if any(len(line) > _LOCKFILE_LINE_LIMIT for line in lines):
        return None
    if lines[0].startswith("lockfileVersion:"):
        if not re.fullmatch(r"lockfileVersion:\s*['\"]?\d+(?:\.\d+)?['\"]?\s*", lines[0]):
            return None
        return stripped if _validate_mapping_lockfile(lines, berry=False) else None
    if lines[0].strip() == "# yarn lockfile v1":
        return stripped if _validate_yarn_classic(lines) else None
    first_content_lines = [line.strip() for line in lines[:10] if line.strip() and not line.lstrip().startswith("#")]
    if first_content_lines and first_content_lines[0] == "__metadata:":
        return stripped if _validate_mapping_lockfile(lines, berry=True) else None
    return None


def _lockfile_uses_trusted_sources(lockfile_bytes: bytes) -> bool:
    normalized = _normalized_lockfile_text(lockfile_bytes)
    if normalized is None:
        return False
    normalized = normalized.replace("\\/", "/")
    lower = normalized.lower()
    if not _text_source_fields_are_trusted(normalized):
        return False
    if any(
        marker in lower
        for marker in ("git+", "git://", "git@", "ssh:", "file:", "link:", "local:")
    ):
        return False
    if re.search(r"[\"'](?:\.\.?[/\\]|[/\\]|[A-Za-z]:[/\\])", normalized) or re.search(
        r"(?m)^\s*[A-Za-z][\w-]*:\s*(?:\.\.?[/\\]|[/\\]{1,2}|[A-Za-z]:[/\\])",
        normalized,
    ):
        return False
    uri_matches = tuple(_URI_RE.finditer(normalized))
    for match in uri_matches:
        try:
            parsed = urlsplit(match.group())
        except ValueError:
            return False
        if (
            match.group(1).lower() != "https"
            or parsed.scheme.lower() != "https"
            or parsed.hostname != "registry.npmjs.org"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            return False
    without_valid_url_shapes = _URI_RE.sub("", normalized).lower()
    if "http:" in without_valid_url_shapes or "https:" in without_valid_url_shapes:
        return False
    return True


def control_files_use_trusted_sources(manifest_bytes: bytes, lockfile_bytes: bytes) -> bool:
    if len(manifest_bytes) > MAX_CONTROL_FILE_BYTES or len(lockfile_bytes) > MAX_CONTROL_FILE_BYTES:
        return False
    try:
        manifest = _strict_json_loads(manifest_bytes)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    if not isinstance(manifest, dict) or _FORBIDDEN_MANIFEST_FIELDS.intersection(manifest):
        return False
    for field in _DEPENDENCY_FIELDS:
        dependencies = manifest.get(field, {})
        if not isinstance(dependencies, dict):
            return False
        if any(not isinstance(name, str) or not isinstance(spec, str) or not _dependency_spec_is_safe(spec) for name, spec in dependencies.items()):
            return False
    return _lockfile_uses_trusted_sources(lockfile_bytes)


def _minimal_node_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    ambient = os.environ if source is None else source
    environment = {name: ambient[name] for name in _LOCALE_ENVIRONMENT if name in ambient}
    if os.name == "nt":
        trusted_windows = _trusted_windows_system_environment()
        environment.update(trusted_windows)
        environment["PATH"] = str(Path(trusted_windows["SYSTEMROOT"]) / "System32")
    else:
        environment["PATH"] = "/usr/bin:/bin"
    return environment


def _command_revalidation_reason(command: TrustedCommand, root: Path) -> str | None:
    for index, reference in enumerate(command.references):
        executable = index == 0 or reference.launcher_path is not None
        if not revalidate_trusted_file(reference, root, executable=executable):
            return "executable_changed" if index == 0 else "command_changed"
    return None


def run_validator(
    root: Path,
    node: TrustedFile,
    validator: TrustedFile,
    timeout: float,
    runner: Runner = run_bounded_process,
) -> StepResult:
    if not math.isfinite(timeout) or timeout <= 0:
        return StepResult(False, "validator_rejected")
    if not revalidate_trusted_file(node, root, executable=True):
        return StepResult(False, "executable_changed")
    if not revalidate_trusted_file(validator, root, executable=False):
        return StepResult(False, "validator_changed")
    try:
        result = runner(
            [str(node.path), str(validator.path), "typescript"],
            cwd=root,
            stdin=None,
            timeout_seconds=timeout,
            max_output_bytes=INSTALL_OUTPUT_LIMIT,
            check=False,
            environment=_minimal_node_environment(),
        )
    except ProbeProcessError as error:
        return StepResult(False, "validator_timeout" if error.code == "timeout" else "validator_rejected")
    except Exception:
        return StepResult(False, "validator_rejected")
    return StepResult(result.returncode == 0, "validated" if result.returncode == 0 else "validator_rejected")


def run_manager_version(
    command: TrustedCommand,
    root: Path,
    timeout: float,
    runner: Runner = run_bounded_process,
) -> VersionResult:
    if not math.isfinite(timeout) or timeout <= 0:
        return VersionResult(False, "manager_unavailable")
    if _command_revalidation_reason(command, root) is not None:
        return VersionResult(False, "manager_unavailable")
    try:
        result = runner(
            [*command.argv, "--version"],
            cwd=root,
            stdin=None,
            timeout_seconds=timeout,
            max_output_bytes=64,
            check=False,
            environment=_minimal_node_environment(),
        )
    except ProbeProcessError as error:
        reason = "manager_timeout" if error.code == "timeout" else "manager_unavailable"
        return VersionResult(False, reason)
    except Exception:
        return VersionResult(False, "manager_unavailable")
    if result.returncode != 0:
        return VersionResult(False, "manager_unavailable")
    version = parse_version(result.stdout)
    if version is None:
        return VersionResult(False, "manager_version_invalid")
    return VersionResult(True, "manager_versioned", version)


def run_install(
    root: Path,
    command: TrustedCommand,
    adapter: ManagerAdapter,
    registry: str,
    isolated_home: Path,
    timeout: float,
    runner: Runner = run_bounded_process,
) -> StepResult:
    if not math.isfinite(timeout) or timeout <= 0:
        return StepResult(False, "install_failed")
    if registry != TRUSTED_REGISTRY:
        return StepResult(False, "install_failed")
    try:
        canonical_adapter = adapter_for(adapter.manager)
        if adapter is not canonical_adapter or not canonical_adapter.automatic:
            return StepResult(False, "install_failed")
    except (KeyError, ValueError):
        return StepResult(False, "install_failed")
    if not _prepare_isolated_install_home(root, isolated_home, adapter.manager):
        return StepResult(False, "install_failed")
    path_reference = (
        command.references[-1]
        if os.name != "nt" and command.references[-1].launcher_path is not None
        else command.references[0]
    )
    directories = (path_reference.command_path.parent,)
    try:
        environment = build_install_environment(
            adapter.manager, isolated_home, directories, TRUSTED_REGISTRY, os.environ
        )
    except (OSError, RuntimeError, ValueError):
        return StepResult(False, "install_failed")
    provenance_reason = _command_revalidation_reason(command, root)
    if provenance_reason is not None:
        return StepResult(False, provenance_reason)
    try:
        result = runner(
            [*command.argv, *adapter.argument_tail(TRUSTED_REGISTRY)],
            cwd=root,
            stdin=None,
            timeout_seconds=timeout,
            max_output_bytes=INSTALL_OUTPUT_LIMIT,
            check=False,
            environment=environment,
        )
    except ProbeProcessError as error:
        return StepResult(False, "install_timeout" if error.code == "timeout" else "install_failed")
    except Exception:
        return StepResult(False, "install_failed")
    return StepResult(result.returncode == 0, "installed_command" if result.returncode == 0 else "install_failed")


def probe_local_typescript(
    root: Path,
    node: TrustedFile,
    timeout: float,
    runner: Runner = run_bounded_process,
) -> bool:
    if not math.isfinite(timeout) or timeout <= 0:
        return False
    if not revalidate_trusted_file(node, root, executable=True):
        return False
    try:
        result = runner(
            [str(node.path), "-e", _LOCAL_TYPESCRIPT_SCRIPT],
            cwd=root,
            stdin=None,
            timeout_seconds=timeout,
            max_output_bytes=4096,
            check=False,
            environment=_minimal_node_environment(),
        )
        payload: Any = json.loads(result.stdout)
        resolved_value = payload.get("resolved") if isinstance(payload, dict) else None
        if result.returncode != 0 or not isinstance(resolved_value, str):
            return False
        canonical_root = root.resolve(strict=True)
        package_root = canonical_root / "node_modules" / "typescript"
        candidate = Path(resolved_value)
        if not candidate.is_absolute() or _path_has_symlink(candidate.absolute()):
            return False
        canonical_candidate = candidate.resolve(strict=True)
        details = candidate.lstat()
        return (
            candidate.absolute() == canonical_candidate
            and _is_under(canonical_candidate, package_root)
            and stat.S_ISREG(details.st_mode)
            and not stat.S_ISLNK(details.st_mode)
            and not _is_reparse(details)
        )
    except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError, ProbeProcessError, RecursionError, ValueError):
        return False
    except Exception:
        return False
