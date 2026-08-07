"""`graphite --version` has to answer "what engine is this repo running?"

The operator-facing problem it exists for: graphite is installed editable, so
`importlib.metadata.version` is frozen at whatever `pyproject.toml` said when the
install happened and does NOT move when the source does. Every consumer
therefore reports `0.1.0` forever, and "which version is that repo on" has been
unanswerable from inside graphite -- a standing note records that a hand-kept
per-repo version list "went stale silently and misdirected sessions for days".

The fix is not the version string. It is the ENGINE FINGERPRINT, a digest over
the engine's own source files, which moves the moment the code moves. Two repos
reporting the same fingerprint are provably running identical code; a repo
reporting a different one provably is not. That is the marker survey.
"""
from __future__ import annotations

from graphite import activation
from graphite.cli import main
from graphite.config import Config
from graphite.engine_identity import EngineIdentityError, engine_identity


def test_version_reports_the_installed_version(capsys) -> None:
    assert main(["--version"]) == 0
    out = capsys.readouterr().out
    assert "graphite" in out.lower()

    from importlib.metadata import version

    assert version("graphite") in out


def test_version_carries_the_engine_fingerprint(capsys) -> None:
    """The discriminating field. Without it `--version` reports `0.1.0` from
    every repo on the machine and answers nothing."""
    assert main(["--version"]) == 0
    out = capsys.readouterr().out

    identity = engine_identity(Config().cache_version)
    assert identity["fingerprint"] in out, "no engine fingerprint in --version output"
    assert identity["cache_version"] in out


def test_version_needs_no_graph_and_no_project(tmp_path, monkeypatch, capsys) -> None:
    """It is a SURVEY tool: it runs in consumer repos that may have no graph
    built, or none of graphite's own layout. Requiring either would make it
    unusable for the one job it exists to do."""
    monkeypatch.chdir(tmp_path)

    assert main(["--version"]) == 0
    assert "graphite" in capsys.readouterr().out.lower()


def test_version_does_not_mark_the_repo_active(monkeypatch, capsys) -> None:
    """Surveying eight consumer repos must not enrol eight repos into daemon
    supervision. Activation is what tells the daemon to keep a graph fresh, so a
    read-only identity question that activates would trigger eight rebuilds --
    the survey would change the thing it measures.
    """
    calls: list[object] = []
    monkeypatch.setattr(activation, "mark_active", lambda *a, **k: calls.append(a))

    assert main(["--version"]) == 0
    capsys.readouterr()

    assert calls == [], "--version marked the repository active"


def test_version_degrades_honestly_when_the_engine_cannot_be_identified(
    monkeypatch, capsys
) -> None:
    """A diagnostic that dies is worse than one that says less.

    But it must SAY the fingerprint is missing rather than print a version line
    that looks complete -- a survey silently missing its discriminating field
    would read as "all repos agree".
    """
    import graphite.cli as cli

    def boom(*args: object, **kwargs: object) -> dict[str, str]:
        raise EngineIdentityError("engine_unreadable")

    monkeypatch.setattr(cli, "engine_identity", boom, raising=False)

    assert main(["--version"]) == 0
    out = capsys.readouterr().out
    assert "unavailable" in out.lower()
    assert "engine_unreadable" in out


def test_version_is_stable_across_two_calls(capsys) -> None:
    """A survey compares outputs across repos, so the same engine must render
    identically every time."""
    assert main(["--version"]) == 0
    first = capsys.readouterr().out
    assert main(["--version"]) == 0
    second = capsys.readouterr().out

    assert first == second
