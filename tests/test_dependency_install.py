"""Tests for `graphite.dependency_install`, the trusted TypeScript activation installer.

Named after the module so aramid's mutation stage 1 (`tests/test_<module>.py`)
has something to run; `-k dependency_install` selected nothing before this
file, and `test_typescript_activation.py` (476 tests, 56 s) reaches it only
through the full onboarding flow. This file pins the pure gates that decide
whether an install may run at all -- the version parser, the adapter table,
the dependency-spec and registry-URL rules, and the control-file source
checks -- with plain bytes.
"""
from __future__ import annotations

import json

import pytest

from graphite.dependency_install import (
    MAX_CONTROL_FILE_BYTES,
    TRUSTED_REGISTRY,
    Manager,
    Version,
    _dependency_spec_is_safe,
    _is_canonical_registry_url,
    _json_source_fields_are_trusted,
    _lockfile_uses_trusted_sources,
    _minimal_node_environment,
    _text_source_fields_are_trusted,
    adapter_for,
    control_files_use_trusted_sources,
    parse_version,
)

_RESOLVED = "https://registry.npmjs.org/typescript/-/typescript-5.4.0.tgz"


def _manifest(**changes: object) -> bytes:
    manifest: dict[str, object] = {"name": "app", "devDependencies": {"typescript": "^5.4.0"}}
    manifest.update(changes)
    return json.dumps(manifest).encode("utf-8")


def _npm_lock(resolved: str = _RESOLVED) -> bytes:
    lock = {
        "name": "app",
        "lockfileVersion": 3,
        "packages": {
            "": {"devDependencies": {"typescript": "^5.4.0"}},
            "node_modules/typescript": {"version": "5.4.0", "resolved": resolved, "integrity": "sha512-x"},
        },
    }
    return json.dumps(lock, indent=2).encode("utf-8")


# --- parse_version -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10.8.2", Version(10, 8, 2)),
        ("v10.8.2\n", Version(10, 8, 2)),
        (b"1.2.3-beta.1+build.7", Version(1, 2, 3)),
        ("01.2.3", None),
        ("1.2", None),
        ("1.2.3.4", None),
        ("", None),
        (b"\xff", None),
    ],
)
def test_parse_version_reads_semver_and_nothing_looser(value: str | bytes, expected: Version | None) -> None:
    assert parse_version(value) == expected


# --- adapters -------------------------------------------------------------------------------


def test_adapter_table_pins_each_manager_lockfiles_majors_and_automation() -> None:
    npm, pnpm, yarn, bun = (adapter_for(m) for m in (Manager.NPM, Manager.PNPM, Manager.YARN, Manager.BUN))
    assert npm.lockfiles == ("package-lock.json",) and npm.supported_majors == frozenset(range(8, 12))
    assert pnpm.lockfiles == ("pnpm-lock.yaml",) and pnpm.supported_majors == frozenset({11})
    assert yarn.lockfiles == ("yarn.lock",) and yarn.automatic is False
    assert bun.lockfiles == ("bun.lock", "bun.lockb") and bun.supported_majors == frozenset({1})
    assert adapter_for("npm") is npm  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        adapter_for("pip")  # type: ignore[arg-type]


def test_adapter_supports_only_an_automatic_manager_at_a_supported_major() -> None:
    npm = adapter_for(Manager.NPM)
    assert npm.supports(Version(10, 0, 0)) is True
    assert npm.supports(Version(7, 9, 9)) is False
    assert npm.supports(None) is False
    assert adapter_for(Manager.YARN).supports(Version(4, 0, 0)) is False


def test_adapter_tails_install_typescript_as_a_dev_dependency_without_scripts() -> None:
    for manager in (Manager.NPM, Manager.PNPM, Manager.BUN):
        tail = adapter_for(manager).argument_tail(TRUSTED_REGISTRY)
        assert tail[-1] == "typescript"
        assert any(flag.startswith("--ignore-scripts") for flag in tail)
        assert TRUSTED_REGISTRY in tail or f"--registry={TRUSTED_REGISTRY}" in tail
    assert adapter_for(Manager.YARN).argument_tail(TRUSTED_REGISTRY) == ("add", "--dev", "--mode=skip-build", "typescript")


# --- dependency specs and registry URLs --------------------------------------------------------


@pytest.mark.parametrize("spec", ["^5.4.0", "5.4.0", "latest", "next", "~1.0.0", ">=1 <2", "*"])
def test_dependency_spec_is_safe_accepts_registry_ranges_and_tags(spec: str) -> None:
    assert _dependency_spec_is_safe(spec) is True


@pytest.mark.parametrize(
    "spec",
    [
        "",
        " ^5.4.0",
        "file:../local",
        "link:../local",
        "git+https://x/y.git",
        "github:org/repo",
        "https://x/y.tgz",
        "git@github.com:org/repo.git",
        "npm:typescript@5",
        "./vendor/ts",
        "..",
        "C:\\ts",
        "workspace:*",
        "ts.tar.gz",
    ],
)
def test_dependency_spec_is_safe_refuses_anything_that_is_not_a_registry_spec(spec: str) -> None:
    assert _dependency_spec_is_safe(spec) is False


@pytest.mark.parametrize(
    ("url", "trusted"),
    [
        (_RESOLVED, True),
        ("https://registry.npmjs.org/", True),
        ("http://registry.npmjs.org/x.tgz", False),
        ("https://registry.npmjs.org:443/x.tgz", False),
        ("https://user@registry.npmjs.org/x.tgz", False),
        ("https://registry.npmjs.org/x.tgz?token=1", False),
        ("https://registry.npmjs.org/x.tgz#frag", False),
        ("https://registry.npmjs.org.evil.example/x.tgz", False),
        ("https://registry.yarnpkg.com/x.tgz", False),
        ("https://[::1", False),
    ],
)
def test_is_canonical_registry_url_admits_only_bare_https_npmjs(url: str, trusted: bool) -> None:
    assert _is_canonical_registry_url(url) is trusted


# --- source fields in JSON and text lockfiles -------------------------------------------------------


def test_json_source_fields_are_trusted_walks_nested_structures() -> None:
    assert _json_source_fields_are_trusted({"a": [{"Resolved": _RESOLVED}], "b": {"resolution": {"integrity": "x"}}})
    assert not _json_source_fields_are_trusted({"packages": {"x": {"resolved": "https://evil.example/x.tgz"}}})
    assert not _json_source_fields_are_trusted([{"tarball": 5}])
    assert not _json_source_fields_are_trusted({"resolution": {"type": "git"}})
    assert _json_source_fields_are_trusted("a string is not a mapping")


def test_text_source_fields_are_trusted_reads_yaml_and_yarn_classic_shapes() -> None:
    assert _text_source_fields_are_trusted(f"  resolution: {{integrity: x}}\n  resolved: '{_RESOLVED}'\n")
    assert _text_source_fields_are_trusted(f'  resolved "{_RESOLVED}#sha1"\n'.replace("#sha1", ""))
    assert not _text_source_fields_are_trusted("  resolved: https://evil.example/x.tgz\n")
    assert not _text_source_fields_are_trusted('  resolved "http://registry.npmjs.org/x.tgz"\n')
    assert _text_source_fields_are_trusted("  version: 5.4.0\n  other: value\n")


# --- lockfiles and control files ----------------------------------------------------------------------


def test_lockfile_uses_trusted_sources_accepts_a_registry_only_npm_lock() -> None:
    assert _lockfile_uses_trusted_sources(_npm_lock()) is True


@pytest.mark.parametrize(
    "lock",
    [
        _npm_lock("https://evil.example/typescript.tgz"),
        _npm_lock("http://registry.npmjs.org/typescript.tgz"),
        _npm_lock("git+https://github.com/x/y.git"),
        _npm_lock("file:../typescript"),
        _npm_lock("../typescript"),
        _npm_lock("C:/typescript"),
        b"\xff\xfe",
        b'{"a": "see https://registry.npmjs.org/x and also https:evil"}',
    ],
    ids=["foreign-host", "plain-http", "git", "file", "relative", "drive", "not-utf8", "stray-https-scheme"],
)
def test_lockfile_uses_trusted_sources_refuses_every_non_registry_source(lock: bytes) -> None:
    assert _lockfile_uses_trusted_sources(lock) is False


def test_control_files_use_trusted_sources_end_to_end() -> None:
    assert control_files_use_trusted_sources(_manifest(), _npm_lock()) is True


@pytest.mark.parametrize(
    ("manifest", "lock"),
    [
        (b"not json", _npm_lock()),
        (b"[]", _npm_lock()),
        (_manifest(workspaces=["packages/*"]), _npm_lock()),
        (_manifest(overrides={"x": "1"}), _npm_lock()),
        (_manifest(dependencies={"left-pad": "file:../left-pad"}), _npm_lock()),
        (_manifest(dependencies=["typescript"]), _npm_lock()),
        (_manifest(dependencies={"x": 5}), _npm_lock()),
        (_manifest(), _npm_lock("https://evil.example/x.tgz")),
        (b"{" + b" " * MAX_CONTROL_FILE_BYTES + b"}", _npm_lock()),
        (_manifest(), b" " * (MAX_CONTROL_FILE_BYTES + 1)),
    ],
    ids=[
        "manifest-not-json",
        "manifest-not-object",
        "workspaces",
        "overrides",
        "file-dependency",
        "dependencies-not-mapping",
        "non-string-spec",
        "foreign-lock-source",
        "manifest-too-large",
        "lock-too-large",
    ],
)
def test_control_files_use_trusted_sources_refuses_untrusted_control_files(manifest: bytes, lock: bytes) -> None:
    assert control_files_use_trusted_sources(manifest, lock) is False


# --- the node environment ---------------------------------------------------------------------------------


def test_minimal_node_environment_keeps_locale_only_and_pins_path() -> None:
    env = _minimal_node_environment({"LANG": "C.UTF-8", "LC_ALL": "C", "NPM_TOKEN": "x", "PATH": "/evil"})
    assert env["LANG"] == "C.UTF-8" and env["LC_ALL"] == "C"
    assert "NPM_TOKEN" not in env
    assert env["PATH"] != "/evil"
    assert env["PATH"].endswith("System32") if "SYSTEMROOT" in env else env["PATH"] == "/usr/bin:/bin"
