from __future__ import annotations

import json
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import graphite.dependency_install as dependency_install
import graphite.ingest as ingest
import graphite.typescript_activation as typescript_activation

from graphite.config import Config
from graphite.dependency_install import (
    ACTIVATION_MAX_FILES,
    INSTALL_OUTPUT_LIMIT,
    MAX_CONTROL_FILE_BYTES,
    MAX_TRUSTED_FILE_BYTES,
    TRUSTED_REGISTRY,
    Manager,
    ManagerAdapter,
    ProbeProcessError,
    StepResult,
    TrustedCommand,
    TrustedFile,
    Version,
    adapter_for,
    build_install_environment,
    control_files_use_trusted_sources,
    parse_version,
    probe_local_typescript,
    resolve_trusted_file,
    resolve_trusted_executable,
    resolve_windows_npm_prefix,
    run_install,
    run_manager_version,
    run_validator,
    snapshot_control_file,
)
from graphite.probe_process import ProbeProcessResult
from graphite.typescript_activation import (
    FATAL_OUTCOMES,
    ActivationDependencies,
    ActivationDetection,
    ActivationOutcome,
    ActivationRequest,
    ActivationResult,
    activate_typescript,
    detect_activation,
    revalidate_activation_detection,
)


_SAFE_LOCKS = {
    "package-lock.json": b'{"lockfileVersion":3}',
    "pnpm-lock.yaml": b"lockfileVersion: '9.0'\npackages: {}\n",
    "yarn.lock": (
        b"# yarn lockfile v1\n\n"
        b"typescript@5.0.0:\n"
        b'  version "5.0.0"\n'
        b'  resolved "https://registry.npmjs.org/typescript/-/typescript-5.0.0.tgz"\n'
    ),
    "bun.lock": b'{"lockfileVersion":1}',
    "bun.lockb": b'{"lockfileVersion":1}',
}


def _activation_root(
    tmp_path: Path,
    lockfile: str = "package-lock.json",
    *,
    manifest: bytes = b"{}",
) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.ts").write_text("export {};", encoding="utf-8")
    (root / "package.json").write_bytes(manifest)
    (root / lockfile).write_bytes(_SAFE_LOCKS[lockfile])
    return root


def _detect(root: Path, *, available: bool = False, cfg: Config | None = None) -> ActivationDetection:
    return detect_activation(
        root,
        cfg or Config(),
        local_typescript_available=available,
    )


def test_activate_without_typescript_evidence_is_not_applicable_without_process(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def runner(*_args, **_kwargs):
        raise AssertionError("no process should run")

    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False),
        ActivationDependencies(runner=runner, environ={"PATH": str(tmp_path)}),
    )

    assert result == ActivationResult(
        ActivationOutcome.NOT_APPLICABLE,
        None,
        "no_typescript_evidence",
    )


def _activation_dependencies(tmp_path, root, events, *, prompt_answer="yes", install_ok=True, verify_ok=True):
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    node_name = "node.exe" if os.name == "nt" else "node"
    node = tools / node_name
    node.write_bytes(b"trusted node")
    validator = tools / "validate-packages.cjs"
    validator.write_bytes(b"trusted validator")
    if os.name == "nt":
        npm_cli = tools / "node_modules" / "npm" / "bin" / "npm-cli.js"
        npm_cli.parent.mkdir(parents=True, exist_ok=True)
        npm_cli.write_bytes(b"trusted npm cli")
    else:
        npm = tools / "npm"
        npm.write_bytes(b"trusted npm")
        node.chmod(0o700)
        npm.chmod(0o700)
    probes = 0

    def prompt(message):
        events.append(("prompt", message))
        if isinstance(prompt_answer, BaseException):
            raise prompt_answer
        return prompt_answer

    def runner(argv, **kwargs):
        nonlocal probes
        argv = [str(value) for value in argv]
        if "-e" in argv:
            probes += 1
            phase = "local_probe" if probes == 1 else "verify"
            events.append((phase, kwargs["timeout_seconds"]))
            if phase == "verify" and verify_ok:
                package = root / "node_modules" / "typescript" / "package.json"
                package.parent.mkdir(parents=True, exist_ok=True)
                package.write_bytes(b"{}")
                return ProbeProcessResult(0, json.dumps({"resolved": str(package)}).encode(), b"", 0)
            return ProbeProcessResult(1, b"", b"secret probe", 0)
        if argv[-1] == "--version":
            events.append(("manager_version", kwargs["timeout_seconds"]))
            return ProbeProcessResult(0, b"10.9.0", b"", 0)
        if argv[-1] == "typescript" and str(validator) in argv:
            events.append(("validator", kwargs["timeout_seconds"]))
            return ProbeProcessResult(0, b"secret validator output", b"", 0)
        events.append(("install", kwargs["timeout_seconds"]))
        if install_ok:
            (root / "package.json").write_bytes(b'{"devDependencies":{"typescript":"^5.0.0"}}')
            (root / "package-lock.json").write_bytes(b'{"lockfileVersion":3,"packages":{}}')
            return ProbeProcessResult(0, b"secret install output", b"", 0)
        return ProbeProcessResult(1, b"secret failure", b"secret failure", 0)

    environment = {
        "PATH": str(tools),
        "GRAPHITE_PACKAGE_VALIDATOR": str(validator),
    }

    def cleanup(isolated_home, _selected_root, _timeout):
        shutil.rmtree(isolated_home)
        return StepResult(True, "cleaned")

    return ActivationDependencies(
        prompt=prompt,
        environ=environment,
        runner=runner,
        temporary_directory=lambda: tempfile.TemporaryDirectory(dir=tmp_path),
        cleanup=cleanup,
    )


@pytest.mark.parametrize(
    "request_changes",
    [
        {"stdin_is_tty": False},
        {"stdout_is_tty": False},
        {"json_mode": True},
        {"assume_yes": True},
    ],
)
def test_activate_noninteractive_never_prompts_or_installs(tmp_path, request_changes):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    values = dict(
        root=root,
        cfg=Config(),
        stdin_is_tty=True,
        stdout_is_tty=True,
        assume_yes=False,
        json_mode=False,
    )
    values.update(request_changes)

    result = activate_typescript(ActivationRequest(**values), deps)

    assert result == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY,
        Manager.NPM,
        "non_interactive",
        "package.json",
        "package-lock.json",
    )
    assert [event[0] for event in events] == ["local_probe"]


@pytest.mark.parametrize("answer", ["", "n", "junk", EOFError()])
def test_activate_decline_prompts_once_and_stops(tmp_path, answer):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events, prompt_answer=answer)

    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result == ActivationResult(
        ActivationOutcome.DECLINED,
        Manager.NPM,
        "user_declined",
        "package.json",
        "package-lock.json",
    )
    assert [event[0] for event in events] == ["local_probe", "manager_version", "prompt"]
    assert events[-1][1] == (
        "Project-local TypeScript is missing. Install it with npm as a development "
        "dependency? [y/N]"
    )


@pytest.mark.parametrize("answer", ["y", " YES "])
def test_activate_accepts_consent_and_installs_in_exact_order(tmp_path, monkeypatch, answer):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events, prompt_answer=answer)
    original_revalidate = typescript_activation.revalidate_activation_detection
    original_snapshot = typescript_activation.snapshot_control_file
    revalidation_calls = 0
    snapshot_started = False

    def recording_revalidate(*args, **kwargs):
        nonlocal revalidation_calls
        revalidation_calls += 1
        events.append(("prelaunch_snapshot", 0))
        return original_revalidate(*args, **kwargs)

    def recording_snapshot(*args, **kwargs):
        nonlocal snapshot_started
        if not snapshot_started and any(event[0] == "install" for event in events):
            snapshot_started = True
            events.append(("postinstall_snapshot/safety", 0))
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(typescript_activation, "revalidate_activation_detection", recording_revalidate)
    monkeypatch.setattr(typescript_activation, "snapshot_control_file", recording_snapshot)

    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result == ActivationResult(
        ActivationOutcome.INSTALLED,
        Manager.NPM,
        "installed",
        "package.json",
        "package-lock.json",
        ("package-lock.json", "package.json"),
        True,
    )
    assert [event[0] for event in events] == [
        "local_probe",
        "manager_version",
        "prompt",
        "validator",
        "prelaunch_snapshot",
        "install",
        "postinstall_snapshot/safety",
        "verify",
    ]
    assert revalidation_calls == 1


def test_activate_returns_already_available_before_manager_inspection(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    package = root / "node_modules" / "typescript" / "package.json"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"{}")

    def runner(argv, **kwargs):
        events.append("local_probe")
        return ProbeProcessResult(
            0, json.dumps({"resolved": str(package)}).encode(), b"", 0
        )

    deps.runner = runner
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result == ActivationResult(
        ActivationOutcome.ALREADY_AVAILABLE,
        None,
        "local_typescript_available",
    )
    assert events == ["local_probe"]


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "1"])
def test_activate_rejects_invalid_timeout_without_process(tmp_path, timeout):
    root = _activation_root(tmp_path)
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False, timeout),
        ActivationDependencies(runner=lambda *_a, **_k: pytest.fail("runner called")),
    )
    assert result == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY, None, "invalid_timeout"
    )


def test_activate_guides_when_node_or_manager_is_unavailable(tmp_path):
    root = _activation_root(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    no_node = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False),
        ActivationDependencies(environ={"PATH": str(empty)}),
    )
    assert no_node.reason == "node_unavailable"

    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    tools = Path(deps.environ["PATH"])
    if os.name == "nt":
        (tools / "node_modules" / "npm" / "bin" / "npm-cli.js").unlink()
    else:
        (tools / "npm").unlink()
    no_manager = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )
    assert no_manager.reason == "manager_unavailable"
    assert [event[0] for event in events] == ["local_probe"]


@pytest.mark.parametrize(
    ("version_result", "reason"),
    [
        (ProbeProcessResult(0, b"not-a-version", b"secret", 0), "manager_version_invalid"),
        (ProbeProcessResult(1, b"secret", b"secret", 0), "manager_version_unavailable"),
        (ProbeProcessResult(0, b"99.0.0", b"", 0), "manager_version_unsupported"),
        (ProbeProcessError("timeout"), "manager_version_timeout"),
    ],
)
def test_activate_manager_version_failures_are_sanitized(tmp_path, version_result, reason):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    base_runner = deps.runner

    def runner(argv, **kwargs):
        if str(argv[-1]) == "--version":
            if isinstance(version_result, BaseException):
                raise version_result
            return version_result
        return base_runner(argv, **kwargs)

    deps.runner = runner
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.reason == reason
    assert result.outcome is ActivationOutcome.GUIDANCE_ONLY
    assert not result.attempted
    assert "secret" not in repr(result)
    assert all(event[0] != "prompt" for event in events)


@pytest.mark.parametrize("kind", ["unset", "relative", "missing", "inside", "directory"])
def test_activate_rejects_invalid_validator_after_consent(tmp_path, kind):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    if kind == "unset":
        deps.environ.pop("GRAPHITE_PACKAGE_VALIDATOR")
    elif kind == "relative":
        deps.environ["GRAPHITE_PACKAGE_VALIDATOR"] = "validator.cjs"
    elif kind == "missing":
        deps.environ["GRAPHITE_PACKAGE_VALIDATOR"] = str(tmp_path / "missing.cjs")
    elif kind == "inside":
        path = root / "validator.cjs"
        path.write_bytes(b"validator")
        deps.environ["GRAPHITE_PACKAGE_VALIDATOR"] = str(path)
    else:
        path = tmp_path / "validator-dir"
        path.mkdir()
        deps.environ["GRAPHITE_PACKAGE_VALIDATOR"] = str(path)

    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.VALIDATION_FAILED
    assert result.reason == "validator_invalid"
    assert result.attempted
    assert [event[0] for event in events] == ["local_probe", "manager_version", "prompt"]


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (ProbeProcessResult(1, b"secret", b"secret", 0), "validator_rejected"),
        (ProbeProcessError("timeout"), "validator_timeout"),
        (ProbeProcessError("output_limit"), "validator_rejected"),
    ],
)
def test_activate_maps_validator_process_failures(tmp_path, failure, reason):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    base_runner = deps.runner
    validator = deps.environ["GRAPHITE_PACKAGE_VALIDATOR"]

    def runner(argv, **kwargs):
        if validator in [str(value) for value in argv]:
            events.append(("validator", kwargs["timeout_seconds"]))
            if isinstance(failure, BaseException):
                raise failure
            return failure
        return base_runner(argv, **kwargs)

    deps.runner = runner
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.VALIDATION_FAILED
    assert result.reason == reason
    assert result.attempted
    assert "secret" not in repr(result)
    assert all(event[0] != "install" for event in events)


@pytest.mark.parametrize(
    "mutation",
    ["manifest", "lock", "source", "config", "new_lock", "shrinkwrap"],
)
def test_activate_revalidates_project_state_after_validator(tmp_path, mutation):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    base_runner = deps.runner
    validator = deps.environ["GRAPHITE_PACKAGE_VALIDATOR"]

    def runner(argv, **kwargs):
        result = base_runner(argv, **kwargs)
        if validator in [str(value) for value in argv]:
            if mutation == "manifest":
                (root / "package.json").write_bytes(b'{"dependencies":{"x":"1.0.0"}}')
            elif mutation == "lock":
                (root / "package-lock.json").write_bytes(b'{"lockfileVersion":2}')
            elif mutation == "source":
                (root / "source.ts").unlink()
            elif mutation == "config":
                (root / ".npmrc").write_text("unsafe", encoding="utf-8")
            elif mutation == "new_lock":
                (root / "pnpm-lock.yaml").write_bytes(_SAFE_LOCKS["pnpm-lock.yaml"])
            else:
                (root / "npm-shrinkwrap.json").write_text("{}", encoding="utf-8")
        return result

    deps.runner = runner
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    assert result.reason == "project_state_changed"
    assert result.attempted
    assert all(event[0] != "install" for event in events)


def test_activate_detects_validator_replacement_before_execution(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    validator = Path(deps.environ["GRAPHITE_PACKAGE_VALIDATOR"])

    monotonic_calls = 0

    def monotonic():
        nonlocal monotonic_calls
        monotonic_calls += 1
        if monotonic_calls == 2:
            validator.write_bytes(b"replacement validator")
        return float(monotonic_calls)

    deps.monotonic = monotonic
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.VALIDATION_FAILED
    assert result.reason == "validator_changed"
    assert result.attempted
    assert all(event[0] != "validator" for event in events)


class _FakeTemporaryDirectory:
    def __init__(self, path):
        self.name = str(path)
        self.cleaned = False

    def cleanup(self):
        self.cleaned = True


def test_activate_rejects_isolated_home_inside_root(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    inside = root / "isolation"
    inside.mkdir()
    temporary = _FakeTemporaryDirectory(inside)
    deps.temporary_directory = lambda: temporary

    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    assert result.reason == "isolation_unavailable"
    assert result.attempted
    assert inside.is_dir()
    assert all(event[0] != "install" for event in events)


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (ProbeProcessResult(1, b"secret", b"secret", 0), "install_failed"),
        (ProbeProcessError("timeout"), "install_timeout"),
        (ProbeProcessError("output_limit"), "install_failed"),
    ],
)
def test_activate_maps_install_failures_without_output_leaks(tmp_path, failure, reason):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    base_runner = deps.runner

    def runner(argv, **kwargs):
        values = [str(value) for value in argv]
        if "install" in values or "add" in values:
            events.append(("install", kwargs["timeout_seconds"]))
            if isinstance(failure, BaseException):
                raise failure
            return failure
        return base_runner(argv, **kwargs)

    deps.runner = runner
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    assert result.reason == reason
    assert result.changed_files == ()
    assert "secret" not in repr(result)


@pytest.mark.parametrize("mutation", ["manifest_missing", "lock_missing", "new_lock", "config"])
def test_activate_fails_closed_on_unsafe_postinstall_state(tmp_path, mutation):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    base_runner = deps.runner

    def runner(argv, **kwargs):
        result = base_runner(argv, **kwargs)
        values = [str(value) for value in argv]
        if "install" in values or "add" in values:
            if mutation == "manifest_missing":
                (root / "package.json").unlink()
            elif mutation == "lock_missing":
                (root / "package-lock.json").unlink()
            elif mutation == "new_lock":
                (root / "pnpm-lock.yaml").write_bytes(_SAFE_LOCKS["pnpm-lock.yaml"])
            else:
                (root / ".npmrc").write_text("unsafe", encoding="utf-8")
        return result

    deps.runner = runner
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    expected = "control_file_unsafe" if mutation.endswith("missing") else "project_state_changed"
    assert result.reason == expected
    assert result.attempted
    assert set(result.changed_files) <= {"package.json", "package-lock.json"}


@pytest.mark.parametrize("escape", [False, True])
def test_activate_requires_verified_local_typescript(tmp_path, escape):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events, verify_ok=False)
    if escape:
        base_runner = deps.runner

        def runner(argv, **kwargs):
            result = base_runner(argv, **kwargs)
            if "-e" in [str(value) for value in argv] and sum(
                event[0] in {"local_probe", "verify"} for event in events
            ) == 2:
                outside = tmp_path / "outside-package.json"
                outside.write_bytes(b"{}")
                return ProbeProcessResult(
                    0, json.dumps({"resolved": str(outside)}).encode(), b"", 0
                )
            return result

        deps.runner = runner

    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.VERIFICATION_FAILED
    assert result.reason == "typescript_not_available"
    assert result.attempted
    assert result.changed_files == ("package-lock.json", "package.json")


def test_activate_shared_deadline_passes_positive_decreasing_budgets(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    tick = -1

    def monotonic():
        nonlocal tick
        tick += 1
        return float(tick)

    deps.monotonic = monotonic
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False, 30), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLED
    budgets = [
        value
        for phase, value in events
        if phase in {"validator", "install", "verify"}
    ]
    assert all(value > 0 for value in budgets)
    assert budgets == sorted(budgets, reverse=True)
    assert len(set(budgets)) == 3


def test_activate_deadline_expiry_before_validator_is_phase_specific(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    values = iter([0.0, 121.0])
    deps.monotonic = lambda: next(values)

    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False, 120), deps
    )

    assert result.outcome is ActivationOutcome.VALIDATION_FAILED
    assert result.reason == "operation_timeout"
    assert result.attempted
    assert all(event[0] != "validator" for event in events)


def test_activate_deadline_expiry_during_post_state_is_operation_timeout(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    calls = 0

    def monotonic():
        nonlocal calls
        calls += 1
        return 31.0 if calls >= 5 else 0.0

    deps.monotonic = monotonic
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False, 30), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    assert result.reason == "operation_timeout"
    assert result.changed_files == ("package-lock.json", "package.json")


def test_activate_deadline_expiry_before_install_is_operation_timeout(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    calls = 0

    def monotonic():
        nonlocal calls
        calls += 1
        return 31.0 if calls >= 4 else 0.0

    deps.monotonic = monotonic
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False, 30), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    assert result.reason == "operation_timeout"
    assert result.changed_files == ()


def test_activate_deadline_expiry_during_verification_is_operation_timeout(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    calls = 0

    def monotonic():
        nonlocal calls
        calls += 1
        return 31.0 if calls >= 8 else 0.0

    deps.monotonic = monotonic
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False, 30), deps
    )

    assert result.outcome is ActivationOutcome.VERIFICATION_FAILED
    assert result.reason == "operation_timeout"
    assert result.changed_files == ("package-lock.json", "package.json")


def test_activate_same_root_lock_is_nonblocking_and_released(tmp_path):
    root = _activation_root(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    events = []
    deps = _activation_dependencies(tmp_path, root, events)

    def prompt(message):
        entered.set()
        assert release.wait(5)
        return "n"

    deps.prompt = prompt
    first_result = []
    thread = threading.Thread(
        target=lambda: first_result.append(
            activate_typescript(
                ActivationRequest(root, Config(), True, True, False, False), deps
            )
        )
    )
    thread.start()
    assert entered.wait(5)

    second = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False),
        ActivationDependencies(runner=lambda *_a, **_k: pytest.fail("runner called")),
    )
    release.set()
    thread.join(5)

    assert second == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY, None, "activation_in_progress"
    )
    assert first_result[0].outcome is ActivationOutcome.DECLINED
    third_events = []
    third_deps = _activation_dependencies(tmp_path, root, third_events)
    third = activate_typescript(
        ActivationRequest(root, Config(), False, False, False, False), third_deps
    )
    assert third.reason == "non_interactive"


def test_activate_contains_prompt_failure_and_releases_lock(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    secret = f"secret prompt path {tmp_path}"
    deps = _activation_dependencies(
        tmp_path, root, events, prompt_answer=RuntimeError(secret)
    )

    failed = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert failed == ActivationResult(
        ActivationOutcome.DECLINED,
        Manager.NPM,
        "prompt_failed",
        "package.json",
        "package-lock.json",
    )
    assert secret not in repr(failed)
    assert [event[0] for event in events] == [
        "local_probe",
        "manager_version",
        "prompt",
    ]

    second_events = []
    deps = _activation_dependencies(
        tmp_path, root, second_events, prompt_answer="n"
    )
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )
    assert result.outcome is ActivationOutcome.DECLINED


def test_activate_contains_prompt_response_parsing_failure(tmp_path):
    root = _activation_root(tmp_path)
    events = []

    class ExplodingAnswer(str):
        def strip(self, *args, **kwargs):
            raise RuntimeError(f"secret response path {tmp_path}")

    deps = _activation_dependencies(
        tmp_path,
        root,
        events,
        prompt_answer=ExplodingAnswer("yes"),
    )
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.DECLINED
    assert result.reason == "prompt_failed"
    assert str(tmp_path) not in repr(result)
    assert all(event[0] not in {"validator", "install"} for event in events)


@pytest.mark.parametrize("mutation", ["config", "new_lock", "shrinkwrap", "manifest"])
def test_activate_revalidates_after_temporary_factory_before_install(
    tmp_path, mutation
):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    temporary = tempfile.TemporaryDirectory(dir=tmp_path)

    def temporary_factory():
        if mutation == "config":
            (root / ".npmrc").write_text("unsafe", encoding="utf-8")
        elif mutation == "new_lock":
            (root / "pnpm-lock.yaml").write_bytes(_SAFE_LOCKS["pnpm-lock.yaml"])
        elif mutation == "shrinkwrap":
            (root / "npm-shrinkwrap.json").write_text("{}", encoding="utf-8")
        else:
            (root / "package.json").write_bytes(b'{"dependencies":{"x":"1.0.0"}}')
        return temporary

    deps.temporary_directory = temporary_factory
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    assert result.reason == "project_state_changed"
    assert result.attempted
    assert all(event[0] != "install" for event in events)
    assert not Path(temporary.name).exists()


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (ProbeProcessResult(1, b"secret", b"secret", 0), "install_failed"),
        (ProbeProcessError("timeout"), "install_timeout"),
    ],
)
def test_activate_inspects_changed_controls_after_failed_install(
    tmp_path, failure, reason
):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    base_runner = deps.runner

    def runner(argv, **kwargs):
        values = [str(value) for value in argv]
        if "install" in values or "add" in values:
            events.append(("install", kwargs["timeout_seconds"]))
            (root / "package.json").write_bytes(
                b'{"devDependencies":{"typescript":"^5.1.0"}}'
            )
            (root / "package-lock.json").write_bytes(
                b'{"lockfileVersion":3,"packages":{}}'
            )
            if isinstance(failure, BaseException):
                raise failure
            return failure
        return base_runner(argv, **kwargs)

    deps.runner = runner
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    assert result.reason == reason
    assert result.changed_files == ("package-lock.json", "package.json")
    assert "secret" not in repr(result)
    assert [event[0] for event in events].count("install") == 1
    assert all(event[0] != "verify" for event in events)


@pytest.mark.parametrize("control", ["package.json", "package-lock.json"])
def test_activate_reports_unsafe_control_after_failed_install(tmp_path, control):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    base_runner = deps.runner

    def runner(argv, **kwargs):
        values = [str(value) for value in argv]
        if "install" in values or "add" in values:
            events.append(("install", kwargs["timeout_seconds"]))
            path = root / control
            path.unlink()
            path.mkdir()
            return ProbeProcessResult(1, b"secret", b"secret", 0)
        return base_runner(argv, **kwargs)

    deps.runner = runner
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    assert result.reason == "control_file_unsafe"
    assert result.changed_files == (control,)
    assert all(event[0] != "verify" for event in events)


@pytest.mark.parametrize("unsafe", ["config", "new_lock", "shrinkwrap"])
def test_activate_reports_new_negative_state_after_failed_install(tmp_path, unsafe):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    base_runner = deps.runner

    def runner(argv, **kwargs):
        values = [str(value) for value in argv]
        if "install" in values or "add" in values:
            events.append(("install", kwargs["timeout_seconds"]))
            if unsafe == "config":
                (root / ".npmrc").write_text("unsafe", encoding="utf-8")
            elif unsafe == "new_lock":
                (root / "pnpm-lock.yaml").write_bytes(_SAFE_LOCKS["pnpm-lock.yaml"])
            else:
                (root / "npm-shrinkwrap.json").write_text("{}", encoding="utf-8")
            return ProbeProcessResult(1, b"secret", b"secret", 0)
        return base_runner(argv, **kwargs)

    deps.runner = runner
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    assert result.reason == "project_state_changed"
    assert all(event[0] != "verify" for event in events)


@pytest.mark.parametrize("phase", ["failed_install", "prelaunch_revalidation"])
def test_activate_cleanup_error_overrides_every_provisional_result(tmp_path, phase):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events, install_ok=False)
    base_runner = deps.runner

    def runner(argv, **kwargs):
        process_result = base_runner(argv, **kwargs)
        values = [str(value) for value in argv]
        if "install" in values or "add" in values:
            (root / "package.json").write_bytes(
                b'{"devDependencies":{"typescript":"^5.1.0"}}'
            )
            (root / "package-lock.json").write_bytes(
                b'{"lockfileVersion":3,"packages":{}}'
            )
        return process_result

    deps.runner = runner
    real_temporary = tempfile.TemporaryDirectory(dir=tmp_path)
    cleanup_calls = 0

    def factory():
        if phase == "prelaunch_revalidation":
            (root / ".npmrc").write_text("unsafe", encoding="utf-8")
        return real_temporary

    def cleanup(isolated_home, _selected_root, _timeout):
        nonlocal cleanup_calls
        cleanup_calls += 1
        shutil.rmtree(isolated_home)
        return StepResult(False, "cleanup_failed")

    deps.temporary_directory = factory
    deps.cleanup = cleanup
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    assert result.reason == "isolation_cleanup_failed"
    assert result.attempted
    assert cleanup_calls == 1
    assert str(tmp_path) not in repr(result)
    expected_installs = 0 if phase == "prelaunch_revalidation" else 1
    assert [event[0] for event in events].count("install") == expected_installs
    expected_changed = (
        ()
        if phase == "prelaunch_revalidation"
        else ("package-lock.json", "package.json")
    )
    assert result.changed_files == expected_changed


@pytest.mark.parametrize("phase", ["failed_install", "prelaunch_revalidation"])
def test_activate_cleanup_deadline_overrides_every_provisional_result(tmp_path, phase):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events, install_ok=False)
    real_temporary = tempfile.TemporaryDirectory(dir=tmp_path)
    now = 0.0
    cleanup_calls = 0

    def factory():
        if phase == "prelaunch_revalidation":
            (root / ".npmrc").write_text("unsafe", encoding="utf-8")
        return real_temporary

    def cleanup(isolated_home, _selected_root, _timeout):
        nonlocal cleanup_calls, now
        cleanup_calls += 1
        shutil.rmtree(isolated_home)
        now = 31.0
        return StepResult(True, "cleaned")

    deps.monotonic = lambda: now
    deps.temporary_directory = factory
    deps.cleanup = cleanup
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False, 30), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    assert result.reason == "operation_timeout"
    assert result.attempted
    assert cleanup_calls == 1
    expected_installs = 0 if phase == "prelaunch_revalidation" else 1
    assert [event[0] for event in events].count("install") == expected_installs


def test_activate_maps_terminated_cleanup_timeout_without_worker_retention(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    before_threads = {thread.ident for thread in threading.enumerate()}
    cleanup_calls = 0

    def cleanup(_isolated_home, _selected_root, timeout):
        nonlocal cleanup_calls
        cleanup_calls += 1
        assert 0 < timeout <= 1
        return StepResult(False, "cleanup_timeout")

    deps.cleanup = cleanup
    started = time.monotonic()
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.75
    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    assert result.reason == "operation_timeout"
    assert cleanup_calls == 1
    assert {thread.ident for thread in threading.enumerate()} == before_threads
    assert typescript_activation._ACTIVE_CLEANUP_ROOTS == set()


def test_blocked_cleanup_does_not_serialize_separate_roots(tmp_path):
    first_parent = tmp_path / "first"
    second_parent = tmp_path / "second"
    first_parent.mkdir()
    second_parent.mkdir()
    first_root = _activation_root(first_parent)
    second_root = _activation_root(second_parent)
    first_events = []
    first_dependencies = _activation_dependencies(
        first_parent,
        first_root,
        first_events,
    )
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    first_result = []

    def blocking_cleanup(isolated_home, _selected_root, _timeout):
        cleanup_started.set()
        assert release_cleanup.wait(5)
        shutil.rmtree(isolated_home)
        return StepResult(True, "cleaned")

    first_dependencies.cleanup = blocking_cleanup
    thread = threading.Thread(
        target=lambda: first_result.append(
            activate_typescript(
                ActivationRequest(
                    first_root,
                    Config(),
                    True,
                    True,
                    False,
                    False,
                ),
                first_dependencies,
            )
        )
    )
    thread.start()
    try:
        assert cleanup_started.wait(5)
        same_root = activate_typescript(
            ActivationRequest(first_root, Config(), True, True, False, False),
            ActivationDependencies(
                runner=lambda *_args, **_kwargs: pytest.fail("runner called")
            ),
        )
        second_events = []
        second_dependencies = _activation_dependencies(
            second_parent,
            second_root,
            second_events,
        )
        second = activate_typescript(
            ActivationRequest(second_root, Config(), True, True, False, False),
            second_dependencies,
        )
    finally:
        release_cleanup.set()
        thread.join(5)

    assert same_root == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY,
        None,
        "activation_in_progress",
    )
    assert first_result[0].outcome is ActivationOutcome.INSTALLED
    assert second.outcome is ActivationOutcome.INSTALLED


def test_cleanup_ownership_blocks_root_before_any_activation_work(tmp_path):
    root = _activation_root(tmp_path)
    typescript_activation._mark_cleanup_active(root)
    try:
        result = activate_typescript(
            ActivationRequest(root, Config(), True, True, False, False),
            ActivationDependencies(
                prompt=lambda _message: pytest.fail("prompt called"),
                environ=_RaisingEnvironment({}, "PATH", "environment accessed"),
                runner=lambda *_args, **_kwargs: pytest.fail("runner called"),
            ),
        )
    finally:
        typescript_activation._clear_cleanup_active(root)

    assert result == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY,
        None,
        "activation_in_progress",
    )


class _RaisingEnvironment(dict):
    def __init__(self, values, failing_key, secret):
        super().__init__(values)
        self.failing_key = failing_key
        self.secret = secret

    def get(self, key, default=None):
        if key == self.failing_key:
            raise RuntimeError(self.secret)
        return super().get(key, default)


def test_activate_contains_path_environment_exception_before_consent(tmp_path):
    root = _activation_root(tmp_path)
    secret = f"secret environment path {tmp_path}"
    dependencies = ActivationDependencies(
        environ=_RaisingEnvironment({}, "PATH", secret),
        runner=lambda *_args, **_kwargs: pytest.fail("runner called"),
    )

    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), dependencies
    )

    assert result == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY,
        None,
        "dependency_failed",
    )
    assert secret not in repr(result)


def test_activate_contains_validator_environment_exception_after_consent(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    dependencies = _activation_dependencies(tmp_path, root, events)
    secret = f"secret validator environment path {tmp_path}"
    dependencies.environ = _RaisingEnvironment(
        dependencies.environ,
        "GRAPHITE_PACKAGE_VALIDATOR",
        secret,
    )

    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), dependencies
    )

    assert result.outcome is ActivationOutcome.VALIDATION_FAILED
    assert result.reason == "dependency_failed"
    assert result.attempted
    assert secret not in repr(result)
    assert all(event[0] not in {"validator", "install"} for event in events)


@pytest.mark.parametrize(
    ("failure_call", "outcome"),
    [
        (1, ActivationOutcome.VALIDATION_FAILED),
        (2, ActivationOutcome.VALIDATION_FAILED),
        (4, ActivationOutcome.INSTALLATION_FAILED),
    ],
)
def test_activate_contains_monotonic_exception_by_phase(
    tmp_path, failure_call, outcome
):
    root = _activation_root(tmp_path)
    events = []
    dependencies = _activation_dependencies(tmp_path, root, events)
    secret = f"secret monotonic path {tmp_path}"
    calls = 0

    def monotonic():
        nonlocal calls
        calls += 1
        if calls >= failure_call:
            raise RuntimeError(secret)
        return float(calls)

    dependencies.monotonic = monotonic
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), dependencies
    )

    assert result.outcome is outcome
    assert result.reason == "dependency_failed"
    assert result.attempted
    assert secret not in repr(result)
    assert all(event[0] != "install" for event in events)


def test_verification_dependency_failure_survives_cleanup_clock_failure(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    dependencies = _activation_dependencies(tmp_path, root, events)
    secret = f"secret verification clock path {tmp_path}"
    calls = 0

    def monotonic():
        nonlocal calls
        calls += 1
        if calls >= 7:
            raise RuntimeError(secret)
        return float(calls)

    dependencies.monotonic = monotonic
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), dependencies
    )

    assert result.outcome is ActivationOutcome.VERIFICATION_FAILED
    assert result.reason == "dependency_failed"
    assert secret not in repr(result)


def test_cleanup_timeout_stress_retains_no_workers_or_root_ownership(tmp_path):
    before_threads = {thread.ident for thread in threading.enumerate()}
    cleanup_calls = 0

    def terminated_never_returning_cleanup(_isolated_home, _root, timeout):
        nonlocal cleanup_calls
        cleanup_calls += 1
        assert 0 < timeout <= 1
        return StepResult(False, "cleanup_timeout")

    for index in range(64):
        root = tmp_path / f"root-{index}"
        isolated = tmp_path / f"isolated-{index}"
        root.mkdir()
        isolated.mkdir()
        deadline = typescript_activation._Deadline(1.0, lambda: 0.0)
        status, expired_before = typescript_activation._bounded_temporary_cleanup(
            isolated,
            deadline,
            root,
            terminated_never_returning_cleanup,
        )
        assert status == "cleanup_timeout"
        assert not expired_before
        isolated.rmdir()

    assert cleanup_calls == 64
    assert typescript_activation._ACTIVE_CLEANUP_ROOTS == set()
    assert {thread.ident for thread in threading.enumerate()} == before_threads


def test_default_cleanup_uses_exact_bounded_process_contract(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        raise ProbeProcessError("timeout")

    monkeypatch.setattr(typescript_activation, "run_bounded_process", runner)
    result = typescript_activation._cleanup_isolated_home(isolated, root, 0.25)

    assert result == StepResult(False, "cleanup_timeout")
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:3] == [sys.executable, "-I", "-c"]
    assert argv[-1] == str(isolated)
    assert kwargs["cwd"] == isolated.parent
    assert kwargs["stdin"] is None
    assert kwargs["timeout_seconds"] == 0.25
    assert kwargs["check"] is False
    assert "shell" not in kwargs


def test_activate_separate_roots_do_not_share_lock(tmp_path):
    first_parent = tmp_path / "first"
    second_parent = tmp_path / "second"
    first_parent.mkdir()
    second_parent.mkdir()
    first_root = _activation_root(first_parent)
    second_root = _activation_root(second_parent)
    entered = threading.Event()
    release = threading.Event()
    first_events = []
    first_deps = _activation_dependencies(first_parent, first_root, first_events)

    def prompt(_message):
        entered.set()
        assert release.wait(5)
        return "n"

    first_deps.prompt = prompt
    thread = threading.Thread(
        target=lambda: activate_typescript(
            ActivationRequest(first_root, Config(), True, True, False, False),
            first_deps,
        )
    )
    thread.start()
    assert entered.wait(5)
    second_events = []
    second_deps = _activation_dependencies(second_parent, second_root, second_events)

    second = activate_typescript(
        ActivationRequest(second_root, Config(), False, False, False, False),
        second_deps,
    )
    release.set()
    thread.join(5)

    assert second.reason == "non_interactive"
    assert [event[0] for event in second_events] == ["local_probe"]


def test_activate_rejects_symlinked_isolation_directory(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    outside = tmp_path / "outside-isolation"
    outside.mkdir()
    link = tmp_path / "isolation-link"
    _symlink_or_skip(link, outside)
    temporary = _FakeTemporaryDirectory(link)
    deps.temporary_directory = lambda: temporary

    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    assert result.reason == "isolation_unavailable"
    assert all(event[0] != "install" for event in events)


def test_activate_revalidates_node_after_consent(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    tools = Path(deps.environ["PATH"])
    node = tools / ("node.exe" if os.name == "nt" else "node")

    def prompt(message):
        events.append(("prompt", message))
        node.write_bytes(b"replaced node")
        return "yes"

    deps.prompt = prompt
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.VALIDATION_FAILED
    assert result.reason == "executable_changed"
    assert all(event[0] != "validator" for event in events)


def test_activate_revalidates_manager_command_before_install(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    tools = Path(deps.environ["PATH"])
    manager_file = (
        tools / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if os.name == "nt"
        else tools / "npm"
    )
    validator = deps.environ["GRAPHITE_PACKAGE_VALIDATOR"]
    base_runner = deps.runner

    def runner(argv, **kwargs):
        result = base_runner(argv, **kwargs)
        if validator in [str(value) for value in argv]:
            manager_file.write_bytes(b"replaced manager")
        return result

    deps.runner = runner
    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    assert result.reason == "command_changed"
    assert result.attempted
    assert all(event[0] != "install" for event in events)


def test_activate_shared_deadline_includes_temporary_cleanup(tmp_path):
    root = _activation_root(tmp_path)
    events = []
    deps = _activation_dependencies(tmp_path, root, events)
    now = 0.0
    real_temporary = tempfile.TemporaryDirectory(dir=tmp_path)

    def cleanup(isolated_home, _selected_root, _timeout):
        nonlocal now
        shutil.rmtree(isolated_home)
        now = 31.0
        return StepResult(True, "cleaned")

    deps.monotonic = lambda: now
    deps.temporary_directory = lambda: real_temporary
    deps.cleanup = cleanup

    result = activate_typescript(
        ActivationRequest(root, Config(), True, True, False, False, 30), deps
    )

    assert result.outcome is ActivationOutcome.INSTALLATION_FAILED
    assert result.reason == "operation_timeout"
    assert result.changed_files == ("package-lock.json", "package.json")


def _file(path: Path, content: bytes = b"tool") -> TrustedFile:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)
    stat = path.stat()
    return TrustedFile(
        path.resolve(),
        (stat.st_dev, stat.st_ino),
        stat.st_size,
        stat.st_mtime_ns,
        hashlib.sha256(content).hexdigest(),
    )


def _command(path: Path) -> TrustedCommand:
    reference = _file(path)
    return TrustedCommand((str(reference.path),), (reference,))


@pytest.mark.parametrize(
    ("manager", "version", "supported", "locks", "tail", "unsafe", "automatic"),
    [
        (
            Manager.NPM,
            Version(8, 0, 0),
            True,
            ("package-lock.json",),
            ("install", "--save-dev", "--ignore-scripts", "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org/", "typescript"),
            (".npmrc", "npm-shrinkwrap.json"),
            True,
        ),
        (
            Manager.NPM,
            Version(11, 9, 1),
            True,
            ("package-lock.json",),
            ("install", "--save-dev", "--ignore-scripts", "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org/", "typescript"),
            (".npmrc", "npm-shrinkwrap.json"),
            True,
        ),
        (
            Manager.PNPM,
            Version(11, 0, 0),
            True,
            ("pnpm-lock.yaml",),
            ("add", "--save-dev", "--ignore-scripts", "--ignore-workspace-root-check", "--registry=https://registry.npmjs.org/", "typescript"),
            (".npmrc", "pnpm-workspace.yaml", ".pnpmfile.cjs", ".pnpmfile.mjs"),
            True,
        ),
        (
            Manager.YARN,
            Version(4, 0, 0),
            False,
            ("yarn.lock",),
            ("add", "--dev", "--mode=skip-build", "typescript"),
            (".yarnrc.yml", ".yarnrc", ".yarn/plugins"),
            False,
        ),
        (
            Manager.BUN,
            Version(1, 2, 3),
            True,
            ("bun.lock", "bun.lockb"),
            ("add", "--dev", "--ignore-scripts", "--registry", "https://registry.npmjs.org/", "typescript"),
            (".npmrc", "bunfig.toml"),
            True,
        ),
        (
            Manager.PNPM,
            Version(10, 0, 0),
            False,
            ("pnpm-lock.yaml",),
            ("add", "--save-dev", "--ignore-scripts", "--ignore-workspace-root-check", "--registry=https://registry.npmjs.org/", "typescript"),
            (".npmrc", "pnpm-workspace.yaml", ".pnpmfile.cjs", ".pnpmfile.mjs"),
            True,
        ),
    ],
)
def test_adapter_matrix(manager, version, supported, locks, tail, unsafe, automatic):
    adapter = adapter_for(manager)
    assert adapter.lockfiles == locks
    assert adapter.argument_tail("https://registry.npmjs.org/") == tail
    assert adapter.unsafe_root_files == unsafe
    assert adapter.automatic is automatic
    assert adapter.supports(version) is supported


@pytest.mark.parametrize("value", ["1.2", "1.2.3.4", "1.2.x", "v 1.2.3", "1.2.3 junk", "1.2.3-", "01.2.3", "", " 1.2.3 x "])
def test_parse_version_rejects_non_exact_forms(value):
    assert parse_version(value) is None


def test_parse_version_accepts_semver_suffix_and_outer_whitespace():
    assert parse_version(" v1.2.3-rc.1+build.7\n") == Version(1, 2, 3)


@pytest.mark.parametrize(
    ("manager", "manager_environment"),
    [
        (
            Manager.NPM,
            {
                "NPM_CONFIG_USERCONFIG": "npmrc",
                "NPM_CONFIG_GLOBALCONFIG": "npm-globalconfig",
                "NPM_CONFIG_CACHE": "npm-cache",
                "NPM_CONFIG_PREFIX": "npm-prefix",
                "NPM_CONFIG_REGISTRY": TRUSTED_REGISTRY,
                "NPM_CONFIG_IGNORE_SCRIPTS": "true",
                "NPM_CONFIG_AUDIT": "false",
                "NPM_CONFIG_FUND": "false",
                "npm_config_registry": TRUSTED_REGISTRY,
                "npm_config_ignore_scripts": "true",
            },
        ),
        (
            Manager.PNPM,
            {
                "NPM_CONFIG_USERCONFIG": "npmrc",
                "NPM_CONFIG_GLOBALCONFIG": "npm-globalconfig",
                "NPM_CONFIG_CACHE": "npm-cache",
                "NPM_CONFIG_PREFIX": "npm-prefix",
                "NPM_CONFIG_REGISTRY": TRUSTED_REGISTRY,
                "NPM_CONFIG_IGNORE_SCRIPTS": "true",
                "NPM_CONFIG_AUDIT": "false",
                "NPM_CONFIG_FUND": "false",
                "npm_config_registry": TRUSTED_REGISTRY,
                "npm_config_ignore_scripts": "true",
                "PNPM_HOME": "pnpm-home",
                "PNPM_STORE_DIR": "pnpm-store",
                "PNPM_CONFIG_DIR": "pnpm-config",
                "npm_config_store_dir": "pnpm-store",
            },
        ),
        (
            Manager.YARN,
            {
                "YARN_NPM_REGISTRY_SERVER": TRUSTED_REGISTRY,
                "YARN_ENABLE_SCRIPTS": "false",
                "YARN_ENABLE_TELEMETRY": "0",
                "YARN_GLOBAL_FOLDER": "yarn-global",
            },
        ),
        (Manager.BUN, {"BUN_INSTALL_CACHE_DIR": "bun-cache"}),
    ],
)
def test_install_environment_is_exact_for_each_manager(
    tmp_path, manager, manager_environment, monkeypatch
):
    trusted_windows = {
        "SYSTEMROOT": r"C:\TrustedWindows",
        "WINDIR": r"C:\TrustedWindows",
        "COMSPEC": r"C:\TrustedWindows\System32\cmd.exe",
        "PATHEXT": ".COM;.EXE",
    }
    monkeypatch.setattr(
        dependency_install, "_trusted_windows_system_environment", lambda: trusted_windows
    )
    source = {
        "SYSTEMROOT": r"C:\hostile",
        "WINDIR": r"C:\hostile",
        "COMSPEC": str(tmp_path / "repo-controlled" / "cmd.exe"),
        "LANG": "en_GB.UTF-8",
        "PATH": str(tmp_path / "repo-controlled"),
        "NPM_TOKEN": "secret",
        "NODE_AUTH_TOKEN": "secret",
        "HTTP_PROXY": "http://secret",
        "HTTPS_PROXY": "http://secret",
        "ALL_PROXY": "http://secret",
        "GRAPHITE_API_KEY": "secret",
        "GRAPHITE_LLM_API_KEY": "secret",
        "OPENAI_API_KEY": "secret",
    }
    tools = tmp_path / "tools"
    tools.mkdir()
    home = tmp_path / "isolated"
    base = home.resolve()
    env = build_install_environment(manager, home, (tools,), TRUSTED_REGISTRY, source)
    expected_path = [str(tools.resolve())]
    expected_path.extend(
        [str(Path(trusted_windows["SYSTEMROOT"]) / "System32")]
        if os.name == "nt"
        else ["/usr/bin", "/bin"]
    )
    common = {
        "LANG": source["LANG"],
        "PATH": os.pathsep.join(expected_path),
        "HOME": str(base / "home"),
        "USERPROFILE": str(base / "home"),
        "XDG_CONFIG_HOME": str(base / "config"),
        "XDG_CACHE_HOME": str(base / "cache"),
        "TEMP": str(base / "tmp"),
        "TMP": str(base / "tmp"),
        "APPDATA": str(base / "appdata"),
        "LOCALAPPDATA": str(base / "localappdata"),
    }
    if os.name == "nt":
        common |= trusted_windows
    expected_manager = {
        key: (
            str(base / "npm-config" / "user.npmrc")
            if value == "npmrc"
            else str(base / "npm-config" / "global.npmrc")
            if value == "npm-globalconfig"
            else str(base / value)
            if value
            in {
                "npm-cache",
                "npm-prefix",
                "pnpm-home",
                "pnpm-store",
                "pnpm-config",
                "yarn-global",
                "bun-cache",
            }
            else value
        )
        for key, value in manager_environment.items()
    }
    assert env == common | expected_manager
    assert str(tmp_path / "repo-controlled") not in env["PATH"]
    assert not (
        {
            "NPM_TOKEN",
            "NODE_AUTH_TOKEN",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "GRAPHITE_API_KEY",
            "GRAPHITE_LLM_API_KEY",
            "OPENAI_API_KEY",
        }
        & env.keys()
    )


def test_resolve_and_revalidate_external_executable(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    external = tmp_path / "bin"
    executable = _file(external / ("tool.exe" if os.name == "nt" else "tool"))
    assert resolve_trusted_executable("tool", root, str(external), windows=os.name == "nt") == executable
    assert resolve_trusted_executable("tool", root, f"relative{os.pathsep}{root}", windows=False) is None
    inside = _file(root / ("tool.exe" if os.name == "nt" else "tool"))
    assert resolve_trusted_executable("tool", root, str(inside.path.parent), windows=os.name == "nt") is None


def test_resolve_trusted_external_regular_file(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    external = tmp_path / "validate-packages.cjs"
    expected = _file(external)
    assert resolve_trusted_file(external, root, executable=False) == expected
    inside = root / "package.json"
    _file(inside)
    assert resolve_trusted_file(inside, root, executable=False) is None


def test_provenance_rejects_non_file_and_escaping_symlink(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    directory = tmp_path / "external-directory"
    directory.mkdir()
    assert resolve_trusted_file(directory, root, executable=False) is None
    target = tmp_path / "external-tool"
    _file(target)
    link = root / "escaping-tool"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error.__class__.__name__}")
    assert resolve_trusted_file(link, root, executable=False) is None


def test_windows_resolution_rejects_batch_and_builds_npm_node_prefix(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    bin_dir = tmp_path / "node"
    _file(bin_dir / "npm.cmd")
    node = _file(bin_dir / "node.exe")
    cli = _file(bin_dir / "node_modules" / "npm" / "bin" / "npm-cli.js")
    assert resolve_trusted_executable("npm", root, str(bin_dir), windows=True) is None
    prefix = resolve_windows_npm_prefix(root, str(bin_dir))
    assert prefix == TrustedCommand((str(node.path), str(cli.path)), (node, cli))
    assert not any(value.lower().endswith((".cmd", ".bat")) for value in prefix.argv)


def test_snapshot_control_file_rejects_symlink_and_detects_content(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    manifest = root / "package.json"
    manifest.write_bytes(b'{"name":"safe"}')
    snapshot = snapshot_control_file(root, "package.json")
    assert snapshot.relative_path == "package.json"
    assert len(snapshot.sha256) == 64
    with pytest.raises(ValueError, match="control_file_invalid"):
        snapshot_control_file(root, "../outside")
    target = tmp_path / "target"
    target.write_text("x", encoding="utf-8")
    link = root / "linked.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="control_file_invalid"):
        snapshot_control_file(root, "linked.json")


@pytest.mark.parametrize(
    ("manifest", "lockfile", "expected"),
    [
        (b'{"dependencies":{"x":"^1.0.0"},"devDependencies":{"typescript":"latest"}}', b'{"lockfileVersion":3,"resolved":"https://registry.npmjs.org/x/-/x-1.0.0.tgz"}', True),
        (b'{"dependencies":{"x":"file:../x"}}', b"", False),
        (b'{"dependencies":{"x":"../x"}}', b"", False),
        (b'{"dependencies":{"x":"./../outside"}}', b"", False),
        (b'{"dependencies":{"x":"git@github.com:org/repo.git"}}', b"", False),
        (b'{"dependencies":{"x":"org/repo"}}', b"", False),
        (b'{"dependencies":{"x":"payload.tgz"}}', b'{"lockfileVersion":3}', False),
        (b'{"dependencies":{"x":"C:\\\\outside"}}', b"", False),
        (b'{"dependencies":{"x":"\\\\\\\\server\\\\share"}}', b"", False),
        (b'{"workspaces":["packages/*"]}', b"", False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"lockfileVersion":3,"resolved":"https://evil.example/x.tgz"}', False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"lockfileVersion":3,"resolved":"ftp://registry.npmjs.org/x.tgz"}', False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"lockfileVersion":3,"resolved":"custom://registry.npmjs.org/x.tgz"}', False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"lockfileVersion":3,"resolved":"https:\\/\\/evil.example/x.tgz"}', False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"lockfileVersion":3,"resolved":"https\\u003a//evil.example/x.tgz"}', False),
        (b'{"dependencies":{"x":"1.0.0"}}', json.dumps({"lockfileVersion": 3, "resolved": r"https:\\evil.example\x"}).encode(), False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"lockfileVersion":3,"resolved":"git+ssh://example/x"}', False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"lockfileVersion":3,"resolved":"git://example/x"}', False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"lockfileVersion":3,"resolved":"ssh://example/x"}', False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"lockfileVersion":3,"resolved":"file:../x"}', False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"lockfileVersion":3,"resolved":"link:../x"}', False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"lockfileVersion":3,"resolved":"local:../x"}', False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"lockfileVersion":3,"resolved":"../x"}', False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"lockfileVersion":3,"resolved":"payload.tgz"}', False),
        (b'{"dependencies":{"x":"1.0.0"}}', b"{}", False),
        (b'{"dependencies":{"x":"1.0.0"}}', b"lockfileVersion: '9.0'\npackages: {}\n", True),
        (b'{"dependencies":{"x":"1.0.0"}}', b"lockfileVersion: '9.0'\npackages:\n  x:\n    tarball: payload.tgz\n", False),
        (b'{"dependencies":{"x":"1.0.0"}}', b"lockfileVersion: '9.0'\npackages:\n  x:\n    fetch: mystery\n", False),
        (b'{"dependencies":{"x":"1.0.0"}}', b"lockfileVersion: '9.0'\npackages:\n  x:\n    resolution: {\"tarball\":\"https://registry.npmjs.org/x.tgz\"}\n", True),
        (b'{"dependencies":{"x":"1.0.0"}}', b"lockfileVersion: '9.0'\npackages:\n  x:\n    resolution: {\"tarball\":\"https\\u003a//evil.example/x.tgz\"}\n", False),
        (b'{"dependencies":{"x":"1.0.0"}}', b"lockfileVersion: nope\npackages: {}\n", False),
        (b'{"dependencies":{"x":"1.0.0"}}', b"# yarn lockfile v1\n\nx@1.0.0:\n  version \"1.0.0\"\n  resolved \"https://registry.npmjs.org/x.tgz\"\n", True),
        (b'{"dependencies":{"x":"1.0.0"}}', b"# yarn lockfile v1\n\nx@1.0.0:\n  version \"1.0.0\"\n  resolved \"payload.tgz\"\n", False),
        (b'{"dependencies":{"x":"1.0.0"}}', b"# yarn lockfile v1\n\nx@1.0.0:\n  version \"1.0.0\"\n  resolved \"https\\u003a//evil.example/x.tgz\"\n", False),
        (b'{"dependencies":{"x":"1.0.0"}}', b"# yarn lockfile v1\nnot-an-entry\n", False),
        (b'{"dependencies":{"x":"1.0.0"}}', b"__metadata:\n  version: 8\n\n\"x@npm:^1.0.0\":\n  version: 1.0.0\n  resolution: \"x@npm:1.0.0\"\n", True),
        (b'{"dependencies":{"x":"1.0.0"}}', b"__metadata:\n  malformed: yes\n", False),
        (b'{"dependencies":{"x":"1.0.0"}}', b"", False),
        (b'{"dependencies":{"x":"1.0.0"}}', b" \r\n\t", False),
        (b'{"dependencies":{"x":"1.0.0"}}', b"not a lockfile at all", False),
        (b"not-json", b"", False),
    ],
)
def test_source_policy(manifest, lockfile, expected):
    assert control_files_use_trusted_sources(manifest, lockfile) is expected


def test_source_policy_rejects_malformed_lockfile_encoding():
    assert not control_files_use_trusted_sources(b"{}", b"\xff\x00")


@pytest.mark.parametrize(
    "lockfile",
    [
        b"lockfileVersion: '9.0'\npackages:\n  x:\n    \"tarball\": payload.tgz\n",
        b"lockfileVersion: '9.0'\npackages:\n  x:\n    resolution:\n      'tarball': payload.tgz\n",
        b"lockfileVersion: '9.0'\npackages:\n  x:\n    \"tar\\u0062all\": payload.tgz\n",
        b"__metadata:\n  version: 8\n\n\"x@npm:1\":\n  version: 1\n  \"resolved\": payload.tgz\n",
        b"__metadata:\n  version: 8\n\n\"x@npm:1\":\n  version: 1\n  resolution:\n    'path': payload.tgz\n",
        b"__metadata:\n  version: 8\n\n\"x@npm:1\":\n  version: 1\n  \"u\\u0072l\": payload.tgz\n",
    ],
)
def test_source_policy_rejects_quoted_or_escaped_source_keys(lockfile):
    assert not control_files_use_trusted_sources(b'{"dependencies":{"x":"1.0.0"}}', lockfile)


@pytest.mark.parametrize(
    "lockfile",
    [
        b"lockfileVersion: '9.0'\npackages:\n  x:\n    \"tarball\": https://registry.npmjs.org/x.tgz\n",
        b"__metadata:\n  version: 8\n\n\"x@npm:1\":\n  version: 1\n  'url': https://registry.npmjs.org/x.tgz\n",
    ],
)
def test_source_policy_allows_quoted_source_keys_with_canonical_registry(lockfile):
    assert control_files_use_trusted_sources(b'{"dependencies":{"x":"1.0.0"}}', lockfile)


@pytest.mark.parametrize(
    ("manifest", "lockfile"),
    [
        (b'{"dependencies":{},"score":NaN}', b'{"lockfileVersion":3}'),
        (b'{"dependencies":{},"score":Infinity}', b'{"lockfileVersion":3}'),
        (b'{"dependencies":{},"score":1,"score":2}', b'{"lockfileVersion":3}'),
        (b'{"dependencies":{},"score":' + b"9" * 5000 + b"}", b'{"lockfileVersion":3}'),
        (b'{"dependencies":{}}', b'{"lockfileVersion":3,"score":NaN}'),
        (b'{"dependencies":{}}', b'{"lockfileVersion":3,"score":Infinity}'),
        (b'{"dependencies":{}}', b'{"lockfileVersion":3,"score":1,"score":2}'),
        (b'{"dependencies":{}}', b'{"lockfileVersion":' + b"9" * 5000 + b"}"),
    ],
)
def test_source_policy_strict_json_never_accepts_or_raises(manifest, lockfile):
    assert not control_files_use_trusted_sources(manifest, lockfile)


@pytest.mark.parametrize(
    "lockfile",
    [
        b"lockfileVersion: '9.0'\npackages:\n\tx: {}\n",
        b"lockfileVersion: '9.0'\npackages:\n   x: {}\n",
        b"lockfileVersion: '9.0'\npackages:\n    x: {}\n",
        b"lockfileVersion: '9.0'\npackages:\n  x: [unterminated\n",
        b"lockfileVersion: '9.0'\npackages:\n  x: https\\u00ZZ//evil.example\n",
        b"lockfileVersion: '9.0'\npackages:\n  x: https\\U003A\\U002F\\U002Fevil.example\n",
        b"lockfileVersion: '9.0'\npackages:\n  this is not a field\n",
        b"lockfileVersion: '9.0'\npackages:\n  x:\n    resolution: x: y\n",
        b"# yarn lockfile v1\n\nx@1.0.0:\n version \"1.0.0\"\n",
        b"# yarn lockfile v1\n\nx@1.0.0:\n  version \"unterminated\n",
        b"# yarn lockfile v1\n\nx@1.0.0:\n  nonsense value\n",
        b"# yarn lockfile v1\n\nx@1.0.0:\n  resolved \"https://registry.npmjs.org/x.tgz\"\n",
        b"__metadata:\n  version: 8\n",
        b"__metadata:\n    version: 8\n\n\"x@npm:1\":\n  version: 1\n",
        b"__metadata:\n  version: 8\n\n\"x@npm:1\":\n  broken line\n",
        b"__metadata:\n  version: 8\n\n\"x@npm:1\":\n  version: 1\n  resolution: x: y\n",
    ],
)
def test_source_policy_rejects_structurally_malformed_recognized_text_lockfiles(lockfile):
    assert not control_files_use_trusted_sources(b'{"dependencies":{"x":"1.0.0"}}', lockfile)


@pytest.mark.parametrize("manager", ["pnpm", "berry"])
@pytest.mark.parametrize(
    ("scalar", "expected"),
    [
        ("(x: y)", False),
        ('"x: y" trailing', False),
        ("{x: y} trailing", False),
        ("[x, y] trailing", False),
        ('"x: y"', True),
        ('"x: y" # comment', True),
        ('{"x":"y"}', True),
        ('{"x":"y"} # comment', True),
        ('["x","y"]', True),
        ('{"number":1.25,"enabled":true,"missing":null}', True),
        ('{"x":"y",,}', False),
        ('["x",,"y"]', False),
        ('{"x":NaN}', False),
        ('{"x":Infinity}', False),
        ('{"x":-Infinity}', False),
        ("x:", False),
    ],
)
def test_source_policy_requires_yaml_scalar_to_consume_entire_value(manager, scalar, expected):
    if manager == "pnpm":
        lockfile = f"lockfileVersion: '9.0'\npackages:\n  x:\n    resolution: {scalar}\n"
    else:
        lockfile = (
            '__metadata:\n  version: 8\n\n"x@npm:1":\n'
            f"  version: 1\n  resolution: {scalar}\n"
        )
    assert control_files_use_trusted_sources(
        b'{"dependencies":{"x":"1.0.0"}}', lockfile.encode()
    ) is expected


def test_validator_exact_argv_and_fixed_results(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    node = _file(tmp_path / "bin" / ("node.exe" if os.name == "nt" else "node"))
    validator = _file(tmp_path / "scripts" / "validate-packages.cjs")
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return ProbeProcessResult(0, b"RAW secret", b"RAW path", 0.01)

    assert run_validator(root, node, validator, 2, runner).reason == "validated"
    assert calls[0][0] == [str(node.path), str(validator.path), "typescript"]
    assert calls[0][1]["stdin"] is None
    assert calls[0][1]["max_output_bytes"] == INSTALL_OUTPUT_LIMIT
    assert calls[0][1]["check"] is False
    assert "NPM_TOKEN" not in calls[0][1]["environment"]


def test_manager_version_and_install_use_fixed_argv(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    command = _command(tmp_path / "bin" / ("npm.exe" if os.name == "nt" else "npm"))
    calls = []

    def version_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return ProbeProcessResult(0, b"11.2.0\n", b"secret", 0.01)

    version = run_manager_version(command, root, 1, version_runner)
    assert version.ok and version.version == Version(11, 2, 0) and version.reason == "manager_versioned"
    assert calls[0][0] == [*command.argv, "--version"]
    assert calls[0][1]["max_output_bytes"] == 64
    assert calls[0][1]["check"] is False

    install = run_install(root, command, adapter_for(Manager.NPM), "https://registry.npmjs.org/", tmp_path / "home", 2, version_runner)
    assert install.reason == "installed_command"
    assert calls[1][0] == [*command.argv, *adapter_for(Manager.NPM).argument_tail("https://registry.npmjs.org/")]
    assert calls[1][1]["check"] is False


def test_install_rejects_untrusted_registry_without_launch(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    command = _command(tmp_path / "bin" / ("npm.exe" if os.name == "nt" else "npm"))
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True
        return ProbeProcessResult(0, b"", b"", 0)

    result = run_install(
        root,
        command,
        adapter_for(Manager.NPM),
        "https://evil.example/",
        tmp_path / "home",
        1,
        runner,
    )
    assert result.reason == "install_failed"
    assert not called


def test_install_rejects_repo_or_symlink_isolated_home_before_launch(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    command = _command(tmp_path / "bin" / ("npm.exe" if os.name == "nt" else "npm"))
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True
        return ProbeProcessResult(0, b"", b"", 0)

    assert not run_install(
        root, command, adapter_for(Manager.NPM), TRUSTED_REGISTRY, root / "home", 1, runner
    ).ok
    external = tmp_path / "external"
    external.mkdir()
    link = tmp_path / "linked-home"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable: {error.__class__.__name__}")
    assert not run_install(
        root, command, adapter_for(Manager.NPM), TRUSTED_REGISTRY, link, 1, runner
    ).ok
    assert not called


def test_install_creates_only_external_isolated_npm_locations(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    command = _command(tmp_path / "bin" / ("npm.exe" if os.name == "nt" else "npm"))
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return ProbeProcessResult(0, b"", b"", 0)

    isolated = tmp_path / "isolated"
    assert run_install(
        root, command, adapter_for(Manager.NPM), TRUSTED_REGISTRY, isolated, 1, runner
    ).ok
    environment = calls[0][1]["environment"]
    for name in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "TEMP",
        "TMP",
        "NPM_CONFIG_CACHE",
        "NPM_CONFIG_PREFIX",
    ):
        path = Path(environment[name]).resolve(strict=True)
        assert isolated.resolve() in path.parents or path == isolated.resolve()
        assert root.resolve() not in path.parents
        assert path.is_dir() and not path.is_symlink()
    for name in ("NPM_CONFIG_USERCONFIG", "NPM_CONFIG_GLOBALCONFIG"):
        path = Path(environment[name]).resolve(strict=True)
        assert isolated.resolve() in path.parents
        assert path.is_file() and path.read_bytes() == b""


def test_install_rejects_noncanonical_adapter_before_launch(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    command = _command(tmp_path / "bin" / ("npm.exe" if os.name == "nt" else "npm"))
    canonical = adapter_for(Manager.NPM)
    adversarial = ManagerAdapter(
        canonical.manager,
        canonical.lockfiles,
        canonical.supported_majors,
        lambda registry: ("--config=hostile", "typescript"),
        canonical.unsafe_root_files,
    )
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True
        return ProbeProcessResult(0, b"", b"", 0)

    assert not run_install(
        root, command, adversarial, TRUSTED_REGISTRY, tmp_path / "isolated", 1, runner
    ).ok
    assert not called


def test_install_never_launches_canonical_guidance_only_yarn(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    command = _command(tmp_path / "bin" / ("yarn.exe" if os.name == "nt" else "yarn"))
    isolated = tmp_path / "isolated"
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True
        return ProbeProcessResult(0, b"", b"", 0)

    result = run_install(
        root, command, adapter_for(Manager.YARN), TRUSTED_REGISTRY, isolated, 1, runner
    )
    assert result.reason == "install_failed"
    assert not called
    assert not isolated.exists()


def test_install_rejects_linked_config_without_mutating_target(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    command = _command(tmp_path / "bin" / ("npm.exe" if os.name == "nt" else "npm"))
    isolated = tmp_path / "isolated"
    config_dir = isolated / "npm-config"
    config_dir.mkdir(parents=True)
    target = tmp_path / "sensitive"
    target.write_bytes(b"must-survive")
    try:
        os.link(target, config_dir / "user.npmrc")
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error.__class__.__name__}")
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True
        return ProbeProcessResult(0, b"", b"", 0)

    assert not run_install(
        root, command, adapter_for(Manager.NPM), TRUSTED_REGISTRY, isolated, 1, runner
    ).ok
    assert target.read_bytes() == b"must-survive"
    assert not called


def test_install_rejects_symlinked_config_without_mutating_target(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    command = _command(tmp_path / "bin" / ("npm.exe" if os.name == "nt" else "npm"))
    isolated = tmp_path / "isolated"
    config_dir = isolated / "npm-config"
    config_dir.mkdir(parents=True)
    target = tmp_path / "sensitive"
    target.write_bytes(b"must-survive")
    try:
        (config_dir / "user.npmrc").symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error.__class__.__name__}")
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True
        return ProbeProcessResult(0, b"", b"", 0)

    assert not run_install(
        root, command, adapter_for(Manager.NPM), TRUSTED_REGISTRY, isolated, 1, runner
    ).ok
    assert target.read_bytes() == b"must-survive"
    assert not called


def test_windows_npm_install_path_uses_only_executable_parent(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    node = _file(tmp_path / "node" / "node.exe")
    cli = _file(tmp_path / "node" / "node_modules" / "npm" / "bin" / "npm-cli.js")
    command = TrustedCommand((str(node.path), str(cli.path)), (node, cli))
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return ProbeProcessResult(0, b"", b"", 0)

    assert run_install(
        root,
        command,
        adapter_for(Manager.NPM),
        TRUSTED_REGISTRY,
        tmp_path / "home",
        1,
        runner,
    ).ok
    path_entries = calls[0][1]["environment"]["PATH"].split(os.pathsep)
    expected_system = (
        str(
            Path(dependency_install._trusted_windows_system_environment()["SYSTEMROOT"])
            / "System32"
        )
        if os.name == "nt"
        else "/usr/bin"
    )
    assert path_entries[:2] == [str(node.path.parent), expected_system]
    assert str(cli.path.parent) not in path_entries
    assert calls[0][0][-1] == "typescript"


def test_manager_nonzero_is_unavailable_not_a_version_parse_failure(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    command = _command(tmp_path / "bin" / ("npm.exe" if os.name == "nt" else "npm"))
    result = run_manager_version(
        command,
        root,
        1,
        lambda *a, **k: ProbeProcessResult(1, b"RAW invalid", b"RAW secret", 0),
    )
    assert result.reason == "manager_unavailable"


def test_nonpositive_timeouts_fail_before_runner_invocation(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    node = _file(tmp_path / "bin" / ("node.exe" if os.name == "nt" else "node"))
    validator = _file(tmp_path / "validate-packages.cjs")
    command = TrustedCommand((str(node.path),), (node,))
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True
        return ProbeProcessResult(0, b"1.2.3", b"", 0)

    assert run_validator(root, node, validator, 0, runner).reason == "validator_rejected"
    assert run_manager_version(command, root, -1, runner).reason == "manager_unavailable"
    assert run_install(
        root,
        command,
        adapter_for(Manager.NPM),
        "https://registry.npmjs.org/",
        tmp_path / "home",
        0,
        runner,
    ).reason == "install_failed"
    assert not probe_local_typescript(root, node, 0, runner)
    assert not called


@pytest.mark.skipif(os.name != "nt", reason="Windows command suffix policy")
def test_windows_batch_prefix_cannot_be_constructed(tmp_path):
    batch = _file(tmp_path / "npm.cmd")
    with pytest.raises(ValueError, match="trusted_command_invalid"):
        TrustedCommand((str(batch.path),), (batch,))
    root = tmp_path / "repo"
    root.mkdir()
    validator = _file(tmp_path / "validate-packages.cjs")
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True
        return ProbeProcessResult(0, b"", b"", 0)

    assert run_validator(root, batch, validator, 1, runner).reason == "executable_changed"
    assert not called


def test_trusted_command_rejects_unproven_prefix_arguments(tmp_path):
    executable = _file(tmp_path / ("npm.exe" if os.name == "nt" else "npm"))
    with pytest.raises(ValueError, match="trusted_command_invalid"):
        TrustedCommand((str(executable.path), "--config=untrusted"), (executable,))


@pytest.mark.parametrize(
    ("code", "validator_reason", "version_reason", "install_reason"),
    [
        ("timeout", "validator_timeout", "manager_timeout", "install_timeout"),
        ("nonzero", "validator_rejected", "manager_unavailable", "install_failed"),
        ("output_limit", "validator_rejected", "manager_unavailable", "install_failed"),
    ],
)
def test_wrappers_map_transport_failures_without_leaks(tmp_path, code, validator_reason, version_reason, install_reason):
    root = tmp_path / "repo"
    root.mkdir()
    node = _file(tmp_path / "bin" / ("node.exe" if os.name == "nt" else "node"))
    validator = _file(tmp_path / "validate-packages.cjs")
    command = TrustedCommand((str(node.path),), (node,))

    def failing(*args, **kwargs):
        raise ProbeProcessError(code)

    results = (
        run_validator(root, node, validator, 1, failing),
        run_manager_version(command, root, 1, failing),
        run_install(root, command, adapter_for(Manager.NPM), "https://registry.npmjs.org/", tmp_path / "home", 1, failing),
    )
    assert [result.reason for result in results] == [validator_reason, version_reason, install_reason]
    assert all("secret" not in result.reason and str(tmp_path) not in result.reason for result in results)


def test_identity_replacement_fails_closed(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    node = _file(tmp_path / "bin" / ("node.exe" if os.name == "nt" else "node"))
    validator = _file(tmp_path / "validate-packages.cjs")
    old = node.path.with_suffix(".old")
    node.path.rename(old)
    node.path.write_bytes(b"replacement")
    node.path.chmod(0o755)
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True
        return ProbeProcessResult(0, b"", b"", 0)

    assert run_validator(root, node, validator, 1, runner).reason == "executable_changed"
    assert not called


def test_in_place_executable_mutation_fails_closed(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    node = _file(tmp_path / "bin" / ("node.exe" if os.name == "nt" else "node"), b"original")
    validator = _file(tmp_path / "validate-packages.cjs")
    node.path.write_bytes(b"mutated!")
    node.path.chmod(0o755)
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True
        return ProbeProcessResult(0, b"", b"", 0)

    assert run_validator(root, node, validator, 1, runner).reason == "executable_changed"
    assert not called


def test_hard_link_mutation_setup_fails_closed(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    node = _file(tmp_path / "bin" / ("node.exe" if os.name == "nt" else "node"), b"original")
    validator = _file(tmp_path / "validate-packages.cjs")
    link = tmp_path / "linked-node"
    try:
        os.link(node.path, link)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error.__class__.__name__}")
    link.write_bytes(b"mutated!")
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True
        return ProbeProcessResult(0, b"", b"", 0)

    assert run_validator(root, node, validator, 1, runner).reason == "executable_changed"
    assert resolve_trusted_file(node.path, root, executable=True) is None
    assert not called


def test_oversized_trusted_file_rejects_before_open(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    oversized = tmp_path / ("node.exe" if os.name == "nt" else "node")
    with oversized.open("wb") as stream:
        stream.seek(MAX_TRUSTED_FILE_BYTES)
        stream.write(b"x")
    oversized.chmod(0o755)
    called = False
    original_open = dependency_install.os.open

    def guarded_open(path, *args, **kwargs):
        nonlocal called
        if Path(path) == oversized.resolve():
            called = True
            raise AssertionError("oversized file must not be opened")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(dependency_install.os, "open", guarded_open)
    assert resolve_trusted_file(oversized, root, executable=True) is None
    assert not called


def test_probe_local_typescript_requires_resolved_regular_file_under_package(tmp_path):
    root = tmp_path / "repo"
    package_json = root / "node_modules" / "typescript" / "package.json"
    package_json.parent.mkdir(parents=True)
    package_json.write_text('{}', encoding="utf-8")
    node = _file(tmp_path / "bin" / ("node.exe" if os.name == "nt" else "node"))
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return ProbeProcessResult(0, json.dumps({"resolved": str(package_json.resolve())}).encode(), b"secret", 0.01)

    assert probe_local_typescript(root, node, 1, runner)
    assert calls[0][0][:2] == [str(node.path), "-e"]
    assert "require.resolve('typescript/package.json'" in calls[0][0][2]
    assert calls[0][1]["max_output_bytes"] <= 4096
    assert calls[0][1]["check"] is False
    assert not probe_local_typescript(root, node, 1, lambda *a, **k: ProbeProcessResult(0, b"not-json secret", b"", 0))


def test_activation_outcome_values_and_fatal_mapping_are_exact():
    assert [outcome.value for outcome in ActivationOutcome] == [
        "installed",
        "already_available",
        "not_applicable",
        "declined",
        "guidance_only",
        "validation_failed",
        "installation_failed",
        "verification_failed",
    ]
    expected_fatal = {
        ActivationOutcome.VALIDATION_FAILED,
        ActivationOutcome.INSTALLATION_FAILED,
        ActivationOutcome.VERIFICATION_FAILED,
    }
    assert FATAL_OUTCOMES == expected_fatal
    for outcome in ActivationOutcome:
        result = ActivationResult(outcome, None, "fixed_reason")
        assert result.fatal is (outcome in expected_fatal)


def test_activation_result_serialization_is_exact_and_literal():
    result = ActivationResult(
        outcome=ActivationOutcome.GUIDANCE_ONLY,
        manager=Manager.NPM,
        reason="fixed_reason",
        manifest="package.json",
        lockfile="package-lock.json",
        changed_files=("package-lock.json", "package.json"),
        attempted=True,
    )

    assert result.to_dict() == {
        "outcome": "guidance_only",
        "manager": "npm",
        "reason": "fixed_reason",
        "manifest": "package.json",
        "lockfile": "package-lock.json",
        "changed_files": ["package-lock.json", "package.json"],
        "attempted": True,
    }
    assert list(result.to_dict()) == [
        "outcome",
        "manager",
        "reason",
        "manifest",
        "lockfile",
        "changed_files",
        "attempted",
    ]


def test_detect_without_typescript_evidence_is_not_applicable(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    detection = _detect(root)

    assert detection.result == ActivationResult(
        ActivationOutcome.NOT_APPLICABLE,
        None,
        "no_typescript_evidence",
    )
    assert detection.manager is None
    assert detection.manifest_snapshot is None
    assert detection.lockfile_snapshot is None


@pytest.mark.parametrize("evidence", ["main.ts", "component.tsx", "tsconfig.json"])
def test_detect_accepts_typescript_and_safe_root_config_evidence(tmp_path, evidence):
    root = _activation_root(tmp_path)
    (root / "source.ts").unlink()
    (root / evidence).write_text("{}" if evidence == "tsconfig.json" else "export {};", encoding="utf-8")

    detection = _detect(root)

    assert detection.result is None
    assert detection.manager is Manager.NPM


@pytest.mark.parametrize("configured", [None, 0, ACTIVATION_MAX_FILES + 1])
def test_evidence_scan_applies_real_activation_cap(
    tmp_path,
    monkeypatch,
    configured,
):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "first.js").write_text("", encoding="utf-8")
    (root / "second.js").write_text("", encoding="utf-8")
    monkeypatch.setattr(typescript_activation, "ACTIVATION_MAX_FILES", 1)

    detection = _detect(root, cfg=Config(max_files=configured))

    assert detection.result.reason == "evidence_scan_limited"


def test_evidence_scan_counts_skipped_and_ineligible_entries(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "node_modules").mkdir()
    (root / "ordinary.js").write_text("", encoding="utf-8")

    detection = _detect(root, cfg=Config(max_files=1))

    assert detection.result.reason == "evidence_scan_limited"


@pytest.mark.parametrize("configured", [-1, 1.5, True, False, "10"])
def test_evidence_scan_rejects_invalid_file_limit(tmp_path, configured):
    root = tmp_path / "repo"
    root.mkdir()

    detection = _detect(root, cfg=Config(max_files=configured))

    assert detection.result == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY,
        None,
        "invalid_file_limit",
    )


def test_safe_root_tsconfig_proves_evidence_without_traversal(tmp_path, monkeypatch):
    root = _activation_root(tmp_path)
    (root / "source.ts").unlink()
    (root / "tsconfig.json").write_text("{}", encoding="utf-8")

    def traversal_forbidden(*_args, **_kwargs):
        raise AssertionError("root tsconfig must bypass traversal")

    monkeypatch.setattr(typescript_activation.os, "scandir", traversal_forbidden)

    assert _detect(root).result is None


def test_evidence_scan_exits_on_first_typescript_entry(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.ts").write_text("content must not be read", encoding="utf-8")

    detection = _detect(root, available=True, cfg=Config(max_files=1))

    assert detection.result.reason == "local_typescript_available"


def test_evidence_scan_deadline_is_fixed_guidance(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "ordinary.js").write_text("", encoding="utf-8")
    ticks = iter((10.0, 20.0))
    monkeypatch.setattr(typescript_activation.time, "monotonic", lambda: next(ticks))

    detection = _detect(root)

    assert detection.result.reason == "evidence_scan_limited"


def test_evidence_scan_reads_no_file_contents_or_hashes(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.tsx").write_text("content must not be read", encoding="utf-8")
    original_open = typescript_activation.os.open
    directory_flag = getattr(typescript_activation.os, "O_DIRECTORY", 0)

    def directory_only_open(path, flags, *args, **kwargs):
        if directory_flag and flags & directory_flag:
            return original_open(path, flags, *args, **kwargs)
        raise AssertionError("evidence file content was opened")

    def content_read_forbidden(*_args, **_kwargs):
        raise AssertionError("evidence file content was read")

    def content_hash_forbidden(*_args, **_kwargs):
        raise AssertionError("evidence file content was hashed")

    monkeypatch.setattr(typescript_activation.os, "open", directory_only_open)
    monkeypatch.setattr(typescript_activation.os, "read", content_read_forbidden)
    monkeypatch.setattr(ingest, "file_hash", content_hash_forbidden)

    detection = _detect(root, available=True)

    assert detection.result.reason == "local_typescript_available"


def test_evidence_scan_does_not_follow_symlinked_files_or_directories(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside_file = tmp_path / "outside.ts"
    outside_file.write_text("export {};", encoding="utf-8")
    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    (outside_directory / "nested.tsx").write_text("export {};", encoding="utf-8")
    _symlink_or_skip(root / "linked.ts", outside_file)
    _symlink_or_skip(root / "linked-directory", outside_directory)

    detection = _detect(root)

    assert detection.result.reason == "no_typescript_evidence"


def test_evidence_scan_does_not_follow_simulated_reparse_entries(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    reparse_directory = root / "reparse-directory"
    reparse_directory.mkdir()
    (reparse_directory / "nested.tsx").write_text("export {};", encoding="utf-8")
    calls = 0

    def simulate_only_root_reparse(_details):
        nonlocal calls
        calls += 1
        if calls == 1:
            return True
        raise AssertionError("nested reparse target was inspected")

    monkeypatch.setattr(
        typescript_activation,
        "_is_reparse",
        simulate_only_root_reparse,
    )

    detection = _detect(root)

    assert detection.result.reason == "no_typescript_evidence"
    assert calls == 1


def test_evidence_scan_limits_directory_depth(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    (child / "nested").mkdir()
    monkeypatch.setattr(typescript_activation, "_EVIDENCE_MAX_DEPTH", 1)

    detection = _detect(root)

    assert detection.result.reason == "evidence_scan_limited"


def test_evidence_scan_rejects_directory_swapped_to_symlink_before_child_open(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "repo"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    original_child = root / "original-child"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external.ts").write_text("export {};", encoding="utf-8")
    original_open_child = typescript_activation._open_child_scan_directory
    original_scandir = typescript_activation.os.scandir
    swapped = False

    def swap_then_open(parent, name, expected_identity, canonical_root):
        nonlocal swapped
        if not swapped and name == "child":
            child.rename(original_child)
            _symlink_or_skip(child, outside)
            swapped = True
        return original_open_child(parent, name, expected_identity, canonical_root)

    def guard_external_scandir(target):
        if swapped and not isinstance(target, int) and Path(target) in {child, outside}:
            raise AssertionError("replacement target must never be enumerated")
        return original_scandir(target)

    monkeypatch.setattr(
        typescript_activation,
        "_open_child_scan_directory",
        swap_then_open,
    )
    monkeypatch.setattr(typescript_activation.os, "scandir", guard_external_scandir)

    detection = _detect(root)

    assert swapped
    assert detection.result.reason == "evidence_collection_failed"


def test_evidence_scan_prunes_repository_exclusions(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for directory in ("node_modules", ".hidden", "generated", "cache-data"):
        target = root / directory
        target.mkdir()
        (target / "hidden.ts").write_text("export {};", encoding="utf-8")
    cfg = Config(output_dir=Path("generated"), cache_dir=Path("cache-data"))

    detection = _detect(root, cfg=cfg)

    assert detection.result.reason == "no_typescript_evidence"


def test_already_available_precedes_package_manager_inspection(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.ts").write_text("export {};", encoding="utf-8")
    (root / "package.json").write_bytes(b"not json")
    (root / "package-lock.json").write_bytes(b"not json")
    (root / "pnpm-lock.yaml").write_bytes(b"not yaml")

    detection = _detect(root, available=True)

    assert detection.result == ActivationResult(
        ActivationOutcome.ALREADY_AVAILABLE,
        None,
        "local_typescript_available",
    )
    assert detection.manager is None
    assert detection.manifest is None
    assert detection.lockfile is None


@pytest.mark.parametrize(
    ("lockfile", "manager", "outcome", "reason"),
    [
        ("package-lock.json", Manager.NPM, None, None),
        ("pnpm-lock.yaml", Manager.PNPM, None, None),
        ("yarn.lock", Manager.YARN, ActivationOutcome.GUIDANCE_ONLY, "manager_guidance_only"),
        ("bun.lock", Manager.BUN, None, None),
        ("bun.lockb", Manager.BUN, None, None),
    ],
)
def test_detect_identifies_one_root_lockfile_family(tmp_path, lockfile, manager, outcome, reason):
    detection = _detect(_activation_root(tmp_path, lockfile))

    if outcome is None:
        assert detection.result is None
    else:
        assert detection.result == ActivationResult(
            outcome,
            manager,
            reason,
            "package.json",
            lockfile,
        )
    assert detection.manager is manager
    assert detection.manifest == "package.json"
    assert detection.lockfile == lockfile


@pytest.mark.parametrize(
    ("extra_lockfile", "reason"),
    [
        ("bun.lockb", "lockfile_ambiguous"),
        ("pnpm-lock.yaml", "lockfile_ambiguous"),
    ],
)
def test_detect_rejects_multiple_supported_root_lockfiles(tmp_path, extra_lockfile, reason):
    first = "bun.lock" if extra_lockfile == "bun.lockb" else "package-lock.json"
    root = _activation_root(tmp_path, first)
    (root / extra_lockfile).write_bytes(_SAFE_LOCKS[extra_lockfile])

    detection = _detect(root)

    assert detection.result.outcome is ActivationOutcome.GUIDANCE_ONLY
    assert detection.result.reason == reason
    assert detection.result.manager is None
    assert detection.result.lockfile is None


def test_detect_rejects_missing_root_lockfile(tmp_path):
    root = _activation_root(tmp_path)
    (root / "package-lock.json").unlink()

    detection = _detect(root)

    assert detection.result == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY,
        None,
        "lockfile_missing",
        "package.json",
    )


@pytest.mark.parametrize("nested_control", ["package.json", "package-lock.json"])
def test_detect_ignores_nested_control_files(tmp_path, nested_control):
    root = _activation_root(tmp_path)
    (root / nested_control).unlink()
    nested = root / "nested"
    nested.mkdir()
    content = b"{}" if nested_control == "package.json" else _SAFE_LOCKS[nested_control]
    (nested / nested_control).write_bytes(content)

    detection = _detect(root)

    expected_reason = "manifest_missing" if nested_control == "package.json" else "lockfile_missing"
    assert detection.result.outcome is ActivationOutcome.GUIDANCE_ONLY
    assert detection.result.reason == expected_reason


@pytest.mark.parametrize("manifest", [b"not json", b"[]", b"null", b'{"x":1,"x":2}'])
def test_detect_rejects_invalid_manifest_json(tmp_path, manifest):
    detection = _detect(_activation_root(tmp_path, manifest=manifest))

    assert detection.result == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY,
        None,
        "manifest_invalid",
        "package.json",
    )


def test_detect_rejects_oversized_manifest(tmp_path):
    manifest = b" " * (MAX_CONTROL_FILE_BYTES + 1)

    detection = _detect(_activation_root(tmp_path, manifest=manifest))

    assert detection.result.reason == "manifest_invalid"
    assert detection.manifest_snapshot is None


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error.__class__.__name__}")


@pytest.mark.parametrize(
    ("control", "expected_reason"),
    [
        ("package.json", "manifest_unsafe"),
        ("package-lock.json", "lockfile_unsafe"),
    ],
)
def test_detect_rejects_symlinked_or_escaping_control_file(tmp_path, control, expected_reason):
    root = _activation_root(tmp_path)
    outside = tmp_path / f"outside-{control}"
    outside.write_bytes(b"{}" if control == "package.json" else _SAFE_LOCKS[control])
    (root / control).unlink()
    _symlink_or_skip(root / control, outside)

    detection = _detect(root)

    assert detection.result.outcome is ActivationOutcome.GUIDANCE_ONLY
    assert detection.result.reason == expected_reason
    assert str(outside) not in str(detection.result.to_dict())


def test_unsafe_root_tsconfig_is_guidance_not_evidence(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside-tsconfig.json"
    outside.write_text("{}", encoding="utf-8")
    _symlink_or_skip(root / "tsconfig.json", outside)

    detection = _detect(root)

    assert detection.result == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY,
        None,
        "typescript_configuration_unsafe",
    )


@pytest.mark.parametrize(
    ("control", "lockfile", "expected_reason"),
    [
        ("tsconfig.json", "package-lock.json", "typescript_configuration_unsafe"),
        ("package.json", "package-lock.json", "manifest_unsafe"),
        ("package-lock.json", "package-lock.json", "lockfile_unsafe"),
        ("pnpm-lock.yaml", "pnpm-lock.yaml", "lockfile_unsafe"),
        ("yarn.lock", "yarn.lock", "lockfile_unsafe"),
        ("bun.lock", "bun.lock", "lockfile_unsafe"),
    ],
)
def test_detect_rejects_simulated_reparse_control_without_following(
    tmp_path,
    monkeypatch,
    control,
    lockfile,
    expected_reason,
):
    root = _activation_root(tmp_path, lockfile)
    target = root / control
    if control == "tsconfig.json":
        target.write_text("{}", encoding="utf-8")
    target_identity = target.lstat().st_dev, target.lstat().st_ino
    original_is_reparse = typescript_activation._is_reparse
    original_resolve = Path.resolve

    def simulate_reparse(details):
        identity = details.st_dev, details.st_ino
        return identity == target_identity or original_is_reparse(details)

    def forbid_target_resolution(path, *args, **kwargs):
        if path == target:
            raise AssertionError("reparse control must not be followed")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(typescript_activation, "_is_reparse", simulate_reparse)
    monkeypatch.setattr(Path, "resolve", forbid_target_resolution)

    detection = _detect(root)

    assert detection.result.reason == expected_reason


def test_available_compiler_performs_no_package_control_inspection(
    tmp_path,
    monkeypatch,
):
    root = _activation_root(tmp_path)
    original_root_file_state = typescript_activation._root_file_state
    original_open = typescript_activation.os.open
    directory_flag = getattr(typescript_activation.os, "O_DIRECTORY", 0)

    def guard_root_file_state(selected_root, relative_path, **kwargs):
        if relative_path != "tsconfig.json":
            raise AssertionError(f"package control inspected: {relative_path}")
        return original_root_file_state(selected_root, relative_path, **kwargs)

    def directory_only_open(path, flags, *args, **kwargs):
        if directory_flag and flags & directory_flag:
            return original_open(path, flags, *args, **kwargs)
        raise AssertionError("package control content opened")

    def stable_control_read_forbidden(*_args, **_kwargs):
        raise AssertionError("package control content read")

    monkeypatch.setattr(typescript_activation, "_root_file_state", guard_root_file_state)
    monkeypatch.setattr(typescript_activation.os, "open", directory_only_open)
    monkeypatch.setattr(
        typescript_activation,
        "_read_stable_control_file",
        stable_control_read_forbidden,
    )

    detection = _detect(root, available=True)

    assert detection.result.reason == "local_typescript_available"


@pytest.mark.parametrize(
    ("metadata", "expected_reason"),
    [
        (None, None),
        ("npm@10.9.0", None),
        ("npm", "package_manager_invalid"),
        ("npm@", "package_manager_invalid"),
        ("npm@10 9", "package_manager_invalid"),
        (10, "package_manager_invalid"),
        ("pnpm@10.0.0", "package_manager_conflict"),
    ],
)
def test_detect_validates_package_manager_without_overriding_lockfile(
    tmp_path,
    metadata,
    expected_reason,
):
    manifest = {} if metadata is None else {"packageManager": metadata}
    root = _activation_root(tmp_path, manifest=json.dumps(manifest).encode())

    detection = _detect(root)

    if expected_reason is None:
        assert detection.result is None
        assert detection.manager is Manager.NPM
    else:
        assert detection.result == ActivationResult(
            ActivationOutcome.GUIDANCE_ONLY,
            Manager.NPM,
            expected_reason,
            "package.json",
            "package-lock.json",
        )


def test_detect_rejects_present_null_package_manager(tmp_path):
    root = _activation_root(
        tmp_path,
        manifest=json.dumps({"packageManager": None}).encode(),
    )

    detection = _detect(root)

    assert detection.result == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY,
        Manager.NPM,
        "package_manager_invalid",
        "package.json",
        "package-lock.json",
    )


@pytest.mark.parametrize(
    ("lockfile", "unsafe_path", "directory"),
    [
        ("package-lock.json", ".npmrc", False),
        ("pnpm-lock.yaml", ".pnpmfile.cjs", False),
        ("yarn.lock", ".yarn/plugins", True),
        ("bun.lock", "bunfig.toml", False),
    ],
)
def test_detect_rejects_manager_root_configuration(tmp_path, lockfile, unsafe_path, directory):
    root = _activation_root(tmp_path, lockfile)
    path = root / unsafe_path
    if directory:
        path.mkdir(parents=True)
    else:
        path.write_text("unsafe", encoding="utf-8")

    detection = _detect(root)

    assert detection.result.outcome is ActivationOutcome.GUIDANCE_ONLY
    assert detection.result.reason == "manager_configuration_unsafe"
    assert detection.result.lockfile == lockfile


@pytest.mark.parametrize(
    "kind",
    ["regular", "directory", "symlink", "broken_symlink", "simulated_reparse"],
)
def test_detect_rejects_every_npm_shrinkwrap_path_kind(tmp_path, monkeypatch, kind):
    root = _activation_root(tmp_path)
    shrinkwrap = root / "npm-shrinkwrap.json"
    if kind == "regular":
        shrinkwrap.write_text("{}", encoding="utf-8")
    elif kind == "directory":
        shrinkwrap.mkdir()
    elif kind in {"symlink", "broken_symlink"}:
        target = tmp_path / "outside-shrinkwrap.json"
        if kind == "symlink":
            target.write_text("{}", encoding="utf-8")
        _symlink_or_skip(shrinkwrap, target)
    else:
        original_present = typescript_activation._path_is_lexically_present

        def simulate_reparse(path):
            return Path(path) == shrinkwrap or original_present(Path(path))

        monkeypatch.setattr(
            typescript_activation,
            "_path_is_lexically_present",
            simulate_reparse,
        )

    detection = _detect(root)

    assert detection.result == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY,
        Manager.NPM,
        "manager_configuration_unsafe",
        "package.json",
        "package-lock.json",
    )


@pytest.mark.parametrize("broken", [False, True])
def test_detect_rejects_symlinked_manager_configuration_without_following(
    tmp_path,
    broken,
):
    root = _activation_root(tmp_path)
    target = tmp_path / "outside-npm-config"
    if not broken:
        target.write_text("secret registry configuration", encoding="utf-8")
    _symlink_or_skip(root / ".npmrc", target)

    detection = _detect(root)

    assert detection.result == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY,
        Manager.NPM,
        "manager_configuration_unsafe",
        "package.json",
        "package-lock.json",
    )
    assert str(target) not in repr(detection)


def test_detect_rejects_simulated_windows_reparse_manager_configuration(
    tmp_path,
    monkeypatch,
):
    root = _activation_root(tmp_path)
    simulated_reparse = root / ".npmrc"
    observed = []
    original_present = typescript_activation._path_is_lexically_present

    def simulate_reparse(path):
        candidate = Path(path)
        if candidate == simulated_reparse:
            observed.append(candidate)
            return True
        return original_present(candidate)

    monkeypatch.setattr(
        typescript_activation,
        "_path_is_lexically_present",
        simulate_reparse,
    )

    detection = _detect(root)

    assert detection.result.reason == "manager_configuration_unsafe"
    assert observed == [simulated_reparse]
    assert not simulated_reparse.exists()


@pytest.mark.parametrize(
    ("manifest", "lockfile_bytes"),
    [
        (b'{"dependencies":{"x":"file:../x"}}', _SAFE_LOCKS["package-lock.json"]),
        (
            b'{"dependencies":{"x":"1.0.0"}}',
            b'{"lockfileVersion":3,"resolved":"https://evil.example/x.tgz"}',
        ),
        (b'{"workspaces":["packages/*"]}', _SAFE_LOCKS["package-lock.json"]),
    ],
)
def test_detect_rejects_hostile_dependency_sources(tmp_path, manifest, lockfile_bytes):
    root = _activation_root(tmp_path, manifest=manifest)
    (root / "package-lock.json").write_bytes(lockfile_bytes)

    detection = _detect(root)

    assert detection.result.outcome is ActivationOutcome.GUIDANCE_ONLY
    assert detection.result.reason == "dependency_source_unsafe"


def test_yarn_is_guidance_only_after_safe_policy_checks(tmp_path):
    detection = _detect(_activation_root(tmp_path, "yarn.lock"))

    assert detection.result == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY,
        Manager.YARN,
        "manager_guidance_only",
        "package.json",
        "yarn.lock",
    )
    assert detection.manifest_snapshot is None
    assert detection.lockfile_snapshot is None


def test_eligible_detection_retains_only_bounded_snapshots(tmp_path):
    detection = _detect(_activation_root(tmp_path))

    assert detection.result is None
    assert detection.manifest_snapshot.relative_path == "package.json"
    assert detection.lockfile_snapshot.relative_path == "package-lock.json"
    assert set(vars(detection.manifest_snapshot)) == {"relative_path", "identity", "sha256"}
    assert set(vars(detection.lockfile_snapshot)) == {"relative_path", "identity", "sha256"}
    serialized = repr(detection)
    assert str(tmp_path) not in serialized
    assert "lockfileVersion" not in serialized


def test_terminal_detection_never_retains_snapshots(tmp_path):
    root = _activation_root(tmp_path, manifest=b"not-json")

    detection = _detect(root)

    assert detection.result.reason == "manifest_invalid"
    assert detection.manifest_snapshot is None
    assert detection.lockfile_snapshot is None


def test_evidence_scan_failure_maps_to_fixed_guidance_without_error_text(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "repo"
    root.mkdir()

    def fail_scan(*_args, **_kwargs):
        raise OSError(f"secret path: {tmp_path}")

    monkeypatch.setattr(typescript_activation.os, "scandir", fail_scan)

    detection = _detect(root)

    assert detection.result == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY,
        None,
        "evidence_collection_failed",
    )
    assert str(tmp_path) not in str(detection.result.to_dict())


def test_invalid_repository_root_maps_to_fixed_guidance(tmp_path):
    detection = _detect(tmp_path / "missing")

    assert detection.result == ActivationResult(
        ActivationOutcome.GUIDANCE_ONLY,
        None,
        "repository_unsafe",
    )


def test_control_file_change_during_read_fails_closed(tmp_path, monkeypatch):
    root = _activation_root(tmp_path)
    original_snapshot = typescript_activation.snapshot_control_file
    calls = 0

    def changed_snapshot(selected_root, relative_path):
        nonlocal calls
        snapshot = original_snapshot(selected_root, relative_path)
        calls += 1
        if relative_path == "package.json" and calls == 2:
            return dependency_install.FileSnapshot(
                snapshot.relative_path,
                snapshot.identity,
                "0" * 64,
            )
        return snapshot

    monkeypatch.setattr(typescript_activation, "snapshot_control_file", changed_snapshot)

    detection = _detect(root)

    assert detection.result.reason == "manifest_unsafe"
    assert detection.manifest_snapshot is None


@pytest.mark.parametrize(
    ("failing_close_call", "expected_reason"),
    [(2, "manifest_unsafe"), (5, "lockfile_unsafe")],
)
def test_control_file_close_failure_maps_to_fixed_guidance(
    tmp_path,
    monkeypatch,
    failing_close_call,
    expected_reason,
):
    root = _activation_root(tmp_path)
    original_close = typescript_activation.os.close
    close_calls = 0

    def fail_selected_close(descriptor):
        nonlocal close_calls
        close_calls += 1
        original_close(descriptor)
        if close_calls == failing_close_call:
            raise OSError(f"secret close failure at {tmp_path}")

    monkeypatch.setattr(typescript_activation.os, "close", fail_selected_close)

    detection = _detect(root)

    assert detection.result.outcome is ActivationOutcome.GUIDANCE_ONLY
    assert detection.result.reason == expected_reason
    assert str(tmp_path) not in str(detection.result.to_dict())


@pytest.mark.parametrize(
    "lockfile",
    ["package-lock.json", "pnpm-lock.yaml", "bun.lock", "bun.lockb"],
)
def test_revalidation_accepts_unchanged_eligible_detection(tmp_path, lockfile):
    root = _activation_root(tmp_path, lockfile)
    expected = _detect(root)
    assert expected.result is None

    assert revalidate_activation_detection(root, Config(), expected)


@pytest.mark.parametrize(
    ("lockfile", "unsafe_path", "directory"),
    [
        ("package-lock.json", ".npmrc", False),
        ("package-lock.json", "npm-shrinkwrap.json", False),
        ("pnpm-lock.yaml", ".npmrc", False),
        ("pnpm-lock.yaml", "pnpm-workspace.yaml", False),
        ("pnpm-lock.yaml", ".pnpmfile.cjs", False),
        ("pnpm-lock.yaml", ".pnpmfile.mjs", False),
        ("bun.lock", ".npmrc", False),
        ("bun.lock", "bunfig.toml", False),
    ],
)
def test_revalidation_rejects_new_manager_configuration(
    tmp_path,
    lockfile,
    unsafe_path,
    directory,
):
    root = _activation_root(tmp_path, lockfile)
    expected = _detect(root)
    assert expected.result is None
    path = root / unsafe_path
    if directory:
        path.mkdir(parents=True)
    else:
        path.write_text("unsafe", encoding="utf-8")

    assert not revalidate_activation_detection(root, Config(), expected)


def test_revalidation_rejects_new_second_lockfile(tmp_path):
    root = _activation_root(tmp_path)
    expected = _detect(root)
    (root / "pnpm-lock.yaml").write_bytes(_SAFE_LOCKS["pnpm-lock.yaml"])

    assert not revalidate_activation_detection(root, Config(), expected)


@pytest.mark.parametrize("control", ["package.json", "package-lock.json"])
def test_revalidation_rejects_replaced_control_identity(tmp_path, control):
    root = _activation_root(tmp_path)
    expected = _detect(root)
    path = root / control
    original = path.read_bytes()
    path.rename(root / f"old-{control}")
    path.write_bytes(original)

    assert not revalidate_activation_detection(root, Config(), expected)


@pytest.mark.parametrize("control", ["package.json", "package-lock.json"])
def test_revalidation_rejects_changed_control_content(tmp_path, control):
    root = _activation_root(tmp_path)
    expected = _detect(root)
    if control == "package.json":
        (root / control).write_bytes(b'{"dependencies":{"x":"file:../x"}}')
    else:
        (root / control).write_bytes(
            b'{"lockfileVersion":3,"resolved":"https://evil.example/x.tgz"}'
        )

    assert not revalidate_activation_detection(root, Config(), expected)


def test_revalidation_contains_detection_errors(tmp_path, monkeypatch):
    root = _activation_root(tmp_path)
    expected = _detect(root)

    def fail_detection(*_args, **_kwargs):
        raise OSError(f"secret detection error: {tmp_path}")

    monkeypatch.setattr(typescript_activation, "detect_activation", fail_detection)

    assert not revalidate_activation_detection(root, Config(), expected)


def test_revalidation_rejects_terminal_expected_detection(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    expected = _detect(root)
    assert expected.result.reason == "no_typescript_evidence"

    assert not revalidate_activation_detection(root, Config(), expected)


def test_revalidation_rejects_deleted_typescript_evidence(tmp_path):
    root = _activation_root(tmp_path)
    expected = _detect(root)
    assert expected.result is None
    (root / "source.ts").unlink()

    assert not revalidate_activation_detection(root, Config(), expected)


def test_revalidation_rejects_new_symlinked_evidence_directory(tmp_path):
    root = _activation_root(tmp_path)
    (root / "source.ts").unlink()
    source_directory = root / "source"
    source_directory.mkdir()
    (source_directory / "main.ts").write_text("export {};", encoding="utf-8")
    expected = _detect(root)
    assert expected.result is None
    outside = tmp_path / "outside-source"
    source_directory.rename(outside)
    _symlink_or_skip(source_directory, outside)

    assert not revalidate_activation_detection(root, Config(), expected)
