"""docs/reference/configuration.md cannot drift from what the code reads.

Three sources of truth, each derived from the source rather than from a list
kept beside the document: the `Config` dataclass fields, every environment key
`Config.from_env` looks up, every `GRAPHITE_*` name any module reads from the
environment, and the `RoutingSettings` fields (`GRAPHITE_ROUTE_<FIELD>`).
"""
from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path

from graphite.config import Config
from graphite.routing.settings import RoutingSettings

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "reference" / "configuration.md"
SRC = ROOT / "src" / "graphite"

_ENV_NAME = re.compile(r"^GRAPHITE_[A-Z][A-Z0-9_]*$")


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def _from_env_keys() -> set[str]:
    """Every `graphite_*` key string inside `Config.from_env`, upper-cased."""
    tree = ast.parse((SRC / "config.py").read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "from_env":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str) and inner.value.startswith("graphite_"):
                    keys.add(inner.value.upper())
    assert keys, "from_env read no keys -- the scan is broken, not the doc"
    return keys


def _environment_reads_outside_config() -> set[str]:
    """`GRAPHITE_*` literals that are (a) bound to an `ENV_*` constant or
    (b) the first argument of an `environ.get(...)` / `.get(...)` on an
    environment mapping, anywhere under src/graphite except config.py."""
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        if path.name == "config.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                if any(isinstance(t, ast.Name) and t.id.startswith("ENV_") for t in node.targets) and _ENV_NAME.match(node.value.value):
                    names.add(node.value.value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
                first = node.args[0]
                receiver = node.func.value
                receiver_name = receiver.attr if isinstance(receiver, ast.Attribute) else getattr(receiver, "id", "")
                if receiver_name in ("environ", "env") and isinstance(first, ast.Constant) and isinstance(first.value, str) and _ENV_NAME.match(first.value):
                    names.add(first.value)
    assert names, "no environment reads found outside config.py -- the scan is broken, not the doc"
    return names


def test_every_config_field_is_documented() -> None:
    text = _doc_text()
    for field in dataclasses.fields(Config):
        assert f"`{field.name}`" in text, f"Config.{field.name} is missing from configuration.md"


def test_every_from_env_key_is_documented() -> None:
    text = _doc_text()
    for key in sorted(_from_env_keys()):
        assert f"`{key}`" in text, f"{key} is read by Config.from_env but missing from configuration.md"


def test_every_environment_read_outside_config_is_documented() -> None:
    text = _doc_text()
    found = _environment_reads_outside_config()
    for key in sorted(found):
        assert f"`{key}`" in text, f"{key} is read from the environment but missing from configuration.md"
    # The scan must actually see the reads this test exists for.
    assert {"GRAPHITE_STATE_DIR", "GRAPHITE_DAEMON_CHILD", "GRAPHITE_PROJECTS_ROOT"} <= found


def test_every_routing_setting_is_documented() -> None:
    text = _doc_text()
    for field in dataclasses.fields(RoutingSettings):
        name = f"GRAPHITE_ROUTE_{field.name.upper()}"
        assert f"`{name}`" in text, f"{name} is missing from configuration.md"


def test_the_document_lists_no_variable_the_code_does_not_read() -> None:
    """Drift runs both ways: a variable documented but never read is a lie."""
    documented = set(re.findall(r"`(GRAPHITE_[A-Z][A-Z0-9_]*)`", _doc_text()))
    read = _from_env_keys() | _environment_reads_outside_config()
    read |= {f"GRAPHITE_ROUTE_{f.name.upper()}" for f in dataclasses.fields(RoutingSettings)}
    prefixes = {"GRAPHITE_LLM", "GRAPHITE_PROVIDER_", "GRAPHITE_ROUTE_"}
    unread = {name for name in documented if name not in read and name not in prefixes}
    assert not unread, f"documented but not read anywhere: {sorted(unread)}"
