# Python value references: `callers` stops reading as complete when a function is passed as a value

Date: 2026-10-04. Status: revised after an audit (2026-10-04); awaits the
maintainer's review. **Phase 2 of 2: depends on phase 1,
`2026-10-04-python-scope-identity-design.md`** (one node per definition, and
the lexical resolver this design binds through). Channel rounds: 55
(aramid-agent), 303 (atlas-agent), 305 (graphite-agent's reply to 303); 308 and 309 asked
aramid-agent about consumers, as development-time input only, not a release
gate (§7).

## 1. Summary

A Python function that is passed as a *value* -- `functools.partial(generate, p)`,
`threading.Timer(5, watchdog)`, `atexit.register(on_exit)` -- produces no edge
in graphite's graph. Round 55's fix (`afbdd48`, 2026-08-06) made an EMPTY
`callers` answer over such a function honest: it caps at `advisory` and carries
the conditional `python-callback-registration` caveat. It deliberately left
NON-EMPTY answers alone ("the callers you DID find are real"). Round 303 showed
why that is not enough: a non-empty answer at `decision_grade` reads as a
COMPLETE list, and when the only other uses are value uses, it is not.

This design adds a Python `references` edge for a function used as a value,
bound through phase 1's lexical resolver, so those sites appear in the answer
itself, each with the lines it occurs on. Grading is unchanged; a conditional
caveat says when an answer contains value references. What stays unmodelled
(decorator registration, `self.method` values) is disclosed on the function
it affects, on any answer about that function, not only on empty ones.

## 2. Problem and evidence

**Round 303 (atlas-agent, graphite 1.1.0).** `callers generate` returned one
caller (a module node) at `decision_grade`. An AST scan of the consumer's repo
found `generate` passed as a value four times, every one as
`functools.partial(generate, ...)` handed to `loop.run_in_executor`, and called
directly zero times there. None of the four appeared. The cost was concrete:
atlas was tracing whether an unvalidated request field could reach command
execution through `generate`, and a decision-grade "only atlas_llm calls it"
said the web routes never reach it.

**Why grading alone cannot fix it (round 305).** The extractor records nothing
for a value use, so at query time an answer whose function has value uses looks
identical to one without. A grading rule could only downgrade every Python
`callers` answer, which is the unconditional-caveat problem round 55 named: an
always-on hedge carries no signal and trains readers to ignore caveats.

**Why the health ratio cannot see it.** `resolution_health` measures how many
DETECTED call sites bound. A value use is not a call site, so it never enters
the denominator (round 55: a resolution metric cannot underwrite a coverage
claim).

## 3. Measured baseline

All measured 2026-10-04 on the working tree at `f006763` with the dev venv,
not inherited.

**Fixture** (`app/llm.py` defines `generate` and calls it from `warm`;
`app/routes.py` passes it through `functools.partial` in `route_a` and
`route_b`, takes a PARAMETER named `generate` in `route_c`, and registers
`watchdog` with `threading.Timer` and `Thread(target=)` and `on_exit` with
`atexit.register` from `start`):

| query | result | grade | caveats |
|---|---|---|---|
| `callers generate` | 1: `warm` -- **`route_a`, `route_b` missing** | `decision_grade` | `python-dynamic-dispatch` |
| `callers watchdog` | 0 | `advisory` | + `python-callback-registration` |
| `callers on_exit` | 0 | `advisory` | + `python-callback-registration` |

A second file, `app/worker.py`, adds `class Worker` with methods `run` and
`go` (`go` does a bare `return run()`) and a module function decorated
`@atexit.register`: `callers run` (the method) returns `go` at
`decision_grade` -- a false caller, #71, fixed by phase 1 -- and
`callers on_exit2` returns 0 with the empty-answer caveat; decorator
registration is unmodelled.

**graphite's own graph**: 7,801 nodes at `f006763` (7,800 earlier the same
day: the count drifts with the tree, so invariants are stated on a FROZEN tree
built by two engines, §6), zero `references` edges, Python `calls` cell total
10,554, bound 10,309, ratio 0.977.

**The `references` relation already exists**: `query._CALL_RELATIONS =
{"calls", "references"}`, so `callers`, `calls` and `reaches` already walk it;
the TypeScript resolver emits FILE-to-FILE `references`, which never reach a
per-function answer. `cli._impact` walks every predecessor edge.

**Census against CPython `symtable`** (graphite's own Python and the Django
5.2.7 sdist; every value use in a §5.1 position scored; the oracle and the
binding models are throwaway scripts, the release-gating version is §6's):

| binding | graphite TP / FP / FN | Django TP / FP / FN |
|---|---|---|
| this spec's first draft (file-wide maps, shared ids) | 393 / **149** / 20 | 328 / **43** / 137 |
| fail-closed (refuse names the file binds twice) | 353 / 0 / **201** | 320 / 3 / 156 |
| **phase 1 ids + lexical resolution (this design)** | **554 / 0 / 0** | **340 / 0 / 136** |

Django's 136 are out of v1 by design (88 decorators, 34 attribute-of-a-function,
5 comparisons) or deliberate refusals (9 module-level rebindings such as
`void_output = partial(void_output, cpl=True)`). How far to trust "0 FP": the
first-draft arm is the firing control (it finds 149); a 12-file known-answer
corpus reads 15/0/0; but the oracle and the lexical model share their notion of
what a scope contains, and only `symtable`'s local/free/global classification
and import resolution are independent of it. Two oracle defects were found and
fixed during the audit (free variables were all read as local; a `def` inside
an `if`/`for` was not seen as a function).

## 4. Decisions (maintainer, 2026-10-04)

1. **Grade.** An answer that contains value references keeps the grade its
   health cells give it. Each row says how it was reached, and a conditional
   caveat appears only when references are present. Capping such an answer at
   `advisory` was rejected: it would grade the MORE complete answer lower than
   one whose references went undetected.
2. **Scope of v1.** Bare names, and `module.name` where `module` resolves to an
   import alias. `self.method` / `obj.method` value uses
   (`Thread(target=self._run)`) are not edges in v1 -- binding them would reuse
   the name-based dispatch heuristic whose false bindings took #54 and #56 to
   tame -- but they ARE disclosed on the method (decision 7).
3. **Targets.** Functions only. Classes passed as values are a separate
   question.
4. **Language.** Python only.
5. **Identity** comes from phase 1 (scope-qualified ids), not from refusing
   names the file binds twice.
6. **Rows carry their site lines** (round 305 promised atlas the sites).
7. **Residuals are disclosed per function**, on any answer about it, not only
   on empty answers.

## 5. Design

### 5.1 Extraction (`extract/ast.py`, `_extract_python`)

**Value positions.** An operand in one of these positions is a candidate. Node
and field names are tree-sitter-python's, read from parses of samples covering
every form:

| Position | Node | Operand(s) |
|---|---|---|
| positional argument | `argument_list` | each direct expression child (not `keyword_argument`, `list_splat`, `dictionary_splat`) |
| keyword argument | `keyword_argument` | field `value` |
| assignment | `assignment` | field `right` |
| walrus | `named_expression` | field `value` |
| return / yield | `return_statement`, `yield` | the expression child |
| container element | `list`, `tuple`, `set`, `expression_list` | each element |
| dict value | `pair` | field `value` (not `key`); covers dict-comprehension bodies |
| default parameter | `default_parameter`, `typed_default_parameter` | field `value` |
| lambda body | `lambda` | field `body` |
| conditional | `conditional_expression` | the FIRST and THIRD named children; the middle one is the condition |
| boolean operand | `boolean_operator` | fields `left`, `right` (`cb or default_cb`) |
| comprehension body | `list_comprehension`, `set_comprehension`, `generator_expression` | field `body` |

`parenthesized_expression` is transparent; nested containers are reached by the
normal walk. Excluded on purpose: `augmented_assignment` (`x += f` is
arithmetic), a call's `function` field (that is a call), an attribute's
`object` (`f.__name__`), comparisons (`f is g`), and decorators (§5.3's
evidence covers them).

**Operand shapes.** An `identifier` (bare name), or an `attribute` whose
`object` is an `identifier` that resolves to an import alias (`module.name`).
Every other attribute shape is not an edge in v1; `self.X` / `cls.X` feeds the
evidence in §5.3.

**Binding: phase 1's lexical resolver**, so a reference binds exactly where a
call of the same spelling would. In the resolving scope:

| binders | result |
|---|---|
| exactly one `def` (a function node, not `is_method`), nothing else | a `references` edge to it |
| exactly one in-repo `from m import n`, nothing else | a `references` edge to `_make_id(m_file, n)` (dropped in §5.2 unless it is a function node) |
| a `class`, a method, an unresolved import, a builtin | nothing |
| anything else (a parameter, an assignment, two binders) | nothing: it is a local value, not a reference to a definition |

Unlike calls, a value use that does not bind leaves NO edge: references are
not counted in the health ratio, so there is no denominator to protect.
**Operands evaluated outside the definition's own scope** resolve in the
ENCLOSING scope: a parameter default (`def f(cb=handler)`) and a decorator
argument resolve where the `def` statement is, not inside `f`. A bare name in
a class body that resolves to a method of that class (`signals = {"tick":
on_tick}` after `def on_tick(self)`) is not an edge; it is evidence (§5.3).

**Edge.** `_edge(scope_id, target, "references", rel_path, line,
confidence="PY_VALUE_REFERENCE")`, `scope_id` being the enclosing definition
or the file node at module level, as for calls.

### 5.2 Bound-only post-pass (`_merge`)

Beside `_resolve_method_dispatch`, before the dedup: keep a
`PY_VALUE_REFERENCE` edge only if its target is a node of kind `function` not
tagged `is_method`. Phase 1's resolver already refuses the rest; this is the
guard of last resort, and it keys on the confidence tag, so TypeScript's
file-level `references` are untouched.

**`_merge` keeps every site.** The dedup folds duplicate `(source, target,
relation)` edges into one; the survivor now also accumulates the site lines of
the edges it absorbs, as `sites` (numerically sorted, capped at 20) and
`sites_total`. Scope, stated exactly: `calls` and `references` edges, in EVERY
language -- `_merge` is language-agnostic and the `callers` / `calls` rows that
read `sites` are too, so a TypeScript or Go `callers` row gains `sites` as
well, and those golden outputs change deliberately. Other relations keep their
single location.

**`build_graph` keeps per-relation attributes.** It merges edges that share a
node pair across relations and keeps the FIRST-sorted edge's confidence and
`source_file`. Two pairs become common here and must not lose information:
`calls` + `references` (a function both called and passed by the same caller)
and `contains` + `references` (`def outer(): def cb(): ...; register(cb)`).
The merged edge must keep each relation's confidence and `sites`, so that the
`calls` cell is computed from the calls edge's own attributes whatever the sort
order, and `stats.edges_by_relation` counts every relation of a merged edge
(it reads only the first today).

Invariants the tests pin: **no new nodes** (a dropped reference creates
nothing); **health unchanged** (`references` is not in
`health._COUNTED_RELATIONS`; an edge set bound by construction would score 1.0
and say nothing); **TypeScript untouched**.

### 5.3 Answers (`query.py`, `answer_contract.py`, `debt.py`)

- **`via` and `sites` on every `callers` / `calls` row**:
  `via: sorted(edge_relations(edge) & _CALL_RELATIONS)` and
  `sites: {"calls": [12], "references": [40, 41]}`, plus
  `sites_total: {"references": 57}` naming only the relations whose list was
  capped at 20. Added by `_capped_edge_listing`, not `_node_view` (which also feeds
  `impact`, `search` and the golden outputs). Schema-compatible: verb payload
  rows are additive by contract.
- **`answer.relations`** names what the verb walked: `["calls", "references"]`.
  Grading reads only relations that HAVE health cells; `references` has none by
  design, and `build_answer_block` skips a relation with no cell, so the grade
  is unchanged (a test pins it).
- **New conditional caveat `python-value-reference`** (relations `calls`,
  languages `python`), only when the answer contains a reference row. Summary:
  "some of these sites pass the function as a VALUE (an argument, assignment,
  return or container element); it runs wherever that value is later invoked,
  which may not be the listed site". The count is `answer.value_references`,
  over the FULL edge list, not the `max_results` slice. **Plumbing**: the verb
  passes the graph, node and direction into `build_answer_block`, and the
  counting happens inside its `try`, so a failure drops the block (fail-open)
  instead of raising. `callers` / `calls` only.
- **Registry `kind`.** `python-value-reference` is a DISCLOSURE (it describes
  rows that are correct), not a blind spot, so it gets `kind: "disclosure"` and
  `debt.py` skips disclosures; otherwise `graphite debt` would count it as open
  debt forever.
- **`python-callback-registration` is retired** (`retired_by` = the release
  date): bare-name registrations now produce edges, so its summary is no longer
  true, and a published code's meaning never changes. Successor
  **`python-indirect-invocation-unmodelled`**, emitted when the answer is EMPTY
  **or** the answered function carries evidence. The extractor sets
  `unmodelled_invocation: [reason, ...]` on a function node:
  - `decorator` -- decorated by anything outside a transparent set
    (`staticmethod`, `classmethod`, `property` and its setters,
    `abstractmethod`, `overload`, `override`, `functools.wraps`, `cache`,
    `lru_cache`, `cached_property`, `contextmanager`, `asynccontextmanager`).
    `@atexit.register`, `@app.route(...)`, `@pytest.fixture`, `@receiver(...)`
    all count;
  - `method-value` -- a method `X` of class `C` where `self.X`, `cls.X` or
    `C.X` appears in a §5.1 value position inside `C`;
  - `refused-value` -- a value use that names the function but was not bound:
    a class-body bare name resolving to the method, or a module-level rebinding
    (`x = partial(x, ...)`, 9 sites on Django).

  Reason counts go in `answer.unmodelled_invocations`, so the summary stays a
  fixed string. The empty-answer `advisory` cap stays: a non-detection class
  still exists (`getattr`, decorators of OTHER functions, dynamic import).
- **`NON_DETECTION_REASONS` is keyed by language.** Python's names the residual
  classes above; JavaScript/TypeScript keep their own wording.
- **`reaches`**: each hop of a returned path carries `via`, and
  `python-value-reference` fires when any hop is reached only through
  `references`. **`impact`**: no change and no caveat -- a function that passes
  X as a value IS affected by a change to X.
- **Human output**: query verbs print JSON, so the caveats are in what an agent
  reads. `cli._answer_lines` (used by `impact`) keeps its current rule; no
  print-rule change is needed.

### 5.4 Cache and engine identity

The extraction cache partitions on engine identity (#21); the daemon rebuilds
on an engine change (#18). A file's references depend on its imports'
definitions, which the resolver-language file-set digest (#2) covers; a test
pins it (adding a sibling definition re-extracts the importer).

## 6. Testing and measurement

**Tests first** (dev venv; `tests/test_python_value_references.py` plus
additions to `tests/test_answer_contract.py`):

- one test per §5.1 position and operand shape, each asserting the exact edge
  (source, target, relation, confidence, sites);
- each non-binding outcome in §5.1's table emits nothing; defaults and
  decorator arguments resolve in the enclosing scope;
- `via` and `sites` on rows, including a pair reached both ways; `sites`
  numerically ordered and capped;
- caveat present with references, absent without; grade unchanged with
  `references` in `answer.relations`; `answer.value_references` counts beyond
  the slice; a raising count drops the block instead of the answer;
- `unmodelled_invocation` for each reason, and the successor caveat on a
  NON-EMPTY answer for a decorated function; the retired code never emitted;
- `debt.py` skips disclosures; `NON_DETECTION_REASONS` per language;
- `calls`+`references` and `contains`+`references` merges keep both relations'
  attributes, in both sort orders, and `edges_by_relation` counts both;
- the cache-sibling test; TypeScript `references` survive unchanged;
- golden outputs (`test_query_verb_outputs_are_golden_stable`) and the registry
  tests updated deliberately in the same commit;
- the §3 fixture end to end: `callers generate` lists `warm`, `route_a`,
  `route_b` and not `route_c`; `callers watchdog` and `callers on_exit` list
  `start`.

**Each guard proven by mutation**, each mutant failing for its own reason: the
resolver bypassed for file-wide maps; defaults resolved in the inner scope; the
bound-only filter removed; the method refusal removed; the caveat condition
inverted; the count taken over the slice; `sites` kept by string order; the
per-relation merge reduced to first-wins; `debt.py`'s disclosure skip removed.

**Oracle**: `scripts/pyvalueoracle.py` with `tests/test_pyvalueoracle.py`,
following `scripts/sqloracle.py`: an exhaustive differential of the BUILT
graph's `references` edges against the `symtable` truth, no sampling (the
first draft's 40-edge sample had no power to see a 1% error rate). Corpora:
graphite at a pinned commit and the Django 5.2.7 sdist pinned by sha256.
Pre-registered: **FP = 0** on both; in-scope FN at most 1% (graphite) and 3%
(Django); out-of-scope classes reported separately. The control is the
first-draft binding run through the same oracle: it must report false
references (149 on graphite), or the oracle is blind.

**Invariants on a frozen tree, built by both engines**: node id sets
identical; `calls` edges identical as `(source, target)` pairs with identical
attributes; the Python `calls` / `imports` cells identical; the number of
`references` edges reported; `graph.json` size before and after (`sites` is
added to `calls` and `references` edges in every language). The synthetic benchmark corpus has no value uses,
so it cannot see this change: re-measure on Django. The full suite through the
gate, and CI.

## 7. Delivery

- After phase 1 is released. Commits on `main` through the pre-commit and
  pre-push gates, each with the maintainer's go; `CHANGELOG.md` and
  `docs/agent-integration.md` (`via`, `sites`, the caveats,
  `answer.unmodelled_invocations`) updated in the same change.
- Release: version measured per `RELEASING.md`. This release adds edges, adds
  two caveat codes and retires one, and changes no node id. It is the first
  retirement of a code that shipped in a 1.x release; the notes say so.
  Rollback is the previous wheel.
- Consumers are checked through their tool surfaces, never through an agent
  (as in phase 1 §6). New edges change `impact` (a function that passes X as a
  value now appears in X's impact), which feeds aramid's blast-radius triage
  score, so the same before/after `impact` measurement goes in the release
  notes. Retiring `python-callback-registration` is within the contract: caveat
  codes are not a `docs/compatibility.md` surface, and a retired code is never
  re-used.
- **External verification after DEPLOY**: ask atlas-agent to re-run its round
  303 reproduction once its graph's engine fingerprint matches the release;
  round 303 is marked `done` only when that is verified.

## 8. Out of scope and known residuals

- `self.method` / `obj.method` values and decorator registration as EDGES
  (disclosed per function, §5.3).
- Classes as values (decision 3); JavaScript/TypeScript function-level value
  references (decision 4).
- Attribute-of-a-function (`f.cache_clear`) and comparisons (`f is g`): not
  value flows of `f` into another invocation.
- Module-level rebinding refused (`refused-value` evidence).
