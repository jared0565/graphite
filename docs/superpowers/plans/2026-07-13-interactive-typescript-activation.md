# Interactive TypeScript Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a consent-gated, fail-closed TypeScript activation step to interactive `graphite init` and `graphite bootstrap` without granting installation authority to any other Graphite path.

**Architecture:** A new `typescript_activation` policy service performs bounded evidence detection and coordinates a separate `dependency_install` boundary that owns executable provenance, package-manager adapters, validator execution, registry isolation, and post-install verification. The CLI calls the service only after onboarding files are written and before the existing build, while the shared bounded process transport gains an exact-environment option so no ambient credential is inherited.

**Tech Stack:** Python 3.11+, `argparse`, frozen dataclasses and `StrEnum`, existing native-contained subprocess transport, pytest, Node.js/package-manager CLIs supplied by the host (no new Python or npm dependency).

---

## Scope and locked file structure

- Create `src/graphite/dependency_install.py`: safe package-manager adapter definitions, external executable provenance, isolated install environment, validator execution, manager execution, bounded local-TypeScript probe, and file snapshots.
- Create `src/graphite/typescript_activation.py`: TypeScript evidence/manager detection, prompt and outcome policy, process-local root locking, shared deadline, orchestration, and stable result serialization.
- Modify `src/graphite/probe_process.py`: permit a caller to supply a complete exact environment; never merge it with ambient variables.
- Modify `src/graphite/cli.py`: invoke activation from `init` and `bootstrap`, add `bootstrap --yes`, emit stable text/JSON, and combine exit status.
- Create `tests/test_typescript_activation.py`: pure policy, adapter, orchestration, and adversarial tests using fake runners and temporary files only.
- Modify `tests/test_doctor.py`: regression tests for the process transport's exact-environment boundary.
- Modify `tests/test_init.py` and `tests/test_bootstrap.py`: CLI integration, no-prompt modes, ordering, preservation, JSON, and exit status.
- Modify `README.md`, `CONTRIBUTING.md`, `ARCHITECTURE.md`, and `tests/test_documentation.py`: operator contract and documentation guardrails.
- Modify `docs/superpowers/specs/2026-07-13-interactive-typescript-activation-design.md`: mark the approved design implemented only after acceptance passes.

The initial automatic support matrix is intentionally conservative:

| Manager | Evidence | Automatic versions | Fixed add command after executable prefix | Extra fail-closed checks |
|---|---|---:|---|---|
| npm | `package-lock.json` | 8-11 | `install --save-dev --ignore-scripts --no-audit --no-fund --registry=<registry> typescript` | reject root `.npmrc`; on Windows invoke trusted `node` plus trusted `npm-cli.js`, never a batch shim |
| pnpm | `pnpm-lock.yaml` | 11 | `add --save-dev --ignore-scripts --ignore-workspace-root-check --registry=<registry> typescript` | reject `.npmrc`, `pnpm-workspace.yaml`, `.pnpmfile.cjs`, and `.pnpmfile.mjs` |
| Yarn | `yarn.lock` | none | documented adapter: `add --dev --mode=skip-build typescript` | always guidance-only in this release because safe PnP verification would execute repository-controlled loader code |
| Bun | `bun.lock` or `bun.lockb` | 1 | `add --dev --ignore-scripts --registry <registry> typescript` | reject `.npmrc` and `bunfig.toml` |

The command choices are grounded in the official [npm install](https://docs.npmjs.com/cli/install/), [pnpm add](https://pnpm.io/cli/add), [pnpm `ignoreScripts`](https://pnpm.io/settings#ignorescripts), [Yarn add](https://yarnpkg.com/cli/add), and [Bun add](https://bun.com/docs/pm/cli/add) contracts. Any future matrix expansion requires primary-documentation review and adapter tests first.

### Stable public model

Use these exact outcome values:

```python
class ActivationOutcome(StrEnum):
    INSTALLED = "installed"
    ALREADY_AVAILABLE = "already_available"
    NOT_APPLICABLE = "not_applicable"
    DECLINED = "declined"
    GUIDANCE_ONLY = "guidance_only"
    VALIDATION_FAILED = "validation_failed"
    INSTALLATION_FAILED = "installation_failed"
    VERIFICATION_FAILED = "verification_failed"
```

Use these exact serialized keys and no raw diagnostic fields:

```python
{
    "outcome": "guidance_only",
    "manager": "npm",              # or null
    "reason": "non_interactive",   # fixed reason code
    "manifest": "package.json",    # or null
    "lockfile": "package-lock.json",  # or null
    "changed_files": [],
    "attempted": False,
}
```

Fatal outcomes are exactly `validation_failed`, `installation_failed`, and `verification_failed`.

## Task 1: Add exact-environment bounded process support

**Files:**
- Modify: `src/graphite/probe_process.py:177-230`
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Write failing transport tests**

Append tests that prove a supplied environment is used verbatim, is copied before launch, rejects malformed keys/values, and leaves the default sanitized behavior unchanged:

```python
def test_bounded_process_accepts_an_exact_environment(tmp_path: Path) -> None:
    from graphite.probe_process import run_bounded_process

    script = "import os;print(os.environ.get('GRAPHITE_SENTINEL'));print(os.environ.get('SECRET_TOKEN'))"
    result = run_bounded_process(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        environment={"GRAPHITE_SENTINEL": "safe"},
        timeout_seconds=5,
    )

    assert result.stdout.decode().splitlines() == ["safe", "None"]


@pytest.mark.parametrize(
    "environment",
    [
        {"": "value"},
        {"BAD=KEY": "value"},
        {"BAD\0KEY": "value"},
        {"GOOD": "bad\0value"},
    ],
)
def test_bounded_process_rejects_invalid_exact_environment(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    from graphite.probe_process import ProbeProcessError, run_bounded_process

    with pytest.raises(ProbeProcessError, match="invalid_environment"):
        run_bounded_process(
            [sys.executable, "-c", "raise AssertionError('must not launch')"],
            cwd=tmp_path,
            environment=environment,
            timeout_seconds=5,
        )
```

- [ ] **Step 2: Run the focused tests and verify the red state**

Run: `python -m pytest tests/test_doctor.py -k "exact_environment or invalid_exact_environment" -q`

Expected: failures because `run_bounded_process()` does not accept `environment` and `invalid_environment` is not classified.

- [ ] **Step 3: Implement exact-environment validation and propagation**

Make these exact structural changes:

```python
_SAFE_ERROR_CODES = frozenset(
    {
        # existing values remain
        "invalid_environment",
    }
)


def _validated_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    selected = sanitized_probe_environment() if environment is None else dict(environment)
    for key, value in selected.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or "=" in key
            or "\0" in key
            or "\0" in value
        ):
            raise ProbeProcessError("invalid_environment")
    return selected


def _launch_process(
    argv: list[str],
    *,
    cwd: Path,
    input_data: bytes | None,
    environment: Mapping[str, str],
) -> _Process:
    if os.name == "nt":
        from .windows_job import launch

        return launch(
            argv,
            cwd=cwd,
            environment=environment,
            with_stdin=input_data is not None,
        )
    # Preserve the existing POSIX setup and replace only
    # env=sanitized_probe_environment() with env=environment.
```

Extend the public function with a keyword-only exact map and validate it before process launch:

```python
def run_bounded_process(
    argv: list[str],
    *,
    cwd: Path,
    stdin: bytes | str | None = None,
    timeout_seconds: float,
    max_output_bytes: int = OUTPUT_LIMIT_BYTES,
    check: bool = True,
    environment: Mapping[str, str] | None = None,
) -> ProbeProcessResult:
    selected_environment = _validated_environment(environment)
    # Preserve the current deadline/input/output implementation.
    process = _launch_process(
        argv,
        cwd=cwd,
        input_data=input_data,
        environment=selected_environment,
    )
```

Do not merge an explicit map with `os.environ`, and do not change any existing caller.

- [ ] **Step 4: Run transport regressions**

Run: `python -m pytest tests/test_doctor.py -k "bounded_process or environment" -q`

Expected: all selected tests pass, including the existing sanitized-default and descendant-cleanup tests.

- [ ] **Step 5: Commit the transport boundary**

```powershell
git add src/graphite/probe_process.py tests/test_doctor.py
git commit -m "feat: support exact bounded process environments"
```

## Task 2: Build safe package-manager and validator primitives

**Files:**
- Create: `src/graphite/dependency_install.py`
- Test: `tests/test_typescript_activation.py`

- [ ] **Step 1: Write adapter and provenance tests**

Create `tests/test_typescript_activation.py` with helpers and table-driven assertions:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite.dependency_install import (
    Manager,
    adapter_for,
    build_install_environment,
    parse_version,
    resolve_external_executable,
)


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("manager", "version", "supported", "tail"),
    [
        (Manager.NPM, "8.19.4", True, ("install", "--save-dev", "--ignore-scripts")),
        (Manager.NPM, "11.13.0", True, ("install", "--save-dev", "--ignore-scripts")),
        (Manager.PNPM, "11.0.0", True, ("add", "--save-dev", "--ignore-scripts")),
        (Manager.YARN, "4.9.2", False, ("add", "--dev", "--mode=skip-build")),
        (Manager.BUN, "1.2.0", True, ("add", "--dev", "--ignore-scripts")),
        (Manager.PNPM, "10.9.0", False, ("add", "--save-dev", "--ignore-scripts")),
    ],
)
def test_manager_support_matrix(
    manager: Manager, version: str, supported: bool, tail: tuple[str, ...]
) -> None:
    adapter = adapter_for(manager)
    assert adapter.supports(parse_version(version)) is supported
    assert adapter.argument_tail("https://registry.npmjs.org/")[: len(tail)] == tail
    assert adapter.argument_tail("https://registry.npmjs.org/")[-1] == "typescript"


def test_install_environment_is_allowlisted_and_credential_free(tmp_path: Path) -> None:
    env = build_install_environment(
        manager=Manager.NPM,
        isolated_home=tmp_path,
        executable_directories=(tmp_path / "bin",),
        registry="https://registry.npmjs.org/",
        source={
            "PATH": "C:/repo-controlled;C:/trusted",
            "NPM_TOKEN": "secret",
            "NODE_AUTH_TOKEN": "secret",
            "GRAPHITE_LLM_API_KEY": "secret",
            "HTTP_PROXY": "http://secret-proxy",
            "SYSTEMROOT": "C:/Windows",
        },
    )

    serialized = json.dumps(env)
    assert "secret" not in serialized
    assert "repo-controlled" not in serialized
    assert env["NPM_CONFIG_REGISTRY"] == "https://registry.npmjs.org/"
    assert env["NPM_CONFIG_IGNORE_SCRIPTS"] == "true"
    assert env["NPM_CONFIG_USERCONFIG"].startswith(str(tmp_path))
```

Also add platform-neutral fake executable tests proving relative paths, repository-contained paths, symlinks escaping through the repository, non-files, and untrusted `.cmd`/`.bat` shims are rejected. Test the allowed Windows npm form as a trusted `node.exe` plus an adjacent regular `node_modules/npm/bin/npm-cli.js`.

- [ ] **Step 2: Verify adapter tests fail**

Run: `python -m pytest tests/test_typescript_activation.py -q`

Expected: collection fails because `graphite.dependency_install` does not exist.

- [ ] **Step 3: Implement immutable adapters and safe reason types**

Create the module with these concrete types and constants:

```python
"""Fail-closed dependency-install primitives for explicit onboarding consent."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .probe_process import ProbeProcessError, ProbeProcessResult, run_bounded_process

TRUSTED_REGISTRY = "https://registry.npmjs.org/"
INSTALL_OUTPUT_LIMIT = 64 * 1024
MAX_CONTROL_FILE_BYTES = 8 * 1024 * 1024
ACTIVATION_MAX_FILES = 100_000
_VERSION_RE = re.compile(r"^(?:v)?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$")


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
        return bool(self.automatic and version and version.major in self.supported_majors)

    def argument_tail(self, registry: str) -> tuple[str, ...]:
        return self.command_builder(registry)


@dataclass(frozen=True)
class TrustedFile:
    path: Path
    identity: tuple[int, int]


def parse_version(raw: str) -> Version | None:
    match = _VERSION_RE.fullmatch(raw.strip())
    if match is None:
        return None
    return Version(*(int(part) for part in match.groups()))
```

Define `_ADAPTERS` with the exact matrix and argv tails shown above. Yarn's adapter must retain its documented command for diagnostics/tests but set `automatic=False`.

- [ ] **Step 4: Implement exact environment and executable provenance**

`build_install_environment()` must start with an empty dict, copy only OS essentials needed for process creation (`SYSTEMROOT`, `WINDIR`, `COMSPEC`, `PATHEXT`, `LANG`, `LC_ALL` when present), build `PATH` exclusively from already-proven executable parent directories plus Windows System32 or `/usr/bin:/bin`, and set isolated `HOME`, `USERPROFILE`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `TEMP`, and `TMP` under `isolated_home`.

Manager-specific values are exact:

```python
if manager in {Manager.NPM, Manager.PNPM}:
    env.update(
        {
            "NPM_CONFIG_USERCONFIG": str(isolated_home / "npmrc"),
            "NPM_CONFIG_REGISTRY": registry,
            "NPM_CONFIG_IGNORE_SCRIPTS": "true",
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_FUND": "false",
            "npm_config_registry": registry,
            "npm_config_ignore_scripts": "true",
        }
    )
elif manager is Manager.BUN:
    env["BUN_INSTALL_CACHE_DIR"] = str(isolated_home / "bun-cache")
elif manager is Manager.YARN:
    env.update(
        {
            "YARN_NPM_REGISTRY_SERVER": registry,
            "YARN_ENABLE_SCRIPTS": "false",
            "YARN_ENABLE_TELEMETRY": "0",
            "YARN_GLOBAL_FOLDER": str(isolated_home / "yarn-global"),
        }
    )
```

`resolve_external_executable(name, root, source_path)` must consider only absolute PATH entries, use `resolve(strict=True)`, require a regular executable, reject anything resolving inside `root`, and return `TrustedFile(path, (st_dev, st_ino))`. On POSIX require `os.access(path, os.X_OK)`. On Windows accept only `.exe` and `.com`; npm batch shims are handled by a separate `resolve_npm_prefix()` that returns trusted Node plus trusted `npm-cli.js` after proving both regular and outside `root`.

Implement `revalidate_trusted_file(reference, root)` to repeat canonical containment, regular-file, executable (when applicable), and identity checks immediately before every launch. A changed validator, Node binary, manager binary, or npm CLI file fails closed with a fixed `executable_changed` or `validator_changed` reason.

- [ ] **Step 5: Implement bounded snapshots, validator, manager version, install, and local probe**

Use these exact contracts so later orchestration never sees raw output:

```python
@dataclass(frozen=True)
class FileSnapshot:
    relative_path: str
    identity: tuple[int, int]
    sha256: str


@dataclass(frozen=True)
class StepResult:
    ok: bool
    reason: str


Runner = Callable[..., ProbeProcessResult]


def run_validator(
    *, root: Path, node: Path, validator: Path, timeout: float, runner: Runner
) -> StepResult:
    try:
        runner(
            [str(node), str(validator), "typescript"],
            cwd=root,
            timeout_seconds=timeout,
            max_output_bytes=INSTALL_OUTPUT_LIMIT,
            environment=minimal_node_environment(node.parent),
        )
    except ProbeProcessError as exc:
        return StepResult(False, "validator_timeout" if exc.code == "timeout" else "validator_rejected")
    return StepResult(True, "validated")
```

`run_manager_version()` invokes only the trusted prefix plus `--version`, parses at most 64 bytes, and returns a fixed `manager_version_invalid` reason on malformed/nonzero/timeout output. `run_install()` invokes `prefix + adapter.argument_tail(TRUSTED_REGISTRY)` with stdin closed, exact isolated environment, bounded output, and returns only `installed_command`, `install_timeout`, or `install_failed`.

`probe_local_typescript()` invokes trusted Node with a fixed `-e` script that calls `require.resolve('typescript/package.json', {paths:[process.cwd()]})`, prints one JSON object, and then verifies in Python that the resolved package file is a regular file under `<root>/node_modules/typescript`. A path outside the selected root, invalid JSON, nonzero exit, timeout, symlink, or reparse point returns `False` without exposing the path.

`snapshot_control_file()` must use `lstat`, reject symlinks/reparse points/non-regular files, enforce `MAX_CONTROL_FILE_BYTES`, read bytes once, `fstat` the open descriptor before and after reading, verify the canonical path remains under `root`, and return SHA-256 plus `(st_dev, st_ino)`. This function is used for `package.json` and the selected lockfile before consent-adjacent mutation and immediately after installation.

Add `control_files_use_trusted_sources(manifest_bytes, lockfile_bytes)` and fail closed when a declared dependency uses `file:`, `link:`, `git`, `ssh:`, an absolute/parent-relative path, or an HTTP(S) URL; or when the lockfile contains `git+`, `ssh:`, `file:`, or an HTTP(S) host other than `registry.npmjs.org`. This prevents an add operation from using the existing dependency graph to contact an untrusted source even though the new package name is fixed.

- [ ] **Step 6: Run primitive tests and the package guardrail check**

No package is installed by this task. Run:

`python -m pytest tests/test_typescript_activation.py -q`

Expected: all primitive tests pass and no test contacts a registry.

Run: `python -m ruff check src/graphite/dependency_install.py tests/test_typescript_activation.py`

Expected: no findings.

- [ ] **Step 7: Commit the safe primitives**

```powershell
git add src/graphite/dependency_install.py tests/test_typescript_activation.py
git commit -m "feat: add safe dependency install primitives"
```

## Task 3: Implement TypeScript eligibility and typed policy outcomes

**Files:**
- Create: `src/graphite/typescript_activation.py`
- Modify: `tests/test_typescript_activation.py`

- [ ] **Step 1: Write failing detection and serialization tests**

Add tests for every manager lockfile, both Bun lockfiles together, missing root manifest, nested-only manifest, malformed JSON, conflicting `packageManager`, unsafe manager configuration, no TS evidence, TS evidence through `.ts`, `.tsx`, and root `tsconfig.json`, and an already available local compiler. Assert exact dictionaries rather than substring matching.

Use this result contract in the tests:

```python
from graphite.typescript_activation import (
    ActivationOutcome,
    ActivationResult,
    detect_activation,
)


def test_result_serialization_is_bounded_and_stable() -> None:
    result = ActivationResult(
        outcome=ActivationOutcome.GUIDANCE_ONLY,
        manager=Manager.NPM,
        reason="non_interactive",
        manifest="package.json",
        lockfile="package-lock.json",
    )
    assert result.to_dict() == {
        "outcome": "guidance_only",
        "manager": "npm",
        "reason": "non_interactive",
        "manifest": "package.json",
        "lockfile": "package-lock.json",
        "changed_files": [],
        "attempted": False,
    }
```

- [ ] **Step 2: Run detection tests and verify they fail**

Run: `python -m pytest tests/test_typescript_activation.py -k "detect or serialization or evidence or lockfile" -q`

Expected: import failure for `graphite.typescript_activation`.

- [ ] **Step 3: Implement the typed model and pure eligibility detector**

Create the module with these exact public definitions:

```python
"""Consent and policy orchestration for optional project-local TypeScript."""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from .config import Config
from .dependency_install import ACTIVATION_MAX_FILES, FileSnapshot, Manager, adapter_for
from .ingest import collect_files
from .probe_process import run_bounded_process


@dataclass(frozen=True)
class ActivationDetection:
    result: ActivationResult | None
    manager: Manager | None
    manifest: str | None
    lockfile: str | None
    manifest_snapshot: FileSnapshot | None
    lockfile_snapshot: FileSnapshot | None


class ActivationOutcome(StrEnum):
    INSTALLED = "installed"
    ALREADY_AVAILABLE = "already_available"
    NOT_APPLICABLE = "not_applicable"
    DECLINED = "declined"
    GUIDANCE_ONLY = "guidance_only"
    VALIDATION_FAILED = "validation_failed"
    INSTALLATION_FAILED = "installation_failed"
    VERIFICATION_FAILED = "verification_failed"


FATAL_OUTCOMES = frozenset(
    {
        ActivationOutcome.VALIDATION_FAILED,
        ActivationOutcome.INSTALLATION_FAILED,
        ActivationOutcome.VERIFICATION_FAILED,
    }
)


@dataclass(frozen=True)
class ActivationResult:
    outcome: ActivationOutcome
    manager: Manager | None
    reason: str
    manifest: str | None = None
    lockfile: str | None = None
    changed_files: tuple[str, ...] = ()
    attempted: bool = False

    @property
    def fatal(self) -> bool:
        return self.outcome in FATAL_OUTCOMES

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "manager": self.manager.value if self.manager else None,
            "reason": self.reason,
            "manifest": self.manifest,
            "lockfile": self.lockfile,
            "changed_files": list(self.changed_files),
            "attempted": self.attempted,
        }
```

`detect_activation(root, cfg, *, local_typescript_available)` must:

1. Resolve and require an existing directory root.
2. Use `collect_files(root, replace(cfg, max_files=min(cfg.max_files or ACTIVATION_MAX_FILES, ACTIVATION_MAX_FILES)))` and check for a `typescript`/`tsx` entry; also accept only a safe regular contained root `tsconfig.json`. Import `replace` from `dataclasses` and `ACTIVATION_MAX_FILES` from the dependency module so detection is bounded even when normal ingestion has no configured cap.
3. Return `not_applicable/no_typescript_evidence` before inspecting manager state when there is no evidence.
4. Return `already_available/local_typescript_available` when the injected probe says true.
5. Snapshot a regular root `package.json`, parse at most the bounded bytes, require a JSON object, and reject executable/non-registry dependency specifiers (`file:`, `link:`, `git`, URL, absolute, parent-relative), `workspaces`, `resolutions`, and manager-specific hook/config files.
6. Select a manager only when exactly one lockfile family is present; both Bun formats count as two and are ambiguous.
7. Accept `packageManager` only when it is absent or matches `^(npm|pnpm|yarn|bun)@[^\s]+$` and the family agrees.
8. Apply `control_files_use_trusted_sources()` so an existing manifest or lockfile cannot redirect installation through an exotic or non-canonical source.
9. Return `ActivationDetection`; terminal no-op/guidance cases populate `result`, while an eligible candidate populates manager, relative control-file names, and snapshots. Never retain parsed repository content.

- [ ] **Step 4: Run policy detection tests**

Run: `python -m pytest tests/test_typescript_activation.py -k "detect or serialization or evidence or lockfile" -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit detection policy**

```powershell
git add src/graphite/typescript_activation.py tests/test_typescript_activation.py
git commit -m "feat: detect safe TypeScript activation candidates"
```

## Task 4: Add consent-gated activation orchestration

**Files:**
- Modify: `src/graphite/typescript_activation.py`
- Modify: `tests/test_typescript_activation.py`

- [ ] **Step 1: Write failing orchestration tests with injected fakes**

Add a `RecordingRunner` that records argv, cwd, environment, and timeout, and returns fixed `ProbeProcessResult` objects. Add explicit tests for:

- stdin TTY false, stdout TTY false, JSON true, or `assume_yes` true: `guidance_only/non_interactive`, zero prompt calls, zero validator calls, zero manager calls;
- prompt responses `"y"` and `"YES"`: validation then manager version then install then verification;
- prompt responses `""`, EOF, `"n"`, and arbitrary text: `declined/user_declined`, zero child calls;
- invalid/missing/repository-contained validator: `validation_failed` before manager execution;
- validator rejection/timeout: `validation_failed`;
- manager unsupported, unsafe, or version malformed: `guidance_only` before validation so consent never grants an unverifiable command;
- install nonzero/timeout/output-limit: `installation_failed` with no raw output;
- successful manager exit but failed local probe: `verification_failed`;
- successful verification: `installed` and sorted changed allowlisted file names;
- manifest/lock identity changed between preflight and launch: fail closed without install;
- concurrent call for the same canonical root: second call returns `guidance_only/activation_in_progress`;
- separate roots can activate independently;
- every child timeout is positive and does not exceed one injected shared deadline.

The acceptance-order assertion must be exact:

```python
assert events == [
    "manager_version",
    "prompt",
    "validator",
    "prelaunch_snapshot",
    "install",
    "postinstall_snapshot",
    "verify",
]
```

- [ ] **Step 2: Verify orchestration tests fail**

Run: `python -m pytest tests/test_typescript_activation.py -k "interactive or validator or install or verification or concurrent or deadline" -q`

Expected: failures because `activate_typescript()` is not implemented.

- [ ] **Step 3: Implement request, deadline, root lock, and prompt policy**

Use these exact interfaces:

```python
@dataclass(frozen=True)
class ActivationRequest:
    root: Path
    cfg: Config
    stdin_is_tty: bool
    stdout_is_tty: bool
    assume_yes: bool
    json_mode: bool
    timeout_seconds: float = 120.0


@dataclass
class ActivationDependencies:
    prompt: Callable[[str], str] = input
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    monotonic: Callable[[], float] = time.monotonic
    runner: Callable[..., object] = run_bounded_process


class _Deadline:
    def __init__(self, seconds: float, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self._end = monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self._end - self._monotonic())
```

Maintain `_ROOT_LOCKS: dict[Path, threading.Lock]` behind a global guard. Acquire the canonical-root lock nonblocking and release it in `finally`; remove it only while holding the guard and only when unlocked/no longer current.

Prompt exactly once with:

```python
answer = dependencies.prompt(
    f"Project-local TypeScript is missing. Install it with {manager.value} "
    "as a development dependency? [y/N]"
)
approved = answer.strip().lower() in {"y", "yes"}
```

Catch `EOFError` as decline. Prompting is allowed only when both TTY booleans are true and neither JSON nor `assume_yes` is set.

- [ ] **Step 4: Implement validation/install/verification ordering**

`activate_typescript()` must perform this sequence under the root lock:

1. Detect evidence and local availability.
2. Resolve trusted Node and manager command prefix; run bounded `--version`; require the adapter matrix.
3. Return guidance before prompting if any automatic safety prerequisite fails.
4. Prompt once.
5. Validate `GRAPHITE_PACKAGE_VALIDATOR` as an absolute regular file outside root.
6. Start one shared deadline and run exact `node validator typescript`.
7. Re-snapshot `package.json` and lockfile and require them to equal the pre-consent snapshots.
8. Revalidate Node, validator, manager executable, and npm CLI identities immediately before their respective launches.
9. Create a temporary isolated home, prove its canonical path is outside root, build the exact environment, and run fixed install argv. If the platform temp directory falls under the selected root, return `installation_failed/isolation_unavailable` without launching.
10. Snapshot both control files again and return `installation_failed/control_file_unsafe` if either is no longer a safe contained regular file.
11. Probe project-local TypeScript with revalidated trusted Node.
12. Return `installed/installed` only after verification.

Map `ProbeProcessError` to fixed categories and never include `str(exc)`, stdout, stderr, validator path, executable path, registry response, or repository content in `ActivationResult`.

- [ ] **Step 5: Run the full activation suite**

Run: `python -m pytest tests/test_typescript_activation.py -q`

Expected: all tests pass without live network or real package installation.

Run: `python -m ruff check src/graphite/dependency_install.py src/graphite/typescript_activation.py tests/test_typescript_activation.py`

Expected: no findings.

- [ ] **Step 6: Commit orchestration**

```powershell
git add src/graphite/typescript_activation.py tests/test_typescript_activation.py
git commit -m "feat: orchestrate consent-gated TypeScript activation"
```

## Task 5: Integrate activation into init and bootstrap

**Files:**
- Modify: `src/graphite/cli.py:371-473`
- Modify: `src/graphite/cli.py:947-965`
- Modify: `tests/test_init.py`
- Modify: `tests/test_bootstrap.py`

- [ ] **Step 1: Write failing CLI integration tests**

Monkeypatch `graphite.cli.activate_typescript` so integration tests never run a package manager. Cover:

```python
def test_init_json_emits_non_mutating_typescript_activation(tmp_path, capsys, monkeypatch) -> None:
    calls = []

    def fake_activate(request):
        calls.append(request)
        return ActivationResult(
            ActivationOutcome.GUIDANCE_ONLY,
            Manager.NPM,
            "non_interactive",
            "package.json",
            "package-lock.json",
        )

    monkeypatch.setattr("graphite.cli.activate_typescript", fake_activate)
    result = main([
        "init", str(tmp_path), "--platform", "codex",
        "--no-build", "--no-validate", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["typescript_activation"]["outcome"] == "guidance_only"
    assert calls[0].json_mode is True
    assert calls[0].stdin_is_tty is False or calls[0].stdout_is_tty is False
```

Add matching bootstrap tests for success and each fatal outcome. Assert onboarding files exist after a fatal activation result, build/validation still follow their existing flags, and the final exit is nonzero. Add `bootstrap --yes` parsing coverage and assert it suppresses activation prompting through `request.assume_yes is True`.

Add an ordering test by monkeypatching `bootstrap_project`, `activate_typescript`, and `_build_project`; assert `events == ["onboarding", "activation", "build"]`.

- [ ] **Step 2: Run CLI tests and verify red state**

Run: `python -m pytest tests/test_init.py tests/test_bootstrap.py -q`

Expected: failures because the payload has no `typescript_activation` key and bootstrap has no `--yes`.

- [ ] **Step 3: Add a shared CLI activation helper**

Import `ActivationRequest`, `ActivationResult`, and `activate_typescript`. Add:

```python
def _activate_typescript_for_onboarding(
    args: argparse.Namespace, root: Path, cfg: Config
) -> ActivationResult:
    return activate_typescript(
        ActivationRequest(
            root=root,
            cfg=cfg,
            stdin_is_tty=sys.stdin.isatty(),
            stdout_is_tty=sys.stdout.isatty(),
            assume_yes=bool(getattr(args, "yes", False)),
            json_mode=bool(args.json),
        )
    )
```

Call it immediately after `init_project(...).to_dict()` or `bootstrap_project(...).to_dict()` and configuration construction, before `_build_project`. Add `"typescript_activation": activation.to_dict()` to both payloads.

- [ ] **Step 4: Add stable text and exit behavior**

Human output must append one bounded line:

```python
manager = activation.manager.value if activation.manager else "none"
changed = ", ".join(activation.changed_files) if activation.changed_files else "none"
print(
    "  - TypeScript activation: "
    f"{activation.outcome.value} (manager={manager}, reason={activation.reason}, changed={changed})"
)
```

For `guidance_only`, print the fixed five-step manual workflow from the spec without any global install command, absolute host path, or credential. The manual command must use `<absolute-validator-path>` as a literal marker and `<project-manager>` as a literal marker.

Combine status exactly:

```python
validation_failed = validation.get("ok") is False
return 1 if activation.fatal or validation_failed else 0
```

Add to the bootstrap parser:

```python
p_bootstrap.add_argument(
    "--yes",
    action="store_true",
    help="Run non-interactively and never offer dependency installation",
)
```

Clarify init's existing `--yes` help to state that it also suppresses optional dependency prompts.

- [ ] **Step 5: Run CLI integration and parser regressions**

Run: `python -m pytest tests/test_init.py tests/test_bootstrap.py -q`

Expected: all tests pass.

Run: `python -m graphite bootstrap --help`

Expected: help includes `--yes`; it performs no onboarding or installation.

- [ ] **Step 6: Commit CLI integration**

```powershell
git add src/graphite/cli.py tests/test_init.py tests/test_bootstrap.py
git commit -m "feat: offer TypeScript activation during onboarding"
```

## Task 6: Prove non-onboarding paths cannot install

**Files:**
- Modify: `tests/test_typescript_activation.py`
- Modify: `tests/test_doctor.py`

- [ ] **Step 1: Add import-boundary and command-path tests**

Add an AST/import test asserting `activate_typescript` is imported only by `graphite.cli` and defined only by `graphite.typescript_activation`. Run `build`, `report`, `check`, `doctor`, daemon parser paths, and MCP module import with a monkeypatched activation function that raises if called.

Use this exact invariant:

```python
def forbidden_activation(*args, **kwargs):
    raise AssertionError("non-onboarding path attempted TypeScript activation")
```

Test redirected input, redirected output, CI-like environment, JSON, and `--yes` on both onboarding commands. Assert zero fake validator/manager events.

- [ ] **Step 2: Add adversarial filesystem and output tests**

Cover symlinked/reparse-point control files where the platform supports them, escaping `node_modules/typescript`, validator output containing credentials and absolute paths, manager output over the byte cap, PATH entries under root, a manager executable replaced between resolution and launch, a validator replaced between validation and launch, lockfile replacement, Unicode/path edge cases, and a descendant process that holds output pipes open. Assertions must inspect only fixed reason codes and confirm the hostile strings never occur in `repr(result.to_dict())`.

- [ ] **Step 3: Run the security-focused suite**

Run: `python -m pytest tests/test_typescript_activation.py tests/test_doctor.py -k "typescript_activation or bounded_process or environment" -q`

Expected: all selected tests pass; platform-specific reparse tests may skip with an explicit capability reason.

- [ ] **Step 4: Commit hardening coverage**

```powershell
git add tests/test_typescript_activation.py tests/test_doctor.py
git commit -m "test: harden TypeScript activation boundaries"
```

## Task 7: Document the operator and security contract

**Files:**
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `ARCHITECTURE.md`
- Modify: `tests/test_documentation.py`

- [ ] **Step 1: Write failing documentation contract tests**

Add assertions that the documentation states all of the following exact concepts:

```python
def test_docs_define_consent_gated_typescript_activation() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    architecture = Path("ARCHITECTURE.md").read_text(encoding="utf-8")
    contributing = Path("CONTRIBUTING.md").read_text(encoding="utf-8")
    combined = "\n".join((readme, architecture, contributing)).lower()

    assert "project-local typescript" in combined
    assert "defaults to no" in combined
    assert "graphite_package_validator" in combined
    assert "non-interactive" in combined
    assert "lifecycle scripts" in combined
    assert "private registries" in combined
    assert "yarn" in combined and "guidance-only" in combined
    assert "global typescript" in combined and "does not" in combined
```

- [ ] **Step 2: Verify documentation tests fail**

Run: `python -m pytest tests/test_documentation.py -q`

Expected: the new contract test fails before the documentation edit.

- [ ] **Step 3: Update user, contributor, and architecture documentation**

Document:

- exact eligibility and prompt text;
- explicit consent and default-No behavior;
- JSON/redirected/CI/`--yes` no-install behavior;
- local-only `typescript` dev dependency and no `@types/*` inference;
- validator-before-install ordering and the package guardrail command;
- supported automatic manager/version matrix and guidance-only Yarn rationale;
- canonical public registry and private-mirror manual path;
- failure outcomes, preserved onboarding files, and nonzero exit behavior;
- no install authority in build/report/check/doctor/daemon/watch/MCP;
- process isolation, bounded output/deadline, script suppression, no ambient tokens, and no automatic rollback.

Do not publish a global TypeScript installation command. Manual examples must validate exact package `typescript` before a local dev-dependency command.

- [ ] **Step 4: Run documentation tests**

Run: `python -m pytest tests/test_documentation.py -q`

Expected: all documentation tests pass.

- [ ] **Step 5: Commit documentation**

```powershell
git add README.md CONTRIBUTING.md ARCHITECTURE.md tests/test_documentation.py
git commit -m "docs: explain safe TypeScript activation"
```

## Task 8: Run acceptance, update design status, and finish

**Files:**
- Modify: `docs/superpowers/specs/2026-07-13-interactive-typescript-activation-design.md`
- Verify: all changed source, tests, and docs

- [ ] **Step 1: Run focused behavior tests**

Run:

```powershell
python -m pytest tests/test_typescript_activation.py tests/test_init.py tests/test_bootstrap.py tests/test_documentation.py -q
```

Expected: all pass with no network access and no real package installation.

- [ ] **Step 2: Run process containment regressions**

Run:

```powershell
python -m pytest tests/test_doctor.py -q
```

Expected: all pass or retain only pre-existing platform capability skips.

- [ ] **Step 3: Run lint and the full suite**

Run:

```powershell
python -m ruff check src tests
python -m pytest -q
```

Expected: Ruff reports no findings; the full suite passes with only documented pre-existing skips.

- [ ] **Step 4: Exercise non-mutating CLI smoke paths**

Create temporary fixtures only under pytest or the OS temp directory, never under a real project. Run:

```powershell
python -m graphite init --list-platforms
python -m graphite bootstrap --help
python -m graphite doctor . --json
```

Expected: all exit successfully; help lists `--yes`; none prompt or install.

- [ ] **Step 5: Perform the written-plan security audit**

Run:

```powershell
rg -n "activate_typescript|run_install|GRAPHITE_PACKAGE_VALIDATOR" src tests
rg -n "npm install -g|pnpm add -g|yarn global|bun add -g" README.md CONTRIBUTING.md ARCHITECTURE.md src tests
git diff --check
git status --short
```

Expected:

- activation entry is reachable only from `cmd_init` and `cmd_bootstrap`;
- no global TypeScript command exists;
- no whitespace errors;
- only intentional task files are modified.

- [ ] **Step 6: Mark the design implemented**

Change only the design status line to:

```markdown
**Status:** Implemented and acceptance-tested
```

- [ ] **Step 7: Commit acceptance metadata**

```powershell
git add docs/superpowers/specs/2026-07-13-interactive-typescript-activation-design.md
git commit -m "docs: mark TypeScript activation implemented"
```

- [ ] **Step 8: Review the final branch without pushing**

Run:

```powershell
git log --oneline --decorate -12
git status --short --branch
```

Expected: the branch is clean and ahead of `origin/main`; pushing remains a separate explicit action.
