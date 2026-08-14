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
store. Omit it to skip the digest arm; every other arm still runs.

TWO TRAPS, both of which produced a false reading before this script existed:

* **Never pass ``--only-binary=:all:`` to the install arm.** It applies to every
  DEPENDENCY, and ``python-louvain`` 0.16 ships an sdist and no wheel at all, so
  a perfectly healthy install reports ``No matching distribution found`` -- a
  defect in the checker reading as a defect in the package. It belongs only on
  the download whose purpose is to hash the wheel.
* **Run this BEFORE publishing too, as a negative control.** Every arm must fail
  while the version does not exist, including ``ModuleNotFoundError`` from the
  import arm. That failure is what makes "the venv is isolated from the
  checkout" a measured fact rather than an assumption -- a verifier only ever
  seen passing has not been shown capable of failing.

Exit code is 0 only when every attempted arm passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import venv
from pathlib import Path

DISTRIBUTION = "graphite-code"
IMPORT_PACKAGE = "graphite"


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv is a list; no shell, no user string
        argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", check=False
    )


def _venv_python(root: Path) -> Path:
    candidate = root / "Scripts" / "python.exe"
    return candidate if candidate.exists() else root / "bin" / "python"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--wheel-sha256", default=None)
    args = parser.parse_args()

    failures: list[str] = []

    def report(arm: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {arm}{(' -- ' + detail) if detail else ''}")
        if not ok:
            failures.append(arm)

    # A neutral working directory. Running from the checkout would let a relative
    # import or a stray sys.path entry answer for the installed package.
    with tempfile.TemporaryDirectory(prefix="graphite-verify-") as raw:
        work = Path(raw)
        env_root = work / "venv"
        print(f"workspace: {work}")

        venv.EnvBuilder(with_pip=True, clear=True).create(env_root)
        python = _venv_python(env_root)
        report("venv created", python.exists(), str(python))
        if not python.exists():
            return 1

        spec = f"{DISTRIBUTION}=={args.version}"

        # ARM 1 -- the index knows the version at all.
        listed = _run([str(python), "-m", "pip", "index", "versions", DISTRIBUTION], work)
        report("index lists the distribution", args.version in listed.stdout,
               (listed.stdout or listed.stderr).strip().splitlines()[:1] and
               (listed.stdout or listed.stderr).strip().splitlines()[0] or "")

        # ARM 2 -- what a stranger types. No --only-binary: see the module docstring.
        install = _run([str(python), "-m", "pip", "install", "--no-cache-dir", spec], work)
        report("pip install from the index", install.returncode == 0,
               (install.stdout or install.stderr).strip().splitlines()[-1:] and
               (install.stdout or install.stderr).strip().splitlines()[-1] or "")

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
        located = _run([str(python), "-P", "-c", probe], work)
        try:
            info = json.loads(located.stdout.strip() or "{}")
        except json.JSONDecodeError:
            info = {}
        report(
            "installed package is the INDEX copy",
            bool(info.get("in_site_packages")) and info.get("version") == args.version,
            info.get("file", located.stderr.strip().splitlines()[-1:] and
                     located.stderr.strip().splitlines()[-1] or "no output"),
        )

        # ARM 4 -- the bytes the index served are the bytes that were approved.
        if args.wheel_sha256:
            downloads = work / "dl"
            downloads.mkdir(exist_ok=True)
            fetched = _run(
                [str(python), "-m", "pip", "download", "--no-deps", "--no-cache-dir",
                 "--only-binary=:all:", "-d", str(downloads), spec],
                work,
            )
            wheels = sorted(downloads.glob("*.whl"))
            if fetched.returncode == 0 and len(wheels) == 1:
                served = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
                report("served wheel matches the approved digest",
                       served == args.wheel_sha256, served)
            else:
                report("served wheel matches the approved digest", False,
                       f"download rc={fetched.returncode}, wheels={len(wheels)}")

        # ARM 5 -- the entry point actually runs from the installed package.
        cli = _run([str(python), "-P", "-m", IMPORT_PACKAGE, "--version"], work)
        report("installed CLI runs", cli.returncode == 0,
               (cli.stdout or cli.stderr).strip().splitlines()[:1] and
               (cli.stdout or cli.stderr).strip().splitlines()[0] or "")

    print()
    if failures:
        print(f"OVERALL: FAIL ({len(failures)} arm(s): {', '.join(failures)})")
        return 1
    print("OVERALL: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
