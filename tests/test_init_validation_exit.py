"""`init --no-build` must not report failure on success.

Validation requires a graph that `--no-build` deliberately did not build, so a
first-time onboarding returned exit 1 after writing every file correctly. That
is the exact command in the managed template and in every round asking an agent
to onboard: `python -m graphite init . --no-build --yes --strict`.

It went unnoticed because the six repos in the 2026-08-01 rollout already had
graphs, so they exited 0; only a fresh repo shows it.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from graphite.cli import _onboarding_validation, main


def _args(*, no_build: bool, no_validate: bool = False) -> SimpleNamespace:
    return SimpleNamespace(no_build=no_build, no_validate=no_validate)


def _cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(output_dir=tmp_path / "graph-out")


# --- the four combinations, stated explicitly -------------------------------


def test_no_build_with_no_graph_is_not_applicable(tmp_path: Path) -> None:
    """The headline case. `ok` must be None, not False: None means "nothing to
    judge", and the exit code already treats only an explicit False as failure."""
    result = _onboarding_validation(_args(no_build=True), _cfg(tmp_path))

    assert result["ok"] is None
    assert result["skipped"] == "no_graph_no_build"


def test_a_missing_graph_without_no_build_is_still_a_failure(tmp_path: Path) -> None:
    """The regression this fix must not cause. If a build was supposed to happen
    and produced no graph, that is a real failure and must keep failing."""
    result = _onboarding_validation(_args(no_build=False), _cfg(tmp_path))

    assert result["ok"] is False
    assert "graph not found" in result["error"]


def test_an_existing_graph_is_validated_even_with_no_build(tmp_path: Path) -> None:
    out = tmp_path / "graph-out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")

    result = _onboarding_validation(_args(no_build=True), _cfg(tmp_path))

    assert result["ok"] is not None, "a graph that exists must be judged, not skipped"


def test_no_validate_short_circuits(tmp_path: Path) -> None:
    result = _onboarding_validation(_args(no_build=True, no_validate=True), _cfg(tmp_path))

    assert result["requested"] is False
    assert result["ok"] is None


# --- end to end -------------------------------------------------------------


def test_init_no_build_on_a_fresh_repo_exits_zero(tmp_path: Path, capsys) -> None:
    """What an agent following the managed instructions actually runs."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")

    code = main(["init", str(tmp_path), "--platform", "claude", "--no-build", "--yes"])

    out = capsys.readouterr().out
    assert code == 0, out
    assert (tmp_path / "GRAPHITE.md").is_file()
    # And it must say why, rather than printing a bare "ok" that implies a graph
    # was checked when none exists.
    assert "skipped" in out


def test_init_no_build_json_does_not_claim_failure(tmp_path: Path, capsys) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")

    assert main(["init", str(tmp_path), "--platform", "claude", "--no-build", "--yes", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["validation"]["ok"] is None
    assert payload["validation"].get("error") is None


@pytest.mark.parametrize("command", ["init", "bootstrap"])
def test_both_onboarding_commands_share_the_behaviour(
    tmp_path: Path, capsys, command: str
) -> None:
    """cmd_init and cmd_bootstrap carried byte-identical validation blocks. They
    now share one, so they cannot drift apart on this again."""
    repo = tmp_path / command
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "a.py").write_text("print(1)\n", encoding="utf-8")

    argv = [command, str(repo), "--no-build", "--yes"]
    if command == "init":
        argv += ["--platform", "claude"]

    assert main(argv) == 0, capsys.readouterr().out
