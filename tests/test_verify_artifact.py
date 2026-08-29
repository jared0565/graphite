"""The artifact gate has to be able to fail.

`scripts/verify_artifact.py` checks the built distribution rather than the source
tree, which means nothing else in this suite exercises it -- and a gate nobody
tests is a gate that can rot into a no-op without a single red run. It already
caught two real defects (a developer path inside wheel METADATA, a distribution
renamed out from under the release runbook), so it is worth keeping honest.

These build miniature archives instead of invoking the real backend: the point
is the DETECTION, and a full `python -m build` per assertion would make this the
slowest file in the suite for no extra coverage.
"""
from __future__ import annotations

import io
import re
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import verify_artifact  # noqa: E402


def _write_archives(
    directory: Path,
    *,
    wheel_members: dict[str, bytes],
    sdist_members: tuple[str, ...] = ("graphite_code-0.2.1/PKG-INFO",),
    prefix: str = "graphite_code",
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(directory / f"{prefix}-0.2.1-py3-none-any.whl", "w") as archive:
        for name, blob in wheel_members.items():
            archive.writestr(name, blob)
    with tarfile.open(directory / f"{prefix}-0.2.1.tar.gz", "w:gz") as archive:
        for name in sdist_members:
            info = tarfile.TarInfo(name)
            info.size = 0
            archive.addfile(info, io.BytesIO(b""))


def _clean_members() -> dict[str, bytes]:
    return {
        "graphite/__init__.py": b"__version__ = '0.2.1'\n",
        "graphite/ts_resolver.mjs": b"// resolver\n",
        "graphite/py.typed": b"",
        "graphite_code-0.2.1.dist-info/METADATA": b"Name: graphite-code\nVersion: 0.2.1\n",
    }


def test_a_clean_distribution_passes(tmp_path: Path) -> None:
    """Falsifiability control. Without it every assertion below is satisfied by
    a checker that rejects everything."""
    _write_archives(tmp_path, wheel_members=_clean_members())

    assert verify_artifact.verify(tmp_path) == []


def test_a_developer_path_in_the_content_is_caught(tmp_path: Path) -> None:
    """The defect that actually shipped: README is the packaging `readme`, so
    its text lands in METADATA and renders on PyPI. A green suite cannot see it,
    because it is not a property of the source tree."""
    members = _clean_members()
    members["graphite_code-0.2.1.dist-info/METADATA"] += (
        f"\nDescription: run it against {verify_artifact.ROOT.parent}\n".encode()
    )
    _write_archives(tmp_path, wheel_members=members)

    problems = verify_artifact.verify(tmp_path)

    assert any("names the build machine" in p for p in problems), problems


def test_an_excluded_path_is_caught(tmp_path: Path) -> None:
    members = _clean_members()
    members[".aramid/ledger.db"] = b"local state"
    _write_archives(tmp_path, wheel_members=members)

    problems = verify_artifact.verify(tmp_path)

    assert any("excluded path" in p for p in problems), problems


def test_a_renamed_distribution_is_caught(tmp_path: Path) -> None:
    """The rename this repo actually performed. `pyproject.toml` is the source of
    truth and the filename is derived from it, so a rename that misses one place
    fails here rather than at the release step."""
    _write_archives(tmp_path, wheel_members=_clean_members(), prefix="graphite")

    problems = verify_artifact.verify(tmp_path)

    assert any("does not start with" in p for p in problems), problems


def test_a_missing_resolver_is_caught(tmp_path: Path) -> None:
    """`ts_resolver.mjs` is package DATA, not Python, so a packaging config
    change can drop it without breaking a single import."""
    members = {k: v for k, v in _clean_members().items() if not k.endswith(".mjs")}
    _write_archives(tmp_path, wheel_members=members)

    problems = verify_artifact.verify(tmp_path)

    assert any("ts_resolver.mjs" in p for p in problems), problems


def test_a_missing_typed_marker_is_caught(tmp_path: Path) -> None:
    """`py.typed` is what makes `Typing :: Typed` true for a consumer's type
    checker; like the resolver it is data, so nothing imports it and nothing
    but this check would notice it dropped out of the wheel."""
    members = {k: v for k, v in _clean_members().items() if not k.endswith("py.typed")}
    _write_archives(tmp_path, wheel_members=members)

    problems = verify_artifact.verify(tmp_path)

    assert any("py.typed" in p for p in problems), problems


@pytest.mark.parametrize("count", [0, 2])
def test_the_wrong_number_of_artifacts_is_caught(tmp_path: Path, count: int) -> None:
    """`RELEASING.md` says to stop for extra or missing artifacts; a stale build
    directory holding a previous version is the realistic way that happens."""
    _write_archives(tmp_path, wheel_members=_clean_members())
    if count == 0:
        next(tmp_path.glob("*.whl")).unlink()
    else:
        (tmp_path / "graphite_code-0.1.0-py3-none-any.whl").write_bytes(b"stale")

    problems = verify_artifact.verify(tmp_path)

    assert any("expected exactly 1 wheel" in p for p in problems), problems


def test_the_needles_are_derived_not_hardcoded() -> None:
    """A literal list only ever catches the machine it was written on.

    This is the check the previous round got wrong in the other direction:
    `test_documentation.py` derives from `ROOT` and therefore sees
    `.../graphite` but not its PARENT, which is exactly where the shipped path
    lived. Deriving both is what closes it.
    """
    needles = verify_artifact._machine_needles()

    assert str(verify_artifact.ROOT) in needles
    assert str(verify_artifact.ROOT.parent) in needles, (
        "the parent directory is where the shipped developer path actually was"
    )
    assert all(len(n) > 3 for n in needles), "a short needle would match everything"
    # The MSYS spelling (/f/Projects/...) is a real form on this platform.
    assert any(re.match(r"^/[a-z]/", n) for n in needles) or sys.platform != "win32"
