from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path

import pytest
import graphite.dependency_install as dependency_install
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
from graphite.ingest import IngestError
from graphite.probe_process import ProbeProcessResult
from graphite.typescript_activation import (
    FATAL_OUTCOMES,
    ActivationDetection,
    ActivationOutcome,
    ActivationResult,
    detect_activation,
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


@pytest.mark.parametrize("outcome", list(ActivationOutcome))
def test_activation_result_serialization_and_fatal_mapping_are_exact(outcome):
    result = ActivationResult(
        outcome=outcome,
        manager=Manager.NPM,
        reason="fixed_reason",
        manifest="package.json",
        lockfile="package-lock.json",
        changed_files=("package-lock.json", "package.json"),
        attempted=True,
    )

    assert result.fatal is (outcome in FATAL_OUTCOMES)
    assert result.to_dict() == {
        "outcome": outcome.value,
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


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, ACTIVATION_MAX_FILES),
        (ACTIVATION_MAX_FILES + 1, ACTIVATION_MAX_FILES),
        (17, 17),
    ],
)
def test_detect_always_bounds_evidence_collection(tmp_path, monkeypatch, configured, expected):
    root = tmp_path / "repo"
    root.mkdir()
    observed = []

    def bounded_collect(selected_root, cfg):
        observed.append((selected_root, cfg.max_files))
        return []

    monkeypatch.setattr(typescript_activation, "collect_files", bounded_collect)

    assert _detect(root, cfg=Config(max_files=configured)).result.reason == "no_typescript_evidence"
    assert observed == [(root.resolve(), expected)]


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
        link.symlink_to(target)
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


def test_ingestion_failure_maps_to_fixed_guidance_without_error_text(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()

    def fail_collection(*_args, **_kwargs):
        raise IngestError(f"secret path: {tmp_path}")

    monkeypatch.setattr(typescript_activation, "collect_files", fail_collection)

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
