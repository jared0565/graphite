"""Tests for Graphite's multi-project daemon."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from graphite.cli import main
from graphite.config import Config
from graphite.daemon import BuildResult, DaemonOptions, discover_projects, run_daemon
from graphite.provider_observer import ProviderObservationSummary


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


def test_discover_projects_honors_graphite_ignore_marker(tmp_path: Path) -> None:
    _write(tmp_path / "app" / "package.json", "{}\n")
    # A third-party checkout (e.g. an SDK) opts out of supervision entirely.
    _write(tmp_path / "sdk" / ".graphite-ignore", "")
    _write(tmp_path / "sdk" / "package.json", "{}\n")
    # The marker prunes the whole subtree, even when the marked directory is
    # not itself a project root.
    _write(tmp_path / "vendor-tree" / ".graphite-ignore", "")
    _write(tmp_path / "vendor-tree" / "nested" / "package.json", "{}\n")

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


def test_daemon_forces_canonical_project_config(tmp_path: Path) -> None:
    project = tmp_path / "app"
    _write(project / "package.json", "{}\n")
    observed: list[Config] = []

    def fake_build(_root: Path, cfg: Config, _timeout: float) -> BuildResult:
        observed.append(cfg)
        return BuildResult(True, 0, 0.01)

    run_daemon(
        tmp_path,
        Config(
            llm_mode="cloud",
            llm_provider="openrouter",
            llm_model="vendor/model",
            llm_base_url="https://provider.invalid/v1",
            llm_api_key="must-not-propagate",
        ),
        DaemonOptions(once=True, debounce_seconds=0, state_dir=tmp_path / "state"),
        build_project=fake_build,
    )

    assert len(observed) == 1
    assert observed[0].llm_mode == "none"
    assert observed[0].llm_provider == "none"
    assert observed[0].llm_model is None
    assert observed[0].llm_base_url is None
    assert observed[0].llm_api_key is None


def test_provider_observation_cannot_delay_or_consume_graph_build_budget(tmp_path: Path) -> None:
    project = tmp_path / "app"
    _write(project / "package.json", "{}\n")
    entered = threading.Event()
    release = threading.Event()
    builds: list[Path] = []

    def blocked_observer() -> ProviderObservationSummary:
        entered.set()
        release.wait(10)
        return ProviderObservationSummary(1, 0, 0, 1, {}, {"probe_timeout": 1})

    def fake_build(root: Path, _cfg: Config, _timeout: float) -> BuildResult:
        assert entered.wait(1)
        builds.append(root)
        return BuildResult(True, 0, 0.01)

    try:
        status = run_daemon(
            tmp_path,
            Config(),
            DaemonOptions(
                once=True,
                debounce_seconds=0,
                max_builds_per_cycle=1,
                state_dir=tmp_path / "state",
            ),
            build_project=fake_build,
            provider_observation_cycle=blocked_observer,
        )
    finally:
        release.set()

    assert builds == [project]
    assert status["healthy_projects"] == 1
    assert status["provider_lifecycle"]["status"] == "observing"


def test_daemon_status_contains_only_sanitized_provider_aggregates(tmp_path: Path) -> None:
    project = tmp_path / "app"
    _write(project / "package.json", "{}\n")
    observed = threading.Event()

    def observer() -> ProviderObservationSummary:
        observed.set()
        return ProviderObservationSummary(
            4,
            0,
            2,
            2,
            {"unavailable": 2, "verification_required": 2},
            {"hash_changed": 2, "probe_timeout": 2},
        )

    def fake_build(_root: Path, _cfg: Config, _timeout: float) -> BuildResult:
        assert observed.wait(1)
        return BuildResult(True, 0, 0.01)

    status = run_daemon(
        tmp_path,
        Config(),
        DaemonOptions(once=True, debounce_seconds=0, state_dir=tmp_path / "state"),
        build_project=fake_build,
        provider_observation_cycle=observer,
    )
    serialized = json.dumps(status)

    assert status["provider_lifecycle"] == {
        "status": "degraded",
        "attempted": 4,
        "deferred": 0,
        "succeeded": 2,
        "failed": 2,
        "state_counts": {"unavailable": 2, "verification_required": 2},
        "reason_counts": {"hash_changed": 2, "probe_timeout": 2},
    }
    for forbidden in ("executable", "endpoint", "header", "credential", "prompt", "source"):
        assert forbidden not in serialized.casefold()


def test_provider_lifecycle_state_cannot_change_canonical_daemon_graph(tmp_path: Path) -> None:
    summaries = (
        ProviderObservationSummary(1, 0, 1, 0, {"active": 1}, {"identity_unchanged": 1}),
        ProviderObservationSummary(1, 0, 0, 1, {"unavailable": 1}, {"probe_timeout": 1}),
        ProviderObservationSummary(1, 0, 0, 1, {}, {"lifecycle_persistence_failed": 1}),
    )
    manifests: list[dict] = []
    for index, summary in enumerate(summaries):
        base = tmp_path / f"case-{index}"
        project = base / "app"
        _write(project / "package.json", "{}\n")
        _write(project / "src" / "index.ts", "export const value = 1;\n")
        run_daemon(
            base,
            Config(),
            DaemonOptions(
                once=True,
                debounce_seconds=0,
                state_dir=base / "state",
            ),
            provider_observation_cycle=lambda summary=summary: summary,
        )
        manifests.append(
            json.loads(
                (project / "graph-out" / ".graphite_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
        )

    assert manifests[0]["engine"] == manifests[1]["engine"] == manifests[2]["engine"]
    assert manifests[0]["files"] == manifests[1]["files"] == manifests[2]["files"]


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


def test_daemon_space_form_suggests_the_hyphenated_subcommand(tmp_path, monkeypatch, capsys) -> None:
    """#9: `graphite daemon status` read `status` as a base path.

    The old error named a path the user never typed, which reads as "that
    directory is missing" rather than "that is not how this is spelled".
    """
    monkeypatch.chdir(tmp_path)

    result = main(["daemon", "status"])
    err = capsys.readouterr().err

    assert result == 2
    assert "daemon-status" in err
    assert str(tmp_path / "status") not in err


def test_daemon_space_form_suggestion_covers_the_whole_subcommand_family(tmp_path, monkeypatch, capsys) -> None:
    """The suffix list is derived from the parser, so it cannot drift."""
    monkeypatch.chdir(tmp_path)

    for suffix in ("health", "startup-status"):
        result = main(["daemon", suffix])
        err = capsys.readouterr().err
        assert result == 2, suffix
        assert f"daemon-{suffix}" in err, suffix


def test_daemon_suggestion_does_not_hijack_a_real_directory(tmp_path, monkeypatch) -> None:
    """A folder genuinely named `status` must still work as a base path."""
    from graphite.cli import _daemon_subcommand_suggestion

    (tmp_path / "status").mkdir()
    monkeypatch.chdir(tmp_path)

    assert _daemon_subcommand_suggestion("status", {"daemon-status"}) is None
    assert _daemon_subcommand_suggestion("health", {"daemon-health"}) == "daemon-health"
