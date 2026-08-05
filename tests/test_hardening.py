"""Hardening tests for graph accuracy and development workflow helpers."""
from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from graphite.cli import main
from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.analyze import analyze
from graphite.graph import build_graph
from graphite.ingest import IngestError, collect_files
import graphite.git as git_module
import graphite.ingest as ingest_module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _FakeGitProcess:
    def __init__(self, output: bytes, returncode: int = 0) -> None:
        self.stdout = io.BytesIO(output)
        self.returncode = returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        pass


def test_ingest_skips_generated_tooling_and_vendor_dirs(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app.ts", "export const app = 1;\n")
    _write(tmp_path / "graph-out" / "old.ts", "export const stale = 1;\n")
    _write(tmp_path / ".cache" / "graphite" / "cached.ts", "export const cached = 1;\n")
    _write(tmp_path / "tools" / "graphite" / "src" / "tool.ts", "export const tool = 1;\n")
    _write(tmp_path / "vendor" / "lib.ts", "export const vendor = 1;\n")
    _write(tmp_path / "src" / "__pycache__" / "ignored.py", "x = 1\n")

    rel_paths = [e.rel_path for e in collect_files(tmp_path, Config(include_dotfiles=True))]

    assert "src/app.ts" in rel_paths
    assert "graph-out/old.ts" not in rel_paths
    assert ".cache/graphite/cached.ts" not in rel_paths
    assert "tools/graphite/src/tool.ts" not in rel_paths
    assert "vendor/lib.ts" not in rel_paths
    assert "src/__pycache__/ignored.py" not in rel_paths


def test_ingest_git_enumeration_uses_shared_hardened_runner(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    repository_bin = root / "tools-bin"
    trusted_bin = tmp_path / "trusted-bin"
    repository_bin.mkdir(parents=True)
    trusted_bin.mkdir()
    (root / ".git").mkdir()
    _write(root / "src" / "app.py", "value = 1\n")
    fake_name = "git.exe" if os.name == "nt" else "git"
    _write(repository_bin / fake_name, "malicious")
    _write(trusted_bin / fake_name, "trusted")
    if os.name != "nt":
        (repository_bin / fake_name).chmod(0o700)
        (trusted_bin / fake_name).chmod(0o700)
    monkeypatch.setenv("PATH", str(repository_bin) + os.pathsep + str(trusted_bin))
    monkeypatch.setenv("GiT_DiR", "private")
    monkeypatch.setenv("git_index_file", "private")
    calls = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        if command[1:] == ["--version"]:
            return _FakeGitProcess(b"git version 2.38.0\n")
        return _FakeGitProcess(b"src/app.py\0")

    monkeypatch.setattr(git_module.subprocess, "Popen", fake_popen)

    entries = collect_files(root, Config())

    assert [entry.rel_path for entry in entries] == ["src/app.py"]
    assert len(calls) == 2
    assert calls[0][0][1:] == ["--version"]
    command, kwargs = calls[1]
    assert Path(command[0]) == (trusted_bin / fake_name).resolve()
    assert command[1:4] == ["--no-optional-locks", "-c", "core.fsmonitor=false"]
    assert command[4:6] == ["-c", f"safe.directory={root.resolve()}"]
    assert command[6:] == [
        "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    ]
    assert kwargs["shell"] is False
    assert {name for name in kwargs["env"] if name.casefold().startswith("git_")} == {
        "GIT_OPTIONAL_LOCKS",
        "GIT_TERMINAL_PROMPT",
    }


def test_git_runner_enumerates_a_differently_owned_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(root)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _write(root / "tracked.py", "value = 1\n")
    subprocess.run(
        ["git", "-C", str(root), "add", "tracked.py"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    different_owner_environment = {
        name: value
        for name, value in os.environ.items()
        if not name.casefold().startswith("git_")
    }
    ownership_test_git_environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "nonexistent-global-git-config"),
        "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1",
    }
    different_owner_environment.update(ownership_test_git_environment)
    baseline = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        env=different_owner_environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if baseline.returncode == 0:
        pytest.skip("installed Git does not trigger different-owner protection")
    baseline_diagnostic = baseline.stderr.decode("utf-8", errors="replace").casefold()
    for repository_path in (str(root), root.as_posix()):
        baseline_diagnostic = baseline_diagnostic.replace(
            repository_path.casefold(), "<repository>"
        )
    assert "dubious ownership" in baseline_diagnostic
    assert "safe.directory" in baseline_diagnostic

    original_isolated_environment = git_module._isolated_environment

    def different_owner_isolated_environment() -> dict[str, str]:
        environment = original_isolated_environment()
        environment.update(ownership_test_git_environment)
        return environment

    monkeypatch.setattr(
        git_module, "_isolated_environment", different_owner_isolated_environment
    )

    try:
        result = git_module.GitRunner(root).run(
            ["ls-files", "-z"], timeout_seconds=2
        )
    except git_module.GitUnsupportedVersionError:
        pytest.skip("installed Git does not support protected command configuration")

    assert result.returncode == 0
    assert result.stdout == b"tracked.py\0"


def test_ingest_fails_closed_when_repository_path_has_only_fake_git(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    repository_bin = root / "tools-bin"
    repository_bin.mkdir(parents=True)
    (root / ".git").mkdir()
    _write(root / "src" / "app.py", "value = 1\n")
    fake_name = "git.exe" if os.name == "nt" else "git"
    _write(repository_bin / fake_name, "malicious")
    if os.name != "nt":
        (repository_bin / fake_name).chmod(0o700)
    monkeypatch.setenv("PATH", str(repository_bin))

    with pytest.raises(RuntimeError, match="unable to enumerate Git repository safely"):
        collect_files(root, Config())


@pytest.mark.parametrize(
    "failure_mode",
    ["unavailable", "timeout", "output_limit", "nonzero", "malformed", "invalid_utf8"],
)
def test_git_repository_enumeration_failures_abort_without_fallback(
    tmp_path: Path, monkeypatch, failure_mode: str
) -> None:
    root = tmp_path / "repository"
    (root / ".git").mkdir(parents=True)
    _write(root / "ignored" / "secret.env", "SECRET=private\n")

    class FakeRunner:
        def __init__(self, project_root):
            if failure_mode == "unavailable":
                raise git_module.GitUnavailableError("private")

        def run(self, arguments, *, timeout_seconds, **kwargs):
            if failure_mode == "timeout":
                raise git_module.GitTimeoutError("private")
            if failure_mode == "output_limit":
                error_type = getattr(
                    git_module, "GitOutputLimitError", git_module.GitError
                )
                raise error_type("private")
            if failure_mode == "nonzero":
                return subprocess.CompletedProcess(arguments, 128, b"", b"")
            if failure_mode == "malformed":
                return subprocess.CompletedProcess(arguments, 0, b"missing-nul", b"")
            return subprocess.CompletedProcess(arguments, 0, b"invalid-\xff.py\0", b"")

    def must_not_walk(*args, **kwargs):
        raise AssertionError("Git repository must not use filesystem fallback")

    def must_not_read(*args, **kwargs):
        raise AssertionError("ignored secret must not be read or hashed")

    monkeypatch.setattr(ingest_module, "GitRunner", FakeRunner)
    monkeypatch.setattr(ingest_module, "_walk_files", must_not_walk)
    monkeypatch.setattr(ingest_module, "_is_binary", must_not_read)
    monkeypatch.setattr(ingest_module, "file_hash", must_not_read)

    with pytest.raises(RuntimeError, match="unable to enumerate Git repository safely"):
        collect_files(root, Config(include_dotfiles=True))


def test_non_git_directory_still_uses_filesystem_walk(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app.py", "value = 1\n")

    assert [entry.rel_path for entry in collect_files(tmp_path, Config())] == [
        "src/app.py"
    ]


def test_nested_path_inside_git_repository_fails_closed_without_walk(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "nested"
    _write(nested / "ignored" / "secret.env", "SECRET=private\n")

    def must_not_walk(*args, **kwargs):
        raise AssertionError("nested Git root must not use filesystem walk")

    def must_not_read(*args, **kwargs):
        raise AssertionError("nested ignored file must not be read or hashed")

    monkeypatch.setattr(ingest_module, "_walk_files", must_not_walk)
    monkeypatch.setattr(ingest_module, "_is_binary", must_not_read)
    monkeypatch.setattr(ingest_module, "file_hash", must_not_read)

    with pytest.raises(IngestError, match="nested Git roots are unsupported"):
        collect_files(nested, Config(include_dotfiles=True))


def test_git_enumeration_applies_max_files_before_returning_paths(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    (root / ".git").mkdir(parents=True)
    for index in range(5):
        _write(root / f"file-{index}.py", f"value = {index}\n")

    class FakeRunner:
        def __init__(self, project_root):
            assert project_root == root.resolve()

        def run(self, arguments, *, timeout_seconds, **kwargs):
            return subprocess.CompletedProcess(
                arguments,
                0,
                b"file-0.py\0file-1.py\0..\\record-beyond-cap.py\0",
                b"",
            )

    monkeypatch.setattr(ingest_module, "GitRunner", FakeRunner)

    assert ingest_module._git_ls_files(root.resolve(), max_files=2) == [
        "file-0.py",
        "file-1.py",
    ]


@pytest.mark.parametrize(
    ("max_files", "expected"),
    [
        (1, ["src/app.py"]),
        (2, ["src/app.py", "src/next.py"]),
        (
            3,
            [
                "custom-artifacts-source/kept.py",
                "src/app.py",
                "src/next.py",
            ],
        ),
    ],
)
def test_git_max_files_counts_only_eligible_ingest_paths(
    tmp_path: Path, monkeypatch, max_files: int, expected: list[str]
) -> None:
    root = tmp_path / "repository"
    (root / ".git").mkdir(parents=True)
    ordered_paths = [
        "custom-artifacts/generated.py",
        "custom-cache/cached.py",
        ".hidden.py",
        "vendor/lib.py",
        "build/generated.py",
        "assets/image.png",
        "src/app.py",
        "src/next.py",
        "custom-artifacts-source/kept.py",
    ]
    for path in ordered_paths:
        _write(root / path, "value = 1\n")

    class FakeRunner:
        def __init__(self, project_root):
            assert project_root == root.resolve()

        def run(self, arguments, *, timeout_seconds, **kwargs):
            output = "\0".join(ordered_paths).encode("utf-8") + b"\0"
            return subprocess.CompletedProcess(arguments, 0, output, b"")

    hashed: list[str] = []

    def record_hash(path: Path) -> str:
        hashed.append(path.relative_to(root).as_posix())
        return "hash"

    monkeypatch.setattr(ingest_module, "GitRunner", FakeRunner)
    monkeypatch.setattr(ingest_module, "_is_binary", lambda _path: False)
    monkeypatch.setattr(ingest_module, "file_hash", record_hash)

    entries = collect_files(
        root,
        Config(
            output_dir=root / "custom-artifacts",
            cache_dir=root / "custom-cache",
            max_files=max_files,
        ),
    )

    assert [entry.rel_path for entry in entries] == expected
    assert sorted(hashed) == expected


def test_walk_max_files_stops_iteration_before_materializing_all_paths(
    tmp_path: Path, monkeypatch
) -> None:
    _write(tmp_path / "first.py", "first = 1\n")
    _write(tmp_path / "second.py", "second = 2\n")

    def bounded_walk(*args, **kwargs):
        yield "first.py"
        yield "second.py"
        raise AssertionError("walk iterated beyond max_files")

    monkeypatch.setattr(ingest_module, "_walk_files", bounded_walk)

    entries = collect_files(tmp_path, Config(max_files=2))

    assert [entry.rel_path for entry in entries] == ["first.py", "second.py"]


def test_git_file_listing_rejects_invalid_utf8(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    (root / ".git").mkdir(parents=True)

    class FakeRunner:
        def __init__(self, project_root):
            assert project_root == root.resolve()

        def run(self, arguments, *, timeout_seconds):
            return subprocess.CompletedProcess(arguments, 0, b"invalid-\xff.py\0", b"")

    monkeypatch.setattr(ingest_module, "GitRunner", FakeRunner)

    with pytest.raises(RuntimeError, match="unable to enumerate Git repository safely"):
        ingest_module._git_ls_files(root.resolve())


@pytest.mark.skipif(os.name != "nt", reason="Windows native path containment")
@pytest.mark.parametrize(
    "hostile_path",
    [r"..\secret.py", r"C:\secret.py", r"\\server\share\secret.py"],
)
def test_git_file_listing_rejects_windows_escape_paths(
    tmp_path: Path, monkeypatch, hostile_path: str
) -> None:
    root = tmp_path / "repository"
    (root / ".git").mkdir(parents=True)

    class FakeRunner:
        def __init__(self, project_root):
            assert project_root == root.resolve()

        def run(self, arguments, *, timeout_seconds):
            return subprocess.CompletedProcess(
                arguments, 0, hostile_path.encode("utf-8") + b"\0", b""
            )

    monkeypatch.setattr(ingest_module, "GitRunner", FakeRunner)

    with pytest.raises(RuntimeError, match="unable to enumerate Git repository safely"):
        ingest_module._git_ls_files(root.resolve())


@pytest.mark.skipif(os.name == "nt", reason="POSIX literal backslash behavior")
def test_git_file_listing_preserves_safe_posix_literal_backslash(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    (root / ".git").mkdir(parents=True)
    _write(root / r"..\secret.py", "safe literal filename\n")

    class FakeRunner:
        def __init__(self, project_root):
            assert project_root == root.resolve()

        def run(self, arguments, *, timeout_seconds):
            return subprocess.CompletedProcess(arguments, 0, b"..\\secret.py\0", b"")

    monkeypatch.setattr(ingest_module, "GitRunner", FakeRunner)

    assert ingest_module._git_ls_files(root.resolve()) == [r"..\secret.py"]


def test_collect_files_never_reads_git_listed_symlink_outside_root(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    outside = tmp_path / "secret.py"
    link = root / "linked.py"
    (root / ".git").mkdir(parents=True)
    _write(root / "src" / "safe.py", "safe = True\n")
    _write(outside, "secret = True\n")
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    class FakeRunner:
        def __init__(self, project_root):
            assert project_root == root.resolve()

        def run(self, arguments, *, timeout_seconds):
            return subprocess.CompletedProcess(arguments, 0, b"linked.py\0", b"")

    def guarded_binary(path: Path) -> bool:
        assert path.resolve() != outside.resolve()
        return False

    def guarded_hash(path: Path) -> str:
        assert path.resolve() != outside.resolve()
        return "safe-hash"

    monkeypatch.setattr(ingest_module, "GitRunner", FakeRunner)
    monkeypatch.setattr(
        ingest_module,
        "_walk_files",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe Git record must not fall back to filesystem walk")
        ),
    )
    monkeypatch.setattr(ingest_module, "_is_binary", guarded_binary)
    monkeypatch.setattr(ingest_module, "file_hash", guarded_hash)

    with pytest.raises(RuntimeError, match="unable to enumerate Git repository safely"):
        collect_files(root, Config())


def test_collect_files_final_boundary_rejects_symlink_escape(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    outside = tmp_path / "secret.py"
    link = root / "linked.py"
    root.mkdir()
    _write(outside, "secret = True\n")
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    monkeypatch.setattr(
        ingest_module, "_git_ls_files", lambda _root, **_kwargs: ["linked.py"]
    )

    def must_not_read(_path: Path):
        raise AssertionError("external file must not be read or hashed")

    monkeypatch.setattr(ingest_module, "_is_binary", must_not_read)
    monkeypatch.setattr(ingest_module, "file_hash", must_not_read)

    assert collect_files(root, Config()) == []


def test_internal_symlink_keeps_distinct_identity_in_git_and_walk_modes(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repository"
    target = root / "src" / "app.py"
    alias = root / "alias.py"
    (root / ".git").mkdir(parents=True)
    _write(target, "value = 1\n")
    try:
        alias.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    class FakeRunner:
        def __init__(self, project_root):
            assert project_root == root.resolve()

        def run(self, arguments, *, timeout_seconds):
            return subprocess.CompletedProcess(
                arguments, 0, b"alias.py\0src/app.py\0", b""
            )

    monkeypatch.setattr(ingest_module, "GitRunner", FakeRunner)

    assert ingest_module._git_ls_files(root.resolve()) == [
        "alias.py",
        "src/app.py",
    ]
    assert [entry.rel_path for entry in collect_files(root, Config())] == [
        "alias.py",
        "src/app.py",
    ]

    monkeypatch.setattr(
        ingest_module, "_git_ls_files", lambda _root, **_kwargs: None
    )

    assert [entry.rel_path for entry in collect_files(root, Config())] == [
        "alias.py",
        "src/app.py",
    ]


def test_filesystem_fallback_prunes_dynamic_dirs_by_component_prefix(tmp_path: Path) -> None:
    output_dir = tmp_path / "nested" / "custom-artifacts"
    cache_dir = tmp_path / "state" / "custom-cache"
    _write(tmp_path / "src" / "app.py", "value = 1\n")
    _write(output_dir / "graph.json", "{}\n")
    _write(output_dir / "nested" / "generated.py", "value = 2\n")
    _write(cache_dir / "cached.py", "value = 3\n")
    _write(tmp_path / "nested" / "custom-artifacts-source" / "kept.py", "value = 4\n")

    entries = collect_files(
        tmp_path,
        Config(output_dir=output_dir, cache_dir=cache_dir, include_dotfiles=True),
    )

    assert [entry.rel_path for entry in entries] == [
        "nested/custom-artifacts-source/kept.py",
        "src/app.py",
    ]


def test_relative_dynamic_dirs_resolve_from_process_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "project"
    _write(root / "src" / "app.py", "value = 1\n")
    _write(root / "custom-artifacts" / "graph.json", "{}\n")
    _write(root / "custom-cache" / "cached.py", "value = 2\n")
    _write(root / "custom-artifacts-source" / "kept.py", "value = 3\n")
    monkeypatch.chdir(tmp_path)

    entries = collect_files(
        root,
        Config(
            output_dir=Path("project/custom-artifacts"),
            cache_dir=Path("project/custom-cache"),
            include_dotfiles=True,
        ),
    )

    assert [entry.rel_path for entry in entries] == [
        "custom-artifacts-source/kept.py",
        "src/app.py",
    ]


def test_dynamic_dirs_outside_root_and_root_itself_do_not_exclude_sources(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    _write(root / "src" / "app.py", "value = 1\n")

    entries = collect_files(
        root,
        Config(output_dir=root, cache_dir=tmp_path / "outside-cache"),
    )

    assert [entry.rel_path for entry in entries] == ["src/app.py"]


def test_custom_output_and_cache_never_self_ingest_or_make_review_stale(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "repository"
    output_dir = root / "custom-artifacts"
    cache_dir = root / "custom-cache"
    _write(root / "src" / "app.py", "value = 1\n")
    _write(root / "custom-artifacts-source" / "kept.py", "value = 2\n")
    subprocess.run(
        ["git", "init"],
        cwd=root,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    monkeypatch.chdir(root)
    common = [
        "--output-dir",
        str(output_dir),
        "--cache-dir",
        str(cache_dir),
        "--typescript-resolver",
        "disabled",
    ]

    assert main([*common, "build", str(root)]) == 0
    capsys.readouterr()
    assert main(
        [
            *common,
            "review-changes",
            str(root),
            "src/app.py",
            "--graph-json",
            "custom-artifacts/graph.json",
            "--json",
        ]
    ) == 0

    review = json.loads(capsys.readouterr().out)
    assert review["graph"]["status"]["stale"] is False

    assert main([*common, "build", str(root)]) == 0
    capsys.readouterr()
    manifest = json.loads((output_dir / ".graphite_manifest.json").read_text(encoding="utf-8"))
    graph_bundle = json.loads((output_dir / "graph.json").read_text(encoding="utf-8"))
    manifest_paths = {item["rel_path"] for item in manifest["files"]}
    graph_source_paths = {
        item["source_file"]
        for item in graph_bundle["nodes"]
        if isinstance(item.get("source_file"), str)
    }

    assert "custom-artifacts-source/kept.py" in manifest_paths
    assert all(not path.startswith("custom-artifacts/") for path in manifest_paths)
    assert all(not path.startswith("custom-cache/") for path in manifest_paths)
    assert all(not path.startswith("custom-artifacts/") for path in graph_source_paths)
    assert all(not path.startswith("custom-cache/") for path in graph_source_paths)


def test_ts_import_resolution_handles_index_files_and_tsconfig_paths(tmp_path: Path) -> None:
    _write(tmp_path / "tsconfig.json", json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"@lib/*": ["src/lib/*"]}}}))
    _write(tmp_path / "src" / "util" / "index.ts", "export const util = 1;\n")
    _write(tmp_path / "src" / "lib" / "service.ts", "export const service = 1;\n")
    _write(
        tmp_path / "src" / "app.ts",
        "import { util } from './util';\nimport { service } from '@lib/service';\nexport function run() { return util + service; }\n",
    )

    entries = collect_files(tmp_path, Config())
    result = extract_all(entries, Config(workers=1, typescript_resolver="disabled", cache_dir=tmp_path / ".cache" / "graphite"))
    imports = {(e["source"], e["target"], e["relation"], e.get("confidence")) for e in result.edges if e["relation"] == "imports"}

    assert ("src_app", "src_util_index", "imports", "EXACT_IMPORT") in imports
    assert ("src_app", "src_lib_service", "imports", "EXACT_IMPORT") in imports


def test_ts_call_noise_filter_keeps_local_calls_and_drops_builtin_member_calls(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "app.ts",
        "const items = [1, 2];\nfunction localHelper() { return 1; }\nexport function run() { items.map(x => x); JSON.parse('{}'); console.log(localHelper()); }\n",
    )

    entries = collect_files(tmp_path, Config())
    result = extract_all(entries, Config(workers=1, typescript_resolver="disabled", cache_dir=tmp_path / ".cache" / "graphite"))
    call_targets = {e["target"] for e in result.edges if e["relation"] == "calls"}

    assert "src_app_localhelper" in call_targets
    assert "src_app_items_map" not in call_targets
    assert "src_app_json_parse" not in call_targets
    assert "src_app_console_log" not in call_targets


def test_check_reports_fresh_and_then_stale_changed_file(tmp_path: Path, monkeypatch, capsys) -> None:
    _write(tmp_path / "src" / "app.ts", "export const app = 1;\n")
    monkeypatch.chdir(tmp_path)

    assert main(["build", "."]) == 0
    capsys.readouterr()

    assert main(["check", ".", "--json"]) == 0
    fresh = json.loads(capsys.readouterr().out)
    assert fresh["stale"] is False

    _write(tmp_path / "src" / "app.ts", "export const app = 2;\n")
    assert main(["check", ".", "--json"]) == 1
    stale = json.loads(capsys.readouterr().out)
    assert stale["stale"] is True
    assert "src/app.ts" in stale["changed"]


def test_check_names_engine_staleness_reason_without_json(tmp_path: Path, monkeypatch, capsys) -> None:
    _write(tmp_path / "src" / "app.ts", "export const app = 1;\n")
    monkeypatch.chdir(tmp_path)
    assert main(["build", "."]) == 0
    capsys.readouterr()

    manifest_path = tmp_path / "graph-out" / ".graphite_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["engine"] = "stale-engine-identity"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert main(["check", "."]) == 1
    out = capsys.readouterr().out
    assert "graph is stale" in out
    assert "engine_changed" in out


def test_check_ignore_engine_reports_source_drift_only(tmp_path: Path, monkeypatch, capsys) -> None:
    _write(tmp_path / "src" / "app.ts", "export const app = 1;\n")
    monkeypatch.chdir(tmp_path)
    assert main(["build", "."]) == 0
    capsys.readouterr()

    manifest_path = tmp_path / "graph-out" / ".graphite_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["engine"] = "stale-engine-identity"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    # Engine drift alone is ignored: sources unchanged -> fresh, exit 0.
    assert main(["check", ".", "--ignore-engine"]) == 0
    assert "graph is fresh" in capsys.readouterr().out

    # Source drift still fails under --ignore-engine, with the reason named.
    _write(tmp_path / "src" / "app.ts", "export const app = 2;\n")
    assert main(["check", ".", "--ignore-engine"]) == 1
    out = capsys.readouterr().out
    assert "graph is stale (source changes)" in out
    assert "src/app.ts" in out


def test_impact_suggests_reverse_dependencies_and_likely_tests(tmp_path: Path, monkeypatch, capsys) -> None:
    _write(tmp_path / "src" / "store.ts", "export function readStore() { return 1; }\n")
    _write(tmp_path / "src" / "jobs.ts", "import { readStore } from './store';\nexport function runJob() { return readStore(); }\n")
    _write(tmp_path / "tests" / "store.test.ts", "import { readStore } from '../src/store';\ntest('store', () => readStore());\n")
    _write(tmp_path / "tests" / "jobs.test.ts", "import { runJob } from '../src/jobs';\ntest('jobs', () => runJob());\n")
    monkeypatch.chdir(tmp_path)

    assert main(["build", "."]) == 0
    capsys.readouterr()

    assert main(["impact", "src/store.ts", "--graph-json", "graph-out/graph.json", "--json"]) == 0
    impact = json.loads(capsys.readouterr().out)

    assert "src/jobs.ts" in impact["impacted_files"]
    assert "tests/store.test.ts" in impact["likely_tests"]
    assert "tests/jobs.test.ts" in impact["likely_tests"]


def test_analysis_rankings_ignore_external_import_nodes() -> None:
    graph = build_graph(
        nodes=[
            {"id": "src_app", "kind": "file", "name": "app.ts", "source_file": "src/app.ts"},
            {"id": "src_store", "kind": "file", "name": "store.ts", "source_file": "src/store.ts"},
            {"id": "src_store_read", "kind": "function", "name": "read", "source_file": "src/store.ts"},
        ],
        edges=[
            {"source": "src_app", "target": "vitest", "relation": "imports", "source_file": "src/app.ts", "confidence": "EXTERNAL_IMPORT"},
            {"source": "src_app", "target": "src_store", "relation": "imports", "source_file": "src/app.ts", "confidence": "EXACT_IMPORT"},
            {"source": "src_store", "target": "src_store_read", "relation": "contains", "source_file": "src/store.ts"},
        ],
    )

    report = analyze(graph, top_n=5)

    assert "vitest" not in {item["id"] for item in report["god_nodes"]}
    assert "vitest" not in {item["id"] for item in report["orphans"]}
    assert all(item["target"] != "vitest" for item in report["surprising_connections"])


# --- zero-arg super() inside a slots dataclass (graphite portability matrix) ---


def test_no_slots_dataclass_uses_zero_arg_super() -> None:
    """`@dataclass(slots=True)` REBUILDS the class, so the `__class__` cell that
    zero-arg `super()` closes over still points at the pre-slots class. Calling
    `super()` from such a method raises

        TypeError: super(type, obj): obj must be an instance or subtype of type

    on Python 3.11 and 3.12. CPython fixed it in 3.13 -- dated empirically by the
    portability matrix, which failed 11 tests on windows-py3.11 and py3.12 and
    passed on py3.13 and py3.14, same OS and same code.

    That is why this is an AST invariant rather than a runtime test: on the 3.14
    the gate runs, the broken form works, so a runtime test could never catch a
    reintroduction. `ApprovedRoutePool.to_dict` carried it and nobody noticed for
    as long as CI ran one Python version.

    Fix is the explicit two-arg form: the class name resolves through the module
    global at call time, which IS the rebuilt class.
    """
    import ast

    offenders = []
    for path in sorted(Path(__file__).parents[1].joinpath("src").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(
                isinstance(dec, ast.Call)
                and any(
                    kw.arg == "slots" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                    for kw in dec.keywords
                )
                for dec in node.decorator_list
            ):
                continue
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "super"
                    and not sub.args
                ):
                    offenders.append(f"{path.name}:{sub.lineno} in {node.name}")

    assert offenders == [], (
        "zero-arg super() inside a slots=True dataclass breaks on Python 3.11/3.12; "
        f"use super(<ClassName>, self) instead: {offenders}"
    )
