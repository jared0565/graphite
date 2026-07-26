# Incident Ledger — Design

**Date:** 2026-07-25
**Round:** incident ledger (queued during the trust-signal round; operator-approved)
**Base:** main `2776452` (python resolver binding merged)

## 1. Problem and goal

Graphite fails open by design in several places, and those failures currently
leave no durable trace:

- Per-file extraction failures become `ExtractionResult(error="read_error|
  parse_error|worker_error: ...")` and are **discarded by `_merge`** except the
  last one (`extract/ast.py:1106-1107` keeps only the final error string);
  nothing downstream aggregates or reports them.
- `persisted_resolution` (`health.py`) deliberately swallows
  `OSError/ValueError/RecursionError` when the persisted analysis artifact is
  malformed — correct fail-open behavior, but the malformation itself is
  never recorded.
- Graph-load failures (`GraphReadError`) surface once to the invoking command
  and vanish.
- INCONCLUSIVE query results tell the caller the graph could not answer, but
  nobody remembers where and how often.
- Daemon build-cycle and provider-probe failures live in `status.json`, which
  ages out.

The incident ledger gives every one of these a durable, deduplicated,
machine-readable destination, surfaced through doctor and daemon-health and
triaged through a small CLI — feeding the governed spec loop with real failure
traffic. It is explicitly NOT autonomous self-modification: the ledger records
and displays; humans (and governed rounds) decide.

## 2. Operator decisions (2026-07-25)

1. **Capture scope: all three classes** — hard failures (build/extract/
   artifacts/graph-load), inconclusive query results, daemon/provider
   incidents.
2. **Full triage lifecycle** — `graphite incidents list|ack|resolve`,
   event-sourced (ack/resolve are appended entries, never mutations).
3. **Approach 1** — per-repo JSONL ledger following the `usage_ledger.py`
   idiom, dedup at read time by fingerprint, daemon-global ledger in the
   daemon state dir.

## 3. Storage

New module `src/graphite/incident_ledger.py` (sibling of `usage_ledger.py`,
same conventions).

**Paths.**
- Per-repo ledger at `local_dir(root)/incidents.jsonl` → `.graphite/local/incidents.jsonl`
  with rotation to `incidents.jsonl.1` (matching `usage_ledger`'s actual layout:
  `usage.jsonl` / `usage.jsonl.1` under `.graphite/local/`). Rotation at
  `MAX_LEDGER_BYTES` (5 MB, same constant semantics) via `os.replace`;
  readers read rotated generation first, then current (mirror
  `usage_ledger.iter_entries`).
- Daemon-global: `<daemon base>/.graphite-daemon/incidents.jsonl` (same dir as
  `status.json`, `daemon.py:464`), same rotation.

**Entry shapes** (one JSON object per line; unknown keys ignored on read):

Occurrence:
```json
{"schema": 1, "kind": "occurrence", "fingerprint": "9f2c11ab04d1e772",
 "ts": "2026-07-25T14:03:11Z", "class": "build", "code": "parse_error",
 "subject": "src/pkg/broken.py", "detail": "parse_error: <exception text>"}
```

Lifecycle:
```json
{"schema": 1, "kind": "ack", "fingerprint": "9f2c11ab04d1e772",
 "ts": "2026-07-25T15:00:00Z", "note": "known vendored file"}
```
(`kind: "resolve"` identical shape; `note` optional in both.)

**Fields.**
- `schema`: int, `1`.
- `kind`: `"occurrence" | "ack" | "resolve"`.
- `fingerprint`: first 16 hex chars of SHA-256 over
  `f"{class}|{code}|{subject}"`. Stable across occurrences; `detail` and
  timestamps do NOT enter the fingerprint.
- `ts`: UTC ISO-8601 with `Z` suffix, seconds precision.
- `class`: `"build" | "query" | "daemon"`.
- `code` (closed set, round 1): `parse_error`, `read_error`, `worker_error`
  (build); `artifact_malformed`, `graph_load_failed` (build); 
  `query_inconclusive` (query); `daemon_build_failed`,
  `provider_probe_failed` (daemon).
- `subject`: repo-relative path (build codes), `"<verb> <target>"` (query),
  project root or provider name (daemon). Capped 512 chars
  (`daemon_health._MAX_ROOT_LENGTH` precedent).
- `detail`: free text, capped 2048 chars
  (`daemon_health._MAX_LAST_ERROR_LENGTH` precedent). For
  `query_inconclusive`, detail carries the ratios, e.g.
  `"imports 0.05, calls 0.26, healthy false"`.
- `note`: lifecycle entries only, capped 2048.
- Whole line capped 8 KB; a record that would exceed it truncates `detail`
  further.

## 4. Module API

```python
def record_incident(ledger_dir, *, klass, code, subject, detail) -> None
    # Fail-open: never raises (OSError, encoding, rotation errors swallowed).
    # Appends an occurrence entry; rotates at cap first (usage_ledger pattern).

def append_lifecycle(ledger_dir, fingerprint, kind, note=None) -> bool
    # kind in ("ack", "resolve"). Returns False (no write) if the fingerprint
    # has no occurrence in the ledger; True on append. May raise on I/O —
    # it is invoked interactively by the CLI, not from fail-open paths.

def read_incident_entries(ledger_dir) -> tuple[list[dict], int]
    # Returns (entries, skipped). Rotated generation first, then current;
    # corrupt/oversized lines skipped and counted; never raises.

def fold_incidents(entries) -> list[IncidentView]
    # Group by fingerprint. IncidentView: fingerprint, class, code, subject,
    # state ("open"|"acked"|"resolved"), first_seen, last_seen, count,
    # last_detail, last_note. State = latest lifecycle entry unless an
    # occurrence is newer than a resolve -> "open" again (reopen). An ack
    # followed by newer occurrences stays "acked" (ack means "known, noisy").

def incident_fingerprint(klass, code, subject) -> str
```

`fold_incidents` sorting: open first, then acked, then resolved; within a
state, most-recent `last_seen` first.

**One location mechanism:** every function takes `ledger_dir: Path` (the
directory holding `incidents.jsonl`). Two thin helpers provide it:
`repo_ledger_dir(root) -> Path` (= `usage_ledger.local_dir(root)`) and the
daemon passes its state dir directly. `record_incident`/`append_lifecycle`
therefore take `ledger_dir`, not `root`.

## 5. Capture points (writers)

All writers call `record_incident` and are individually fail-open. No writer
runs inference (Canonical Graph Isolation). Volume is naturally bounded: one
entry per fingerprint per operation (build run / query invocation / daemon
cycle) — the build writer dedups within its own run before writing.

1. **Extraction errors.** `extract_all` (`extract/ast.py:1009`) holds the
   `entry` at both the serial loop and the futures map. It collects
   `{"code": <prefix before first ':'>, "subject": entry.rel_path,
   "detail": result.error}` for every per-file result with a non-None
   `.error` into a new `errors: list[dict]` field on the merged
   `ExtractionResult`. Per-file `.error` strings and the extraction cache
   format are UNTOUCHED (no cache-version bump). The legacy merged `.error`
   field keeps its current last-error value. `_build_project` (cli.py, after
   the `extract_all` call at ~line 200) records one incident per collected
   error dict, class `"build"`.
2. **Malformed artifacts.** `persisted_resolution` (health.py) gains an
   optional `on_error` callback (default None → today's silent behavior).
   The call sites that know a repo root (agent_hooks strict path, `cmd_check`)
   pass a callback that records `artifact_malformed` with
   `subject=".graphite_analysis.json"` and the exception text. The reader
   itself stays fail-open and inference-free.
3. **Graph-load failures.** The `GraphReadError` handler used by query/impact/
   context commands (cli.py:271 idiom) records `graph_load_failed`,
   `subject="graph-out/graph.json"`, detail = error code. Class is `"build"`
   for both artifact codes (`artifact_malformed`, `graph_load_failed`): the
   class names the subsystem that owns the defect (the built artifact), not
   the command that tripped over it.
4. **Inconclusive queries.** Where the CLI layer receives a result with
   `inconclusive: true` (query verbs via `_attach_resolution`, `_impact`,
   `build_context`), it records `query_inconclusive`,
   `subject=f"{verb} {target}"`, detail = the ratios line. Recorded once per
   invocation, only when a repo root is in play.
5. **Daemon.** Build-cycle failure for a project → that project's per-repo
   ledger (`daemon_build_failed`, subject = project root, detail = capped
   error). Watcher-level and provider-probe failures not tied to a project →
   the daemon-global ledger (`provider_probe_failed` / `daemon_build_failed`
   with subject = provider or `"daemon"`). Daemon-executed surface: rollout
   requires a daemon restart (operator-run).

## 6. CLI

`graphite incidents` subcommand group (inference-free, like all canonical
commands):

- `graphite incidents list [--json] [--all] [--global]` — folded views,
  default open+acked only (`--all` adds resolved); `--global` reads the
  daemon-global ledger instead of the repo's. Human output: one line per
  incident — `state fingerprint class/code subject ×count last_seen` — plus
  a trailing `skipped N corrupt lines` note when nonzero. JSON output:
  `{"schema_version": 1, "incidents": [IncidentView...], "skipped": N}`.
- `graphite incidents ack <fingerprint> [-m NOTE] [--global]`
- `graphite incidents resolve <fingerprint> [-m NOTE] [--global]`
  — both print the updated folded view of that incident; unknown fingerprint
  → exit 1 with `unknown fingerprint` (no write).

## 7. Surfacing

- **Doctor** (`cmd_doctor`, cli.py:419): new "incidents" section — counts by
  state and class, plus the top 10 open incidents (fingerprint, code,
  subject, count). Reads fail-open; a broken ledger is reported inline as
  `incidents: unreadable (<reason>)`, never fatal to doctor.
  Shipped narrowing: the doctor summary is state counts only —
  `"{open} open / {acked} acked"` (plus a `", N corrupt line(s)"` suffix when
  `skipped` is nonzero) — with the top-10 open lines carrying per-line
  `fingerprint code subject xcount`, so class is visible per open incident
  but there is no separate by-class breakdown in doctor output (unlike
  daemon-health's `by_class` map below); a conscious narrowing from this
  bullet's original description, not a bug.
- **Daemon-health** (`daemon_health.py`): the health report gains
  `incidents: {"open": N, "acked": N, "by_class": {...}}` for the global
  ledger plus per-project open counts, following that module's existing
  caps/shape conventions.

## 8. Contracts and docs

- Published schema `docs/schemas/incidents.v1.schema.json` covering the
  `incidents list --json` envelope (query-interface program pattern), plus a
  compat test in the published-schemas test module.
- `docs/agent-integration.md`: new section — when to check incidents, what
  ack/resolve mean, fingerprint stability, and that INCONCLUSIVE queries
  self-record.
- `GRAPHITE.md` managed template: one new item telling agents
  `python -m graphite incidents list` exists and that recurring incidents
  belong in a governed round, not ad-hoc fixes. `DOC_VERSION` lands at 8
  (`init.py:17`): the round bumped 6 → 7, then the final-review style fix
  to the template item consumed 7 → 8 intra-branch (the init mechanism
  skips exact-version matches, so any template content change bumps the
  version; 7 never shipped to a consumer). Consumer re-init at rollout.

## 9. Governance and error handling

- **Fail-open capture, everywhere:** a ledger write failure must never break
  the operation being recorded. `record_incident` swallows all I/O errors.
- **No inference:** ledger, CLI, doctor, daemon-health additions are all
  canonical/inference-free surfaces.
- **Append-only:** nothing ever rewrites or deletes ledger lines; lifecycle
  is event-sourced; rotation moves whole files.
- **Honest reads:** corrupt lines are skipped AND counted; the skip count is
  visible in `--json` and doctor output. An unreadable ledger is reported as
  unreadable, never as "no incidents". The unreadable channel is implemented
  as the `skipped` count itself, not a separate flag: an unreadable
  generation file (exists but `read_text` raises — permissions, path is a
  directory, decode failure) counts as >=1 `skipped`, same as a corrupt line.
- **Caps:** subject 512, detail/note 2048, line 8 KB, file 5 MB with one
  rotated generation — bounded disk, bounded output.

## 10. Testing

Repo conventions: real files under `tmp_path`, real pipeline, no mocks.

- **Unit (new `tests/test_incident_ledger.py`):** fingerprint stability and
  detail-independence; fold semantics (open → ack → resolve; reopen on
  occurrence-after-resolve; ack stays acked under newer occurrences);
  rotation at cap; corrupt-line skip+count; caps (subject/detail/line);
  fail-open write to a read-only/unwritable dir (no raise); lifecycle append
  refuses unknown fingerprints.
- **Integration:** build over a fixture containing a syntactically broken
  `.py` file → `parse_error` incident with the right subject appears in the
  ledger and in `incidents list`; inconclusive query on an unhealthy fixture
  graph → `query_inconclusive` incident; `cmd_check` on a corrupted
  `.graphite_analysis.json` → `artifact_malformed`; doctor output contains
  the incidents section; `incidents list --json` validates against the
  published schema.
- **Daemon:** unit-level tests of the daemon writer hooks per existing daemon
  test idioms (no live daemon in tests).
- **Extraction-errors plumbing:** `extract_all` merged result carries
  `errors` for a broken file (serial and worker paths); `_merge`'s legacy
  `.error` behavior unchanged.

## 11. Rollout and acceptance (operator-gated, post-merge)

1. Merge + push per finishing-a-development-branch (operator chooses).
2. **Daemon restart required** (daemon writer is a daemon-executed surface) —
   operator runs Stop-Process.
3. Re-init consumer repos for DOC_VERSION 8.
4. Live acceptance: (a) seed a deliberately broken file in a scratch repo,
   build, confirm the incident + doctor section + `list`/`ack`/`resolve`
   round-trip; (b) confirm daemon-health shows incident counts after a
   daemon cycle; (c) confirm a ledger-write failure (read-only dir) leaves
   the build exit code unchanged.
5. Update memory; notify aramid's agent only if the published schema matters
   to them (optional this round).
