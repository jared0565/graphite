"""The benchmark generator is deterministic and its files actually cross-reference."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.build_benchmark import main as benchmark_main
from benchmarks.synthetic_repo import generate


def test_generate_is_deterministic_and_cross_linked(tmp_path: Path) -> None:
    a = generate(tmp_path / "a", files=50, seed=3)
    b = generate(tmp_path / "b", files=50, seed=3)

    assert a == b
    assert sum(a.values()) == 50
    names_a = sorted(p.relative_to(tmp_path / "a").as_posix() for p in (tmp_path / "a").rglob("*") if p.is_file())
    names_b = sorted(p.relative_to(tmp_path / "b").as_posix() for p in (tmp_path / "b").rglob("*") if p.is_file())
    assert names_a == names_b
    py = sorted((tmp_path / "a" / "py").glob("mod_*.py"))
    assert len(py) == a["py"] >= 10
    text = "\n".join(p.read_text(encoding="utf-8") for p in py)
    assert "from .mod_" in text and "def fn_" in text
    ts = sorted((tmp_path / "a" / "ts").glob("*.ts"))
    assert any("import { fn0 } from './mod_" in p.read_text(encoding="utf-8") for p in ts)
    assert (tmp_path / "a" / "rs" / "lib.rs").read_text(encoding="utf-8").startswith("pub mod mod_")


def test_generate_refuses_a_non_positive_count(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        generate(tmp_path, files=0)


def test_benchmark_builds_a_small_repo_and_records_metrics(tmp_path: Path) -> None:
    """End to end through the real `graphite build`: small enough to be a
    unit test, real enough that a broken generator or CLI flag shows here
    before it burns a CI benchmark minute."""
    out = tmp_path / "metrics.json"

    rc = benchmark_main(["--files", "25", "--out", str(out)])

    assert rc == 0
    metrics = json.loads(out.read_text(encoding="utf-8"))
    assert metrics["files"] == 25 and metrics["returncode"] == 0
    assert metrics["nodes"] > 25 and metrics["edges"] > 0
    assert metrics["graph_bytes"] > 0 and metrics["wall_s"] > 0
    assert set(metrics["counts"]) == {"py", "ts", "js", "go", "rs"}


def test_benchmark_budget_exceeded_is_exit_two(tmp_path: Path) -> None:
    """The catastrophe detector must be able to fire: a budget no build can
    meet turns the exit status into 2 while the metrics are still recorded."""
    out = tmp_path / "metrics.json"

    rc = benchmark_main(["--files", "10", "--out", str(out), "--wall-budget-s", "0.000001"])

    assert rc == 2
    assert json.loads(out.read_text(encoding="utf-8"))["returncode"] == 0
