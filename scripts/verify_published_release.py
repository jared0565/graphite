"""Verify a PUBLISHED release from the index, not from this checkout.

The claim under test is not "the upload succeeded". It is **"a stranger can
`pip install graphite-code` and get the approved artifact"**, and those differ.
A check run from inside the source tree cannot tell them apart, which is why
every arm here runs from a throwaway directory in a venv that has no path back
to the repository.

Usage::

    python scripts/verify_published_release.py 0.3.0 \\
        --wheel-sha256 9744b035faf01e4598db5df6fe789cfe17f4b64ee0f9aa31e1ef8d0729582788

The expected digest comes from that version's ``EVIDENCE.md`` in the release
store. Omit it to skip the digest arm; every other arm still runs, and the skip
is printed and counted so it cannot be mistaken for a pass.

THREE TRAPS, each of which produced a false reading:

* **Never pass ``--only-binary=:all:`` to the install arm.** It applies to every
  DEPENDENCY, and ``python-louvain`` 0.16 ships an sdist and no wheel at all, so
  a perfectly healthy install reports ``No matching distribution found`` -- a
  defect in the checker reading as a defect in the package. It belongs only on
  the download whose purpose is to hash the wheel. ``tests/`` asserts this
  placement, because a comment cannot fail a build.
* **Run this BEFORE publishing too, as a negative control.** Every arm must fail
  while the version does not exist, including ``ModuleNotFoundError`` from the
  import arm. That failure is what makes "the venv is isolated from the
  checkout" a measured fact rather than an assumption -- a verifier only ever
  seen passing has not been shown capable of failing.
* **A skipped arm is not a passed arm.** The digest arm is the load-bearing one
  and it is the only arm that can be omitted, so omitting it silently would
  print ``OVERALL: PASS`` having never compared a single byte. An expected
  digest that is present but malformed is a FAIL that says so, distinct from a
  mismatch: they are different operator diagnoses.

Exit code is 0 only when every attempted arm passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import venv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DISTRIBUTION = "graphite-code"
IMPORT_PACKAGE = "graphite"

#: Arms `verify` attempts. Printed in the summary so "4 of 5 ran" is legible.
TOTAL_ARMS = 5

#: A sha256 hexdigest, in either case. `pip`'s own digests are lowercase and so
#: is `hashlib`, but PowerShell's `Get-FileHash` prints uppercase, so a correct
#: digest pasted from a Windows shell must not read as a mismatch.
_SHA256 = re.compile(r"\A[0-9a-fA-F]{64}\Z")

Runner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv is a list; no shell, no user string
        argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", check=False
    )


def _venv_python(root: Path) -> Path:
    candidate = root / "Scripts" / "python.exe"
    return candidate if candidate.exists() else root / "bin" / "python"


def _text(completed: subprocess.CompletedProcess[str]) -> str:
    return (completed.stdout or completed.stderr or "").strip()


def _first_line(completed: subprocess.CompletedProcess[str]) -> str:
    lines = _text(completed).splitlines()
    return lines[0].strip() if lines else ""


def _last_line(completed: subprocess.CompletedProcess[str]) -> str:
    lines = _text(completed).splitlines()
    return lines[-1].strip() if lines else ""


def _available_versions(listed: subprocess.CompletedProcess[str]) -> list[str] | None:
    """Exact versions from `pip index versions`, or None when unparseable.

    The output shape is::

        graphite-code (0.3.0)
        Available versions: 0.3.0, 0.2.1

    Parsed rather than substring-matched because `0.3` is a substring of `0.3.0`
    and would otherwise report a version the index does not carry. `pip index`
    is documented as unstable, so an unrecognised shape has to read as "could
    not tell" and not as "the version is absent" -- opposite diagnoses that a
    bare `in` test collapses into one.
    """
    for line in (listed.stdout or "").splitlines():
        _, marker, tail = line.partition("Available versions:")
        if marker:
            return [version.strip() for version in tail.split(",") if version.strip()]
    return None


@dataclass
class Outcome:
    """What each arm did. Skips are carried, not folded into passes."""

    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def verify(
    version: str,
    wheel_sha256: str | None,
    python: Path,
    work: Path,
    *,
    run: Runner = _run,
) -> Outcome:
    """Run every arm against `python`, an interpreter with no path to this repo.

    `run` is injected so the suite can exercise the arms -- including the ones
    that only fire on a bad release -- without a network round trip.
    """
    outcome = Outcome()

    def report(arm: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {arm}{(' -- ' + detail) if detail else ''}")
        (outcome.passed if ok else outcome.failed).append(arm)

    def skip(arm: str, detail: str) -> None:
        print(f"[SKIP] {arm} -- {detail}")
        outcome.skipped.append(arm)

    spec = f"{DISTRIBUTION}=={version}"

    # ARM 1 -- the index knows the version at all.
    listed = run([str(python), "-m", "pip", "index", "versions", DISTRIBUTION], work)
    available = _available_versions(listed)
    if available is None:
        report("index lists the distribution", False,
               f"could not parse pip index output: {_first_line(listed) or 'no output'}")
    else:
        report("index lists the distribution", version in available,
               ", ".join(available) or "index lists no versions")

    # ARM 2 -- what a stranger types. No --only-binary: see the module docstring.
    install = run([str(python), "-m", "pip", "install", "--no-cache-dir", spec], work)
    report("pip install from the index", install.returncode == 0, _last_line(install))

    # ARM 3 -- WHICH artifact answered. A pip that quietly resolved to a local
    # path or a cached wheel reads identically to a successful index install
    # unless this is asked explicitly.
    probe = (
        "import json, pathlib, " + IMPORT_PACKAGE + " as pkg;"
        "f=pathlib.Path(pkg.__file__).resolve();"
        "print(json.dumps({'file': str(f),"
        " 'version': getattr(pkg, '__version__', None),"
        " 'in_site_packages': 'site-packages' in f.parts}))"
    )
    located = run([str(python), "-P", "-c", probe], work)
    try:
        info = json.loads(located.stdout.strip() or "{}")
    except json.JSONDecodeError:
        info = {}
    report(
        "installed package is the INDEX copy",
        bool(info.get("in_site_packages")) and info.get("version") == version,
        info.get("file") or _last_line(located) or "no output",
    )

    # ARM 4 -- the bytes the index served are the bytes that were approved.
    digest_arm = "served wheel matches the approved digest"
    if wheel_sha256 is None:
        skip(digest_arm, "no --wheel-sha256 given; no byte was compared")
    elif not _SHA256.match(wheel_sha256.strip()):
        report(digest_arm, False,
               f"malformed expected digest {wheel_sha256.strip()!r}: not 64 hex characters")
    else:
        expected = wheel_sha256.strip().lower()
        downloads = work / "dl"
        downloads.mkdir(exist_ok=True)
        fetched = run(
            [str(python), "-m", "pip", "download", "--no-deps", "--no-cache-dir",
             "--only-binary=:all:", "-d", str(downloads), spec],
            work,
        )
        wheels = sorted(downloads.glob("*.whl"))
        if fetched.returncode == 0 and len(wheels) == 1:
            served = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
            report(digest_arm, served == expected, served)
        else:
            report(digest_arm, False,
                   f"download rc={fetched.returncode}, wheels={len(wheels)}")

    # ARM 5 -- the entry point actually runs from the installed package.
    cli = run([str(python), "-P", "-m", IMPORT_PACKAGE, "--version"], work)
    report("installed CLI runs", cli.returncode == 0, _first_line(cli))

    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--wheel-sha256", default=None)
    args = parser.parse_args()

    # A neutral working directory. Running from the checkout would let a relative
    # import or a stray sys.path entry answer for the installed package.
    with tempfile.TemporaryDirectory(prefix="graphite-verify-") as raw:
        work = Path(raw)
        env_root = work / "venv"
        print(f"workspace: {work}")

        venv.EnvBuilder(with_pip=True, clear=True).create(env_root)
        python = _venv_python(env_root)
        print(f"[{'PASS' if python.exists() else 'FAIL'}] venv created -- {python}")
        if not python.exists():
            return 1

        outcome = verify(args.version, args.wheel_sha256, python, work)

    print()
    print(f"arms: {len(outcome.passed)} passed, {len(outcome.failed)} failed, "
          f"{len(outcome.skipped)} skipped, of {TOTAL_ARMS}")
    if outcome.failed:
        print(f"OVERALL: FAIL ({len(outcome.failed)} arm(s): {', '.join(outcome.failed)})")
        return 1
    if outcome.skipped:
        print(f"OVERALL: PASS, but {len(outcome.skipped)} arm(s) never ran: "
              f"{', '.join(outcome.skipped)}")
        print("A skipped arm is not a passed arm.")
        return 0
    print("OVERALL: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
