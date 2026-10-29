"""The certifying suite for aramid's mutation gate (`[mutation].test_command`).

aramid runs this once on the unmutated tree (the baseline) and once per
mutant, and reads only the exit code: non-zero kills the mutant. It runs
graphite's whole suite, so no mutant can survive by scoping -- but it runs the
tests named after the changed modules FIRST, with `-x`, so a kill lands in
seconds while a survivor still has to pass everything.

Why a launcher rather than a bare pytest command:

- **Robust to cwd.** `[tests].command` points at the dev venv by a path
  relative to the repo root, which is the gate's cwd. The mutation baseline
  failed in 2-7 seconds on every attempt from 2026-08-12 13:58Z to 08-31
  (43 ledger rows), the signature of a command that cannot start, and the
  consumer records neither an exit code nor stderr. The interpreter is found
  from THIS file's location instead: `<repo>/../.venvs/graphite-dev`, or, in
  a `git worktree` copy, the same path beside the worktree's main repository.
  No developer path is baked into the file.
- **Observable.** Every invocation appends one JSON line to
  `<venvs dir>/graphite-mutation-tests.log`: cwd, repo root, whether the tree
  is mutated in place, HEAD, the changed sources, the targeted test files,
  each stage's exit code and the duration. The next stand-down explains
  itself from this side.
- **Cheap kills.** A changed `src/graphite/<stem>.py` or `scripts/<stem>.py`
  targets `tests/test_<stem>.py` and `tests/test_<stem>_*.py`. Those run first with
  `-x`; if they pass, the rest of the suite runs with the targeted files
  excluded. pytest re-sorts a file named twice into directory order, which is
  why this is two invocations rather than one.

The tree is tested through the dev venv, with the tree's own `src` FIRST on
the import path, so the code the suite imports is the code that was mutated.
That pin is not a detail: MEASURED 2026-10-28, a run from a worktree imported
the CANONICAL repository's `src` (the dev venv's editable install names it),
so the worktree's mutation was never exercised and the gate certified
nothing. The launcher now confirms that the mutated module resolves under the
tree it was given, and refuses with exit 70 if it does not -- a clean run
over code it never mutated is worse than no run at all. The machine
interpreter this script starts under imports nothing from graphite. Extra
arguments are passed to both pytest stages; `--dry-run` prints the stages
without running them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_PYTEST_NO_TESTS = 5


def _git_executable(root: Path) -> Path | None:
    """git from PATH by absolute path, never from the tree or the cwd.

    The drain starts this script with the worktree root as its current
    directory, outside any agent session, and Windows looked a bare `git` up
    there before PATH (graphite channel round 304). The same rule as
    `graphite.programs.resolve_program`, inlined because this launcher imports
    nothing from graphite.
    """
    names = ("git.exe",) if os.name == "nt" else ("git",)
    # The tree's whole subtree is refused; the cwd only itself, which is what
    # Windows searches. Refusing everything beneath the cwd refused every git
    # on drive C for a run from C:\.
    try:
        tree: Path | None = root.resolve()
    except OSError:
        tree = None
    try:
        cwd: Path | None = Path.cwd().resolve()
    except OSError:
        cwd = None

    def refused(directory: Path) -> bool:
        return directory == cwd or (tree is not None and directory.is_relative_to(tree))

    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if not raw or not Path(raw).is_absolute():
            continue
        # The directory itself, as written and as resolved: a symlink inside
        # the tree must not choose which outside git runs (1d8d8cf).
        spellings = [Path(os.path.normpath(raw))]
        try:
            spellings.append(Path(raw).resolve())
        except OSError:
            pass
        if any(refused(spelling) for spelling in spellings):
            continue
        for name in names:
            candidate = Path(raw) / name
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if not resolved.is_file() or (os.name != "nt" and not os.access(resolved, os.X_OK)):
                continue
            if refused(resolved.parent):
                continue
            return resolved
    return None


def _git(root: Path, *args: str) -> str:
    git = _git_executable(root)
    if git is None:
        return ""
    try:
        # Argument list, no shell; the git on PATH -- the same git the gate and
        # the drain use, wherever this copy of the tree lives.
        result = subprocess.run(  # noqa: S603
            [str(git), *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _canonical_root(root: Path) -> Path:
    """The main repository's root, when `root` is a `git worktree` of it."""
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        path = Path(common)
        if path.name == ".git":
            return path.parent
    return root


def _dev_python(*roots: Path) -> Path | None:
    leaf = Path("Scripts", "python.exe") if os.name == "nt" else Path("bin", "python")
    for root in roots:
        candidate = root.parent / ".venvs" / "graphite-dev" / leaf
        if candidate.exists():
            return candidate
    return None


def _changed_sources(root: Path) -> list[str]:
    names = _git(root, "diff", "--name-only", "HEAD", "--", "src", "scripts")
    return [n for n in names.splitlines() if n.endswith(".py")]


def _targeted_tests(root: Path, changed: list[str]) -> list[str]:
    seen: list[str] = []
    for name in changed:
        stem = Path(name).stem
        if stem == "__init__":
            continue
        for candidate in sorted(root.glob(f"tests/test_{stem}.py")) + sorted(root.glob(f"tests/test_{stem}_*.py")):
            rel = candidate.relative_to(root).as_posix()
            if rel not in seen:
                seen.append(rel)
    return seen


def _run(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> int:
    try:
        # argv[0] is the dev venv interpreter this script resolved; no shell.
        return subprocess.run(argv, cwd=cwd, env=env).returncode  # noqa: S603
    except OSError as exc:
        print(f"mutation_tests: cannot start {argv[0]}: {exc}", file=sys.stderr)
        return 127


def _package_root(root: Path) -> Path:
    """Where the tree's package is imported from: `src/` in a src layout.

    The suite runs from the tree under test, so the tree's own `src` has to be
    on the import path -- see `_test_environment`.
    """
    candidate = root / "src"
    if (candidate / "graphite" / "__init__.py").is_file():
        return candidate
    return root


def _test_environment(root: Path) -> dict[str, str]:
    """The environment the suite runs in, with the tree under test FIRST.

    MEASURED 2026-10-28: the dev venv's editable install names the CANONICAL
    repository's `src`, so a run from a worktree imported the canonical tree
    and the worktree's mutation was never exercised. aramid's mutation
    consumer applies its mutations to a worktree, so this is the shape every
    mutation ran in.

    The tree under test goes first, and any inherited PYTHONPATH follows it,
    so an ambient one can never outrank the tree being certified.
    """
    env = dict(os.environ)
    package_root = _package_root(root)
    inherited = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(package_root) + (os.pathsep + inherited if inherited else "")
    return env


_IMPORT_TIMEOUT_ENV = "GRAPHITE_MUTATION_IMPORT_TIMEOUT_S"
_DEFAULT_IMPORT_TIMEOUT_SECONDS = 120


def _import_timeout_seconds() -> int:
    """The import probe's budget: the operator's value, or the default.

    The probe starts an interpreter and imports a module, so it needs a
    budget -- without one a hung import would hang the gate. The number is
    POLICY, not a derived quantity: no other quantity in this file bounds an
    import, and the launcher cannot measure the machine's speed in advance.
    It is a knob for that reason, and the default is pinned by a test, so a
    change to it has to be a decision rather than a drift. (An int-bound
    mutant of the old bare `120` SURVIVED the suite on 2026-10-28 -- nothing
    asserted the value, so nothing noticed the change.)
    """
    raw = os.environ.get(_IMPORT_TIMEOUT_ENV, "").strip()
    if raw.isdigit():
        try:
            value = int(raw)
        except ValueError:
            # `str.isdigit` is true for characters `int` cannot parse -- a
            # superscript two, for one -- so a digit is not yet a number. The
            # operator set a value; the gate answers with the default rather
            # than failing the run.
            return _DEFAULT_IMPORT_TIMEOUT_SECONDS
        if value > 0:
            return value
    return _DEFAULT_IMPORT_TIMEOUT_SECONDS


def _imported_module(python: Path, env: dict[str, str], module: str, cwd: Path) -> str | None:
    """Where `python` would import `module` from, or None if it cannot.

    None is not a verdict about the tree: a mutation can break the module it
    mutates, and the suite will see that as an error. It is only the case
    where the module resolves SOMEWHERE ELSE that the gate must refuse.
    """
    try:
        result = subprocess.run(  # noqa: S603
            [str(python), "-c", f"import {module}; print({module}.__file__)"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_import_timeout_seconds(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _mutated_modules(root: Path, changed: list[str]) -> list[str]:
    """The package modules the changed files are, e.g. `src/graphite/cli.py`
    -> `graphite.cli`. A changed file outside the package (`scripts/`) has no
    module: its own test loads it by path."""
    package_root = _package_root(root)
    modules: list[str] = []
    for name in changed:
        path = (root / name).resolve()
        try:
            relative = path.relative_to(package_root.resolve())
        except ValueError:
            continue
        parts = list(relative.parts)
        if not parts:
            continue
        parts[-1] = Path(parts[-1]).stem
        if parts[-1] == "__init__":
            parts.pop()
        if parts:
            modules.append(".".join(parts))
    return modules



def main(argv: list[str]) -> int:
    started = time.monotonic()
    dry_run = "--dry-run" in argv
    extra = [a for a in argv if a != "--dry-run"]
    root = Path(__file__).resolve().parents[1]
    canonical = _canonical_root(root)
    python = _dev_python(canonical, root)
    changed = _changed_sources(root)
    targeted = _targeted_tests(root, changed)
    env = _test_environment(root)
    record: dict[str, object] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cwd": os.getcwd(),
        "root": str(root),
        "canonical_root": str(canonical),
        "in_place": root == canonical,
        "head": _git(root, "rev-parse", "--short", "HEAD"),
        "python": str(python) if python else None,
        "changed": changed,
        "targeted": targeted,
        "extra": extra,
        "package_root": str(_package_root(root)),
    }
    log_path = (python.parents[2] if python else root.parent / ".venvs") / "graphite-mutation-tests.log"

    def finish(rc: int) -> int:
        record["rc"] = rc
        record["duration_s"] = round(time.monotonic() - started, 3)
        line = json.dumps(record, sort_keys=True)
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
        print(f"mutation_tests: {line}", file=sys.stderr)
        return rc

    if python is None:
        print(
            "mutation_tests: no dev venv at <repo>/../.venvs/graphite-dev "
            f"(looked beside {canonical} and {root}); see aramid.toml [tests]",
            file=sys.stderr,
        )
        return finish(78)  # EX_CONFIG

    # The gate's verdict is only about the code it MUTATED, so the code the
    # suite imports has to be that code. MEASURED 2026-10-28: it was not --
    # the dev venv's editable install names the canonical repository's `src`,
    # so a mutation applied in a worktree was never exercised and the gate
    # certified nothing. A launcher that cannot confirm the import must
    # refuse, not report a clean run over code it never mutated.
    for module in _mutated_modules(root, changed):
        resolved = _imported_module(python, env, module, root)
        if resolved is None:
            continue  # the module does not import at all; the suite will say so
        record["import_root"] = resolved
        if not Path(resolved).resolve().is_relative_to(root.resolve()):
            print(
                f"mutation_tests: refusing to certify {module}: it resolves to {resolved}, "
                f"outside the tree under test ({root}). The mutation would never be "
                "exercised, so a clean run would say nothing about it.",
                file=sys.stderr,
            )
            return finish(70)  # EX_SOFTWARE: the tree under test is not the imported one

    base = [str(python), "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *extra]
    stages: list[tuple[str, list[str]]] = []
    if targeted:
        stages.append(("targeted", base + targeted))
    stages.append(("rest", base + ["tests"] + [f"--ignore={t}" for t in targeted]))

    if dry_run:
        for name, cmd in stages:
            print(f"{name}: {' '.join(cmd)}")
        return finish(0)

    for name, cmd in stages:
        rc = _run(cmd, root, env)
        record[f"rc_{name}"] = rc
        if name == "targeted" and rc == _PYTEST_NO_TESTS:
            continue  # the named files held no tests; the rest still decides
        if rc != 0:
            return finish(rc)
    return finish(0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
