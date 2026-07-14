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

This bootstrap uses the repository-reviewed declared `.[dev]` extra and does not require per-package validation. The validator rule applies before optional activation installs and before adding or changing external dependencies or package names.

For MCP development, the mandatory repository package-validation policy requires a trusted local validator before activation. Set `GRAPHITE_PACKAGE_VALIDATOR` to the absolute path of the trusted `validate-packages.cjs` maintained by your environment. If the variable is unset, relative, missing, or does not name an existing file, stop. Never execute a relative repository-local validator. Do not download a validator, discover an unknown replacement, or use an unverified fallback.

PowerShell:

```powershell
if (
  [string]::IsNullOrWhiteSpace($env:GRAPHITE_PACKAGE_VALIDATOR) -or
  -not ([System.IO.Path]::IsPathFullyQualified($env:GRAPHITE_PACKAGE_VALIDATOR)) -or
  -not (Test-Path -LiteralPath $env:GRAPHITE_PACKAGE_VALIDATOR -PathType Leaf)
) { throw "GRAPHITE_PACKAGE_VALIDATOR is unset, relative, or missing; stop." }
node $env:GRAPHITE_PACKAGE_VALIDATOR mcp
if ($LASTEXITCODE -ne 0) { throw "Package validation failed; stop." }
```

POSIX shell:

```sh
if [ -z "${GRAPHITE_PACKAGE_VALIDATOR:-}" ]; then
  printf '%s\n' 'GRAPHITE_PACKAGE_VALIDATOR is unset; stop.' >&2
  exit 1
fi
case "$GRAPHITE_PACKAGE_VALIDATOR" in
  /*) ;;
  *) printf '%s\n' 'GRAPHITE_PACKAGE_VALIDATOR must be an absolute POSIX path; stop.' >&2; exit 1 ;;
esac
if [ ! -f "$GRAPHITE_PACKAGE_VALIDATOR" ]; then
  printf '%s\n' 'GRAPHITE_PACKAGE_VALIDATOR is missing; stop.' >&2
  exit 1
fi
node "$GRAPHITE_PACKAGE_VALIDATOR" mcp || exit 1
```

Only after the applicable validator command succeeds, the package-validation policy permits installing the optional MCP extra:

```bash
python -m pip install -e ".[dev,mcp]"
```

Installing a repository-reviewed declared bootstrap extra is different from adding or changing an external dependency or package name. For the latter:

1. Verify its exact registry name and project identity to prevent typo-squatting or substitution.
2. Review its maintenance activity and reputation, license compatibility, published security advisories, and transitive dependency risk.
3. Obtain maintainer approval before changing dependency declarations or installing it for repository work.
4. Run the configured repository package validator before installation; an unset, missing, or failed validator blocks the install.

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

Before any optional activation install, follow the repository package-validation policy and fail-closed `GRAPHITE_PACKAGE_VALIDATOR` workflow above. For TypeScript, run `node $env:GRAPHITE_PACKAGE_VALIDATOR typescript` in PowerShell or `node "$GRAPHITE_PACKAGE_VALIDATOR" typescript` in a POSIX shell, then stop on any non-zero result.

The command below uses a clearly marked placeholder. Replace it with the trusted absolute validator path maintained by your environment; never use the placeholder literally or resolve it inside the repository:

```text
node "<absolute-path-to-validator>" typescript
```

In shorthand, the validated target is `validate-packages.cjs typescript`. If validation exits 1, stop; if registry lookup is unavailable, manually verify the spelling and identity before proceeding. After validation, use the target project's existing package manager and project-local dependency declaration. Do not use a global TypeScript install.

## TypeScript activation maintenance contract

`graphite init` and `graphite bootstrap` are the only callers allowed to request automatic TypeScript activation. Their normal onboarding writes complete first; activation is a consent-gated post-step before the normal build and validation stages. Build, report, check, doctor, daemon, watch, MCP, agents, JSON, CI, redirected streams, and `--yes` must never gain prompt or installation authority. Preserve tests for those boundaries whenever CLI dispatch or imports change.

Eligibility is deliberately conservative. A selected root needs contained `.ts`/`.tsx` or `tsconfig.json` evidence, no resolvable project-local TypeScript, a regular contained `package.json`, exactly one supported root lockfile, and an external trusted manager executable. If `package.json#packageManager` exists, its manager family must match the lockfile. Multiple lockfiles, nested-only package roots, invalid metadata, unsafe manager configuration, non-canonical dependency sources, or unsupported/unknown versions are `guidance_only` without mutation.

The implemented automatic matrix is:

| Manager | Supported automatic major | Required root lockfile | Disallowed root configuration |
| --- | --- | --- | --- |
| npm | npm 8–11 | `package-lock.json` | `.npmrc`, `npm-shrinkwrap.json` |
| pnpm | pnpm 11 | `pnpm-lock.yaml` | `.npmrc`, `pnpm-workspace.yaml`, `.pnpmfile.cjs`, `.pnpmfile.mjs` |
| Bun | Bun 1 | exactly one of `bun.lock` or `bun.lockb` | `.npmrc`, `bunfig.toml` |
| Yarn | none | `yarn.lock` | `.yarnrc.yml`, `.yarnrc`, `.yarn/plugins` |

Yarn is `guidance_only`: automatic execution remains disabled because its version/configuration families do not yet provide one verified unattended registry, credential-isolation, and no-build contract. Do not widen supported majors or flags without official-source verification plus adapter, argv, environment, and adversarial tests.

Only explicit interactive `y` or `yes` consent may proceed. The trusted validator must be absolute, a regular file, outside the selected root, revalidated before launch, and invoked with exact argument `typescript` before any package-manager action. Automatic npm, pnpm, and Bun use `https://registry.npmjs.org/`, an isolated configuration/home, stripped ambient credentials and repository overrides, fixed argv, closed stdin, and lifecycle scripts disabled. Private registries and mirrors are manual-only. Activation installs only local development dependency `typescript` and does not infer `@types/*` or any adjacent tool.

Manual TypeScript workflow, in fixed order:

1. Set `GRAPHITE_PACKAGE_VALIDATOR` to the trusted environment-specific absolute validator path outside the project.
2. Fail closed if it is unset, relative, missing, repository-contained, or not a regular file.
3. Run the validator for the exact package name `typescript`; stop on any non-zero result.
4. Only after successful validation, use the existing project manager to add local development dependency `typescript` with lifecycle scripts disabled under the operator's registry and credential policy.
5. Rerun `graphite doctor` or onboarding to confirm detection.

Never add a global TypeScript command. `validation_failed`, `installation_failed`, and `verification_failed` are fatal to the overall onboarding exit and return 1, but completed onboarding files and reviewable manager changes remain preserved. Do not add automatic rollback: concurrent editors or package managers may own those changes.

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
