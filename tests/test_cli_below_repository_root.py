"""The CLI run from a folder below the repository root (#81).

The CLI took its working directory for the repository. One `cd` below the root
every read reported the graph missing, `check .` reported it stale, `build .`
was refused with a sentence about something else, and each run registered the
subfolder with the daemon and wrote a ledger into it.

Most tests here run `main()` in a repository made under `tmp_path`, with the
working directory changed into it; the rest call the rule
(`ingest.repository_root`), the hooks' sentence or the parser directly. The
activation registry is already pointed at a throwaway directory by
`conftest.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from graphite import activation, agent_hooks, ingest, usage_ledger
from graphite.cli import build_parser, main
from graphite.incident_ledger import read_incident_entries, repo_ledger_dir

CORE = "def compute_total(items):\n    return sum(items)\n"
USE = (
    "from app.core import compute_total\n\n\n"
    "def report(items):\n    return compute_total(items)\n\n\n"
    "def twice(items):\n    return report(items) * 2\n"
)
# Nine calls to names nothing defines. With this file in it, a repository's
# calls cell reads far below the line, so an answer that comes back empty is
# graded inconclusive.
BARE = (
    "def alone():\n    return 1\n\n\n"
    "def begin():\n" + "".join(f"    spectre{i}()\n" for i in range(9)) + "    return 1\n"
)
APP = {"app/__init__.py": "", "app/core.py": CORE, "app/use.py": USE}

READS = {
    "callers": ["query", "callers compute_total"],
    "stats": ["query", "stats"],
    "path": ["query", "path twice -> compute_total"],
    "reaches": ["query", "reaches twice -> compute_total"],
    "natural": ["query", "--natural", "who calls compute_total"],
    "search": ["search", "compute_total"],
    "context": ["context", "app/core.py"],
    "impact": ["impact", "app/core.py"],
    "validate": ["validate"],
}

INCONCLUSIVE = {
    "query": ["query", "callers alone"],
    "natural": ["query", "--natural", "who calls alone"],
    "context": ["context", "bare.py", "--json"],
    "impact": ["impact", "bare.py", "--json"],
}


def _git_init(directory: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(directory)], check=True, capture_output=True)


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _built(
    root: Path, files: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Path:
    """A git repository holding `files`, with a graph built at its root."""
    root.mkdir()
    _git_init(root)
    _write(root, files)
    monkeypatch.chdir(root)
    assert main(["build", "."]) == 0
    for marker in activation.active_dir().glob("*.json"):
        marker.unlink()  # the build above registered the root; each test starts from none
    capsys.readouterr()
    return root


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Path:
    """A git repository with code in `app/` and a graph built at its root."""
    return _built(tmp_path / "repo", APP, monkeypatch, capsys)


@pytest.fixture
def unbound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Path:
    """`repo` with a file whose calls bind to nothing: empty answers are inconclusive."""
    return _built(tmp_path / "unbound", {**APP, "app/bare.py": BARE}, monkeypatch, capsys)


@pytest.fixture
def other(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Path:
    """A different program: one `core.py`, at ITS root, that defines a name
    `repo` does not have. Which graph answered can be seen."""
    return _built(
        tmp_path / "other", {"core.py": "def only_in_the_other_graph():\n    return 1\n"}, monkeypatch, capsys
    )


def _keep(graph_of: Path, below: Path) -> None:
    """Put a copy of `graph_of`'s graph at `below`/kept/graph.json."""
    (below / "kept").mkdir()
    (below / "kept" / "graph.json").write_bytes((graph_of / "graph-out" / "graph.json").read_bytes())


def _run(
    directory: Path, argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    monkeypatch.chdir(directory)
    capsys.readouterr()
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _active_roots() -> list[Path]:
    return sorted(record.root.resolve() for record in activation.read_active())


def _incidents(root: Path, code: str) -> list[dict[str, Any]]:
    entries, _ = read_incident_entries(repo_ledger_dir(root))
    return [entry for entry in entries if entry["code"] == code]


# --- one rule for "the repository this directory is in" ----------------------


def test_a_folder_below_a_root_with_a_graph_belongs_to_that_root(repo: Path) -> None:
    assert ingest.repository_root(repo / "app") == repo


def test_a_folder_below_a_root_with_the_committed_marker_belongs_to_that_root(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    (root / "pkg" / "deep").mkdir(parents=True)
    _git_init(root)
    (root / "GRAPHITE.md").write_text("# set up\n", encoding="utf-8")

    assert ingest.repository_root(root / "pkg" / "deep") == root


def test_a_folder_below_a_root_graphite_is_not_set_up_in_stands_for_itself(tmp_path: Path) -> None:
    root = tmp_path / "foreign"
    (root / "pkg").mkdir(parents=True)
    _git_init(root)

    assert ingest.repository_root(root / "pkg") == root / "pkg"


def test_a_folder_in_no_repository_stands_for_itself(tmp_path: Path) -> None:
    plain = tmp_path / "plain" / "sub"
    plain.mkdir(parents=True)
    (tmp_path / "plain" / "GRAPHITE.md").write_text("# not a repository\n", encoding="utf-8")

    assert ingest.repository_root(plain) == plain


def test_the_root_itself_is_the_root(repo: Path) -> None:
    assert ingest.repository_root(repo) == repo


def test_the_hooks_use_the_same_rule(repo: Path, tmp_path: Path) -> None:
    foreign = tmp_path / "foreign"
    (foreign / "pkg").mkdir(parents=True)
    _git_init(foreign)

    for directory in (repo, repo / "app", foreign / "pkg", tmp_path):
        assert agent_hooks._repo_root(directory) == ingest.repository_root(directory)


# --- reads follow the root ----------------------------------------------------


@pytest.mark.parametrize("name", sorted(READS))
def test_a_read_below_the_root_finds_the_root_s_graph(
    name: str, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(repo / "app", READS[name], monkeypatch, capsys)

    assert code == 0, err
    assert "graph_missing" not in out + err
    assert "not found" not in out + err


@pytest.mark.parametrize("name", sorted(READS))
def test_a_read_below_the_root_says_what_it_says_at_the_root(
    name: str, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    at_root = _run(repo, READS[name], monkeypatch, capsys)
    below = _run(repo / "app", READS[name], monkeypatch, capsys)

    assert below == at_root


def test_a_read_two_folders_down_finds_the_root_s_graph(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deep = repo / "app" / "deep" / "er"
    deep.mkdir(parents=True)

    code, out, _err = _run(deep, ["query", "callers compute_total"], monkeypatch, capsys)

    assert code == 0
    assert json.loads(out)["count"] == 1


def test_a_read_below_the_root_writes_nothing_below_it(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    before = sorted(path.name for path in (repo / "app").iterdir())

    for argv in READS.values():
        _run(repo / "app", argv, monkeypatch, capsys)

    assert sorted(path.name for path in (repo / "app").iterdir()) == before


def test_a_read_below_the_root_is_counted_in_the_root_s_ledger(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    before = len(list(usage_ledger.iter_entries(repo)))

    _run(repo / "app", ["query", "callers compute_total"], monkeypatch, capsys)

    assert len(list(usage_ledger.iter_entries(repo))) == before + 1
    assert list(usage_ledger.iter_entries(repo / "app")) == []


@pytest.mark.parametrize("name", sorted(INCONCLUSIVE))
def test_an_inconclusive_answer_below_the_root_is_recorded_at_the_root(
    name: str, unbound: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(unbound / "app", INCONCLUSIVE[name], monkeypatch, capsys)

    assert code == 0, err
    assert '"inconclusive": true' in out
    assert len(_incidents(unbound, "query_inconclusive")) == 1
    assert not (unbound / "app" / ".graphite").exists()


def test_a_read_below_a_root_set_up_but_not_built_is_recorded_at_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`init` has committed `GRAPHITE.md` and nothing has built a graph yet:
    the repository is the root all the same, and that is where "there is no
    graph" is written down."""
    root = tmp_path / "fresh"
    (root / "pkg").mkdir(parents=True)
    _git_init(root)
    (root / "GRAPHITE.md").write_text("# set up\n", encoding="utf-8")

    code, _out, err = _run(root / "pkg", ["query", "callers anything"], monkeypatch, capsys)

    assert code == 1
    assert "graph_missing" in err
    assert len(_incidents(root, "graph_load_failed")) == 1
    assert not (root / "pkg" / ".graphite").exists()
    assert _active_roots() == [root.resolve()]


def test_the_savings_switch_below_the_root_is_the_root_s(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(repo, ["savings", "off"], monkeypatch, capsys)[0] == 0

    code, out, _err = _run(repo / "app", ["savings", "status", "--json"], monkeypatch, capsys)

    assert code == 0
    assert json.loads(out)["savings_display"] is False
    assert not (repo / "app" / ".graphite").exists()


# --- --graph-json: left out it is the root's, given it means what it meant ----


def test_a_graph_named_on_the_command_line_is_relative_to_where_it_is_typed(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The path the default used to be, now typed out below the root: there is
    no such file THERE, and the root's graph is not quietly used instead."""
    code, _out, err = _run(
        repo / "app", ["query", "--graph-json", "graph-out/graph.json", "callers compute_total"], monkeypatch, capsys
    )

    assert code == 1
    assert "graph_missing" in err


def test_a_named_graph_that_cannot_be_read_is_recorded_at_the_root(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Where a named graph is looked for is the working directory. Where the
    failure is written down is still the repository."""
    _run(repo / "app", ["query", "--graph-json", "graph-out/graph.json", "callers compute_total"], monkeypatch, capsys)

    assert len(_incidents(repo, "graph_load_failed")) == 1
    assert not (repo / "app" / ".graphite").exists()


def test_a_graph_named_on_the_command_line_at_the_root_is_used(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _err = _run(
        repo, ["query", "--graph-json", "graph-out/graph.json", "callers compute_total"], monkeypatch, capsys
    )

    assert code == 0
    assert json.loads(out)["count"] == 1


def test_a_graph_named_on_the_command_line_below_the_root_is_used(
    repo: Path, other: Path,monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A graph that IS below the root, named from there: the given path is
    read, and the root's own graph is read when none is given."""
    _keep(other, repo / "app")

    given = _run(
        repo / "app", ["search", "--graph-json", "kept/graph.json", "--json", "only_in_the_other_graph"], monkeypatch, capsys
    )
    default = _run(repo / "app", ["search", "--json", "only_in_the_other_graph"], monkeypatch, capsys)

    assert given[0] == 0, given[2]
    assert default[0] == 0, default[2]
    assert json.loads(given[1])["count"] >= 1
    assert json.loads(default[1])["count"] == 0


@pytest.mark.parametrize("command", ["context", "impact"])
def test_with_a_named_graph_a_file_argument_is_left_as_typed(
    command: str, repo: Path, other: Path,monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`core.py` typed in `app/` beside `--graph-json`. Without the option it
    is the repository's `app/core.py`. The graph that is named has a `core.py`
    of its own and no `app/core.py`, and it is asked for the name as typed."""
    _keep(other, repo / "app")

    named = _run(
        repo / "app", [command, "core.py", "--graph-json", "kept/graph.json", "--json"], monkeypatch, capsys
    )
    at_its_own_root = _run(other, [command, "core.py", "--json"], monkeypatch, capsys)

    assert named[0] == 0, named[2]
    assert json.loads(named[1]) == json.loads(at_its_own_root[1])


@pytest.mark.parametrize("command", ["query", "search", "context", "impact", "validate"])
def test_the_option_shows_the_default_it_always_showed(command: str) -> None:
    """`docs/reference/cli.md` is a stable surface and prints this default.
    Telling "left out" from "typed" must not cost the text."""
    parser = build_parser()
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    (option,) = [a for a in commands.choices[command]._actions if "--graph-json" in a.option_strings]

    assert str(option.default) == "graph-out/graph.json"


def test_validate_names_the_root_s_graph_when_none_is_given(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _err = _run(repo / "app", ["validate"], monkeypatch, capsys)

    assert code == 0
    assert "graph valid" in out


def test_validate_of_a_named_graph_is_relative_to_where_it_is_typed(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(repo / "app", ["validate", "--graph-json", "graph-out/graph.json"], monkeypatch, capsys)

    assert code == 1
    assert "not found" in out + err


def test_validate_of_a_named_graph_below_the_root_reads_that_graph(
    repo: Path, other: Path,monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _keep(other, repo / "app")

    named = _run(repo / "app", ["validate", "--graph-json", "kept/graph.json", "--json"], monkeypatch, capsys)
    default = _run(repo / "app", ["validate", "--json"], monkeypatch, capsys)

    assert named[0] == 0 and default[0] == 0
    assert json.loads(named[1])["node_count"] != json.loads(default[1])["node_count"]


# --- a file argument: what the shell would mean, then what the graph means ----


@pytest.mark.parametrize("command", ["context", "impact"])
def test_a_file_named_from_where_it_is_typed_is_found(
    command: str, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    below = _run(repo / "app", [command, "core.py", "--json"], monkeypatch, capsys)
    at_root = _run(repo, [command, "app/core.py", "--json"], monkeypatch, capsys)

    assert below[0] == 0
    assert json.loads(below[1]) == json.loads(at_root[1])


@pytest.mark.parametrize("command", ["context", "impact"])
def test_a_file_named_from_the_root_is_still_found_below_it(
    command: str, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    below = _run(repo / "app", [command, "app/core.py", "--json"], monkeypatch, capsys)
    at_root = _run(repo, [command, "app/core.py", "--json"], monkeypatch, capsys)

    assert below[0] == 0
    assert json.loads(below[1]) == json.loads(at_root[1])


def test_a_file_named_with_dots_from_a_sibling_folder_is_found(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "docs").mkdir()

    below = _run(repo / "docs", ["context", "../app/core.py", "--json"], monkeypatch, capsys)
    at_root = _run(repo, ["context", "app/core.py", "--json"], monkeypatch, capsys)

    assert below[0] == 0
    assert json.loads(below[1]) == json.loads(at_root[1])


@pytest.mark.parametrize("command", ["context", "impact"])
@pytest.mark.parametrize("spelling", ["./app/core.py", "app/../app/core.py", "{root}/app/core.py"])
def test_a_file_spelled_the_way_a_shell_takes_it_is_found_at_the_root_too(
    command: str, spelling: str, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One rule, wherever the command is typed: `./app/core.py` and the whole
    path used to be "missing" at the root, where a shell opens both."""
    typed = spelling.format(root=repo.resolve().as_posix())

    spelled = _run(repo, [command, typed, "--json"], monkeypatch, capsys)
    plain = _run(repo, [command, "app/core.py", "--json"], monkeypatch, capsys)

    assert spelled[0] == 0
    assert json.loads(spelled[1]) == json.loads(plain[1])


def test_where_both_readings_name_a_file_the_one_from_where_it_is_typed_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`app/core.py` typed in `app/`, in a repository that has `app/core.py`
    AND `app/app/core.py`. A shell would open the second."""
    root = _built(
        tmp_path / "both",
        {
            "app/core.py": "def outer_marker():\n    return 1\n",
            "app/app/core.py": "def inner_marker():\n    return 2\n",
        },
        monkeypatch,
        capsys,
    )

    code, out, _err = _run(root / "app", ["context", "app/core.py", "--json"], monkeypatch, capsys)

    assert code == 0
    assert "inner_marker" in out
    assert "outer_marker" not in out


def test_a_node_id_is_left_as_typed(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found = json.loads(_run(repo, ["search", "--json", "compute_total"], monkeypatch, capsys)[1])
    node_id = found["results"][0]["id"]

    below = _run(repo / "app", ["context", node_id, "--json"], monkeypatch, capsys)
    at_root = _run(repo, ["context", node_id, "--json"], monkeypatch, capsys)

    assert below[0] == 0
    assert json.loads(below[1]) == json.loads(at_root[1])


def test_a_name_that_is_also_a_folder_where_it_is_typed_is_left_as_typed(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only a FILE is read as a path. `app` typed in `docs/`, which has a
    folder of that name, is asked of the graph as `app`, as it is at the root."""
    (repo / "docs" / "app").mkdir(parents=True)

    below = _run(repo / "docs", ["context", "app", "--json"], monkeypatch, capsys)
    at_root = _run(repo, ["context", "app", "--json"], monkeypatch, capsys)

    assert below[0] == at_root[0]
    assert json.loads(below[1]) == json.loads(at_root[1])


def test_a_link_is_asked_about_by_its_own_name(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The path is worked out from the text typed. A link made after the build
    is not in the graph, and the answer says so under the link's name; it is
    not answered with the file the link points to."""
    try:
        os.symlink("core.py", repo / "app" / "link.py")
    except OSError:
        pytest.skip("this account cannot make a symbolic link")

    code, out, _err = _run(repo / "app", ["context", "link.py", "--json"], monkeypatch, capsys)

    assert code == 1
    assert json.loads(out)["missing"] == ["app/link.py"]


def test_a_file_that_is_nowhere_is_still_reported_missing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _err = _run(repo / "app", ["context", "nowhere.py", "--json"], monkeypatch, capsys)

    assert code == 1
    assert json.loads(out)["missing"] == ["nowhere.py"]


def test_a_file_outside_the_repository_is_left_as_typed(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "outside.py").write_text("x = 1\n", encoding="utf-8")

    code, out, _err = _run(repo / "app", ["context", "../../outside.py", "--json"], monkeypatch, capsys)

    assert code == 1
    assert json.loads(out)["missing"] == ["../../outside.py"]


# --- the commands that read what a read wrote ---------------------------------


def test_an_incident_recorded_from_below_the_root_is_listed_from_there(
    unbound: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Asked in the folder the read was typed in, straight after it. The
    record went to the root, so that is where the listing has to look."""
    _run(unbound / "app", ["query", "callers alone"], monkeypatch, capsys)

    code, out, _err = _run(unbound / "app", ["incidents", "list", "--json"], monkeypatch, capsys)

    assert code == 0
    assert [incident["code"] for incident in json.loads(out)["incidents"]] == ["query_inconclusive"]


def test_the_incidents_of_a_subfolder_named_from_the_root_are_the_repository_s(
    unbound: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(unbound / "app", ["query", "callers alone"], monkeypatch, capsys)

    named = _run(unbound, ["incidents", "list", "app", "--json"], monkeypatch, capsys)
    plain = _run(unbound, ["incidents", "list", "--json"], monkeypatch, capsys)

    assert named == plain
    assert len(json.loads(named[1])["incidents"]) == 1


@pytest.mark.parametrize("verb, state", [("ack", "acked"), ("resolve", "resolved")])
def test_an_incident_is_closed_from_below_the_root(
    verb: str, state: str, unbound: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(unbound / "app", ["query", "callers alone"], monkeypatch, capsys)
    (incident,) = _incidents(unbound, "query_inconclusive")

    code, out, err = _run(
        unbound / "app", ["incidents", verb, incident["fingerprint"], "-m", "seen"], monkeypatch, capsys
    )

    assert code == 0, err
    assert out.startswith(state)
    assert not (unbound / "app" / ".graphite").exists()


def test_the_incidents_of_a_folder_in_no_repository_are_that_folder_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    _run(plain, ["query", "callers anything"], monkeypatch, capsys)

    code, out, _err = _run(plain, ["incidents", "list", "--json"], monkeypatch, capsys)

    assert code == 0
    assert [incident["code"] for incident in json.loads(out)["incidents"]] == ["graph_load_failed"]


# --- the activation backstop ----------------------------------------------------


def test_a_run_below_the_root_registers_the_root_and_nothing_else(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _active_roots() == []

    _run(repo / "app", ["query", "callers compute_total"], monkeypatch, capsys)

    assert _active_roots() == [repo.resolve()]


def test_a_run_at_the_root_registers_the_root(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(repo, ["query", "callers compute_total"], monkeypatch, capsys)

    assert _active_roots() == [repo.resolve()]


def test_a_run_below_a_root_graphite_is_not_set_up_in_registers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing can be built below a checkout's root, so a marker for that
    folder would fail the daemon at every cycle for as long as it lived."""
    foreign = tmp_path / "foreign"
    (foreign / "pkg").mkdir(parents=True)
    _git_init(foreign)

    code, _out, err = _run(foreign / "pkg", ["query", "callers anything"], monkeypatch, capsys)

    assert code == 1
    assert "graph_missing" in err
    assert _active_roots() == []


def test_a_run_in_no_repository_registers_the_folder_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    code, _out, err = _run(plain, ["query", "callers anything"], monkeypatch, capsys)

    assert code == 1
    assert "graph_missing" in err
    assert _active_roots() == [plain.resolve()]


# --- build and check name `.`: refused below a root, with the root named ------


@pytest.mark.parametrize("command", ["build", "report", "scan"])
def test_a_build_below_the_root_is_refused_with_the_root_named(
    command: str, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _out, err = _run(repo / "app", [command, "."], monkeypatch, capsys)

    assert code == 1
    assert "below the root" in err
    assert f"run it from {repo}" in err
    assert "nested Git roots" not in err
    assert not (repo / "app" / ".cache").exists()
    assert not (repo / "app" / "graph-out").exists()


def test_a_build_of_a_subfolder_named_from_the_root_is_refused_the_same_way(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _out, err = _run(repo, ["build", "app"], monkeypatch, capsys)

    assert code == 1
    assert "below the root" in err
    assert f"run it from {repo}" in err
    assert "nested Git roots" not in err


def test_a_build_of_a_path_that_is_not_there_below_the_root_makes_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refused before the build lock makes the folder it was asked about."""
    code, _out, err = _run(repo / "app", ["build", "nowhere"], monkeypatch, capsys)

    assert code == 1
    assert "below the root" in err
    assert not (repo / "app" / "nowhere").exists()


def test_a_scan_of_a_path_that_is_not_there_says_so_as_before(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo / "app")
    capsys.readouterr()

    with pytest.raises(SystemExit) as stopped:
        main(["scan", "nowhere"])

    assert stopped.value.code == 1
    err = capsys.readouterr().err
    assert "path not found" in err
    assert "below the root" not in err


@pytest.mark.parametrize("where, argv", [("app", ["check", "."]), ("", ["check", "app"])])
def test_a_check_below_the_root_says_where_the_root_is(
    where: str, argv: list[str], repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(repo / where, argv, monkeypatch, capsys)

    assert code == 1
    assert out == ""
    assert "below the root" in err
    assert f"run it from {repo}" in err
    assert "missing manifest" not in err


def test_a_check_below_the_root_still_answers_in_json(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shape a caller parsed before is the shape it parses now: the same
    keys as "stale (missing manifest)" printed here, with a reason of its own
    and the root to go to."""
    code, out, err = _run(repo / "app", ["check", ".", "--json"], monkeypatch, capsys)
    status = json.loads(out)

    assert code == 1
    assert err == ""
    assert status["stale"] is True
    assert status["reason"] == "below_repository_root"
    assert Path(status["repository_root"]) == repo
    assert (status["added"], status["changed"], status["removed"]) == ([], [], [])
    assert status["resolution_health"] is None


def test_below_a_checkout_graphite_is_not_set_up_in_build_and_check_are_refused_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Where a build can run is the root of the Git repository, set up for
    graphite or not. Only the reads ask where the graph is. Here `check .`
    used to say "stale (missing manifest)", and `build .` left
    `.cache/graphite/` behind before it was refused."""
    foreign = tmp_path / "foreign"
    (foreign / "pkg").mkdir(parents=True)
    _git_init(foreign)

    built = _run(foreign / "pkg", ["build", "."], monkeypatch, capsys)
    checked = _run(foreign / "pkg", ["check", "."], monkeypatch, capsys)
    as_json = _run(foreign / "pkg", ["check", ".", "--json"], monkeypatch, capsys)

    assert built[0] == checked[0] == as_json[0] == 1
    assert f"run it from {foreign}" in built[2]
    assert f"run it from {foreign}" in checked[2]
    assert json.loads(as_json[1])["reason"] == "below_repository_root"
    assert list((foreign / "pkg").iterdir()) == []


def test_a_build_and_a_check_at_the_root_are_what_they_were(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(repo, ["check", "."], monkeypatch, capsys)[0] == 0
    assert _run(repo, ["build", "."], monkeypatch, capsys)[0] == 0
    assert json.loads(_run(repo, ["check", ".", "--json"], monkeypatch, capsys)[1])["stale"] is False


def test_a_build_in_a_folder_of_no_repository_is_what_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = tmp_path / "plain"
    _write(plain, {"one.py": "def one():\n    return 1\n"})

    assert _run(plain, ["build", "."], monkeypatch, capsys)[0] == 0
    assert (plain / "graph-out" / "graph.json").is_file()


# --- what the hooks tell a session below the root ----------------------------


def test_the_hooks_send_only_a_build_or_a_check_to_the_root(repo: Path) -> None:
    sentence = agent_hooks._where_to_run(repo, repo / "app")

    assert f"run graphite from `{repo}`" in sentence
    assert "build" in sentence
    assert "queries work from here" in sentence


def test_the_hooks_say_nothing_at_the_root(repo: Path) -> None:
    assert agent_hooks._where_to_run(repo, repo) == ""
