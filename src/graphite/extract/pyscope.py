"""Python lexical scopes for bare-name call binding (#70, #71).

One pass over a tree-sitter Python tree builds every scope Python creates --
module, function, lambda, class, comprehension -- and records each name each
scope binds. `resolve` then answers what a bare name denotes at a call site the
way CPython does: the innermost scope that binds it, enclosing CLASS scopes
skipped from inside a function or comprehension, `global` and `nonlocal`
honoured.

Node ids are not computed here. A definition is reported by `qualname` and by
whether it sits at module scope; `extract/ast.py` turns that into an id. The
collector records the scope each call node is EVALUATED in, so the extractor
and the collector cannot disagree about where a call sits.

Spec: docs/superpowers/specs/2026-10-04-python-scope-identity-design.md, §4.2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

#: (local name, kind, target) for each name one import statement binds. kind is
#: "symbol", "alias", "external" or "unknown"; see `ast._python_import_bindings`.
ImportBindings = Callable[[Any], list[tuple[str, str, str | None]]]
#: A def/class node's name segment exactly as its graph node records it, or None.
NameOf = Callable[[Any], str | None]

_COMPREHENSIONS = frozenset({
    "list_comprehension", "set_comprehension", "dictionary_comprehension", "generator_expression",
})
#: Node types whose identifiers are all binding targets (`a, (b, *c) = ...`).
_TARGET_CONTAINERS = frozenset({
    "pattern_list", "tuple_pattern", "list_pattern", "tuple", "list",
    "expression_list", "parenthesized_expression", "list_splat_pattern", "as_pattern_target",
})
_SPLATS = frozenset({"list_splat_pattern", "dictionary_splat_pattern", "splat_pattern"})


@dataclass(frozen=True)
class Binder:
    """One thing that binds a name in a scope."""

    kind: str  # "def" | "class" | "import" | "value"
    qualname: str | None = None
    module_level: bool = False
    import_kind: str | None = None
    target: str | None = None
    none_literal: bool = False  # a value binder whose value is the literal `None`


_VALUE = Binder("value")
_NONE_VALUE = Binder("value", none_literal=True)


@dataclass(eq=False)
class PyScope:
    kind: str  # "module" | "function" | "lambda" | "class" | "comprehension"
    parent: PyScope | None
    qualname: str
    binders: dict[str, list[Binder]] = field(default_factory=dict)
    globals: set[str] = field(default_factory=set)
    nonlocals: set[str] = field(default_factory=set)

    def bind(self, name: str, binder: Binder) -> None:
        self.binders.setdefault(name, []).append(binder)

    @property
    def prefix(self) -> str:
        return f"{self.qualname}." if self.qualname else ""


@dataclass(frozen=True)
class Resolution:
    """What a bare name denotes at one call site."""

    kind: str  # "def" | "symbol" | "alias" | "external" | "value" | "none"
    qualname: str | None = None
    module_level: bool = False
    target: str | None = None


@dataclass
class ScopeTable:
    module: PyScope
    call_scope: dict[int, PyScope]
    definitions: dict[int, tuple[str, bool]]

    def scope_of_call(self, call_node: Any) -> PyScope:
        return self.call_scope.get(call_node.id, self.module)

    def module_defines(self, name: str) -> bool:
        """A def or class at module scope binds `name`, so `_make_id(file, name)` is its id."""
        return any(
            b.kind in ("def", "class") and b.module_level for b in self.module.binders.get(name, ())
        )


def _text(node: Any) -> str:
    return node.text.decode("utf-8", errors="ignore") if node is not None and node.text else ""


def _target_names(node: Any) -> Iterator[str]:
    """Every identifier a binding target binds: `a`, `a, (b, *c)`, `[a, b]`."""
    if node is None:
        return
    if node.type == "identifier":
        yield _text(node)
    elif node.type in _TARGET_CONTAINERS:
        for child in node.named_children:
            yield from _target_names(child)


def _parameter_names(params: Any) -> Iterator[str]:
    for child in params.named_children:
        if child.type == "identifier":
            yield _text(child)
        elif child.type in ("default_parameter", "typed_default_parameter"):
            yield from _target_names(child.child_by_field_name("name"))
        elif child.type == "typed_parameter":
            for part in child.named_children:
                if part.type == "identifier":
                    yield _text(part)
                    break
                if part.type in _SPLATS:
                    yield from (_text(c) for c in part.named_children if c.type == "identifier")
                    break
        elif child.type in _SPLATS:
            yield from (_text(c) for c in child.named_children if c.type == "identifier")


def _parameter_expressions(params: Any) -> Iterator[Any]:
    """Defaults and annotations: evaluated where the def or lambda itself is."""
    for child in params.named_children:
        for name in ("value", "type"):
            part = child.child_by_field_name(name)
            if part is not None:
                yield part


def _pattern_captures(node: Any) -> Iterator[str]:
    """Names a `case` pattern captures. `_` captures nothing; a dotted name is a value."""
    kind = node.type
    if kind == "dotted_name":
        parts = node.named_children
        if len(parts) == 1 and parts[0].type == "identifier" and _text(parts[0]) != "_":
            yield _text(parts[0])
        return
    if kind in _SPLATS:
        for child in node.named_children:
            if child.type == "identifier" and _text(child) != "_":
                yield _text(child)
        return
    if kind == "as_pattern":
        # In a case pattern the alias is a bare identifier child with no field name.
        for child in node.named_children:
            if child.type == "identifier":
                if _text(child) != "_":
                    yield _text(child)
            elif child.type == "as_pattern_target":
                yield from _target_names(child)
            else:
                yield from _pattern_captures(child)
        return
    if kind in ("keyword_pattern", "class_pattern"):
        # `P(x=fn)`: the keyword and the class are not captures.
        for child in node.named_children[1:]:
            yield from _pattern_captures(child)
        return
    if kind == "dict_pattern":
        for index, child in enumerate(node.children):
            if node.field_name_for_child(index) != "key" and child.is_named:
                yield from _pattern_captures(child)
        return
    for child in node.named_children:
        yield from _pattern_captures(child)


def collect_scopes(root: Any, import_bindings: ImportBindings, name_of: NameOf) -> ScopeTable:
    """Every scope in one Python file, what each binds, and where each call is evaluated."""
    module = PyScope("module", None, "")
    call_scope: dict[int, PyScope] = {}
    definitions: dict[int, tuple[str, bool]] = {}
    scopes: list[PyScope] = [module]

    def open_scope(kind: str, parent: PyScope, qualname: str) -> PyScope:
        inner = PyScope(kind, parent, qualname)
        scopes.append(inner)
        return inner

    def bind_parameters(params: Any, inner: PyScope, outer: PyScope) -> None:
        for name in _parameter_names(params):
            inner.bind(name, _VALUE)
        for expression in _parameter_expressions(params):
            visit(expression, outer)

    def visit_definition(node: Any, scope: PyScope) -> None:
        raw = _text(node.child_by_field_name("name"))
        segment = name_of(node)
        if not raw or segment is None:
            for child in node.children:
                visit(child, scope)
            return
        is_function = node.type == "function_definition"
        qualname = f"{scope.prefix}{segment}"
        module_level = scope.kind == "module"
        definitions[node.id] = (qualname, module_level)
        scope.bind(raw, Binder("def" if is_function else "class", qualname=qualname, module_level=module_level))
        inner = open_scope("function" if is_function else "class", scope, qualname)
        for index, child in enumerate(node.children):
            field_name = node.field_name_for_child(index)
            if field_name == "body":
                visit(child, inner)
            elif field_name == "parameters":
                bind_parameters(child, inner, scope)
            elif field_name != "name":
                # bases, the return annotation, type parameters: evaluated in the
                # enclosing scope (decorators sit outside this node already)
                visit(child, scope)

    def visit_lambda(node: Any, scope: PyScope) -> None:
        inner = open_scope("lambda", scope, scope.qualname)
        for index, child in enumerate(node.children):
            field_name = node.field_name_for_child(index)
            if field_name == "parameters":
                bind_parameters(child, inner, scope)
            elif field_name == "body":
                visit(child, inner)

    def visit_comprehension(node: Any, scope: PyScope) -> None:
        inner = open_scope("comprehension", scope, scope.qualname)
        first = True
        for child in node.children:
            if child.type != "for_in_clause":
                visit(child, inner)
                continue
            for name in _target_names(child.child_by_field_name("left")):
                inner.bind(name, _VALUE)
            for index, part in enumerate(child.children):
                field_name = child.field_name_for_child(index)
                if field_name == "left":
                    continue
                # The FIRST iterable is evaluated in the enclosing scope, so a
                # class-body comprehension can iterate a class-level name.
                visit(part, scope if first and field_name == "right" else inner)
            first = False

    def visit(node: Any, scope: PyScope) -> None:
        kind = node.type
        if kind in ("function_definition", "class_definition"):
            visit_definition(node, scope)
            return
        if kind == "lambda":
            visit_lambda(node, scope)
            return
        if kind in _COMPREHENSIONS:
            visit_comprehension(node, scope)
            return
        if kind == "global_statement":
            scope.globals.update(_text(c) for c in node.named_children if c.type == "identifier")
            return
        if kind == "nonlocal_statement":
            scope.nonlocals.update(_text(c) for c in node.named_children if c.type == "identifier")
            return
        if kind in ("import_statement", "import_from_statement"):
            for local, import_kind, target in import_bindings(node):
                scope.bind(local, Binder("import", import_kind=import_kind, target=target))
            return
        if kind == "call":
            call_scope[node.id] = scope
        elif kind == "assignment":
            # An annotation with no value (`x: int`) binds only in a function.
            right = node.child_by_field_name("right")
            if right is not None or scope.kind in ("function", "lambda"):
                binder = _NONE_VALUE if right is not None and right.type == "none" else _VALUE
                for name in _target_names(node.child_by_field_name("left")):
                    scope.bind(name, binder)
        elif kind == "augmented_assignment":
            for name in _target_names(node.child_by_field_name("left")):
                scope.bind(name, _VALUE)
        elif kind == "named_expression":
            # PEP 572: a walrus binds in the nearest scope that is not a comprehension.
            home = scope
            while home.kind == "comprehension" and home.parent is not None:
                home = home.parent
            for name in _target_names(node.child_by_field_name("name")):
                home.bind(name, _VALUE)
        elif kind == "for_statement":
            for name in _target_names(node.child_by_field_name("left")):
                scope.bind(name, _VALUE)
        elif kind == "as_pattern" and node.parent is not None and node.parent.type != "case_pattern":
            # `with ... as x` and `except E as x`. A case pattern's `as` is a capture.
            for name in _target_names(node.child_by_field_name("alias")):
                scope.bind(name, _VALUE)
        elif kind == "delete_statement":
            for child in node.named_children:
                for name in _target_names(child):
                    scope.bind(name, _VALUE)
        elif kind == "case_clause":
            for child in node.named_children:
                if child.type == "case_pattern":
                    for name in _pattern_captures(child):
                        scope.bind(name, _VALUE)
        for child in node.children:
            visit(child, scope)

    visit(root, module)
    _relocate_declarations(scopes, module)
    return ScopeTable(module, call_scope, definitions)


def _relocate_declarations(scopes: list[PyScope], module: PyScope) -> None:
    """Move the binders of `global` and `nonlocal` names to the scope they bind in.

    Innermost first, so a chain of `nonlocal` declarations ends at its owner.
    `nonlocal` needs nothing else: once its binders sit in the owning function,
    the ordinary lookup walk reaches that function first.
    """
    for scope in reversed(scopes):
        for name in scope.globals:
            moved = scope.binders.pop(name, [])
            if moved:
                module.binders.setdefault(name, []).extend(moved)
        for name in scope.nonlocals:
            moved = scope.binders.pop(name, [])
            home = _nonlocal_home(scope, name)
            if moved and home is not None:
                home.binders.setdefault(name, []).extend(moved)


def _nonlocal_home(scope: PyScope, name: str) -> PyScope | None:
    current = scope.parent
    while current is not None and current.kind != "module":
        if current.kind in ("function", "lambda") and (name in current.binders or name in current.nonlocals):
            return current
        current = current.parent
    return None


def _module_of(scope: PyScope) -> PyScope:
    while scope.parent is not None:
        scope = scope.parent
    return scope


def resolving_scope(scope: PyScope, name: str) -> PyScope | None:
    """The scope whose binding of `name` a lookup from `scope` reaches, or None."""
    if name in scope.globals:
        module = _module_of(scope)
        return module if name in module.binders else None
    current: PyScope | None = scope
    starting = True
    while current is not None:
        # A class scope is visible only to statements directly in its body.
        if (starting or current.kind != "class") and name in current.binders:
            return current
        starting = False
        current = current.parent
    return None


def resolve(scope: PyScope, name: str) -> Resolution:
    """What a bare `name` denotes when called from `scope`."""
    home = resolving_scope(scope, name)
    return Resolution("none") if home is None else _outcome(home.binders[name])


def _outcome(binders: list[Binder]) -> Resolution:
    """§4.2's outcome table, for the binders of one name in its resolving scope."""
    distinct: dict[tuple[str | None, ...], Binder] = {}
    for binder in binders:
        if binder.kind in ("def", "class"):
            key: tuple[str | None, ...] = ("def", binder.qualname)
        elif binder.kind == "import":
            key = ("import", binder.import_kind, binder.target)
        else:
            key = ("value",)
        distinct.setdefault(key, binder)
    if len(distinct) == 1:
        (binder,) = distinct.values()
        if binder.kind in ("def", "class"):
            return Resolution("def", qualname=binder.qualname, module_level=binder.module_level)
        if binder.kind == "import" and binder.import_kind is not None and binder.import_kind != "unknown":
            return Resolution(binder.import_kind, target=binder.target)
        return Resolution("value")
    # Unresolved imports of one name (`try: import ujson as json` /
    # `except ImportError: import json`) leave the repo whichever one runs. So
    # does an optional dependency (`except ImportError: X = None`): calling
    # `None` is an error, never repo code. (`None` alone is one distinct
    # binder, a value, so reaching here means some binder is not `None`.)
    if all((b.kind == "import" and b.import_kind == "external") or b.none_literal for b in binders):
        return Resolution("external")
    return Resolution("value")
