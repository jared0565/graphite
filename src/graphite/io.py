"""Safe file-writing helpers for Graphite artifacts."""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

# `MoveFileEx` cannot replace a file that another handle holds open without
# FILE_SHARE_DELETE, and neither `open()` nor `os.open()` grants that share
# mode. So on Windows every graphite reader -- graph_io, the MCP server, a hook
# running `query` -- makes `os.replace` fail with WinError 5 for the length of
# its read. Seen three times in the daemon log; the daemon masks it by retrying
# next cycle, a hook- or agent-driven `build` fails outright. Readers hold the
# file for milliseconds: wait them out, bounded. On POSIX an EACCES from rename
# is the directory's permissions, which no wait will change, so the retry is
# gated there rather than merely harmless.
_RETRY_REPLACE_ON_ACCESS_DENIED = os.name == "nt"
_REPLACE_RETRY_SECONDS = 2.0
_REPLACE_RETRY_INITIAL_DELAY = 0.01
_REPLACE_RETRY_MAX_DELAY = 0.2
# Module-local aliases so tests can drive the loop without freezing the
# process-wide clock (#45).
_monotonic = time.monotonic
_sleep = time.sleep


def replace_file(src: Path, dst: Path) -> None:
    """`os.replace`, waiting out a reader that holds `dst` open on Windows.

    Every temp-file-then-rename writer in graphite goes through here so the
    retry lives in one place; see the module comment for why it exists.
    """
    if not _RETRY_REPLACE_ON_ACCESS_DENIED:
        os.replace(src, dst)
        return
    deadline = _monotonic() + _REPLACE_RETRY_SECONDS
    delay = _REPLACE_RETRY_INITIAL_DELAY
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if _monotonic() >= deadline:
                raise
            _sleep(delay)
            delay = min(delay * 2, _REPLACE_RETRY_MAX_DELAY)


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace a text file after fsyncing the temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        replace_file(tmp_path, path)
    except Exception:
        # The cleanup's own failure must not replace the error that matters:
        # a WinError 32 on the temp file once stood in for whatever the
        # replace raised, and the log line named only the temp file.
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: Any, *, indent: int | None = 2) -> None:
    """Atomically write JSON with deterministic UTF-8 encoding."""
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    atomic_write_text(path, text)
