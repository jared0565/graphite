"""`doctor` typed below a repository's root reports on the repository (#89).

It took the folder it was given for the repository, so in a subfolder of a
repository with a fresh graph it said "Graph artifact is missing" and offered
a build that is refused there, and its incident line read the folder's own
ledger, which nothing writes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_cli_below_repository_root import _git_init

from graphite.cli import main


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    _git_init(root)
    (root / "app" / "core.py").write_text("def compute(items):\n    return sum(items)\n", encoding="utf-8")
    assert main(["build", str(root)]) == 0
    return root


def _doctor(directory: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> dict:
    monkeypatch.chdir(directory)
    capsys.readouterr()
    main(["doctor", ".", "--json"])
    return json.loads(capsys.readouterr().out)


def _check(report: dict, code: str) -> dict:
    (found,) = [c for c in report["checks"] if c["code"] == code]
    return found


def test_the_graph_line_in_a_subfolder_is_the_roots(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    at_root = _check(_doctor(repo, monkeypatch, capsys), "graph")
    below = _check(_doctor(repo / "app", monkeypatch, capsys), "graph")

    assert at_root["status"] == "ready"
    assert below["status"] == at_root["status"]
    assert below["summary"] == at_root["summary"]


def test_the_report_names_the_repository_it_is_about(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _doctor(repo / "app", monkeypatch, capsys)

    assert report["root"] == "repo"


def test_a_path_that_is_not_a_directory_is_still_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo)
    capsys.readouterr()

    assert main(["doctor", str(repo / "app" / "core.py")]) != 0
    assert "existing directory" in capsys.readouterr().err
