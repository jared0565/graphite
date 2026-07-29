"""The git-hook fast path.

The headline constraint is an import-cost one, so the headline test runs in a
subprocess: pytest has already imported `graphite.cli` in-process for other
tests, so an in-process `"graphite.cli" in sys.modules` assertion would pass
even if `hook_entry` imported it directly.
"""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import graphite.hook_entry as hook_entry


def _run(code: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=str(cwd), capture_output=True, text=True,
    )


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "GRAPHITE.md").write_text("x", encoding="utf-8")
    return tmp_path


def test_hook_entry_does_not_import_the_cli(tmp_path):
    """The whole reason this module exists.

    Measured, best of 3: `import graphite.cli` 1281ms vs 158ms for
    detach+buildlock+os+pathlib, against a 79ms bare-python floor. Importing
    the CLI here would put ~1.2s on every single commit purely to spawn a
    background build.
    """
    result = _run(
        """
        import sys, json
        import graphite.hook_entry
        print(json.dumps({"cli": "graphite.cli" in sys.modules}))
        """,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["cli"] is False


def test_outside_an_onboarded_repo_is_a_silent_noop(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hook_entry, "spawn_detached", lambda cmd, cwd: calls.append(cmd))
    assert hook_entry.main([str(tmp_path)]) == 0
    assert calls == []


def test_spawns_a_detached_build_for_the_repo(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    calls = []
    monkeypatch.setattr(
        hook_entry, "spawn_detached", lambda cmd, cwd: calls.append((cmd, cwd)) or 4321
    )
    assert hook_entry.main([str(root)]) == 0
    assert len(calls) == 1


def test_spawned_build_names_the_repo_root_explicitly(tmp_path, monkeypatch):
    """Guards against graphite's CWD-relative config resolution.

    `Config.cache_dir` / `output_dir` default to RELATIVE paths and `cmd_build`
    resolves them against the process CWD, so a build whose target is implied
    by CWD writes its graph wherever it happened to be launched. Passing the
    resolved root as an explicit argument AND setting cwd=root makes this
    immune to whatever directory git invoked the hook from.
    """
    root = _repo(tmp_path)
    calls = []
    monkeypatch.setattr(
        hook_entry, "spawn_detached", lambda cmd, cwd: calls.append((cmd, cwd)) or 1
    )
    hook_entry.main([str(root)])
    cmd, cwd = calls[0]
    assert str(root.resolve()) in cmd
    assert Path(cwd).resolve() == root.resolve()


def test_child_builds_rather_than_respawning(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    calls = []
    monkeypatch.setattr(
        hook_entry, "spawn_detached", lambda cmd, cwd: calls.append(cmd) or 1
    )
    hook_entry.main([str(root)])
    assert "--detach" not in calls[0]


def test_finds_the_repo_root_from_a_subdirectory(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    nested = root / "src" / "deep"
    nested.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(
        hook_entry, "spawn_detached", lambda cmd, cwd: calls.append((cmd, cwd)) or 1
    )
    assert hook_entry.main([str(nested)]) == 0
    _, cwd = calls[0]
    assert Path(cwd).resolve() == root.resolve()


def test_does_not_hold_the_build_lock_while_spawning(tmp_path, monkeypatch):
    """The child acquires the lock itself; the parent must not be holding it.

    An earlier design had `main()` spawn from inside `with build_lock(...)`.
    The detached child starts immediately, finds the lock held by its own
    parent, and exits with "build skipped" -- so no build would ever happen.
    `cmd_build` already takes the lock, so taking it here is both redundant
    and self-defeating.
    """
    from graphite import buildlock

    root = _repo(tmp_path)
    observed = {}

    def fake_spawn(cmd, cwd):
        # While the child would be starting, the lock must be free.
        with buildlock.build_lock(root / ".cache" / "graphite") as acquired:
            observed["free"] = acquired
        return 1

    monkeypatch.setattr(hook_entry, "spawn_detached", fake_spawn)
    hook_entry.main([str(root)])
    assert observed["free"] is True
