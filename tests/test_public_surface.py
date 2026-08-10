"""What consumers may depend on, and what they may not.

graphite is installed editable, so its source tree IS the deployment for every
repo on the machine: a rename reaches all of them the moment a file is saved,
with no reinstall and no version bump to notice. That makes "which names are
load-bearing" a question worth answering in a test rather than in a habit.

The answer is unusual and worth stating plainly: the supported surface is the
CLI, the MCP server, and the published JSON schemas -- NOT this package's Python
API. `docs/agent-integration.md` is the consumer contract and drives everything
through `graphite <command> --json`; `tests/test_published_schemas.py` already
holds those payloads in lockstep with live output.

So the gap this file closes is not "which functions are exported" -- nobody
imports them. It is that nothing previously said so, which left an internal
refactor and a breaking change indistinguishable.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import graphite

ROOT = Path(__file__).resolve().parents[1]

# Module paths that appear inside GENERATED ARTIFACTS in consumer repositories.
# These are public in the only sense that matters: something on disk elsewhere
# names them, and that something was written once and is never revisited.
ENTRY_POINT_MODULES = (
    "graphite.__main__",   # `python -m graphite` -- the CLI itself
    "graphite.mcp",        # written into every onboarded repo's `.mcp.json`
    "graphite.hook_entry",  # written into every onboarded repo's `.githooks/`
)


def test_the_python_api_is_declared_minimal_on_purpose() -> None:
    """`__all__` is the boundary marker, not an export list.

    Widening it is a real decision -- it promises that a name will keep working
    for consumers who cannot see this repo -- so it should require editing this
    test and explaining why.
    """
    assert graphite.__all__ == ["__version__"]
    assert graphite.__version__


@pytest.mark.parametrize("module", ENTRY_POINT_MODULES)
def test_every_generated_entry_point_still_resolves(module: str) -> None:
    """Renaming one of these breaks consumers that never see the commit.

    `init` writes `python -P -m graphite.mcp` into `.mcp.json` and the hook
    trampolines write `python -P -m graphite.hook_entry`. Both are files on disk
    in other repositories. Fixing the generator afterwards does NOT repair what
    it already emitted -- that lesson cost a day when the daemon launcher was
    regenerated -- so the module path has to be treated as frozen, and a rename
    has to fail here rather than in someone else's repo.
    """
    assert importlib.util.find_spec(module) is not None, (
        f"{module} is named inside artifacts already written to consumer repos; "
        "renaming it silently breaks them"
    )


def test_the_committed_mcp_config_names_a_module_that_exists() -> None:
    """Ties the pinned list above to a real artifact rather than to itself.

    A test that only checks its own constant would keep passing after a rename
    that updated both the code and the constant while leaving every consumer's
    `.mcp.json` pointing at nothing. This reads the config as shipped.
    """
    config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    arguments = config["mcpServers"]["graphite"]["args"]

    assert "-m" in arguments, f"the MCP launch stopped using `-m`: {arguments}"
    module = arguments[arguments.index("-m") + 1]

    assert importlib.util.find_spec(module) is not None, (
        f".mcp.json launches `{module}`, which does not resolve"
    )
    assert module in ENTRY_POINT_MODULES, (
        f".mcp.json launches `{module}`, which is not in the pinned entry-point "
        "list -- add it there or the rename protection does not cover it"
    )


def test_the_mcp_launch_is_still_shadow_proof() -> None:
    """`-P` is what stops a `graphite.py` in the repo root winning the import.

    Filed here rather than with the hook tests because it is a property of the
    PUBLISHED surface: `.mcp.json` is how every non-Claude agent reaches
    graphite, so a hijack there means the agent talks to whatever the repository
    planted, over the broker's own transport.
    """
    config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    arguments = config["mcpServers"]["graphite"]["args"]

    assert "-P" in arguments, (
        "`python -m` puts the CWD on sys.path[0]; without -P a graphite.py at a "
        "repo root shadows the real package"
    )
    assert arguments.index("-P") < arguments.index("-m"), (
        "-P must precede -m to take effect"
    )
