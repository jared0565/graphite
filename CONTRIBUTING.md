# Contributing to Graphite

Contributions should be focused, reviewable changes that preserve Graphite's deterministic, local-first behavior and its secure handling of untrusted repositories.

## Development setup

Graphite requires Python 3.11 or newer, Git, and an isolated virtual environment. Clone and enter the repository using paths appropriate for your system:

```bash
git clone https://github.com/jared0565/graphite.git
cd graphite
python -m venv .venv
```

If you contribute from a fork, clone the fork's verified URL instead. Keep URLs and other environment-specific values outside shell metacharacters.

Activate `.venv` using the command for your shell. Before installing already-declared extras, inspect `pyproject.toml` and confirm the exact extra and dependency names. Then install the declared development extra:

```bash
python -m pip install -e ".[dev]"
```

For MCP development, the mandatory repository package-validation policy requires this check before activation:

```text
node "C:\Users\fbmac\atlas\Codex\.codex_state\user_home\scripts\validate-packages.cjs" mcp
```

If validation exits 1, stop. If registry lookup is unavailable, manually verify the exact package spelling and identity. Only after successful validation, install the optional MCP extra as well:

```bash
python -m pip install -e ".[dev,mcp]"
```

Installing these declared extras is different from proposing a new dependency. Before adding or installing any new dependency:

1. Verify its exact registry name and project identity to prevent typo-squatting or substitution.
2. Review its maintenance activity and reputation, license compatibility, published security advisories, and transitive dependency risk.
3. Obtain maintainer approval before changing dependency declarations or installing it for repository work.
4. Run the repository package validator before installation; a failed validation blocks the install.

Prefer existing declared dependencies and standard-library capabilities when they are sufficient. Never invent a package name or assume that a similarly named registry project is the intended dependency.

## Engineering workflow

1. Start from a clean, current base and create a descriptively named, focused branch.
2. Read `ARCHITECTURE.md` before changing module boundaries, pipeline stages, artifacts, extension points, or failure behavior.
3. Write the smallest behavior test first and run it to observe the expected failure.
4. Implement the minimal complete change, including deliberate handling of errors and edge cases, then rerun the focused checks.
5. Update the owning documentation whenever a command, architecture boundary, workflow, or release process changes.

Keep changes cohesive and preserve existing behavior unless the contribution explicitly and safely changes its contract.

## Testing and quality gates

Run a focused test while iterating. Replace the placeholder path with the test module that owns the behavior you changed:

```bash
python -m pytest tests/test_relevant_area.py -q
```

Before requesting review, run all required checks:

```bash
python -m ruff check .
python -m pytest -q
python -m graphite --help
```

Focused tests shorten the feedback loop; they do not replace the full Ruff, pytest, and CLI smoke checks. Add behavior-level tests for new or changed contracts and ensure failures are meaningful rather than dependent on local machine state.

Doctor changes require focused tests for stable doctor JSON, deterministic ordering, redaction, end-to-end deadlines, process cleanup, missing tools, and optional semantics. Cover both successful and adversarial outcomes. Cross-platform behavior must have Windows and POSIX coverage; use narrowly scoped platform skips only when an operating-system primitive truly has no counterpart, and test the portable contract on every platform.

No live provider calls in automated tests are permitted. Exercise network and subprocess behavior through a fake worker, fake provider, and fake process boundary with controlled time, output, failures, and descendants. Tests must remain offline, deterministic, and safe to run without credentials.

## Security expectations

Treat repository files, file names, symlinks, Git metadata, generated graph data, and model output as untrusted input. Pass subprocess arguments as an argument vector and never interpolate untrusted values into a shell command. Preserve path containment, bounded I/O, explicit timeouts, atomic writes, output encoding, and safe error redaction at every relevant boundary.

Never place credentials or other secrets in commits, fixtures, logs, examples, generated artifacts, or prompts. Do not publish exploitable vulnerability details in a public issue or pull request. This repository does not invent or imply a private security contact; use an officially documented private reporting channel if one exists, and otherwise disclose only through an appropriate repository-owner channel.

Security-sensitive changes should include tests for hostile input, boundary violations, partial failure, and safe cleanup where applicable.

For doctor work, assume the selected root is hostile. Neither text nor JSON diagnostics may include raw outputs, raw errors, secrets, or absolute paths. Preserve private external workspaces, no-follow identity checks, native child-process containment, bounded parsing and output, redacted error categories, and deadline-coordinated cleanup.

Before any optional-integration activation install, follow the repository package-validation policy. The exact user guardrail for TypeScript is:

```text
node "C:\Users\fbmac\atlas\Codex\.codex_state\user_home\scripts\validate-packages.cjs" typescript
```

In shorthand, the validated target is `validate-packages.cjs typescript`. If validation exits 1, stop; if registry lookup is unavailable, manually verify the spelling and identity before proceeding. After validation, use the target project's existing package manager and project-local dependency declaration. Do not use a global TypeScript install.

## Model-agnostic design

Core scanning, graph construction, validation, review, and export must remain deterministic with LLM functionality disabled. Optional model integrations belong behind a `CompletionProvider`-style interface; no vendor SDK, model, host, or API key may become mandatory for core behavior.

Treat model output as untrusted enrichment, never as authoritative control data. Integrations require explicit configuration, timeouts, sanitized errors, and fake-backed tests that do not depend on network access or paid services. Keep provider-specific logic in adapters so the core remains portable and model-agnostic.

## Contribution conventions

- Keep each pull request coherent and use a descriptive branch name.
- Write imperative commit subjects that describe the change.
- Preserve compatibility unless a documented migration is intentional; include migration steps and deprecation impact when contracts or artifacts change.
- Add behavior tests and update the owning documentation for user-visible or operational changes.
- Exclude generated output, unrelated edits, editor state, caches, credentials, and other local-only files.

## Pull request checklist

- [ ] The diff is clean, focused, and free of unrelated or generated files.
- [ ] Focused tests and the full test suite pass.
- [ ] Ruff passes.
- [ ] Security boundaries, hostile inputs, and failure modes were reviewed and tested where relevant.
- [ ] User, architecture, workflow, and release documentation is current.
- [ ] Compatibility impact and any required migration are documented.
- [ ] Deterministic, local-first, model-agnostic behavior is preserved.
- [ ] No secrets appear in code, history, fixtures, logs, examples, artifacts, or prompts.
