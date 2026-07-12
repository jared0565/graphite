# Contributing to Graphite

Contributions should be focused, reviewable changes that preserve Graphite's deterministic, local-first behavior and its secure handling of untrusted repositories.

## Development setup

Graphite requires Python 3.11 or newer, Git, and an isolated virtual environment. Clone and enter the repository using paths appropriate for your system:

```bash
git clone <repository-url>
cd graphite
python -m venv .venv
```

Activate `.venv` using the command for your shell. After following the repository's package-validation policy for every exact package name, install the declared development extra:

```bash
python -m pip install -e ".[dev]"
```

For MCP development, install the optional MCP extra as well:

```bash
python -m pip install -e ".[dev,mcp]"
```

Do not add or install dependencies without first validating the exact package name under the repository package-validation policy. Prefer existing declared dependencies and standard-library capabilities when they are sufficient.

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

## Security expectations

Treat repository files, file names, symlinks, Git metadata, generated graph data, and model output as untrusted input. Pass subprocess arguments as an argument vector and never interpolate untrusted values into a shell command. Preserve path containment, bounded I/O, explicit timeouts, atomic writes, output encoding, and safe error redaction at every relevant boundary.

Never place credentials or other secrets in commits, fixtures, logs, examples, generated artifacts, or prompts. Do not publish exploitable vulnerability details in a public issue or pull request. This repository does not invent or imply a private security contact; use an officially documented private reporting channel if one exists, and otherwise disclose only through an appropriate repository-owner channel.

Security-sensitive changes should include tests for hostile input, boundary violations, partial failure, and safe cleanup where applicable.

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
