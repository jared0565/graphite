from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from graphite.dependency_install import (
    INSTALL_OUTPUT_LIMIT,
    Manager,
    ProbeProcessError,
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


def _file(path: Path, content: bytes = b"tool") -> TrustedFile:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)
    stat = path.stat()
    return TrustedFile(path.resolve(), (stat.st_dev, stat.st_ino))


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
            (".npmrc",),
            True,
        ),
        (
            Manager.NPM,
            Version(11, 9, 1),
            True,
            ("package-lock.json",),
            ("install", "--save-dev", "--ignore-scripts", "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org/", "typescript"),
            (".npmrc",),
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


def test_install_environment_is_exact_and_strips_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("NPM_TOKEN", "secret")
    source = {
        "SYSTEMROOT": r"C:\Windows",
        "LANG": "en_GB.UTF-8",
        "PATH": str(tmp_path / "repo-controlled"),
        "NPM_TOKEN": "secret",
        "NODE_AUTH_TOKEN": "secret",
        "HTTPS_PROXY": "http://secret",
        "GRAPHITE_API_KEY": "secret",
    }
    tools = tmp_path / "tools"
    tools.mkdir()
    home = tmp_path / "isolated"
    env = build_install_environment(Manager.NPM, home, (tools,), "https://registry.npmjs.org/", source)
    assert env["PATH"].split(os.pathsep)[0] == str(tools.resolve())
    assert str(tmp_path / "repo-controlled") not in env["PATH"]
    assert env["HOME"] == str((home / "home").resolve())
    assert env["NPM_CONFIG_USERCONFIG"] == str((home / "npmrc").resolve())
    assert env["NPM_CONFIG_REGISTRY"] == "https://registry.npmjs.org/"
    assert env["npm_config_ignore_scripts"] == "true"
    assert not ({"NPM_TOKEN", "NODE_AUTH_TOKEN", "HTTPS_PROXY", "GRAPHITE_API_KEY"} & env.keys())


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
        (b'{"dependencies":{"x":"^1.0.0"},"devDependencies":{"typescript":"latest"}}', b'{"resolved":"https://registry.npmjs.org/x/-/x-1.0.0.tgz"}', True),
        (b'{"dependencies":{"x":"file:../x"}}', b"", False),
        (b'{"dependencies":{"x":"../x"}}', b"", False),
        (b'{"workspaces":["packages/*"]}', b"", False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"resolved":"https://evil.example/x.tgz"}', False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"resolved":"https:\\/\\/evil.example/x.tgz"}', False),
        (b'{"dependencies":{"x":"1.0.0"}}', b'{"resolved":"git+ssh://example/x"}', False),
        (b"not-json", b"", False),
    ],
)
def test_source_policy(manifest, lockfile, expected):
    assert control_files_use_trusted_sources(manifest, lockfile) is expected


def test_source_policy_rejects_malformed_lockfile_encoding():
    assert not control_files_use_trusted_sources(b"{}", b"\xff\x00")


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

    install = run_install(root, command, adapter_for(Manager.NPM), "https://registry.npmjs.org/", tmp_path / "home", 2, version_runner)
    assert install.reason == "installed_command"
    assert calls[1][0] == [*command.argv, *adapter_for(Manager.NPM).argument_tail("https://registry.npmjs.org/")]


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
    assert not probe_local_typescript(root, node, 1, lambda *a, **k: ProbeProcessResult(0, b"not-json secret", b"", 0))
