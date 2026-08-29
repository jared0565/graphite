"""The published-release verifier has to be able to fail.

`scripts/verify_published_release.py` is the only check that answers "can a
stranger `pip install graphite-code` and get the approved artifact". It runs
against a live index from a throwaway venv, so nothing else in this suite can
exercise it -- and a gate nobody tests is a gate that can rot into a no-op
without a single red run. Its digest arm is the load-bearing one: it is the only
thing standing between "PyPI served something" and "PyPI served the bytes that
were approved".

The runner is injected rather than mocked at the subprocess boundary, so these
tests exercise the real arm logic (parsing, digest comparison, the skip
accounting) against replies a bad release would produce.

`FakeRunner` REFUSES any argv it does not model. That is deliberate and it is
the reason these tests measure the script rather than the fake: a permissive
stub would keep every assertion below green after a change that stopped talking
to pip at all.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import verify_published_release as vpr  # noqa: E402

VERSION = "1.2.3"
WHEEL_BYTES = b"PK\x03\x04 stand-in for graphite_code-1.2.3-py3-none-any.whl"
APPROVED = hashlib.sha256(WHEEL_BYTES).hexdigest()
DIGEST_ARM = "served wheel matches the approved digest"
ARMS = ("index", "install", "probe", "download", "cli", "provenance")
PROVENANCE_ARM = "index reports PEP 740 provenance for the served files"


@dataclass
class Reply:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _default(kind: str) -> Reply:
    """What a healthy published release answers with, arm by arm."""
    if kind == "index":
        return Reply(stdout=f"{vpr.DISTRIBUTION} ({VERSION})\nAvailable versions: {VERSION}\n")
    if kind == "install":
        return Reply(stdout=f"Successfully installed graphite-code-{VERSION}\n")
    if kind == "probe":
        return Reply(stdout=json.dumps({
            "file": f"/w/venv/lib/python3.14/site-packages/{vpr.IMPORT_PACKAGE}/__init__.py",
            "version": VERSION,
            "in_site_packages": True,
        }) + "\n")
    if kind == "download":
        return Reply(stdout=f"Saved dl/graphite_code-{VERSION}-py3-none-any.whl\n")
    if kind == "provenance":
        return Reply(stdout=json.dumps({
            f"graphite_code-{VERSION}-py3-none-any.whl": True,
            f"graphite_code-{VERSION}.tar.gz": True,
        }) + "\n")
    return Reply(stdout=f"graphite {VERSION}\n")


def _classify(argv: list[str]) -> str:
    """Name the arm an argv belongs to, or refuse it.

    Refusing is the whole contract. If `verify` starts sending something else,
    these tests must go red rather than quietly receive a default reply.
    """
    if "-c" in argv:
        # Two arms are `python -c`: the import probe and the provenance fetch.
        return "provenance" if any("urllib" in part for part in argv) else "probe"
    if "pip" in argv:
        for kind in ("index", "install", "download"):
            if kind in argv:
                return kind
    if "--version" in argv:
        return "cli"
    raise AssertionError(f"the fake runner was asked something it does not model: {argv}")


@dataclass
class FakeRunner:
    replies: dict[str, Reply] = field(default_factory=dict)
    wheels: tuple[bytes, ...] = (WHEEL_BYTES,)
    calls: list[tuple[str, list[str]]] = field(default_factory=list)

    def __call__(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        kind = _classify(argv)
        self.calls.append((kind, list(argv)))
        if kind == "download":
            self._drop_wheels(argv)
        reply = self.replies.get(kind) or _default(kind)
        return subprocess.CompletedProcess(argv, reply.returncode, reply.stdout, reply.stderr)

    def _drop_wheels(self, argv: list[str]) -> None:
        destination = Path(argv[argv.index("-d") + 1])
        for index, blob in enumerate(self.wheels):
            destination.mkdir(parents=True, exist_ok=True)
            (destination / f"graphite_code-{VERSION}.{index}-py3-none-any.whl").write_bytes(blob)

    @property
    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]

    def argv_for(self, arm: str) -> list[str]:
        for kind, argv in self.calls:
            if kind == arm:
                return argv
        raise AssertionError(f"{arm} was never invoked; the runner saw {self.kinds}")


def _verify(
    tmp_path: Path,
    *,
    sha: str | None = APPROVED,
    version: str = VERSION,
    replies: dict[str, Reply] | None = None,
    wheels: tuple[bytes, ...] = (WHEEL_BYTES,),
    expect_provenance: bool | None = None,
) -> tuple[vpr.Outcome, FakeRunner]:
    runner = FakeRunner(replies=replies or {}, wheels=wheels)
    outcome = vpr.verify(
        version, sha, tmp_path / "python", tmp_path, run=runner, expect_provenance=expect_provenance
    )
    return outcome, runner


# --- the fake itself, before anything is concluded from it -------------------


def test_the_fake_runner_refuses_an_argv_it_does_not_model(tmp_path: Path) -> None:
    """Falsifiability control FOR THE INSTRUMENT. Without this, a fake that
    answered everything would satisfy every test below while proving nothing."""
    with pytest.raises(AssertionError, match="does not model"):
        FakeRunner()(["python", "-m", "pip", "wat"], tmp_path)


def test_a_clean_release_passes_every_arm(tmp_path: Path) -> None:
    """Falsifiability control. Without it every assertion below is satisfied by
    a verifier that rejects everything."""
    outcome, _ = _verify(tmp_path)

    assert outcome.failed == []
    assert outcome.skipped == []
    assert len(outcome.passed) == vpr.TOTAL_ARMS


def test_every_arm_is_invoked_exactly_once(tmp_path: Path) -> None:
    """An arm that is never sent cannot fail, and a verifier whose arms silently
    stopped firing reports the same PASS as one that ran them all."""
    _, runner = _verify(tmp_path)

    assert sorted(runner.kinds) == sorted(ARMS)


# --- the trap the module docstring names first -------------------------------


def test_the_install_arm_is_not_restricted_to_binaries(tmp_path: Path) -> None:
    """`--only-binary=:all:` applies to every DEPENDENCY, and `python-louvain`
    0.16 ships no wheel, so it turns a healthy install into `No matching
    distribution found` -- a defect in the checker reading as a defect in the
    package. It already produced one false reading; a comment cannot fail a
    build, so assert it."""
    _, runner = _verify(tmp_path)

    assert not any(a.startswith("--only-binary") for a in runner.argv_for("install"))
    assert "--only-binary=:all:" in runner.argv_for("download"), (
        "the download exists to hash ONE wheel, so there it is required"
    )


# --- ARM 4, the load-bearing one ---------------------------------------------


def test_a_served_wheel_that_differs_from_the_approved_digest_fails(tmp_path: Path) -> None:
    """The arm's entire purpose: the index served bytes nobody approved."""
    outcome, _ = _verify(tmp_path, wheels=(b"substituted payload",))

    assert DIGEST_ARM in outcome.failed
    assert DIGEST_ARM not in outcome.passed


def test_an_uppercase_expected_digest_still_matches(tmp_path: Path) -> None:
    """PowerShell's `Get-FileHash` prints uppercase. A correct digest pasted from
    a Windows shell must not read as a substituted artifact -- that failure mode
    teaches the operator to distrust the arm that matters most."""
    outcome, _ = _verify(tmp_path, sha=APPROVED.upper())

    assert outcome.failed == []


def test_an_expected_digest_that_is_not_a_digest_fails_as_malformed(tmp_path: Path) -> None:
    """A truncated or garbled expectation is a different operator diagnosis from
    a substituted wheel, and must not be reported as one."""
    outcome, runner = _verify(tmp_path, sha=APPROVED[:32])

    assert DIGEST_ARM in outcome.failed
    assert "download" not in runner.kinds, "nothing should be fetched to compare against junk"


def test_a_malformed_digest_says_malformed(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _verify(tmp_path, sha="not-a-digest")

    assert "malformed expected digest" in capsys.readouterr().out


def test_omitting_the_digest_is_a_visible_skip_not_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Omitting `--wheel-sha256` is legal and documented. Reporting it as a pass
    would not be: the run would print OVERALL: PASS having compared no bytes."""
    outcome, runner = _verify(tmp_path, sha=None)

    assert outcome.skipped == [DIGEST_ARM]
    assert DIGEST_ARM not in outcome.passed
    assert len(outcome.passed) == vpr.TOTAL_ARMS - 1
    assert "download" not in runner.kinds
    assert "[SKIP]" in capsys.readouterr().out


@pytest.mark.parametrize("wheels", [(), (WHEEL_BYTES, WHEEL_BYTES)])
def test_the_wrong_number_of_downloaded_wheels_fails(
    tmp_path: Path, wheels: tuple[bytes, ...]
) -> None:
    """With no wheel there is nothing to hash; with two, `wheels[0]` would hash
    whichever sorted first -- both must fail rather than pick."""
    outcome, _ = _verify(tmp_path, wheels=wheels)

    assert DIGEST_ARM in outcome.failed


def test_a_download_that_errors_fails(tmp_path: Path) -> None:
    outcome, _ = _verify(
        tmp_path,
        replies={"download": Reply(returncode=1, stderr="ERROR: no matching distribution")},
        wheels=(),
    )

    assert DIGEST_ARM in outcome.failed


# --- ARM 1, the index ---------------------------------------------------------


def test_a_version_the_index_does_not_list_fails(tmp_path: Path) -> None:
    outcome, _ = _verify(
        tmp_path,
        replies={"index": Reply(stdout="graphite-code (0.2.1)\nAvailable versions: 0.2.1\n")},
    )

    assert "index lists the distribution" in outcome.failed


def test_a_prefix_of_a_listed_version_is_not_a_match(tmp_path: Path) -> None:
    """`1.2` is a substring of `1.2.3`, so a bare `in` test reports a version the
    index does not carry."""
    outcome, _ = _verify(tmp_path, version="1.2")

    assert "index lists the distribution" in outcome.failed


def test_unparseable_index_output_is_not_read_as_an_absent_version(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """`pip index` is an unstable subcommand. "I could not tell" and "it is not
    published" are opposite diagnoses and must not print the same sentence."""
    outcome, _ = _verify(
        tmp_path, replies={"index": Reply(returncode=1, stderr="ERROR: unknown command\n")}
    )

    assert "index lists the distribution" in outcome.failed
    assert "could not parse" in capsys.readouterr().out


# --- ARMS 2, 3 and 5 ----------------------------------------------------------


def test_a_failed_install_fails(tmp_path: Path) -> None:
    outcome, _ = _verify(
        tmp_path,
        replies={"install": Reply(returncode=1, stderr="ERROR: No matching distribution found")},
    )

    assert "pip install from the index" in outcome.failed


def test_a_package_answering_from_outside_site_packages_fails(tmp_path: Path) -> None:
    """The arm exists because a pip that resolved to a local checkout reads
    exactly like a successful index install."""
    outcome, _ = _verify(tmp_path, replies={"probe": Reply(stdout=json.dumps({
        "file": "F:/Projects/graphite/src/graphite/__init__.py",
        "version": VERSION,
        "in_site_packages": False,
    }))})

    assert "installed package is the INDEX copy" in outcome.failed


def test_an_installed_package_reporting_another_version_fails(tmp_path: Path) -> None:
    outcome, _ = _verify(tmp_path, replies={"probe": Reply(stdout=json.dumps({
        "file": "/w/venv/lib/python3.14/site-packages/graphite/__init__.py",
        "version": "9.9.9",
        "in_site_packages": True,
    }))})

    assert "installed package is the INDEX copy" in outcome.failed


def test_a_probe_that_could_not_import_fails(tmp_path: Path) -> None:
    """The negative-control shape: before publication this arm raises
    ModuleNotFoundError and prints nothing on stdout."""
    outcome, _ = _verify(tmp_path, replies={"probe": Reply(
        returncode=1, stderr="ModuleNotFoundError: No module named 'graphite'\n"
    )})

    assert "installed package is the INDEX copy" in outcome.failed


def test_a_cli_that_does_not_run_fails(tmp_path: Path) -> None:
    outcome, _ = _verify(tmp_path, replies={"cli": Reply(returncode=1, stderr="Traceback")})

    assert "installed CLI runs" in outcome.failed


# --- main(): one failure list, and a skip that survives to the exit code ------


def _run_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: vpr.Outcome, *, venv_ok: bool = True
) -> int:
    """Drive `main` with the venv and the arms stubbed out.

    Splitting the reporting between `main` and `verify` is exactly how a script
    comes to print PASS while an arm failed, so the seam is asserted directly.
    """
    class _Builder:
        def __init__(self, **_: object) -> None:
            pass

        def create(self, _root: object) -> None:
            pass

    interpreter = tmp_path / ("python" if venv_ok else "absent")
    if venv_ok:
        interpreter.write_text("", encoding="utf-8")

    monkeypatch.setattr(vpr.venv, "EnvBuilder", _Builder)
    monkeypatch.setattr(vpr, "_venv_python", lambda _root: interpreter)
    monkeypatch.setattr(vpr, "verify", lambda *_a, **_k: outcome)
    monkeypatch.setattr(sys, "argv", ["verify_published_release.py", VERSION])
    return vpr.main()


def test_main_passes_when_every_arm_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outcome = vpr.Outcome(passed=[f"arm {n}" for n in range(vpr.TOTAL_ARMS)])

    assert _run_main(monkeypatch, tmp_path, outcome) == 0


def test_main_fails_when_verify_reports_a_failed_arm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outcome = vpr.Outcome(passed=["arm 1"], failed=[DIGEST_ARM])

    assert _run_main(monkeypatch, tmp_path, outcome) == 1


def test_main_announces_an_arm_that_never_ran(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Exit 0 is the documented contract for an omitted digest. Saying nothing
    about it is not."""
    outcome = vpr.Outcome(passed=[f"arm {n}" for n in range(4)], skipped=[DIGEST_ARM])

    assert _run_main(monkeypatch, tmp_path, outcome) == 0
    printed = capsys.readouterr().out
    assert "never ran" in printed
    assert "A skipped arm is not a passed arm." in printed


def test_main_fails_when_the_venv_has_no_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outcome = vpr.Outcome(passed=[f"arm {n}" for n in range(vpr.TOTAL_ARMS)])

    assert _run_main(monkeypatch, tmp_path, outcome, venv_ok=False) == 1


# --- the one platform-branching function, tested on every platform -----------


def test_venv_python_prefers_the_windows_layout(tmp_path: Path) -> None:
    """Built from directories rather than `os.name`, so this runs everywhere. A
    test that skips on the other platform proves nothing there."""
    (tmp_path / "Scripts").mkdir()
    (tmp_path / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").write_text("", encoding="utf-8")

    assert vpr._venv_python(tmp_path) == tmp_path / "Scripts" / "python.exe"


def test_venv_python_falls_back_to_the_posix_layout(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").write_text("", encoding="utf-8")

    assert vpr._venv_python(tmp_path) == tmp_path / "bin" / "python"


# --- ARM 6: provenance ------------------------------------------------------


def test_a_served_file_without_provenance_fails(tmp_path: Path) -> None:
    """From 1.0.0 the publish workflow attests every file it uploads; a file the
    index serves without provenance was not built by that workflow."""
    replies = {"provenance": Reply(stdout=json.dumps({
        f"graphite_code-{VERSION}-py3-none-any.whl": True,
        f"graphite_code-{VERSION}.tar.gz": False,
    }))}

    outcome, _ = _verify(tmp_path, replies=replies)

    assert PROVENANCE_ARM in outcome.failed


def test_provenance_is_a_visible_skip_below_one_point_zero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """0.x artifacts were built on a maintainer's machine and honestly carry
    no attestation; the arm says so instead of failing or passing."""
    outcome, runner = _verify(tmp_path, version="0.9.9")

    assert PROVENANCE_ARM in outcome.skipped
    assert "provenance" not in runner.kinds
    assert "not expected for 0.9.9" in capsys.readouterr().out


def test_provenance_can_be_required_explicitly_below_one_point_zero(tmp_path: Path) -> None:
    outcome, runner = _verify(tmp_path, version="0.9.9", expect_provenance=True)

    assert "provenance" in runner.kinds
    assert PROVENANCE_ARM in outcome.passed


def test_the_provenance_arm_asks_the_simple_json_index_not_the_legacy_api(tmp_path: Path) -> None:
    """The legacy `/pypi/<project>/<version>/json` endpoint has no provenance
    key at all -- measured on 1.0.0 right after publication, where the
    Integrity API already served both attestation bundles. Only the PEP 691
    Simple JSON index (or the Integrity API) can answer this arm."""
    _, runner = _verify(tmp_path)

    script = " ".join(runner.argv_for("provenance"))
    assert "application/vnd.pypi.simple.v1+json" in script
    assert f"https://pypi.org/simple/{vpr.DISTRIBUTION}/" in script
    assert "/pypi/" not in script


def test_an_index_that_cannot_be_read_fails_the_provenance_arm(tmp_path: Path) -> None:
    """Unable to verify is not verified."""
    replies = {"provenance": Reply(returncode=1, stderr="urllib.error.HTTPError: HTTP Error 404: Not Found")}

    outcome, _ = _verify(tmp_path, replies=replies)

    assert PROVENANCE_ARM in outcome.failed
