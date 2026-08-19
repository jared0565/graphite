"""Binding for destructured callables (#20).

`const [value, setValue] = useState(...)` registers no definition for
`setValue`, so every call to a React state setter resolves to a file-scoped
phantom and health counts it UNBOUND. Measured across four React repos this was
the single largest cause of unbound `calls` edges -- 40% to 85% of them.

This is a different declarator shape from #16: there the declarator's *value*
was the function (`const f = () => ...`), so `_declarator_binding_name` could
name it. Here the value is a **call** whose result is destructured, and the
name side is an `array_pattern` (or `object_pattern`, the `const { t } =
useTranslation()` case).

Design (option (a), chosen deliberately over classifying these EXTERNAL_CALL):
register the binding as a real definition node, so `callers setValue` and
impact analysis return something useful -- not merely a better ratio.

A destructured binding becomes a definition ONLY when the same file calls it.
That keeps the node provably callable: it avoids fabricating `function` nodes
for the non-callable half of the pair (`value` in `[value, setValue]`) and
avoids inflating node counts with every destructured local in the repo.

The same-file assertions are the #7 guard: bound-to-wrong-target is invisible
to health, so it is not enough that the call resolves to *a* non-placeholder --
it must resolve to the declarator in its own file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.config import Config
from graphite.extract.ast import extract_all
from graphite.ingest import collect_files


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _extract(tmp_path: Path, resolver: str):
    cfg = Config(
        workers=1,
        cache_dir=tmp_path / ".cache" / "graphite",
        typescript_resolver=resolver,
    )
    entries = collect_files(tmp_path, cfg)
    return extract_all(entries, cfg)


def _calls(result) -> set[tuple[str, str]]:
    return {(e["source"], e["target"]) for e in result.edges if e["relation"] == "calls"}


def _node_ids(result) -> set[str]:
    return {n["id"] for n in result.nodes}


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_usestate_setter_binds_within_one_file(tmp_path: Path, resolver: str) -> None:
    """#20's core case. The NODE must exist -- an edge alone proves nothing,
    because the call machinery emits an edge to a phantom either way."""
    _write(
        tmp_path / "src" / "panel.tsx",
        "import { useState } from 'react';\n"
        "export function Panel() {\n"
        "  const [copied, setCopied] = useState(false);\n"
        "  function mark() { setCopied(true); }\n"
        "  return mark;\n"
        "}\n",
    )

    result = _extract(tmp_path, resolver)

    assert "src_panel_tsx_setcopied_55c89f" in _node_ids(result), (
        "destructured useState setter produced no bindable definition node"
    )
    assert ("src_panel_tsx_mark", "src_panel_tsx_setcopied_55c89f") in _calls(result)


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_object_destructured_callable_binds(tmp_path: Path, resolver: str) -> None:
    """`const { t } = useTranslation()` -- 414 unbound calls in open-design alone."""
    _write(
        tmp_path / "src" / "label.tsx",
        "import { useTranslation } from 'react-i18next';\n"
        "export function Label() {\n"
        "  const { t } = useTranslation();\n"
        "  function render() { return t('key'); }\n"
        "  return render;\n"
        "}\n",
    )

    result = _extract(tmp_path, resolver)

    assert "src_label_tsx_t" in _node_ids(result), (
        "object-destructured callable produced no bindable definition node"
    )
    assert ("src_label_tsx_render", "src_label_tsx_t") in _calls(result)


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_typescript_generic_hook_call_still_binds(tmp_path: Path, resolver: str) -> None:
    """`useState<Foo>(...)` is the dominant TS spelling; a fix keyed on the
    plain `useState(` shape would silently miss every TS repo."""
    _write(
        tmp_path / "src" / "form.tsx",
        "import { useState } from 'react';\n"
        "export function Form() {\n"
        "  const [name, setName] = useState<string>('');\n"
        "  function reset() { setName(''); }\n"
        "  return reset;\n"
        "}\n",
    )

    result = _extract(tmp_path, resolver)

    assert "src_form_tsx_setname_eedc29" in _node_ids(result)
    assert ("src_form_tsx_reset", "src_form_tsx_setname_eedc29") in _calls(result)


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_same_named_setters_bind_to_their_own_file(tmp_path: Path, resolver: str) -> None:
    """#7 guard: a rising ratio is not evidence of correctness. Two files
    declaring the same setter name must not cross-bind."""
    _write(
        tmp_path / "src" / "alpha.tsx",
        "import { useState } from 'react';\n"
        "export function Alpha() {\n"
        "  const [open, setOpen] = useState(false);\n"
        "  function toggle() { setOpen(true); }\n"
        "  return toggle;\n"
        "}\n",
    )
    _write(
        tmp_path / "src" / "beta.tsx",
        "import { useState } from 'react';\n"
        "export function Beta() {\n"
        "  const [open, setOpen] = useState(false);\n"
        "  function toggle() { setOpen(false); }\n"
        "  return toggle;\n"
        "}\n",
    )

    result = _extract(tmp_path, resolver)
    calls = _calls(result)
    ids = _node_ids(result)

    # Assert the NODES exist first: the call machinery emits an edge to a
    # phantom whether or not the target is real, so edge-only assertions here
    # passed against unfixed code.
    assert "src_alpha_tsx_setopen_963925" in ids and "src_beta_tsx_setopen_c22d73" in ids

    assert ("src_alpha_tsx_toggle", "src_alpha_tsx_setopen_963925") in calls
    assert ("src_beta_tsx_toggle", "src_beta_tsx_setopen_c22d73") in calls
    assert ("src_alpha_tsx_toggle", "src_beta_tsx_setopen_c22d73") not in calls
    assert ("src_beta_tsx_toggle", "src_alpha_tsx_setopen_963925") not in calls


@pytest.mark.parametrize("resolver", ["auto", "disabled"])
def test_uncalled_destructured_binding_creates_no_definition(tmp_path: Path, resolver: str) -> None:
    """The non-callable half of the pair must not become a `function` node.
    Without this the fix would fabricate a definition for every destructured
    local in the repo and inflate node counts."""
    _write(
        tmp_path / "src" / "view.tsx",
        "import { useState } from 'react';\n"
        "export function View() {\n"
        "  const [copied, setCopied] = useState(false);\n"
        "  function mark() { setCopied(true); }\n"
        "  return [copied, mark];\n"
        "}\n",
    )

    result = _extract(tmp_path, resolver)
    ids = _node_ids(result)

    assert "src_view_tsx_setcopied_ae9b2b" in ids, "the called half must bind"
    assert "src_view_tsx_copied" not in ids, (
        "never-called destructured binding was fabricated as a definition"
    )
