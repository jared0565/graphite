"""Thin alias for mcp_server so `python -m graphite.mcp` works."""
from __future__ import annotations

from .mcp_server import main

if __name__ == "__main__":
    raise SystemExit(main())
