"""Bridge a z.ai plain-text whole-file edit response into the shared apply payload.

z.ai's native executor returns a bare plain-text message (no JSON envelope), so
the model emits each edited file verbatim between path-qualified markers and a
completion sentinel. This parser converts that into the exact payload
``apply_whole_file_edit`` consumes. It performs structural parsing plus a
defense-in-depth scope-set check only; ``apply_whole_file_edit`` remains the
sole authority on path safety and byte caps. Every non-conformance raises
``AdapterError("response_contract_invalid")``.

Preamble and interstitial prose (e.g. "Here are the files:") are tolerated by
design — non-marker lines outside blocks are skipped and the live smoke relies
on this. Two accepted, documented consequences: (1) a content line byte-identical
to its own path-qualified end marker truncates at that line (line boundaries
follow ``str.splitlines`` — any Unicode line terminator, not only ``\\n`` —
though content bytes are always sliced verbatim from the raw message, so
extraction stays byte-exact); (2) prose containing a begin-marker-shaped line
yields a phantom block, which surfaces as a scope-set mismatch (rejected, not
applied). Path-qualified markers plus first-matching-end parsing make both
improbable.
"""
from __future__ import annotations

import re

from .claude_executor import AdapterError
from .edit_apply import EDIT_RESULT_MARKER

EDIT_BEGIN_TEMPLATE = "===GRAPHITE BEGIN FILE {path}==="
EDIT_END_TEMPLATE = "===GRAPHITE END FILE {path}==="

# Trailing spaces/tabs and a CR are tolerated on marker lines (models append
# them constantly); a content line must still start with the exact marker
# prefix to match, so this stays false-positive-safe.
_BEGIN_RE = re.compile(r"^===GRAPHITE BEGIN FILE (?P<path>.+?)===[ \t]*\r?$")
_END_RE = re.compile(r"^===GRAPHITE END FILE (?P<path>.+?)===[ \t]*\r?$")


def _fail() -> AdapterError:
    return AdapterError("response_contract_invalid")


def parse_whole_file_edit_text(message: str, *, edit_scope: tuple[str, ...]) -> dict:
    """Parse a plain-text multi-file edit response into an apply payload.

    Returns ``{"files": [{"path","content"}, …], "result": EDIT_RESULT_MARKER}``
    with files ordered to match ``edit_scope``. Raises
    ``AdapterError("response_contract_invalid")`` on any non-conformance.
    """
    if not isinstance(message, str) or not message:
        raise _fail()
    if (
        not isinstance(edit_scope, tuple)
        or not edit_scope
        or not all(isinstance(path, str) for path in edit_scope)
        or len(set(edit_scope)) != len(edit_scope)
    ):
        raise _fail()

    lines = message.splitlines(keepends=True)
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    total = offset

    files: list[dict[str, str]] = []
    seen: set[str] = set()
    covered: list[tuple[int, int]] = []
    idx = 0
    n = len(lines)
    while idx < n:
        begin = _BEGIN_RE.match(lines[idx].rstrip("\n"))
        if begin is None:
            idx += 1
            continue
        path = begin.group("path")
        content_start = starts[idx] + len(lines[idx])
        jdx = idx + 1
        end_idx: int | None = None
        while jdx < n:
            stripped = lines[jdx].rstrip("\n")
            end = _END_RE.match(stripped)
            if end is not None and end.group("path") == path:
                end_idx = jdx
                break
            if _BEGIN_RE.match(stripped) is not None:
                break  # a new begin before our end -> malformed block
            jdx += 1
        if end_idx is None:
            raise _fail()
        if path in seen:
            raise _fail()
        seen.add(path)
        files.append({"path": path, "content": message[content_start:starts[end_idx]]})
        covered.append((starts[idx], starts[end_idx] + len(lines[end_idx])))
        idx = end_idx + 1

    if not files or seen != set(edit_scope):
        raise _fail()

    residual: list[str] = []
    cursor = 0
    for block_start, block_end in covered:
        residual.append(message[cursor:block_start])
        cursor = block_end
    residual.append(message[cursor:total])
    if EDIT_RESULT_MARKER not in "".join(residual):
        raise _fail()

    ordered = sorted(files, key=lambda item: edit_scope.index(item["path"]))
    return {"files": ordered, "result": EDIT_RESULT_MARKER}
