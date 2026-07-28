"""The daemon must rebuild a supervised project when the ENGINE changes (#18).

The only build trigger for an already-built project was a file diff, so a repo
whose files were untouched was never rebuilt no matter how much the engine
moved. `graphite check` detected this correctly and reported
`stale (engine_changed)`, but nothing in the daemon ever called it.

Activation-scoped supervision (96aa3cb) bounded the harm window -- every
activation transition forces a full rebuild -- but did not close it: a repo held
continuously open across an engine upgrade never deactivates, never
re-activates, and so never re-enters the initial-build path.

Ordering note: this fix is only meaningful after #21. While the extraction cache
was keyed on `cache_version` alone, an engine-triggered rebuild would have
served cached extraction and rewritten the recorded fingerprint to the new
value, so `check` would report fresh -- automating the self-concealing failure
across every supervised repo instead of fixing it.
"""
from __future__ import annotations

import json
from pathlib import Path

from graphite import activation
from graphite.config import Config
from graphite.daemon import BuildResult, DaemonOptions, run_daemon
from graphite.freshness import graph_engine_changed

CURRENT = {"cache_version": "v11", "schema_version": "1", "version": "1.0", "fingerprint": "a" * 64}
OTHER = {"cache_version": "v11", "schema_version": "1", "version": "1.0", "fingerprint": "b" * 64}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_project(root: Path, engine: dict) -> None:
    """A project with a graph already built under `engine`."""
    _write(root / "package.json", "{}\n")
    _write(root / "src" / "index.ts", "export const a = 1;\n")
    _write(
        root / "graph-out" / ".graphite_manifest.json",
        json.dumps({"root": root.name, "file_count": 1, "engine": engine, "files": []}),
    )


def test_engine_change_is_detected_against_the_recorded_manifest(tmp_path: Path) -> None:
    _seed_project(tmp_path, OTHER)
    cfg = Config(output_dir=tmp_path / "graph-out")

    assert graph_engine_changed(cfg, CURRENT) is True


def test_matching_engine_is_not_stale(tmp_path: Path) -> None:
    """Guard against a permanent rebuild loop: a match must not trigger."""
    _seed_project(tmp_path, CURRENT)
    cfg = Config(output_dir=tmp_path / "graph-out")

    assert graph_engine_changed(cfg, CURRENT) is False


def test_missing_manifest_is_not_reported_as_an_engine_change(tmp_path: Path) -> None:
    """A project with no graph is the initial-build path's job, not this one."""
    cfg = Config(output_dir=tmp_path / "graph-out")

    assert graph_engine_changed(cfg, CURRENT) is False


def test_unreadable_manifest_is_not_reported_as_an_engine_change(tmp_path: Path) -> None:
    _write(tmp_path / "graph-out" / ".graphite_manifest.json", "{not json")
    cfg = Config(output_dir=tmp_path / "graph-out")

    assert graph_engine_changed(cfg, CURRENT) is False


def _run(tmp_path: Path, project: Path, monkeypatch) -> list[Path]:
    """One daemon cycle with build_now disabled, so ONLY a real trigger builds."""
    from graphite import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "engine_identity", lambda _v: CURRENT)

    builds: list[Path] = []

    def fake_build(root: Path, cfg: Config, timeout: float) -> BuildResult:
        builds.append(root)
        return BuildResult(success=True, returncode=0, duration_seconds=0.01, stdout="ok")

    run_daemon(
        tmp_path,
        Config(),
        DaemonOptions(
            once=True,
            debounce_seconds=0,
            max_builds_per_cycle=10,
            state_dir=tmp_path / "state",
            build_now=False,  # nothing builds unless something genuinely triggers it
        ),
        build_project=fake_build,
    )
    return builds


def test_daemon_rebuilds_a_supervised_project_whose_engine_changed(
    tmp_path: Path, monkeypatch
) -> None:
    """The wiring test, and the defect as reported: files untouched, engine moved."""
    project = tmp_path / "alpha"
    _seed_project(project, OTHER)
    activation.mark_active(project)

    builds = _run(tmp_path, project, monkeypatch)

    assert builds == [project], (
        f"engine changed but the daemon never queued a rebuild: {builds}"
    )


def test_daemon_does_not_rebuild_when_the_engine_matches(
    tmp_path: Path, monkeypatch
) -> None:
    """The other half: an engine-triggered rebuild must not fire every cycle."""
    project = tmp_path / "alpha"
    _seed_project(project, CURRENT)
    activation.mark_active(project)

    builds = _run(tmp_path, project, monkeypatch)

    assert builds == [], f"unchanged engine caused a spurious rebuild: {builds}"
