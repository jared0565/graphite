# Claude Code and Codex Router Acceptance Evidence

**Status:** Offline implementation complete; bounded live acceptance remains open.

## Scope and authority

Governed development execution is limited to authenticated Claude Code and Codex
subscription CLIs. OpenRouter is reserved for application inference. Ollama report
enrichment remains a separate compatibility feature, while earlier Ollama routing
records are historical and non-replayable.

Capability evidence is not authorization authority. A verified snapshot proves only
the executable/profile facts observed at verification time. Every execution,
cross-provider review, acceptance, policy promotion, rollback, and cleanup retains a
separate default-No human authority gate. Historical Ollama documentation refers to
`routing.registry.BUNDLED_PROFILES`; that legacy allowlist does not authorize a
Claude Code or Codex execution.

## Offline evidence

Implementation through Task 8 is recorded at commit `12a1bfa`. Verification on
2026-07-18 produced:

- focused telemetry/policy/storage/service/CLI checks: 123 passed, 1 intentional
  platform or capability skip;
- complete routing selection: 355 passed, 1 intentional skip, 1143 deselected;
- complete offline suite: 1457 passed, 44 intentional platform or optional-tool
  skips;
- Ruff checks for changed routing, CLI, and test files: clean;
- Git whitespace/error check: clean;
- branch-source Graphite build and strict validation: 6196 nodes, 13582 edges,
  zero errors, and zero warnings.

The tests establish exact CLI identity checks, strict structured output, isolated
worktrees, immutable approval/audit bindings, fail-closed diff policy, bounded
validation, separate cross-provider review, telemetry field minimization, unknown
subscription cost, signed policy candidates, human-only promotion, and
evidence-preserving rollback.

## Schema migration and rollback drill

The disposable schema fixture reconstructs v3, creates representative legacy
history, migrates a copy to v4, and verifies the automatically created v3 backup and
SHA-256 marker. It confirms v4 integrity and foreign keys, ensures live legacy
provider attempts are quarantined, stops all fixture writers, restores the verified
v3 backup, and proves the v3 reader can recover the schema version and historical
rows. The exercised drill passed. No operator routing database is used by this
drill.

Operational rollback requires the same sequence: stop writers, verify the backup
digest and SQLite integrity, preserve the v4 database, atomically restore the v3
backup, deploy matching v3 code, validate history with a read-only path, and only
then resume writers. Missing or invalid backup evidence requires continued shutdown
and a tested forward fix.

## Remaining external gates

This capability is not yet production-ready. It still requires separately approved,
bounded live calls on a synthetic repository: no-edit verification for both CLIs,
one isolated edit smoke for each provider, one read-only cross-provider high-risk
review, audit persistence verification, and final rollback confirmation. A failure
does not authorize retry, fallback, or model substitution; another attempt requires
a new manifest and explicit approval.
