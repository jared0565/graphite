# Configuration reference

Graphite reads `GRAPHITE_*` environment variables (matched case-insensitively)
and the global CLI options that precede a subcommand; a CLI option wins over
the environment. The canonical graph commands (`scan`, `build`, `report`,
`check`, `validate`, `query`, `search`, `context`, `impact`, `watch`, `daemon`)
force `llm_mode` to `none` and ignore every `GRAPHITE_LLM*` variable.

`tests/test_configuration_reference.py` fails when a `Config` field, an
environment key the loader reads, or a `GRAPHITE_ROUTE_*` setting is missing
from this page, so what is listed here is what the code reads.

## `Config` fields

Numbers are parsed leniently: an integer variable that is not all digits, or a
float that does not parse, falls back to the default rather than failing.
Booleans accept `1`, `true`, `yes`, `on` (any case); anything else is false.

| Field | Environment | CLI | Default | Meaning |
|---|---|---|---|---|
| `output_dir` | `GRAPHITE_OUTPUT_DIR` | `--output-dir` | `graph-out` | Where `graph.json`, `graph.html` and `GRAPH_REPORT.md` are written, relative to the repository root. |
| `cache_dir` | `GRAPHITE_CACHE_DIR` | `--cache-dir` | `.cache/graphite` | Extraction cache root. Partitions are keyed on `cache_version` and the engine fingerprint, so an engine change re-extracts by itself. |
| `cache_version` | `GRAPHITE_CACHE_VERSION` | — | `v11` | Coarse manual cache override. Bumping it forces re-extraction; the engine fingerprint already does that for engine changes. |
| `workers` | `GRAPHITE_WORKERS` | `--workers` | `4` | Extraction parallelism. |
| `max_file_size` | `GRAPHITE_MAX_FILE_SIZE` | — | `1000000` | Files larger than this many bytes are skipped. |
| `max_files` | `GRAPHITE_MAX_FILES` | — | unset | Optional cap on the number of files scanned; unset means no cap. |
| `include_dotfiles` | `GRAPHITE_INCLUDE_DOTFILES` | — | `false` | Scan dot-directories and dot-files too. |
| `typescript_resolver` | `GRAPHITE_TYPESCRIPT_RESOLVER` | `--typescript-resolver` | `auto` | `auto`, `compiler`, `heuristic` or `disabled`: how TypeScript imports and symbol references are resolved. `compiler` needs a project-local TypeScript (see `graphite doctor`). |
| `typescript_resolver_timeout_seconds` | `GRAPHITE_TYPESCRIPT_RESOLVER_TIMEOUT` | `--typescript-resolver-timeout` | `10.0` | Budget for one compiler-resolver invocation, in seconds. |
| `typescript_symbol_references` | `GRAPHITE_TYPESCRIPT_SYMBOL_REFERENCES` | `--no-typescript-symbol-references` | `true` | Emit `references` / `type_references` edges from the TypeScript resolver. |
| `llm_mode` | `GRAPHITE_LLM` | `--llm` | `none` | `none`, `auto`, `local` or `cloud`. Only `overlay build` honours a value other than `none`. |
| `llm_provider` | `GRAPHITE_LLM_PROVIDER` | `--llm-provider` | `ollama` | Provider name for overlay enrichment. |
| `llm_model` | `GRAPHITE_LLM_MODEL` | `--llm-model` | unset | Model identifier for the provider. |
| `llm_base_url` | `GRAPHITE_LLM_BASE_URL` | `--llm-base-url` | unset | Provider endpoint. Ollama is restricted to loopback HTTP; other providers require HTTPS. |
| `llm_api_key` | `GRAPHITE_LLM_API_KEY` | `--llm-api-key` | unset | Provider credential. Prefer a session-scoped secret environment over argv or any repository file. |
| `llm_timeout_seconds` | `GRAPHITE_LLM_TIMEOUT` | `--llm-timeout` | `30.0` | Per-request provider timeout, in seconds. |
| `llm_max_input_chars` | `GRAPHITE_LLM_MAX_INPUT_CHARS` | `--llm-max-input-chars` | `12000` | Prompt input is truncated to this many characters. |
| `llm_max_output_tokens` | `GRAPHITE_LLM_MAX_OUTPUT_TOKENS` | `--llm-max-output-tokens` | `512` | Clamped to 1–4096. |
| `provider_observer_enabled_providers` | `GRAPHITE_PROVIDER_OBSERVER_ENABLED_PROVIDERS` | — | empty | Comma-separated lifecycle provider ids the daemon's non-inference observer may probe. |
| `provider_observer_interval_seconds` | `GRAPHITE_PROVIDER_OBSERVER_INTERVAL` | — | `300.0` | Seconds between observer cycles. |
| `provider_observer_timeout_seconds` | `GRAPHITE_PROVIDER_OBSERVER_TIMEOUT` | — | `15.0` | Per-observation timeout, in seconds. |
| `provider_observer_max_per_cycle` | `GRAPHITE_PROVIDER_OBSERVER_MAX_PER_CYCLE` | — | `4` | Observations per cycle. |
| `provider_observer_backoff_cap_seconds` | `GRAPHITE_PROVIDER_OBSERVER_BACKOFF_CAP` | — | `3600.0` | Ceiling for the observer's failure backoff. |
| `provider_observer_jitter_ratio` | `GRAPHITE_PROVIDER_OBSERVER_JITTER_RATIO` | — | `0.1` | Jitter applied to observer scheduling. |
| `seed` | `GRAPHITE_SEED` | — | `42` | Seed for the community detection step, so `graph.json` is reproducible. |
| `verbose` | `GRAPHITE_VERBOSE` | `-v` | `false` | Verbose progress output. |

## Other environment variables

These are read outside `Config`, each by the module named.

| Variable | Read by | Meaning |
|---|---|---|
| `GRAPHITE_PROJECTS_ROOT` | `config.default_projects_root`, `bootstrap` | Base folder the daemon supervises and `init`/`bootstrap` check visibility against. Unset means the current directory; there is deliberately no other fallback. |
| `GRAPHITE_STATE_DIR` | `activation` | User-scoped state directory for activation records (which repositories are open in a coding agent). Unset means the platform default. |
| `GRAPHITE_DAEMON_CHILD` | `activation`, `buildlock` | Set by the daemon in the environment of the builds it spawns. A child does not register activation (otherwise activation would never expire) and reports a held build lock as a refusal (exit 75) instead of a skip. Never set it by hand. |
| `GRAPHITE_BUILD_LOCK_REPORT_REFUSAL` | `buildlock` | Set by the daemon for its child builds: a build that cannot take the lock exits 75 (`EX_TEMPFAIL`) instead of 0, so the daemon can tell "another builder owns the repo" from a failed build. |
| `GRAPHITE_PACKAGE_VALIDATOR` | `typescript_activation`, `cli` | Absolute path to the trusted package validator that must approve an optional TypeScript activation install. Unset, relative or resolving inside the repository fails closed. |
| `GRAPHITE_DEBUG` | `cli` | When set, an unexpected exception prints its traceback instead of the redacted one-line error. |

The daemon strips every variable starting with `GRAPHITE_LLM`, `GRAPHITE_PROVIDER_`
or `GRAPHITE_ROUTE_` (and the provider vendors' own prefixes) from the
environment of the builds it spawns, so a supervised build can never carry
provider authority into a graph artifact.

## Routing settings (`GRAPHITE_ROUTE_*`)

Read by `routing.settings.RoutingSettings`; an out-of-range or malformed value
raises `<name>_invalid` rather than falling back. The variable is
`GRAPHITE_ROUTE_` followed by the upper-cased field name.

| Variable | Default | Meaning |
|---|---|---|
| `GRAPHITE_ROUTE_MAX_CONTEXT_BYTES` | `262144` | Bytes of repository context handed to a routed model. |
| `GRAPHITE_ROUTE_MAX_CONTEXT_FILES` | `32` | Files of context handed to a routed model. |
| `GRAPHITE_ROUTE_MAX_INPUT_TOKENS` | `32768` | Input token ceiling per request. |
| `GRAPHITE_ROUTE_MAX_OUTPUT_TOKENS` | `4096` | Output token ceiling per request. |
| `GRAPHITE_ROUTE_REQUEST_TIMEOUT_SECONDS` | `180.0` | Per-request timeout. |
| `GRAPHITE_ROUTE_MAX_CONCURRENCY` | `1` | Concurrent routed requests. |
| `GRAPHITE_ROUTE_REPOSITORY_QUOTA_TOKENS` | `2000000` | Token quota per repository. |
| `GRAPHITE_ROUTE_MACHINE_QUOTA_TOKENS` | `10000000` | Token quota per machine. |
| `GRAPHITE_ROUTE_SHADOW_RATE_PERCENT` | `0` | Share of requests also sent to a shadow model. |
| `GRAPHITE_ROUTE_SHADOW_QUOTA_TOKENS` | `200000` | Token quota for shadow requests. |
| `GRAPHITE_ROUTE_APPROVAL_TTL_SECONDS` | `300` | How long a routing approval stays valid. |
| `GRAPHITE_ROUTE_RETENTION_DAYS` | `90` | Retention of routing records. |
| `GRAPHITE_ROUTE_AGGREGATE_OPT_IN` | `false` | Opt in to aggregate routing telemetry. |
| `GRAPHITE_ROUTE_MAX_CHANGED_FILES` | `64` | Files a routed edit may change. |
| `GRAPHITE_ROUTE_MAX_CHANGED_BYTES` | `1048576` | Bytes a routed edit may change. |
