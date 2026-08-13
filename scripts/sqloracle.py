"""An independent oracle for "a query string was built, then executed".

Written to GRADE a semgrep rule, not to replace one. semgrep's rule and this
walk are independent implementations of the same idea, so where they disagree
one of them is wrong and the disagreement is the interesting output.

What it models
--------------
A name is "tainted" when it is assigned from a string-building expression:
an f-string, `+` involving a string, `%` on a string, `.format(...)`, or
`.join(...)`. Aliasing (`b = a`) propagates taint, with no hop limit -- that is
deliberate, because a fixed hop limit is one of the gaps under test. A call to
execute/executemany/executescript whose first argument is a tainted name, or is
itself an inline string-building expression, is a site.

Scope is a single function body plus module level. Assignments are considered in
source order within a scope, so a name built after its use does not taint it.

What it CANNOT see -- published so the oracle does not grade its own blind spot
--------------------------------------------------------------------------------
* cross-function flow: a query built in one function and executed in another,
  including via a return value or a helper that assembles SQL
* attributes and subscripts: `self.q = f"..."`, `d["q"] = f"..."`
* containers: a list/dict of queries built then iterated
* branches: a name string-built on only one arm of an if/else is treated as
  tainted, which is correct for reachability but coarse
* augmented assignment (`q += ...`), and `global`/`nonlocal` rebinding
* tuple unpacking: `for table, (ddl, cols) in TABLES.items()` binds `ddl`
  without an Assign node, so it lands in `unresolved_name`. graphite's real
  migrations use exactly this shape.

Consequently this oracle UNDER-counts real sites. A site it misses is not
evidence another tool over-fires. It is a lower bound on ground truth, which is
the right direction for grading precision and the wrong one for claiming recall
-- so recall figures derived from it are stated as "of the sites this oracle can
see", never as absolute.

For the size of that gap, run `tests/test_sqloracle.py` rather than reading a
number here. A figure written into prose has no verifier and rots in silence;
the same figure asserted in a test goes red. This paragraph carried "it claims
2 of 7" for about four hours before an improvement to this file made it three,
which is the whole argument.

What the `kind` field means -- and why it is four values, not two
----------------------------------------------------------------
* `assigned` / `inline` -- a CLAIM. Gate on `CLAIM_KINDS`, never on a string.
* `cleared`             -- looked at, resolved to a non-built value. SAFE.
* `unresolved_name`     -- a name whose value this walk could not evaluate:
                           never bound here, or bound from a call. A possible
                           cross-scope flow, and the blind spot worth working.
* `unattributable`      -- an attribute, subscript or call expression.
                           Structurally out of reach of a name-based walk.

The last three were one bucket called `unattributed` until 2026-08-13, and that
was a defect rather than a simplification: an execute on a name whose taint had
been correctly CLEARED came out with the same token as a shape the walk cannot
parse at all. "I checked this and it is clean" and "I cannot see this" were
indistinguishable in the output, so the oracle reported its own successes as
blindness -- and any consumer counting non-claims could not tell a verified-safe
result from a gap. Reported to aramid in round 86 as a defect in this tool.

How it was validated -- read this before trusting a number it produced
----------------------------------------------------------------------
Known-answer tests live in `tests/test_sqloracle.py` and run in this repo's
suite; the fixtures are `tests/data/sqloracle/*.py.txt`, carrying that extension
so that code which contains SQL injection BY CONSTRUCTION is not scanned by this
repo's own security gate. They pin both answer sets published on the channel, by
SCOPE NAME rather than by count -- a count drifts quietly, a named set cannot --
so that if either moves, the suite goes red and a round needs correcting instead
of a number changing underneath a published claim.

Three defects this validation caught, none of which a plausible-looking run
would have shown:

1. The module-level pass descended into function bodies, pairing an `execute`
   in one function with a string built in another. Reported 12 sites for a
   7-site fixture -- inflation that looks like thoroughness.
2. Within a compound statement, every Call was processed before any Assign, so
   build-then-execute INSIDE A FOR LOOP was missed entirely. This is the most
   common real-world shape and the fixture had no loop until the miss was
   found, so the first version scored 6/6 while being blind to it.
3. The `unattributed` conflation above.

Defect 2 is the cautionary one: the fixture agreed with the oracle because both
shared the same blind spot. A fixture written by the same person as the tool
grades the tool against its author's assumptions -- which is precisely why an
independent oracle is worth having, and why this one should not be the last
word either.

What this file will NOT tell you: how any other tool scores on the same input.
That comparison is real and it is recorded in the agent-channel rounds, dated
and attributed, where both sides can see it. It does not belong here.

An earlier version of this docstring stated how many of those shapes were
invisible to this oracle AND to the semgrep rule it grades. That sentence
described behaviour in a repository this one may not read and cannot run, so it
was inherited by construction and unverifiable from this side -- and it went
false within two hours, when improving this file moved one shape out of the set.
Nothing here could have noticed. Repository isolation is not only a boundary on
where this repo may act; it is a boundary on what it is entitled to assert.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys

EXECUTORS = {"execute", "executemany", "executescript"}

#: The kinds that are a CLAIM -- the oracle asserting a hazard is here. Every
#: other kind is a record of what it looked at, not a finding.
#:
#: Exported so a caller asking "is this a finding?" never enumerates kind
#: strings itself. That enumeration has already gone stale once: `unattributed`
#: used to hold both "analysed this, it is clean" and "cannot see this at all",
#: and any consumer that had hardcoded a list kept working while meaning
#: something different. Gate on this set, not on a string.
CLAIM_KINDS = frozenset({"assigned", "inline"})


def _is_str_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _building_form(node: ast.AST) -> str | None:
    """Name the string-building form of an expression, or None if it isn't one."""
    if isinstance(node, ast.JoinedStr):
        return "fstring"
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add) and (
            _is_str_constant(node.left) or _is_str_constant(node.right)
        ):
            return "concat"
        if isinstance(node.op, ast.Mod) and _is_str_constant(node.left):
            return "percent"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "format" and _is_str_constant(node.func.value):
            return "format"
        if node.func.attr == "join":
            return "join"
    return None


def _interpolated_names(node: ast.AST) -> list[str]:
    """The names that flow INTO a built string -- what decides if a hit is real.

    A name bound to a module constant or checked against an allowlist is safe;
    one reaching a caller's argument is not. The rule cannot tell these apart,
    which is the whole reason a human audit follows this scan.
    """
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.append(child.id)
        elif isinstance(child, ast.Attribute):
            found.append(f".{child.attr}")
    return sorted(set(found))


_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _walk_scope(node: ast.AST):
    """ast.walk, but stopping at nested scope boundaries.

    Plain ast.walk descends into function bodies, so a module-level pass would
    pair an `execute` inside one function with a string built inside another and
    report a site that does not exist. Caught by the probe's known-answer test,
    which expected 6 sites and got 12.
    """
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _NESTED_SCOPES):
            continue
        yield from _walk_scope(child)


class ScopeScanner(ast.NodeVisitor):
    def __init__(self, path: str, source: str) -> None:
        self.path = path
        self.source = source
        self.sites: list[dict] = []

    def scan_scope(self, body: list[ast.stmt], scope: str) -> None:
        # name -> (form, defining line, names that flowed in, hops from origin)
        tainted: dict[str, tuple[str, int, list[str], int]] = {}

        # Names this walk has RESOLVED to something that is not string-built:
        # bound to a plain constant, or string-built and then rebound to one.
        # An execute on one of these is a verified-clean result.
        #
        # Recorded rather than merely forgotten. Until it was, an execute on a
        # cleared name fell through to the same bucket as an expression the walk
        # cannot parse, so "I checked this and it is safe" and "I cannot see
        # this" were one token and no consumer could tell them apart. A name
        # bound to anything else -- a call, an attribute, an unknown name -- is
        # in NEITHER set: unknown is not clean, and treating it as clean would
        # fail in the one direction a security instrument may not.
        cleared: set[str] = set()

        # Assignments and executions must be interleaved in SOURCE ORDER. An
        # earlier version processed every call in a statement and only then its
        # assignments, which missed the build-then-execute-inside-a-for-loop
        # shape -- the single most common form in real code, and the one this
        # oracle exists to count. Sorting by position handles compound
        # statements without a bespoke recursive walker.
        events: list[tuple[tuple[int, int], ast.AST]] = []
        for stmt in body:
            # A def/class in this body is its own scope and gets its own pass.
            # _walk_scope only refuses to descend into nested CHILDREN, so a
            # statement that IS a scope must be skipped here or the module pass
            # walks every function body again.
            if isinstance(stmt, _NESTED_SCOPES):
                continue
            for node in _walk_scope(stmt):
                # AnnAssign matters in a typed codebase: `q: str = f"..."` is the
                # same hazard as `q = f"..."` and is what graphite would write.
                if isinstance(node, (ast.Call, ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    events.append(((node.lineno, node.col_offset), node))

        for _, node in sorted(events, key=lambda item: item[0]):
            if isinstance(node, ast.Call):
                self._maybe_site(node, tainted, cleared, scope)
                continue

            if isinstance(node, ast.AugAssign):
                # `q += ...`. Was untracked entirely, which was survivable while
                # an unknown name simply fell through -- and became a FALSE CLEAN
                # the moment `cleared` existed: `q = "SELECT ..."` marked the
                # name resolved-safe and the hazard then accumulated through
                # `+=` without ever revoking it. The accumulate-in-a-loop shape
                # is the one graphite's own migrations use.
                if not isinstance(node.target, ast.Name):
                    continue
                aug_form = _building_form(node.value)
                if aug_form is not None:
                    # Keep an existing origin: the first build is the one worth
                    # reporting, and re-stamping it would move the line a reader
                    # is sent to.
                    if node.target.id not in tainted:
                        tainted[node.target.id] = (
                            aug_form,
                            node.lineno,
                            _interpolated_names(node.value),
                            0,
                        )
                    cleared.discard(node.target.id)
                elif not _is_str_constant(node.value):
                    # An unknown right-hand side. Not clean and not attributable.
                    tainted.pop(node.target.id, None)
                    cleared.discard(node.target.id)
                # Appending a LITERAL is deliberately a no-op on both sets: a
                # resolved-clean name stays clean, and a tainted one stays
                # tainted. Clearing taint here would let `q += " AND 1=1"`
                # launder an injection with a harmless suffix.
                continue

            if isinstance(node, ast.AnnAssign):
                if node.value is None:
                    continue
                targets = [node.target]
            else:
                targets = node.targets
            if len(targets) != 1:
                continue
            target = targets[0]
            if not isinstance(target, ast.Name):
                continue
            form = _building_form(node.value)
            if form is not None:
                tainted[target.id] = (
                    form,
                    node.lineno,
                    _interpolated_names(node.value),
                    0,
                )
                cleared.discard(target.id)
            elif isinstance(node.value, ast.Name) and node.value.id in tainted:
                origin_form, origin_line, names, hops = tainted[node.value.id]
                tainted[target.id] = (origin_form, origin_line, names, hops + 1)
                cleared.discard(target.id)
            elif _is_str_constant(node.value):
                # Resolved to a literal. Clean whether or not it was ever
                # tainted -- a constant that was never built is exactly as safe
                # as one that replaced a built value, and reporting only the
                # second would leave the first in the unknown bucket.
                tainted.pop(target.id, None)
                cleared.add(target.id)
            else:
                # A call, an attribute, an unknown name. Any taint is gone,
                # because the name no longer holds the built string -- but this
                # is UNKNOWN, not clean, so it joins neither set.
                tainted.pop(target.id, None)
                cleared.discard(target.id)

    def _maybe_site(
        self,
        node: ast.Call,
        tainted: dict[str, tuple[str, int, list[str], int]],
        cleared: set[str],
        scope: str,
    ) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in EXECUTORS or not node.args:
            return
        first = node.args[0]

        if _is_str_constant(first):
            return  # literal SQL: nothing interpolates

        inline = _building_form(first)
        if inline is not None:
            self.sites.append(
                self._site(node, first, "inline", inline, first.lineno, _interpolated_names(first), 0, scope)
            )
            return

        if isinstance(first, ast.Name) and first.id in tainted:
            form, defined_at, names, hops = tainted[first.id]
            self.sites.append(
                self._site(node, first, "assigned", form, defined_at, names, hops, scope)
            )
            return

        if isinstance(first, ast.Name) and first.id in cleared:
            # Analysed and clean. NOT a claim, and not a blind spot either --
            # the distinction this branch exists to make.
            self.sites.append(
                self._site(node, first, "cleared", None, first.lineno, [], 0, scope)
            )
            return

        # Everything left is something the walk could not evaluate, split by
        # WHY, because the two have different futures. A name is a possible
        # cross-scope flow -- the largest published blind spot and the thing
        # worth working on. An attribute, subscript or call is structurally out
        # of reach of a name-based walk and always will be.
        kind = "unresolved_name" if isinstance(first, ast.Name) else "unattributable"
        self.sites.append(
            self._site(node, first, kind, None, first.lineno, _interpolated_names(first), 0, scope)
        )

    def _site(
        self,
        call: ast.Call,
        arg: ast.AST,
        kind: str,
        form: str | None,
        defined_at: int,
        names: list[str],
        hops: int,
        scope: str,
    ) -> dict:
        segment = ast.get_source_segment(self.source, arg) or "<?>"
        return {
            "file": self.path,
            "exec_line": call.lineno,
            "built_line": defined_at,
            "kind": kind,
            "form": form,
            "hops": hops,
            "scope": scope,
            "names": names,
            "expr": " ".join(segment.split())[:160],
        }


def scan_file(path: pathlib.Path) -> list[dict]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    scanner = ScopeScanner(path.as_posix(), source)

    scanner.scan_scope(tree.body, "<module>")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scanner.scan_scope(node.body, node.name)
    return scanner.sites


def main() -> None:
    roots = [pathlib.Path(a) for a in sys.argv[1:]]
    files: list[pathlib.Path] = []
    for root in roots:
        files.extend(sorted(root.rglob("*.py")) if root.is_dir() else [root])

    all_sites: list[dict] = []
    for path in files:
        try:
            all_sites.extend(scan_file(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            print(f"SKIP {path}: {exc}", file=sys.stderr)

    print(json.dumps(all_sites, indent=1))


if __name__ == "__main__":
    main()
