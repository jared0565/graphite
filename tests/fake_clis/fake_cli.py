"""Deterministic subprocess fixture for Graphite CLI-boundary tests."""
from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    mode = sys.argv[1]
    if mode == "echo":
        payload = sys.stdin.buffer.read()
        sys.stdout.buffer.write(payload)
        return 0
    if mode == "environment":
        sys.stdout.write(json.dumps(dict(os.environ), sort_keys=True))
        return 0
    if mode == "sleep":
        time.sleep(30)
        return 0
    if mode == "overflow":
        sys.stdout.buffer.write(b"x" * (128 * 1024))
        return 0
    if mode == "invalid-utf8":
        sys.stdout.buffer.write(b"\xff")
        return 0
    if mode == "nonzero":
        sys.stderr.write("PRIVATE provider diagnostic")
        return 7
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
