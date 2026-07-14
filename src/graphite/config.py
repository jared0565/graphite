"""Graphite configuration defaults and environment overrides."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def default_projects_root() -> Path:
    """Base folder for daemon/init defaults.

    `GRAPHITE_PROJECTS_ROOT` overrides everything so the tool is not welded to
    this machine's layout; `F:/Projects` remains the legacy fallback when it
    exists, and the current directory is the portable last resort.
    """
    env = os.environ.get("GRAPHITE_PROJECTS_ROOT")
    if env:
        return Path(env)
    legacy = Path("F:/Projects")
    if legacy.exists():
        return legacy
    return Path(".")


@dataclass
class Config:
    """Immutable runtime configuration."""

    output_dir: Path = Path("graph-out")
    cache_dir: Path = Path(".cache/graphite")
    cache_version: str = "v5"  # bump on extraction-format changes (v5: resolve JS-extension + ".."-relative imports; v4: full-path node ids, workspace imports, phantom-edge drop)
    workers: int = 4
    max_file_size: int = 1_000_000  # bytes
    max_files: int | None = None
    include_dotfiles: bool = False
    typescript_resolver: str = "auto"  # auto | compiler | heuristic | disabled
    typescript_resolver_timeout_seconds: float = 10.0
    typescript_symbol_references: bool = True
    llm_mode: str = "none"  # none | auto | local | cloud
    llm_provider: str = "ollama"
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_timeout_seconds: float = 30.0
    llm_max_input_chars: int = 12000
    llm_max_output_tokens: int = 512
    seed: int = 42
    verbose: bool = False

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)

    def routing_settings(self, overrides: dict[str, str] | None = None):
        """Build isolated routing settings without exporting routing secrets."""
        from .routing.settings import RoutingSettings

        return RoutingSettings.from_env(overrides)

    @classmethod
    def from_env(cls, overrides: dict[str, str] | None = None) -> "Config":
        """Build config from environment variables and CLI overrides."""
        env = {k.lower(): v for k, v in os.environ.items() if k.upper().startswith("GRAPHITE_")}
        if overrides:
            env.update({k.lower(): v for k, v in overrides.items()})

        def _path(key: str, default: Path) -> Path:
            return Path(env[key]) if key in env else default

        def _int(key: str, default: int) -> int:
            return int(env[key]) if key in env and env[key].isdigit() else default

        def _bounded_int(key: str, default: int, minimum: int, maximum: int) -> int:
            return min(max(_int(key, default), minimum), maximum)

        def _opt_int(key: str, default: int | None) -> int | None:
            if key in env and env[key].isdigit():
                return int(env[key])
            return default

        def _float(key: str, default: float) -> float:
            try:
                return float(env[key]) if key in env else default
            except ValueError:
                return default

        def _bool(key: str, default: bool) -> bool:
            return env.get(key, str(default).lower()).lower() in ("1", "true", "yes", "on")

        return cls(
            output_dir=_path("graphite_output_dir", Path("graph-out")),
            cache_dir=_path("graphite_cache_dir", Path(".cache/graphite")),
            cache_version=env.get("graphite_cache_version", "v5"),
            workers=_int("graphite_workers", 4),
            max_file_size=_int("graphite_max_file_size", 1_000_000),
            max_files=_opt_int("graphite_max_files", None),
            include_dotfiles=_bool("graphite_include_dotfiles", False),
            typescript_resolver=env.get("graphite_typescript_resolver", "auto"),
            typescript_resolver_timeout_seconds=_float("graphite_typescript_resolver_timeout", 10.0),
            typescript_symbol_references=_bool("graphite_typescript_symbol_references", True),
            llm_mode=env.get("graphite_llm", "none"),
            llm_provider=env.get("graphite_llm_provider", "ollama"),
            llm_model=env.get("graphite_llm_model"),
            llm_base_url=env.get("graphite_llm_base_url"),
            llm_api_key=env.get("graphite_llm_api_key"),
            llm_timeout_seconds=_float("graphite_llm_timeout", 30.0),
            llm_max_input_chars=_int("graphite_llm_max_input_chars", 12000),
            llm_max_output_tokens=_bounded_int(
                "graphite_llm_max_output_tokens", 512, 1, 4096
            ),
            seed=_int("graphite_seed", 42),
            verbose=_bool("graphite_verbose", False),
        )
