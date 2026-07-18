"""Verified normalized effort mappings for exact Ollama model identities."""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Final, Mapping

from .contracts import Effort


class EffortMappingError(ValueError):
    """A fixed unsupported model or effort failure."""


# No model-specific thinking level is enabled until its exact payload has been
# evaluated. Omitting `think` preserves the provider's documented default.
EFFORT_PAYLOADS: Final[Mapping[str, Mapping[Effort, Mapping[str, Any]]]] = MappingProxyType(
    {
        "kimi-k2.7-code:cloud": MappingProxyType(
            {Effort.DEFAULT: MappingProxyType({})}
        ),
        "minimax-m2.7:cloud": MappingProxyType(
            {Effort.DEFAULT: MappingProxyType({})}
        ),
        "nemotron-3-super:cloud": MappingProxyType(
            {Effort.DEFAULT: MappingProxyType({})}
        ),
        "minimax-m3:cloud": MappingProxyType(
            {Effort.DEFAULT: MappingProxyType({})}
        ),
    }
)


def effort_payload(model_id: str, effort: Effort | str) -> dict[str, Any]:
    """Return a copy of the tested request fragment for an exact model/effort."""
    try:
        normalized = Effort(effort)
    except (TypeError, ValueError) as exc:
        raise EffortMappingError("effort_unsupported") from exc
    mapping = EFFORT_PAYLOADS.get(model_id)
    if mapping is None or normalized not in mapping:
        raise EffortMappingError("effort_unsupported")
    return dict(mapping[normalized])
