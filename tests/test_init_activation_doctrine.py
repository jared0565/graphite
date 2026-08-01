"""Doctrine must never make freshness a precondition for querying the graph.

Operator-reported: agents declined to use the graph, citing that graphite was
supervising the repo. The shipped sentence put "skip" beside "a daemon keeps
this repo fresh" and contradicted PRE_TOOL_REMINDER, which correctly says to
fall back only AFTER an insufficient answer. See #22.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from graphite import init as init_mod
from graphite.init import init_project


def _init(root: Path):
    (root / ".git").mkdir(parents=True, exist_ok=True)
    return init_project(root, platforms=["claude"], install_agent_hooks=True)


def test_no_shipped_instruction_pairs_skip_with_supervision() -> None:
    """Mechanical on purpose, so a well-meaning rewrite cannot reintroduce the
    excuse. An earlier draft of the replacement wording proposed 'a just-opened
    repo may still be building' -- a *better* excuse than the one removed."""
    text = Path(init_mod.__file__).read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in text.splitlines()
        if "skip" in line.lower() and re.search(r"daemon|supervis|fresh", line.lower())
    ]
    assert not offenders, f"instruction offers a freshness-based excuse to skip: {offenders}"


def test_doctrine_routes_agents_to_answer_grade() -> None:
    assert "answer.grade" in init_mod.GRAPHITE_DOC
    assert "graphite check ." in init_mod.GRAPHITE_DOC


def test_doc_version_is_11() -> None:
    assert init_mod.DOC_VERSION == 11


def test_doctrine_states_repository_isolation() -> None:
    """Operator rule, 2026-08-01: an agent's world is its own repository --
    source, files, AND graph. Cross-repo knowledge moves only as a
    recommendation through the interop channel, and the owning repo's agent
    acts on it. Pinned in the template because that is the only mechanism that
    reaches the other six repos' agents.
    """
    doc = init_mod.GRAPHITE_DOC
    assert "## Repository Isolation" in doc
    assert "Your repository is your world" in doc
    # The graph is explicitly in scope: reading another repo's graph.json was
    # the specific thing that prompted the rule.
    assert "graph-out/" in doc.split("## Repository Isolation")[1].split("## Canonical")[0]
    # Read-only is NOT a loophole -- `doctor`/`status` are named so nobody
    # reasons their way to "it was only a read".
    assert "read-only" in doc
    # And the pointer every agent actually sees must carry it too.
    assert "Stay inside this repository" in init_mod.SHARED_POINTER


def test_init_defaults_to_strict_hooks(tmp_path: Path) -> None:
    """A setting that has to be applied by sweeping every repo decays the moment
    a new repo appears -- and the sweep itself rebuilds repos nobody opened."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _init(repo)

    settings = (repo / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert "strict" in settings


def test_vscode_task_activates_on_folder_open(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    _init(repo)

    tasks = json.loads((repo / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    graphite_tasks = [t for t in tasks["tasks"] if "graphite" in json.dumps(t).lower()]
    assert graphite_tasks, "no graphite activation task written"
    assert any(t.get("runOptions", {}).get("runOn") == "folderOpen" for t in graphite_tasks)


def test_existing_vscode_tasks_are_preserved(tmp_path: Path) -> None:
    """#13's lesson: never destroy hand-written content."""
    repo = tmp_path / "repo"
    (repo / ".vscode").mkdir(parents=True)
    (repo / ".vscode" / "tasks.json").write_text(
        json.dumps({"version": "2.0.0", "tasks": [{"label": "my-precious-build"}]}),
        encoding="utf-8",
    )

    _init(repo)

    tasks = json.loads((repo / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    labels = [t.get("label") for t in tasks["tasks"]]
    assert "my-precious-build" in labels


def test_repeated_init_does_not_duplicate_the_activation_task(tmp_path: Path) -> None:
    """init is idempotent; running it twice must not stack tasks."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _init(repo)
    _init(repo)

    tasks = json.loads((repo / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    labels = [t.get("label") for t in tasks["tasks"]]
    assert labels.count(init_mod.VSCODE_ACTIVATION_LABEL) == 1


def test_activate_subcommand_marks_the_repo_active(tmp_path: Path) -> None:
    from graphite import activation
    from graphite.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()

    assert main(["activate", str(repo), "--agent", "editor"]) == 0

    records = activation.read_active()
    assert [r.root for r in records] == [repo.resolve()]
    assert records[0].agent == "editor"
