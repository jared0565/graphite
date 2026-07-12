# System Readiness and Doctor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix Graphite's real-system Git and daemon-health defects and add a secure built-in doctor command that makes optional MCP, TypeScript, and LLM testing easy to activate without making those integrations mandatory.

**Architecture:** Preserve the deterministic core and existing trust boundaries. Add an explicit command-scoped Git safe-directory setting for the already-resolved selected repository, classify daemon health from current rather than historical state, and split doctor functionality into a typed read-only orchestrator (`doctor.py`) plus bounded opt-in functional probes (`doctor_probes.py`).

**Tech Stack:** Python 3.11+, argparse, dataclasses, pathlib, subprocess, tempfile, urllib, pytest, Ruff, optional MCP package, project-local Node.js/TypeScript

---

## File Structure

- Modify `src/graphite/git.py`: add the selected canonical repository to Git's command-scoped protected configuration while continuing to discard inherited `GIT_*` variables.
- Modify `tests/test_git_security.py`: lock the hardened Git command contract and safe-directory behavior.
- Modify `tests/test_hardening.py`: verify real Git enumeration under assumed different ownership.
- Modify `src/graphite/daemon_health.py`: classify active failures from `last_error`, not cumulative `failure_count`.
- Modify `tests/test_daemon_health.py`: cover recovery, pending, active failure, and staleness interactions.
- Create `src/graphite/freshness.py`: own graph-manifest freshness calculation without coupling doctor to the CLI.
- Create `src/graphite/doctor.py`: result types, fast checks, aggregation, status calculation, redacted rendering, and doctor command service.
- Create `src/graphite/doctor_probes.py`: bounded deterministic, MCP, TypeScript, and opt-in LLM deep probes.
- Create `tests/test_doctor.py`: unit and integration contracts for fast/deep doctor behavior and secret non-disclosure.
- Modify `src/graphite/cli.py`: expose `doctor`, `--deep`, `--include-llm`, and `--json`.
- Modify `README.md`, `CONTRIBUTING.md`, and `ARCHITECTURE.md`: document diagnostics, optional activation, package validation, and trust boundaries.
- Modify `tests/test_documentation.py`: enforce doctor discoverability and credential-safe activation guidance.
- Modify `docs/superpowers/specs/2026-07-12-system-readiness-doctor-design.md`: record implementation only after every acceptance gate passes.

### Task 1: Restore Hardened Git Enumeration on Differently Owned Repositories

**Files:**
- Modify: `src/graphite/git.py`
- Modify: `tests/test_git_security.py`
- Modify: `tests/test_hardening.py`

- [ ] **Step 1: Add a failing command-contract test**

In `tests/test_git_security.py`, update the existing fake-`Popen` command assertion and add a focused test requiring the resolved project root to be passed as protected command configuration:

```python
def test_git_runner_sets_only_selected_root_as_safe_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    executable = tmp_path / "trusted" / "git.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"MZ")

    captured: dict[str, object] = {}

    class FakeProcess:
        stdout = io.BytesIO(b"")

        def wait(self, timeout: float) -> int:
            return 0

        def kill(self) -> None:
            return None

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("graphite.git._resolve_git_executable", lambda *_args, **_kwargs: executable)
    monkeypatch.setattr("graphite.git.subprocess.Popen", fake_popen)

    GitRunner(root).run(["ls-files", "-z"], timeout_seconds=2)

    assert captured["command"] == [
        str(executable),
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"safe.directory={root.resolve()}",
        "ls-files",
        "-z",
    ]
```

Update other command-slice assertions from `command[4:]` to `command[6:]` after the two new arguments.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python -B -m pytest -p no:cacheprovider tests/test_git_security.py::test_git_runner_sets_only_selected_root_as_safe_directory -q
```

Expected: failure because `safe.directory=<resolved root>` is absent.

- [ ] **Step 3: Add command-scoped safe-directory configuration**

In `GitRunner.run`, construct the command as:

```python
command = [
    str(self.executable),
    "--no-optional-locks",
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"safe.directory={self.root}",
    *arguments,
]
```

Do not preserve inherited `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_*`, or `GIT_CONFIG_VALUE_*`. The trusted value comes only from the canonical `project_root.resolve()` already stored as `self.root`.

- [ ] **Step 4: Add a real ownership-regression test**

In `tests/test_hardening.py`, add a Git-dependent test that uses Git's own ownership test switch for the baseline command and proves Graphite's command-scoped safe directory succeeds. Because `GitRunner` intentionally strips `GIT_*`, test the external command contract directly and the runner separately:

```python
def test_git_runner_handles_repository_requiring_safe_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    (root / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=root, check=True, capture_output=True)

    env = os.environ.copy()
    env["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"
    baseline = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, env=env, capture_output=True, check=False
    )
    if baseline.returncode == 0:
        pytest.skip("installed Git does not support the ownership test switch")

    original = graphite_git._isolated_environment

    def ownership_test_environment() -> dict[str, str]:
        isolated = original()
        isolated["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = "1"
        return isolated

    monkeypatch.setattr(graphite_git, "_isolated_environment", ownership_test_environment)
    result = graphite_git.GitRunner(root).run(["ls-files", "-z"], timeout_seconds=5)
    assert result.returncode == 0
    assert result.stdout == b"tracked.py\x00"
```

Add the required imports (`os`, `subprocess`, `pytest`, and `graphite.git as graphite_git`) only if they are not already present.

- [ ] **Step 5: Run Git and ingestion tests**

Run:

```bash
python -B -m pytest -p no:cacheprovider tests/test_git_security.py tests/test_hardening.py -q
python -m ruff check src/graphite/git.py tests/test_git_security.py tests/test_hardening.py
```

Expected: all pass.

- [ ] **Step 6: Verify the real command path**

Run from the Graphite repository:

```bash
python -m graphite check . --json
python -B -m graphite check . --json
```

Expected: both return zero with equivalent `stale`, file-count, and change-list fields.

- [ ] **Step 7: Commit**

```bash
git add src/graphite/git.py tests/test_git_security.py tests/test_hardening.py
git commit -m "fix: trust the selected Git repository explicitly"
```

### Task 2: Correct Active Daemon Failure Classification

**Files:**
- Modify: `src/graphite/daemon_health.py`
- Modify: `tests/test_daemon_health.py`

- [ ] **Step 1: Write recovery and active-state tests**

Add tests based on the existing status fixture:

```python
def test_recovered_project_is_not_failing() -> None:
    now = datetime(2026, 7, 12, 20, 30, tzinfo=timezone.utc)
    status = _status(
        now,
        [
            _project(
                root="F:/Projects/recovered",
                failure_count=7,
                last_error=None,
                build_count=8,
                last_success_at=(now - timedelta(seconds=30)).isoformat(),
            )
        ],
    )

    health = _project_health(status, now, 86_400)

    assert health["failing"] == []
    assert health["pending"] == []
    assert health["not_built_recently"] == []


def test_current_error_is_failing_even_after_prior_success() -> None:
    now = datetime(2026, 7, 12, 20, 30, tzinfo=timezone.utc)
    status = _status(
        now,
        [
            _project(
                root="F:/Projects/failing",
                failure_count=1,
                last_error="build failed",
                build_count=3,
                last_success_at=(now - timedelta(seconds=30)).isoformat(),
            )
        ],
    )

    health = _project_health(status, now, 86_400)

    assert [item["root"] for item in health["failing"]] == ["F:/Projects/failing"]
```

Use the actual existing test helpers or extend them with explicit keyword overrides; do not create a second incompatible fixture system.

- [ ] **Step 2: Run the recovery test and verify RED**

Run:

```bash
python -B -m pytest -p no:cacheprovider tests/test_daemon_health.py::test_recovered_project_is_not_failing -q
```

Expected: recovered project is incorrectly present in `failing`.

- [ ] **Step 3: Change active failure classification**

Replace:

```python
if item.get("last_error") or int(item.get("failure_count") or 0) > 0:
    failing.append(project)
```

with:

```python
if item.get("last_error"):
    failing.append(project)
```

Leave `failure_count` in `_project_summary` as historical telemetry.

- [ ] **Step 4: Run daemon tests and real health check**

Run:

```bash
python -B -m pytest -p no:cacheprovider tests/test_daemon_health.py tests/test_daemon.py -q
python -m ruff check src/graphite/daemon_health.py tests/test_daemon_health.py
python -B -m graphite daemon-health F:/Projects --json
```

Expected: Graphite and other recovered projects with `last_error: null` are absent from active `failing`; genuine current errors remain.

- [ ] **Step 5: Commit**

```bash
git add src/graphite/daemon_health.py tests/test_daemon_health.py
git commit -m "fix: classify only active daemon failures"
```

### Task 3: Add Typed Doctor Results and Fast Readiness Checks

**Files:**
- Create: `src/graphite/freshness.py`
- Create: `src/graphite/doctor.py`
- Modify: `src/graphite/cli.py`
- Create: `tests/test_doctor.py`

- [ ] **Step 1: Write result-model tests**

Create `tests/test_doctor.py` with stable schema and aggregation tests:

```python
from __future__ import annotations

import json
from pathlib import Path

from graphite.doctor import DoctorCheck, build_report, format_doctor_text


def test_report_status_and_exit_code_are_deterministic() -> None:
    checks = [
        DoctorCheck("python", "Python", "ready", "Python is supported"),
        DoctorCheck("typescript", "TypeScript", "optional", "compiler not installed"),
        DoctorCheck("git", "Git", "blocked", "enumeration failed", remediation=("Check repository ownership",)),
    ]

    report = build_report(Path("repo"), checks, deep=False, llm_included=False)

    assert report["status"] == "blocked"
    assert report["exit_code"] == 1
    assert [item["code"] for item in report["checks"]] == ["git", "python", "typescript"]
    encoded = json.dumps(report)
    assert "api_key" not in encoded.casefold()


def test_optional_and_degraded_checks_do_not_fail_core() -> None:
    report = build_report(
        Path("repo"),
        [
            DoctorCheck("mcp", "MCP", "optional", "not installed"),
            DoctorCheck("daemon", "Daemon", "degraded", "warnings present"),
        ],
        deep=False,
        llm_included=False,
    )
    assert report["status"] == "degraded"
    assert report["exit_code"] == 0
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
python -B -m pytest -p no:cacheprovider tests/test_doctor.py -q
```

Expected: import failure because `graphite.doctor` does not exist.

- [ ] **Step 3: Implement immutable result types and aggregation**

First extract `_manifest_map` and `_check_status` from `src/graphite/cli.py` into `src/graphite/freshness.py` as public functions with unchanged behavior:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Config
from .ingest import collect_files


def manifest_map(manifest: dict[str, Any]) -> dict[str, str]:
    return {
        item["rel_path"]: item.get("hash", "")
        for item in manifest.get("files", [])
        if "rel_path" in item
    }


def check_graph_freshness(root: Path, cfg: Config) -> dict[str, Any]:
    manifest_path = cfg.output_dir / ".graphite_manifest.json"
    if not manifest_path.exists():
        return {
            "stale": True,
            "reason": "missing manifest",
            "manifest": manifest_path.as_posix(),
            "added": [],
            "changed": [],
            "removed": [],
        }
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "stale": True,
            "reason": f"unreadable manifest: {exc}",
            "added": [],
            "changed": [],
            "removed": [],
        }
    entries = collect_files(root, cfg)
    current = {entry.rel_path: entry.content_hash for entry in entries}
    old = manifest_map(previous)
    added = sorted(set(current) - set(old))
    removed = sorted(set(old) - set(current))
    changed = sorted(path for path in set(current).intersection(old) if current[path] != old[path])
    return {
        "stale": bool(added or removed or changed),
        "file_count": len(current),
        "manifest_file_count": len(old),
        "added": added,
        "changed": changed,
        "removed": removed,
    }
```

Change `cmd_check` to call `check_graph_freshness` and remove the two private implementations from `cli.py`. Run existing check/freshness tests before continuing; this is a behavior-preserving ownership refactor.

Create `src/graphite/doctor.py` with:

```python
from __future__ import annotations

import importlib.util
import json
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from .config import Config, default_projects_root
from .daemon_health import HealthOptions, evaluate_daemon_health
from .freshness import check_graph_freshness
from .git import GitError, GitRunner
from .validation import validate_graph_bundle

DoctorStatus = Literal["ready", "optional", "degraded", "blocked"]
_STATUS_RANK: dict[DoctorStatus, int] = {
    "ready": 0,
    "optional": 1,
    "degraded": 2,
    "blocked": 3,
}
_MAX_GRAPH_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class DoctorCheck:
    code: str
    label: str
    status: DoctorStatus
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    remediation: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
            "remediation": list(self.remediation),
        }


def build_report(
    root: Path,
    checks: Iterable[DoctorCheck],
    *,
    deep: bool,
    llm_included: bool,
) -> dict[str, Any]:
    ordered = sorted(checks, key=lambda item: item.code)
    overall = max(ordered, key=lambda item: _STATUS_RANK[item.status]).status if ordered else "ready"
    return {
        "schema_version": 1,
        "root": root.resolve().name,
        "deep": deep,
        "llm_included": llm_included,
        "status": overall,
        "exit_code": 1 if overall == "blocked" else 0,
        "checks": [item.to_dict() for item in ordered],
    }
```

Do not include absolute selected-root paths in JSON; use the root basename and project-relative paths only.

- [ ] **Step 4: Add fast checker functions**

Implement `check_python()`, `check_git(root)`, `check_graph(root, cfg)`,
`check_daemon(root, daemon_base)`, `check_mcp()`,
`check_typescript(root, timeout_seconds=5.0)`, and `check_llm_config(cfg)`. Each
returns one `DoctorCheck` and catches only its own expected failures.

Required behavior:

- `check_python`: `ready` for supported Python, `blocked` below 3.11; details contain version only.
- `check_git`: run `GitRunner(root).run(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], timeout_seconds=10)`; non-zero or `GitError` is `blocked`; details contain record count, not file paths.
- `check_graph`: if `graph-out/graph.json` is absent, return `degraded`; reject files above `_MAX_GRAPH_BYTES`; load JSON, run `validate_graph_bundle`, then call `check_graph_freshness(root, cfg)`. Report counts/freshness, no nodes or paths.
- `check_daemon`: call `evaluate_daemon_health` with current defaults; map errors to `degraded`, warnings to `degraded`, clean to `ready`. Daemon is operationally optional for core CLI use, so missing status is `optional`, not `blocked`.
- `check_mcp`: `importlib.util.find_spec("mcp")` and `shutil.which("graphite-mcp")`; both available is `ready`, otherwise `optional` with exact activation guidance.
- `check_typescript`: if Node is absent, `optional`; otherwise run `node -e` from `root` to print `require('typescript').version`, with timeout, no shell, bounded `capture_output`; missing module is `optional`, success `ready`, timeout/invalid output `degraded`.
- `check_llm_config`: when `cfg.llm_mode == "none"`, return `optional`; details may contain `mode`, normalized provider, and `credential_present: bool` only. If a credential is present while disabled, summary must say it is unused and should be removed/rotated without echoing it.

Use a shared helper that truncates sanitized subprocess text to 500 characters and never includes environment mappings.

- [ ] **Step 5: Add the fast orchestration and text formatter**

```python
FastCheck = Callable[[], DoctorCheck]


def run_doctor(
    root: Path,
    *,
    cfg: Config,
    daemon_base: Path | None = None,
    deep: bool = False,
    include_llm: bool = False,
    deep_runner: Callable[..., list[DoctorCheck]] | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    checks = [
        check_python(),
        check_git(root),
        check_graph(root, cfg),
        check_daemon(root, (daemon_base or default_projects_root()).resolve()),
        check_mcp(),
        check_typescript(root),
        check_llm_config(cfg),
    ]
    if deep:
        from .doctor_probes import run_deep_probes
        runner = deep_runner or run_deep_probes
        checks.extend(runner(root, cfg=cfg, include_llm=include_llm))
    return build_report(root, checks, deep=deep, llm_included=bool(deep and include_llm))


def format_doctor_text(report: dict[str, Any]) -> str:
    lines = [f"[graphite] doctor: {report['status']}"]
    for check in report["checks"]:
        lines.append(f"  [{check['status']}] {check['label']}: {check['summary']}")
        for item in check["remediation"]:
            lines.append(f"    - {item}")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 6: Test fast checks and redaction**

Add table-driven tests with monkeypatched `GitRunner`, subprocess, module discovery, graph files, daemon evaluator, and configs. At minimum assert:

```python
def test_disabled_llm_reports_presence_without_secret() -> None:
    secret = "credential-value-that-must-not-appear"
    cfg = Config(llm_mode="none", llm_provider="openrouter", llm_api_key=secret)
    check = check_llm_config(cfg)
    payload = json.dumps(check.to_dict())
    assert check.status == "optional"
    assert check.details["credential_present"] is True
    assert secret not in payload
```

- [ ] **Step 7: Run and commit**

```bash
python -B -m pytest -p no:cacheprovider tests/test_doctor.py -q
python -m ruff check src/graphite/freshness.py src/graphite/doctor.py src/graphite/cli.py tests/test_doctor.py
git add src/graphite/freshness.py src/graphite/doctor.py src/graphite/cli.py tests/test_doctor.py
git commit -m "feat: add fast system readiness checks"
```

### Task 4: Add the Doctor CLI

**Files:**
- Modify: `src/graphite/cli.py`
- Modify: `tests/test_doctor.py`

- [ ] **Step 1: Add failing parser and rendering tests**

Monkeypatch `graphite.cli.run_doctor` and assert:

```python
def test_doctor_cli_json_and_exit_code(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        "graphite.cli.run_doctor",
        lambda *args, **kwargs: {
            "schema_version": 1,
            "root": "repo",
            "deep": False,
            "llm_included": False,
            "status": "blocked",
            "exit_code": 1,
            "checks": [],
        },
    )
    assert main(["doctor", ".", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
```

Also assert `--include-llm` without `--deep` is rejected by argparse with exit code 2.

- [ ] **Step 2: Verify RED**

```bash
python -B -m pytest -p no:cacheprovider tests/test_doctor.py -k "doctor_cli" -q
```

Expected: `doctor` is not a known command.

- [ ] **Step 3: Wire the command**

Import `format_doctor_text` and `run_doctor`, add:

```python
def cmd_doctor(args: argparse.Namespace) -> int:
    if args.include_llm and not args.deep:
        raise ValueError("--include-llm requires --deep")
    root = Path(args.path).resolve()
    if not root.is_dir():
        raise ValueError("doctor path must be an existing directory")
    cfg = _project_scoped_config(args, root)
    report = run_doctor(
        root,
        cfg=cfg,
        daemon_base=Path(args.daemon_base).resolve() if args.daemon_base else None,
        deep=args.deep,
        include_llm=args.include_llm,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_doctor_text(report), end="")
    return int(report["exit_code"])
```

Add parser:

```python
p_doctor = sub.add_parser("doctor", help="Check Graphite core and optional integration readiness")
p_doctor.add_argument("path", nargs="?", default=".", help="Project path (default: current directory)")
p_doctor.add_argument("--daemon-base", default=None, help="Daemon base folder (default: auto-detect)")
p_doctor.add_argument("--deep", action="store_true", help="Run bounded functional probes in temporary storage")
p_doctor.add_argument("--include-llm", action="store_true", help="With --deep, run one synthetic LLM connectivity probe")
p_doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
p_doctor.set_defaults(func=cmd_doctor)
```

Enforce the flag dependency with `p_doctor.error`-equivalent parser validation or a custom argparse action so misuse produces exit 2 rather than the generic runtime catch. The test determines the final parser arrangement.

- [ ] **Step 4: Run CLI tests and commit**

```bash
python -B -m pytest -p no:cacheprovider tests/test_doctor.py -q
python -m ruff check src/graphite/cli.py tests/test_doctor.py
python -B -m graphite doctor --help
git add src/graphite/cli.py tests/test_doctor.py
git commit -m "feat: expose graphite doctor command"
```

### Task 5: Add the Deterministic Deep Probe

**Files:**
- Create: `src/graphite/doctor_probes.py`
- Modify: `tests/test_doctor.py`

- [ ] **Step 1: Write a failing deterministic-probe test**

```python
def test_core_deep_probe_uses_external_temporary_workspace(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    check = probe_core_pipeline(selected, python_executable=sys.executable, timeout_seconds=30)
    assert check.status == "ready"
    assert check.code == "deep_core"
    assert list(selected.iterdir()) == []
```

- [ ] **Step 2: Verify RED**

```bash
python -B -m pytest -p no:cacheprovider tests/test_doctor.py::test_core_deep_probe_uses_external_temporary_workspace -q
```

Expected: import failure for `doctor_probes`.

- [ ] **Step 3: Implement bounded subprocess execution**

Create `doctor_probes.py` with:

```python
@dataclass(frozen=True)
class ProbeProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _run_bounded(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    input_text: str | None = None,
    max_output_chars: int = 32_000,
    environment: dict[str, str] | None = None,
) -> ProbeProcessResult:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        input=input_text,
        text=True,
        capture_output=True,
        shell=False,
        timeout=timeout_seconds,
        check=False,
        env=environment,
    )
    if len(completed.stdout) > max_output_chars or len(completed.stderr) > max_output_chars:
        raise DoctorProbeError("probe output limit exceeded")
    return ProbeProcessResult(completed.returncode, completed.stdout, completed.stderr)
```

Never pass the full parent environment to an LLM probe; non-network subprocesses may use a copy with all `GRAPHITE_LLM_*`, `OPENAI_*`, `ANTHROPIC_*`, and provider-key variables removed.

- [ ] **Step 4: Implement the synthetic core pipeline**

`probe_core_pipeline` must:

1. Create `TemporaryDirectory(prefix="graphite-doctor-")` under the OS temp root.
2. Resolve both the temp root and created directory and verify containment before any cleanup assumption.
3. Create `repo/src/lib.py` and `repo/src/app.py` with constant synthetic content.
4. Run, with global options before subcommand:

```text
python -B -m graphite --output-dir <temp>/out --cache-dir <temp>/cache --llm none build <temp>/repo
python -B -m graphite validate --graph-json <temp>/out/graph.json --json
python -B -m graphite query stats --graph-json <temp>/out/graph.json
```

5. Parse validation/query JSON, require zero validation errors and positive node count.
6. Return only counts and elapsed/result metadata; no absolute paths.
7. Map timeouts, output limits, malformed JSON, and non-zero exits to a `blocked` `DoctorCheck` with stable summary and remediation.

- [ ] **Step 5: Run tests and commit**

```bash
python -B -m pytest -p no:cacheprovider tests/test_doctor.py -k "core_deep" -q
python -m ruff check src/graphite/doctor_probes.py tests/test_doctor.py
git add src/graphite/doctor_probes.py tests/test_doctor.py
git commit -m "feat: add isolated core doctor probe"
```

### Task 6: Add MCP and TypeScript Deep Probes

**Files:**
- Modify: `src/graphite/doctor_probes.py`
- Modify: `tests/test_doctor.py`

- [ ] **Step 1: Add MCP protocol tests**

Extract the safe JSON-RPC interaction pattern from `tests/test_mcp.py` into doctor-specific tests using a fake subprocess/queue fixture. Require:

- `initialize` response server name `graphite`.
- `tools/list` contains `graphite_query`, `graphite_summary`, `graphite_community`, and `graphite_refresh`.
- Closed stdout, malformed JSON, response above 1 MiB, timeout, and non-zero exit return `degraded` without hanging.
- The probe terminates then kills after a bounded cleanup timeout.

- [ ] **Step 2: Implement MCP deep probe**

Implement `probe_mcp(root, *, python_executable=sys.executable,
timeout_seconds=10.0) -> DoctorCheck`.

Launch `[python_executable, "-B", "-m", "graphite.mcp"]` with `shell=False`, selected `root` as cwd, stdin/stdout pipes, sanitized environment, and stderr captured only up to the output limit. Send the same MCP protocol version already used by `tests/test_mcp.py`. Do not call `graphite_refresh`; initialize/list-tools is sufficient and read-only.

- [ ] **Step 3: Add TypeScript probe tests**

Test three outcomes by monkeypatching `_run_bounded`:

- Node missing: `optional`.
- `require('typescript')` fails: `optional` with activation guidance.
- Compiler returns version and transpiles a synthetic constant: `ready`, details contain version only.
- Timeout/invalid result: `degraded`.

- [ ] **Step 4: Implement TypeScript deep probe**

```python
_TYPESCRIPT_PROBE_SCRIPT = (
    "const ts=require('typescript');"
    "const source='const value: number = 1';"
    "const result=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022}});"
    "if(!result.outputText.includes('const value = 1')) process.exit(3);"
    "process.stdout.write(JSON.stringify({version:ts.version}));"
)
```

Implement `probe_typescript(root, *, timeout_seconds=10.0) -> DoctorCheck` by
running `node -e <script>` with cwd `root`, timeout, bounded output, and no
repository writes. Missing TypeScript remediation must say:

```text
Validate package: node C:/Users/fbmac/atlas/Codex/.codex_state/user_home/scripts/validate-packages.cjs typescript
Then add typescript with the target project's existing package manager.
```

Do not execute either command from doctor.

- [ ] **Step 5: Run and commit the direct MCP and TypeScript probes**

```bash
python -B -m pytest -p no:cacheprovider tests/test_mcp.py tests/test_doctor.py -q
python -m ruff check src/graphite/doctor_probes.py tests/test_doctor.py
git add src/graphite/doctor_probes.py tests/test_doctor.py
git commit -m "feat: probe optional MCP and TypeScript readiness"
```

### Task 7: Add Explicit Synthetic LLM Probe and Redaction

**Files:**
- Modify: `src/graphite/doctor_probes.py`
- Modify: `src/graphite/doctor.py`
- Modify: `tests/test_doctor.py`

- [ ] **Step 1: Write opt-in and synthetic-data tests**

```python
def test_deep_doctor_does_not_call_llm_without_explicit_flag() -> None:
    provider = FakeProvider()
    checks = run_deep_probes(Path("repo"), cfg=Config(llm_mode="cloud"), include_llm=False, provider_factory=lambda _cfg: provider)
    assert provider.calls == []
    assert next(item for item in checks if item.code == "deep_llm").status == "optional"


def test_llm_probe_uses_constant_synthetic_content_and_redacts_failure() -> None:
    secret = "never-show-this-value"
    provider = RecordingProvider(error=RuntimeError(f"authorization failed {secret}"))
    check = probe_llm(
        Config(llm_mode="cloud", llm_provider="openrouter", llm_api_key=secret),
        provider_factory=lambda _cfg: provider,
    )
    assert provider.calls == [
        ("You are a connectivity probe. Reply with READY only.", "Synthetic Graphite connectivity test. No repository data is included.")
    ]
    assert check.status == "degraded"
    assert secret not in json.dumps(check.to_dict())
```

- [ ] **Step 2: Verify RED**

```bash
python -B -m pytest -p no:cacheprovider tests/test_doctor.py -k "llm" -q
```

- [ ] **Step 3: Implement the probe**

```python
_LLM_SYSTEM = "You are a connectivity probe. Reply with READY only."
_LLM_USER = "Synthetic Graphite connectivity test. No repository data is included."


def probe_llm(
    cfg: Config,
    *,
    provider_factory: Callable[[Config], CompletionProvider] = make_provider,
) -> DoctorCheck:
    if cfg.llm_mode == "none":
        return DoctorCheck("deep_llm", "LLM connectivity", "optional", "LLM mode is disabled")
    try:
        result = provider_factory(cfg).complete(_LLM_SYSTEM, _LLM_USER)
    except Exception as exc:
        return DoctorCheck(
            "deep_llm",
            "LLM connectivity",
            "degraded",
            "synthetic connectivity probe failed",
            details={"error_type": type(exc).__name__},
            remediation=("Verify provider endpoint, rotated session credential, model, and timeout",),
        )
    return DoctorCheck(
        "deep_llm",
        "LLM connectivity",
        "ready",
        "synthetic connectivity probe succeeded",
        details={"provider": cfg.llm_provider, "response_present": bool(result.text)},
    )
```

Do not include response text, raw exception text, URL query strings, headers, or credential fragments.

- [ ] **Step 4: Wire all optional probes atomically**

Add the orchestrator only after `probe_llm` exists:

```python
def run_deep_probes(root: Path, *, cfg: Config, include_llm: bool) -> list[DoctorCheck]:
    checks = [probe_core_pipeline(root), probe_mcp(root), probe_typescript(root)]
    checks.append(
        probe_llm(cfg)
        if include_llm
        else DoctorCheck(
            "deep_llm",
            "LLM connectivity",
            "optional",
            "not requested; use --deep --include-llm",
        )
    )
    return checks
```

Update `run_deep_probes` to accept an injectable `provider_factory` in tests, or test `probe_llm` directly and monkeypatch it before invoking the orchestrator. Do not add a temporary LLM stub in Task 6.

- [ ] **Step 5: Run secret scans and tests**

```bash
python -B -m pytest -p no:cacheprovider tests/test_llm.py tests/test_doctor.py -q
python -m ruff check src/graphite/doctor.py src/graphite/doctor_probes.py tests/test_doctor.py
```

Expected: all pass; no test output contains the sentinel secret.

- [ ] **Step 6: Commit**

```bash
git add src/graphite/doctor.py src/graphite/doctor_probes.py tests/test_doctor.py
git commit -m "feat: add opt-in synthetic LLM readiness probe"
```

### Task 8: Document Optional Activation and Doctor Boundaries

**Files:**
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `ARCHITECTURE.md`
- Modify: `tests/test_documentation.py`

- [ ] **Step 1: Add failing documentation contracts**

Add exact-line/phrase contracts requiring:

```python
def test_readme_documents_doctor_and_optional_activation() -> None:
    readme = read_document("README.md")
    assert "## System readiness and optional integrations" in set(readme.splitlines())
    for command in (
        "python -m graphite doctor .",
        "python -m graphite doctor . --deep",
        "python -m graphite doctor . --deep --include-llm",
    ):
        assert command in readme
    assert "validate-packages.cjs typescript" in readme
    assert "provider dashboard" in readme


def test_architecture_documents_doctor_trust_boundary() -> None:
    architecture = read_document("ARCHITECTURE.md")
    assert "### Doctor and deep-probe boundary" in set(architecture.splitlines())
    assert "synthetic content only" in architecture
```

- [ ] **Step 2: Verify RED**

```bash
python -B -m pytest -p no:cacheprovider tests/test_documentation.py -k "doctor or optional_activation" -q
```

- [ ] **Step 3: Update README**

Add `System readiness and optional integrations` after the contributor navigation or deterministic review section. Cover:

- Fast and deep commands and status meanings.
- Deep mode writes only to external temporary storage.
- MCP activation with `python -m pip install -e ".[mcp]"` only after repository package-validation policy.
- TypeScript validation command followed by target-project package-manager installation; no global install.
- Local Ollama activation without a key.
- Cloud activation with a newly rotated session-scoped `GRAPHITE_LLM_API_KEY` and explicit provider/model/base URL.
- Provider dashboard revocation of the exposed key and parent-process restart.
- `--include-llm` synthetic-only warning.

Do not include a real key-shaped example. Use shell-neutral prose for the credential and provider-specific commands only where safe.

- [ ] **Step 4: Update contributor and architecture guides**

`CONTRIBUTING.md` must require:

- Tests for stable doctor JSON, redaction, timeouts, and missing optional tools.
- Package validation before any activation install.
- No live provider calls in automated tests.

`ARCHITECTURE.md` must describe:

- Fast checks as read-only.
- Deep probes in external temporary storage.
- Subprocess and model trust boundaries.
- Synthetic-only LLM data flow.
- Optional states not blocking core.

- [ ] **Step 5: Validate docs and commit**

```bash
python -B -m pytest -p no:cacheprovider tests/test_documentation.py -q
python -m ruff check tests/test_documentation.py
git diff --check
git add README.md CONTRIBUTING.md ARCHITECTURE.md tests/test_documentation.py
git commit -m "docs: add secure optional activation guide"
```

### Task 9: Full Acceptance and Operational Handoff

**Files:**
- Modify: `docs/superpowers/specs/2026-07-12-system-readiness-doctor-design.md`

- [ ] **Step 1: Run focused reliability commands**

```bash
python -m graphite check . --json
python -B -m graphite check . --json
python -B -m graphite daemon-health F:/Projects --json
```

Expected: normal/no-bytecode freshness outputs agree; recovered projects are not active failures.

- [ ] **Step 2: Run doctor fast modes**

```bash
python -B -m graphite doctor .
python -B -m graphite doctor . --json
```

Expected: core Python/Git/graph checks ready; absent TypeScript is optional; disabled LLM is optional and credential value never appears.

- [ ] **Step 3: Run doctor deep mode without network**

```bash
python -B -m graphite doctor . --deep
python -B -m graphite doctor . --deep --json
```

Expected: deterministic core and MCP probes ready; TypeScript optional when absent; LLM reports not requested; selected repository remains unchanged.

- [ ] **Step 4: Do not run live LLM until rotation is confirmed**

The user must revoke the exposed provider credential and remove it from the parent application's secret configuration. After restarting affected processes, confirm only presence/absence:

```powershell
if ($env:GRAPHITE_LLM_API_KEY) { 'credential present' } else { 'credential absent' }
```

Do not print the value. Do not run `--include-llm` with the exposed credential. After the user supplies a new session-scoped key and explicitly authorizes a provider request, run the deep LLM check separately.

- [ ] **Step 5: Run complete automated verification**

```bash
python -m ruff check .
python -B -m pytest -p no:cacheprovider -q
git diff --check
git status --short
```

Expected: Ruff and all tests pass; diff check clean; no unintended files.

- [ ] **Step 6: Mark the design implemented**

Only after Steps 1–5 pass, change:

```markdown
**Status:** Design approved; written spec pending review
```

to:

```markdown
**Status:** Implemented and verified
```

- [ ] **Step 7: Commit the acceptance record**

```bash
git add docs/superpowers/specs/2026-07-12-system-readiness-doctor-design.md
git commit -m "docs: record system readiness acceptance"
```

- [ ] **Step 8: Report the operational boundary**

Report separately:

- Repository implementation verification.
- Optional capability readiness.
- Provider credential rotation status.
- Whether a live LLM probe was intentionally skipped or explicitly authorized.

Never report the key value, provider response text, or raw credential-bearing error.

## Plan Self-Review

- Spec coverage: Tasks 1–2 fix both observed core defects; Tasks 3–7 implement fast/deep doctor checks and optional integrations; Task 8 documents activation; Task 9 enforces repository and operational acceptance.
- Isolation: `doctor.py` owns typed read-only orchestration; `doctor_probes.py` owns bounded side-effecting probes; CLI only adapts arguments/output.
- Model-agnostic behavior: no provider is required, absent integrations remain optional, and live network use requires two explicit flags plus operational credential rotation.
- Package safety: no dependency installation is part of implementation; TypeScript activation documents the mandatory validator and target-project-local installation only.
- Security: Git trust is scoped to the selected canonical root; deep probes avoid repository writes; errors/outputs are bounded; secret values and raw model responses never enter reports.
- Type consistency: `DoctorCheck`, doctor status literals, report schema, and deep-probe signatures are used consistently across all tasks.
