"""Graphite — local-first, zero-LLM knowledge graph extraction."""
from __future__ import annotations

#: THE version. `pyproject.toml` reads this file rather than the other way
#: round (`[tool.hatch.version]`), because every consumer on this machine runs
#: graphite from one shared editable install: the source tree is the deployment,
#: and `importlib.metadata` only ever reports what was true at install time.
#:
#: Coarse and hand-maintained by construction. It is a release label, not
#: evidence that a given fix is present -- to answer that, survey for the marker
#: the fix introduced, or compare `graphite --version` fingerprints.
__version__ = "0.2.0"
