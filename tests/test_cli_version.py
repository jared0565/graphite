"""`graphite --version` has to answer "what engine is this repo running?"

The operator-facing problem it exists for: graphite is installed editable, so
`importlib.metadata.version` is frozen at whatever `pyproject.toml` said when the
install happened and does NOT move when the source does. Read from there, every
consumer reports the same string forever, and "which version is that repo on"
has been unanswerable from inside graphite -- a standing note records that a
hand-kept per-repo version list "went stale silently and misdirected sessions
for days".

Two independent fixes, and it matters which does which:

* the version is read from `graphite.__version__` in the SOURCE tree, because
  under an editable install the source tree is the deployment. That makes a
  release bump reach every consumer on file save instead of on reinstall -- but
  it is still a hand-maintained label, so it says nothing about whether a
  given fix is present;
* the ENGINE FINGERPRINT is the discriminating field: a digest over the
  engine's own source files, which moves the moment the code moves. Two repos
  reporting the same fingerprint are provably running identical code. That is
  the marker survey.

The fingerprint implication runs ONE way -- see
`test_the_fingerprint_is_byte_exact_not_content_equivalent`.
"""
from __future__ import annotations

from importlib import metadata

import graphite
from graphite import activation
from graphite.cli import main
from graphite.config import Config
from graphite.engine_identity import EngineIdentityError, engine_identity


def test_version_reports_a_version(capsys) -> None:
    assert main(["--version"]) == 0
    out = capsys.readouterr().out
    assert "graphite" in out.lower()
    assert graphite.__version__ in out


def test_version_comes_from_the_source_not_the_frozen_install(monkeypatch, capsys) -> None:
    """The whole point, and the reason bumping `pyproject.toml` is theatre.

    `importlib.metadata` reads a METADATA file written once, at install time.
    Measured on this machine: the dist-info directory is literally named
    `graphite-0.1.0.dist-info` and its METADATA was last written 2026-07-24,
    while the source has moved on every day since. Reading the version from
    there means a release bump reaches nobody until all eight consumers
    reinstall -- which nothing in the workflow ever does.

    `graphite.__version__` lives in the source tree, and the source tree IS the
    deployment under an editable install, so it moves on file save.
    """
    monkeypatch.setattr(graphite, "__version__", "9.9.9-from-source")

    assert main(["--version"]) == 0
    assert "9.9.9-from-source" in capsys.readouterr().out


def test_a_stale_install_is_named_rather_than_hidden(monkeypatch, capsys) -> None:
    """Source and metadata disagreeing is a FACT about the install, not noise.

    It is the normal state of an editable install between reinstalls, but it is
    also what a shadowing `graphite/` on `sys.path` looks like -- the import
    resolved somewhere the installed distribution does not describe. Printing
    the source version and silently discarding the disagreement would make
    those two indistinguishable from a healthy install.
    """
    monkeypatch.setattr(graphite, "__version__", "9.9.9-from-source")
    monkeypatch.setattr(metadata, "version", lambda _name: "0.0.1-frozen")

    assert main(["--version"]) == 0
    out = capsys.readouterr().out

    assert "9.9.9-from-source" in out, "reported version must be the source one"
    assert "0.0.1-frozen" in out, "the stale install-time string must still be surfaced"
    assert "stale" in out.lower(), "the disagreement must be NAMED, not left to be spotted"


def test_an_agreeing_install_says_nothing_extra(monkeypatch, capsys) -> None:
    """Falsifiability guard for the test above: if the staleness note printed
    unconditionally, that test would pass while proving nothing."""
    monkeypatch.setattr(graphite, "__version__", "7.7.7")
    monkeypatch.setattr(metadata, "version", lambda _name: "7.7.7")

    assert main(["--version"]) == 0
    out = capsys.readouterr().out

    assert "7.7.7" in out
    assert "stale" not in out.lower()


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


def test_the_fingerprint_is_byte_exact_not_content_equivalent(tmp_path) -> None:
    """Characterization test: pins what the fingerprint actually promises.

    It passes against today's code by construction -- it documents behaviour
    rather than driving it -- and it exists because the docstrings around it
    previously claimed the converse, which is false.

    A digest over raw bytes cannot tell "different code" from "same code, saved
    differently". Line endings are the case that bites: `.gitattributes` here is
    `* text=auto eol=lf`, so every clone gets LF while a working tree can hold
    CRLF indefinitely. Measured on this tree, 13 of 106 engine files are CRLF,
    and a detached worktree at HEAD fingerprints differently from the tree it
    was made from -- same commit, same code.

    So: equal fingerprint => identical bytes => identical code. Unequal
    fingerprint => look closer. The survey answers "are these provably the
    same", never "are these provably different".

    If someone normalizes line endings inside `engine_identity`, this test
    fails. That is a legitimate design change -- but it changes what the
    fingerprint means, so it should fail loudly and take the docs with it.
    """
    same_code = "def run():\n    return 1\n"

    lf_root = tmp_path / "lf" / "graphite"
    crlf_root = tmp_path / "crlf" / "graphite"
    for root in (lf_root, crlf_root):
        root.mkdir(parents=True)
    (lf_root / "mod.py").write_bytes(same_code.encode("utf-8"))
    (crlf_root / "mod.py").write_bytes(same_code.replace("\n", "\r\n").encode("utf-8"))

    lf = engine_identity("v1", package_root=lf_root, version="0.0.0")["fingerprint"]
    crlf = engine_identity("v1", package_root=crlf_root, version="0.0.0")["fingerprint"]

    assert lf != crlf, (
        "identical code saved with different line endings must fingerprint "
        "differently -- the digest is over bytes, and the docs must not promise "
        "that a differing fingerprint means differing code"
    )

    twin = tmp_path / "twin" / "graphite"
    twin.mkdir(parents=True)
    (twin / "mod.py").write_bytes(same_code.encode("utf-8"))
    assert engine_identity("v1", package_root=twin, version="0.0.0")["fingerprint"] == lf, (
        "the direction that DOES hold: identical bytes fingerprint identically"
    )


def test_version_is_stable_across_two_calls(capsys) -> None:
    """A survey compares outputs across repos, so the same engine must render
    identically every time."""
    assert main(["--version"]) == 0
    first = capsys.readouterr().out
    assert main(["--version"]) == 0
    second = capsys.readouterr().out

    assert first == second
