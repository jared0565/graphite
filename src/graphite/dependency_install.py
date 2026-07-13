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
ACTIVATION_MAX_FILES = 100_000

_VERSION_RE = re.compile(
    r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_ESSENTIAL_ENVIRONMENT = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL")
_DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
_FORBIDDEN_MANIFEST_FIELDS = frozenset(
    {"workspaces", "resolutions", "overrides", "pnpm", "installConfig", "publishConfig"}
)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
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
class TrustedFile:
    path: Path
    identity: tuple[int, int]


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

    The first reference is always executable.  Remaining references are data
    files consumed by that executable (for example npm-cli.js).
    """

    argv: tuple[str, ...]
    references: tuple[TrustedFile, ...]

    def __post_init__(self) -> None:
        reference_arguments = tuple(str(reference.path) for reference in self.references)
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
        Manager.NPM, ("package-lock.json",), frozenset(range(8, 12)), _npm_tail, (".npmrc",)
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


def _trusted_file(path: Path, root: Path, *, executable: bool) -> TrustedFile | None:
    if not path.is_absolute():
        return None
    try:
        lexical = path.absolute()
        if _path_has_symlink(lexical):
            return None
        canonical = lexical.resolve(strict=True)
        root_path = root.resolve(strict=True)
        details = canonical.stat()
    except (OSError, RuntimeError):
        return None
    if _is_under(canonical, root_path) or not stat.S_ISREG(details.st_mode) or _is_reparse(details):
        return None
    if executable:
        if os.name == "nt" and canonical.suffix.lower() not in {".exe", ".com"}:
            return None
        if os.name != "nt" and not os.access(canonical, os.X_OK):
            return None
    return TrustedFile(canonical, (details.st_dev, details.st_ino))


def revalidate_trusted_file(reference: TrustedFile, root: Path, executable: bool) -> bool:
    current = _trusted_file(reference.path, root, executable=executable)
    return current is not None and current.path == reference.path and current.identity == reference.identity


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
    for directory in _path_entries(path_source):
        for candidate_name in names:
            candidate = directory / candidate_name
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


def command_for(reference: TrustedFile) -> TrustedCommand:
    return TrustedCommand((str(reference.path),), (reference,))


def build_install_environment(
    manager: Manager,
    isolated_home: Path,
    executable_directories: Iterable[Path],
    registry: str,
    source: Mapping[str, str],
) -> dict[str, str]:
    base = isolated_home.resolve()
    environment = {name: source[name] for name in _ESSENTIAL_ENVIRONMENT if name in source}
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
        windows_root = source.get("SYSTEMROOT") or source.get("WINDIR") or r"C:\Windows"
        trusted_directories.append(str(Path(windows_root) / "System32"))
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
        }
    )
    selected = Manager(manager)
    if selected in {Manager.NPM, Manager.PNPM}:
        environment.update(
            {
                "NPM_CONFIG_USERCONFIG": str(base / "npmrc"),
                "NPM_CONFIG_REGISTRY": registry,
                "NPM_CONFIG_IGNORE_SCRIPTS": "true",
                "NPM_CONFIG_AUDIT": "false",
                "NPM_CONFIG_FUND": "false",
                "npm_config_registry": registry,
                "npm_config_ignore_scripts": "true",
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
    if lower.startswith("git@") or any(separator in value for separator in ("/", "\\", ":")):
        return False
    if value.startswith("."):
        return False
    return True


def _normalized_lockfile_text(lockfile_bytes: bytes) -> str | None:
    """Return decoded text only for a conservatively recognized lockfile format."""
    try:
        text = lockfile_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\x00" in text:
        return None
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped:
        return ""
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, RecursionError):
            return None
        if not isinstance(parsed, dict):
            return None
        lockfile_version = parsed.get("lockfileVersion")
        if isinstance(lockfile_version, bool) or not (
            isinstance(lockfile_version, int)
            or (isinstance(lockfile_version, str) and re.fullmatch(r"\d+(?:\.\d+)?", lockfile_version))
        ):
            return None
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    lines = stripped.splitlines()
    if lines[0].startswith("lockfileVersion:"):
        if not re.fullmatch(r"lockfileVersion:\s*['\"]?\d+(?:\.\d+)?['\"]?\s*", lines[0]):
            return None
        if len(lines) < 2 or not any(
            line.lstrip().startswith(("importers:", "packages:", "snapshots:")) for line in lines[1:]
        ):
            return None
        return stripped
    if lines[0].strip() == "# yarn lockfile v1":
        entries = [line for line in lines[1:] if line.strip()]
        if not any(not line[0].isspace() and line.rstrip().endswith(":") for line in entries):
            return None
        if not any(line[0].isspace() and line.lstrip().startswith("version ") for line in entries):
            return None
        return stripped
    first_content_lines = [line.strip() for line in lines[:10] if line.strip() and not line.lstrip().startswith("#")]
    if first_content_lines and first_content_lines[0] == "__metadata:":
        return stripped if any(re.fullmatch(r"\s+version:\s*\d+\s*", line) for line in lines[1:]) else None
    return None


def _lockfile_uses_trusted_sources(lockfile_bytes: bytes) -> bool:
    normalized = _normalized_lockfile_text(lockfile_bytes)
    if normalized is None:
        return False
    normalized = normalized.replace("\\/", "/")
    lower = normalized.lower()
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
    url_matches = tuple(_URL_RE.finditer(normalized))
    for match in url_matches:
        try:
            parsed = urlsplit(match.group())
        except ValueError:
            return False
        if (
            parsed.scheme.lower() != "https"
            or parsed.hostname != "registry.npmjs.org"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            return False
    without_valid_url_shapes = _URL_RE.sub("", normalized).lower()
    if "http:" in without_valid_url_shapes or "https:" in without_valid_url_shapes:
        return False
    return True


def control_files_use_trusted_sources(manifest_bytes: bytes, lockfile_bytes: bytes) -> bool:
    if len(manifest_bytes) > MAX_CONTROL_FILE_BYTES or len(lockfile_bytes) > MAX_CONTROL_FILE_BYTES:
        return False
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
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
    environment = {name: ambient[name] for name in _ESSENTIAL_ENVIRONMENT if name in ambient}
    if os.name == "nt":
        windows_root = ambient.get("SYSTEMROOT") or ambient.get("WINDIR") or r"C:\Windows"
        environment["PATH"] = str(Path(windows_root) / "System32")
    else:
        environment["PATH"] = "/usr/bin:/bin"
    return environment


def _command_revalidation_reason(command: TrustedCommand, root: Path) -> str | None:
    for index, reference in enumerate(command.references):
        if not revalidate_trusted_file(reference, root, executable=index == 0):
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
    provenance_reason = _command_revalidation_reason(command, root)
    if provenance_reason is not None:
        return StepResult(False, provenance_reason)
    directories = (command.references[0].path.parent,)
    environment = build_install_environment(
        adapter.manager, isolated_home, directories, TRUSTED_REGISTRY, os.environ
    )
    try:
        result = runner(
            [*command.argv, *adapter.argument_tail(TRUSTED_REGISTRY)],
            cwd=root,
            stdin=None,
            timeout_seconds=timeout,
            max_output_bytes=INSTALL_OUTPUT_LIMIT,
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
