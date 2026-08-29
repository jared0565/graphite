"""Graphite — local-first, zero-LLM knowledge graph extraction.

**The supported surface is the CLI, the MCP server, and the published JSON
schemas — not this package's Python API.**

That is a deliberate boundary, not an omission. `docs/agent-integration.md` is
the consumer contract and every example in it is `graphite <command> --json`;
the payload shapes are pinned in `docs/schemas/` and held in lockstep with live
output by tests. Nothing documented asks a consumer to import anything from
here, so `__all__` stays at the one name that is genuinely public.

Importing internals (`graphite.query`, `graphite.routing.*`, …) is therefore
supported by nobody: those move without notice, and an editable install means
they move the moment a file is saved.

The other half of the surface is a set of MODULE PATHS rather than objects.
`graphite.mcp` is written into every onboarded repo's `.mcp.json` and
`graphite.hook_entry` into its `.githooks/` trampolines, so renaming either
breaks consumers that will never see the change — and because those artifacts
are already on disk, fixing the generator afterwards does not repair them. They
are pinned by `tests/test_public_surface.py` for that reason.
"""
from __future__ import annotations

#: The only object this package promises. See the module docstring for why.
__all__ = ["__version__"]

#: THE version. `pyproject.toml` reads this file rather than the other way
#: round (`[tool.hatch.version]`), so there is exactly one place to set it.
#:
#: Through 0.2.1 this was doubly load-bearing: every consumer on this machine
#: ran graphite from one shared editable install, so the source tree WAS the
#: deployment and a saved file was live everywhere with no build and no
#: boundary. 0.3.0 is the first version consumers install as a built wheel, so
#: this string and the installed metadata are now set together by a release
#: rather than drifting apart between them.
#:
#: Coarse and hand-maintained by construction. It is a release label, not
#: evidence that a given fix is present -- to answer that, survey for the marker
#: the fix introduced, or compare `graphite --version` fingerprints.
__version__ = "1.0.0"
