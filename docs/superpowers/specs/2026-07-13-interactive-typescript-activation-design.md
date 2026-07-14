# Interactive TypeScript Activation Design

**Date:** 2026-07-13  
**Status:** Implemented and acceptance-tested

## Objective

Let Graphite offer to add project-local TypeScript compiler support during interactive project onboarding without granting dependency-install authority to normal builds, doctor checks, the daemon, MCP, agents, CI, or redirected input.

Graphite must remain safe and useful when activation is declined, unavailable, ambiguous, or fails. Tree-sitter parsing and heuristic resolution continue to work without the npm `typescript` package.

## Goals

- Detect a credible need for compiler-backed TypeScript support during `graphite init` and `graphite bootstrap`.
- Ask once, defaulting to No, before any validator, network, manifest, lockfile, or dependency-store mutation.
- Validate the exact package name `typescript` through the trusted external package validator before installation.
- Use the selected project's existing, unambiguous package manager.
- Add only `typescript` as a development dependency with lifecycle scripts disabled.
- Re-detect project-local TypeScript after installation and return a typed, sanitized outcome.
- Preserve completed onboarding files when an approved activation later fails, while returning a non-zero exit code for that explicit failed action.

## Non-goals

- Global TypeScript installation or global Node module discovery.
- Installing `@types/*`, frameworks, transpilers, bundlers, language servers, or other inferred packages.
- Creating a new `package.json` or lockfile strategy for a project that does not already have an eligible package-manager setup.
- Automatically installing from `build`, `report`, `doctor`, `daemon`, `watch`, MCP, or any non-interactive execution path.
- Repairing broken existing TypeScript installations or changing a project's selected TypeScript version.
- Running package lifecycle scripts.
- Supporting private or repository-redirected registries in the automatic path.

## Eligibility and detection

Activation is evaluated only after normal `init` or `bootstrap` onboarding writes complete.

The selected root is resolved canonically and treated as hostile. Detection is bounded by the existing configured file-count and traversal controls and does not follow escaping symlinks or Windows reparse points.

A project is eligible only when all of the following are true:

1. The selected root contains at least one contained `.ts` or `.tsx` source file, or a contained `tsconfig.json`.
2. Project-local `typescript` is not currently resolvable from the selected root.
3. The selected root contains a regular, contained `package.json`.
4. Exactly one supported root lockfile identifies the package manager:
   - `package-lock.json` for npm;
   - `pnpm-lock.yaml` for pnpm;
   - `yarn.lock` for Yarn;
   - `bun.lock` or `bun.lockb` for Bun.
5. If `package.json#packageManager` is present, it agrees with the lockfile family.
6. The package-manager executable resolves to a trusted executable outside the selected root.

Multiple supported lockfile paths—including both Bun lockfile formats—conflicting `packageManager` metadata, a missing root manifest, a missing lockfile, nested-only package roots, or an unsupported manager produce `guidance_only`. Graphite does not choose a workspace package or default to npm. A user may rerun onboarding with a nested package directory as the selected root when that directory independently satisfies the contract.

## Interaction contract

An installation prompt is allowed only when both stdin and stdout are interactive terminals and onboarding was not invoked with `--yes`. Redirected input, CI, agent execution, and other non-interactive contexts never install.

The single prompt is:

```text
Project-local TypeScript is missing. Install it with <manager> as a development dependency? [y/N]
```

Only an explicit case-insensitive `y` or `yes` authorizes validation followed by installation. Empty input, EOF, malformed input, or any other response means `declined`. There is no remembered consent across repositories or invocations.

Non-interactive execution returns `guidance_only`, prints or emits the policy-compliant manual workflow without running the validator, and exits successfully because TypeScript remains optional.

## Components

### Activation service

A dedicated TypeScript activation module owns detection, prompt eligibility, validation, installation, verification, and typed results. `init` and `bootstrap` are thin callers. No other command imports an installation entry point.

The service accepts explicit dependencies for filesystem inspection, terminal capability, executable resolution, validator execution, package-manager execution, and clock/deadline handling so security behavior is testable without live registry calls.

### Package-manager adapters

Adapters exist for npm, pnpm, Yarn, and Bun. Each adapter provides:

- lockfile and `packageManager` identity rules;
- a fixed argument-vector command for adding exact package `typescript` as a development dependency;
- a tested, version-compatible mechanism that disables all lifecycle/build scripts;
- a supported-version predicate;
- sanitized outcome mapping.

An unknown version, an unavailable no-scripts mechanism, or conflicting manager evidence fails closed to `guidance_only`. The implementation plan must verify every supported command and no-scripts mechanism against the package manager's official documentation; unverified flags are not accepted.

### Typed result

The activation service returns one of these fixed outcomes:

- `installed`: validation, installation, and re-detection succeeded.
- `already_available`: local TypeScript was already resolvable.
- `not_applicable`: no `.ts`, `.tsx`, or `tsconfig.json` evidence exists.
- `declined`: the interactive user did not authorize installation.
- `guidance_only`: execution was non-interactive or package-manager selection was unsafe or ambiguous.
- `validation_failed`: the trusted package validator was missing, invalid, or rejected `typescript`.
- `installation_failed`: the bounded package-manager process failed or timed out.
- `verification_failed`: installation exited successfully but local TypeScript was still not resolvable.

Results include only fixed booleans, manager identifiers, relative manifest/lockfile names, and fixed reason codes. They never contain credentials, raw subprocess output, registry responses, absolute system paths, or repository source.

## Validation and registry trust

After consent, Graphite requires `GRAPHITE_PACKAGE_VALIDATOR` to name an absolute, existing regular file outside the selected root. Relative, missing, repository-contained, or non-file paths stop with `validation_failed`. Graphite invokes the validator with a trusted external Node executable and the exact single argument `typescript`. There is no download, search, fallback validator, alternate spelling, or continuation after a non-zero result.

Automatic installation may contact only the trusted canonical registry used by the validator, defaulting to `https://registry.npmjs.org/`. The package-manager adapter runs with an isolated configuration/home and strips ambient registry tokens and repository-controlled registry overrides. Repository `.npmrc`, Yarn configuration, environment registry overrides, and equivalent files must not redirect the automatic request or receive ambient credentials.

If the manager cannot be safely constrained to the trusted registry without honoring project-controlled credentials or configuration, Graphite returns `guidance_only`. Private registries and enterprise mirrors remain supported through the manual workflow under the user's existing package-management policy, not through automatic activation.

## Process and filesystem security

- Validator and package-manager commands use fixed argv with `shell=False`.
- Executables must resolve outside the selected root and are revalidated immediately before launch. POSIX package-manager launchers may use a bounded external symlink route while argv preserves the manager basename needed by Corepack-style dispatch. Immutable provenance records every root-to-leaf directory/component binding used by the launcher and its symlink targets: path, identity, owner, mode, and symlink target text where applicable. A bounded prefix captured by the same pinned-file read accepts only exact `#!/usr/bin/env node`, `#!/usr/bin/node`, or `#!/bin/node` manager scripts or recognized ELF/Mach-O native managers. Node scripts run as canonical trusted-Node plus lexical-launcher argv; they never use child `PATH` for interpreter selection. Ambiguous and unsupported interpreters fail closed to guidance. Both Node and the complete route are revalidated before version and install launches. Root- or current-user-owned sticky directories such as the canonical temporary directory are allowed only when sticky behavior plus trusted child ownership prevents cross-user replacement; group/world-writable non-sticky ancestors are rejected. Directory-component symlinks, including `/tmp`-style canonical redirects, follow the same bounded provenance rules. Cycles, dangling or excessive routes, unsafe ownership, selected-root crossings, and any component, chain, interpreter, or target replacement fail closed. Validators, control files, and Windows executables retain their strict non-symlink rules. Same-UID interference in the final interval between revalidation and path-based OS launch remains part of the existing local-user trust boundary rather than a cross-process-isolation claim.
- The selected root is the working directory but never an executable search location.
- Stdin is closed after the one Graphite-owned prompt; child processes cannot prompt.
- A single shared deadline bounds validation, installation, verification, and process-tree cleanup.
- Stdout and stderr are byte-capped and converted to sanitized fixed categories. Raw output is never returned by doctor JSON, onboarding JSON, logs, or exceptions.
- The child environment is allowlisted, lifecycle scripts are disabled, and no Graphite LLM/API credential is forwarded.
- Native process-tree containment is used so timeouts terminate descendants.
- A process-local canonical-root lock rejects concurrent Graphite activation attempts. It does not claim cross-process exclusion.
- Manifest and lockfile paths are required to remain contained regular files. Their identities and hashes are captured before installation and rechecked afterward.

Package managers may modify `package.json`, the selected lockfile, and their normal dependency store. Graphite reports which allowlisted manifest or lockfile paths changed. It does not blindly restore files after failure because another process or editor may have changed them concurrently. On failure it returns a non-zero outcome with recovery guidance and leaves reviewable package-manager changes visible.

## Onboarding integration and exit behavior

Normal `init` or `bootstrap` writes happen first and retain their existing idempotency and output contracts. TypeScript activation runs as a post-step:

- `installed`, `already_available`, `not_applicable`, `declined`, and `guidance_only` preserve the normal successful onboarding exit code.
- `validation_failed`, `installation_failed`, and `verification_failed` preserve completed onboarding files but make the overall command exit non-zero.

Human-readable output names the fixed manager identifier, outcome, and relative changed manifest/lockfile paths. If the command already supports JSON, activation is represented as a bounded nested object with stable field names. JSON mode never prompts and therefore can only produce non-mutating outcomes such as `already_available`, `not_applicable`, or `guidance_only`.

## Manual guidance

Guidance preserves this mandatory order:

1. Set `GRAPHITE_PACKAGE_VALIDATOR` to the trusted environment-specific absolute validator path.
2. Fail closed if it is unset, relative, missing, or not a regular file.
3. Run the validator for exact package name `typescript`.
4. Only after successful validation, use the project's existing package manager to add `typescript` locally as a development dependency with lifecycle scripts disabled according to local policy.
5. Rerun `graphite doctor` or onboarding to confirm detection.

No global install command is emitted.

## Testing strategy

All automated tests use fake executables, fake validators, bounded temporary repositories, and deterministic process results. No test contacts a live registry or performs a real package installation.

Coverage includes:

- bounded TypeScript evidence detection and containment;
- already-resolvable TypeScript and no-op behavior;
- npm, pnpm, Yarn, and Bun lockfile identification;
- missing, multiple, conflicting, nested-only, malformed, symlinked, and reparse-point manifests/lockfiles;
- agreement with `packageManager` metadata;
- interactive accept/decline and default-No behavior;
- non-interactive stdin/stdout, EOF, `--yes`, JSON, CI, daemon, MCP, doctor, build, and report non-install guarantees;
- validator path containment, file type, exact argument, ordering, timeout, and rejection;
- manager executable provenance and version support;
- fixed development-dependency argv and lifecycle-script suppression;
- trusted-registry enforcement and credential/environment stripping;
- bounded output, sanitized errors, deadlines, descendant cleanup, and canonical-root locking;
- manifest/lockfile identity and pre/post hash evidence;
- install success followed by mandatory local re-detection;
- all typed results and exit-code mappings;
- preservation of completed onboarding files after activation failure;
- documentation contracts that prohibit global or silent installation.

## Acceptance criteria

1. An eligible interactive onboarding run prompts exactly once and defaults to No.
2. Consent is required before validator or network activity.
3. Validation of exact package `typescript` always precedes installation.
4. A supported, unambiguous existing manager installs only local development dependency `typescript` with lifecycle scripts disabled.
5. Non-interactive and non-onboarding paths never install or prompt.
6. Ambiguous or unsafe manager, executable, validator, registry, or filesystem state fails closed without mutation.
7. Explicit validation, installation, or verification failure returns non-zero while preserving completed onboarding files.
8. Successful activation is not reported until project-local TypeScript is re-detected.
9. Outputs and logs contain no credentials, raw process/registry output, absolute host paths, or repository source.
10. Existing onboarding behavior remains compatible for projects without eligible TypeScript evidence.

## Operational risks and trade-offs

- Package-manager behavior varies by major version. The supported adapter/version matrix must remain explicit, tested, and conservative.
- Automatic installation intentionally does not support private mirrors because safe unattended credential and registry inheritance would expand the trust boundary. Those projects receive manual guidance.
- Installation can leave reviewable manifest, lockfile, or dependency-store changes after package-manager failure. Avoiding automatic rollback prevents Graphite from overwriting concurrent user edits.
- The activation lock is process-local; external package-manager or editor concurrency remains possible and is detected through path identity and hash changes where practical.
- Compiler availability can change graph resolution outcomes. Project-local lockfiles provide the required reproducibility boundary.
