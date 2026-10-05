# Python scope identity: one node per definition, bare names resolved by Python's scoping rules

Date: 2026-10-04. Status: the design direction was approved by the maintainer
in conversation (2026-10-04); this document awaits their review before the
implementation plan is written. Issues: #70, #71. Phase 1 of 2; phase 2
(Python value references) is `2026-10-04-python-value-references-design.md`
and depends on this one. Channel rounds 308 and 309 asked aramid-agent about
consumers before the channel was limited to bug reports and recommended
improvements; nothing depends on them (§6).

## 1. Summary

graphite gives every Python `def` and `class` the id `_make_id(file_id, name)`
at every depth (`extract/ast.py`, `_extract_python`). Two nested helpers with
the same name, two methods with the same name in different classes, or a method
and a module function with the same name, become ONE node, and every call to
any of them binds to whichever was walked first (#70). Bare-name call
resolution looks names up in file-wide maps with no view of local bindings, so
a call through a parameter binds to a same-named module function, and a bare
call inside a method binds to a sibling method (#71). All of it is reported at
`decision_grade`.

This design gives each definition its own node (scope-qualified ids for
methods and nested definitions; module-scope ids unchanged) and resolves a
bare name the way Python does: innermost enclosing scope that binds it, then
module scope. Phase 2 reuses the same resolver for value references.

## 2. Problem and evidence

Both defects reproduce on the deployed 1.1.1 wheel (engine fingerprint
`b176a01c...`); the fixtures are in #70 and #71.

| fixture | query | result | grade |
|---|---|---|---|
| two nested `fake_build`, one per test | `callers fake_build` | both tests, against ONE definition | `decision_grade` |
| `Alpha.setup`, `Beta.setup` | the graph | one `setup` node for both | -- |
| `def route_c(generate): return generate()` | `callers generate` | `route_c` (it calls its parameter) | `decision_grade` |
| `def go(self): return run()` beside method `run` | `callers run` | `go` (a bare name in a method body never denotes a method) | `decision_grade` |

**In real code** (measured 2026-10-04 at `f006763`). A harness ran graphite's
REAL `_extract_python` on every file with `_python_call_target` and `_edge`
wrapped, so each bare-name call site was paired with the exact edge graphite
emitted for it, and judged every site against CPython `symtable` (scope
classification) plus the AST (which binder in the resolving scope). Validated
first on the #70/#71 fixtures: it reported exactly the planted wrong bindings.

| | graphite | Django 5.2.7 (sdist) |
|---|---|---|
| bare-name call sites with a non-external edge | 12,082 | 6,517 |
| **wrong target today** | **118** -- 115 shared id (#70), 3 local shadow (#71) | **76** -- 27 shared id, 49 local shadow or unbound name bound to a method |
| sites whose edge changes under lexical resolution | 118 | 92 |
| bound sites, today -> lexical | 11,809 -> 11,806 | 4,790 -> 4,725 |
| **call sites credited to the wrong CALLER today** (their enclosing def lost the shared id) | **1,345** of 34,658 | **6,614** of 33,670 |

The second half of #70 is the caller side: every call made from inside a
definition that lost its shared id is credited to the definition that kept it,
so `calls X` and `impact` conflate same-named helpers as well as `callers`.

Read at the source, not just counted: graphite's own `extract/ast.py:875`
(`visit(child)` inside the nested `visit` at L862) is bound to a DIFFERENT
function's nested `visit` at L735; `_simple_rust_value(node, _text)` calls its
parameter `_text`, bound to a module-level `_text`; Django's
`password_changed` rebinds its own name to a validator method on the line
before the call, so graphite records a recursion that never happens; Django's
`dateformat.format` (a module function) loses its id to a method `format`
defined earlier in the file.

**Node-id survival, judged by what each id DENOTES** (a string that survives
but now names a different definition is the dangerous case):

| | graphite | Django |
|---|---|---|
| def/class ids today | 5,388 | 9,155 |
| same id, same definition | 4,473 (83%) | 3,082 (34%) |
| id string removed | 915 | 6,068 |
| **same string, different definition** | **0** | **5** |
| new ids | 1,298 | 8,111 |

## 3. Decisions (maintainer, 2026-10-04)

1. **Scope-qualified ids** for methods and nested definitions, rather than
   keeping shared ids and refusing ambiguous names (measured on the
   value-reference census: refusal loses 201 of 554 true sites on graphite;
   scope-qualified loses none).
2. **Two phases, two releases.** This document is phase 1 (identity and call
   binding). Value references are phase 2.
3. **Python only.** TypeScript, JavaScript, Go and Rust build ids with the same
   `_make_id(file_id, name)`; whether they collide the same way is unmeasured
   and is a separate question (§7).
4. #70 and #71 filed with their repros (done 2026-10-04).

## 4. Design

### 4.1 Node identity

- **Module-scope definitions keep their id**: a `def` or `class` whose nearest
  enclosing scope is the module (including one inside a module-level `if` /
  `try`) is still `_make_id(file_id, name)`.
- **Every other definition gets a scope-qualified id.** Its `qualname` is the
  enclosing definitions' names and its own, joined by `.`: `Worker.run`,
  `test_one.fake_build`, `Outer.method.helper`. The id is built by a new
  `_scoped_id(file_id, qualname)` that ALWAYS appends the hex discriminator,
  hashed over a namespace tag no plain id uses (`"py-scope\x00" + file_id +
  "\x00" + qualname`). It must not go through `_make_id`'s ambiguity test:
  `_identity_preserving_form` treats `.` -> `_` as lossless, so -- measured --
  `_make_id(f, "outer.inner")` and `_make_id(f, "outer", "inner")` both return
  the same id as a module-level `outer_inner`. `Worker.run` happened to come
  out distinct only because of its capital letter.
- **Same-scope redefinitions share one node**, exactly as module-level ones do
  today: `if X: def f(): ... else: def f(): ...` is one binding slot.
- **A `qualname` attribute on every Python def/class node** (equal to `name`
  at module scope). `is_method` is unchanged (a def directly in a class body).
- `contains` edges keep their shape; they now connect distinct nodes. Lambdas
  still get no node; a call inside one is attributed to the nearest enclosing
  def, as today.
- **Caller attribution follows**: a call's `scope_id` is the enclosing
  definition's (now distinct) id, so a nested helper's calls are no longer
  pooled with every same-named helper's.

### 4.2 Lexical resolution of bare-name calls

The walk builds a scope tree (module, function, lambda, class; comprehension
scopes for their targets) and records each scope's binders. A bare-name call
`n(...)` in scope S resolves as Python does:

1. `global n` in S -> module scope. `nonlocal n` in S -> the nearest enclosing
   function scope that binds `n`.
2. Otherwise the innermost scope, starting at S, that binds `n`. From inside a
   function or lambda body, enclosing CLASS scopes are skipped; a statement
   directly in a class body sees that class's scope first. A comprehension's
   targets bind only inside the comprehension (a walrus inside one binds in the
   enclosing function, PEP 572).
3. Otherwise module scope.

**Binders**, complete: parameters (positional-only, regular, keyword-only,
`*args`, `**kwargs`, lambda parameters); assignment targets (`=`, annotated
with a value, augmented, walrus), including every name inside a target pattern;
`for` / `async for` targets; `with ... as`; `except ... as`; `import` and
`from ... import` (PER SCOPE: a function-local import binds only in that
function); `def` and `class` names; `del` targets; `match` captures (`as`,
`*rest`, mapping `**rest`, capture patterns). Any of these, in a function that
declares the name `global`, binds at MODULE scope: a module-level
`def handler` plus `def setup(): global handler; handler = make()` gives the
module two binders of `handler`, so module-level calls to it are ambiguous
(local-value), not calls to the def. Tree-sitter names for each are
pinned by a parse of a sample covering every form (as the phase 2 spec does for
value positions).

**Outcome in the resolving scope:**

| binders of `n` there | edge |
|---|---|
| exactly one `def`/`class`, nothing else | to that definition's node |
| exactly one in-repo `from m import n`, nothing else | to `_make_id(m_file, n)`, as today, but scoped |
| one unresolved import | `EXTERNAL_CALL`, as today |
| anything else: a parameter, an assignment, two def/import binders, ... | **local-value call**, `LOCAL_CALL`, counted UNBOUND. Target: today's placeholder `_make_id(file_id, n)`, UNLESS that id is a definition in this file (a module-scope `def`/`class n` exists), in which case a phantom no definition can own, `_scoped_id(file_id, "<local>." + n)` (`<` is not an identifier character). So the calls that are already unbound keep their exact target, and only the wrongly bound ones (#71) move |
| none anywhere | `_make_id(file_id, n)`, as today (a placeholder; cannot collide with a definition, because a module-scope definition would have been found and every other one is qualified) |

Names in `_LANGUAGE_BUILTIN_GLOBALS` are skipped as today.

**Why a local-value call keeps an edge.** Dropping it would remove the site
from the `calls` denominator, and a resolution metric cannot see a missing
site (the round-55 lesson). The edge stays, unbound. At SITE level the bound
count falls by the sites that were wrongly counted as bound: 3 on graphite, 65
on Django. The health cell counts DEDUPLICATED EDGES, though, and phase 1 moves
that count in other ways too (splitting a merged node splits its edges;
dispatch finds methods that a same-named module function used to hide), so the
edge-level change is measured on the implementation, and the release notes
report the measured direction and its causes -- no direction is claimed before
then.

**`module.attr` calls** resolve `module` through the same scope walk (aliases
are per scope too); the target, `_make_id(module_file, attr)`, is unchanged.
**Member calls** (`obj.m()`) are unchanged and still go through the dispatch
post-pass. `_call_confidence` reads the resolving binder: a name is external
iff its binder is an unresolved import.

### 4.3 Method dispatch (`_resolve_method_dispatch`)

The algorithm is unchanged; it indexes `is_method` nodes by name, so methods
that shared one node become distinct candidates. A name with several same-named
methods now fans out (one edge per candidate, cap 3) or exceeds the cap. Over
the cap, the edge is kept only if its target is a real node or it is
EXTERNAL_CALL; otherwise it is dropped, and leaves the `calls` denominator.
(Corrected 2026-10-05: this section first said "the phantom edge is kept",
which misread the code.) Measured before and after on graphite and Django and
reported: re-pointed edges, fan-out edges, cap overflows. Measured result: more
member calls exceed the cap, because same-named methods in one file are no
longer one candidate. This is disclosed as the caveat
`python-member-call-over-dispatch-cap`; class-aware `self.`/`super()` dispatch
is #73.

### 4.4 Query surface (`query.py`)

- **Node matching** (`_find_node_detail`): an exact, case-insensitive `qualname`
  match is a new match type `qualname`, tried after `exact-id` and before
  `name`. At equal path depth, name matches rank module-scope definitions, then
  methods, then nested definitions, then id -- so a bare name means the
  module-level definition when one exists, and the choice no longer depends on
  hash order. `alternates` stays capped at 3 and gains `alternates_total`;
  `test_daemon.py` alone will have six `fake_build` nodes.
- **Rows** (`_node_view`) add `qualname` when it differs from `name`. Golden
  outputs change deliberately, in the same commit as the code.

### 4.5 Edge location (`_merge`)

The dedup keeps the first edge of each `(source, target, relation)` after a
sort whose last key is `source_location` as a STRING, so `L12` sorts before
`L9` and the survivor's location is not the first site. Sort it numerically.
(Phase 2 builds `sites` on this.)

### 4.6 Cache and engine identity

The extraction cache partitions on engine identity (#21), so the change
invalidates its own cached extraction with no `cache_version` bump; the daemon
rebuilds supervised projects when the engine identity changes (#18).

### 4.7 Caveat registry

The process rule (`answer_contract.py`: an entry the day a class is confirmed,
decoupled from its fix) asks for two entries dated 2026-10-04, scope
disclosures for Python `calls`:

- `python-nested-name-shared-id` (#70): "nested functions and methods share one
  node per file and short name, so a call to or from one may be attributed to
  another definition of the same name";
- `python-bare-call-ignores-scope` (#71): "a bare call through a local binding
  (a parameter, an assignment) or inside a method body may be bound to a
  same-named module function or sibling method".

Both get `retired_by` = this release's date. They were registered in their
own commit on 2026-10-04, ahead of this work; as always-on disclosures they
appear in every Python `callers` / `calls` answer until retired.

## 5. Testing and measurement

**Tests first** (dev venv; new `tests/test_python_scope_identity.py`):

- methods and nested definitions get distinct ids; module-scope ids are
  byte-identical to today's; `qualname` on every def/class node;
- `outer.inner` (nested) and a module-level `outer_inner` are distinct nodes;
- one test per binder kind, each proving a call through it is a local-value
  call (unbound phantom, never a definition);
- `global` and `nonlocal` redirect; an assignment to a `global`-declared name
  in a function makes the module binding ambiguous; class-body statements see
  the class scope,
  method bodies do not; comprehension targets do not leak; a walrus in a
  comprehension binds in the function;
- a function-local import does not change what module-level calls bind to;
- the #70 and #71 fixtures as regression tests, every answer correct;
- query: `qualname` match, the ranking, `alternates_total`, `qualname` in rows;
- `_merge` keeps the numerically first location.

**Each guard proven by mutation**, each mutant failing for its own reason: one
binder kind removed at a time; class scopes not skipped; `global` ignored;
`nonlocal` ignored; imports made file-wide again; local-value calls dropped
instead of kept (the denominator test must catch it); the discriminator made
conditional (the `outer_inner` test must catch it).

**Oracle** -- `scripts/pyscopeoracle.py` with `tests/test_pyscopeoracle.py`,
following `scripts/sqloracle.py`: `symtable`-backed truth for every bare-name
call site against the BUILT graph's edges. Corpora: graphite at a pinned
commit, and the Django 5.2.7 sdist pinned by sha256. Pre-registered acceptance:

- **wrong-target bare-name calls: 0** on both corpora (today 118 and 76);
- **changes are compared by DENOTATION, not by id string.** Both engines'
  `calls` edges are mapped to (source definition, target definition) by
  (file, line), with placeholders and phantoms as one class. Id strings change
  for every call made from a method or nested def (5,521 sites on graphite,
  28,336 on Django) without any change in what they denote; that is normalised
  away. The only denotation changes allowed are: target re-pointed or unbound
  at the bare-name sites the harness predicts (118 and 92); caller
  re-attributed for call sites inside a definition that lost its shared id
  (1,345 and 6,614); and dispatch changes (§4.3), reported separately. Any
  other change fails the gate until explained;
- excluded and reported, not scored: truth-ambiguous sites (two binders in the
  resolving scope), star imports, and re-export hops (§7).

The control is the same oracle on today's engine: it must report the 118 and
76, or it is not seeing anything.

**Measured before and after, on graphite's own graph and on Django**: node ids
by denotation (§2's table); node and edge counts by relation; the Python
`calls` cell (total, bound, ratio) and placeholder share; dispatch counts
(§4.3). The full suite through the gate, and CI.

## 6. Delivery

- Commits on `main` through the pre-commit and pre-push gates, each with the
  maintainer's go. `CHANGELOG.md`, `docs/agent-integration.md` (rows gain
  `qualname`, the `qualname` match type) and the node-id entry in
  `docs/knowledge-base.md` updated in the same change.
- **Release**: proposed 1.2.0. Node ids are documented as stable only within an
  engine version ("never persist node ids across upgrades"), so the change fits
  a minor release; per `RELEASING.md` the version is measured, and §2's
  denotation table is that measurement. Release notes name the id change, the
  5-vs-0 different-denotation strings, the measured `calls`-ratio change and
  its causes, and the retirement of the two registry entries. Rollback is the
  1.1.1 wheel.
- **graphite's own node-id holders, audited before release**: the incident
  ledger, the daemon manifest, `check` records and the rollback store -- each
  checked for whether it persists node ids, and what reads them back. Tests
  hard-code 34 `<file>_py_<name>` id strings across `test_call_graph.py`,
  `test_dispatch_evidence.py`, `test_method_dispatch_scope.py` and
  `test_python_resolver.py`; those naming methods or nested defs are updated
  deliberately, in the same commit as the code.
- **Consumers are checked through their TOOL surfaces, never through an
  agent.** In a real installation only the tools are present, so a release
  cannot wait on an agent's answer. Two checks before release:
  - **The contract.** Node ids are not a stable surface: they are stable only
    within an engine version (`docs/knowledge-base.md`), and
    `docs/compatibility.md` does not list them. Nothing in the contract changes.
  - **The one known tool consumer, measured.** aramid's post-commit triage
    (`aramid triage HEAD`, run by the managed `post-commit` hook) scores every
    commit partly on "graphite blast radius" (`ARAMID.md`, "Always-on triage";
    commits scoring 40 or more are queued for review). Phase 1 changes
    `impact` for any file holding methods or nested definitions, because
    merged nodes split and mis-credited callers move. Measure `impact` result
    sizes before and after for a fixed sample of files on graphite and on
    Django, and put the distribution of changes in the release notes.
- **No channel message about the release.** A consumer has the tool, never
  graphite's agent, so a release reaches consumers through the tool itself:
  `CHANGELOG.md`, the release notes, and `graphite --version`'s engine
  fingerprint. The channel carries only bug reports and recommended
  improvements.

## 7. Out of scope and known residuals

- **Re-exports through a package `__init__`.** `from pkg import f`, where
  `pkg/__init__.py` re-exports `f`, binds to `pkg/__init__`'s `f` id, which no
  definition owns, so the call is unbound. Measured: 22 sites on graphite, 585
  on Django (9% of its bare-name call sites). Honest (counted unbound), not
  introduced here, and a candidate follow-up.
- Other languages (decision 3).
- Dynamic dispatch, `getattr`, decorator rebinding: `python-dynamic-dispatch`.
- Same-scope conditional redefinitions share one node (§4.1).
- Value references: phase 2.
