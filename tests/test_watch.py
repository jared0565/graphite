"""Tests for Graphite's lightweight local watcher."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite.cli import main
from graphite.config import Config
from graphite.watch import WatchChange, WatchOptions, diff_snapshots, snapshot, watch_loop


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_diff_snapshots_reports_added_changed_removed() -> None:
    previous = {"src/a.ts": "1", "src/b.ts": "1"}
    current = {"src/a.ts": "2", "src/c.ts": "1"}

    change = diff_snapshots(previous, current)

    assert change.added == ("src/c.ts",)
    assert change.changed == ("src/a.ts",)
    assert change.removed == ("src/b.ts",)
    assert change.paths == ("src/a.ts", "src/b.ts", "src/c.ts")


def test_snapshot_uses_graphite_ingest_rules(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app.ts", "export const app = 1;\n")
    _write(tmp_path / "graph-out" / "stale.ts", "export const stale = 1;\n")

    snap = snapshot(tmp_path, Config())

    assert "src/app.ts" in snap
    assert "graph-out/stale.ts" not in snap


def test_watch_loop_debounces_until_hashes_are_stable(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app.ts", "export const app = 1;\n")
    cfg = Config()
    events: list[WatchChange] = []
    sleep_calls: list[float] = []
    modified_once = False
    modified_during_debounce = False

    def fake_sleep(seconds: float) -> None:
        nonlocal modified_once, modified_during_debounce
        sleep_calls.append(seconds)
        if seconds == 0.02 and not modified_once:
            _write(tmp_path / "src" / "app.ts", "export const app = 2;\n")
            modified_once = True
        elif seconds == 0.01 and modified_once and not modified_during_debounce:
            _write(tmp_path / "src" / "app.ts", "export const app = 3;\n")
            modified_during_debounce = True

    def on_change(change: WatchChange) -> bool:
        events.append(change)
        return True

    processed = watch_loop(
        tmp_path,
        cfg,
        on_change,
        WatchOptions(interval_seconds=0.02, debounce_seconds=0.01, build_now=False, max_cycles=2),
        sleep=fake_sleep,
    )

    assert processed == 1
    assert len(events) == 1
    assert events[0].changed == ("src/app.ts",)
    assert sleep_calls.count(0.01) >= 2


def test_watch_loop_does_not_advance_snapshot_after_failed_callback(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app.ts", "export const app = 1;\n")
    cfg = Config()
    attempts = 0
    modified = False

    def fake_sleep(seconds: float) -> None:
        nonlocal modified
        if seconds == 0.01 and not modified:
            _write(tmp_path / "src" / "app.ts", "export const app = 2;\n")
            modified = True

    def on_change(_: WatchChange) -> bool:
        nonlocal attempts
        attempts += 1
        return attempts > 1

    processed = watch_loop(
        tmp_path,
        cfg,
        on_change,
        WatchOptions(interval_seconds=0.01, debounce_seconds=0, build_now=False, max_cycles=3),
        sleep=fake_sleep,
    )

    assert attempts == 2
    assert processed == 1


def test_watch_options_validate_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="interval"):
        WatchOptions(interval_seconds=0).validate()
    with pytest.raises(ValueError, match="debounce"):
        WatchOptions(debounce_seconds=-1).validate()
    with pytest.raises(ValueError, match="max cycles"):
        WatchOptions(max_cycles=0).validate()


def test_watch_cli_once_builds_graph_and_exits(tmp_path: Path, monkeypatch, capsys) -> None:
    _write(tmp_path / "src" / "app.ts", "export function app() { return 1; }\n")
    out = tmp_path / "graph-out"
    monkeypatch.chdir(tmp_path)

    result = main([
        "--output-dir",
        str(out),
        "watch",
        ".",
        "--once",
        "--interval",
        "0.01",
        "--debounce",
        "0",
    ])

    assert result == 0
    captured = capsys.readouterr().out
    assert "watch once complete" in captured
    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    assert not any(key.startswith("llm_") for key in graph["metadata"])


def test_watch_forces_canonical_config_despite_ambient_llm_settings(
    tmp_path: Path, monkeypatch
) -> None:
    _write(tmp_path / "src" / "app.ts", "export const app = 1;\n")
    observed: list[Config] = []
    monkeypatch.setenv("GRAPHITE_LLM", "cloud")
    monkeypatch.setenv("GRAPHITE_LLM_API_KEY", "must-not-be-read")
    monkeypatch.setattr(
        "graphite.cli._build_project",
        lambda _root, cfg: observed.append(cfg),
    )

    result = main(
        ["watch", str(tmp_path), "--once", "--interval", "0.01", "--debounce", "0"]
    )

    assert result == 0
    assert len(observed) == 1
    assert observed[0].llm_mode == "none"
    assert observed[0].llm_provider == "none"
    assert observed[0].llm_api_key is None
