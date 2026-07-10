# Graphite Accuracy Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce Graphite graph noise and make it more useful for safer app development through better ingestion, import resolution, freshness checks, and test-impact suggestions.

**Architecture:** Keep Graphite local-first and zero-LLM. Add deterministic static analysis improvements without introducing TypeScript compiler or network dependencies. Extend the CLI with `check` and `impact` commands so every project under `F:\Projects` can rebuild, verify freshness, and pick tests from the graph.

**Tech Stack:** Python 3.14, tree-sitter, networkx, pytest, existing Graphite CLI.

---

### Task 1: File Ingestion Hygiene

**Files:**
- Modify: `src/graphite/ingest.py`
- Test: `tests/test_hardening.py`

- [ ] Write failing tests proving generated folders (`graph-out`, `.cache`, `.pytest_cache`, `__pycache__`, `tools`, `vendor`) are skipped while source files remain included.
- [ ] Implement expanded skip lists and normalized path matching.
- [ ] Run `python -B -m pytest tests/test_hardening.py -q`.

### Task 2: TypeScript Import Resolution and Call Noise Filtering

**Files:**
- Modify: `src/graphite/extract/ast.py`
- Test: `tests/test_hardening.py`

- [ ] Write failing tests for extensionless relative imports, directory `index.ts` imports, tsconfig-style alias imports, and built-in/member-call noise such as `items.map`, `JSON.parse`, `console.log`.
- [ ] Add a source index into extraction so import resolution uses known project files instead of shell cwd assumptions.
- [ ] Add edge confidence labels for exact imports, external imports, local calls, and inferred calls.
- [ ] Run `python -B -m pytest tests/test_hardening.py -q`.

### Task 3: Freshness Check Command

**Files:**
- Modify: `src/graphite/cli.py`
- Test: `tests/test_hardening.py`

- [ ] Write failing tests for a fresh graph and for changed/added/removed files after graph generation.
- [ ] Add `graphite check <path> [--json]` comparing current scan hashes with `graph-out/.graphite_manifest.json`.
- [ ] Run `python -B -m pytest tests/test_hardening.py -q`.

### Task 4: Test Impact Command

**Files:**
- Modify: `src/graphite/cli.py`
- Test: `tests/test_hardening.py`

- [ ] Write failing tests proving `graphite impact src/foo.ts --graph-json graph-out/graph.json` returns impacted source files and likely test files.
- [ ] Implement reverse traversal from changed source nodes and prioritize tests under `tests/` or files ending in `.test.ts`, `.spec.ts`, `.test.py`, `.spec.py`.
- [ ] Run `python -B -m pytest tests/test_hardening.py -q`.

### Task 5: Docs and Verification

**Files:**
- Modify: `README.md`
- Modify: `skill/SKILL.md`

- [ ] Document `check` and `impact`.
- [ ] Run full tests: `python -B -m pytest tests`.
- [ ] Rebuild Pawscout graph from `F:\Projects\Shopify\demo-store2\pawscout-worker` with `graphite -v build .`.
- [ ] Verify `graphite check .` reports fresh and `graphite impact src/tenant-store.ts` suggests relevant tests.
