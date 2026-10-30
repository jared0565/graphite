"""Verify the BUILT DISTRIBUTION, not the source tree.

Every other gate in this repo tests the working tree. CI installs
`pip install -e .`, so the suite imports `src/graphite` directly and the thing
users would actually receive is never executed by anything. The only check that
looked at a real artifact was a manual step in `RELEASING.md`, run once per
release -- which means drift between releases is invisible by construction, and
two defects have already been found by finally looking:

* a developer path shipped inside the wheel METADATA, rendering on PyPI;
* the release runbook naming artifacts the build no longer produces;
* a developer path shipped inside the SDIST -- which carries the tests and the
  scripts, and was never scanned for content at all.

All were invisible to 2856 passing tests, because none is a property of the
source tree.

Run with a path to a directory containing exactly one wheel and one sdist:

    python scripts/verify_artifact.py dist/

Exits non-zero and prints every violation. Import-time behaviour is deliberately
NOT checked here -- that is what `--smoke` does, from outside the checkout.
"""
from __future__ import annotations

import argparse
import re
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Paths that must never appear inside a distribution. Caches and VCS metadata
# bloat it; `.aramid`, `.graphite` and `graph-out` are local state; `.env` and
# `.claude`/`.codex` can carry credentials or machine configuration.
FORBIDDEN_PATHS = re.compile(
    r"(^|/)("
    r"\.git|\.venv|__pycache__|\.pytest_cache|\.ruff_cache|\.mypy_cache"
    r"|\.aramid|\.graphite|graph-out|\.cache|\.env|\.claude|\.codex|\.vscode"
    r"|node_modules"
    r")(/|$)"
)

# The SHAPE of an absolute path that belongs to one developer's machine.
#
# Deliberately machine-INDEPENDENT. The first version of this check derived
# its needles from `ROOT` and `Path.home()`, which is right for catching a
# path leaked out of the machine doing the building -- but the gate runs on a
# CI runner, where those needles are the runner's own POSIX paths and can
# never match a path baked into the source on a developer's Windows box. A
# rule that only sees its own machine's spelling of a path is a local check,
# not a release gate; this one matches the shape instead, so it sees the path
# whoever wrote it.
#
# The trailing group is optional on purpose: the literal that actually
# shipped was a bare projects root, and the version with a required
# separator missed it.
#
# Separators are matched as runs, because source code spells a Windows path
# with escaped backslashes and a document spells the same path with one.
DEVELOPER_PATH = re.compile(
    # `C:/Users/x`, `C:\Projects`, and their escaped spellings.
    r"[A-Za-z]:[\\/]+(?:Users|Projects)(?:[\\/]+[^\s\"'`)\]}]*)?"
    # the MSYS spelling of the same thing.
    r"|(?<![A-Za-z0-9])/[a-zA-Z][\\/]+(?:Users|Projects)(?:[\\/]+[^\s\"'`)\]}]*)?"
    # `/home/someone`, `/Users/me`. A segment is required, so a bare home
    # prefix is a prefix rather than a path -- the same shape the
    # repository's own rule uses.
    r"|(?<![A-Za-z0-9])/(?:home|Users)[\\/]+[a-z][\w.-]*(?:[\\/]+[^\s\"'`)\]}]*)?",
    re.IGNORECASE,
)

# The roots this repository writes as EXAMPLES, as normalised path prefixes.
# A path under one of these is an example on purpose; a path with the same
# shape under any other root is somebody's machine. Each entry is a path the
# repository itself writes -- the README's example projects root, and the
# placeholder user profiles the fixtures use -- so the list is closed and
# short rather than a denylist of every path that ever leaked.
#
# A prefix rather than a whole path, because the examples nest: the README
# shows both a bare projects root and a project under it.
EXAMPLE_ROOTS = (
    "c:/projects",
    "c:/users/someone",
    "c:/users/x",
    "c:/users/example",
    "c:/users/<user>",
    "/users/me",
    "/home/someone",
    "/home/x",
)


def _distribution_name() -> str:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return declared["project"]["name"]


def _expected_prefix() -> str:
    # PEP 427: runs of non-alphanumerics collapse to a single underscore.
    return re.sub(r"[-_.]+", "_", _distribution_name())


def _machine_needles() -> list[str]:
    """Absolute paths that would identify the machine that built this.

    Derived rather than hardcoded, so this fails on any checkout that names
    itself -- the same reasoning as `test_documentation.py`, including the MSYS
    spelling, because a literal list only ever catches the machine it was
    written on.
    """
    root = str(ROOT)
    drive, _, rest = root.partition(":")
    needles = [root, root.replace("\\", "/"), str(ROOT.parent), str(ROOT.parent).replace("\\", "/")]
    if rest:
        needles.append(f"/{drive.lower()}{rest}".replace("\\", "/"))
    home = str(Path.home())
    needles.extend([home, home.replace("\\", "/")])
    return sorted({n for n in needles if len(n) > 3})


def _check_members(label: str, names: list[str], problems: list[str]) -> None:
    for name in names:
        if FORBIDDEN_PATHS.search(name):
            problems.append(f"{label} contains an excluded path: {name}")


def _normalise(path: str) -> str:
    collapsed = re.sub(r"[\\/]+", "/", path)
    return collapsed.lower()


def _msys_to_windows(normalised: str) -> str:
    """`/c/users/x` is the same path as `c:/users/x`."""
    parts = normalised.lstrip("/").split("/")
    if len(parts) > 1 and len(parts[0]) == 1 and parts[0].isalpha():
        return parts[0] + ":/" + "/".join(parts[1:])
    return normalised


def _is_example(path: str) -> bool:
    """Whether `path` sits under a root this repository writes as an example."""
    normalised = _normalise(path)
    candidates = {normalised, _msys_to_windows(normalised)}
    return any(
        candidate == root or candidate.startswith(root + "/")
        for candidate in candidates
        for root in EXAMPLE_ROOTS
    )


def developer_paths(text: str) -> list[str]:
    """The absolute paths in `text` that name one developer's machine.

    Machine-independent by construction: it matches the SHAPE of such a path
    rather than this machine's spelling of it. Returns the offending path
    strings in the order they appear.
    """
    found = []
    for match in DEVELOPER_PATH.finditer(text):
        path = match.group(0).rstrip(",;:!?")
        if not path or _is_example(path):
            continue
        found.append(path)
    return found


def _check_content(label: str, blobs: dict[str, bytes], problems: list[str]) -> None:
    for needle in _machine_needles():
        for name, blob in blobs.items():
            if needle.encode("utf-8") in blob:
                problems.append(f"{label}:{name} names the build machine: {needle!r}")
    for name, blob in blobs.items():
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for path in developer_paths(text):
            problems.append(f"{label}:{name} names a developer's machine: {path!r}")


def verify(dist_dir: Path) -> list[str]:
    problems: list[str] = []
    prefix = _expected_prefix()

    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1:
        problems.append(f"expected exactly 1 wheel, found {len(wheels)}: {[w.name for w in wheels]}")
    if len(sdists) != 1:
        problems.append(f"expected exactly 1 sdist, found {len(sdists)}: {[s.name for s in sdists]}")
    if problems:
        return problems

    wheel, sdist = wheels[0], sdists[0]
    for artifact in (wheel, sdist):
        if not artifact.name.startswith(f"{prefix}-"):
            problems.append(
                f"{artifact.name} does not start with {prefix!r}; the distribution "
                f"is named {_distribution_name()!r} in pyproject.toml"
            )

    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
        _check_members("wheel", wheel_names, problems)
        blobs = {}
        for name in wheel_names:
            try:
                blobs[name] = archive.read(name)
            except KeyError:  # pragma: no cover - defensive
                continue
        _check_content("wheel", blobs, problems)

        if not any(n.endswith("ts_resolver.mjs") for n in wheel_names):
            problems.append("wheel is missing graphite/ts_resolver.mjs (the TypeScript resolver)")
        # PEP 561: the package advertises inline types only if this marker
        # ships. `Typing :: Typed` in the classifiers is a claim; this is the
        # artifact that makes it true for a type checker on the consumer's side.
        if "graphite/py.typed" not in wheel_names:
            problems.append("wheel does not ship graphite/py.typed (PEP 561 marker)")
        metadata = next((n for n in wheel_names if n.endswith("METADATA")), None)
        if metadata is None:
            problems.append("wheel has no METADATA")
        else:
            text = blobs[metadata].decode("utf-8", "ignore")
            if f"Name: {_distribution_name()}" not in text:
                problems.append(f"wheel METADATA does not declare Name: {_distribution_name()}")

    with tarfile.open(sdist) as archive:
        _check_members("sdist", archive.getnames(), problems)
        # The sdist's CONTENT, not just its member names. It ships the test
        # suite and the scripts, and the whole of that is text a developer
        # path can hide in -- a comment ships exactly as a string literal
        # does. Scanning only the wheel left the larger archive unchecked,
        # and the larger archive is the one that carries the sources.
        blobs = {}
        for member in archive.getmembers():
            if not member.isfile():
                continue
            handle = archive.extractfile(member)
            if handle is None:  # pragma: no cover - defensive
                continue
            blobs[member.name] = handle.read()
        _check_content("sdist", blobs, problems)

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist_dir", type=Path, help="directory holding the built wheel and sdist")
    arguments = parser.parse_args(argv)

    if not arguments.dist_dir.is_dir():
        print(f"not a directory: {arguments.dist_dir}", file=sys.stderr)
        return 2

    problems = verify(arguments.dist_dir)
    if problems:
        print(f"ARTIFACT VERIFICATION FAILED ({len(problems)} problem(s)):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("artifact verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
