"""Tests for Graphite's multi-project daemon."""
from __future__ import annotations

import json
from pathlib import Path

from graphite.cli import main
from graphite.config import Config
from graphite.daemon import BuildResult, DaemonOptions, discover_projects, run_daemon


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_discover_projects_stops_at_project_roots_and_skips_heavy_tool_directories(tmp_path: Path) -> None:
    _write(tmp_path / "app" / "package.json", "{}\n")
    _write(tmp_path / "app" / "src" / "index.ts", "export const app = 1;\n")
    # v4: a workspace nested inside a discovered project is covered by the
    # parent project's graph and must NOT become its own supervised project.
    _write(tmp_path / "app" / "worker" / "package.json", "{}\n")
    _write(tmp_path / "app" / "worker" / "src" / "index.ts", "export const worker = 1;\n")
    _write(tmp_path / "node_modules" / "bad" / "package.json", "{}\n")
    _write(tmp_path / "_tools" / "internal" / "package.json", "{}\n")

    projects = discover_projects(tmp_path)

    assert projects == [tmp_path / "app"]


def test_run_daemon_once_builds_discovered_projects_and_writes_status(tmp_path: Path) -> None:
    project = tmp_path / "alpha"
    _write(project / "package.json", "{}\n")
    _write(project / "src" / "index.ts", "export const alpha = 1;\n")
    state_dir = tmp_path / "state"
    builds: list[tuple[Path, Path, Path, float]] = []

    def fake_build(root: Path, cfg: Config, timeout: float) -> BuildResult:
        builds.append((root, cfg.output_dir, cfg.cache_dir, timeout))
        return BuildResult(success=True, returncode=0, duration_seconds=0.01, stdout="ok")

    status = run_daemon(
        tmp_path,
        Config(),
        DaemonOptions(
            once=True,
            debounce_seconds=0,
            max_builds_per_cycle=10,
            state_dir=state_dir,
            build_timeout_seconds=12,
        ),
        build_project=fake_build,
    )

    assert status["status"] == "ok"
    assert status["project_count"] == 1
    assert status["healthy_projects"] == 1
    assert status["pending_projects"] == 0
    assert len(builds) == 1
    assert builds[0][0] == project
    assert builds[0][1] == project / "graph-out"
    assert builds[0][2] == project / ".cache" / "graphite"
    assert builds[0][3] == 12
    assert (state_dir / "status.json").exists()
    assert (state_dir / "graphite-daemon.log").exists()


def test_daemon_respects_max_builds_per_cycle(tmp_path: Path) -> None:
    for name in ("alpha", "beta"):
        _write(tmp_path / name / "package.json", "{}\n")
        _write(tmp_path / name / "src" / "index.ts", f"export const {name} = 1;\n")
    builds: list[Path] = []

    def fake_build(root: Path, cfg: Config, timeout: float) -> BuildResult:
        builds.append(root)
        return BuildResult(success=True, returncode=0, duration_seconds=0.01)

    status = run_daemon(
        tmp_path,
        Config(),
        DaemonOptions(once=True, debounce_seconds=0, max_builds_per_cycle=1, state_dir=tmp_path / "state"),
        build_project=fake_build,
    )

    assert len(builds) == 1
    assert status["project_count"] == 2
    assert status["pending_projects"] == 1


def test_daemon_cli_once_builds_project_and_status_can_be_read(tmp_path: Path, capsys) -> None:
    project = tmp_path / "app"
    _write(project / "package.json", "{}\n")
    _write(project / "src" / "math.ts", "export const add = (a: number, b: number) => a + b;\n")
    state_dir = tmp_path / "state"

    result = main([
        "daemon",
        str(tmp_path),
        "--once",
        "--debounce",
        "0",
        "--max-builds-per-cycle",
        "2",
        "--state-dir",
        str(state_dir),
    ])
    output = capsys.readouterr().out

    assert result == 0
    assert "daemon status: ok" in output
    assert (project / "graph-out" / "graph.json").exists()

    result = main(["daemon-status", str(tmp_path), "--state-dir", str(state_dir), "--json"])
    status = json.loads(capsys.readouterr().out)

    assert result == 0
    assert status["project_count"] == 1
    assert status["projects"][0]["build_count"] == 1

