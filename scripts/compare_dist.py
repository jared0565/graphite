"""Compare two builds of a distribution by CONTENT, not by archive digest.

    python scripts/compare_dist.py <dist-dir-A> <dist-dir-B>

Exit 0 only when the wheel's members (name, CRC, size, mode, timestamp) and the
sdist's decompressed tar stream are identical between the two directories.

Why this exists: archive digests of the same commit differ between operating
systems for two reasons that are not content. The zip central directory
records `create_system` (0 on Windows, 3 on Unix) for every member, and the
deflate stream depends on the platform's zlib -- measured at cd0f528, a
Windows build (CPython bundling zlib-ng 1.3.1) and an ubuntu-latest build
(zlib 1.3.2) had 111 wheel members of different COMPRESSED size and a
byte-identical decompressed sdist. So the digests a release approves are the
CI build's, and a maintainer's local build is checked against them with this
script: same content, or the release stops. Container-level differences are
printed for the record and do not fail the comparison.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import sys
import tarfile
import zipfile
from pathlib import Path


def _one(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(f"{directory}: expected exactly one {pattern}, found {[m.name for m in matches]}")
    return matches[0]


def wheel_members(path: Path) -> dict[str, tuple[int, int, tuple[int, ...], int]]:
    with zipfile.ZipFile(path) as archive:
        return {i.filename: (i.CRC, i.file_size, i.date_time, i.external_attr) for i in archive.infolist()}


def wheel_container(path: Path) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        return {
            "create_system": sorted({i.create_system for i in infos}),
            "compressed_bytes": sum(i.compress_size for i in infos),
        }


def sdist_tar_digest(path: Path) -> str:
    return hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest()


def sdist_members(path: Path) -> dict[str, tuple[int, str, int, int]]:
    with tarfile.open(path) as archive:
        out: dict[str, tuple[int, str, int, int]] = {}
        for member in archive.getmembers():
            if not member.isfile():
                continue
            blob = archive.extractfile(member)
            digest = hashlib.sha256(blob.read()).hexdigest() if blob is not None else ""
            out[member.name.split("/", 1)[-1]] = (member.size, digest, member.mode, member.mtime)
        return out


def compare(a: Path, b: Path) -> list[str]:
    """Return every CONTENT difference; an empty list means identical content."""
    problems: list[str] = []
    wa, wb = wheel_members(_one(a, "*.whl")), wheel_members(_one(b, "*.whl"))
    for name in sorted(set(wa) | set(wb)):
        if wa.get(name) != wb.get(name):
            problems.append(f"wheel member differs: {name}: {wa.get(name)} != {wb.get(name)}")
    sa, sb = _one(a, "*.tar.gz"), _one(b, "*.tar.gz")
    if sdist_tar_digest(sa) != sdist_tar_digest(sb):
        ma, mb = sdist_members(sa), sdist_members(sb)
        for name in sorted(set(ma) | set(mb)):
            if ma.get(name) != mb.get(name):
                problems.append(f"sdist member differs: {name}: {ma.get(name)} != {mb.get(name)}")
        if not problems:
            problems.append("sdist tar streams differ but every file member matches (ordering or non-file entries)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dist_a", type=Path)
    parser.add_argument("dist_b", type=Path)
    arguments = parser.parse_args(argv)
    problems = compare(arguments.dist_a, arguments.dist_b)
    ca, cb = wheel_container(_one(arguments.dist_a, "*.whl")), wheel_container(_one(arguments.dist_b, "*.whl"))
    print(f"container (informative): A create_system={ca['create_system']} compressed={ca['compressed_bytes']}; "
          f"B create_system={cb['create_system']} compressed={cb['compressed_bytes']}")
    if problems:
        print(f"CONTENT DIFFERS ({len(problems)} member(s)):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("content identical: every wheel member and the decompressed sdist match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
