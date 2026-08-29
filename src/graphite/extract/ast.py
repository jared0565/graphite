"""Deterministic structural extraction via tree-sitter."""
from __future__ import annotations

import hashlib
import importlib
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Final, Sequence

from ..cache import Cache
from ..config import Config
from ..ingest import LANGUAGE_BY_EXT, FileEntry
from ..resolve import SourceIndex, should_keep_call_target


@dataclass
class ExtractionResult:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


_LANGUAGE_BUILTIN_GLOBALS: frozenset[str] = frozenset({
    "String", "Number", "Boolean", "Object", "Array", "Symbol", "BigInt",
    "Date", "RegExp", "Error", "TypeError", "RangeError", "SyntaxError",
    "ReferenceError", "EvalError", "URIError",
    "Promise", "Map", "Set", "WeakMap", "WeakSet", "JSON", "Math",
    "Reflect", "Proxy", "Intl",
    "parseInt", "parseFloat", "isNaN", "isFinite",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI",
    "URL", "URLSearchParams", "FormData", "Blob", "File",
    "Headers", "Request", "Response", "AbortController", "AbortSignal",
    "TextEncoder", "TextDecoder", "console",
    "str", "int", "float", "bool", "list", "dict", "set", "tuple", "bytes",
    "len", "range", "enumerate", "zip", "map", "filter", "sum", "min", "max",
    "print", "open", "isinstance", "type", "super", "sorted", "reversed",
    "any", "all", "abs", "round", "next", "iter", "hash", "id", "repr",
    "callable", "getattr", "setattr", "hasattr", "delattr", "vars", "dir",
    # Go builtins
    "append", "cap", "make", "copy", "delete", "panic", "recover", "close",
    "new", "println", "clear",
    # Rust prelude constructors/functions (macros like println! never reach
    # call extraction; these are the plain-call noise sources)
    "Some", "None", "Ok", "Err", "Box", "drop", "Default",
})


# Names that are never defined in-repo: test-framework injections, runtime
# globals, and language builtins missing from _LANGUAGE_BUILTIN_GLOBALS.
# These are TAGGED EXTERNAL_CALL and excluded from the health ratio by
# health.py -- NOT dropped -- so the excluded evidence stays visible and
# countable in graph.json. Nothing moves between this set and the drop-list
# above; see spec §4.3.
#
# Deliberately absent: generic words a repo plausibly defines itself
# (`context`, `run`, `setup`, `main`). A false external costs more than a
# missed one, because it would mask real code. Also absent: `process`,
# `console`, `window`, `document` -- already in resolve.py's _BUILTIN_OBJECTS,
# so their member calls are dropped before reaching this classifier.
_EXTERNAL_GLOBALS: frozenset[str] = frozenset({
    # test-framework injected globals (vitest / jest / mocha)
    "expect", "it", "describe", "test", "vi", "jest",
    "beforeEach", "afterEach", "beforeAll", "afterAll",
    "suite", "xit", "xdescribe", "fit", "fdescribe",
    # JS / Web runtime globals absent from the drop-list
    "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "fetch", "queueMicrotask", "structuredClone", "atob", "btoa",
    "crypto", "performance", "Buffer", "require",
    # Python builtins absent from the drop-list. This was a partial list of
    # Python's builtin exception hierarchy (found incomplete via operation
    # -firewall dogfooding, 2026-07-31: SystemExit and FileNotFoundError were
    # missing, so `raise FileNotFoundError(...)` classified LOCAL_CALL against
    # a synthesized unknown target instead of EXTERNAL_CALL like its sibling
    # ValueError) -- now the full builtin exception/warning hierarchy, not
    # just the names one dogfooding pass happened to exercise.
    "ValueError", "OSError", "AssertionError", "KeyError", "IndexError",
    "RuntimeError", "NotImplementedError", "StopIteration",
    "frozenset", "bytearray", "complex", "object", "Exception",
    "BaseException", "property", "staticmethod", "classmethod",
    "slice", "divmod", "format",
    "SystemExit", "GeneratorExit", "KeyboardInterrupt",
    "ArithmeticError", "FloatingPointError", "OverflowError", "ZeroDivisionError",
    "AttributeError", "BufferError", "EOFError",
    "ImportError", "ModuleNotFoundError", "LookupError", "MemoryError",
    "NameError", "UnboundLocalError", "RecursionError", "SystemError",
    "UnicodeError", "UnicodeDecodeError", "UnicodeEncodeError", "UnicodeTranslateError",
    "IndentationError", "TabError",
    "BlockingIOError", "ChildProcessError", "ConnectionError", "BrokenPipeError",
    "ConnectionAbortedError", "ConnectionRefusedError", "ConnectionResetError",
    "FileExistsError", "FileNotFoundError", "InterruptedError", "IsADirectoryError",
    "NotADirectoryError", "PermissionError", "ProcessLookupError", "TimeoutError",
    "Warning", "DeprecationWarning", "PendingDeprecationWarning", "RuntimeWarning",
    "UserWarning", "FutureWarning", "ImportWarning", "UnicodeWarning",
    "BytesWarning", "ResourceWarning",
    "BaseExceptionGroup", "ExceptionGroup",
})


def _call_confidence(
    called: str,
    external_names: Collection[str] = (),
    in_repo_names: Collection[str] = (),
    *,
    attributable: bool = True,
) -> str:
    """`LOCAL_CALL`, or `EXTERNAL_CALL` when the call provably leaves the repo.

    ``called`` may be dotted (``z.object``); the ROOT carries the binding, so
    that is what is tested. ``external_names`` holds local names bound by
    imports that did not resolve in-repo (Tasks 3 and 4). ``in_repo_names``
    holds local names bound by imports that DID resolve in-repo -- for
    TypeScript/JavaScript this is every binding form (default, namespace,
    named), not just the named imports resolution already tracks. An in-repo
    binding wins over both ``_EXTERNAL_GLOBALS`` and ``external_names``: the
    source index proved the name local, so a name collision with a global
    (``crypto`` the module vs. `crypto` the Web Crypto global) cannot make it
    external.

    This precedence check is threaded for BOTH TypeScript and Python (#14
    mechanism B). Python's bare-identifier and unresolved-member paths pass the
    keys of `symbol_map` from `_collect_python_import_maps`, so a name the
    source index proved local -- `helpers.py` defining `def format(...)`, then
    `from helpers import format` and a call to `format(...)` -- stays
    `LOCAL_CALL` rather than colliding with `_EXTERNAL_GLOBALS`.

    ``attributable`` is False when ``called`` is a bare method name recovered
    from a receiver the extractor could not name -- a regex or string literal,
    a call result, a subscript (``/re/.test(s)``, ``"{}".format(x)``,
    ``f().g()``). Such a name says nothing about where the call goes, so
    classifying it against the globals list produced a **false external**
    (#14 mechanism A): an edge excused from the health denominator despite
    never being shown to leave the repo. A false external inflates health,
    which is the more dangerous direction than a missed one. Unattributable
    calls are therefore never classified external.
    """
    if not attributable:
        return "LOCAL_CALL"
    root = called.split(".", 1)[0]
    if root in in_repo_names:
        return "LOCAL_CALL"
    if root in _EXTERNAL_GLOBALS or root in external_names:
        return "EXTERNAL_CALL"
    return "LOCAL_CALL"


# Languages whose extraction consults the SourceIndex, and whose cached result
# therefore depends on the repo's file set rather than on file content alone.
_RESOLVER_LANGUAGES: Final = frozenset({"python", "javascript", "typescript", "tsx", "jsx"})

_MAX_ID_LEN = 120


#: Length, in hex characters, of the ambiguity discriminator appended by
#: `_make_id`. Hex is the only alphabet that survives BOTH normalisations that
#: caused #57: `_+ -> _` cannot collapse it (it holds no underscores) and
#: `casefold()` cannot flatten it (it holds no case). A marker built from
#: anything else would be eaten by the very code it exists to defeat.
_ID_DISCRIMINATOR_LEN: Final = 6


def _identity_preserving_form(parts: Sequence[str]) -> str:
    """The id these parts would produce if the only loss were separator identity.

    Applies `[^\\w] -> _` one character at a time — deliberately NOT per run, so
    `a.b` and `a..b` stay distinct here even though the real pipeline collapses
    both. Everything else the sanitiser does is skipped.

    Comparing the real id against this answers the only question that matters:
    did the sanitiser throw away something that distinguished this input from a
    different one? If it did, the id is ambiguous and needs a discriminator.
    """
    return "_".join(
        re.sub(r"[^\w]", "_", unicodedata.normalize("NFKC", p), flags=re.UNICODE)
        for p in parts
    )


def _make_id(*parts: str) -> str:
    """Node id for a file or a symbol, ambiguous inputs discriminated (#57).

    The sanitiser has FIVE lossy operations — `strip("_.")` per part,
    `[^\\w]+ -> _`, `_+ -> _`, `casefold()` and truncation — and each of them
    merged real definitions. Measured on this repo before the fix: 5 same-file
    collisions and 6 definitions absent from the graph, including
    `routing/storage.py`'s `initialize` (L950) swallowing `_initialize` (L3965),
    so `self._initialize()` was recorded as a call to an unrelated method.

    Removing a normalisation does not fix this, which is worth knowing before
    trying: drop the strip and `path`/`_path` are STILL merged, because the
    `_+` collapse two lines down puts them back together; drop both and
    `path`/`Path` remain merged by the casefold. Only an appended discriminator
    works, and only one made of hex.

    A canonical input keeps its plain, readable id — the discriminator is for
    ambiguity, not decoration, and hashing everything would cost a far larger
    migration for no extra correctness.

    Known residual: two DIFFERENT part tuples can still produce one id when the
    only difference is a separator versus a literal underscore — `a/b.py` and
    `a_b.py` both reach `a_b_py`, and both are ambiguity-free by this test.
    Closing it would mean discriminating every path containing an underscore,
    which is most of `tests/`. Measured occurrences in this repo: zero.
    """
    kept = [p for p in parts if p]
    combined = unicodedata.normalize("NFKC", "_".join(p.strip("_.") for p in kept))
    cleaned = re.sub(r"[^\w]+", "_", combined, flags=re.UNICODE)
    cleaned = re.sub(r"_+", "_", cleaned)
    cleaned = cleaned.strip("_").casefold()
    if cleaned == _identity_preserving_form(kept) and len(cleaned) <= _MAX_ID_LEN:
        return cleaned
    # Hashed over the exact input with a separator no name can contain, so the
    # marker distinguishes ("a_b", "c") from ("a", "b_c") even though both
    # sanitise alike.
    marker = hashlib.blake2s(
        "\x00".join(kept).encode("utf-8"), digest_size=_ID_DISCRIMINATOR_LEN // 2
    ).hexdigest()
    return f"{cleaned[: _MAX_ID_LEN - len(marker) - 1].rstrip('_')}_{marker}"


def _file_node_id(rel_path: str) -> str:
    """Stable node id for a file: a slug of the FULL repo-relative path AND name.

    Two claims this docstring used to make were false, and both were disproved
    on graphite's own source rather than in theory.

    It said full-path ids "keep every file distinct". They did not. `ARAMID.md`
    and `aramid.toml` both reached `aramid` (case), and `src/graphite/init.py`
    and `src/graphite/__init__.py` both reached `src_graphite_init` (underscore
    strip plus collapse) — so a 791-line module had NO file node of its own and
    its 21 symbols hung off a 41-line `__init__.py`. `_make_id`'s discriminator
    is what fixes that half.

    It also built the id from `path.stem`, silently DISCARDING the extension, so
    `index.ts` and `index.js`, or `Button.tsx` and its `Button.css`, were one
    node (#58). That is not a sanitisation loss and no discriminator could fix
    it — the two inputs were already identical by the time `_make_id` saw them.
    The extension is part of a file's identity and is now kept.

    The original reason for full-path ids stands: parent-dir + stem merged any
    two files sharing that tail, so a monorepo with `apps/worker/src/db/queries.ts`
    and `apps/workers/booking/src/db/queries.ts` got ONE `db_queries` node.
    """
    path = Path(rel_path)
    # `path.parts` already ends in the full filename; the old scheme replaced
    # that last element with `path.stem` and lost the suffix with it.
    parts = list(path.parts)
    return _make_id(".".join(p for p in parts if p not in (".", "")))


def _node(
    id_: str,
    kind: str,
    name: str,
    rel_path: str,
    line: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    n: dict[str, Any] = {
        "id": id_,
        "kind": kind,
        "name": name,
        "source_file": rel_path,
    }
    if line is not None:
        n["source_location"] = f"L{line}"
    if extra:
        n.update(extra)
    return n


def _edge(
    source: str,
    target: str,
    relation: str,
    rel_path: str,
    line: int | None = None,
    context: str | None = None,
    confidence: str = "EXTRACTED",
) -> dict[str, Any]:
    e: dict[str, Any] = {
        "source": source,
        "target": target,
        "relation": relation,
        "source_file": rel_path,
        "confidence": confidence,
        "weight": 1.0,
    }
    if line is not None:
        e["source_location"] = f"L{line}"
    if context:
        e["context"] = context
    return e


class _TreeSitterLoader:
    """Lazy parser cache per language."""

    def __init__(self) -> None:
        self._parsers: dict[str, Any] = {}

    def parser(self, language: str) -> Any | None:
        if language in self._parsers:
            return self._parsers[language]
        mapping = {
            "javascript": ("tree_sitter_javascript", "language"),
            "typescript": ("tree_sitter_typescript", "language_typescript"),
            "tsx": ("tree_sitter_typescript", "language_tsx"),
            "jsx": ("tree_sitter_javascript", "language"),
            "python": ("tree_sitter_python", "language"),
            "go": ("tree_sitter_go", "language"),
            "rust": ("tree_sitter_rust", "language"),
        }
        item = mapping.get(language)
        if not item:
            return None
        pkg, attr = item
        try:
            mod = importlib.import_module(pkg)
            lang_fn = getattr(mod, attr)
            lang_mod = importlib.import_module("tree_sitter")
            parser = lang_mod.Parser(lang_mod.Language(lang_fn()))
            self._parsers[language] = parser
            return parser
        except Exception as e:
            if sys.stderr:
                print(f"[graphite] parser load failed for {language}: {e}", file=sys.stderr)
            return None


_LOADER = _TreeSitterLoader()


class _Scope:
    """A call-attribution scope (the nearest enclosing function/method/arrow).

    ``pending`` holds a node dict that is emitted lazily the first time the scope
    is used as the source of an edge. Named functions are materialized eagerly
    (``pending is None``); anonymous arrows/functions are only materialized if
    they actually emit a call, so trivial callbacks never bloat the graph.
    """

    __slots__ = ("id", "pending")

    def __init__(self, node_id: str, pending: dict[str, Any] | None = None) -> None:
        self.id = node_id
        self.pending = pending


def _extract_ts_js(file_id: str, rel_path: str, source: bytes, tree: Any, source_index: SourceIndex | None = None) -> ExtractionResult:
    result = ExtractionResult()
    root = tree.root_node

    def _line(node: Any) -> int:
        return (node.start_point[0] + 1) if node.start_point else 1

    def _name(node: Any) -> str | None:
        # identifiers, property_identifier, type_identifier, etc.
        for child in node.children:
            if child.type.endswith("identifier"):
                text = child.text.decode("utf-8", errors="ignore") if child.text else None
                return _short_name(text)
        return None

    # Add file node.
    result.nodes.append(_node(file_id, "file", Path(rel_path).name, rel_path))

    # Pre-pass: map locally-bound imported names to the definition node id in
    # the file that defines them, so cross-file calls resolve to the real target
    # instead of a same-file phantom.
    bindings = _collect_ts_import_symbols(root, rel_path, source_index)

    def _materialize(scope: _Scope) -> None:
        if scope.pending is not None:
            result.nodes.append(scope.pending)
            scope.pending = None

    file_scope = _Scope(file_id)  # file node already emitted above

    def _anon_scope(node: Any) -> _Scope:
        syn = _safe_label(_synthetic_fn_name(node, source))
        line = _line(node)
        col = (node.start_point[1] + 1) if node.start_point else 1
        mid = _make_id(file_id, syn, f"l{line}", f"c{col}")
        pending = _node(mid, "function", syn, rel_path, line, extra={"anonymous": True})
        return _Scope(mid, pending=pending)

    # Destructured hook results: `const [v, setV] = useState(...)` binds a
    # callable that no declaration names, so calls to it could never resolve
    # (#20). Collected during the walk, materialized after it -- see the
    # post-pass below for why the decision has to be deferred.
    hook_bindings: dict[str, tuple[str | None, int]] = {}

    def _record_hook_destructuring(node: Any, parent_id: str | None) -> None:
        name = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name is None or value is None:
            return
        if name.type not in ("array_pattern", "object_pattern"):
            return
        if not _is_hook_call(value):
            return
        for ident, text in _pattern_identifiers(name):
            if text and text not in hook_bindings:
                hook_bindings[text] = (parent_id, _line(ident))

    # Walk for declarations and calls. ``parent_id`` is the nearest named
    # container (for ``contains`` edges); ``scope`` is the nearest function-like
    # scope (for ``calls`` attribution).
    def walk(node: Any, parent_id: str | None, scope: _Scope) -> None:
        t = node.type
        if t in ("function_declaration", "generator_function_declaration", "function", "generator_function", "method_definition"):
            # An anonymous function expression assigned to a name is callable by
            # that name, so it takes the named path too (#16).
            name = _name(node) or _declarator_binding_name(node)
            if name:
                mid = _make_id(file_id, name)
                # Tag class methods so the global method-dispatch post-pass can
                # resolve `recv.method()` member calls to this definition (_merge).
                extra = {"is_method": True} if t == "method_definition" else None
                result.nodes.append(_node(mid, "function", name, rel_path, _line(node), extra=extra))
                if parent_id:
                    result.edges.append(_edge(parent_id, mid, "contains", rel_path, _line(node)))
                walk_children(node, mid, _Scope(mid))
            else:
                walk_children(node, parent_id, _anon_scope(node))
        elif t in ("arrow_function", "function_expression", "generator_function_expression"):
            # Arrows carry no name of their own; _name would misread a bare
            # single parameter as the name, so they are anonymous by default.
            # A variable-declarator binding is the one exception: it makes the
            # function callable by name, so it must get the same id shape a
            # `function f()` declaration produces or nothing can bind to it.
            bound = _declarator_binding_name(node)
            # A class field holding an arrow is callable too, but only through
            # `this.handle()` / `obj.handle()`, so it needs the `is_method` tag
            # the dispatch post-pass indexes on -- a bare name is not enough (#19).
            field = _class_field_binding_name(node) if not bound else None
            name = bound or field
            if name:
                mid = _make_id(file_id, name)
                extra = {"is_method": True} if field else None
                result.nodes.append(_node(mid, "function", name, rel_path, _line(node), extra=extra))
                if parent_id:
                    result.edges.append(_edge(parent_id, mid, "contains", rel_path, _line(node)))
                walk_children(node, mid, _Scope(mid))
            elif t == "arrow_function":
                walk_children(node, parent_id, _anon_scope(node))
            else:
                # Unbound function expressions keep their previous handling:
                # calls inside them stay attributed to the enclosing scope.
                walk_children(node, parent_id, scope)
        elif t == "class_declaration":
            name = _name(node)
            if name:
                cid = _make_id(file_id, name)
                result.nodes.append(_node(cid, "class", name, rel_path, _line(node)))
                if parent_id:
                    result.edges.append(_edge(parent_id, cid, "contains", rel_path, _line(node)))
                walk_children(node, cid, scope)
            else:
                walk_children(node, parent_id, scope)
        elif t == "import_statement":
            # import ... from 'module' or import 'module'
            source_lit = None
            for child in node.children:
                if child.type == "string":
                    source_lit = child.text.decode("utf-8", errors="ignore").strip("'\"")
                elif child.type == "import_clause":
                    pass
            if source_lit:
                resolved = _resolve_import(rel_path, source_lit, source_index)
                if resolved:
                    result.edges.append(
                        _edge(
                            file_id,
                            _file_node_id(resolved.rel_path),
                            "imports",
                            rel_path,
                            _line(node),
                            confidence=resolved.confidence,
                        )
                    )
                else:
                    result.edges.append(_edge(file_id, _make_id(source_lit), "imports", rel_path, _line(node), confidence="EXTERNAL_IMPORT"))
            walk_children(node, parent_id, scope)
        elif t in ("call_expression", "new_expression"):
            # A `require('<literal>')` is a module load wearing a call's syntax.
            # Emitted here, beside the call handling, because that is where the
            # node actually appears -- the `import_statement` arm above can
            # never see it. Detection is shared with the binding collector via
            # `_require_source_literal` so the two cannot diverge.
            require_lit = _require_source_literal(node) if t == "call_expression" else None
            if require_lit:
                required = _resolve_import(rel_path, require_lit, source_index)
                if required:
                    result.edges.append(
                        _edge(
                            file_id,
                            _file_node_id(required.rel_path),
                            "imports",
                            rel_path,
                            _line(node),
                            confidence=required.confidence,
                        )
                    )
                else:
                    result.edges.append(
                        _edge(
                            file_id,
                            _make_id(require_lit),
                            "imports",
                            rel_path,
                            _line(node),
                            confidence="EXTERNAL_IMPORT",
                        )
                    )
            func = node.child_by_field_name("function")
            if func is None and t == "new_expression":
                # tree-sitter names this field `constructor` on a new_expression,
                # not `function`, so the lookup above always returned None and
                # this whole arm was dead code for construction (#15).
                func = node.child_by_field_name("constructor")
            if func:
                called = _call_target_name(func, source)
                if called and called not in _LANGUAGE_BUILTIN_GLOBALS and should_keep_call_target(called):
                    target_id = _resolve_call(
                        file_id, called, bindings.resolved, bindings.namespaces
                    )
                    _materialize(scope)
                    # A member call whose receiver could not be named leaves
                    # `called` as a bare method name, which says nothing about
                    # where the call goes -- do not classify it (#14).
                    attributable = True
                    namespace_resolved = False
                    if func.type == "member_expression":
                        obj = func.child_by_field_name("object")
                        obj_name = _simple_object_name(obj) if obj is not None else None
                        attributable = bool(obj_name)
                        namespace_resolved = bool(
                            obj_name and obj_name in bindings.namespaces
                        )
                    edge = _edge(
                        scope.id, target_id, "calls", rel_path, _line(node),
                        confidence=_call_confidence(
                            called, bindings.external, bindings.in_repo, attributable=attributable
                        ),
                    )
                    # Method dispatch: for `recv.method(...)` the callee is a
                    # member_expression and target_id above is only a file-scoped
                    # phantom. Stash the bare method name so the global post-pass
                    # can re-point this edge to the real class method definition
                    # (see _resolve_method_dispatch). `new x.Foo()` is construction,
                    # not dispatch, so restrict to call_expression.
                    # Skipped when the receiver was a whole-module binding: the
                    # post-pass exists for edges whose target is "only a
                    # file-scoped phantom", and re-points on method NAME alone.
                    # A namespace-resolved edge already points at the real
                    # definition, so leaving `_member` set would let any
                    # same-named class method elsewhere steal it back.
                    if (
                        t == "call_expression"
                        and func.type == "member_expression"
                        and not namespace_resolved
                    ):
                        prop = func.child_by_field_name("property")
                        method = prop.text.decode("utf-8", errors="ignore") if prop is not None and prop.text else None
                        if method:
                            edge["_member"] = method
                    result.edges.append(edge)
            walk_children(node, parent_id, scope)
        elif t == "variable_declarator":
            _record_hook_destructuring(node, parent_id)
            walk_children(node, parent_id, scope)
        else:
            walk_children(node, parent_id, scope)

    def walk_children(node: Any, parent_id: str | None, scope: _Scope) -> None:
        for child in node.children:
            walk(child, parent_id, scope)

    walk(root, file_id, file_scope)
    # Materialize a destructured hook binding ONLY if this file actually calls
    # it. Deferring the decision is what keeps the node provably callable: the
    # non-callable half of `[value, setValue]` never gets a `function` node, and
    # destructured locals that nobody invokes do not inflate the node count.
    # A name that resolved to an import is skipped too -- `_resolve_call` would
    # have pointed the call at the exporting file, so `mid` is absent from
    # `called` and the real cross-file target keeps the edge (#20).
    if hook_bindings:
        existing = {n["id"] for n in result.nodes}
        called = {e["target"] for e in result.edges if e["relation"] == "calls"}
        for bound_name, (parent_id, line) in hook_bindings.items():
            mid = _make_id(file_id, bound_name)
            if mid in existing or mid not in called:
                continue
            result.nodes.append(_node(mid, "function", bound_name, rel_path, line))
            if parent_id:
                result.edges.append(_edge(parent_id, mid, "contains", rel_path, line))
            existing.add(mid)
    if source_index is not None:
        for edge in source_index.supplemental_ts_edges(rel_path):
            result.edges.append(
                _edge(
                    file_id,
                    _file_node_id(edge.target),
                    edge.relation,
                    rel_path,
                    edge.line,
                    context=edge.specifier,
                    confidence=edge.confidence,
                )
            )
    return result


@dataclass(frozen=True)
class _ImportBindings:
    """What a file's import statements bind.

    ``resolved`` maps a local name to the definition node id in the in-repo
    file that exports it (used for call resolution). ``external`` holds local
    names bound by imports that did NOT resolve in-repo, in every binding form
    -- those calls leave the repo and are tagged EXTERNAL_CALL. ``in_repo``
    holds local names bound by imports that DID resolve in-repo, in every
    binding form -- an in-repo binding must win over an `_EXTERNAL_GLOBALS`
    name collision (`crypto` the local module vs. `crypto` the Web Crypto
    global), mirroring the precedence Python's import maps already give
    resolved names for free (they simply never enter `external_names`).
    """
    resolved: dict[str, str]
    external: frozenset[str]
    in_repo: frozenset[str] = frozenset()
    #: Local name -> the FILE node id it stands for, for a whole-module binding:
    #: `import * as ns from './x'` and `const m = require('./x')`. A call
    #: `ns.f()` resolves to `<that file>_f`, which is exactly what Python's
    #: `alias_map` has always done for `import x` + `x.attr()`. Separate from
    #: `resolved` because that maps a name to a DEFINITION, and these names
    #: stand for a whole file rather than any one export.
    namespaces: dict[str, str] = field(default_factory=dict)


def _collect_ts_import_symbols(root: Any, rel_path: str, source_index: SourceIndex | None) -> _ImportBindings:
    """What this file's imports bind: resolved definitions and external names.

    Only *named* imports are mapped for resolution: ``import { foo }`` /
    ``import { foo as bar }``. The value is ``<defining-file-id>_<original-export-name>``
    so a call to the local name links to the real definition node in the file
    that exports it. Default and namespace imports are skipped for resolution
    (they can't be tied to a single named definition), so those calls fall
    back to same-file resolution. Every binding form (default, namespace,
    named) is collected for externality when its import does not resolve
    in-repo, and for `in_repo` (classification precedence, not resolution)
    when it does.
    """
    symbols: dict[str, str] = {}
    external: set[str] = set()
    in_repo: set[str] = set()
    namespaces: dict[str, str] = {}
    #: Local names bound by a `require()` declarator, so the shadowing guard
    #: below applies to exactly what CommonJS support introduced.
    cjs_locals: set[str] = set()
    if source_index is None:
        return _ImportBindings(symbols, frozenset(), frozenset(), {})

    def _handle_import_statement(node: Any) -> None:
        source_lit = None
        clause = None
        for child in node.children:
            if child.type == "string":
                source_lit = child.text.decode("utf-8", errors="ignore").strip("'\"")
            elif child.type == "import_clause":
                clause = child
        if not source_lit or clause is None:
            return
        resolved = _resolve_import(rel_path, source_lit, source_index)
        if resolved is None:
            # Unresolved module: every name it binds leaves the repo.
            external.update(_iter_bound_local_names(clause))
            return
        # Resolved module: every name it binds is proven in-repo, regardless
        # of binding form -- must win over an _EXTERNAL_GLOBALS collision.
        in_repo.update(_iter_bound_local_names(clause))
        target_file_id = _file_node_id(resolved.rel_path)
        for local, original in _iter_named_imports(clause):
            symbols[local] = _make_id(target_file_id, original)
        for child in clause.children:
            if child.type != "namespace_import":  # import * as ns from './x'
                continue
            for sub in child.children:
                if sub.type.endswith("identifier") and sub.text:
                    namespaces[sub.text.decode("utf-8", errors="ignore")] = target_file_id

    def _handle_require_declarator(node: Any) -> None:
        """`const m = require('./x')` and `const { f } = require('./x')`.

        A require is a call expression, so none of this is reachable from the
        `import_statement` walk above -- which is why CommonJS bound nothing at
        all before #49.
        """
        value = node.child_by_field_name("value")
        name_node = node.child_by_field_name("name")
        if value is None or name_node is None:
            return
        source_lit = _require_source_literal(value)
        if not source_lit:
            return
        if name_node.type.endswith("identifier"):
            if not name_node.text:
                return
            local = name_node.text.decode("utf-8", errors="ignore")
            bound, pairs = [local], []
        elif name_node.type == "object_pattern":
            pairs = list(_iter_object_pattern_names(name_node))
            bound = [local for local, _original in pairs]
        else:
            return
        if not bound:
            return
        cjs_locals.update(bound)
        resolved = _resolve_import(rel_path, source_lit, source_index)
        if resolved is None:
            external.update(bound)
            return
        in_repo.update(bound)
        target_file_id = _file_node_id(resolved.rel_path)
        if pairs:
            for local, original in pairs:
                symbols[local] = _make_id(target_file_id, original)
        else:
            namespaces[bound[0]] = target_file_id

    def visit(node: Any) -> None:
        # Recursive, unlike the old top-level-only scan: a `require` inside a
        # function body binds a name the same way one at module scope does.
        if node.type == "import_statement":
            _handle_import_statement(node)
        elif node.type == "variable_declarator":
            _handle_require_declarator(node)
        for child in node.children:
            visit(child)

    visit(root)
    # Applied only to what CommonJS introduced. ESM binding forms are
    # statement-level and were never re-derived from a declarator, so filtering
    # them here would change long-standing behaviour for a hazard this change
    # did not create.
    rebound = _rebound_local_names(root) & cjs_locals
    for name in rebound:
        namespaces.pop(name, None)
        symbols.pop(name, None)
    return _ImportBindings(symbols, frozenset(external), frozenset(in_repo), namespaces)


def _iter_named_imports(clause: Any):
    """Yield (local_name, original_export_name) for each named import in a clause."""
    for child in clause.children:
        if child.type != "named_imports":
            continue
        for spec in child.children:
            if spec.type != "import_specifier":
                continue
            name_node = spec.child_by_field_name("name")
            if name_node is None or not name_node.text:
                continue
            original = name_node.text.decode("utf-8", errors="ignore")
            alias_node = spec.child_by_field_name("alias")
            local = (
                alias_node.text.decode("utf-8", errors="ignore")
                if alias_node is not None and alias_node.text
                else original
            )
            yield local, original


def _iter_bound_local_names(clause: Any):
    """Yield every local name a TS import clause binds.

    `_iter_named_imports` is deliberately narrower: resolution needs the
    original export name, so it handles only `named_imports`. Externality needs
    only the local name, so all three binding forms count here -- otherwise
    `import axios from 'axios'` and `import * as lib from 'lib'` stay invisible.
    """
    for child in clause.children:
        if child.type == "identifier":          # import axios from 'axios'
            if child.text:
                yield child.text.decode("utf-8", errors="ignore")
        elif child.type == "namespace_import":  # import * as lib from 'lib'
            for sub in child.children:
                if sub.type == "identifier" and sub.text:
                    yield sub.text.decode("utf-8", errors="ignore")
        elif child.type == "named_imports":     # import { a, b as c } from 'lib'
            for spec in child.children:
                if spec.type != "import_specifier":
                    continue
                alias_node = spec.child_by_field_name("alias")
                name_node = spec.child_by_field_name("name")
                chosen = alias_node if alias_node is not None else name_node
                if chosen is not None and chosen.text:
                    yield chosen.text.decode("utf-8", errors="ignore")


def _require_source_literal(node: Any) -> str | None:
    """The module string of `require('<literal>')`, else None.

    ONE definition, called from both the binding collector and the walk that
    emits the import edge. Two independent detections of the same syntax drift
    the moment one of them learns about `require.resolve` or a template literal
    and the other does not.

    Literal-only on purpose: `require(someExpr)` is a genuine dynamic import
    with no statically knowable target, and stays unmodelled -- and declared.
    """
    if node.type != "call_expression":
        return None
    func = node.child_by_field_name("function")
    if func is None or not func.type.endswith("identifier") or not func.text:
        return None
    if func.text.decode("utf-8", errors="ignore") != "require":
        return None
    arguments = node.child_by_field_name("arguments")
    if arguments is None:
        return None
    literals = [child for child in arguments.children if child.type == "string"]
    # Exactly one string argument. `require(a, b)` is not a module load, and a
    # template literal parses as `template_string`, so it never lands here.
    if len(literals) != 1 or not literals[0].text:
        return None
    return literals[0].text.decode("utf-8", errors="ignore").strip("'\"") or None


def _rebound_local_names(root: Any) -> frozenset[str]:
    """Names this file binds more than once, anywhere, at any depth.

    The CommonJS binding maps are FILE-level while calls are walked per scope,
    so an inner `const m = ...`, a parameter named `m`, or a second destructure
    of the same name is indistinguishable from the module binding at resolution
    time -- and would make `m.real()` claim the module's definition, putting a
    caller in `callers real` that does not exist.

    Deliberately blunt: count every binding occurrence and distrust any name
    that appears twice, rather than modelling JavaScript scope. That FAILS
    CLOSED, giving up an edge instead of inventing one, which is the right
    direction for a graph whose empty answers are graded honestly. Real
    scope tracking would bind more, and is a larger change than #49.
    """
    counts: dict[str, int] = {}

    def _count(name_node: Any) -> None:
        if name_node is None:
            return
        if name_node.type.endswith("identifier"):
            if name_node.text:
                name = name_node.text.decode("utf-8", errors="ignore")
                counts[name] = counts.get(name, 0) + 1
        elif name_node.type in ("object_pattern", "array_pattern"):
            for _node, name in _pattern_identifiers(name_node):
                counts[name] = counts.get(name, 0) + 1

    def visit(node: Any) -> None:
        if node.type == "variable_declarator":
            _count(node.child_by_field_name("name"))
        elif node.type in ("formal_parameters", "arrow_function"):
            for child in node.children:
                if child.type in ("required_parameter", "optional_parameter"):
                    _count(child.child_by_field_name("pattern"))
                elif child.type.endswith("identifier") or child.type in (
                    "object_pattern",
                    "array_pattern",
                ):
                    _count(child)
        for child in node.children:
            visit(child)

    visit(root)
    return frozenset(name for name, count in counts.items() if count > 1)


def _iter_object_pattern_names(pattern: Any):
    """Yield (local_name, original_export_name) for `const { a, b: c } = ...`."""
    for child in pattern.children:
        if child.type == "shorthand_property_identifier_pattern":
            if child.text:
                name = child.text.decode("utf-8", errors="ignore")
                yield name, name
        elif child.type == "pair_pattern":
            key = child.child_by_field_name("key")
            value = child.child_by_field_name("value")
            if key is None or value is None or not key.text or not value.text:
                continue
            if not value.type.endswith("identifier"):
                # Nested destructuring binds no single callable name.
                continue
            yield (
                value.text.decode("utf-8", errors="ignore"),
                key.text.decode("utf-8", errors="ignore"),
            )


def _declarator_binding_name(node: Any) -> str | None:
    """The plain identifier a function-valued variable declarator binds to.

    ``const f = () => ...`` and ``const f = function () {}`` make ``f`` callable
    by that name, so the definition must carry the same id shape a
    ``function f()`` declaration produces -- otherwise no call to ``f()`` can
    ever bind, even inside the defining file (#16).

    Returns None for anything that binds no such callable name: destructuring
    patterns, object-literal property values, class fields, and callbacks.
    """
    parent = node.parent
    if parent is None or parent.type != "variable_declarator":
        return None
    value = parent.child_by_field_name("value")
    # Compare by node id, not identity: the tree-sitter binding returns a fresh
    # Node object per call, so `value is node` is always False.
    if value is None or value.id != node.id:
        return None
    name = parent.child_by_field_name("name")
    if name is None or name.type != "identifier" or not name.text:
        return None
    return name.text.decode("utf-8", errors="ignore") or None


def _class_field_binding_name(node: Any) -> str | None:
    """The field name a function-valued class field binds to.

    ``handle = () => 1`` inside a class parses as ``public_field_definition``
    (``field_definition`` in plain JS), not ``method_definition``, so it never
    reached the named path and produced no call target at all (#19). This is the
    case #16 explicitly scoped out.

    Such a field is invoked as ``this.handle()`` / ``obj.handle()`` -- through
    method dispatch rather than by bare name -- so the caller must also tag the
    resulting node ``is_method``, or the dispatch post-pass will not index it and
    the call edge stays dropped.

    Returns None for anything that binds no such callable name: object-literal
    property values and callbacks keep the deliberate anonymity of #16.
    """
    parent = node.parent
    if parent is None or parent.type not in ("public_field_definition", "field_definition"):
        return None
    value = parent.child_by_field_name("value")
    # Compare by node id, not identity: the tree-sitter binding returns a fresh
    # Node object per call, so `value is node` is always False.
    if value is None or value.id != node.id:
        return None
    name = parent.child_by_field_name("name")
    if name is None or name.type not in ("property_identifier", "identifier") or not name.text:
        return None
    return name.text.decode("utf-8", errors="ignore") or None


# React enforces that hooks are named `use<Capital>` -- it is a real language
# convention (eslint-plugin-react-hooks keys on exactly this), not a guess.
# Restricting destructure-binding to hook calls is deliberate: `const { readFile }
# = require('fs')` destructures an EXTERNAL callable, and registering that as an
# in-repo definition would be a bound-to-wrong-target error, which #7 established
# is invisible to health. A missed binding is honestly counted; a false one is not.
_HOOK_CALL_NAME = re.compile(r"^use[A-Z]\w*$")


def _is_hook_call(value: Any) -> bool:
    """True for `useThing(...)` / `React.useThing(...)`, including `useThing<T>(...)`.

    The TS generic spelling matters: `useState<string>('')` is the dominant form
    in typed React code, and keying on the plain `useState(` shape would miss
    every TypeScript repo (#20).
    """
    if value is None or value.type != "call_expression":
        return False
    func = value.child_by_field_name("function")
    if func is None or not func.text:
        return False
    name = func.text.decode("utf-8", errors="ignore").strip().rsplit(".", 1)[-1]
    return bool(_HOOK_CALL_NAME.match(name))


def _pattern_identifiers(pattern: Any) -> list[tuple[Any, str]]:
    """Binding identifiers introduced by a destructuring pattern.

    Covers `[a, b]`, `{ a }`, `{ a: b }`, defaults (`[a = 1]`) and nesting. Only
    the *binding* side is collected -- an object pattern's key is a
    `property_identifier`, so `{ a: b }` yields `b` and never `a`.
    """
    out: list[tuple[Any, str]] = []

    def visit(node: Any) -> None:
        for child in node.children:
            ct = child.type
            if ct in ("identifier", "shorthand_property_identifier_pattern"):
                if child.text:
                    out.append((child, child.text.decode("utf-8", errors="ignore")))
            elif ct == "pair_pattern":
                value = child.child_by_field_name("value")
                if value is None:
                    continue
                if value.type == "identifier" and value.text:
                    out.append((value, value.text.decode("utf-8", errors="ignore")))
                elif value.type in ("array_pattern", "object_pattern"):
                    visit(value)
            elif ct == "assignment_pattern":
                # `[a = fallback()]` -- take the binding, never the default expr.
                left = child.child_by_field_name("left")
                if left is not None and left.type == "identifier" and left.text:
                    out.append((left, left.text.decode("utf-8", errors="ignore")))
            elif ct in ("array_pattern", "object_pattern", "rest_pattern"):
                visit(child)

    visit(pattern)
    return out


def _synthetic_fn_name(node: Any, source: bytes) -> str | None:
    """Derive a stable, human-ish name for an anonymous function from its context.

    Handles the common shapes: object property value (``run: () => ...``),
    variable binding (``const f = () => ...``), assignment, class field, and
    callback arguments (``app.post('/x', () => ...)`` -> ``app.post /x``).
    Returns None when no context is available (caller falls back to ``anon``).
    """
    parent = node.parent
    if parent is None:
        return None
    pt = parent.type
    if pt == "pair":
        key = parent.child_by_field_name("key")
        if key is not None and key.text:
            return _short_name(key.text.decode("utf-8", errors="ignore"))
    elif pt == "variable_declarator":
        nm = parent.child_by_field_name("name")
        if nm is not None and nm.text:
            return _short_name(nm.text.decode("utf-8", errors="ignore"))
    elif pt == "assignment_expression":
        left = parent.child_by_field_name("left")
        if left is not None:
            nm = _simple_object_name(left)
            if nm:
                return _short_name(nm)
            if left.text:
                return _short_name(left.text.decode("utf-8", errors="ignore"))
    elif pt in ("public_field_definition", "field_definition", "property_signature"):
        nm = parent.child_by_field_name("name")
        if nm is not None and nm.text:
            return _short_name(nm.text.decode("utf-8", errors="ignore"))
    elif pt == "arguments":
        gp = parent.parent
        if gp is not None and gp.type in ("call_expression", "new_expression"):
            callee = gp.child_by_field_name("function")
            callee_name = _call_target_name(callee, source) if callee is not None else None
            str_arg = None
            for arg in parent.children:
                if arg.type == "string":
                    str_arg = arg.text.decode("utf-8", errors="ignore").strip("'\"`")
                    break
            if callee_name and str_arg:
                return _short_name(f"{callee_name} {str_arg}")
            if callee_name:
                return _short_name(callee_name)
    return None


_MAX_NAME_LEN = 80


def _short_name(text: str | None) -> str | None:
    if not text:
        return None
    text = text.strip()
    if len(text) > _MAX_NAME_LEN:
        # For chained/long names keep the final segment.
        return text.split(".")[-1][: _MAX_NAME_LEN]
    return text


def _safe_label(text: str | None) -> str:
    """Normalize a synthetic label into a compact, console-safe display name.

    Synthetic names are derived from arbitrary source strings (route paths, test
    descriptions, i18n literals) which may contain newlines, emoji, or other
    non-ASCII characters. Collapse whitespace and drop anything outside printable
    ASCII so labels stay short and render on any terminal/encoding.
    """
    if not text:
        return "anon"
    # Replace control/non-ASCII with spaces, then collapse, so a stripped
    # character between words doesn't leave a double space.
    text = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_NAME_LEN] if text else "anon"


def _simple_object_name(node: Any) -> str | None:
    """Return a short name for the object of a member_expression, or None if too complex."""
    if node.type.endswith("identifier"):
        return node.text.decode("utf-8", errors="ignore") if node.text else None
    if node.type == "member_expression":
        obj = node.child_by_field_name("object")
        prop = node.child_by_field_name("property")
        if obj and prop:
            obj_name = _simple_object_name(obj)
            prop_name = prop.text.decode("utf-8", errors="ignore") if prop.text else None
            if obj_name and prop_name:
                combined = f"{obj_name}.{prop_name}"
                return _short_name(combined)
        return None
    # Complex literals (array, object, parenthesized, call) — don't stringify.
    return None


def _call_target_name(node: Any, source: bytes) -> str | None:
    """Best-effort name for a call target."""
    if node.type.endswith("identifier"):
        return _short_name(node.text.decode("utf-8", errors="ignore") if node.text else None)
    if node.type == "member_expression":
        obj = node.child_by_field_name("object")
        prop = node.child_by_field_name("property")
        prop_name = prop.text.decode("utf-8", errors="ignore") if prop and prop.text else None
        obj_name = _simple_object_name(obj) if obj else None
        if prop_name:
            return _short_name(f"{obj_name}.{prop_name}" if obj_name else prop_name)
        return _short_name(prop_name)
    # Fallback: first identifier child.
    for child in node.children:
        if child.type.endswith("identifier"):
            return _short_name(child.text.decode("utf-8", errors="ignore") if child.text else None)
    return None


def _python_import_modules(node: Any) -> list[tuple[str, int]]:
    """(module_dotted, relative_dots) per module imported by this statement.

    import_statement: one entry per dotted_name / aliased_import child.
    import_from_statement: exactly one entry from the module_name field —
    imported NAMES are deliberately ignored (they are symbols, not modules).
    """
    def _text(n: Any) -> str:
        return n.text.decode("utf-8", errors="ignore") if n is not None and n.text else ""

    out: list[tuple[str, int]] = []
    if node.type == "import_statement":
        for child in node.children:
            if child.type == "dotted_name":
                if _text(child):
                    out.append((_text(child), 0))
            elif child.type == "aliased_import":
                name = child.child_by_field_name("name")
                if _text(name):
                    out.append((_text(name), 0))
    elif node.type == "import_from_statement":
        module = node.child_by_field_name("module_name")
        if module is None:
            return out
        if module.type == "relative_import":
            dots = 0
            dotted = ""
            for child in module.children:
                if child.type == "import_prefix":
                    dots = len(_text(child))
                elif child.type == "dotted_name":
                    dotted = _text(child)
            if dots:
                out.append((dotted, dots))
        elif module.type == "dotted_name" and _text(module):
            out.append((_text(module), 0))
    return out


def _python_ancestor_packages(
    module: str, dots: int, rel_path: str, source_index: SourceIndex | None
) -> list[str]:
    """Resolved paths of the in-repo packages `import module` also executes (#55).

    `import pkg.sub` imports `pkg` as well: Python executes `pkg/__init__.py` and
    binds the ROOT name in the importer, so `pkg.build()` afterwards is a call into
    the package. Recording only the deepest module made every dotted importer
    invisible as a dependent of that `__init__.py` -- and a package `__init__.py` is
    where re-exports and shared constants live, so `impact` understated the blast
    radius of a high-traffic edit target. Silently: a missing edge cannot lower the
    imports ratio, which is computed over the edges that WERE emitted.

    Scoped to ancestors that resolve IN-REPO. `import os.path` gains nothing from a
    second edge to `os`: it would inflate the external count and move the imports
    ratio for a package that can never be a blast-radius target.

    The counterpart for the other spelling is `_python_from_import_submodules`,
    added for the mirror-image defect in issue #7.
    """
    if source_index is None or "." not in module:
        return []
    segments = module.split(".")
    out: list[str] = []
    for depth in range(1, len(segments)):
        resolved = source_index.resolve_python_module(rel_path, ".".join(segments[:depth]), dots)
        if resolved:
            out.append(resolved)
    return out


def _python_from_import_submodules(
    node: Any, rel_path: str, source_index: SourceIndex | None
) -> list[str]:
    """Resolved submodule paths for `from P import a, b` when a/b are modules.

    Mirrors _collect_python_import_maps' module-first probe (the
    `as_module = source_index.resolve_python_module(...)` check tried
    before the symbol-map fallback, in its import_from_statement branch)
    at the import-EDGE layer: the emission site only ever saw the base
    module, which is how `from aramid import pipeline` bound to the package
    __init__ and hid test files from impact (issue #7).
    """
    if node.type != "import_from_statement" or source_index is None:
        return []
    modules = _python_import_modules(node)
    if not modules:
        return []
    base_module, dots = modules[0]
    module_field = node.child_by_field_name("module_name")

    def _text(n: Any) -> str:
        return n.text.decode("utf-8", errors="ignore") if n is not None and n.text else ""

    out: list[Any] = []
    for child in node.children:
        if module_field is not None and child.id == module_field.id:
            # Identity-skip the module_name's own dotted_name (paren-safe;
            # see _collect_python_import_maps for the sibling-token trap).
            continue
        original = None
        if child.type == "dotted_name":
            original = _text(child)
        elif child.type == "aliased_import":
            original = _text(child.child_by_field_name("name"))
        if not original or "." in original:
            continue
        sub = f"{base_module}.{original}" if base_module else original
        resolved = source_index.resolve_python_module(rel_path, sub, dots)
        if resolved:
            out.append(resolved)
    return out


def _collect_python_import_maps(
    root: Any, rel_path: str, source_index: SourceIndex | None
) -> tuple[dict[str, str], dict[str, str], frozenset[str]]:
    """(symbol_map, alias_map, external_names).

    symbol_map: local -> definition node id. alias_map: local -> module file id.
    external_names: local names bound by imports that did NOT resolve in-repo --
    calls through them leave the repo (EXTERNAL_CALL).

    Walked at ALL depths (Python allows function-local imports). For
    `from P import name`, `P.name` is tried as a MODULE first (alias), then
    as a symbol defined in P's file. Unresolvable modules enter neither map,
    but DO enter external_names.
    Last binding wins, matching Python shadowing.
    """
    symbol_map: dict[str, str] = {}
    alias_map: dict[str, str] = {}
    external: set[str] = set()
    if source_index is None:
        return symbol_map, alias_map, frozenset()

    def _text(n: Any) -> str:
        return n.text.decode("utf-8", errors="ignore") if n is not None and n.text else ""

    def visit(node: Any) -> None:
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    module = _text(child)
                    if module and "." not in module:
                        resolved = source_index.resolve_python_module(rel_path, module)
                        if resolved:
                            alias_map[module] = _file_node_id(resolved)
                        else:
                            external.add(module)
                    elif module:
                        # `import pkg.sub` binds only the root name `pkg`.
                        # Mark it external ONLY if that root does not
                        # resolve in-repo -- a local `pkg` would otherwise
                        # have its calls excluded from the ratio as a false
                        # external (spec §4.2: a false external costs more
                        # than a missed one).
                        root = module.split(".", 1)[0]
                        if source_index.resolve_python_module(rel_path, root) is None:
                            external.add(root)
                elif child.type == "aliased_import":
                    module = _text(child.child_by_field_name("name"))
                    # Optional by declaration: the `from` branch below starts
                    # the name at None and narrows before every use.
                    local: str | None = _text(child.child_by_field_name("alias"))
                    if module and local:
                        resolved = source_index.resolve_python_module(rel_path, module)
                        if resolved:
                            alias_map[local] = _file_node_id(resolved)
                        else:
                            external.add(local)
        elif node.type == "import_from_statement":
            modules = _python_import_modules(node)
            if modules:
                base_module, dots = modules[0]
                module_field = node.child_by_field_name("module_name")
                for child in node.children:
                    if module_field is not None and child.id == module_field.id:
                        # The module_name field's own dotted_name (e.g. `pkg`
                        # in `from pkg import a`) is ALSO a plain child of
                        # this statement. Skip it by identity rather than by
                        # sibling-token sniffing: `prev_sibling in ("import",
                        # ",")` fails for the first name inside parens
                        # (`from x import (a, b)` — `a`'s prev_sibling is
                        # `(`), silently dropping black-style multi-imports.
                        continue
                    local = original = None
                    if child.type == "dotted_name":
                        original = local = _text(child)
                    elif child.type == "aliased_import":
                        original = _text(child.child_by_field_name("name"))
                        local = _text(child.child_by_field_name("alias"))
                    if not original or not local or "." in original:
                        continue
                    sub = f"{base_module}.{original}" if base_module else original
                    as_module = source_index.resolve_python_module(rel_path, sub, dots)
                    if as_module:
                        alias_map[local] = _file_node_id(as_module)
                        continue
                    parent = source_index.resolve_python_module(rel_path, base_module, dots)
                    if parent:
                        symbol_map[local] = _make_id(_file_node_id(parent), original)
                    else:
                        external.add(local)
        for child in node.children:
            visit(child)

    visit(root)
    return symbol_map, alias_map, frozenset(external)


def _python_call_target(func: Any) -> tuple[str | None, str | None, str | None]:
    """(bare_name, object_name, attribute_name) for a Python call's function node."""
    def _text(n: Any) -> str | None:
        return n.text.decode("utf-8", errors="ignore") if n is not None and n.text else None

    if func.type == "identifier":
        return _text(func), None, None
    if func.type == "attribute":
        obj = func.child_by_field_name("object")
        attr = _text(func.child_by_field_name("attribute"))
        obj_name = _text(obj) if obj is not None and obj.type == "identifier" else None
        return None, obj_name, attr
    return None, None, None


def _python_attribute_root(node: Any) -> str | None:
    """Leftmost identifier of a (possibly nested) attribute chain.

    `os.path.join` parses as `attribute(attribute(identifier(os), path), join)`
    -- `_python_call_target` only looks at the immediate object, so for a
    depth->=2 chain `obj_name` comes back None and the import-bound root
    (`os`) is invisible to `_call_confidence`. Walked here purely to recover
    that root for classification; the `dotted` string used for targeting,
    dispatch (`_member`), and `should_keep_call_target` noise-filtering is
    untouched by this -- changing that shape trips the noise filter on leaf
    names like `join` for reasons unrelated to externality.
    """
    while node is not None and node.type == "attribute":
        node = node.child_by_field_name("object")
    if node is not None and node.type == "identifier" and node.text:
        return node.text.decode("utf-8", errors="ignore")
    return None


def _extract_python(file_id: str, rel_path: str, _source: bytes, tree: Any, source_index: SourceIndex | None = None) -> ExtractionResult:
    result = ExtractionResult()
    root = tree.root_node

    def _line(node: Any) -> int:
        return (node.start_point[0] + 1) if node.start_point else 1

    result.nodes.append(_node(file_id, "file", Path(rel_path).name, rel_path))

    symbol_map, alias_map, external_names = _collect_python_import_maps(root, rel_path, source_index)

    class_ids: set[str] = set()

    # ``parent_id`` is the nearest named container (for ``contains`` edges);
    # ``scope_id`` is the nearest enclosing function (for ``calls`` attribution).
    def walk(node: Any, parent_id: str | None, scope_id: str) -> None:
        if node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            name = _short_name(name_node.text.decode("utf-8", errors="ignore")) if name_node and name_node.text else None
            if name:
                mid = _make_id(file_id, name)
                extra = {"is_method": True} if parent_id in class_ids else None
                result.nodes.append(_node(mid, "function", name, rel_path, _line(node), extra))
                if parent_id:
                    result.edges.append(_edge(parent_id, mid, "contains", rel_path, _line(node)))
                walk_children(node, mid, mid)
            else:
                walk_children(node, parent_id, scope_id)
        elif node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            name = _short_name(name_node.text.decode("utf-8", errors="ignore")) if name_node and name_node.text else None
            if name:
                cid = _make_id(file_id, name)
                class_ids.add(cid)
                result.nodes.append(_node(cid, "class", name, rel_path, _line(node)))
                if parent_id:
                    result.edges.append(_edge(parent_id, cid, "contains", rel_path, _line(node)))
                # Inheritance
                for base in node.children:
                    if base.type == "argument_list":
                        for arg in base.children:
                            if arg.type.endswith("identifier") and arg.text:
                                base_name = arg.text.decode("utf-8", errors="ignore")
                                result.edges.append(_edge(cid, _make_id(base_name), "inherits", rel_path, _line(arg)))
                walk_children(node, cid, scope_id)
            else:
                walk_children(node, parent_id, scope_id)
        elif node.type in ("import_statement", "import_from_statement"):
            for module, dots in _python_import_modules(node):
                resolved = (
                    source_index.resolve_python_module(rel_path, module, dots)
                    if source_index is not None
                    else None
                )
                if resolved:
                    result.edges.append(_edge(
                        file_id, _file_node_id(resolved), "imports", rel_path,
                        _line(node), confidence="EXACT_IMPORT",
                    ))
                    # `import pkg.sub` also imports `pkg` (#55). Deliberately NOT
                    # applied to `from pkg.sub import x`: that executes the package
                    # too, but binds only `x`, so there is no `pkg.` call to
                    # attribute -- and `test_from_import_symbol_only_edge_unchanged`
                    # pins one edge per module for that spelling.
                    for ancestor in (
                        _python_ancestor_packages(module, dots, rel_path, source_index)
                        if node.type == "import_statement" else ()
                    ):
                        result.edges.append(_edge(
                            file_id, _file_node_id(ancestor), "imports", rel_path,
                            _line(node), confidence="EXACT_IMPORT",
                        ))
                else:
                    result.edges.append(_edge(
                        file_id, _make_id(module) if module else _make_id("package"),
                        "imports", rel_path, _line(node), confidence="EXTERNAL_IMPORT",
                    ))
            for sub in _python_from_import_submodules(node, rel_path, source_index):
                result.edges.append(_edge(
                    file_id, _file_node_id(sub), "imports", rel_path,
                    _line(node), confidence="EXACT_IMPORT",
                ))
            walk_children(node, parent_id, scope_id)
        elif node.type == "call":
            func = node.child_by_field_name("function")
            bare, obj_name, attr = _python_call_target(func) if func is not None else (None, None, None)
            edge = None
            if bare and bare not in _LANGUAGE_BUILTIN_GLOBALS:
                target = symbol_map.get(bare) or _resolve_call(file_id, bare)
                edge = _edge(
                    scope_id, target, "calls", rel_path, _line(node),
                    confidence=_call_confidence(bare, external_names, symbol_map),
                )
            elif attr:
                dotted = f"{obj_name}.{attr}" if obj_name else attr
                if obj_name and obj_name in alias_map:
                    edge = _edge(scope_id, _make_id(alias_map[obj_name], attr), "calls", rel_path, _line(node), confidence="LOCAL_CALL")
                elif should_keep_call_target(dotted):
                    # Unresolved member call: file-scoped phantom now, re-pointed
                    # (or dropped) by the method-dispatch post-pass via _member.
                    # Confidence is classified off the recovered chain root
                    # (falls back to `dotted` itself for a non-attribute
                    # receiver, e.g. `foo().bar()`), not off `dotted` -- a
                    # depth->=2 chain like `os.path.join` would otherwise
                    # test "join" instead of the bound name "os".
                    # When neither a simple receiver nor a chain root can be
                    # recovered, `dotted` is the bare attribute name and carries
                    # no information about the receiver -- classifying it would
                    # be a false external (#14).
                    recovered_root = obj_name or _python_attribute_root(func)
                    root_name = recovered_root or dotted
                    edge = _edge(
                        scope_id, _resolve_call(file_id, dotted), "calls", rel_path, _line(node),
                        confidence=_call_confidence(
                            root_name, external_names, symbol_map,
                            attributable=recovered_root is not None,
                        ),
                    )
                    edge["_member"] = attr
            if edge is not None:
                result.edges.append(edge)
            walk_children(node, parent_id, scope_id)
        else:
            walk_children(node, parent_id, scope_id)

    def walk_children(node: Any, parent_id: str | None, scope_id: str) -> None:
        for child in node.children:
            walk(child, parent_id, scope_id)

    walk(root, file_id, file_id)
    return result


def _extract_go(file_id: str, rel_path: str, source: bytes, tree: Any) -> ExtractionResult:
    """Heuristic Go extraction: functions, methods, types, imports, calls.

    Same fidelity tier as the Python path — no package-level import resolution.
    `recv.Method()` selector calls carry `_member` so the global method-dispatch
    post-pass links them to `method_declaration` definitions (tagged is_method);
    selector calls that resolve to no known method (fmt.Println, http.Get, ...)
    are dropped by the phantom filter. Known limitation: cross-file calls to
    plain package FUNCTIONS via selector (`utils.Helper()`) resolve only when
    the name matches a method; the file-level import edge still records the
    package dependency.
    """
    result = ExtractionResult()
    root = tree.root_node

    def _line(node: Any) -> int:
        return (node.start_point[0] + 1) if node.start_point else 1

    def _text(node: Any) -> str | None:
        return node.text.decode("utf-8", errors="ignore") if node is not None and node.text else None

    result.nodes.append(_node(file_id, "file", Path(rel_path).name, rel_path))

    def _emit_import(spec: Any) -> None:
        path_node = spec.child_by_field_name("path")
        mod = (_text(path_node) or "").strip("'\"`")
        if mod:
            # No resolver for Go imports (no package-level resolution, see
            # docstring above) — default EXTRACTED confidence, not
            # EXTERNAL_IMPORT. EXTERNAL_IMPORT is reserved for genuinely
            # external/stdlib modules a resolver *tried and failed* to
            # resolve (health schema 2 excludes it from imports ratios);
            # tagging every Go import that way would hide 100% of this
            # language's phantom cross-file linkage from resolution_health.
            result.edges.append(_edge(file_id, _make_id(mod), "imports", rel_path, _line(spec)))

    def walk(node: Any, parent_id: str | None, scope_id: str) -> None:
        t = node.type
        if t in ("function_declaration", "method_declaration"):
            name = _short_name(_text(node.child_by_field_name("name")))
            if name:
                mid = _make_id(file_id, name)
                extra = {"is_method": True} if t == "method_declaration" else None
                result.nodes.append(_node(mid, "function", name, rel_path, _line(node), extra=extra))
                if parent_id:
                    result.edges.append(_edge(parent_id, mid, "contains", rel_path, _line(node)))
                walk_children(node, mid, mid)
            else:
                walk_children(node, parent_id, scope_id)
        elif t == "type_declaration":
            for spec in node.children:
                if spec.type == "type_spec":
                    name = _short_name(_text(spec.child_by_field_name("name")))
                    if name:
                        tid = _make_id(file_id, name)
                        result.nodes.append(_node(tid, "class", name, rel_path, _line(spec)))
                        if parent_id:
                            result.edges.append(_edge(parent_id, tid, "contains", rel_path, _line(spec)))
            walk_children(node, parent_id, scope_id)
        elif t == "import_declaration":
            for child in node.children:
                if child.type == "import_spec":
                    _emit_import(child)
                elif child.type == "import_spec_list":
                    for spec in child.children:
                        if spec.type == "import_spec":
                            _emit_import(spec)
        elif t == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None:
                member: str | None = None
                if func.type == "selector_expression":
                    operand = _text(func.child_by_field_name("operand"))
                    field = _text(func.child_by_field_name("field"))
                    called = _short_name(f"{operand}.{field}" if operand and field else field)
                    member = field
                else:
                    called = _call_target_name(func, source)
                if called and called not in _LANGUAGE_BUILTIN_GLOBALS and should_keep_call_target(called):
                    edge = _edge(scope_id, _resolve_call(file_id, called), "calls", rel_path, _line(node), confidence=_call_confidence(called))
                    if member:
                        edge["_member"] = member
                    result.edges.append(edge)
            walk_children(node, parent_id, scope_id)
        else:
            walk_children(node, parent_id, scope_id)

    def walk_children(node: Any, parent_id: str | None, scope_id: str) -> None:
        for child in node.children:
            walk(child, parent_id, scope_id)

    walk(root, file_id, file_id)
    return result


def _extract_rust(
    file_id: str,
    rel_path: str,
    source: bytes,
    tree: Any,
    source_index: SourceIndex | None = None,
) -> ExtractionResult:
    """Heuristic Rust extraction: fns, impl methods, types, use decls, calls.

    `x.method()` (field_expression) and `Type::assoc()` (scoped_identifier)
    calls carry `_member` so the method-dispatch post-pass links them to impl
    functions (tagged is_method); unresolved ones (.clone(), String::from, ...)
    are dropped by the phantom filter. Macro invocations (println!, vec!) are a
    different node type and are never extracted as calls.
    """
    result = ExtractionResult()
    root = tree.root_node

    def _line(node: Any) -> int:
        return (node.start_point[0] + 1) if node.start_point else 1

    def _text(node: Any) -> str | None:
        return node.text.decode("utf-8", errors="ignore") if node is not None and node.text else None

    result.nodes.append(_node(file_id, "file", Path(rel_path).name, rel_path))

    def _use_target(node: Any) -> str | None:
        # `use a::b::{c, d};` -> record the common prefix `a::b`; `use x as y` -> `x`.
        raw = _text(node.child_by_field_name("argument"))
        if not raw:
            return None
        raw = raw.split("{")[0].split(" as ")[0].strip().rstrip(":").strip()
        return raw or None

    def _inline_mod_depth(node: Any) -> int:
        """How many inline `mod { ... }` blocks enclose this node.

        `super::` inside an inline module still refers to *this* file, at any
        nesting depth, so a `#[cfg(test)] mod tests { use super::X; }` must not
        be resolved against the parent directory. Read from the ancestor chain
        rather than threaded through walk(), which would touch every recursive
        call site for one rarely-needed number.
        """
        depth = 0
        current = node.parent
        while current is not None:
            if current.type == "mod_item" and current.child_by_field_name("body") is not None:
                depth += 1
            current = current.parent
        return depth

    def walk(node: Any, parent_id: str | None, scope_id: str, in_impl: bool) -> None:
        t = node.type
        if t == "function_item":
            name = _short_name(_text(node.child_by_field_name("name")))
            if name:
                mid = _make_id(file_id, name)
                extra = {"is_method": True} if in_impl else None
                result.nodes.append(_node(mid, "function", name, rel_path, _line(node), extra=extra))
                if parent_id:
                    result.edges.append(_edge(parent_id, mid, "contains", rel_path, _line(node)))
                walk_children(node, mid, mid, in_impl)
            else:
                walk_children(node, parent_id, scope_id, in_impl)
        elif t in ("struct_item", "enum_item", "trait_item"):
            name = _short_name(_text(node.child_by_field_name("name")))
            if name:
                cid = _make_id(file_id, name)
                result.nodes.append(_node(cid, "class", name, rel_path, _line(node)))
                if parent_id:
                    result.edges.append(_edge(parent_id, cid, "contains", rel_path, _line(node)))
                walk_children(node, cid, scope_id, in_impl)
            else:
                walk_children(node, parent_id, scope_id, in_impl)
        elif t == "impl_item":
            impl_type = _text(node.child_by_field_name("type"))
            impl_parent = _make_id(file_id, impl_type.split("<")[0]) if impl_type else parent_id
            walk_children(node, impl_parent, scope_id, True)
        elif t == "mod_item":
            body = node.child_by_field_name("body")
            if body is None:
                # `mod foo;` -- a file module. This is Rust's file-inclusion
                # mechanism and the only structural link between the files of a
                # multi-file crate, so without it such a crate has no
                # module-structure edges at all.
                mod_name = _short_name(_text(node.child_by_field_name("name")))
                if mod_name and source_index is not None:
                    resolved = source_index.resolve_rust_mod(rel_path, mod_name)
                    if resolved is not None and resolved != rel_path:
                        result.edges.append(
                            _edge(
                                file_id,
                                _file_node_id(resolved),
                                "imports",
                                rel_path,
                                _line(node),
                            )
                        )
            else:
                # Inline `mod foo { ... }` -- same file, no edge. Nesting depth
                # is read from the ancestor chain by _inline_mod_depth.
                walk_children(node, parent_id, scope_id, in_impl)
        elif t == "use_declaration":
            target = _use_target(node)
            if target:
                resolution = (
                    source_index.resolve_rust_use(rel_path, target, _inline_mod_depth(node))
                    if source_index is not None
                    else None
                )
                if resolution is not None and resolution.rel_path is not None:
                    # _file_node_id, NOT _make_id: the target must be the id the
                    # file node was created under, or the edge stays unbound.
                    if resolution.rel_path != rel_path:
                        # A file importing itself carries no dependency.
                        result.edges.append(
                            _edge(
                                file_id,
                                _file_node_id(resolution.rel_path),
                                "imports",
                                rel_path,
                                _line(node),
                            )
                        )
                elif resolution is not None and resolution.external:
                    # Confirmed external (allowlisted root or declared Cargo
                    # dependency) -- excluded from the imports ratio.
                    result.edges.append(
                        _edge(
                            file_id,
                            _make_id(target),
                            "imports",
                            rel_path,
                            _line(node),
                            confidence="EXTERNAL_IMPORT",
                        )
                    )
                else:
                    # Unresolved, or no index at all: default confidence, so the
                    # miss stays visible to resolution_health rather than being
                    # laundered as external.
                    result.edges.append(
                        _edge(file_id, _make_id(target), "imports", rel_path, _line(node))
                    )
        elif t == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None:
                called: str | None
                member: str | None = None
                # False when `called` degrades to the bare method name because the
                # receiver could not be named -- `build().format()`, `(a + b).test()`.
                # Such a name says nothing about where the call goes, so classifying
                # it against _EXTERNAL_GLOBALS produces a false external (#14
                # mechanism A, threaded for Python at :1414 and TS/JS at :474 and
                # missed here until #56).
                attributable = True
                if func.type == "field_expression":
                    value = _simple_rust_value(func.child_by_field_name("value"), _text)
                    field = _text(func.child_by_field_name("field"))
                    attributable = bool(value and field)
                    called = _short_name(f"{value}.{field}" if value and field else field)
                    member = field
                elif func.type == "scoped_identifier":
                    name = _text(func.child_by_field_name("name"))
                    called = _short_name((_text(func) or "").replace("::", "."))
                    member = name
                elif func.type == "generic_function":
                    inner = func.child_by_field_name("function")
                    called = _short_name((_text(inner) or "").replace("::", "."))
                    member = _text(inner.child_by_field_name("name")) if inner is not None and inner.type == "scoped_identifier" else None
                else:
                    called = _call_target_name(func, source)
                if called and called not in _LANGUAGE_BUILTIN_GLOBALS and should_keep_call_target(called):
                    edge = _edge(
                        scope_id, _resolve_call(file_id, called), "calls", rel_path, _line(node),
                        confidence=_call_confidence(called, attributable=attributable),
                    )
                    if member:
                        edge["_member"] = member
                    result.edges.append(edge)
            walk_children(node, parent_id, scope_id, in_impl)
        else:
            walk_children(node, parent_id, scope_id, in_impl)

    def walk_children(node: Any, parent_id: str | None, scope_id: str, in_impl: bool) -> None:
        for child in node.children:
            walk(child, parent_id, scope_id, in_impl)

    walk(root, file_id, file_id, False)
    return result


def _simple_rust_value(node: Any, _text: Any) -> str | None:
    """Short receiver name for a Rust field_expression value, or None if complex."""
    if node is None:
        return None
    if node.type in ("identifier", "self"):
        return _text(node)
    return None


def _resolve_import(rel_path: str, source_lit: str, source_index: SourceIndex | None = None) -> Any | None:
    """Best-effort resolve relative/local imports to a known project file path."""
    if source_index is not None:
        return source_index.resolve_ts_import_detail(rel_path, source_lit)
    return None


def _resolve_call(
    file_id: str,
    called: str,
    import_symbols: dict[str, str] | None = None,
    namespaces: dict[str, str] | None = None,
) -> str:
    """Convert a call target string into a node id.

    A plain identifier that was imported into this file resolves to the
    definition node in the file that exports it (cross-file). A single-segment
    member call whose receiver is a whole-module binding -- `import * as ns` or
    `const m = require('./x')` -- resolves to that export in the module's file.
    Otherwise the call is attached to the current file's namespace (same-file
    resolution).
    """
    if import_symbols and called and "." not in called:
        mapped = import_symbols.get(called)
        if mapped:
            return mapped
    if namespaces and called and called.count(".") == 1:
        receiver, member = called.split(".")
        module_file_id = namespaces.get(receiver)
        if module_file_id:
            return _make_id(module_file_id, member)
    # Local call: if called starts with lowercase or is relative, attach to file namespace.
    if called and not called.startswith(".") and not called.startswith("node_modules"):
        # Try namespaced under file first; fallback to bare id.
        return _make_id(file_id, called)
    return _make_id(called)


def _extract_generic(file_id: str, rel_path: str, source: bytes, language: str) -> ExtractionResult:
    """Fallback for languages we can't parse structurally yet."""
    result = ExtractionResult()
    result.nodes.append(_node(file_id, "file", Path(rel_path).name, rel_path))
    # TODO: add comment extraction, markdown headings, JSON keys, etc.
    return result


def extract_file(entry: FileEntry, cfg: Config, cache: Cache | None = None, source_index: SourceIndex | None = None) -> ExtractionResult:
    """Extract nodes and edges from a single file, using cache if available."""
    compiler_resolver_active = bool(
        source_index
        and source_index.typescript.available
        and entry.language in ("javascript", "typescript", "tsx", "jsx")
    )
    cache_language = f"{entry.language or 'unknown'}:{'tsc' if compiler_resolver_active else 'ast'}"
    if source_index is not None and entry.language in _RESOLVER_LANGUAGES:
        # Import resolution happens at extraction time, so a cached result
        # embeds which sibling modules existed when it was written. Keying on
        # content_hash alone let an unchanged importer keep stale EXACT_IMPORT /
        # EXTERNAL_IMPORT edges after a sibling was added or removed (#2).
        # Only resolver-consulting languages pay this invalidation: Go and Rust
        # extraction is file-local and cannot go stale this way.
        cache_language = f"{cache_language}:fs{source_index.file_set_digest()}"
    if cache is not None and not compiler_resolver_active:
        cached = cache.read("ast", entry.content_hash, cache_language)
        if cached is not None:
            return _result_from_dict(cached)

    rel_path = entry.rel_path
    file_id = _file_node_id(rel_path)

    try:
        with open(entry.abs_path, "rb") as f:
            source = f.read()
    except Exception as e:
        return ExtractionResult(error=f"read_error: {e}")

    if entry.language in ("javascript", "typescript", "tsx", "jsx"):
        parser = _LOADER.parser(entry.language)
        if parser is None:
            return _extract_generic(file_id, rel_path, source, entry.language)
        try:
            tree = parser.parse(source)
        except Exception as e:
            return ExtractionResult(error=f"parse_error: {e}")
        result = _extract_ts_js(file_id, rel_path, source, tree, source_index)
    elif entry.language in ("python", "go", "rust"):
        parser = _LOADER.parser(entry.language)
        if parser is None:
            return _extract_generic(file_id, rel_path, source, entry.language)
        try:
            tree = parser.parse(source)
        except Exception as e:
            return ExtractionResult(error=f"parse_error: {e}")
        if entry.language == "python":
            result = _extract_python(file_id, rel_path, source, tree, source_index)
        elif entry.language == "rust":
            result = _extract_rust(file_id, rel_path, source, tree, source_index)
        else:
            result = _extract_go(file_id, rel_path, source, tree)
    else:
        result = _extract_generic(file_id, rel_path, source, entry.language or "unknown")

    if cache is not None:
        cache.write(_result_to_dict(result), "ast", entry.content_hash, cache_language)

    return result


def _result_to_dict(result: ExtractionResult) -> dict[str, Any]:
    return {"nodes": result.nodes, "edges": result.edges, "error": result.error}


def _result_from_dict(data: dict[str, Any]) -> ExtractionResult:
    return ExtractionResult(
        nodes=data.get("nodes", []),
        edges=data.get("edges", []),
        error=data.get("error"),
    )


def _error_record(rel_path: str, error: str) -> dict[str, Any]:
    code, _, _rest = error.partition(":")
    return {"code": code.strip() or "extract_error", "subject": rel_path, "detail": error}


def extract_all(entries: list[FileEntry], cfg: Config, cache: Cache | None = None) -> ExtractionResult:
    """Extract all files, optionally in parallel."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: list[ExtractionResult] = []
    error_records: list[dict[str, Any]] = []
    source_index = SourceIndex.from_entries(entries, cfg)
    if cfg.workers <= 1:
        for entry in entries:
            result = extract_file(entry, cfg, cache, source_index)
            if result.error:
                error_records.append(_error_record(entry.rel_path, result.error))
            results.append(result)
        merged = _merge(results)
        merged.errors = sorted(error_records, key=lambda r: r["subject"])
        return merged

    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futures = {pool.submit(extract_file, entry, cfg, cache, source_index): entry for entry in entries}
        for future in as_completed(futures):
            entry = futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = ExtractionResult(error=f"worker_error: {entry.rel_path}: {e}", nodes=[], edges=[])
            if result.error:
                error_records.append(_error_record(entry.rel_path, result.error))
            results.append(result)
    merged = _merge(results)
    merged.errors = sorted(error_records, key=lambda r: r["subject"])
    return merged


# Cap on how many same-named class methods one `recv.method()` call may link to.
# Method names in real codebases are almost always globally unique, so the common
# path is a 1:1 re-point. A small ambiguous set (2..cap) links to every candidate
# (bounded fan-out that keeps reachability useful). Above the cap the name is too
# generic to guess, so resolution is skipped and the original file-scoped phantom
# edge is left untouched (pre-fix behavior).
_MAX_METHOD_DISPATCH_CANDIDATES = 3

# Languages where the reachability gate has been MEASURED. Deliberately just
# Python -- see `_dispatch_is_gated` for why this is not simply "every language
# whose imports bind to a file node".
_DISPATCH_GATED_LANGUAGES = frozenset({"python"})

# Languages that share one runtime and can therefore hold one another's objects.
# Dispatch may cross freely INSIDE a family and never BETWEEN two of them.
#
# Only entries that MERGE distinct `LANGUAGE_BY_EXT` labels belong here; anything
# absent is its own family (see `_interop_family`), so a new extractor is isolated
# by default rather than silently pooled with an existing ecosystem.
_INTEROP_FAMILY_BY_LANGUAGE: Final = {
    "javascript": "ecmascript",
    "typescript": "ecmascript",
    "jsx": "ecmascript",
    "tsx": "ecmascript",
}


def _interop_family(source_file: str | None) -> str | None:
    """Which call-compatible ecosystem this file belongs to, or None if unknown.

    An EXACT invariant, unlike the reachability proxy in `_dispatch_is_gated`:
    graphite models no FFI, so a JavaScript call site cannot reach a Python `def`
    under any typing discipline. That is why this gate needs no per-language
    measurement before being applied everywhere -- it removes only bindings that
    are impossible, never ones that are merely unlikely.

    Scoped to a FAMILY rather than to the raw language label because
    `LANGUAGE_BY_EXT` maps `.ts -> typescript` and `.tsx -> tsx`: a plain label
    comparison would delete every call from a `.ts` module into a `.tsx`
    component, which is ordinary code in every React repo graphite is pointed at.

    Resolved through `LANGUAGE_BY_EXT` for the same reason `_dispatch_is_gated` is
    -- a private suffix list would agree with the extractor only by coincidence.

    FAILS OPEN on `None`: a file whose extension the extractor does not classify
    has no family, and dispatch from or to it is left exactly as it was.
    """
    if not source_file:
        return None
    language = LANGUAGE_BY_EXT.get(Path(source_file).suffix.casefold())
    if language is None:
        return None
    return _INTEROP_FAMILY_BY_LANGUAGE.get(language, language)


def _dispatch_is_gated(source_file: str | None) -> bool:
    """Whether method dispatch from this file is restricted to reachable definitions.

    ONE OF THREE FILTERS, and the only optional one. Do not read "not gated here"
    as "dispatch from this file is unconstrained": the evidence gate (never
    re-point a proven-EXTERNAL_CALL) and the interop-family gate (never re-point
    across `_interop_family`) both apply in EVERY language, gated or not. They are
    exact -- an externality proof and a "no FFI is modelled" invariant -- so they
    needed no per-language measurement. This one is a proxy, which is why it does.

    Resolved through `LANGUAGE_BY_EXT` -- the same table `collect_files` uses to pick
    an extractor -- so the gate cannot disagree with the walk that produced the edge.
    A private suffix list here would agree with that table only by coincidence: `.py`
    happens to be the sole extension mapping to `python` today, and a `.pyw` or `.pyi`
    entry added there would silently escape a hardcoded list while still being
    extracted as Python.

    FAILS OPEN, and the allowlist is narrower than the mechanism can support.

    The gate rests on a proxy: "the caller does not import the definer" standing in
    for "the caller cannot be holding one of those". That proxy is never exact --
    every language here is duck-typed or structurally typed, so a function can be
    handed an instance of a class it never mentions. It is admitted for Python
    because it was MEASURED there (#54): on this repo it removed 1400+ edges whose
    every sampled member was a false binding, and added back ~350 real ones that the
    ambiguity cap had been abandoning.

    TypeScript/JavaScript are NOT gated BY THIS FILTER, on evidence rather than
    caution. `test_member_call_ambiguous_small_set_links_to_all` pins the documented
    small-ambiguous-set fan-out using `x: any` and no import at all -- a shape that
    is idiomatic in TS and that this gate would delete. Rust is ungated for the
    duller reason that this repo contains none, so nothing here could measure it.
    Extending the allowlist is a per-language measurement, not a one-line edit.
    (The two exact filters above still constrain TS/JS dispatch -- #56 removed a
    `.mjs` call bound to a Python `def` without any help from this gate.)

    Go could not be gated even with evidence: its in-repo imports target a
    synthesized PACKAGE id (`example_com_repo_store`) that never equals the file
    node id of any file in that package, and files within one package see each
    other with no import statement at all.
    """
    if not source_file:
        return False
    language = LANGUAGE_BY_EXT.get(Path(source_file).suffix.casefold())
    return language in _DISPATCH_GATED_LANGUAGES


def _resolve_method_dispatch(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Re-point `recv.method(...)` member-call edges to class method definitions.

    NAME-BASED heuristic (full TS type inference is out of scope). Each member call
    carries the bare method name in ``_member`` (set during the TS/JS walk); the
    call target as extracted is only a file-scoped phantom (``<file>_<recv>_<method>``).
    Here we map the method name to every class-method definition node (tagged
    ``is_method``) sharing that name and re-point the edge accordingly, which is what
    makes ``store.X()`` / ``cj.X()`` / ``this.X()`` dispatch visible to callers/calls/
    reaches, including across files.

    Ambiguity policy (``_MAX_METHOD_DISPATCH_CANDIDATES``): a unique name re-points to
    the single definition; 2..cap candidates each get an edge; more than cap (or zero)
    candidates leaves the original phantom edge unchanged. Arrow-valued class fields
    (``foo = () => {}``) parse as ``public_field_definition`` rather than
    ``method_definition``; they are tagged ``is_method`` at extraction so they are
    indexed here on equal terms (#19).

    ``_member`` is stripped from every returned edge so it never leaks into the graph.
    """
    # method name (casefolded) -> set of definition node ids. A set collapses the
    # same method appearing twice in the pre-dedup node list, so the cap counts
    # distinct definitions.
    methods_by_name: dict[str, set[str]] = {}
    for n in nodes:
        if n.get("is_method") and n.get("name"):
            methods_by_name.setdefault(n["name"].casefold(), set()).add(n["id"])
    known_ids = {n["id"] for n in nodes}

    # Which file each definition lives in, and which files each file imports
    # DIRECTLY. Both are needed to answer "could this caller have reached that
    # definition at all" -- see the reachability filter below (#54).
    file_of_node: dict[str, str] = {
        n["id"]: _file_node_id(n["source_file"]) for n in nodes if n.get("source_file")
    }
    # Which interop family each definition belongs to, for the family gate below.
    family_of_node: dict[str, str | None] = {
        n["id"]: _interop_family(n["source_file"]) for n in nodes if n.get("source_file")
    }
    imports_by_file: dict[str, set[str]] = {}
    for e in edges:
        if e.get("relation") == "imports" and e.get("source") and e.get("target"):
            imports_by_file.setdefault(e["source"], set()).add(e["target"])

    # This gate reads import edges literally, which is only sound while they are
    # complete. It once carried its own ancestor-package expansion, because
    # `import pkg.sub` emitted no edge to `pkg/__init__.py` and the gate would
    # otherwise refuse `pkg.build()` -- a call on the very name that statement
    # binds. #55 emits that edge, so the compensation became unfalsifiable (removing
    # it changed no test) and was deleted rather than left looking like protection.
    # `test_a_dotted_import_reaches_the_package_it_binds` is what now guards the
    # coupling: it fails if those edges ever stop being emitted.

    out: list[dict[str, Any]] = []
    for e in edges:
        method = e.pop("_member", None)  # strip from every edge, resolved or not
        if not method or e.get("relation") != "calls":
            out.append(e)
            continue
        # Evidence gate (#56), applied BEFORE the name lookup and in every language.
        # `_call_confidence` tags EXTERNAL_CALL only when the call PROVABLY leaves
        # the repo: an attributable receiver root bound by an import that did not
        # resolve in-repo, or an `_EXTERNAL_GLOBALS` name no in-repo binding shadows.
        # An unnameable receiver returns LOCAL_CALL precisely so a bare method name
        # can never be classified external (#14 mechanism A). Re-pointing such an
        # edge by bare method name overrules evidence with a guess -- and did, 52
        # times on graphite's own graph: `os.close(fd)` bound to an in-repo `close`,
        # `sys.stdin.read()` to `cache.py::read`, `os.kill(pid, 0)` to three
        # different in-repo `kill`s, `path.resolve()` in a .mjs file to a `resolve`
        # defined in a PYTHON test.
        #
        # The #54 gate below cannot catch these: the import that makes the
        # definition "reachable" is real and irrelevant, because the receiver is
        # `os`. Externality is evidence that gate never consults.
        if e.get("confidence") == "EXTERNAL_CALL":
            candidates: set[str] | None = None
        else:
            candidates = methods_by_name.get(method.casefold())
        # Interop-family gate (#56). An EXACT invariant rather than a proxy:
        # graphite models no FFI, so a JavaScript call site cannot reach a Python
        # `def` under any typing discipline. Measured live before this existed --
        # `path.resolve(input.root)` in `src/graphite/ts_resolver.mjs` bound to a
        # `resolve` defined in `tests/test_git_security.py`, and reported at
        # decision_grade.
        #
        # Applies in EVERY language, gated or not, because it removes only bindings
        # that are impossible; the per-language census the reachability gate below
        # still waits on is about bindings that are merely unlikely. Fails open on
        # either side: an unclassified extension has no family and is left alone.
        if candidates:
            caller_family = _interop_family(e.get("source_file"))
            if caller_family is not None:
                candidates = {
                    c for c in candidates
                    if family_of_node.get(c) in (None, caller_family)
                }
        # Reachability gate (#54). A name-only match let `Path(...).resolve()` bind
        # to a test double's `resolve`, and `f.write(text)` to a `write` defined in
        # a test file -- 232 and 30 false callers respectively, graded
        # decision_grade, with the mis-bindings counted as `bound` so the health
        # ratio rose as the defect got worse. A definition the caller neither
        # declares nor imports cannot be the receiver's method, whatever its name.
        # Filtering BEFORE the cap is deliberate: reachability is exactly the
        # disambiguation the cap was standing in for, so a common name with one
        # reachable definition now resolves instead of being abandoned as
        # "too generic to guess".
        if candidates and _dispatch_is_gated(e.get("source_file")):
            caller_file = _file_node_id(e["source_file"])
            reachable: set[str] | frozenset[str] = imports_by_file.get(caller_file, frozenset())
            candidates = {
                c for c in candidates
                if file_of_node.get(c) == caller_file or file_of_node.get(c) in reachable
            }
        if not candidates or len(candidates) > _MAX_METHOD_DISPATCH_CANDIDATES:
            # Member call that resolves to no known definition -- or one the
            # evidence gate above refused to resolve. Keep it when the target
            # is a real node, or when the call was classified EXTERNAL_CALL.
            #
            # The EXTERNAL_CALL arm is what makes the evidence gate a REFUSAL TO
            # RE-POINT rather than a deletion: the edge stays, pointing where it
            # already pointed, so health.py can go on excluding it from the
            # resolution denominator. It can only exclude what is present.
            #
            # (This comment used to justify that arm by saying EXTERNAL_CALL "does
            # NOT require an attributable receiver". That was already false when it
            # was written: _call_confidence:139 returns LOCAL_CALL when
            # `attributable` is False, which is #14 mechanism A. The Rust site was
            # the one place the argument still held, and it was threaded in fefe350.)
            #
            # Everything else still DROPS: `c.json()`, `db.prepare()`,
            # `stmt.bind()` -- unattributable receivers whose bare method name
            # does NOT collide with _EXTERNAL_GLOBALS -- are the
            # framework/runtime noise this filter exists to remove.
            if e.get("target") in known_ids or e.get("confidence") == "EXTERNAL_CALL":
                out.append(e)
            continue
        # Unique -> single re-point; small set -> one edge per candidate (sorted
        # for determinism). Duplicates are absorbed by the dedup in _merge.
        for target in sorted(candidates):
            re_pointed = dict(e)
            re_pointed["target"] = target
            out.append(re_pointed)
    return out


def _merge(results: list[ExtractionResult]) -> ExtractionResult:
    merged = ExtractionResult()
    # Collect all nodes/edges, then sort deterministically before dedup.
    all_nodes: list[dict[str, Any]] = []
    all_edges: list[dict[str, Any]] = []
    for r in results:
        all_nodes.extend(r.nodes)
        all_edges.extend(r.edges)
        if r.error:
            merged.error = r.error

    # Resolve `recv.method()` member calls against the global set of class-method
    # definitions. Done here (post-merge, pre-dedup) because it needs every file's
    # methods at once, and because re-pointing can create duplicate edges (e.g.
    # `a.foo()` and `b.foo()` both -> the real `foo`) that the dedup below merges.
    all_edges = _resolve_method_dispatch(all_nodes, all_edges)

    all_nodes.sort(key=lambda n: (n.get("id", ""), n.get("source_file", "")))
    seen_nodes: set[str] = set()
    for n in all_nodes:
        if n["id"] not in seen_nodes:
            merged.nodes.append(n)
            seen_nodes.add(n["id"])

    all_edges.sort(
        key=lambda e: (
            e.get("source", ""),
            e.get("target", ""),
            e.get("relation", ""),
            e.get("source_file", ""),
            e.get("source_location", ""),
        )
    )
    # Duplicate (source, target, relation) triples collapse to one edge, but the
    # duplicate's weight is folded into the survivor rather than discarded — two
    # distinct call sites reaching the same target (e.g. `tdd.auto_resolve_tdd(...)`
    # and an aliased `art(...)` both binding to the same def) is a real multiplicity
    # signal, not noise, and dropping it silently understated call weight for every
    # language before this fix.
    seen_edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in all_edges:
        key = (e["source"], e["target"], e["relation"])
        survivor = seen_edges.get(key)
        if survivor is None:
            seen_edges[key] = e
            merged.edges.append(e)
        else:
            survivor["weight"] = survivor.get("weight", 1.0) + e.get("weight", 1.0)
    return merged







