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
- **Cheap kills.** A changed `src/graphite/<stem>.py` targets
  `tests/test_<stem>.py` and `tests/test_<stem>_*.py`. Those run first with
  `-x`; if they pass, the rest of the suite runs with the targeted files
  excluded. pytest re-sorts a file named twice into directory order, which is
  why this is two invocations rather than one.

The tree is tested through the dev venv's editable install, exactly as the
pre-push gate does; the machine interpreter this script starts under imports
nothing from graphite. Extra arguments are passed to both pytest stages;
`--dry-run` prints the stages without running them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_PYTEST_NO_TESTS = 5


def _git(root: Path, *args: str) -> str:
    try:
        # Argument list, no shell; `git` by name on purpose -- the same git the
        # gate and the drain use, wherever this copy of the tree lives.
        result = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
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
    names = _git(root, "diff", "--name-only", "HEAD", "--", "src")
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


def _run(argv: list[str], cwd: Path) -> int:
    try:
        # argv[0] is the dev venv interpreter this script resolved; no shell.
        return subprocess.run(argv, cwd=cwd).returncode  # noqa: S603
    except OSError as exc:
        print(f"mutation_tests: cannot start {argv[0]}: {exc}", file=sys.stderr)
        return 127


def main(argv: list[str]) -> int:
    started = time.monotonic()
    dry_run = "--dry-run" in argv
    extra = [a for a in argv if a != "--dry-run"]
    root = Path(__file__).resolve().parents[1]
    canonical = _canonical_root(root)
    python = _dev_python(canonical, root)
    changed = _changed_sources(root)
    targeted = _targeted_tests(root, changed)
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
        rc = _run(cmd, root)
        record[f"rc_{name}"] = rc
        if name == "targeted" and rc == _PYTEST_NO_TESTS:
            continue  # the named files held no tests; the rest still decides
        if rc != 0:
            return finish(rc)
    return finish(0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
