"""`build` of a path that is not there refuses and creates nothing (#88).

The build lock makes its cache directory first, and making that directory
made the path, so `_scan` then found a folder to scan and wrote a complete,
empty graph into it. `scan`, which takes no lock, always said `path not found`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.cli import main


@pytest.mark.parametrize("command", ["build", "report", "scan"])
def test_a_missing_path_is_refused_and_left_uncreated(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nowhere"

    with pytest.raises(SystemExit) as raised:
        main([command, str(missing)])

    assert raised.value.code == 1
    assert "path not found" in capsys.readouterr().err
    assert not missing.exists()


def test_a_detached_build_of_a_missing_path_starts_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nowhere"

    with pytest.raises(SystemExit) as raised:
        main(["build", "--detach", str(missing)])

    assert raised.value.code == 1
    assert not missing.exists()
    assert "detached build started" not in capsys.readouterr().out


def test_a_path_that_is_there_still_builds(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    assert main(["build", str(tmp_path)]) == 0
    assert (tmp_path / "graph-out" / "graph.json").is_file()
