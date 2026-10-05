"""An independent oracle for Python bare-name call binding (#70, #71).

Written to GRADE graphite's extractor, not to replace it. Truth comes from
CPython: `symtable` says how each function classifies a name (declared global,
nonlocal, local), and the `ast` of the resolving scope says which binder it
is. graphite's side is the REAL extractor: `_extract_python` runs on every
file with `_python_call_target` and `_edge` wrapped, so each call site is
paired with the exact edge graphite emitted for it.

It reads the EXTRACTION stage, not the built graph. The built graph keeps one
edge per (source, target, relation), so it cannot say which call site an edge
came from, and per-site truth needs exactly that. `dump` then runs graphite's
own dispatch post-pass (`_resolve_method_dispatch`) over the same edges, so
member calls are compared as they reach the graph.

Modes
-----
  sites ROOT [--only PREFIX] [--out FILE]   score every bare-name call site
  dump ROOT [--only PREFIX] --out FILE      every call edge as a denotation record
  compare OLD NEW [--out FILE]              diff two dumps in pre-registered categories

It imports only names that exist in graphite 1.1.1 as well as later, so one
script grades both engines: run it under the interpreter whose graphite you
want to grade. Every report records which graphite ran (`engine`) and
the sha256 of this script, so a changed oracle is never read as a changed engine.

What it CANNOT see -- published so it does not grade its own blind spot
----------------------------------------------------------------------
* A name some function declares `global` (or `nonlocal`) and assigns:
  reported as `truth-rebound`, not scored.
* Two or more def/class/import binders of one name in the resolving scope,
  an if/else redefinition included: `truth-ambiguous`, not scored.
* A symbol import that reaches its definition only through a re-export
  (`pkg/__init__.py` importing it): `unbound-but-def(reexport)`, the known
  residual (spec §7), not counted as wrong.
* Star imports bind nothing it can name; PEP 695 type-parameter scopes are not
  modelled; two calls to one name on one line are paired in source order.
* Where CPython's `symtable` and this oracle's own binder walk disagree about
  whether a function binds a name, the site is still scored and the
  disagreement is counted in `selfcheck.symtable_disagreements`. A non-zero
  count is an oracle defect until each example is read and explained.
"""
from __future__ import annotations

import argparse
import ast
import collections
import hashlib
import json
import symtable
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
COMPS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
#: Every graphite name this oracle reads. All exist in 1.1.1 and later.
ENGINE_NAMES = (
    "_python_call_target", "_edge", "_extract_python", "_file_node_id",
    "_resolve_method_dispatch", "_LOADER",
)
MAX_HOPS = 8

Truth = tuple[Any, ...]


# --- corpus -------------------------------------------------------------------


@dataclass
class Corpus:
    root: Path
    files: dict[str, bytes]  # rel path -> source, for the files extracted and scored
    index: Any  # graphite SourceIndex over EVERY Python file under root


def load(root: Path, only: str | None = None) -> Corpus:
    """Every Python file graphite would extract under `root`.

    The SourceIndex covers all of them, as a real build's does; `only` (a
    rel-path prefix such as `django/`) limits which are extracted and scored.
    """
    from graphite.config import Config
    from graphite.ingest import collect_files
    from graphite.resolve import SourceIndex

    with tempfile.TemporaryDirectory() as cache:
        cfg = Config(workers=1, cache_dir=Path(cache), typescript_resolver="disabled")
        entries = [e for e in collect_files(root, cfg) if e.language == "python"]
        index = SourceIndex.from_entries(entries, cfg)
    files = {
        e.rel_path: Path(e.abs_path).read_bytes()
        for e in entries
        if only is None or e.rel_path.startswith(only)
    }
    return Corpus(root=root, files=files, index=index)


# --- graphite's side ------------------------------------------------------------


@dataclass
class Extraction:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)  # every relation
    calls: list[dict[str, Any]] = field(default_factory=list)  # one record per call edge
    call_nodes: int = 0
    distinct_call_ids: int = 0


def _ts_calls(root: Any) -> list[Any]:
    found, stack = [], [root]
    while stack:
        node = stack.pop()
        if node.type == "call":
            found.append(node)
        stack.extend(node.children)
    return found


def extract(corpus: Corpus) -> Extraction:
    """Run graphite's REAL Python extractor, pairing each call edge with its site.

    `_python_call_target` runs once per call node and the call's edge is made
    before that call's children are walked, so the edge that follows a site
    event belongs to that site. Both are wrapped as module attributes and
    restored in `finally`; an engine missing either is refused, not guessed.
    """
    import graphite.extract.ast as A

    missing = [name for name in ENGINE_NAMES if not hasattr(A, name)]
    if missing:
        raise SystemExit(f"pyscopeoracle: graphite.extract.ast lacks {missing}; cannot pair sites")
    parser = A._LOADER.parser("python")
    out = Extraction()
    events: list[tuple[Any, ...]] = []
    real_target, real_edge = A._python_call_target, A._edge

    def target(func: Any) -> Any:
        found = real_target(func)
        events.append(("site", found, func.start_point[0] + 1))
        return found

    def edge(*args: Any, **kwargs: Any) -> Any:
        made = real_edge(*args, **kwargs)
        if made.get("relation") == "calls":
            events.append(("edge", made))
        return made

    A._python_call_target, A._edge = target, edge
    try:
        for rel, source in corpus.files.items():
            events.clear()
            tree = parser.parse(source)
            ids = [node.id for node in _ts_calls(tree.root_node)]
            out.call_nodes += len(ids)
            out.distinct_call_ids += len(set(ids))
            result = A._extract_python(A._file_node_id(rel), rel, source, tree, corpus.index)
            out.nodes.extend(result.nodes)
            out.edges.extend(result.edges)
            pending: tuple[Any, ...] | None = None
            for event in events:
                if event[0] == "site":
                    pending = event
                    continue
                made = event[1]
                bare = pending[1][0] if pending is not None else None
                made["_oracle_seq"] = len(out.calls)
                out.calls.append({
                    "file": rel,
                    "line": int(str(made.get("source_location", "L0"))[1:]),
                    "site_line": pending[2] if pending is not None else 0,
                    "name": bare,
                    "kind": "bare" if bare else ("member" if "_member" in made else "alias"),
                    "edge": made,
                })
                pending = None
    finally:
        A._python_call_target, A._edge = real_target, real_edge
    return out


def denotations(nodes: list[dict[str, Any]]) -> dict[str, str]:
    """id -> `file:line` of the definition `_merge` keeps for it, or `file:file`.

    Mirrors `_merge`: nodes sorted stably by (id, source_file), first one kept.
    """
    kept: dict[str, str] = {}
    for n in sorted(nodes, key=lambda n: (n.get("id", ""), n.get("source_file", ""))):
        if n["id"] in kept:
            continue
        if n.get("kind") in ("function", "class"):
            kept[n["id"]] = f"{n['source_file']}:{str(n.get('source_location', 'L0'))[1:]}"
        elif n.get("kind") == "file":
            kept[n["id"]] = f"{n['source_file']}:file"
    return kept


def _bound(denote: dict[str, str], target: str) -> str | None:
    """The definition an edge target reaches, or None when it reaches none."""
    found = denote.get(target)
    return None if found is None or found.endswith(":file") else found


# --- CPython's side ---------------------------------------------------------------


@dataclass
class Frame:
    kind: str  # "module" | "function" | "class" | "comp"
    node: Any
    table: Any = None  # symtable for module / function / class frames
    targets: frozenset[str] = frozenset()  # comprehension targets


def _store_names(target: Any) -> list[str]:
    return [n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]


def _evaluated_outside(node: Any) -> list[Any]:
    """Parts of a def / lambda / class evaluated in the ENCLOSING scope."""
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *(k.value for k in node.keywords)]
    args = node.args
    parts: list[Any] = [*getattr(node, "decorator_list", [])]
    parts.extend(args.defaults)
    parts.extend(d for d in args.kw_defaults if d is not None)
    for arg in (*args.posonlyargs, *args.args, args.vararg, *args.kwonlyargs, args.kwarg):
        if arg is not None and arg.annotation is not None:
            parts.append(arg.annotation)
    if getattr(node, "returns", None) is not None:
        parts.append(node.returns)
    return parts


def _scope_binders(scope: Any) -> tuple[dict[str, list[Truth]], list[Any], set[str]]:
    """(binders by name, import statements, comprehension targets) of ONE scope.

    A binder is ("def", line), ("class", line) or ("value",); imports are
    returned unexpanded. Comprehension targets bind in the comprehension, not
    here, so they are reported apart; a walrus inside one still binds here.
    """
    binders: dict[str, list[Truth]] = collections.defaultdict(list)
    imports: list[Any] = []
    comp_targets: set[str] = set()
    in_function = isinstance(scope, (*FUNCS, ast.Lambda))
    if in_function:
        args = scope.args
        for arg in (*args.posonlyargs, *args.args, args.vararg, *args.kwonlyargs, args.kwarg):
            if arg is not None:
                binders[arg.arg].append(("value",))
        todo: list[Any] = [scope.body] if isinstance(scope, ast.Lambda) else list(scope.body)
    else:
        todo = list(scope.body)
    while todo:
        node = todo.pop()
        if isinstance(node, FUNCS):
            binders[node.name].append(("def", node.lineno))
            todo.extend(_evaluated_outside(node))
            continue
        if isinstance(node, ast.ClassDef):
            binders[node.name].append(("class", node.lineno))
            todo.extend(_evaluated_outside(node))
            continue
        if isinstance(node, ast.Lambda):
            todo.extend(_evaluated_outside(node))
            continue
        if isinstance(node, COMPS):
            for generator in node.generators:
                comp_targets.update(_store_names(generator.target))
                todo.append(generator.iter)
                todo.extend(generator.ifs)
            todo.extend([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt])
            continue
        if isinstance(node, ast.AnnAssign) and node.value is None and not in_function:
            todo.append(node.annotation)  # `x: int` at module or class scope binds nothing
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            binders[node.id].append(("value",))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            binders[node.name].append(("value",))
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            binders[node.name].append(("value",))
        elif isinstance(node, ast.MatchMapping) and node.rest:
            binders[node.rest].append(("value",))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        todo.extend(ast.iter_child_nodes(node))
    return binders, imports, comp_targets


def _import_bindings(statement: Any, rel: str, index: Any) -> list[tuple[str, str, Any]]:
    """(local, kind, target) per name, by graphite's own module resolution.

    Module resolution is not what phase 1 changes, so the oracle reuses it; what
    it checks independently is WHERE each binding lives and what it denotes.
    """
    out: list[tuple[str, str, Any]] = []
    if isinstance(statement, ast.Import):
        for alias in statement.names:
            if alias.asname:
                found = index.resolve_python_module(rel, alias.name)
                out.append((alias.asname, "alias" if found else "external", found))
            else:
                root = alias.name.split(".", 1)[0]
                found = index.resolve_python_module(rel, alias.name if "." not in alias.name else root)
                out.append((root, "alias" if found else "external", found))
        return out
    base, dots = statement.module or "", statement.level
    for alias in statement.names:
        if alias.name == "*":
            continue
        local = alias.asname or alias.name
        sub = f"{base}.{alias.name}" if base else alias.name
        if index.resolve_python_module(rel, sub, dots):
            out.append((local, "alias", None))
            continue
        parent = index.resolve_python_module(rel, base, dots)
        out.append((local, "symbol", (parent, alias.name)) if parent else (local, "external", None))
    return out


def _lookup(table: Any, name: str) -> Any:
    if table is None:
        return None
    try:
        return table.lookup(name)
    except KeyError:
        return None


def _rebinds(table: Any, name: str, how: str) -> bool:
    """Does a function below `table` declare `name` global/nonlocal AND bind it?"""
    for child in table.get_children():
        symbol = _lookup(child, name)
        if symbol is not None and (symbol.is_assigned() or symbol.is_imported()):
            if how == "global" and symbol.is_declared_global():
                return True
            if how == "nonlocal" and symbol.is_nonlocal():
                return True
        if _rebinds(child, name, how):
            return True
    return False


class TruthModel:
    """CPython's answer, from `symtable` and `ast`, for every file of one corpus."""

    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus
        self.trees: dict[str, Any] = {}
        self.tables: dict[str, Any] = {}
        self.cache: dict[tuple[str, int], tuple[dict[str, list[Truth]], set[str]]] = {}
        self.disagreements: list[str] = []
        self.parse_errors = 0

    def tree(self, rel: str) -> Any:
        if rel not in self.trees:
            source = self.corpus.files.get(rel)
            if source is None:
                path = self.corpus.root / rel
                source = path.read_bytes() if path.is_file() else None
            try:
                self.trees[rel] = ast.parse(source) if source is not None else None
                self.tables[rel] = symtable.symtable(source.decode("utf-8"), rel, "exec") if source is not None else None
            except (SyntaxError, ValueError, UnicodeDecodeError):
                self.trees[rel], self.tables[rel] = None, None
                self.parse_errors += 1
        return self.trees[rel]

    def binders(self, rel: str, scope: Any) -> tuple[dict[str, list[Truth]], set[str]]:
        key = (rel, id(scope))
        if key not in self.cache:
            found, imports, comp_targets = _scope_binders(scope)
            for statement in imports:
                for local, kind, target in _import_bindings(statement, rel, self.corpus.index):
                    found[local].append(("import", kind, target))
            self.cache[key] = (found, comp_targets)
        return self.cache[key]

    def in_scope(self, rel: str, scope: Any, name: str, depth: int = 0) -> Truth | None:
        """What `name` denotes if `scope` binds it; None when it binds nothing there."""
        found = self.binders(rel, scope)[0].get(name)
        if not found:
            return None
        if any(b[0] == "value" for b in found):
            return ("value",)
        if len(found) > 1:
            return ("ambiguous",)
        binder = found[0]
        if binder[0] in ("def", "class"):
            return ("def", rel, binder[1])
        _, kind, target = binder
        if kind == "external":
            return ("external",)
        if kind == "alias":
            return ("value",)  # a module object: calling it reaches no definition
        target_file, original = target
        if depth >= MAX_HOPS:
            return ("none",)
        resolved = self.in_module(target_file, original, depth + 1)
        if resolved[0] == "def" and depth >= 1:
            return ("def", resolved[1], resolved[2], "reexport")
        return resolved

    def in_module(self, rel: str, name: str, depth: int = 0) -> Truth:
        tree = self.tree(rel)
        if tree is None:
            return ("none",)
        if _rebinds(self.tables[rel], name, "global"):
            return ("rebound",)
        return self.in_scope(rel, tree, name, depth) or ("none",)

    def in_function(self, rel: str, name: str, frame: Frame) -> Truth | None:
        """The function frame's own answer, or None to keep looking outward."""
        symbol = _lookup(frame.table, name)
        found = self.in_scope(rel, frame.node, name)
        if symbol is not None:
            local = symbol.is_local() and not symbol.is_declared_global()
            comp_only = name in self.binders(rel, frame.node)[1]
            if local and found is None and not comp_only:
                self.disagreements.append(f"{rel}:{frame.node.lineno}:{name}: symtable local, no binder")
            elif not local and found is not None and not symbol.is_nonlocal():
                self.disagreements.append(f"{rel}:{frame.node.lineno}:{name}: binder, symtable not local")
        if found is not None and frame.table is not None and _rebinds(frame.table, name, "nonlocal"):
            return ("rebound",)
        return found

    def outward(self, rel: str, name: str, frames: list[Frame]) -> Truth:
        """Look `name` up from just outside `frames[-1]`'s body, skipping classes."""
        for frame in reversed(frames):
            if frame.kind == "module":
                return self.in_module(rel, name)
            if frame.kind == "class":
                continue
            if frame.kind == "comp":
                if name in frame.targets:
                    return ("value",)
                continue
            symbol = _lookup(frame.table, name)
            if symbol is not None and symbol.is_declared_global():
                return self.in_module(rel, name)
            # The same rule `resolve` applies to the call's own frame: an
            # ENCLOSING function that declares `nonlocal n` and binds it moves
            # that binder into a further-out function (Django's `_deferredSkip`).
            if symbol is not None and symbol.is_nonlocal():
                if symbol.is_assigned() or symbol.is_imported():
                    return ("rebound",)
                continue
            found = self.in_function(rel, name, frame)
            if found is not None:
                return found
        return self.in_module(rel, name)

    def resolve(self, rel: str, name: str, frames: list[Frame]) -> Truth:
        """What a bare `name` called inside `frames[-1]` denotes."""
        i = len(frames) - 1
        crossed = False
        while frames[i].kind == "comp":
            if name in frames[i].targets:
                return ("value",)
            crossed = True
            i -= 1
        frame = frames[i]
        if frame.kind == "module":
            return self.in_module(rel, name)
        if frame.kind == "class":
            if crossed:  # a comprehension in a class body never sees the class scope
                return self.outward(rel, name, frames[:i])
            found = self.in_scope(rel, frame.node, name)
            if found is not None:
                return found
            symbol = _lookup(frame.table, name)
            if symbol is not None and symbol.is_free():
                return self.outward(rel, name, frames[:i])
            return self.in_module(rel, name)
        symbol = _lookup(frame.table, name)
        if symbol is not None and symbol.is_declared_global():
            return self.in_module(rel, name)
        if symbol is not None and symbol.is_nonlocal():
            if symbol.is_assigned() or symbol.is_imported():
                return ("rebound",)
            return self.outward(rel, name, frames[:i])
        found = self.in_function(rel, name, frame)
        return found if found is not None else self.outward(rel, name, frames[:i])

    def sites(self, rel: str) -> dict[tuple[int, str], list[Truth]]:
        """(line, name) -> truth for every bare-name call, in source order."""
        found: dict[tuple[int, str], list[Truth]] = collections.defaultdict(list)
        tree = self.tree(rel)
        if tree is None:
            return found
        tables: dict[tuple[str, int], Any] = {}

        def index(table: Any) -> None:
            for child in table.get_children():
                tables.setdefault((child.get_name(), child.get_lineno()), child)
                index(child)

        index(self.tables[rel])

        def visit(node: Any, frames: list[Frame]) -> None:
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                found[(node.func.lineno, node.func.id)].append(self.resolve(rel, node.func.id, frames))
            if isinstance(node, (*FUNCS, ast.Lambda)):
                for part in _evaluated_outside(node):
                    visit(part, frames)
                key = ("lambda", node.lineno) if isinstance(node, ast.Lambda) else (node.name, node.lineno)
                inner = [*frames, Frame("function", node, tables.get(key))]
                for statement in [node.body] if isinstance(node, ast.Lambda) else node.body:
                    visit(statement, inner)
                return
            if isinstance(node, ast.ClassDef):
                for part in _evaluated_outside(node):
                    visit(part, frames)
                inner = [*frames, Frame("class", node, tables.get((node.name, node.lineno)))]
                for statement in node.body:
                    visit(statement, inner)
                return
            if isinstance(node, COMPS):
                first = node.generators[0]
                visit(first.iter, frames)  # the first iterable is evaluated outside
                targets = frozenset(n for g in node.generators for n in _store_names(g.target))
                inner = [*frames, Frame("comp", node, None, targets)]
                for generator in node.generators:
                    if generator is not first:
                        visit(generator.iter, inner)
                    for condition in generator.ifs:
                        visit(condition, inner)
                for part in [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]:
                    visit(part, inner)
                return
            for child in ast.iter_child_nodes(node):
                visit(child, frames)

        visit(tree, [Frame("module", tree, self.tables[rel])])
        return found


# --- scoring --------------------------------------------------------------------


def classify(truth: Truth, bound: str | None) -> str:
    """One scored site; `bound` is `file:line` of the definition the edge reaches."""
    kind = truth[0]
    if kind == "def":
        if bound is None:
            return "unbound-but-def(reexport)" if len(truth) > 3 else "unbound-but-def"
        return "correct" if bound == f"{truth[1]}:{truth[2]}" else "WRONG-def->other-def"
    if kind in ("value", "external", "none"):
        return "correct-unbound" if bound is None else f"WRONG-{kind}-bound"
    return f"truth-{kind}"


def measure_sites(corpus: Corpus) -> dict[str, Any]:
    """Score every bare-name call site that has a non-external edge."""
    run = extract(corpus)
    denote = denotations(run.nodes)
    model = TruthModel(corpus)
    stats: collections.Counter[str] = collections.Counter()
    examples: dict[str, list[str]] = collections.defaultdict(list)
    by_file: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for call in run.calls:
        if call["kind"] == "bare":
            by_file[call["file"]].append(call)
    for rel in sorted(by_file):
        truths = model.sites(rel)
        taken: collections.Counter[tuple[int, str]] = collections.Counter()
        lines = corpus.files[rel].decode("utf-8", errors="replace").splitlines()
        for call in by_file[rel]:
            key = (call["site_line"], call["name"])
            options = truths.get(key, [])
            truth = options[taken[key]] if taken[key] < len(options) else ("unmatched",)
            taken[key] += 1
            edge = call["edge"]
            if edge.get("confidence") == "EXTERNAL_CALL":
                stats["external-edge"] += 1
                continue
            stats["sites"] += 1
            bound = _bound(denote, edge["target"])
            category = classify(truth, bound)
            stats[category] += 1
            # Every WRONG site is kept (they are the gate); other categories are sampled.
            keep = category.startswith("WRONG") or (
                category.startswith(("unbound-but-def", "truth-unmatched")) and len(examples[category]) < 15
            )
            if keep:
                line = call["site_line"]
                text = lines[line - 1].strip()[:100] if 0 < line <= len(lines) else ""
                examples[category].append(f"{rel}:{line}: {text}  [graphite {bound} truth {truth}]")
    return {
        "files": len(corpus.files),
        "wrong": sum(n for category, n in stats.items() if category.startswith("WRONG")),
        "stats": dict(sorted(stats.items())),
        "examples": dict(examples),
        "selfcheck": {
            "call_nodes": run.call_nodes,
            "distinct_call_ids": run.distinct_call_ids,
            "symtable_disagreements": len(model.disagreements),
            "disagreement_examples": model.disagreements[:15],
            "parse_errors": model.parse_errors,
        },
    }


def dump(corpus: Corpus) -> dict[str, Any]:
    """Every call edge as (caller, targets, confidence) denotations, after dispatch."""
    import graphite.extract.ast as A

    run = extract(corpus)
    denote = denotations(run.nodes)
    methods: dict[str, set[str]] = collections.defaultdict(set)
    for n in run.nodes:
        if n.get("is_method") and n.get("name"):
            methods[n["name"].casefold()].add(n["id"])
    dispatched = A._resolve_method_dispatch(run.nodes, [dict(e) for e in run.edges])
    outputs: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    for e in dispatched:
        if "_oracle_seq" in e:
            outputs[e["_oracle_seq"]].append(e)
    records: dict[str, dict[str, Any]] = {}
    ordinal: collections.Counter[tuple[str, int]] = collections.Counter()
    summary: collections.Counter[str] = collections.Counter()
    for seq, call in enumerate(run.calls):
        edge = call["edge"]
        place = (call["file"], call["line"])
        key = f"{call['file']}:{call['line']}:{ordinal[place]}"
        ordinal[place] += 1
        made = outputs.get(seq, [])
        member = edge.get("_member")
        if not member:
            status = "n/a"
        elif not made:
            status = "dropped"
        elif len(made) > 1:
            status = "fanout"
        else:
            status = "kept" if made[0]["target"] == edge["target"] else "repointed"
        candidates = len(methods.get(member.casefold(), ())) if member else None
        summary[f"{call['kind']}:{status}"] += 1
        if status in ("kept", "dropped") and candidates is not None and candidates > 3:
            summary["member:over-cap(ungated)"] += 1
        records[key] = {
            "kind": call["kind"],
            "name": call["name"],
            "caller": denote.get(edge["source"], "unknown"),
            "targets": sorted({_bound(denote, e["target"]) or "unbound" for e in made}),
            "confidence": edge.get("confidence"),
            "dispatch": status,
            "member_candidates": candidates,
        }
    return {"files": len(corpus.files), "dispatch": dict(sorted(summary.items())), "records": records}


def compare(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Pre-registered denotation categories between two dumps (spec §5).

    A record can land in several. Anything outside these names is a finding.
    """
    old_records, new_records = old["records"], new["records"]
    stats: collections.Counter[str] = collections.Counter()
    examples: dict[str, list[str]] = collections.defaultdict(list)
    stats["only-old"] = len(old_records.keys() - new_records.keys())
    stats["only-new"] = len(new_records.keys() - old_records.keys())
    renamed = {"bare": "bare-target-changed", "member": "dispatch-changed", "alias": "alias-target-changed"}
    for key in sorted(old_records.keys() & new_records.keys()):
        a, b = old_records[key], new_records[key]
        changes = []
        if a["caller"] != b["caller"]:
            changes.append("caller-reattributed")
        if a["targets"] != b["targets"]:
            changes.append(renamed[b["kind"]])
        if a["confidence"] != b["confidence"]:
            changes.append(f"confidence-{a['confidence']}->{b['confidence']}")
            if b["kind"] == "member" and a["targets"] != b["targets"]:
                changes.append("confidence-flip-then-dispatched")
        if not changes:
            stats["same"] += 1
        for change in changes:
            stats[change] += 1
            if len(examples[change]) < 15:
                examples[change].append(f"{key}: {a} -> {b}")
    return {"stats": dict(sorted(stats.items())), "examples": dict(examples)}


def _engine() -> dict[str, str]:
    import graphite
    import graphite.extract.ast as A

    return {
        "version": str(getattr(graphite, "__version__", "?")),
        "extractor": str(Path(A.__file__).resolve()),
        # Two reports differ for an ENGINE reason only when this hash matches too.
        "oracle_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grade graphite's Python bare-name call binding.")
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("sites", "dump"):
        p = sub.add_parser(mode)
        p.add_argument("root", type=Path)
        p.add_argument("--only", default=None, help="rel-path prefix to extract and score, e.g. django/")
        p.add_argument("--out", type=Path, default=None)
    p = sub.add_parser("compare")
    p.add_argument("old", type=Path)
    p.add_argument("new", type=Path)
    p.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    if args.mode == "compare":
        report = compare(
            json.loads(args.old.read_text(encoding="utf-8")),
            json.loads(args.new.read_text(encoding="utf-8")),
        )
    else:
        corpus = load(args.root, args.only)
        report = measure_sites(corpus) if args.mode == "sites" else dump(corpus)
        report["engine"] = _engine()
    if args.out is not None:
        args.out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("examples", "records")}, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
