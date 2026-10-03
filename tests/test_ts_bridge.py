"""Tests for `graphite.ts_bridge`, the optional TypeScript compiler bridge.

Named after the module so aramid's mutation stage 1 (`tests/test_<module>.py`)
has something to run; `-k ts_bridge` selected nothing before this file. The
resolver suites reach this module only through a real `node`, so the index
contract, the fallback reasons and the one load-bearing subprocess argument
(the UTF-8 codec, see the comment in the module) are pinned here without
node at all.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from graphite import ts_bridge
from graphite.config import Config
from graphite.ts_bridge import (
    TypeScriptCompilerEdge,
    TypeScriptCompilerIndex,
    _edge_from_raw,
    build_typescript_index,
)


@dataclass(frozen=True)
class _Entry:
    rel_path: str
    language: str


def _raw_edge(**changes: object) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "source": "src/a.ts",
        "target": "src/b.ts",
        "specifier": "./b",
        "syntax": "import",
        "relation": "imports",
        "confidence": "high",
        "line": 3,
    }
    raw.update(changes)
    return raw


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["node"], returncode, stdout=stdout, stderr=stderr)


# --- the index --------------------------------------------------------------------


def test_index_lookups_normalise_posix_paths_before_matching() -> None:
    # `./` prefixes and doubled separators collapse; a backslash does NOT (it
    # is a plain character to PurePosixPath), so callers must pass posix paths.
    edge = TypeScriptCompilerEdge("src/a.ts", "src/b.ts", "./b", "import", "imports", "high")
    extra = TypeScriptCompilerEdge("src/a.ts", "src/c.ts", "c", "export", "reexports", "high")
    index = TypeScriptCompilerIndex(
        available=True,
        import_map={("src/a.ts", "./b"): edge},
        edges_by_file={"src/a.ts": (extra,)},
    )
    assert index.resolve_import("./src//a.ts", "./b") is edge
    assert index.resolve_import("src/a.ts", "./missing") is None
    assert index.resolve_import("src\\a.ts", "./b") is None
    assert index.supplemental_edges("./src/a.ts") == (extra,)
    assert index.supplemental_edges("src/other.ts") == ()


# --- _edge_from_raw -----------------------------------------------------------------


def test_edge_from_raw_normalises_paths_and_keeps_the_line() -> None:
    edge = _edge_from_raw(_raw_edge(source="./src//a.ts", target="src/./b.ts", line="7"))
    assert edge == TypeScriptCompilerEdge("src/a.ts", "src/b.ts", "./b", "import", "imports", "high", line=7)


def test_edge_from_raw_leaves_the_line_unset_when_node_sent_none() -> None:
    edge = _edge_from_raw(_raw_edge(line=None))
    assert edge is not None and edge.line is None


@pytest.mark.parametrize(
    "raw",
    [
        {k: v for k, v in _raw_edge().items() if k != "specifier"},
        _raw_edge(line="not a number"),
        _raw_edge(source="src/same.ts", target="src/same.ts"),
        _raw_edge(source="./x.ts", target="x.ts"),
    ],
    ids=["missing-field", "non-integer-line", "self-edge", "self-edge-after-normalising"],
)
def test_edge_from_raw_drops_records_it_cannot_trust(raw: dict[str, Any]) -> None:
    assert _edge_from_raw(raw) is None


def test_edge_from_raw_normalises_a_blank_path_to_dot_rather_than_dropping_it() -> None:
    # Found while writing this file: `PurePosixPath("").as_posix()` is ".", so
    # the `not source` / `not target` guard can never fire on an empty string
    # and a blank path reaches the index as ".". Node never sends one today;
    # pinned so a fix to the guard is a deliberate change, not a surprise.
    edge = _edge_from_raw(_raw_edge(source=""))
    assert edge is not None and edge.source == "."
    edge = _edge_from_raw(_raw_edge(target=""))
    assert edge is not None and edge.target == "."


# --- build_typescript_index: the fallbacks ---------------------------------------------


@pytest.mark.parametrize("mode", ["disabled", "heuristic", "OFF", " none ", "0", "false"])
def test_build_index_is_disabled_by_any_disabling_mode_without_starting_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("node must not start when the resolver is disabled")

    monkeypatch.setattr(ts_bridge.subprocess, "run", never)
    entries = [_Entry("src/a.ts", "typescript")]
    index = build_typescript_index(tmp_path, entries, Config(typescript_resolver=mode))
    assert index == TypeScriptCompilerIndex(available=False, reason="disabled")


def test_build_index_reports_no_typescript_files_without_starting_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("node must not start with nothing to resolve")

    monkeypatch.setattr(ts_bridge.subprocess, "run", never)
    entries = [_Entry("main.py", "python"), _Entry("notes.md", "markdown")]
    index = build_typescript_index(tmp_path, entries, Config())
    assert index == TypeScriptCompilerIndex(available=False, reason="no_typescript_files")


@pytest.fixture
def fake_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Resolve `node` to a fixed absolute path, so a test that stubs
    `subprocess.run` does not also depend on a real node being on PATH."""
    node = tmp_path / "toolchain" / "node.exe"
    monkeypatch.setattr(ts_bridge, "_node_executable", lambda root: node)
    return node


@pytest.mark.parametrize(
    ("raised", "reason"),
    [
        (FileNotFoundError("node"), "node_not_available"),
        (subprocess.TimeoutExpired(["node"], 10.0), "timeout"),
        (PermissionError("denied"), "bridge_error: denied"),
    ],
)
def test_build_index_turns_a_launch_failure_into_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raised: Exception, reason: str, fake_node: Path
) -> None:
    def failing(*args: object, **kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(ts_bridge.subprocess, "run", failing)
    index = build_typescript_index(tmp_path, [_Entry("src/a.ts", "typescript")], Config())
    assert index == TypeScriptCompilerIndex(available=False, reason=reason)


def test_build_index_reports_a_failing_node_with_its_stderr_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_node: Path
) -> None:
    monkeypatch.setattr(
        ts_bridge.subprocess, "run", lambda *a, **k: _completed(stderr="  boom " + "x" * 600, returncode=2)
    )
    index = build_typescript_index(tmp_path, [_Entry("src/a.ts", "typescript")], Config())
    assert index.available is False
    assert index.reason is not None
    assert index.reason.startswith("node_exit_2: boom ")
    assert len(index.reason) <= len("node_exit_2: ") + 500


def test_build_index_reports_unparseable_and_declined_replies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_node: Path
) -> None:
    entries = [_Entry("src/a.ts", "typescript")]
    monkeypatch.setattr(ts_bridge.subprocess, "run", lambda *a, **k: _completed(stdout="{not json"))
    index = build_typescript_index(tmp_path, entries, Config())
    assert index.available is False and (index.reason or "").startswith("invalid_json: ")

    monkeypatch.setattr(
        ts_bridge.subprocess, "run", lambda *a, **k: _completed(stdout=json.dumps({"ok": False, "reason": "no_tsconfig"}))
    )
    assert build_typescript_index(tmp_path, entries, Config()) == TypeScriptCompilerIndex(
        available=False, reason="no_tsconfig"
    )

    monkeypatch.setattr(ts_bridge.subprocess, "run", lambda *a, **k: _completed(stdout=json.dumps({"ok": False})))
    assert build_typescript_index(tmp_path, entries, Config()).reason == "unavailable"


# --- build_typescript_index: the happy path -----------------------------------------------


def test_build_index_sends_the_sorted_typescript_files_as_utf8_and_indexes_the_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_node: Path
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    reply = {
        "ok": True,
        "typescriptVersion": "5.4.0",
        "configPath": "tsconfig.json",
        "edges": [
            _raw_edge(),
            _raw_edge(source="src/a.ts", target="src/c.ts", specifier="c", syntax="export", relation="reexports"),
            _raw_edge(source="src/a.ts", target="src/d.ts", specifier="d", syntax="reference", relation="references"),
            _raw_edge(source="src/same.ts", target="src/same.ts"),  # dropped
        ],
    }

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return _completed(stdout=json.dumps(reply))

    monkeypatch.setattr(ts_bridge.subprocess, "run", fake_run)
    entries = [
        _Entry("src/z.tsx", "tsx"),
        _Entry("./src/a.ts", "typescript"),
        _Entry("main.py", "python"),
        _Entry("lib/j.js", "javascript"),
    ]
    cfg = Config(typescript_resolver="auto", typescript_resolver_timeout_seconds=7.5, typescript_symbol_references=False)
    index = build_typescript_index(tmp_path / "répo", entries, cfg)

    (argv, kwargs), = calls
    assert argv[0] == str(fake_node) and argv[1].endswith("ts_resolver.mjs")
    assert json.loads(kwargs["input"]) == {
        "root": str(tmp_path / "répo"),
        "files": ["lib/j.js", "src/a.ts", "src/z.tsx"],
        "symbolReferences": False,
    }
    assert "répo" in kwargs["input"]  # ensure_ascii=False: the path is sent verbatim
    assert kwargs["encoding"] == "utf-8"  # the load-bearing codec; cp1252 cannot round-trip it
    assert kwargs["timeout"] == 7.5
    assert kwargs["check"] is False

    assert index.available is True
    assert index.typescript_version == "5.4.0"
    assert index.config_path == "tsconfig.json"
    assert index.resolve_import("src/a.ts", "./b") == _edge_from_raw(_raw_edge())
    assert [e.syntax for e in index.supplemental_edges("src/a.ts")] == ["export", "reference"]
    assert index.supplemental_edges("src/same.ts") == ()


# --- which node is launched (channel round 304) -------------------------------


def test_node_is_launched_by_an_absolute_path_outside_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    trusted = tmp_path / "trusted bin"
    repo.mkdir()
    trusted.mkdir()
    real = trusted / ("node.exe" if os.name == "nt" else "node")
    real.write_text("", encoding="utf-8")
    real.chmod(0o755)
    (repo / real.name).write_text("", encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", os.pathsep.join(("", ".", str(repo), str(trusted))))
    seen: list[list[str]] = []

    def record(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return _completed(stdout=json.dumps({"ok": True, "edges": []}))

    monkeypatch.setattr(ts_bridge.subprocess, "run", record)

    build_typescript_index(repo, [_Entry("src/a.ts", "typescript")], Config())

    assert [Path(argv[0]) for argv in seen] == [real.resolve()]


def test_no_node_outside_the_repo_reads_as_node_not_available_without_launching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ("node.exe" if os.name == "nt" else "node")).write_text("", encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", str(repo))
    monkeypatch.setattr(ts_bridge.subprocess, "run", _must_not_launch)

    index = build_typescript_index(repo, [_Entry("src/a.ts", "typescript")], Config())

    assert index == TypeScriptCompilerIndex(available=False, reason="node_not_available")


def _must_not_launch(*args: object, **kwargs: object) -> None:
    raise AssertionError("node was launched")
