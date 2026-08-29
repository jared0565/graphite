# Graphite 1.0 Production-Grade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every declaration criterion D1–D11 in the PRD true and release graphite 1.0.0.

**Architecture:** Nine work streams, each a branch merged into `main` with a merge commit in the repo's style (`Merge <branch>: <what changed> (PRD WS-x)`). Gates first (CI matrix, mypy, coverage, security), then the POSIX daemon subsystem, the benchmark, the documentation/contract, then provenance and the release. Every gate is proven able to fail before it is trusted.

**Tech Stack:** Python 3.11–3.14, GitHub Actions, hatchling, pytest + pytest-cov + coverage, mypy, ruff, aramid 0.5.1 (gitleaks/semgrep/ruff-S/shadow), systemd (user units), launchd (`plistlib`), `pypa/gh-action-pypi-publish` with PEP 740 attestations.

**Spec:** `docs/superpowers/specs/2026-08-29-production-grade-design.md`

## Global Constraints

- Run pytest and every `-m graphite` gate through the dev venv: `F:\Projects\.venvs\graphite-dev\Scripts\python.exe` (the machine `python` imports the released wheel and would pass on code it never loaded). In this document `$DEV` means that interpreter.
- Never pipe a gate's exit status: redirect to a file, then `echo EXIT=$?` into the same file and read it.
- `requires-python >= 3.11`; the CI matrix is exactly windows/ubuntu/macos × 3.11, 3.12, 3.13, 3.14.
- Every `python -m graphite` launch a generator writes carries `-P`.
- No live provider or network calls in tests; subprocess boundaries are exercised through an injected `run`.
- A `# type: ignore` must carry an error code (`# type: ignore[arg-type]`) and a trailing reason.
- No file under `graph-out/` is edited by hand; commit only files named explicitly (`git add <paths>`, never `git add .` or `-A`).
- Merge commits, not squashes; branch names `prod/ws-<letter>-<topic>`.
- Two aramid runs must never overlap; the local pre-push hook runs the whole suite (~12 min) — push in the background and read the log.
- Long markdown is written with the Write tool, not shell heredocs.

---

## WS-A — Every matrix cell gates (D1)

### Task A1: Twelve-cell test matrix, separate lint job

**Files:**
- Modify: `.github/workflows/ci.yml` (jobs `test`, `portability`; delete `portability`)

**Interfaces:**
- Produces: job ids `lint`, `test` (matrix), `artifact` — later tasks add `security`, `coverage`, `benchmark` beside them.

- [ ] **Step 1: Branch**

```bash
git switch -c prod/ws-a-matrix-gates main
```

- [ ] **Step 2: Rewrite the `test` job as the matrix and add `lint`**

Replace the `test:` job body and delete the whole `portability:` job (and its `workflow_dispatch` input `portability` plus the billing comments that justified it). The result:

```yaml
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

concurrency:
  group: ci-${{ github.ref }}-${{ github.event_name == 'workflow_dispatch' && github.run_id || 'auto' }}
  cancel-in-progress: true

jobs:
  lint:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: pyproject.toml
      - name: Install
        run: python -m pip install -e ".[dev,mcp]"
      - name: Ruff
        run: python -m ruff check .

  test:
    # Every supported OS x Python cell gates. This repository is public, so
    # Actions minutes are free; the old advisory `portability` job existed
    # only while legs were flaky (#45, #46), and all of them have passed
    # since. A cell that cannot fail a merge is not a supported platform.
    strategy:
      fail-fast: false
      matrix:
        os: [windows-latest, ubuntu-latest, macos-latest]
        python: ["3.11", "3.12", "3.13", "3.14"]
    runs-on: ${{ matrix.os }}
    name: test (${{ matrix.os }}, py${{ matrix.python }})
    timeout-minutes: 25
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: ${{ matrix.python }}
          cache: pip
          cache-dependency-path: pyproject.toml
      - name: Install
        run: python -m pip install -e ".[dev,mcp]"
      - name: Sample deep-probe cold (dispatch only)
        if: github.event_name == 'workflow_dispatch' && matrix.os == 'windows-latest' && matrix.python == '3.14'
        continue-on-error: true
        run: python -m pytest tests/test_doctor.py -k real_server -rA -q
      - name: Test
        # `--timeout` makes a hung leg name itself instead of dying silently
        # at `timeout-minutes` (#45). `--timeout-method=thread` because
        # `signal` raises in the main thread only.
        run: python -m pytest -q --timeout=120 --timeout-method=thread
```

Keep the `artifact` job exactly as it is.

- [ ] **Step 3: Validate the YAML parses and shows 14 jobs**

```bash
python - <<'EOF'
import yaml
d = yaml.safe_load(open(".github/workflows/ci.yml", encoding="utf-8"))
jobs = d["jobs"]
assert set(jobs) == {"lint", "test", "artifact"}, set(jobs)
m = jobs["test"]["strategy"]["matrix"]
assert len(m["os"]) * len(m["python"]) == 12
assert "continue-on-error" not in jobs["test"]
print("ok: lint + 12 test cells + artifact")
EOF
```

- [ ] **Step 4: Commit, push the branch, watch the run**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: every OS x Python cell gates; lint is its own job (PRD WS-A)"
git push -u origin prod/ws-a-matrix-gates > /tmp/push-a.log 2>&1; echo EXIT=$? >> /tmp/push-a.log
```

Then `gh run list --branch prod/ws-a-matrix-gates --limit 1` and `gh run watch <id> --exit-status`. Expected: 14 jobs succeed. If a 3.13 leg fails, read its log; a real platform defect is fixed in this branch before merging (D1 needs 12/12).

- [ ] **Step 5: Merge**

```bash
git switch main
git merge --no-ff prod/ws-a-matrix-gates -m "Merge prod/ws-a-matrix-gates: every OS x Python cell gates merges (PRD WS-A)"
git push origin main > /tmp/push-main-a.log 2>&1; echo EXIT=$? >> /tmp/push-main-a.log
```

Read `EXIT=` and the `gh run` on the merge commit: all jobs green by SHA.

---

## WS-D — Type gate (D4)

### Task D1: mypy configuration and dev dependency

**Files:**
- Modify: `pyproject.toml` (`[project.optional-dependencies].dev`, new `[tool.mypy]`)

**Interfaces:**
- Produces: `python -m mypy` runs with `files` preset; later tasks only need to run it.

- [ ] **Step 1: Branch**

```bash
git switch -c prod/ws-d-type-gate main
```

- [ ] **Step 2: Add mypy to `[dev]` and configure**

In `pyproject.toml`, change the `dev` extra to:

```toml
dev = ["pytest>=8.0", "ruff>=0.5", "pytest-timeout>=2.3", "pytest-cov>=5.0", "mypy>=1.14"]
```

Append:

```toml
[tool.mypy]
files = ["src/graphite"]
python_version = "3.11"
check_untyped_defs = true
warn_unused_ignores = true
warn_redundant_casts = true
no_implicit_optional = true
show_error_codes = true
pretty = false

[[tool.mypy.overrides]]
# No stubs are published for these; `ignore_missing_imports` narrowly.
module = ["networkx", "networkx.*", "community", "community.*", "tree_sitter", "tree_sitter_*", "louvain", "mcp", "mcp.*"]
ignore_missing_imports = true
```

- [ ] **Step 3: Install into the dev venv and take the baseline**

```bash
$DEV -m pip install -e ".[dev,mcp]" > /tmp/mypy-install.log 2>&1; echo EXIT=$? >> /tmp/mypy-install.log
$DEV -m mypy > /tmp/mypy-baseline.txt 2>&1; echo EXIT=$? >> /tmp/mypy-baseline.txt
tail -3 /tmp/mypy-baseline.txt
```

Expected: `Found N errors in M files` with `EXIT=1`. Record N and M in the commit message of Task D2.

- [ ] **Step 4: Commit the configuration alone**

```bash
git add pyproject.toml
git commit -m "build: configure mypy for src/graphite (baseline: N errors in M files)"
```

### Task D2: Fix every mypy finding

**Files:**
- Modify: every file mypy names; start with the largest counts (baseline order from pyright: `src/graphite/detach.py`, `src/graphite/routing/probe_runner.py`, `src/graphite/analyze.py`, `src/graphite/probe_process.py`, `src/graphite/doctor_probes.py`, `src/graphite/doctor.py`, `src/graphite/query.py`, `src/graphite/typescript_activation.py`).
- Test: the existing suite; a focused test is added first whenever a fix changes runtime behaviour.

**Interfaces:**
- Consumes: `python -m mypy` from Task D1.
- Produces: `src/graphite` passes mypy; no public signature changes.

- [ ] **Step 1: Work file by file, largest first**

For each file:

```bash
$DEV -m mypy src/graphite/<file>.py 2>&1 | tee /tmp/mypy-file.txt | tail -20
```

Apply the fix rules in this order of preference:
1. **Narrow** — `if value is None: raise/return` or `assert value is not None` with a message naming why the invariant holds.
2. **Annotate** — add the missing parameter/return annotation, or widen a container type (`list[str] | None`).
3. **Correct** — a real defect (e.g. a `str` passed where `Path` is needed, an attribute that does not exist on that branch). Write the failing test in the owning `tests/test_*.py` first, run it to see it fail, then fix.
4. **Ignore with a code** — only for third-party types that are wrong: `# type: ignore[attr-defined]  # networkx Graph is untyped`.

Example of rule 1 (shape seen in `detach.py`-style code):

```python
# before
proc = self._process
proc.terminate()          # error: Item "None" of "Popen[str] | None" has no attribute "terminate"

# after
proc = self._process
if proc is None:
    raise RuntimeError("detach: terminate called before spawn")
proc.terminate()
```

Example of rule 3 with its test:

```python
def test_probe_runner_passes_a_path_not_a_str(tmp_path: Path) -> None:
    runner = ProbeRunner(workspace=tmp_path)
    assert isinstance(runner.workspace, Path)
```

- [ ] **Step 2: Run the owning tests after each file**

```bash
$DEV -m pytest tests/test_<area>.py -q > /tmp/pt.txt 2>&1; echo EXIT=$? >> /tmp/pt.txt; tail -3 /tmp/pt.txt
```

Use `python -P -m graphite impact src/graphite/<file>.py` to find the owning tests when the name is not obvious.

- [ ] **Step 3: Commit per file or per small cluster**

```bash
git add src/graphite/<file>.py tests/test_<area>.py
git commit -m "types: <file> passes mypy (<n> narrowings, <m> annotations, <k> ignores with codes)"
```

- [ ] **Step 4: Whole-tree gate**

```bash
$DEV -m mypy > /tmp/mypy-final.txt 2>&1; echo EXIT=$? >> /tmp/mypy-final.txt; tail -2 /tmp/mypy-final.txt
$DEV -m pytest -q > /tmp/pt-all.txt 2>&1; echo EXIT=$? >> /tmp/pt-all.txt; tail -2 /tmp/pt-all.txt
```

Expected: `Success: no issues found in 106 source files` / `EXIT=0`, and the suite passes.

- [ ] **Step 5: Count the ignores added and check each has a code**

```bash
git diff main --unified=0 -- src | grep '^+' | grep -c 'type: ignore'
git diff main --unified=0 -- src | grep '^+' | grep 'type: ignore' | grep -vc 'type: ignore\['
```

Expected second number: `0`.

### Task D3: `py.typed`, classifier, CI gate

**Files:**
- Create: `src/graphite/py.typed` (empty)
- Modify: `pyproject.toml` (classifiers), `.github/workflows/ci.yml` (lint job), `scripts/verify_artifact.py` (assert `py.typed` is packaged)

- [ ] **Step 1: Write the failing packaging test**

In `scripts/verify_artifact.py`, in the wheel inspection, add:

```python
if not any(name.endswith("graphite/py.typed") for name in wheel_names):
    violations.append("wheel does not ship graphite/py.typed (PEP 561 marker)")
```

(`wheel_names` is the list the script already builds from `zipfile.ZipFile(wheel).namelist()`; use the existing variable name in that function.)

- [ ] **Step 2: Run it to see it fail**

```bash
$DEV -m build --wheel --outdir /tmp/wt > /tmp/wt.log 2>&1; echo EXIT=$? >> /tmp/wt.log
$DEV scripts/verify_artifact.py /tmp/wt
```

Expected: the new violation printed, non-zero exit. (If `build` is not in the dev venv, use `F:\Projects\.venvs\graphite-build\Scripts\python.exe -m build`.)

- [ ] **Step 3: Add the marker and classifier**

```bash
: > src/graphite/py.typed
```

In `pyproject.toml` classifiers add `"Typing :: Typed"`.

- [ ] **Step 4: Rebuild and re-verify**

Same commands as Step 2; expected: no violations, exit 0.

- [ ] **Step 5: Gate in CI**

In `ci.yml` `lint` job, after Ruff:

```yaml
      - name: Mypy
        run: python -m mypy
```

- [ ] **Step 6: Commit, push, watch, merge**

```bash
git add src/graphite/py.typed pyproject.toml .github/workflows/ci.yml scripts/verify_artifact.py
git commit -m "types: ship py.typed and gate mypy in CI (PRD WS-D)"
git push -u origin prod/ws-d-type-gate > /tmp/push-d.log 2>&1; echo EXIT=$? >> /tmp/push-d.log
```

Watch the run; then merge with `Merge prod/ws-d-type-gate: src/graphite passes mypy, py.typed shipped, lint job gates it (PRD WS-D)` and push `main`.

---

## WS-C — Coverage floor (D3)

### Task C1: Relative coverage paths and three-OS collection

**Files:**
- Modify: `pyproject.toml` (`[tool.coverage.run]`), `.github/workflows/ci.yml` (`test` job steps)

- [ ] **Step 1: Branch**

```bash
git switch -c prod/ws-c-coverage-floor main
```

- [ ] **Step 2: Make coverage data combinable across machines**

In `pyproject.toml`:

```toml
[tool.coverage.run]
source = ["src/graphite"]
branch = true
relative_files = true
```

- [ ] **Step 3: Collect on the 3.12 leg of each OS**

In the `test` job replace the `Test` step with:

```yaml
      - name: Test
        if: matrix.python != '3.12'
        run: python -m pytest -q --timeout=120 --timeout-method=thread
      - name: Test with coverage
        if: matrix.python == '3.12'
        env:
          COVERAGE_FILE: .coverage.${{ matrix.os }}
        run: python -m pytest -q --timeout=120 --timeout-method=thread --cov --cov-branch --cov-report=
      - name: Upload coverage data
        if: matrix.python == '3.12'
        uses: actions/upload-artifact@v4
        with:
          name: coverage-${{ matrix.os }}
          path: .coverage.${{ matrix.os }}
          include-hidden-files: true
          if-no-files-found: error
```

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml .github/workflows/ci.yml
git commit -m "ci: collect branch coverage on the 3.12 leg of each OS"
```

### Task C2: Combine job with a floor, set from the first measurement

**Files:**
- Modify: `.github/workflows/ci.yml` (new `coverage` job), `CONTRIBUTING.md` (floor policy)

- [ ] **Step 1: Add the job with a placeholder-free first floor of 0**

```yaml
  coverage:
    needs: test
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Install coverage
        run: python -m pip install "coverage[toml]>=7.6"
      - uses: actions/download-artifact@v4
        with:
          pattern: coverage-*
          merge-multiple: true
      - name: Combine the three OS runs
        run: |
          set -euo pipefail
          ls -la .coverage.*
          python -m coverage combine .coverage.*
          python -m coverage json -o coverage.json
          python -m coverage report --format=markdown > coverage.md
          cat coverage.md >> "$GITHUB_STEP_SUMMARY"
      - name: Enforce the floor
        # COVERAGE_FLOOR is the integer part of the first combined run and only
        # ever rises; lowering it needs a CHANGELOG entry saying why.
        env:
          COVERAGE_FLOOR: "0"
        run: python -m coverage report --fail-under="$COVERAGE_FLOOR" | tail -3
      - uses: actions/upload-artifact@v4
        with:
          name: coverage-report
          path: |
            coverage.json
            coverage.md
```

- [ ] **Step 2: Commit, push, read the combined number**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: combine coverage across OS legs and enforce a floor"
git push -u origin prod/ws-c-coverage-floor > /tmp/push-c.log 2>&1; echo EXIT=$? >> /tmp/push-c.log
```

After the run: open the job summary (`gh run view <id> --job <coverage-job-id> --log | grep -A3 TOTAL`) and note the combined percentage P.

- [ ] **Step 3: Negative control — floor above the measurement must fail**

Set `COVERAGE_FLOOR: "<floor(P)+1>"`, commit `ci: coverage negative control (floor above measurement)`, push, confirm the `coverage` job FAILS with `Coverage failure: total of P is less than fail-under=…`. Record the run id in the next commit message.

- [ ] **Step 4: Set the real floor**

Set `COVERAGE_FLOOR: "<floor(P)>"`, commit:

```bash
git commit -am "ci: coverage floor <floor(P)> (first combined measurement P%; negative control run <id> failed as required)"
```

Push; confirm green.

- [ ] **Step 5: Write the policy**

In `CONTRIBUTING.md` under "Testing and quality gates" add:

```markdown
### Coverage floor

CI combines branch coverage from the 3.12 leg of every OS and fails under
`COVERAGE_FLOOR` in `.github/workflows/ci.yml`. The floor is the integer part
of the measured total; raise it when a release measures higher, and never
lower it without a CHANGELOG entry saying what was removed and why.
```

Commit, push, watch, then merge: `Merge prod/ws-c-coverage-floor: three-OS coverage combined in CI with an enforced floor (PRD WS-C)`.

---

## WS-B — Security in CI and governance (D2, D8)

### Task B1: `security` job running both aramid gates unmodified

**Files:**
- Modify: `.github/workflows/ci.yml` (new `security` job)

- [ ] **Step 1: Branch**

```bash
git switch -c prod/ws-b-security-governance main
```

- [ ] **Step 2: Add the job**

```yaml
  security:
    # Runs the SAME gates the local git hooks run, against the committed
    # aramid.toml, unmodified. Windows because `[tests].command` there points
    # at `../.venvs/graphite-dev/Scripts/python.exe`, the real venv layout on
    # Windows; the job creates that venv so the configuration needs no patch.
    runs-on: windows-latest
    timeout-minutes: 40
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v6
        with:
          python-version: "3.14"
          cache: pip
          cache-dependency-path: pyproject.toml
      - name: Create the dev venv aramid.toml points at
        shell: bash
        run: |
          set -euo pipefail
          python -m venv ../.venvs/graphite-dev
          ../.venvs/graphite-dev/Scripts/python.exe -m pip install -e ".[dev,mcp]"
      - name: Install aramid
        run: python -m pip install "aramid==0.5.1"
      - name: CI-only keys the repo leaves unset
        shell: bash
        # User layer of aramid's defaults <- user <- repo merge. Nothing in the
        # repository is patched; the LLM reviewer has no provider in CI.
        run: |
          mkdir -p ~/.aramid
          printf '[llm]\nenabled = false\n' > ~/.aramid/config.toml
      - name: aramid doctor
        run: aramid doctor
      - name: Pre-commit gate (gitleaks, ruff security rules, shadow)
        shell: bash
        run: |
          aramid check --gate pre-commit --all --strict --json > precommit.json; echo "EXIT=$?" > precommit.exit
          cat precommit.exit
          python - <<'PY'
          import json
          d = json.load(open("precommit.json", encoding="utf-8"))
          ran = set(d.get("tools_ran", []))
          need = {"gitleaks", "ruff", "shadow"}
          assert need <= ran, f"pre-commit gate did not run {need - ran}; ran={sorted(ran)}"
          print("pre-commit tools_ran:", sorted(ran))
          PY
          grep -q '^EXIT=0$' precommit.exit
      - name: Pre-push gate (gitleaks, semgrep, tests)
        shell: bash
        run: |
          aramid check --gate pre-push --all --strict --json > prepush.json; echo "EXIT=$?" > prepush.exit
          cat prepush.exit
          python - <<'PY'
          import json
          d = json.load(open("prepush.json", encoding="utf-8"))
          ran = set(d.get("tools_ran", []))
          need = {"gitleaks", "semgrep"}
          assert need <= ran, f"pre-push gate did not run {need - ran}; ran={sorted(ran)}"
          assert any(t in ran for t in ("pytest", "python.exe", "tests")), f"tests slot did not run; ran={sorted(ran)}"
          print("pre-push tools_ran:", sorted(ran))
          PY
          grep -q '^EXIT=0$' prepush.exit
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: aramid-gate-reports
          path: |
            precommit.json
            prepush.json
```

If `[llm]` is not the key aramid uses, `aramid doctor` output on the first run names the reviewer's config section; adjust the printf to the documented key and record the correction in the commit message.

- [ ] **Step 3: Commit, push, read the first run**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run both aramid gates unmodified in a security job"
git push -u origin prod/ws-b-security-governance > /tmp/push-b.log 2>&1; echo EXIT=$? >> /tmp/push-b.log
```

Read the `security` job log. Expected: both `tools_ran` assertions print, both `EXIT=0`. If a tool is missing from `tools_ran`, the job must be red — fix the bootstrap (`aramid doctor` output says which tool), never relax the assertion.

- [ ] **Step 4: Known-positive control**

```bash
printf 'import os\nos.system("echo shadow")\n' > graphite.py
git add graphite.py
git commit -m "ci: security known-positive control (DELETE THIS COMMIT'S FILE)"
git push > /tmp/push-b2.log 2>&1; echo EXIT=$? >> /tmp/push-b2.log
```

Expected: the local pre-commit hook may already block this (the `shadow` runner is armed). If it does, that is the local control; bypass ONLY for this control with `git commit --no-verify`, push, and confirm the CI `security` job fails on `shadow`. Then:

```bash
git rm graphite.py
git commit -m "ci: remove the security known-positive plant (CI run <id> went red on shadow as required)"
git push > /tmp/push-b3.log 2>&1; echo EXIT=$? >> /tmp/push-b3.log
```

Confirm green.

### Task B2: Governance files and author metadata

**Files:**
- Create: `SECURITY.md`, `.github/CODEOWNERS`, `.github/ISSUE_TEMPLATE/bug_report.yml`, `.github/ISSUE_TEMPLATE/feature_request.yml`, `.github/ISSUE_TEMPLATE/config.yml`, `.github/dependabot.yml`
- Modify: `pyproject.toml` (`authors`), `tests/test_documentation.py` (new test)

- [ ] **Step 1: Write the failing documentation test**

Append to `tests/test_documentation.py`:

```python
def test_governance_surfaces_exist_and_point_at_each_other() -> None:
    root = Path(__file__).resolve().parents[1]
    security = (root / "SECURITY.md").read_text(encoding="utf-8")
    assert "Private vulnerability reporting" in security
    assert "1.x" in security
    assert (root / ".github" / "CODEOWNERS").read_text(encoding="utf-8").strip().startswith("* @")
    templates = root / ".github" / "ISSUE_TEMPLATE"
    assert (templates / "bug_report.yml").is_file()
    assert (templates / "feature_request.yml").is_file()
    config = (templates / "config.yml").read_text(encoding="utf-8")
    assert "SECURITY.md" in config
    dependabot = (root / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "github-actions" in dependabot and '"pip"' in dependabot
    import tomllib
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["authors"] and project["authors"][0]["email"].endswith("@gmail.com")
```

Run: `$DEV -m pytest tests/test_documentation.py -k governance -q` → FAIL (`SECURITY.md` missing).

- [ ] **Step 2: Create the files**

`SECURITY.md`:

```markdown
# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 1.x | yes — fixes ship in the next patch release |
| < 1.0 | no |

## Reporting a vulnerability

Use GitHub **Private vulnerability reporting** on this repository
(Security → Report a vulnerability). Do not open a public issue or pull
request for an exploitable defect; `CONTRIBUTING.md` already forbids
publishing exploit details. Include the graphite version
(`graphite --version` prints the version and engine fingerprint), the
platform, and the smallest reproduction you have.

You will get an acknowledgement within 7 days and a fix or a documented
mitigation before any public disclosure. Reports are handled by the
maintainer named in `.github/CODEOWNERS`.

## What graphite treats as untrusted

Repository files, file names, symlinks, Git metadata, generated graph data
and model output are all untrusted input (see "Security expectations" in
`CONTRIBUTING.md`). A finding that any of them can escape path containment,
run through a shell, or reach an unbounded read is in scope.
```

`.github/CODEOWNERS`:

```
* @jared0565
```

`.github/ISSUE_TEMPLATE/bug_report.yml`:

```yaml
name: Bug report
description: Something graphite does is wrong
labels: [bug]
body:
  - type: input
    id: version
    attributes:
      label: graphite --version
      description: Paste the full line (version and engine fingerprint).
    validations:
      required: true
  - type: dropdown
    id: platform
    attributes:
      label: Platform
      options: [Windows, Linux, macOS]
    validations:
      required: true
  - type: textarea
    id: repro
    attributes:
      label: Smallest reproduction
      description: The command you ran and the repository shape it ran against.
    validations:
      required: true
  - type: textarea
    id: expected
    attributes:
      label: Expected vs actual
    validations:
      required: true
```

`.github/ISSUE_TEMPLATE/feature_request.yml`:

```yaml
name: Feature request
description: Something graphite should do
labels: [enhancement]
body:
  - type: textarea
    id: problem
    attributes:
      label: The problem this solves
    validations:
      required: true
  - type: textarea
    id: proposal
    attributes:
      label: Proposed behaviour
      description: Include the command or output you expect. Compatibility promises are in docs/compatibility.md.
    validations:
      required: true
```

`.github/ISSUE_TEMPLATE/config.yml`:

```yaml
blank_issues_enabled: true
contact_links:
  - name: Security vulnerability
    url: https://github.com/jared0565/graphite/security/advisories/new
    about: Report privately — see SECURITY.md. Never in a public issue.
```

`.github/dependabot.yml`:

```yaml
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
```

`pyproject.toml`, under `[project]`:

```toml
authors = [{ name = "jared0565", email = "jared0565@gmail.com" }]
```

- [ ] **Step 3: Run the test, then the doc suite**

```bash
$DEV -m pytest tests/test_documentation.py -q > /tmp/doc.txt 2>&1; echo EXIT=$? >> /tmp/doc.txt; tail -2 /tmp/doc.txt
```

Expected PASS.

- [ ] **Step 4: Commit, push, merge**

```bash
git add SECURITY.md .github/CODEOWNERS .github/ISSUE_TEMPLATE .github/dependabot.yml pyproject.toml tests/test_documentation.py
git commit -m "governance: SECURITY.md, CODEOWNERS, issue templates, dependabot, authors (PRD WS-B)"
```

Push, watch, merge: `Merge prod/ws-b-security-governance: aramid gates run in CI; governance surfaces (PRD WS-B)`.

**Operator step (record in the merge commit body that it is pending):** enable Private vulnerability reporting — `gh api -X PUT repos/jared0565/graphite/private-vulnerability-reporting`; verify with `gh api repos/jared0565/graphite/private-vulnerability-reporting` → `{"enabled": true}`.

---

## WS-F — POSIX daemon supervision (D5)

### Task F1: Shared launch argv builder

**Files:**
- Create: `src/graphite/daemon_launch.py`
- Modify: `src/graphite/windows_task.py:91-134` (`daemon_task_command` delegates)
- Test: `tests/test_daemon_launch.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class DaemonLaunch:
      interpreter: Path
      arguments: tuple[str, ...]   # begins ("-P", "-m", "graphite", "daemon", <base>)
      working_dir: Path
      @property
      def argv(self) -> tuple[str, ...]: ...   # (str(interpreter), *arguments)

  def daemon_launch(base_path: Path, *, interpreter: str | None = None,
                    scan_interval: float = 15.0, discover_interval: float = 90.0,
                    max_projects: int = 128, max_depth: int = 6,
                    max_builds_per_cycle: int = 1, build_timeout: float = 240.0,
                    debounce: float = 1.0) -> DaemonLaunch
  ```

- [ ] **Step 1: Branch and write the failing test**

```bash
git switch -c prod/ws-f-posix-daemon main
```

`tests/test_daemon_launch.py`:

```python
"""The one argv builder every daemon launcher shares."""
from __future__ import annotations

import sys
from pathlib import Path

from graphite.daemon_launch import daemon_launch


def test_launch_argv_begins_with_the_interpreter_and_safe_path(tmp_path: Path) -> None:
    base = tmp_path / "Projects Root"
    base.mkdir()
    launch = daemon_launch(base)
    assert launch.interpreter == Path(sys.executable)
    assert launch.arguments[:5] == ("-P", "-m", "graphite", "daemon", str(base.resolve()))
    assert launch.working_dir == base.resolve()
    assert launch.argv[0] == sys.executable
    assert "-B" not in launch.arguments


def test_launch_argv_renders_numbers_without_trailing_zero(tmp_path: Path) -> None:
    launch = daemon_launch(tmp_path, build_timeout=240.0, debounce=1.5)
    args = launch.arguments
    assert args[args.index("--build-timeout") + 1] == "240"
    assert args[args.index("--debounce") + 1] == "1.5"


def test_windows_task_command_uses_the_shared_builder(tmp_path: Path) -> None:
    from graphite.windows_task import daemon_task_command
    command = daemon_task_command(tmp_path)
    assert command.arguments == daemon_launch(tmp_path).arguments
```

Run: `$DEV -m pytest tests/test_daemon_launch.py -q` → FAIL (`No module named graphite.daemon_launch`).

- [ ] **Step 2: Implement**

`src/graphite/daemon_launch.py`:

```python
"""The argument vector every daemon launcher shares.

Windows (Scheduled Task), Linux (systemd user unit) and macOS (launchd agent)
all start the same process; only the supervisor differs. Building the argv in
one place keeps `-P` -- the flag that keeps the working directory off
`sys.path[0]` -- in every launcher by construction.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .windows_task import resolve_launcher_interpreter


@dataclass(frozen=True)
class DaemonLaunch:
    interpreter: Path
    arguments: tuple[str, ...]
    working_dir: Path

    @property
    def argv(self) -> tuple[str, ...]:
        return (str(self.interpreter), *self.arguments)


def _fmt_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def daemon_launch(
    base_path: Path,
    *,
    interpreter: str | None = None,
    scan_interval: float = 15.0,
    discover_interval: float = 90.0,
    max_projects: int = 128,
    max_depth: int = 6,
    max_builds_per_cycle: int = 1,
    build_timeout: float = 240.0,
    debounce: float = 1.0,
) -> DaemonLaunch:
    base = base_path.resolve()
    # `-P` is the whole point. `-B` is deliberately absent: it only suppresses
    # bytecode and does nothing to `sys.path`.
    arguments = (
        "-P", "-m", "graphite", "daemon", str(base),
        "--scan-interval", _fmt_number(scan_interval),
        "--discover-interval", _fmt_number(discover_interval),
        "--max-projects", str(max_projects),
        "--max-depth", str(max_depth),
        "--max-builds-per-cycle", str(max_builds_per_cycle),
        "--build-timeout", _fmt_number(build_timeout),
        "--debounce", _fmt_number(debounce),
    )
    return DaemonLaunch(
        interpreter=resolve_launcher_interpreter(interpreter),
        arguments=arguments,
        working_dir=base,
    )
```

Then in `windows_task.py` replace the body of `daemon_task_command` with:

```python
    launch = daemon_launch(
        base_path,
        interpreter=graphite_executable,
        scan_interval=scan_interval,
        discover_interval=discover_interval,
        max_projects=max_projects,
        max_depth=max_depth,
        max_builds_per_cycle=max_builds_per_cycle,
        build_timeout=build_timeout,
        debounce=debounce,
    )
    return TaskCommand(executable=launch.interpreter, arguments=launch.arguments, working_dir=launch.working_dir)
```

with a lazy import inside the function (`from .daemon_launch import daemon_launch`) to avoid the circular import with `resolve_launcher_interpreter`. Delete `_fmt_number` from `windows_task.py` if nothing else uses it (check with `python -P -m graphite query "callers _fmt_number"`).

- [ ] **Step 3: Run the new and the Windows tests**

```bash
$DEV -m pytest tests/test_daemon_launch.py tests/test_windows_task.py tests/test_windows_startup.py -q
```

Expected PASS.

- [ ] **Step 4: Commit**

```bash
git add src/graphite/daemon_launch.py src/graphite/windows_task.py tests/test_daemon_launch.py
git commit -m "daemon: one shared launch argv builder; Windows task delegates to it"
```

### Task F2: systemd user unit

**Files:**
- Create: `src/graphite/systemd_unit.py`
- Test: `tests/test_systemd_unit.py`

**Interfaces:**
- Produces:
  ```python
  DEFAULT_UNIT_NAME = "graphite-daemon"
  def require_linux() -> None
  def unit_path(name: str = DEFAULT_UNIT_NAME, *, home: Path | None = None) -> Path
  def render_unit(launch: DaemonLaunch, *, description: str = "Graphite daemon") -> str
  def install_unit(launch: DaemonLaunch, *, name: str = DEFAULT_UNIT_NAME, home: Path | None = None,
                   start_now: bool = True, run=subprocess.run) -> dict[str, object]
  def query_unit(name: str = DEFAULT_UNIT_NAME, *, run=subprocess.run) -> dict[str, object]
  def uninstall_unit(name: str = DEFAULT_UNIT_NAME, *, home: Path | None = None, run=subprocess.run) -> dict[str, object]
  def systemd_quote(arg: str) -> str
  ```

- [ ] **Step 1: Write the failing tests**

`tests/test_systemd_unit.py`:

```python
"""systemd user unit for the daemon: rendering, quoting, and the command sequence."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from graphite.daemon_launch import daemon_launch
from graphite.systemd_unit import (
    DEFAULT_UNIT_NAME,
    install_unit,
    query_unit,
    render_unit,
    systemd_quote,
    uninstall_unit,
    unit_path,
)


def test_render_unit_carries_safe_path_and_restart_policy(tmp_path: Path) -> None:
    base = tmp_path / "Projects Root"
    base.mkdir()
    text = render_unit(daemon_launch(base))
    assert "[Unit]" in text and "[Service]" in text and "[Install]" in text
    assert f"ExecStart={systemd_quote(sys.executable)} -P -m graphite daemon {systemd_quote(str(base.resolve()))}" in text
    assert f"WorkingDirectory={base.resolve()}" in text
    assert "Restart=on-failure" in text
    assert "Environment=PYTHONSAFEPATH=1" in text
    assert "WantedBy=default.target" in text


def test_systemd_quote_wraps_spaces_and_escapes_quotes() -> None:
    assert systemd_quote("plain") == "plain"
    assert systemd_quote("has space") == '"has space"'
    assert systemd_quote('say "hi"') == '"say \\"hi\\""'


def test_unit_path_lives_under_the_user_config_dir(tmp_path: Path) -> None:
    assert unit_path(home=tmp_path) == tmp_path / ".config" / "systemd" / "user" / f"{DEFAULT_UNIT_NAME}.service"


class FakeRun:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, cmd, capture_output, text, check):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def test_install_writes_the_unit_then_reloads_and_enables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.systemd_unit.platform.system", lambda: "Linux")
    run = FakeRun()
    payload = install_unit(daemon_launch(tmp_path), home=tmp_path, run=run)
    assert payload["ok"] is True
    assert unit_path(home=tmp_path).read_text(encoding="utf-8").startswith("[Unit]")
    assert run.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", f"{DEFAULT_UNIT_NAME}.service"],
    ]


def test_install_without_start_only_enables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.systemd_unit.platform.system", lambda: "Linux")
    run = FakeRun()
    install_unit(daemon_launch(tmp_path), home=tmp_path, start_now=False, run=run)
    assert run.calls[-1] == ["systemctl", "--user", "enable", f"{DEFAULT_UNIT_NAME}.service"]


def test_query_reports_active_state_and_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.systemd_unit.platform.system", lambda: "Linux")
    run = FakeRun(stdout="ActiveState=active\nSubState=running\nMainPID=4242\n")
    payload = query_unit(run=run)
    assert payload["exists"] is True
    assert payload["unit"] == {"ActiveState": "active", "SubState": "running", "MainPID": "4242"}


def test_query_absent_unit_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.systemd_unit.platform.system", lambda: "Linux")
    run = FakeRun(stdout="ActiveState=inactive\nSubState=dead\nMainPID=0\nLoadState=not-found\n")
    payload = query_unit(run=run)
    assert payload["exists"] is False


def test_uninstall_disables_then_removes_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.systemd_unit.platform.system", lambda: "Linux")
    run = FakeRun()
    install_unit(daemon_launch(tmp_path), home=tmp_path, run=run)
    payload = uninstall_unit(home=tmp_path, run=run)
    assert payload["ok"] is True
    assert not unit_path(home=tmp_path).exists()
    assert run.calls[-2:] == [
        ["systemctl", "--user", "disable", "--now", f"{DEFAULT_UNIT_NAME}.service"],
        ["systemctl", "--user", "daemon-reload"],
    ]


def test_refuses_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.systemd_unit.platform.system", lambda: "Darwin")
    with pytest.raises(RuntimeError, match="only available on Linux"):
        query_unit(run=FakeRun())


@pytest.mark.skipif(sys.platform != "linux", reason="systemd-analyze verify runs on the Linux leg only")
def test_rendered_unit_passes_systemd_analyze(tmp_path: Path) -> None:
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze not installed on this Linux host")
    unit = tmp_path / f"{DEFAULT_UNIT_NAME}.service"
    unit.write_text(render_unit(daemon_launch(tmp_path)), encoding="utf-8")
    result = subprocess.run([analyze, "--user", "verify", str(unit)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
```

Run: `$DEV -m pytest tests/test_systemd_unit.py -q` → FAIL (module missing).

- [ ] **Step 2: Implement**

`src/graphite/systemd_unit.py`:

```python
"""systemd user unit for the Graphite daemon (Linux)."""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import Callable, Sequence

from .daemon_launch import DaemonLaunch
from .graph_io import replace_file

DEFAULT_UNIT_NAME = "graphite-daemon"

Runner = Callable[..., subprocess.CompletedProcess[str]]


def require_linux() -> None:
    if platform.system().lower() != "linux":
        raise RuntimeError("systemd user unit integration is only available on Linux")


def systemd_quote(arg: str) -> str:
    """Quote one ExecStart argument per systemd.service(5) rules."""
    if arg and not any(ch.isspace() for ch in arg) and '"' not in arg and "'" not in arg:
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unit_path(name: str = DEFAULT_UNIT_NAME, *, home: Path | None = None) -> Path:
    root = home if home is not None else Path.home()
    return root / ".config" / "systemd" / "user" / f"{name}.service"


def render_unit(launch: DaemonLaunch, *, description: str = "Graphite daemon") -> str:
    exec_start = " ".join(systemd_quote(part) for part in launch.argv)
    return (
        "[Unit]\n"
        f"Description={description}\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        f"WorkingDirectory={launch.working_dir}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "Environment=PYTHONSAFEPATH=1\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _payload(result: subprocess.CompletedProcess[str], *, command: Sequence[str]) -> dict[str, object]:
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": (result.stdout or "").strip(),
        "stderr": (result.stderr or "").strip(),
        "command": list(command),
    }


def _systemctl(args: Sequence[str], run: Runner) -> dict[str, object]:
    cmd = ["systemctl", "--user", *args]
    return _payload(run(cmd, capture_output=True, text=True, check=False), command=cmd)


def install_unit(
    launch: DaemonLaunch,
    *,
    name: str = DEFAULT_UNIT_NAME,
    home: Path | None = None,
    start_now: bool = True,
    run: Runner = subprocess.run,
) -> dict[str, object]:
    require_linux()
    path = unit_path(name, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, render_unit(launch).encode("utf-8"))
    steps = [_systemctl(["daemon-reload"], run)]
    enable = ["enable", "--now", f"{name}.service"] if start_now else ["enable", f"{name}.service"]
    steps.append(_systemctl(enable, run))
    return {"ok": all(bool(s["ok"]) for s in steps), "unit_path": str(path), "steps": steps}


def query_unit(name: str = DEFAULT_UNIT_NAME, *, run: Runner = subprocess.run) -> dict[str, object]:
    require_linux()
    payload = _systemctl(["show", "-p", "ActiveState,SubState,MainPID,LoadState", f"{name}.service"], run)
    fields: dict[str, str] = {}
    for line in str(payload["stdout"]).splitlines():
        key, _, value = line.partition("=")
        if key:
            fields[key] = value
    load = fields.pop("LoadState", "")
    payload["exists"] = payload["ok"] and load not in ("", "not-found")
    payload["unit"] = fields
    return payload


def uninstall_unit(
    name: str = DEFAULT_UNIT_NAME,
    *,
    home: Path | None = None,
    run: Runner = subprocess.run,
) -> dict[str, object]:
    require_linux()
    steps = [_systemctl(["disable", "--now", f"{name}.service"], run)]
    path = unit_path(name, home=home)
    removed = False
    if path.exists():
        path.unlink()
        removed = True
    steps.append(_systemctl(["daemon-reload"], run))
    return {"ok": all(bool(s["ok"]) for s in steps), "unit_path": str(path), "removed": removed, "steps": steps}
```

If `replace_file` in `graph_io.py` has a different signature (check with `python -P -m graphite search "replace_file"` and read it), call it the way `windows_startup.py` writes its launcher and note the actual signature in the commit.

- [ ] **Step 3: Run tests, commit**

```bash
$DEV -m pytest tests/test_systemd_unit.py -q
git add src/graphite/systemd_unit.py tests/test_systemd_unit.py
git commit -m "daemon: systemd user unit installer for Linux"
```

### Task F3: launchd agent

**Files:**
- Create: `src/graphite/launchd_agent.py`
- Test: `tests/test_launchd_agent.py`

**Interfaces:**
- Produces:
  ```python
  DEFAULT_LABEL = "com.graphite.daemon"
  def require_macos() -> None
  def agent_path(label: str = DEFAULT_LABEL, *, home: Path | None = None) -> Path
  def render_plist(launch: DaemonLaunch, *, label: str = DEFAULT_LABEL, home: Path | None = None) -> bytes
  def install_agent(launch, *, label=DEFAULT_LABEL, home=None, uid: int | None = None, run=subprocess.run) -> dict
  def query_agent(label=DEFAULT_LABEL, *, uid=None, run=subprocess.run) -> dict
  def uninstall_agent(label=DEFAULT_LABEL, *, home=None, uid=None, run=subprocess.run) -> dict
  ```

- [ ] **Step 1: Write the failing tests**

`tests/test_launchd_agent.py`:

```python
"""launchd agent for the daemon: plist rendering and the launchctl sequence."""
from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from graphite.daemon_launch import daemon_launch
from graphite.launchd_agent import (
    DEFAULT_LABEL,
    agent_path,
    install_agent,
    query_agent,
    render_plist,
    uninstall_agent,
)


def test_plist_round_trips_with_the_launch_argv(tmp_path: Path) -> None:
    base = tmp_path / "Projects Root"
    base.mkdir()
    launch = daemon_launch(base)
    data = plistlib.loads(render_plist(launch, home=tmp_path))
    assert data["Label"] == DEFAULT_LABEL
    assert data["ProgramArguments"] == list(launch.argv)
    assert data["ProgramArguments"][1:5] == ["-P", "-m", "graphite", "daemon"]
    assert data["WorkingDirectory"] == str(base.resolve())
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["EnvironmentVariables"] == {"PYTHONSAFEPATH": "1"}
    assert data["StandardOutPath"] == str(tmp_path / "Library" / "Logs" / "graphite" / "daemon.log")
    assert data["StandardErrorPath"] == str(tmp_path / "Library" / "Logs" / "graphite" / "daemon.err")


def test_agent_path_lives_under_launch_agents(tmp_path: Path) -> None:
    assert agent_path(home=tmp_path) == tmp_path / "Library" / "LaunchAgents" / f"{DEFAULT_LABEL}.plist"


class FakeRun:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, cmd, capture_output, text, check):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def test_install_writes_plist_and_bootstraps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.launchd_agent.platform.system", lambda: "Darwin")
    run = FakeRun()
    payload = install_agent(daemon_launch(tmp_path), home=tmp_path, uid=501, run=run)
    assert payload["ok"] is True
    path = agent_path(home=tmp_path)
    assert plistlib.loads(path.read_bytes())["Label"] == DEFAULT_LABEL
    assert (tmp_path / "Library" / "Logs" / "graphite").is_dir()
    assert run.calls == [["launchctl", "bootstrap", "gui/501", str(path)]]


def test_query_parses_state_and_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.launchd_agent.platform.system", lambda: "Darwin")
    run = FakeRun(stdout="com.graphite.daemon = {\n\tactive count = 1\n\tpid = 777\n\tstate = running\n}\n")
    payload = query_agent(uid=501, run=run)
    assert payload["exists"] is True
    assert payload["agent"] == {"pid": "777", "state": "running"}
    assert run.calls == [["launchctl", "print", f"gui/501/{DEFAULT_LABEL}"]]


def test_query_absent_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.launchd_agent.platform.system", lambda: "Darwin")
    payload = query_agent(uid=501, run=FakeRun(returncode=113))
    assert payload["exists"] is False


def test_uninstall_boots_out_then_removes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.launchd_agent.platform.system", lambda: "Darwin")
    run = FakeRun()
    install_agent(daemon_launch(tmp_path), home=tmp_path, uid=501, run=run)
    payload = uninstall_agent(home=tmp_path, uid=501, run=run)
    assert payload["ok"] is True
    assert not agent_path(home=tmp_path).exists()
    assert run.calls[-1] == ["launchctl", "bootout", f"gui/501/{DEFAULT_LABEL}"]


def test_refuses_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("graphite.launchd_agent.platform.system", lambda: "Linux")
    with pytest.raises(RuntimeError, match="only available on macOS"):
        query_agent(uid=501, run=FakeRun())


@pytest.mark.skipif(sys.platform != "darwin", reason="plutil runs on the macOS leg only")
def test_rendered_plist_passes_plutil_lint(tmp_path: Path) -> None:
    plutil = shutil.which("plutil")
    assert plutil is not None, "plutil must exist on macOS"
    path = tmp_path / "agent.plist"
    path.write_bytes(render_plist(daemon_launch(tmp_path), home=tmp_path))
    result = subprocess.run([plutil, "-lint", str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
```

Run → FAIL (module missing).

- [ ] **Step 2: Implement**

`src/graphite/launchd_agent.py`:

```python
"""launchd agent for the Graphite daemon (macOS)."""
from __future__ import annotations

import os
import platform
import plistlib
import subprocess
from pathlib import Path
from typing import Callable, Sequence

from .daemon_launch import DaemonLaunch
from .graph_io import replace_file

DEFAULT_LABEL = "com.graphite.daemon"

Runner = Callable[..., subprocess.CompletedProcess[str]]


def require_macos() -> None:
    if platform.system().lower() != "darwin":
        raise RuntimeError("launchd agent integration is only available on macOS")


def _home(home: Path | None) -> Path:
    return home if home is not None else Path.home()


def _uid(uid: int | None) -> int:
    if uid is not None:
        return uid
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise RuntimeError("launchd agent integration needs a POSIX uid")
    return int(getuid())


def agent_path(label: str = DEFAULT_LABEL, *, home: Path | None = None) -> Path:
    return _home(home) / "Library" / "LaunchAgents" / f"{label}.plist"


def log_dir(*, home: Path | None = None) -> Path:
    return _home(home) / "Library" / "Logs" / "graphite"


def render_plist(launch: DaemonLaunch, *, label: str = DEFAULT_LABEL, home: Path | None = None) -> bytes:
    logs = log_dir(home=home)
    payload = {
        "Label": label,
        "ProgramArguments": list(launch.argv),
        "WorkingDirectory": str(launch.working_dir),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "EnvironmentVariables": {"PYTHONSAFEPATH": "1"},
        "StandardOutPath": str(logs / "daemon.log"),
        "StandardErrorPath": str(logs / "daemon.err"),
    }
    return plistlib.dumps(payload, sort_keys=True)


def _payload(result: subprocess.CompletedProcess[str], *, command: Sequence[str]) -> dict[str, object]:
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": (result.stdout or "").strip(),
        "stderr": (result.stderr or "").strip(),
        "command": list(command),
    }


def _launchctl(args: Sequence[str], run: Runner) -> dict[str, object]:
    cmd = ["launchctl", *args]
    return _payload(run(cmd, capture_output=True, text=True, check=False), command=cmd)


def install_agent(
    launch: DaemonLaunch,
    *,
    label: str = DEFAULT_LABEL,
    home: Path | None = None,
    uid: int | None = None,
    run: Runner = subprocess.run,
) -> dict[str, object]:
    require_macos()
    path = agent_path(label, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    log_dir(home=home).mkdir(parents=True, exist_ok=True)
    replace_file(path, render_plist(launch, label=label, home=home))
    step = _launchctl(["bootstrap", f"gui/{_uid(uid)}", str(path)], run)
    return {"ok": bool(step["ok"]), "agent_path": str(path), "steps": [step]}


def query_agent(label: str = DEFAULT_LABEL, *, uid: int | None = None, run: Runner = subprocess.run) -> dict[str, object]:
    require_macos()
    payload = _launchctl(["print", f"gui/{_uid(uid)}/{label}"], run)
    fields: dict[str, str] = {}
    if payload["ok"]:
        for line in str(payload["stdout"]).splitlines():
            key, sep, value = line.strip().partition(" = ")
            if sep and key in ("pid", "state"):
                fields[key] = value.strip()
    payload["exists"] = bool(payload["ok"])
    payload["agent"] = fields
    return payload


def uninstall_agent(
    label: str = DEFAULT_LABEL,
    *,
    home: Path | None = None,
    uid: int | None = None,
    run: Runner = subprocess.run,
) -> dict[str, object]:
    require_macos()
    path = agent_path(label, home=home)
    removed = False
    if path.exists():
        path.unlink()
        removed = True
    step = _launchctl(["bootout", f"gui/{_uid(uid)}/{label}"], run)
    return {"ok": bool(step["ok"]) or removed, "agent_path": str(path), "removed": removed, "steps": [step]}
```

- [ ] **Step 3: Run tests, commit**

```bash
$DEV -m pytest tests/test_launchd_agent.py -q
git add src/graphite/launchd_agent.py tests/test_launchd_agent.py
git commit -m "daemon: launchd agent installer for macOS"
```

### Task F4: CLI commands and platform-aware health

**Files:**
- Modify: `src/graphite/cli.py` (`_CANONICAL_COMMANDS` list near line 150; subparsers near lines 2952-2996; new `cmd_daemon_install_linux`, `cmd_daemon_uninstall_linux`, `cmd_daemon_install_macos`, `cmd_daemon_uninstall_macos`, `cmd_daemon_service_status`), `src/graphite/daemon_health.py:316-323` (`check_startup_launcher`)
- Test: `tests/test_daemon_service_cli.py`, `tests/test_daemon_health.py` (one new test)

- [ ] **Step 1: Write the failing CLI tests**

`tests/test_daemon_service_cli.py`:

```python
"""The POSIX daemon commands: refuse on the wrong platform, install via the modules."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite.cli import main


def test_install_linux_refuses_elsewhere(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    monkeypatch.setattr("graphite.systemd_unit.platform.system", lambda: "Windows")
    rc = main(["daemon-install-linux", str(tmp_path), "--json"])
    assert rc == 1
    assert "only available on Linux" in json.loads(capsys.readouterr().out)["error"]


def test_install_linux_calls_the_installer(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_install(launch, *, name, home, start_now, run):
        seen.update(base=launch.working_dir, name=name, start_now=start_now)
        return {"ok": True, "unit_path": "x", "steps": []}

    monkeypatch.setattr("graphite.cli.install_unit", fake_install)
    rc = main(["daemon-install-linux", str(tmp_path), "--no-start", "--json"])
    assert rc == 0
    assert seen == {"base": tmp_path.resolve(), "name": "graphite-daemon", "start_now": False}
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_install_macos_calls_the_installer(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_install(launch, *, label, home, uid, run):
        seen.update(base=launch.working_dir, label=label)
        return {"ok": True, "agent_path": "x", "steps": []}

    monkeypatch.setattr("graphite.cli.install_agent", fake_install)
    rc = main(["daemon-install-macos", str(tmp_path), "--json"])
    assert rc == 0
    assert seen == {"base": tmp_path.resolve(), "label": "com.graphite.daemon"}


def test_service_status_dispatches_by_platform(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("graphite.cli.platform.system", lambda: "Linux")
    monkeypatch.setattr("graphite.cli.query_unit", lambda name, run: {"ok": True, "exists": True, "unit": {"ActiveState": "active"}})
    rc = main(["daemon-service-status", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["platform"] == "linux" and out["exists"] is True


def test_capabilities_lists_the_new_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["capabilities", "--json"]) == 0
    commands = json.loads(capsys.readouterr().out)["commands"]
    for name in ("daemon-install-linux", "daemon-uninstall-linux", "daemon-install-macos", "daemon-uninstall-macos", "daemon-service-status"):
        assert name in commands
```

Run → FAIL (unknown subcommand).

- [ ] **Step 2: Implement in `cli.py`**

Imports at the top (module level, so tests can monkeypatch `graphite.cli.install_unit` etc.):

```python
import platform
from .daemon_launch import daemon_launch
from .systemd_unit import DEFAULT_UNIT_NAME, install_unit, query_unit, uninstall_unit
from .launchd_agent import DEFAULT_LABEL, install_agent, query_agent, uninstall_agent
```

Add the five names to `_CANONICAL_COMMANDS` beside the Windows ones. Command functions (place beside `cmd_daemon_install_windows`):

```python
def _emit(args: argparse.Namespace, payload: dict[str, object], *, head: str) -> int:
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[graphite] {head}: {'ok' if payload.get('ok') else 'failed'}")
        for step in payload.get("steps", []) or []:
            print(f"  - {' '.join(step['command'])}: rc={step['returncode']}")
    return 0 if payload.get("ok") else 1


def _refused(args: argparse.Namespace, exc: Exception, *, head: str) -> int:
    return _emit(args, {"ok": False, "error": str(exc)}, head=head)


def cmd_daemon_install_linux(args: argparse.Namespace) -> int:
    try:
        launch = daemon_launch(Path(args.base), interpreter=args.interpreter)
        payload = install_unit(launch, name=args.name, home=None, start_now=not args.no_start, run=subprocess.run)
    except (RuntimeError, ValueError, FileNotFoundError, OSError) as exc:
        return _refused(args, exc, head="daemon-install-linux")
    return _emit(args, payload, head="daemon-install-linux")


def cmd_daemon_uninstall_linux(args: argparse.Namespace) -> int:
    try:
        payload = uninstall_unit(args.name, home=None, run=subprocess.run)
    except (RuntimeError, OSError) as exc:
        return _refused(args, exc, head="daemon-uninstall-linux")
    return _emit(args, payload, head="daemon-uninstall-linux")


def cmd_daemon_install_macos(args: argparse.Namespace) -> int:
    try:
        launch = daemon_launch(Path(args.base), interpreter=args.interpreter)
        payload = install_agent(launch, label=args.label, home=None, uid=None, run=subprocess.run)
    except (RuntimeError, ValueError, FileNotFoundError, OSError) as exc:
        return _refused(args, exc, head="daemon-install-macos")
    return _emit(args, payload, head="daemon-install-macos")


def cmd_daemon_uninstall_macos(args: argparse.Namespace) -> int:
    try:
        payload = uninstall_agent(args.label, home=None, uid=None, run=subprocess.run)
    except (RuntimeError, OSError) as exc:
        return _refused(args, exc, head="daemon-uninstall-macos")
    return _emit(args, payload, head="daemon-uninstall-macos")


def cmd_daemon_service_status(args: argparse.Namespace) -> int:
    system = platform.system().lower()
    try:
        if system == "linux":
            payload = dict(query_unit(args.name, run=subprocess.run))
        elif system == "darwin":
            payload = dict(query_agent(args.label, uid=None, run=subprocess.run))
        elif system == "windows":
            from .windows_task import query_daemon_task
            from .windows_startup import startup_status
            payload = {"task": query_daemon_task(args.name, run=subprocess.run), "startup": startup_status(Path(args.base), name=args.name)}
            payload["ok"] = True
            payload["exists"] = bool(payload["task"].get("exists"))
        else:
            payload = {"ok": False, "exists": False, "error": f"unsupported platform: {system}"}
    except (RuntimeError, OSError) as exc:
        return _refused(args, exc, head="daemon-service-status")
    payload["platform"] = system
    return _emit(args, payload, head="daemon-service-status")
```

Subparsers, beside the Windows ones:

```python
    for name, fn, default_name in (
        ("daemon-install-linux", cmd_daemon_install_linux, DEFAULT_UNIT_NAME),
        ("daemon-install-macos", cmd_daemon_install_macos, DEFAULT_LABEL),
    ):
        p = sub.add_parser(name, help=f"Install the Graphite daemon as a {'systemd user unit' if 'linux' in name else 'launchd agent'}")
        p.add_argument("base", nargs="?", default=str(default_projects_root()), help="Projects root the daemon supervises")
        p.add_argument("--interpreter", default=None, help="Python interpreter for the launcher (never a console script)")
        p.add_argument("--no-start", action="store_true", help="Install and enable without starting now")
        p.add_argument("--json", action="store_true")
        if "linux" in name:
            p.add_argument("--name", default=default_name, help="Unit name (default: graphite-daemon)")
        else:
            p.add_argument("--label", default=default_name, help="Agent label (default: com.graphite.daemon)")
        p.set_defaults(func=fn)
    p = sub.add_parser("daemon-uninstall-linux", help="Disable and remove the Graphite daemon systemd user unit")
    p.add_argument("--name", default=DEFAULT_UNIT_NAME); p.add_argument("--json", action="store_true"); p.set_defaults(func=cmd_daemon_uninstall_linux)
    p = sub.add_parser("daemon-uninstall-macos", help="Boot out and remove the Graphite daemon launchd agent")
    p.add_argument("--label", default=DEFAULT_LABEL); p.add_argument("--json", action="store_true"); p.set_defaults(func=cmd_daemon_uninstall_macos)
    p = sub.add_parser("daemon-service-status", help="Report the daemon supervisor for this platform (systemd, launchd, or the Windows task)")
    p.add_argument("base", nargs="?", default=str(default_projects_root()))
    p.add_argument("--name", default=DEFAULT_UNIT_NAME); p.add_argument("--label", default=DEFAULT_LABEL); p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_daemon_service_status)
```

Match how the existing subparsers wire `func`/dispatch (read `cmd_daemon_install_windows`'s parser block first and copy its pattern exactly, including how `--json` is read).

- [ ] **Step 3: Platform-aware health**

Replace `check_startup_launcher` in `daemon_health.py`:

```python
def check_startup_launcher(base: Path, name: str) -> dict[str, Any]:
    system = platform.system().lower()
    try:
        if system == "windows":
            status = startup_status(base, name=name)
            return {"checked": True, "supported": True, **status}
        if system == "linux":
            from .systemd_unit import DEFAULT_UNIT_NAME, query_unit
            unit = query_unit(DEFAULT_UNIT_NAME)
            return {"checked": True, "supported": True, "installed": bool(unit.get("exists")), "supervisor": "systemd", "detail": unit.get("unit", {})}
        if system == "darwin":
            from .launchd_agent import DEFAULT_LABEL, query_agent
            agent = query_agent(DEFAULT_LABEL)
            return {"checked": True, "supported": True, "installed": bool(agent.get("exists")), "supervisor": "launchd", "detail": agent.get("agent", {})}
    except Exception as exc:
        return {"checked": True, "supported": True, "installed": False, "error": str(exc)}
    return {"checked": True, "supported": False, "installed": None, "error": f"startup check unsupported on {system}"}
```

Add to `tests/test_daemon_health.py`:

```python
def test_startup_check_consults_systemd_on_linux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("graphite.daemon_health.platform.system", lambda: "Linux")
    monkeypatch.setattr("graphite.systemd_unit.query_unit", lambda name, run=None: {"exists": True, "unit": {"ActiveState": "active"}})
    from graphite.daemon_health import check_startup_launcher
    result = check_startup_launcher(tmp_path, "graphite-daemon")
    assert result["installed"] is True and result["supervisor"] == "systemd"
```

(The lambda must accept the keyword the production call uses — `query_unit(DEFAULT_UNIT_NAME)` passes no `run`, so `run=None` default is enough.)

- [ ] **Step 4: Run the focused tests, then the suite**

```bash
$DEV -m pytest tests/test_daemon_service_cli.py tests/test_daemon_health.py tests/test_listing_surfaces.py tests/test_cli_version.py -q
$DEV -m pytest -q > /tmp/pt-f.txt 2>&1; echo EXIT=$? >> /tmp/pt-f.txt; tail -2 /tmp/pt-f.txt
```

`test_listing_surfaces.py` and any `_CANONICAL_COMMANDS` snapshot tests will name the new commands; update their expected lists.

- [ ] **Step 5: Commit, push, watch the Linux and macOS legs prove the validation tests RAN**

```bash
git add src/graphite/cli.py src/graphite/daemon_health.py tests/test_daemon_service_cli.py tests/test_daemon_health.py tests/test_listing_surfaces.py
git commit -m "daemon: daemon-install-linux/macos, daemon-service-status, platform-aware health (PRD WS-F)"
git push -u origin prod/ws-f-posix-daemon > /tmp/push-f.log 2>&1; echo EXIT=$? >> /tmp/push-f.log
```

Then on the ubuntu 3.12 and macos 3.12 legs: `gh run view <id> --job <job-id> --log | grep -E "systemd_analyze|plutil"` must show the test names as PASSED, not SKIPPED. If `systemd-analyze` is absent on the runner, install it in the workflow for Linux (`sudo apt-get install -y systemd`) — it is present on `ubuntu-latest` images.

- [ ] **Step 6: README section and merge**

Add to README "Machine-wide usage" after the Windows daemon paragraph:

```markdown
On Linux, `graphite daemon-install-linux <projects-root>` writes a systemd
**user** unit (`~/.config/systemd/user/graphite-daemon.service`), reloads,
enables and starts it; on macOS, `graphite daemon-install-macos
<projects-root>` writes a launchd agent
(`~/Library/LaunchAgents/com.graphite.daemon.plist`) and bootstraps it.
`graphite daemon-service-status` reports the supervisor on any platform, and
`daemon-uninstall-linux` / `daemon-uninstall-macos` reverse the install. A
systemd user unit runs only while you are logged in unless you enable
lingering (`loginctl enable-linger $USER`), which is a policy choice graphite
does not make for you.
```

Merge: `Merge prod/ws-f-posix-daemon: daemon supervision on Linux (systemd) and macOS (launchd) (PRD WS-F)`.

---

## WS-H — Scale: measured and declared (D9)

### Task H1: Synthetic repository generator

**Files:**
- Create: `benchmarks/synthetic_repo.py`
- Test: `tests/test_synthetic_repo.py`

**Interfaces:**
- Produces: `generate(root: Path, *, files: int, seed: int = 7) -> dict[str, int]` returning per-language counts; layout `<root>/{py,ts,js,go,rs}/...` with cross-file imports and calls.

- [ ] **Step 1: Failing test**

```python
from pathlib import Path
from benchmarks.synthetic_repo import generate


def test_generate_is_deterministic_and_cross_linked(tmp_path: Path) -> None:
    a = generate(tmp_path / "a", files=50, seed=3)
    b = generate(tmp_path / "b", files=50, seed=3)
    assert a == b and sum(a.values()) == 50
    py = sorted((tmp_path / "a" / "py").glob("*.py"))
    assert len(py) >= 10
    text = "\n".join(p.read_text(encoding="utf-8") for p in py)
    assert "from mod_" in text and "def fn_" in text
    ts = sorted((tmp_path / "a" / "ts").glob("*.ts"))
    assert any("import {" in p.read_text(encoding="utf-8") for p in ts)
```

- [ ] **Step 2: Implement**

```python
"""Deterministic synthetic repository for build benchmarks.

Five languages in fixed proportions (py 40 %, ts 25 %, js 15 %, go 10 %,
rs 10 %). Every file defines `fn_<i>` functions and calls into the previous
file of its language, so the resolver has real cross-file work to do.
"""
from __future__ import annotations

import random
from pathlib import Path

_SHARES = (("py", 0.40), ("ts", 0.25), ("js", 0.15), ("go", 0.10), ("rs", 0.10))
_FUNCS_PER_FILE = 12


def _py(i: int, prev: str | None) -> str:
    head = f"from mod_{prev} import fn_0 as prev_fn\n\n" if prev else ""
    body = "".join(
        f"def fn_{k}(x: int) -> int:\n    return {'prev_fn(x)' if prev and k == 0 else 'x'} + {k}\n\n\n"
        for k in range(_FUNCS_PER_FILE)
    )
    return head + body + "class Widget:\n    def run(self) -> int:\n        return fn_1(1)\n"


def _ts(i: int, prev: str | None, ext: str) -> str:
    head = f"import {{ fn0 }} from './mod_{prev}';\n\n" if prev else ""
    body = "".join(
        f"export function fn{k}(x: number): number {{ return {'fn0(x)' if prev and k == 0 else 'x'} + {k}; }}\n"
        for k in range(_FUNCS_PER_FILE)
    )
    return head + body + "export class Widget { run(): number { return fn1(1); } }\n"


def _go(i: int, prev: str | None) -> str:
    body = "".join(f"func Fn{k}(x int) int {{ return {'Fn0(x)' if k and False else 'x'} + {k} }}\n" for k in range(_FUNCS_PER_FILE))
    return "package synth\n\n" + body + "func Run() int { return Fn1(1) }\n"


def _rs(i: int, prev: str | None) -> str:
    head = f"use crate::mod_{prev}::fn_0 as prev_fn;\n\n" if prev else ""
    body = "".join(
        f"pub fn fn_{k}(x: i64) -> i64 {{ {'prev_fn(x)' if prev and k == 0 else 'x'} + {k} }}\n"
        for k in range(_FUNCS_PER_FILE)
    )
    return head + body + "pub fn run() -> i64 { fn_1(1) }\n"


def generate(root: Path, *, files: int, seed: int = 7) -> dict[str, int]:
    rng = random.Random(seed)
    counts = {lang: int(files * share) for lang, share in _SHARES}
    counts["py"] += files - sum(counts.values())
    for lang, n in counts.items():
        d = root / lang
        d.mkdir(parents=True, exist_ok=True)
        prev: str | None = None
        for i in range(n):
            name = f"{i:05d}_{rng.randrange(1000):03d}"
            if lang == "py":
                (d / f"mod_{name}.py").write_text(_py(i, prev), encoding="utf-8")
            elif lang == "ts":
                (d / f"mod_{name}.ts").write_text(_ts(i, prev, "ts"), encoding="utf-8")
            elif lang == "js":
                (d / f"mod_{name}.js").write_text(_ts(i, prev, "js").replace(": number", "").replace("(x: number)", "(x)"), encoding="utf-8")
            elif lang == "go":
                (d / f"mod_{name}.go").write_text(_go(i, prev), encoding="utf-8")
            else:
                (d / f"mod_{name}.rs").write_text(_rs(i, prev), encoding="utf-8")
            prev = name
    if counts["rs"]:
        (root / "rs" / "lib.rs").write_text("".join(f"pub mod mod_{p.stem[4:]};\n" for p in sorted((root / "rs").glob("mod_*.rs"))), encoding="utf-8")
    (root / "package.json").write_text('{"name": "synth", "private": true}\n', encoding="utf-8")
    (root / "go.mod").write_text("module synth\n\ngo 1.22\n", encoding="utf-8")
    return counts
```

- [ ] **Step 3: Run, commit**

```bash
git switch -c prod/ws-h-benchmark main
$DEV -m pytest tests/test_synthetic_repo.py -q
git add benchmarks/synthetic_repo.py tests/test_synthetic_repo.py
git commit -m "benchmarks: deterministic synthetic repository generator"
```

### Task H2: Build benchmark runner, CI job, declared limit

**Files:**
- Create: `benchmarks/build_benchmark.py`, `docs/benchmarks.md`
- Modify: `src/graphite/cli.py:1313` (`limits`), `.github/workflows/ci.yml` (new `benchmark` job), `tests/test_capabilities.py` (or the test that pins `limits`)

**Interfaces:**
- Produces: `python benchmarks/build_benchmark.py --files N --out metrics.json` → JSON `{files, wall_s, peak_rss_mb, nodes, edges, ms_per_file, graph_bytes, platform, python}`; capabilities `limits.supported_repo_files` (int) and `limits.supported_repo_files_basis` (str).

- [ ] **Step 1: Failing capabilities test**

In the test module that already asserts `limits` (find it with `python -P -m graphite query "callers cmd_capabilities"`; if none pins `limits`, add to `tests/test_capabilities.py`):

```python
def test_capabilities_declare_a_measured_repo_size(capsys):
    assert main(["capabilities", "--json"]) == 0
    limits = json.loads(capsys.readouterr().out)["limits"]
    assert limits["max_graph_bytes"] == 134217728
    assert limits["supported_repo_files"] == 20000
    assert "docs/benchmarks.md" in limits["supported_repo_files_basis"]
```

- [ ] **Step 2: Implement the runner**

`benchmarks/build_benchmark.py`:

```python
"""Build a synthetic repository and record how graphite scales.

    python benchmarks/build_benchmark.py --files 3000 --out metrics.json

Catastrophe detector, not an SLA: CI fails only on the budget flags.
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from benchmarks.synthetic_repo import generate  # noqa: E402


def _peak_rss_mb(proc: subprocess.CompletedProcess[str], rusage) -> float:
    if rusage is not None:  # POSIX: ru_maxrss is KiB on Linux, bytes on macOS
        raw = rusage.ru_maxrss
        return raw / 1024 if sys.platform == "linux" else raw / (1024 * 1024)
    return -1.0


def run(files: int, out: Path, *, wall_budget_s: float | None) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        counts = generate(root, files=files)
        argv = [sys.executable, "-P", "-m", "graphite", "--llm", "none", "--output-dir", str(Path(tmp) / "out"),
                "--cache-dir", str(Path(tmp) / "cache"), "build", str(root)]
        start = time.perf_counter()
        rusage = None
        if hasattr(subprocess, "_posixsubprocess") and sys.platform != "win32":
            import os
            import resource
            pid = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            stdout, _ = pid.communicate()
            _, status, rusage = os.wait4(pid.pid, 0) if False else (pid.pid, pid.returncode, resource.getrusage(resource.RUSAGE_CHILDREN))
            proc = subprocess.CompletedProcess(argv, pid.returncode, stdout, "")
        else:
            proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        wall = time.perf_counter() - start
        graph = Path(tmp) / "out" / "graph.json"
        graph_bytes = graph.stat().st_size if graph.is_file() else -1
        nodes = edges = -1
        if graph.is_file():
            data = json.loads(graph.read_text(encoding="utf-8"))
            nodes, edges = len(data.get("nodes", [])), len(data.get("edges", []))
        metrics = {
            "files": files, "counts": counts, "returncode": proc.returncode, "wall_s": round(wall, 2),
            "ms_per_file": round(wall * 1000 / files, 2), "peak_rss_mb": round(_peak_rss_mb(proc, rusage), 1),
            "nodes": nodes, "edges": edges, "graph_bytes": graph_bytes,
            "platform": platform.platform(), "python": platform.python_version(),
        }
        out.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        print(json.dumps(metrics, indent=2))
        if proc.returncode != 0:
            print(proc.stdout[-2000:])
            return 1
        if wall_budget_s is not None and wall > wall_budget_s:
            print(f"BUDGET EXCEEDED: {wall:.1f}s > {wall_budget_s}s")
            return 2
        if graph_bytes > 134217728:
            print(f"GRAPH TOO LARGE: {graph_bytes} bytes")
            return 3
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--files", type=int, default=3000)
    ap.add_argument("--out", type=Path, default=Path("benchmark-metrics.json"))
    ap.add_argument("--wall-budget-s", type=float, default=None)
    a = ap.parse_args()
    return run(a.files, a.out, wall_budget_s=a.wall_budget_s)


if __name__ == "__main__":
    raise SystemExit(main())
```

Simplify the rusage branch to plain `subprocess.run` + `resource.getrusage(resource.RUSAGE_CHILDREN)` after the call on POSIX (`RUSAGE_CHILDREN` is cumulative over waited-for children, which is exactly the one build); on Windows report `-1.0` and say so in `docs/benchmarks.md`. The `os.wait4`/`if False` line above must not survive review — write the simple form.

- [ ] **Step 3: Declare the limit**

In `cmd_capabilities` (`cli.py:1313`):

```python
        "limits": {
            "max_graph_bytes": MAX_GRAPH_BYTES,
            "supported_repo_files": SUPPORTED_REPO_FILES,
            "supported_repo_files_basis": SUPPORTED_REPO_FILES_BASIS,
        },
```

with module constants next to `MAX_GRAPH_BYTES`'s import:

```python
SUPPORTED_REPO_FILES = 20000
SUPPORTED_REPO_FILES_BASIS = "synthetic 20 000-file build recorded in docs/benchmarks.md (2026-08-29)"
```

- [ ] **Step 4: Measure locally at 3 000 and 20 000 files**

```bash
$DEV benchmarks/build_benchmark.py --files 3000 --out /tmp/bm-3000.json > /tmp/bm-3000.log 2>&1; echo EXIT=$? >> /tmp/bm-3000.log
$DEV benchmarks/build_benchmark.py --files 20000 --out /tmp/bm-20000.json > /tmp/bm-20000.log 2>&1; echo EXIT=$? >> /tmp/bm-20000.log
```

Also one real repository: clone `django/django` at a pinned commit into the scratchpad and time `python -P -m graphite --llm none build .` there; record file count (`graphite scan` output), wall, graph bytes.

- [ ] **Step 5: `docs/benchmarks.md`**

```markdown
# Build benchmarks

Recorded, not promised: these are single runs on the machine named. Re-run
`python benchmarks/build_benchmark.py --files N` to measure your own.

| Date | Repository | Files | Wall (s) | ms/file | Peak RSS (MB) | Nodes | Edges | graph.json (MB) | Machine |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-29 | synthetic (seed 7) | 3 000 | <wall> | <ms> | <rss> | <n> | <e> | <mb> | Windows 11, CPython 3.14, <cpu> |
| 2026-08-29 | synthetic (seed 7) | 20 000 | <wall> | <ms> | <rss> | <n> | <e> | <mb> | same |
| 2026-08-29 | django/django @ <sha> | <files> | <wall> | <ms> | <rss> | <n> | <e> | <mb> | same |
| 2026-08-29 | graphite (this repo) | 345 | 10.6 | 30.7 | — | 6 881 | 19 712 | 9.0 | same |

`capabilities` declares `supported_repo_files: 20000` on the strength of the
second row. Peak RSS is reported as -1 on Windows (no `resource` module); the
CI benchmark job on ubuntu records it.
```

Fill every `<…>` from the JSON files before committing; the table must contain no angle-bracket placeholders (add a test line in `tests/test_documentation.py`: `assert "<" not in benchmarks_table_rows`).

- [ ] **Step 6: CI job**

```yaml
  benchmark:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Install
        run: python -m pip install -e .
      - name: Build a 3000-file synthetic repository
        # Catastrophe detector: fails only past a 600 s wall or the graph
        # byte limit. Numbers are recorded as an artifact, not compared.
        run: python benchmarks/build_benchmark.py --files 3000 --out benchmark-metrics.json --wall-budget-s 600
      - uses: actions/upload-artifact@v4
        with:
          name: benchmark-metrics
          path: benchmark-metrics.json
```

- [ ] **Step 7: Tests, commit, push, merge**

```bash
$DEV -m pytest tests/test_capabilities.py tests/test_synthetic_repo.py tests/test_documentation.py -q
git add benchmarks/build_benchmark.py docs/benchmarks.md src/graphite/cli.py tests/test_capabilities.py tests/test_documentation.py .github/workflows/ci.yml
git commit -m "benchmarks: build benchmark, CI catastrophe detector, declared supported_repo_files (PRD WS-H)"
```

Merge: `Merge prod/ws-h-benchmark: scale measured and declared (PRD WS-H)`.

---

## WS-G — Documentation, compatibility contract, metadata (D6, D7)

### Task G1: `build_parser()` and the generated CLI reference

**Files:**
- Modify: `src/graphite/cli.py:2353-…` (extract `build_parser()` from `main()`)
- Create: `scripts/gen_cli_reference.py`, `docs/reference/cli.md`
- Test: `tests/test_cli_reference.py`

- [ ] **Step 1: Failing test**

```python
"""docs/reference/cli.md is generated from the parser; a stale copy fails."""
from __future__ import annotations

from pathlib import Path

from scripts.gen_cli_reference import render_reference

ROOT = Path(__file__).resolve().parents[1]


def test_cli_reference_is_current() -> None:
    expected = render_reference()
    actual = (ROOT / "docs" / "reference" / "cli.md").read_text(encoding="utf-8")
    assert actual == expected, "run: python scripts/gen_cli_reference.py"


def test_cli_reference_names_every_subcommand() -> None:
    from graphite.cli import build_parser
    text = (ROOT / "docs" / "reference" / "cli.md").read_text(encoding="utf-8")
    sub = next(a for a in build_parser()._actions if a.dest == "command")
    for name in sub.choices:
        assert f"## `graphite {name}`" in text
```

(`scripts/` has no `__init__.py`; `pytest` `pythonpath = ["."]` in `pyproject.toml` makes `scripts.gen_cli_reference` importable only if `scripts/__init__.py` exists — create an empty one.)

- [ ] **Step 2: Extract `build_parser()`**

In `cli.py`, cut everything in `main()` from `parser = argparse.ArgumentParser(...)` down to the line before `args = parser.parse_args(argv)` into:

```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="graphite", description="Local-first code knowledge graph.")
    ...  # the moved block, unchanged
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ...
```

The subparsers action must have `dest="command"` (check `sub = parser.add_subparsers(...)`; add `dest="command"` if missing — `args.command` may already be used, keep behaviour identical).

- [ ] **Step 3: Generator**

`scripts/gen_cli_reference.py`:

```python
"""Generate docs/reference/cli.md from graphite's argparse tree."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from graphite.cli import build_parser  # noqa: E402

OUT = ROOT / "docs" / "reference" / "cli.md"


def _options(parser: argparse.ArgumentParser) -> list[str]:
    rows = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction) or action.help == argparse.SUPPRESS:
            continue
        flags = ", ".join(f"`{o}`" for o in action.option_strings) or f"`{action.dest}`"
        default = "" if action.default in (None, argparse.SUPPRESS, False) else f" (default: `{action.default}`)"
        rows.append(f"| {flags} | {(action.help or '').replace('|', '\\|')}{default} |")
    return rows


def render_reference() -> str:
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    lines = [
        "# CLI reference",
        "",
        "Generated by `python scripts/gen_cli_reference.py` from the argparse tree;",
        "`tests/test_cli_reference.py` fails when this file is stale. Global options",
        "go before the subcommand: `graphite --output-dir out build .`.",
        "",
        "## Global options",
        "",
        "| Option | Meaning |",
        "|---|---|",
        *_options(parser),
        "",
    ]
    seen: set[int] = set()
    for name, child in sub.choices.items():
        if id(child) in seen:  # aliases share a parser
            continue
        seen.add(id(child))
        aliases = [n for n, c in sub.choices.items() if c is child and n != name]
        lines += [f"## `graphite {name}`", ""]
        if aliases:
            lines += [f"Aliases: {', '.join(f'`{a}`' for a in aliases)}", ""]
        lines += [(child.description or sub._name_parser_map and next((a.help for a in sub._choices_actions if a.dest == name), "") or "").strip(), ""]
        rows = _options(child)
        if rows:
            lines += ["| Option | Meaning |", "|---|---|", *rows, ""]
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render_reference(), encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Replace the awkward description expression with a helper `_help_for(sub, name)` that returns the `help=` text registered for that subcommand (`next(a.help for a in sub._choices_actions if a.dest == name)`), falling back to `child.description or ""`.

- [ ] **Step 4: Generate, test, commit**

```bash
git switch -c prod/ws-g-docs-contract main
: > scripts/__init__.py
$DEV scripts/gen_cli_reference.py
$DEV -m pytest tests/test_cli_reference.py -q
git add scripts/__init__.py scripts/gen_cli_reference.py docs/reference/cli.md src/graphite/cli.py tests/test_cli_reference.py
git commit -m "docs: generated CLI reference with a staleness test; build_parser() extracted"
```

Negative control (once): edit one word in `docs/reference/cli.md`, run the test → FAIL; `git checkout docs/reference/cli.md`.

### Task G2: Configuration reference with a lockstep test

**Files:**
- Create: `docs/reference/configuration.md`
- Test: `tests/test_configuration_reference.py`

- [ ] **Step 1: Failing test**

```python
"""Every Config field and every GRAPHITE_* key the loader reads is documented."""
from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

from graphite.config import Config

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "reference" / "configuration.md"


def _env_keys_read_by_loader() -> set[str]:
    tree = ast.parse((ROOT / "src" / "graphite" / "config.py").read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("graphite_"):
            keys.add(node.value.upper())
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("GRAPHITE_") and node.value != "GRAPHITE_":
            keys.add(node.value)
    return keys


def test_every_config_field_is_documented() -> None:
    text = DOC.read_text(encoding="utf-8")
    for field in dataclasses.fields(Config):
        assert f"`{field.name}`" in text, f"Config.{field.name} missing from configuration.md"


def test_every_env_key_is_documented() -> None:
    text = DOC.read_text(encoding="utf-8")
    for key in sorted(_env_keys_read_by_loader()):
        assert f"`{key}`" in text, f"{key} missing from configuration.md"
```

- [ ] **Step 2: Write the document**

Build the table by reading `Config` (field, default) and `from_env` (env key, parser, bounds) in `src/graphite/config.py` — one row per field:

```markdown
# Configuration reference

Graphite reads `GRAPHITE_*` environment variables (case-insensitive) and CLI
global options; a CLI option wins over the environment. `--llm none` (the
default) also ignores every `GRAPHITE_LLM*` variable.
`tests/test_configuration_reference.py` fails when a `Config` field or an
environment key read by the loader is missing here.

| Field | Environment | CLI | Default | Meaning |
|---|---|---|---|---|
| `output_dir` | `GRAPHITE_OUTPUT_DIR` | `--output-dir` | `graph-out` | Where `graph.json`, `graph.html` and `GRAPH_REPORT.md` are written. |
| `cache_dir` | `GRAPHITE_CACHE_DIR` | `--cache-dir` | `.cache/graphite` | Extraction cache root; partitions are keyed on `cache_version` and the engine fingerprint. |
| `cache_version` | `GRAPHITE_CACHE_VERSION` | — | `v11` | Coarse manual cache override; an engine change already invalidates the cache. |
| `workers` | `GRAPHITE_WORKERS` | `--workers` | `4` | Extraction parallelism. |
...
```

Continue for every field (`max_file_size`, `max_files`, `include_dotfiles`, `typescript_resolver`, `typescript_resolver_timeout_seconds` ← `GRAPHITE_TYPESCRIPT_RESOLVER_TIMEOUT`, `typescript_symbol_references`, the `llm_*` fields, the `provider_observer_*` fields, and every remaining field the dataclass declares). Add a final section "Other environment variables" listing `GRAPHITE_PROJECTS_ROOT` (from `default_projects_root`) and every other key the AST scan finds, each with one sentence and the module that reads it.

- [ ] **Step 3: Run until both tests pass; commit**

```bash
$DEV -m pytest tests/test_configuration_reference.py -q
git add docs/reference/configuration.md tests/test_configuration_reference.py
git commit -m "docs: configuration reference kept in lockstep with Config by test"
```

### Task G3: Exit codes, compatibility contract, README links, metadata

**Files:**
- Create: `docs/reference/exit-codes.md`, `docs/compatibility.md`
- Modify: `README.md` (link the references; trim the four exit-code paragraphs to one sentence each pointing at the table), `CHANGELOG.md` (header), `pyproject.toml` (classifiers), `tests/test_documentation.py` (new test)

- [ ] **Step 1: Failing test**

```python
def test_readme_links_the_reference_and_compatibility_docs() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    for target in ("docs/reference/cli.md", "docs/reference/configuration.md", "docs/reference/exit-codes.md", "docs/compatibility.md", "docs/benchmarks.md"):
        assert target in readme, target
    compat = (Path(__file__).resolve().parents[1] / "docs" / "compatibility.md").read_text(encoding="utf-8")
    for phrase in ("schema_version", "Deprecation policy", "one minor release", "Support matrix"):
        assert phrase in compat, phrase
    changelog = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "minor versions may break things" not in changelog.split("## [", 1)[0]
```

- [ ] **Step 2: `docs/reference/exit-codes.md`**

Derive from the source: for each command, `grep -n "return [0-9]" ` within that command's function in `cli.py` and the README paragraphs at lines 46, 280, 330, 490. Table:

```markdown
# Exit codes

| Command | 0 | 1 | 2 | Other |
|---|---|---|---|---|
| any | success | operational error or invalid input | argparse usage error | — |
| `doctor` | `ready`, `optional`, `degraded` | `blocked` | usage | — |
| `check` | graph is fresh | graph is stale (`--json` names the reason) | usage | — |
| `validate` | graph valid | integrity violation | usage | — |
| `query` / `search` | executed (zero matches is still 0 — branch on the JSON) | not-found or plan error | usage | — |
| `init` / `bootstrap` | onboarded (including `declined`, `guidance_only`) | approved activation `validation_failed` / `installation_failed` / `verification_failed` | usage | — |
| `review-changes` | packet built | `--fail-on-blocker` hit an evidence blocker, or invalid input | usage | — |
| `build` (daemon child) | built | failed | usage | 75: lock held by another writer, only when the daemon asked for a refusal report |
| `daemon-install-*` / `daemon-uninstall-*` / `daemon-service-status` | supervisor command succeeded | refused (wrong platform) or supervisor command failed | usage | — |
| `agent-hook` | always 0 (never blocks the agent) | — | — | — |
```

Verify every row against the code before committing (read the `return` statements); a row that cannot be verified is removed, not guessed.

- [ ] **Step 3: `docs/compatibility.md`**

```markdown
# Compatibility and support

From 1.0.0 graphite follows semantic versioning for the surfaces listed here.

## Stable surfaces

- **CLI**: every subcommand and option in `docs/reference/cli.md`. New options
  may be added in a minor; removing or renaming one is a major.
- **JSON outputs** that carry `schema_version` (`query`, `search`,
  `capabilities`, `doctor --json`, `check --json`, `daemon-health --json`,
  `review-changes`). Fields are added in minors; a field is removed or its
  meaning changed only with a `schema_version` bump, which is a major.
- **`graph-out/graph.json`** at `schema_version` 1: node and edge shape,
  `metadata.engine.fingerprint`, `resolution_health` schema 3.
- **Configuration**: every environment variable and field in
  `docs/reference/configuration.md`.
- **Exit codes** in `docs/reference/exit-codes.md`.
- **Hook and launcher contract**: `.githooks/` trampolines,
  `graphite agent-hook` (exit 0 always), the `-P -m graphite` launch shape.
- **MCP tool names** exposed by `graphite-mcp`.
- **Channel protocol** as documented by `graphite channel` and the
  channel's `PROTOCOL.md`.

## Not stable

Internal modules under `graphite.*`, everything under `graph-out/` other than
`graph.json`, routing and overlay storage layouts, `GRAPH_REPORT.md` prose,
and the extraction cache format (`cache_version`).

## Deprecation policy

A deprecated surface keeps working for at least **one minor release** after
the deprecation is announced in `CHANGELOG.md`, prints a warning naming its
replacement, and is removed only in the next major.

## Support matrix

Exactly the CI matrix: Windows, Linux (Ubuntu) and macOS × CPython 3.11,
3.12, 3.13, 3.14. A platform is supported when every one of its cells gates
merges; nothing else counts. Daemon supervision is installable on all three
(`daemon-install-windows`, `daemon-install-linux`, `daemon-install-macos`).

## Security fixes

Ship in the next patch release for the current 1.x minor; see `SECURITY.md`.
```

- [ ] **Step 4: README, CHANGELOG, classifiers**

README: under "Usage" add a short "Reference" list linking the five documents; replace each of the four exit-code paragraphs' code-detail sentences with "Exit codes: see `docs/reference/exit-codes.md`." while keeping every phrase `tests/test_documentation.py` pins (run it after each edit). CHANGELOG header:

```markdown
Notable changes to graphite. Format follows [Keep a Changelog]; versioning is
semantic. From 1.0.0 the surfaces in `docs/compatibility.md` are stable:
breaking changes to them happen only in a major release.
```

`pyproject.toml` classifiers: replace `Development Status :: 4 - Beta` with `Development Status :: 5 - Production/Stable`; add `Programming Language :: Python :: 3 :: Only`.

- [ ] **Step 5: Tests, commit, push, merge**

```bash
$DEV -m pytest tests/test_documentation.py tests/test_cli_reference.py tests/test_configuration_reference.py -q
git add docs/reference/exit-codes.md docs/compatibility.md README.md CHANGELOG.md pyproject.toml tests/test_documentation.py
git commit -m "docs: exit codes, compatibility contract, README references; Production/Stable classifier (PRD WS-G)"
```

Merge: `Merge prod/ws-g-docs-contract: reference docs kept by tests, compatibility contract, stable classifier (PRD WS-G)`.

---

## WS-E — Provenance: build in CI, keep the digest guard (D10)

### Task E1: Normalise the working copy and isolate the reproducibility confound

**Files:**
- No committed content changes expected (index is already LF).

- [ ] **Step 1: Re-checkout the 40 CRLF files through `.gitattributes`**

```bash
git switch -c prod/ws-e-provenance main
git ls-files --eol | awk '$2 ~ /w\/crlf/ {print $NF}' > /tmp/crlf.txt
wc -l /tmp/crlf.txt
xargs -a /tmp/crlf.txt rm --
git checkout -- $(cat /tmp/crlf.txt)
git ls-files --eol | grep -c 'w/crlf'
git status --short
```

Expected: `40`, then `0`, then an empty status (content identical; only bytes on disk changed).

- [ ] **Step 2: Build locally (build venv) and under WSL with the SAME frontend, compare**

```bash
F:/Projects/.venvs/graphite-build/Scripts/python.exe -m build --sdist --wheel --outdir /tmp/local-dist > /tmp/local-build.log 2>&1; echo EXIT=$? >> /tmp/local-build.log
sha256sum /tmp/local-dist/*
```

WSL (script via the Write tool, `MSYS_NO_PATHCONV=1 wsl -d Ubuntu -e bash <script>`): clone `main` of the working tree to `~/g-main`, then `uvx --from build==1.5.0 --with hatchling==1.32.0 pyproject-build --sdist --wheel --outdir dist` and `sha256sum dist/*`.

Expected after normalisation: identical wheel digests. If the sdist still differs, diff the two tarballs' member lists and bytes (`tar tzvf`, then `cmp` extracted members) and fix the cause (a stray file included on one OS is the usual one — `verify_artifact.py`'s forbidden-path list is where it goes). Record both digest pairs — before (from the PRD) and after — in the commit message of Task E2.

### Task E2: Pinned build tools, digests and double build in the `artifact` job

**Files:**
- Create: `release-build-constraints.txt`
- Modify: `.github/workflows/ci.yml` (`artifact` job), `RELEASING.md` (Build and inspect section: use the constraints file)

- [ ] **Step 1: Constraints file**

```
# Exact build-tool pins shared by ci.yml (artifact) and publish.yml.
# Bump both here; the publish digest guard proves the result reproduces.
build==1.5.0
hatchling==1.32.0
```

(Use the versions the `graphite-build` venv holds: `F:/Projects/.venvs/graphite-build/Scripts/python.exe -m pip list | grep -i "build\|hatchling"`.)

- [ ] **Step 2: Artifact job**

Replace the "Build the distribution" step with:

```yaml
      - name: Build the distribution twice and prove determinism
        run: |
          set -euo pipefail
          python -m pip install -c release-build-constraints.txt build hatchling
          python -m build --sdist --wheel --outdir dist
          python -m build --sdist --wheel --outdir dist-again
          sha256sum dist/* dist-again/*
          for f in dist/*; do cmp "$f" "dist-again/$(basename "$f")"; done
          echo "both builds byte-identical"
          sha256sum dist/* > dist.sha256
          cat dist.sha256
      - uses: actions/upload-artifact@v4
        with:
          name: dist-${{ github.sha }}
          path: |
            dist/
            dist.sha256
          retention-days: 90
```

(`build` reads `build-system.requires = ["hatchling"]` and installs it into an isolated env; to make the pin bite, pass `--no-isolation` after installing hatchling from the constraints file: `python -m build --no-isolation --sdist --wheel --outdir dist`. Use `--no-isolation` in both builds and in `publish.yml`.)

- [ ] **Step 3: Commit, push, run twice, compare across runs**

```bash
git add release-build-constraints.txt .github/workflows/ci.yml
git commit -m "ci: pinned build tools, digests printed, double build proves determinism"
git push -u origin prod/ws-e-provenance > /tmp/push-e.log 2>&1; echo EXIT=$? >> /tmp/push-e.log
```

Then `gh workflow run ci.yml --ref prod/ws-e-provenance` for a second run of the same SHA; compare the two `dist.sha256` outputs. Expected identical; record both run ids in the Task E3 commit message.

### Task E3: `publish.yml` builds from the tag with attestations

**Files:**
- Modify: `.github/workflows/publish.yml`, `RELEASING.md`, `scripts/verify_published_release.py`, `tests/test_verify_published_release.py`

- [ ] **Step 1: Failing test for the new verifier arm**

In `tests/test_verify_published_release.py` add, following the existing fake-`run` pattern:

```python
def test_provenance_arm_reports_attestation_presence(tmp_path, capsys):
    calls = []
    def fake_run(argv, cwd):
        calls.append(argv)
        if "--provenance" in argv or "attestation" in " ".join(argv):
            return subprocess.CompletedProcess(argv, 0, '{"attestation_bundles": [{"publisher": {"kind": "GitHub"}}]}', "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    outcome = verify("9.9.9", python=tmp_path / "py", run=fake_run, wheel_sha256=None, check_provenance=True)
    assert "PyPI provenance present" in outcome.passed or "PyPI provenance present" in outcome.skipped
```

Match the real `verify()` signature (read lines 121-135); the arm is **informative**: it passes when the PyPI JSON API (`https://pypi.org/pypi/graphite-code/<version>/json` → `urls[*].provenance` or `/integrity/` endpoint) reports an attestation, skips with a named reason when the endpoint is unreachable, and never blocks the digest arm.

- [ ] **Step 2: Rewrite the publish job**

```yaml
env:
  APPROVED_VERSION: "1.0.0"
  APPROVED_COMMIT: "<sha the tag must point at>"
  WHEEL_SHA256: "<from the artifact job on APPROVED_COMMIT and the local build>"
  SDIST_SHA256: "<same, sdist>"

jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: write   # attach the built artifacts to the GitHub Release
    steps:
      - name: Refuse to publish a version this workflow has not approved
        env:
          REQUESTED: ${{ inputs.version }}
        run: |
          set -euo pipefail
          [ "$REQUESTED" = "$APPROVED_VERSION" ] || { echo "::error::'$REQUESTED' != approved '$APPROVED_VERSION'"; exit 1; }
      - uses: actions/checkout@v7
        with:
          ref: refs/tags/v${{ env.APPROVED_VERSION }}
          persist-credentials: false
          fetch-depth: 1
      - name: Assert the tag points at the approved commit
        run: |
          set -euo pipefail
          actual="$(git rev-parse HEAD)"
          [ "$actual" = "$APPROVED_COMMIT" ] || { echo "::error::tag v$APPROVED_VERSION is at $actual, approved $APPROVED_COMMIT"; exit 1; }
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Build from the tag with pinned tools
        run: |
          set -euo pipefail
          python -m pip install -c release-build-constraints.txt build hatchling
          python -m build --no-isolation --sdist --wheel --outdir dist
          python scripts/verify_artifact.py dist
      - name: Verify SHA256 against the approved digests
        run: |
          set -euo pipefail
          manifest="${RUNNER_TEMP}/approved.sha256"
          printf '%s  dist/graphite_code-%s-py3-none-any.whl\n' "$WHEEL_SHA256" "$APPROVED_VERSION" > "$manifest"
          printf '%s  dist/graphite_code-%s.tar.gz\n' "$SDIST_SHA256" "$APPROVED_VERSION" >> "$manifest"
          sha256sum -c --strict "$manifest"
      - name: Attach the artifacts to the GitHub Release
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          set -euo pipefail
          tag="v${APPROVED_VERSION}"
          gh release view "$tag" > /dev/null 2>&1 || gh release create "$tag" --verify-tag --title "$tag" --notes "See CHANGELOG.md."
          for f in dist/*; do
            name="$(basename "$f")"
            if gh release view "$tag" --json assets -q '.assets[].name' | grep -qx "$name"; then
              echo "::error::asset $name already attached; refusing to clobber"; exit 1
            fi
          done
          gh release upload "$tag" dist/*
      - name: Publish to PyPI with provenance
        uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2
        with:
          packages-dir: dist
          skip-existing: false
          print-hash: true
          attestations: true
```

Keep the `inputs.version` block, the `concurrency` block and the header comment, rewriting the comment to say the workflow now builds from the tag and why that is honest (the digest guard proves the bytes equal the reviewed ones). Bump `env` to the values that `RELEASING.md` will have the maintainer fill at release time; until WS-I those four values stay as the 0.5.3 values with `APPROVED_COMMIT: "eadba9c…"` (full SHA), which keeps the workflow dispatchable-but-refusing for any other version.

- [ ] **Step 3: RELEASING.md**

Rewrite "Current release model", "Build and inspect artifacts" and "Tag and publish" to the new order:

1. gates; 2. prepare version commit; 3. push `main` and read the `artifact` job's `dist.sha256` on that commit; 4. local build with `release-build-constraints.txt` — the two digest sources must agree; 5. edit `publish.yml` (`APPROVED_VERSION`, `APPROVED_COMMIT`, both digests) and commit; 6. tag the *prepare* commit, push tag; 7. dispatch `publish.yml`; 8. `verify_published_release.py`; 9. download the wheel from the GitHub Release, verify, retain in `.graphite-releases/<v>/`; 10. deploy.

Replace "it never builds" with the new statement; keep the negative-control paragraph.

- [ ] **Step 4: Negative control — wrong `APPROVED_COMMIT` must stop before upload**

Temporarily set `APPROVED_COMMIT` to `0000000000000000000000000000000000000000` and `APPROVED_VERSION: "0.5.3"`, commit on the branch, run `gh workflow run publish.yml --ref prod/ws-e-provenance -f version=0.5.3`; expected: fails at "Assert the tag points at the approved commit", no later step runs. Restore the real value, commit `ci: publish negative control passed (run <id> stopped at the commit assertion)`.

(Trusted Publishing is bound to the workflow filename and repo, not the branch; the dispatch on a branch still cannot reach PyPI because the step never runs — read the run to confirm the publish step shows as skipped/not started.)

- [ ] **Step 5: Tests, commit, push, merge**

```bash
$DEV -m pytest tests/test_verify_published_release.py tests/test_documentation.py -q
git add .github/workflows/publish.yml RELEASING.md scripts/verify_published_release.py tests/test_verify_published_release.py
git commit -m "release: publish.yml builds from the approved tag, digest-guarded, with PyPI attestations (PRD WS-E)"
```

Merge: `Merge prod/ws-e-provenance: CI-built, digest-guarded, attested releases (PRD WS-E)`.

---

## WS-I — Release 1.0.0 (D11)

### Task I1: Declaration check

- [ ] **Step 1: Run the declaration checklist against `main` and write the result into `.graphite-releases/1.0.0/EVIDENCE.md` (create the directory per RELEASING.md's retention section)**

```bash
gh run list --branch main --limit 1 --json databaseId,headSha,conclusion
gh run view <id> --json jobs -q '.jobs[] | "\(.conclusion)\t\(.name)"' | sort | uniq -c
```

Expected: 12 test legs, lint, artifact, security, coverage, benchmark — all `success`. Then D2–D10 each with the command that proves it (from the PRD table) and its output. Any criterion that fails stops the release.

### Task I2: Prepare, approve, tag, dispatch, verify, deploy

- [ ] Follow the rewritten `RELEASING.md` step by step for version `1.0.0`; the CHANGELOG entry under `## [1.0.0]` lists WS-A…WS-H by declaration criterion.
- [ ] Post the channel round announcing 1.0.0 to every consumer agent (re-list the channel immediately before posting).
- [ ] Update `.graphite-releases/index.md` "Currently deployed" and the memory index.
- [ ] Restart the daemon: stop → install → start; confirm `daemon-health` ok and `graphite --version` reads 1.0.0.

**Blocked on the maintainer:** the `publish.yml` dispatch (§8 of the PRD). Everything up to and after it is the executor's.

---

## Self-review

- **Spec coverage:** D1 → A1; D2 → B1; D3 → C1–C2; D4 → D1–D3; D5 → F1–F4; D6 → G3; D7 → G1–G3; D8 → B2; D9 → H1–H2; D10 → E1–E3; D11 → I1–I2. PRD §8 operator steps appear in B2 and I2.
- **Placeholders:** the only angle-bracket values are ones the executor fills from a measurement in the same task (coverage floor, benchmark rows, approved digests/commit), each with the command that produces them.
- **Type consistency:** `daemon_launch()` returns `DaemonLaunch` with `.interpreter/.arguments/.working_dir/.argv` — used identically in F2, F3, F4; `install_unit(launch, *, name, home, start_now, run)` and `install_agent(launch, *, label, home, uid, run)` match their CLI call sites and the test fakes; `query_unit(name, *, run)` / `query_agent(label, *, uid, run)` match `daemon_health` and `cmd_daemon_service_status`.
