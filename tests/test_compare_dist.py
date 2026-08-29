"""compare_dist judges CONTENT: a container built on another OS passes, a
changed file fails."""
from __future__ import annotations

import gzip
import io
import sys
import tarfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import compare_dist  # noqa: E402

STAMP = (2020, 2, 2, 0, 0, 0)


def _write_dist(directory: Path, *, files: dict[str, bytes], create_system: int, compresslevel: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(directory / "pkg-1.0-py3-none-any.whl", "w") as archive:
        for name, blob in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=STAMP)
            info.create_system = create_system
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, blob, compresslevel=compresslevel)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, blob in sorted(files.items()):
            member = tarfile.TarInfo(f"pkg-1.0/{name}")
            member.size = len(blob)
            member.mtime = 1580601600
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(blob))
    (directory / "pkg-1.0.tar.gz").write_bytes(gzip.compress(raw.getvalue(), compresslevel=compresslevel, mtime=1580601600))


FILES = {"pkg/__init__.py": b"x = 1\n" * 200, "pkg/data.txt": b"hello\n" * 50}


def test_same_content_in_different_containers_is_identical(tmp_path: Path) -> None:
    """Windows create_system + one deflate level versus Unix + another: the
    archive bytes differ, the content does not, and that is the verdict."""
    _write_dist(tmp_path / "a", files=FILES, create_system=0, compresslevel=9)
    _write_dist(tmp_path / "b", files=FILES, create_system=3, compresslevel=1)
    a_whl = (tmp_path / "a" / "pkg-1.0-py3-none-any.whl").read_bytes()
    b_whl = (tmp_path / "b" / "pkg-1.0-py3-none-any.whl").read_bytes()
    assert a_whl != b_whl, "the fixture must produce different archive bytes"

    assert compare_dist.compare(tmp_path / "a", tmp_path / "b") == []
    assert compare_dist.main([str(tmp_path / "a"), str(tmp_path / "b")]) == 0


def test_a_changed_file_is_reported_and_fails(tmp_path: Path, capsys) -> None:  # noqa: ANN001 - pytest fixture
    changed = dict(FILES, **{"pkg/__init__.py": b"x = 2\n" * 200})
    _write_dist(tmp_path / "a", files=FILES, create_system=3, compresslevel=6)
    _write_dist(tmp_path / "b", files=changed, create_system=3, compresslevel=6)

    problems = compare_dist.compare(tmp_path / "a", tmp_path / "b")

    assert any("wheel member differs: pkg/__init__.py" in p for p in problems)
    assert any("sdist member differs: pkg/__init__.py" in p for p in problems)
    assert compare_dist.main([str(tmp_path / "a"), str(tmp_path / "b")]) == 1
    assert "CONTENT DIFFERS" in capsys.readouterr().err


def test_a_missing_member_is_reported(tmp_path: Path) -> None:
    fewer = {"pkg/__init__.py": FILES["pkg/__init__.py"]}
    _write_dist(tmp_path / "a", files=FILES, create_system=3, compresslevel=6)
    _write_dist(tmp_path / "b", files=fewer, create_system=3, compresslevel=6)

    problems = compare_dist.compare(tmp_path / "a", tmp_path / "b")

    assert any("pkg/data.txt" in p for p in problems)
