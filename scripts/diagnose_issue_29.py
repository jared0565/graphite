"""Diagnostic for issue #29 -- why probe_mcp fails under setup-python@v7.

Temporary. Delete once #29 is settled.

Deliberately makes NO production change: `probe_mcp` already exposes `_runner`
and `_builder_runner` injection seams, so the subprocess result the probe
discards on failure (`doctor_probes.py:1490` catches everything and returns a
bare `invalid_response`) can be captured from outside.

Prints the two things the issue needs and could not otherwise see:

1. The parent process's `sys.path` -- the input to
   `_raw_metadata_search_roots`, which is the suspected mechanism.
2. The MCP server subprocess's returncode, stdout and stderr -- the signal that
   actually discriminates "the probe distrusted a root" from "the server could
   not start at all". #27 showed a broken server import produces an
   indistinguishable `degraded` on the same two tests, so without this the
   cause is unknowable.

Always exits 0: this reports, it does not gate.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

MAX_STREAM = 4000


def _show(label: str, value: object) -> None:
    print(f"--- {label}: {value!r}", flush=True)


def main() -> int:
    print("=" * 72, flush=True)
    print(f"issue-29 diagnostic | python {sys.version}", flush=True)
    print(f"executable: {sys.executable}", flush=True)
    print("=" * 72, flush=True)

    print("\n### sys.path (parent process)", flush=True)
    for i, entry in enumerate(sys.path):
        print(f"  [{i:2d}] {entry!r}", flush=True)

    import graphite.doctor_probes as probes

    print("\n### _raw_metadata_search_roots()", flush=True)
    for i, root in enumerate(probes._raw_metadata_search_roots()):
        print(f"  [{i:2d}] {root!r}", flush=True)

    captured: list[tuple[str, object]] = []

    def spy(kind: str):
        real = probes.run_bounded_process

        def wrapper(*args, **kwargs):
            try:
                result = real(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - diagnostic must report, not gate
                captured.append((kind, f"RAISED {type(exc).__name__}: {exc}"))
                raise
            captured.append((kind, result))
            return result

        return wrapper

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "selected"
        target.mkdir()
        print("\n### probe_mcp(...)", flush=True)
        check = probes.probe_mcp(
            target,
            timeout_seconds=60,
            _runner=spy("server"),
            _builder_runner=spy("manifest-builder"),
        )
        _show("status", check.status)
        _show("details", dict(check.details))
        _show("summary", check.summary)

    print("\n### captured subprocess results", flush=True)
    if not captured:
        print("  (none -- probe failed before spawning anything)", flush=True)
    for kind, result in captured:
        print(f"\n  == {kind}", flush=True)
        if isinstance(result, str):
            print(f"     {result}", flush=True)
            continue
        _show("     returncode", getattr(result, "returncode", "<missing>"))
        for stream in ("stdout", "stderr"):
            raw = getattr(result, stream, b"")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            raw = str(raw)
            shown = raw[:MAX_STREAM]
            suffix = f" ...[{len(raw) - MAX_STREAM} more]" if len(raw) > MAX_STREAM else ""
            print(f"     {stream} ({len(raw)} chars): {shown!r}{suffix}", flush=True)

    print("\n=== diagnostic complete ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
