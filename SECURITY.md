# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 1.x | yes — fixes ship in the next patch release of the current minor |
| < 1.0 | no |

## Reporting a vulnerability

Use GitHub **Private vulnerability reporting** on this repository
(Security → Report a vulnerability, or
<https://github.com/jared0565/graphite/security/advisories/new>). Do not open
a public issue or pull request for an exploitable defect; `CONTRIBUTING.md`
already forbids publishing exploit details.

Include the output of `graphite --version` (the version and the engine
fingerprint), the platform, and the smallest reproduction you have. You will
get an acknowledgement within 7 days and a fix or a documented mitigation
before any public disclosure. Reports are handled by the maintainer named in
`.github/CODEOWNERS`.

## What graphite treats as untrusted

Repository files, file names, symlinks, Git metadata, generated graph data,
and model output are all untrusted input (see "Security expectations" in
`CONTRIBUTING.md`). A finding that any of them can escape path containment,
reach a shell, or drive an unbounded read is in scope. So is anything that
makes a generated launcher, hook, or `.mcp.json` entry run a `python -m
graphite` without `-P`, because that is the shape a repo-local shadow needs.

## What is checked on every push

The same scanner configuration runs in the local git hooks and in the CI
`security` job, against the committed `aramid.toml` and
`.aramid-suppressions.toml`: gitleaks, semgrep, ruff's security rules, the
repo-root shadow check, mypy, and the full test suite through the
development interpreter. The CI job asserts from the gate's own JSON report
that every one of those scanners actually ran and that the report holds no
block-tier finding — the verdict is read from the report, never from the
gate's exit status alone — so a scanner that silently never fired, or a
gate that exits 0 over blocking findings, cannot read as clean. There is no dependency-vulnerability
audit in the gate today; dependabot's security updates are the standing
control for that. Suppressions are reviewed in the repository, never
applied ad hoc.
