# Changelog

All notable changes to Ship1000x are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added — production decomposed by nature of work (code / docs / config / data)

- The line classifier now sub-classifies the productive (`real`) lines by work
  nature via a new `work_class` taxonomy (`core.line_classifier`): **code**
  (ts/py/go/sql/…), **docs** (md/rst/txt/…), **config** (yaml/toml/ini/…), and
  **data** (json/csv/srt/svg/html/… — the fallback). Extension lists live in
  `config/line_classification.yaml` and are extendable via the local override.
- `git` events store per-class line counts (`lines_{code,docs,config,data}_*`),
  aggregated by the engine and surfaced as `production_breakdown`.
- **The leverage factor (`multiplier`) now compares CODE to the code benchmark
  only.** Docs/config/data are real work but are reported as *volume*, never
  folded into a "lines of code vs senior" factor. Pre-reclassify events fall
  back to the `real` basis with an explicit `lines_basis=real_pending_reclassify`
  flag. Run `ship1000x reclassify` to populate the breakdown on history.
- Surfaced in the CLI `multiplier`/`insights`, the Markdown report, and the
  insights push payload. New `raw_meta` line keys added to the privacy
  whitelist (numeric counters only; no paths or content).
- **Dashboard — "Production by nature" section** (Overview tab) with four
  widgets backed by a new `/api/work-mix` endpoint: global mix (code/docs/
  config/data), mix over time (stacked daily chart), per-project mix, and the
  docs/code ratio. Volume only; the endpoint returns aggregates and project
  names, never paths or content.

### Changed — trust/reliability corrections (scores may move)

- **`confidence_flag` now reflects measurement quality, not project
  attribution** (`claude_code`, `codex`, `git`). Previously these collectors
  derived the per-event `confidence_flag` (which the global Trust Score
  averages) from `project_conf` — so the headline score reflected how sure we
  were *which project* an event belonged to, not how well its tokens/cost were
  measured. The flag is now derived from `usage.quality.{tokens,cost}` via
  `ship1000x.core.usage.confidence_flag_from_usage` (Git uses its factual
  `numstat` line measurement). Project-attribution confidence stays available
  separately in the event's `project_conf`. Run `ship1000x reclassify` to
  re-derive historical events where the original local source still exists.
  Token-less sources (`cursor`, `cline`, `codex_macapp`, `codex_desktop`,
  `claude_statusline`, `openclaw`) are intentionally not migrated yet — their
  correct mapping is a separate design decision.
- **Exported leverage multiplier now uses real lines + carries a confidence
  band.** `compute_multiplier` switched from raw `lines_added` to
  `lines_real_added` (consistent with `highlights` and engine ratios), and its
  output now includes a `confidence` block (`lines_basis`, `benchmark_source`,
  caveats) surfaced in the Markdown report, the CLI pitch command, and the
  insights push payload.
- **Pricing staleness flows into cost confidence.** A factual native-token cost
  priced against a stale local rate card (`pricing_freshness().stale`, >60d) is
  downgraded to `defensible`.
- **Benchmark provenance honesty.** `lines_per_hour_no_ai` is documented as an
  internal, unsourced assumption (not an industry standard), to be cited as
  such or replaced via `config/benchmarks.yaml`.

### Added

- **CLI command-surface guard** — user-facing CLI messages now point to
  `ship1000x ...` commands instead of the legacy `tracker ...` binary, with a
  package-wide regression test preventing public operator guidance drift.
- **Public asset command-surface guard** — generated documentation assets now
  use current `ship1000x ...` examples and are covered by the same docs scan.
- **Dashboard cost API alias** — `/api/highlights` now exposes
  `cost_api_equivalent` as the explicit dashboard cost field while preserving
  `cost_total` as a backward-compatible alias.
- **Project cost semantics** — `/api/projects` now exposes
  `total_api_equivalent_cost`, and project dashboard labels use API-equivalent
  cost wording instead of a generic "Cost" column.
- **README trust-score semantics** — the public Highlights example now reflects
  raw weighted Trust Score semantics and keeps robustness checks separate from
  the score.
- **Highlights cost semantics** — `ship1000x highlights` and its public asset
  now label dashboard spend as API-equivalent cost instead of generic agentic
  cost.
- **Trust Score docs semantics** — README, collector docs, and Trust Score docs
  now describe the score as a raw weighted global score, with cadence/unified
  checks reported separately as robustness checks.
- **Source-audit unknown billing basis** — `source-audit` now reports
  `unknown_billing_basis_usd` and event counts for legacy/incomplete rows so
  API-equivalent and billed-estimated cost are not silently conflated.
- **Public claim cost evidence** — `public-check` now includes unknown billing
  basis totals in the blocked invoice-grade cost truth claim evidence.
- **Source audit cost summary** — `source-audit --json` now exposes aggregate
  `summary.cost_truth` totals for API-equivalent, billed-estimated,
  subscription-absorbed, and unknown billing-basis cost semantics.
- **Public-check claim evidence** — human `public-check` output now prints
  claim evidence before caveats, making blocked public claims explainable
  without inspecting JSON.
- **Audit-cost cost semantics** — `ship1000x audit-cost` now labels stored and
  recomputed totals as API-equivalent and explicitly avoids invoice-grade
  billed-cost wording.
- **CLI cost labels** — operator-facing CLI tables and Markdown exports now
  label summed `cost_estimated` values as API-equivalent instead of generic
  cost.
- **Provider-policy claim boundary** — `public-check` now blocks
  provider-policy/auth-route compliance claims unless an external policy
  snapshot process exists, while still surfacing local auth-mode evidence.
- **Historical repair claim boundaries** — `history-audit`, `reclassify --dry-run`,
  and `public-check` now expose repair scope, claim boundary, and unrecoverable
  truth fields so partial sources cannot be mistaken for full historical repair.
- **Public proof sequence** — `public-check` now emits the safe rerun sequence,
  aggregate evidence, and quote policy so demos separate allowed, conditional,
  and blocked claims.
- **Public proof docs alignment** — README and Quickstart now include
  `observation-audit` and `public_proof_sequence` in the public-proof path.
- **Public proof command alignment** — `public_proof_sequence` now emits the
  same `source-audit --strict-public` command shown in the public docs.
- **Public proof caveat status** — `public_proof_sequence` now returns
  `caveat` instead of `pass` when a gate is usable but coverage gaps,
  conditional history, or blocked public claims remain visible.
- **Public-check caveat header** — the human `public-check` header now prints
  `CAVEAT` for usable-but-caveated proof instead of a misleading `PASS`.
- **Public-check proof status JSON** — `public-check --json` now exposes
  top-level `proof_status` and `presentation_status`, keeping `passed` as the
  backward-compatible gate/exit-code boolean.
- **Public proof docs status alignment** — README and Quickstart now describe
  `PASS` / `CAVEAT` / `NO GO` and tell machine consumers to prefer
  `proof_status` / `presentation_status` over the legacy `passed` boolean.
- **Reconcile token breakdown** — `ship1000x reconcile` Markdown reports now
  include safe aggregate input/output/cache/reasoning token counters when local
  source metadata exposes them, making cache-driven cost gaps easier to triage.
- **Dashboard cost truth status** — `/api/highlights.cost_truth` now includes
  `presentation_status` and `presentation_label`, and the overview labels
  API-equivalent cost as not invoice truth.
- **Project dashboard cost truth** — `/api/projects` now exposes per-project
  `cost_truth`, billed-estimated, subscription-absorbed, and unknown-basis
  cost fields so project views do not infer paid cost from API-equivalent
  aliases.
- **Dashboard API schema contract** — `/api/highlights` and `/api/projects`
  now expose stable dashboard schema versions, documented in
  `docs/DASHBOARD_API.md` for downstream importer consumption.
- **Remaining dashboard API schema contracts** — `/api/trend`, `/api/trust`,
  and `/api/source-quality` are now documented with explicit schema versions
  so downstream importers do not have to infer their payload shape.
- **Provider-policy snapshot coverage** — `source-audit` now exposes valid,
  invalid, missing, and percentage coverage counters for local policy-snapshot
  evidence, making provider-policy gaps visible without certifying compliance.
- **Public-check missing policy evidence** — provider-policy claim evidence now
  prints missing policy-snapshot counts explicitly instead of requiring
  operators to infer them from `valid/total`.
- **Strict history blockers** — `history-audit --json` now marks rows that
  would block `public-check --strict-history`, and public-check consumes those
  shared failure reasons instead of recomputing separate history wording.
- **CI Node 24 readiness** — the GitHub Actions test matrix now uses
  Node 24-compatible `actions/checkout@v6` and `actions/setup-python@v6`
  ahead of GitHub's June 2026 default switch, and avoids fragile pip cache
  warnings in the public CI run by disabling pip's job-level cache.
- **Public examples cost labels** — README, highlights assets, Trust Score
  examples, reclassify output, and Markdown exports now avoid generic cost
  labels for API-equivalent values.
- **Reclassify recoverability warnings** — `reclassify --dry-run` now surfaces
  per-source historical recoverability and warning summaries before any real
  offset reset, deletion, or rebuild can run.
- **Version/status consistency guard** — README beta wording and PyPI
  classifier now align with package version `1.1.0`, with tests preventing
  Alpha/Beta or stale install-version drift.
- **Public docs command-surface guard** — main public docs now use
  `ship1000x ...` commands consistently, with a regression test blocking
  legacy `tracker ...` command examples.
- **Privacy docs consistency checks** — `docs/PRIVACY.md` now uses the current
  XDG database path, `ship1000x push-insights` command, and separates billing
  API collectors from local manual web exports.
- **Reclassify operator wording guard** — CLI help/tests now describe
  `reclassify` as historical event repair across enabled sources, not a
  git-only legacy command.
- **Reclassify source inventory alignment** — `reclassify` and
  `reclassify --dry-run` now use the canonical production collector inventory,
  so recent sources such as Roo/Kilo Code, Claude statusline, agent runtime,
  the usage proxy, shell, and macOS system signals are included when enabled.
- **Public docs consistency checks** — tests now keep the README status badge,
  quickstart CLI commands, and architecture cost-truth wording aligned with
  the package version and current `ship1000x` command surface.
- **History repair risk sources in public claims** — `public_claim_readiness`
  now includes a safe `history_repair_risks` list so conditional/blocked
  historical repair claims name the affected sources without exposing raw
  metadata.
- **Canonical source inventory** — active collector modules, fixture-only
  parsers, ingest source names, and production event source ids now live in
  production code so source-quality/history tests cannot drift from hidden
  test-local lists.
- **Historical repair public-claim readiness** — `public-check` now feeds
  `history-audit` into `public_claim_readiness`, making historical
  repair/backfill claims explicitly `allowed`, `conditional`, or `blocked`.
- **History recoverability profiles for production sources** — `history-audit`
  now has explicit recoverability semantics for all SHIP production event
  sources, including `trace`, `agent_runtime`, `claude_statusline`,
  `roo_kilo_code`, `openclaw`, `shell`, and `mac_system`.
- **Profiles for SHIP-emitted non-usage sources** — `source-audit` and
  `history-audit` now recognize `web_export` and `git_secret_alert`, so SHIP's
  own aggregate export/secret-scan rows do not show up as `unknown-source`.
  Public docs now mark web exports as aggregate metadata, not token/cost truth.
- **Ingest inventory regression gate** — CLI tests now assert that
  `INGEST_SOURCE_NAMES` covers every production collector module, honors source
  aliases like `git_multi -> git`, and excludes fixture-only parsers.
- **Public doc claim consistency sweep** — coverage, collectors, architecture,
  and observation-audit docs now match the shipped ingest/source-readiness
  behavior for Roo/Kilo Code, usage-proxy/statusline/runtime drop imports, and the
  fixture-only provider boundary.
- **Ingest source validation** — `ship1000x ingest --source <name>` now fails
  with exit code 2 and a safe list of known sources when the source name is
  unknown, instead of succeeding with a silent no-op.
- **Ingest routes for shipped collectors** — `ship1000x ingest --source` now
  routes `roo_kilo_code`, `claude_statusline`, `agent_runtime`, and `trace`.
  Roo/Kilo Code is wired into normal ingest; the live/drop importers remain
  opt-in for `--source all` unless enabled in `privacy.yaml`, but explicit
  source runs now work as documented.
- **Source readiness stages in public gates** — `source-audit --json` now
  exposes per-source `collector_stage`, observed `collector_stage_counts`, and
  `public_untrusted_stage_sources`. `source-audit --strict-public` and
  `public-check` fail when an observed row comes from a fixture-only,
  registry-only, or collector-module-only source, preventing fixture-shaped
  rows from being presented as real SHIP coverage.
- **Public proof gate docs** — README now documents the recommended public
  proof sequence (`source-audit --strict-public`, `history-audit`,
  `reclassify --dry-run`, `public-check --strict-history`) and lists
  `public-check --strict-history` in the command matrix so historical
  recoverability is not hidden behind the default pre-demo gate.
- **LiteLLM provider mapping extended (Wave 7+ batch 7h)** — `_PROVIDER_MAP`
  dans `ship1000x/core/pricing_litellm.py` passe de 5 → 24 entrées,
  couvrant les principaux frontier vendors (Google/Vertex AI, xAI,
  Mistral, Cohere, DeepSeek) et les principaux aggregators (OpenRouter,
  Fireworks, Perplexity, DeepInfra, Together, Groq, Replicate, Novita,
  Dashscope). Anthropic-via-Vertex (`vertex_ai-anthropic_models`)
  attribué à `anthropic` (suit le précédent existant `bedrock → anthropic` :
  model author > hosting layer). Vertex-hébergé non-Google attribué à
  l'auteur du modèle (`vertex_ai-mistral_models → mistral`, etc.).
  `TestVendoredSnapshot` étendu avec 6 modèles flagship (gemini-2.0-flash,
  xai/grok-4, command-r-plus, deepseek/deepseek-chat, perplexity/sonar,
  groq/llama-3.3-70b-versatile) — pinned ids, pas `latest`, pour qu'une
  drift de sync soit visible. 4 nouveaux tests unitaires (Google mapping,
  Vertex-Anthropic precedence, 7 vendors smoke-coverage, regression
  unmapped-fallback). 27 tests `test_pricing_litellm.py` (was 17),
  492 tests suite totale.
- **`ship1000x.collectors.roo_kilo_code`** promoted from fixture-only
  parser to active collector — first Wave 7+ batch 7a delivery. Now
  ingests live tasks from Roo Code (legacy, shutdown 2026-04-21) and
  Kilo Code (successor since 2026-04-02) extensions across Cursor +
  VS Code + VS Code Insiders `globalStorage` paths (six candidate
  paths probed, missing ones skipped silently). Shares on-disk parsing
  with the Cline collector since Roo and Kilo are Cline forks with the
  same task schema. Events emitted under `source="roo_kilo_code"` with
  `variant` (`"roo-code"` or `"kilo-code"`) embedded in `raw_meta` so
  coverage drilldowns can distinguish the two extensions without
  inventing a separate source identity. Ingestion offset key embeds
  the variant to prevent collision between identical task ids across
  the two extensions.

### Changed

- **Insights cost truth** — `compute_overview`, `compute_multiplier`, and
  cost-spike signals now aggregate operator-facing API-equivalent cost through
  the shared cost-truth helper instead of summing raw `cost_estimated`.
- **Highlights/pulse cost truth** — the showcase `highlights` view and daily
  `pulse` line now use shared API-equivalent cost truth for displayed spend
  instead of raw stored event cost.
- **Reclassify dry-run cost truth** — `reclassify --dry-run` now reports
  source-window spend on the same API-equivalent basis as the operator table
  label, with a backward-compatible JSON alias for older consumers.
- **Shared cost row helper** — CLI, Markdown report and dashboard project
  surfaces now reuse the same safe row-level API-equivalent cost helper to
  reduce future semantic drift.
- **Provider policy snapshot evidence** — `source-audit` and `public-check`
  now report aggregate provider-policy snapshot coverage without certifying
  provider compliance or exposing raw policy metadata.
- **Usage policy snapshot helper** — `build_usage_metadata` can now attach a
  minimal owner-reviewed `provider_policy_snapshot` for future collectors,
  keeping only short categorical fields.
- `tests/test_all_collectors_implement_source_collector.py` —
  `roo_kilo_code` moved from `FIXTURE_ONLY_PARSERS` to
  `ACTIVE_COLLECTORS`, per the documented Wave 7+ promotion workflow.
  The fixture-only contract for the remaining six parsers (`aider`,
  `continue_dev`, `copilot_agents`, `cursor_agent`, `gemini_cli`,
  `opencode`) is unchanged.

## [1.1.0] — 2026-05-24 — Wave 2.5 + Wave 3 + Wave 4 bridge

Wave 2.5 + Wave 3 (live runtime) + Wave 4 SHIP-side bridge.

### Added

- **`ship1000x watch`** — foreground polling daemon that snapshots
  AI-relevant processes and listening TCP ports every 30s, mirrors
  them into `~/.ship1000x/drop/watch/<YYYY-MM-DD>.jsonl`. Pattern
  inspired by ABTop (mode 1, zero code copied). Strict AI process
  whitelist (16 patterns); ports are best-effort (macOS `psutil.
  net_connections` returns empty without root, daemon falls back
  cleanly). Optional `[watch]` extra installs `psutil>=5.9`. (#50)
- **`ship1000x.collectors.agent_runtime`** — reads the watch drop
  files, aggregates ticks per `(process_name, minute)`, emits one
  `agent_runtime_tick` event per bucket with `process_tick_count`,
  `max_concurrent_processes`, `open_ports_count` (union across
  ticks), `mcp_servers_detected`. Idempotent via stable event id. (#50)
- **`ship1000x.runtime.statusline_writer`** + **`collectors.
  claude_statusline`** — two-stage Claude Code statusline live
  capture. Writer is invoked by Claude Code on every UI tick,
  appends a whitelisted 7-field record to
  `~/.ship1000x/drop/statusline/`. Collector aggregates per-minute
  into `session_live_tick` events with dominant model, max
  context-usage percentage, and compact-boundary flag. Pattern
  inspired by Claude HUD (mode 1). (#47)
- **`ship1000x.exporters.sinks`** — `S3RollupSink`, `InsightsCloudSink`,
  `NoOpSink` — three classes that wrap the legacy `s3_push` and
  `insights_push` functions behind the Wave 2 `ExportSink` Protocol.
  Pure addition; the legacy functions remain exported and every
  existing caller continues to work. The `markdown_report` generator
  is documented as deliberately not wrapped (report generator, not
  sink). Closes Wave 2 / Day 8. (#48)
- **`ship1000x.collectors.trace`** — bridge collector for an optional
  local opt-in HTTPS usage proxy. Reads `~/.ship1000x/drop/trace/<date>.jsonl`
  (9-key redacted records from the proxy's pipeline) and emits `api_call`
  events. Confidence mapping: `factual` → high, `estimated` → medium.
  50 MB read cap per file as defence against a misbehaving proxy. (#52)
- **`SourceEntry.drop_subpath`** — new optional field on the
  coverage registry pointing at a daemon's drop directory. When set
  on a `SUPPORTED` source with no events in the window, the
  Markdown + JSON reports surface a "Daemon health: N drop file(s)
  present, newest tick @ ..., run `ship1000x ingest --source <id>`
  to materialise events" hint. Closes the Wave 3 / Day 8 UX gap that
  made "no daemon" indistinguishable from "daemon ran but ingest is
  behind". (#51)
- **`ALLOWED_META_KEYS`** entries:
  - `statusline_tick_count`, `context_used_percent_max`,
    `compact_triggered` (Wave 3 / D1-2 statusline). (#47)
  - `process_name`, `process_tick_count`,
    `max_concurrent_processes`, `open_ports_count`,
    `mcp_servers_detected` (Wave 3 / D3-5 daemon). (#50)
  - `provider`, `endpoint`, `usage_quality`,
    `cache_read_input_tokens`, `cache_creation_input_tokens` (Wave 4 /
    Phase 6 trace bridge). (#52)
- **`CATEGORY_RUNTIME`** now has two active sources (`agent_runtime`
  + `trace`), both surfaced via `ship1000x coverage --category runtime`.

### Changed

- `SOURCE_REGISTRY` — the historical `claude_code_statusline`
  NOT_SUPPORTED entry (pointing at audit ref P1.2 with reason
  "needs statusline adapter") is removed: the adapter shipped as
  `claude_statusline`. A short comment in the registry points to
  the replacement so an old audit reader knows where to look. (#51)
- The `coverage` Markdown + JSON renderers gain a `daemon_health`
  field surface (Markdown) / key (JSON) for runtime sources with
  drop files. Backward-compatible — existing consumers that don't
  read the new key are unaffected. (#51)

### Tests + lint

- pytest tests/ → **481 passed, 1 skipped** (was 388 at the start
  of this Unreleased window; +93 new tests across statusline,
  daemon, agent_runtime collector, coverage daemon-health, sinks,
  and trace bridge).
- ruff check . → clean on every commit on `main`.
- CI matrix (ubuntu + macos × Python 3.10 / 3.11 / 3.12) green on
  every PR (#47 → #52).

### Backward compatibility

- All v1.0.0 public symbols continue to work unchanged.
- Both new collectors (`agent_runtime`, `trace`) are passive —
  they only emit events when their respective drop directories
  exist and contain new files. A user who never installs the
  `[watch]` extra or the usage proxy sees zero behavioural
  change.
- The `agent_runtime` source had been a NOT_SUPPORTED gap row in
  v1.0.0; it is now promoted to SUPPORTED. The
  `openai_api_trace` gap row remains as a documented future
  alternative path (Responses API request-level, distinct from
  the proxy capture).

## [1.0.0] — 2026-05-23 — Wave 2 Ports & Adapters Spine

First major release. Wave 2 of the SHIP1000X strategic roadmap.
Extracts the five Hexagonal / Ports & Adapters that unlock the rest
of the roadmap (Wave 3 live runtime, Wave 4 usage proxy, Wave 5
reconciliation, Wave 6 OpenTelemetry/ActivityWatch/Presidio, Wave 7+
provider expansion).

### Why v1.0.0

- The 5 ports lock the public contract for the next 18 months
  minimum. Consumers (downstream OSS adapters, a premium fork,
  future Premium SaaS adapters) can build against a stable
  surface.
- Backward compatibility preserved end-to-end: all Wave 1 callers
  continue to work unchanged (`Storage = SQLiteStorage` alias,
  `sanitize_event()` still exported, default LiteLLM resolver
  preserved as Wave 1 behaviour).
- Test suite grew from 286 (start of session) → 396 (end of Wave 2).
  Zero regression.

### Added

- `ship1000x/ports/` package — five `runtime_checkable` Protocols:
  `PricingResolver`, `PIIScrubber`, `EventStorage`, `SourceCollector`,
  `ExportSink`. Locked until 2027 minimum. (#40)
- `ship1000x.core.pricing.resolve_model_pricing_hybrid()` accepts an
  optional `resolver: PricingResolver | None` argument for explicit
  port injection. Default behaviour (None) preserves Wave 1 LiteLLM
  semantics. (#41)
- `ship1000x.core.scrubber.RegexScrubber` — class adapter wrapping
  `sanitize_event()` behind the `PIIScrubber` Protocol. Module-level
  `default_scrubber()` singleton. (#42)
- `Storage` now accepts an optional `scrubber: object | None` argument.
  Default behaviour identical to Wave 1; injecting a stricter or
  domain-specific scrubber replaces the privacy layer without touching
  call sites. (#42)
- `docs/ARCHITECTURE.md` — canonical ports & adapters reference
  replacing the historical v0.1 partial doc. (#45)
- `tests/test_ports_structural.py` — 11 conformance tests via
  `isinstance(adapter, Port)`. (#40)
- `tests/test_pricing_resolver_port_injection.py` — 5 tests covering
  injection semantics + Wave 1 backward compatibility. (#41)
- `tests/test_scrubber_port_injection.py` — 6 tests covering scrubber
  injection semantics + per-instance isolation. (#42)
- `tests/test_storage_port_naming.py` — 6 tests verifying the
  `Storage = SQLiteStorage` alias preserves `isinstance()` semantics
  for the entire ecosystem. (#43)
- `tests/test_all_collectors_implement_source_collector.py` — 38
  parametrised tests across all 22 collector modules: 15 active
  collectors must expose a runtime `collect()` callable; 7
  fixture-only parsers must NOT (Wave 7+ promotion gate). (#44)

### Changed

- `ship1000x.core.storage.Storage` is now an alias for the renamed
  `SQLiteStorage` class. Identity preserved (assignment, not
  subclass), `isinstance(s, Storage)` continues to return True. (#43)
- `SQLiteStorage.upsert_event` now calls `self._get_scrubber().scrub(event)`
  instead of `sanitize_event(event)` directly. Default behaviour
  identical (the default scrubber is `RegexScrubber` which delegates
  to `sanitize_event`). (#42)

### Deferred to Wave 2.5 (post-v1.0.0)

- Refactor of the three historical exporters (`s3_push.py`,
  `insights_push.py`, `markdown_report.py`) behind the `ExportSink`
  port. Their current interfaces are heterogeneous (push of
  pre-aggregated rollups vs `Iterable[dict[str, Any]]`); adapting
  cleanly requires interface design that mid-session refactor cannot
  resolve. The port itself is defined and ready for new adapters
  (Premium `CloudSyncSink`, `SlackAlertSink`, OTel `OTelGenAISink`).

### Backward compatibility

- All public symbols introduced in v0.6.0 and earlier remain
  available. The `Storage` import, `sanitize_event` function,
  `resolve_model_pricing_hybrid(provider, model)` two-argument call
  shape, and all collector module-level `collect()` functions
  continue to work without modification.
- The separate premium fork was not migrated in Wave 2 — that's
  Wave 5. The backward-compat surface gives it 4 to 6 weeks of stable
  v1.0.x OSS in production before its own migration starts.

## [0.6.0] — 2026-05-23 — Wave 1 Trust Foundation

First wave of the SHIP1000X strategic roadmap. Closes the trust
gaps before any new feature ships — pricing maintenance externalised,
git ingestion no longer blind to leaked secrets, central privacy
guard regressed, share_config defaults locked.

### Added

- `ship1000x/data/litellm_prices.json` — vendored MIT snapshot of the
  LiteLLM model pricing registry (BerriAI/litellm), pinned at commit
  `35f6961526023a89635885194272e894d1b8454f` (2026-05-23, 2731 entries).
  Externalises pricing maintenance and prevents the class of bugs we
  hit in #23 (Anthropic billing ×100). Inspiration mode 2 (vendor with
  attribution) per the "inspired-by, not forked-from" strategy.
  See `ship1000x/data/LITELLM_ATTRIBUTION.md` for the upstream source,
  license notice, and sync workflow. (#35)
- `ship1000x/scripts/sync_litellm_prices.py` — stdlib-only sync helper
  that fetches the latest upstream commit SHA from the GitHub API,
  downloads at that SHA, prints a diff summary with explicit drift
  flags for SHIP-tracked models. `--check` for diff-only,
  `--commit <sha>` to pin. (#35)
- `ship1000x/core/pricing_litellm.py` — `LiteLLMResolver` class shaped
  as a Wave 2 `PricingResolver` Port preview. Converts LiteLLM
  per-token costs to SHIP per-million-tokens; honours `litellm_provider`
  mapping for anthropic / openai / bedrock. (#35)
- `ship1000x/core/pricing.py:resolve_model_pricing_hybrid()` — tries
  LiteLLM first, falls back to the local table on miss. Exposes
  `pricing_source` so the audit pipeline can tell which resolution
  path each event used. (#35)
- `ship1000x/core/gitleaks_scan.py` — subprocess wrapper around the
  gitleaks binary (MIT, github.com/gitleaks/gitleaks). Returns
  sanitised findings (rule id, description, file basename only, line
  number, commit SHA, date) — never the matched secret text, never
  the committer name/email. Inspiration mode 3 (external tool). (#36)
- `git_secret_alert` source — opt-in via `privacy.yaml`'s
  `gitleaks_scan: true`. One event per finding. Optional user-local
  `~/.config/ship1000x/.gitleaks.toml` baseline to mute known false
  positives. Surfaced in `ship1000x coverage`. (#36)

### Changed

- `ship1000x/core/privacy.py:ALLOWED_META_KEYS` — added
  `gitleaks_rule_id`, `gitleaks_description`, `gitleaks_file`,
  `gitleaks_line` with sanitisation justification. (#36)
- `ship1000x/core/coverage.py:SOURCE_REGISTRY` — added
  `git_secret_alert` entry under `dev_code` category. (#36)

### Documented

- Central sanitize guard in `Storage.upsert_event` — already in place
  at `storage.py:276`. Locked by 5 new regression tests including a
  lint-style scan that no collector writes to the events table outside
  the `upsert_event` path. (#37)
- `insights_push.filter_payload_by_share_config` conservative defaults
  locked by 12 new regression tests: None and {} behave identically,
  financial keys stripped globally + per-project, project ids hashed
  except `unclassified` / `unknown` buckets, applied filter stamped
  in `_meta` for audit. (#38)

### Validated empirically

- Pricing divergence between SHIP-local table and the fresh LiteLLM
  snapshot found 3 drift cases on the maintainer's machine (`gpt-5.5`,
  `claude-opus-4-7`, `claude-haiku-4-5`) — exactly the class of
  silent dérive LiteLLM adoption prevents. (#35)
- `codex_macapp` re-ingestion validates the cross-source model
  resolver still resolves 28/43 events via `logs_2_sqlite_join` and
  15/43 as `logs_2_sqlite_expired` (sessions outside the OTEL
  retention window). No regression vs the previous run.

### Cleanup deferred to Wave 2

- The 14 collectors that still call `sanitize_event` upstream are
  safe (idempotent) but redundant. They will be cleaned up as part
  of the `EventStorage` port extraction in Wave 2.

## [Unreleased pre-Wave-1 work in Unreleased section]

### Added

- Added `ship1000x.core.usage` to normalize provider/client/model, token
  breakdown, pricing source/version, and measurement quality metadata.
- Added synthetic Codex rollout tests covering native token usage, cache tokens,
  reasoning tokens, and missing-token `unknown` quality.
- Added `ship1000x source-audit` and `ship1000x.core.source_quality` to expose
  audit-derived source truth: observed sources, unknown sources, missing
  normalized usage metadata, and expected vs observed token/cost quality.
- Added `/api/source-quality` for future dashboard/report surfaces without
  returning raw metadata, prompts, responses, paths, diffs, or commands.
- Added a fixture-only GitHub Copilot agents session parser
  (`ship1000x/collectors/copilot_agents.py`) plus a redacted synthetic
  fixture (`tests/fixtures/provider_expansion/copilot_agents_session.json`)
  and parser tests. The parser captures only safe session metadata (agent
  mode, tool calls, user messages, edits applied) and emits normalized
  unknown-tokens usage metadata when the fixture exposes no token
  breakdown. The `copilot_agents` source profile now states the contract
  is fixture-locked.
- Added `ship1000x reconcile`, a read-only cost reconciliation CLI that
  compares local-collector costs (`claude_code`, `codex`, ...) against
  billing snapshots (`anthropic_usage`, `openai_usage`) for the same
  calendar day. Reports per-day deltas, daily statuses
  (`ok`/`warn`/`missing_billing`/`missing_local`), non-factual cost share,
  and model-fallback events. Backed by `ship1000x.core.reconcile`. Markdown
  output. No provider API call, no store mutation, no prompt/response/path
  leakage. Documented in `docs/RECONCILE.md`.
- Added fixture-only Continue (continue.dev) and Aider session parsers
  (`ship1000x/collectors/continue_dev.py`,
  `ship1000x/collectors/aider.py`) plus redacted synthetic fixtures and
  parser tests for both providers. Each parser exercises both the native
  tokens path and the empty-`usage{}` unknown-tokens path. The new
  `continue` and `aider` source profiles are registered in
  `SOURCE_QUALITY_PROFILES`. The previous combined
  `continue_aider` provider gap is split into two `needs-fixture` entries
  pointing to the locked session contract.

- **Codex Desktop now reports real tokens** (input / output / cache_read /
  reasoning) extracted from `response.completed` SSE payloads inside
  `~/.codex/logs_2.sqlite`, instead of the legacy heuristic
  `active_sec × $10/h` estimate. Two new helpers in
  `ship1000x/collectors/codex_desktop.py`:
  - `_extract_usage_from_payload`: brace-balanced parser that reads
    `usage.input_tokens`, `usage.input_tokens_details.cached_tokens`,
    `usage.output_tokens`, `usage.output_tokens_details.reasoning_tokens`,
    `usage.total_tokens`, and the response id. Tolerates truncated rows,
    invalid JSON, missing detail blocks, and empty `usage:{}`.
  - `_detect_auth_mode`: session-wide auth-mode sniff from URL signals
    (`chatgpt.com/backend-api/` ⇒ `oauth`, `api.openai.com/v1/` ⇒
    `api_key`, both seen or none ⇒ `unknown`).
  When real tokens are exposed, the collector switches the cost basis
  from `hourly_active_time_estimate` to `token_equivalent_api` (uses
  `estimate_openai_cost` with the same `pricing.py` rate card every
  other SHIP collector uses), emits a `cost_quality: factual` usage
  block, fills `token_input`/`token_output` on the event, and tags
  `auth_mode` on the raw_meta. Validated on the real local store:
  21/21 events with real tokens (was 0/35), `cost_basis: token_equivalent_api`
  everywhere, models `gpt-5.5` (17) and `gpt-5.4-mini` (4), total
  $0.35 cost (cohérent avec OpenAI billing $0.35 — Codex Desktop OAuth
  ChatGPT Plus).
- Added a Claude Desktop session sidecar collector
  (`ship1000x/collectors/claude_desktop_sessions.py` + CLI source
  `--source claude_desktop`). Reads
  `~/Library/Application Support/Claude/claude-code-sessions/<org>/<user>/local_*.json`
  and emits one `claude_desktop` event per session with safe metadata
  only: auto-generated title, precise model string (including the
  `[1m]` context-window suffix the JSONL never exposes), reasoning
  effort, completed turn count, GitHub PR linkage (number / URL /
  repository / state), MCP tool counts, `cli_session_id` (joinable
  with the JSONL-side `claude_code` source). No prompt, response, or
  tool I/O is read. The collector is metadata-only; tokens and cost
  continue to flow through the `claude_code` source so this lot does
  not double-count usage. Validated on the real local store: 77
  sessions ingested with titles, top model
  `claude-opus-4-6[1m]` (39) followed by `claude-opus-4-7[1m]` (26),
  5 sessions linked to a GitHub PR, 2 279 completed turns total.
  Whitelisted new raw_meta keys (`cli_session_id`, `title`,
  `title_source`, `permission_mode`, `pr_number`, `pr_url`,
  `pr_repository`, `pr_state`, `remote_mcp_count`, `is_archived`) in
  `ship1000x.core.privacy.sanitize_event`.
- Added `ship1000x audit-cost`, a drill-down CLI that for every event of
  the chosen source(s) prints the SHIP-stored cost, a manual
  re-computation from per-model tokens × the published `pricing.py`
  rate card, and the per-rate breakdown (uncached input / cache_read /
  cache_write / output). The summary block exposes the max absolute and
  percent delta between stored and recomputed, so the operator can
  prove that a reconcile total like `$26,955` is derivable line by line
  from the raw token counts the provider itself wrote into the local
  logs. Backed by `ship1000x.core.cost_audit`. Validated on the real
  local store: 118 Claude Code events, max abs delta `$0.0001`, max
  pct delta `+0.0044%`. No provider API call, no store mutation, no
  prompt/response/path leakage.
- Added `ship1000x.core.auth_mode` with heuristic detectors for Claude
  Code/Desktop and Codex CLI/Desktop auth modes (`oauth` / `api_key` /
  `unknown`). Detection inspects the JSONL `entrypoint` (Claude) and
  `session_meta.originator` (Codex), and an optional API-key env-var
  presence test. Never reads credentials, token files, or the keychain.
- Wired auth_mode through `claude_code` and `codex` collectors so every
  ingested event now carries `raw_meta.usage.auth_mode`. The
  `build_usage_metadata` helper exposes the new `auth_mode` argument and
  splits the SHIP-estimated cost into `api_equivalent_usd` (always) and
  `billed_estimated_usd` (the full estimate under `api_key`, zero under
  `oauth`/`unknown`), preventing the dashboard from showing $26k of OAuth
  Claude Code usage as $26k of "billed cost" when Anthropic only invoices
  the overage portion.
- Extended `ship1000x reconcile` to read `raw_meta.model_stats` (the
  canonical SHIP-side per-model breakdown used by Claude Code, Codex,
  anthropic_usage and openai_usage) in addition to the legacy
  `usage.model_raw`. Prior versions always reported Claude Code events as
  "100% non-factual" with a `(empty)` model fallback because Claude Code
  never wrote `usage.model_raw`. The report now also includes a
  "Cost honest split" section (`API-equivalent` vs `Billed-estimated` vs
  `Subscription absorption`) and an "Auth mode distribution" table per
  local source.

- Aligned the Anthropic and OpenAI billing collectors on the SHIP cost
  honesty contract introduced by the auth_mode lot:
  * `anthropic_usage` and `openai_usage` events now pass
    `auth_mode="api_key"` to `build_usage_metadata`, so
    `raw_meta.usage.auth_mode` and `raw_meta.auth_mode` both surface
    `api_key` (billing snapshots represent what the user actually pays
    via API key today). The previous legacy label `billing_aggregated`
    is preserved under a new `billing_source` field so reports keep the
    rich detail (provider billing aggregates OAuth + API key on the
    provider side; SHIP needs to know it is API-key-billed for the
    honest split).
  * `model_stats[model]` now carries per-model `cache_read_tokens`,
    `cache_write_tokens` (Anthropic only — OpenAI exposes only
    `cache_read_tokens`), and `uncached_input_tokens` so downstream
    reports do not have to re-attribute from the top-level totals.
  * `usage.cost` now exposes both `api_equivalent_usd` and
    `billed_estimated_usd` (equal under `api_key`, matching the cost
    contract from PR #21).
- Whitelisted the new `billing_source` and `entrypoint` raw_meta keys
  in `ship1000x.core.privacy.sanitize_event`. Before this fix the
  strict allow-list silently dropped them at ingest time, which made
  the OAuth/api_key separation invisible to the reconcile report on
  freshly ingested billing events.

### Fixed

- **Codex Desktop stopped ingesting in mid-2026 because its SQLite layout
  changed silently.** The collector hardcoded
  `~/.codex/state_5.sqlite` with a `message` column. Codex Desktop now
  writes to `~/.codex/logs_2.sqlite` with a `feedback_log_body` column,
  and the legacy path was empty on freshly installed apps. The collector
  now tries `logs_2.sqlite` first and falls back to `state_5.sqlite`,
  and auto-detects whether the table exposes `feedback_log_body` (new)
  or `message` (legacy) via `PRAGMA table_info`. On a real machine this
  turned 0 newly-ingested events into 35 over 7 days.

- **Codex Desktop and Codex MacApp events never carried a model name**,
  so cost estimates defaulted to a 10 USD/h heuristic with no provider
  attribution and the reconcile audit showed `model=(none)` for
  115/124 historical events. Both collectors now extract the model
  identifier directly from their respective log streams:
  - `codex_desktop` reads OTEL spans (`model=gpt-5.5`) and JSON SSE
    payloads (`"model":"gpt-5-codex"`) in the `feedback_log_body`,
    keeps a per-day occurrence counter, and stores the dominant model
    on every `session_day` event.
  - `codex_macapp` reads `model=<id>` mentions from the app log lines
    (restricted to known prefixes `gpt-`, `o\d`, `claude-` to avoid
    matching unrelated `model=` strings), bucketed per day.
  Validated on the real local store: `codex_desktop` now reports
  `gpt-5.5` (27 events) and `gpt-5.4-mini` (8 events) across 35
  events with zero `model_raw=None`; `codex_macapp` recovers
  `gpt-5.4-mini` for 16 of 42 events (the remaining 26 are idle/git
  only days where the app never emitted a `model=<id>` line).

- **Anthropic and OpenAI billing cost was ~100x too high since the
  switch to cost_report fix (PR #20).** Empirical reverse-check on a
  known-tokens day (2026-04-27, claude-sonnet-4-6, 1M cache_read +
  1.7M cache_write + 9k output + 69 uncached) priced at $6.89 with
  published Anthropic per-token rates, while the cost_report endpoint
  returned $689.30 for the same day. Whatever unit the field is
  actually expressed in (it is labeled `"currency": "USD"` in the
  response), it does not match the per-token billed cost the SHIP
  cost-honesty contract expects. The user reported "$100-200" as their
  rough real billing, which matches the per-token computation
  ($62.59/30d) and not the cost_report value ($6,275/30d).
  
  Fix: anthropic_usage and openai_usage now compute `cost_estimated`
  from `tokens × pricing.py` (the same source of truth used by every
  other SHIP collector: claude_code, codex, ...) instead of the
  provider Admin cost endpoint. The legacy API value is preserved
  under `raw_meta.api_reported_cost` for future divergence analysis
  and added to the privacy whitelist. `cost_basis` updated to
  `tokens_times_published_{provider}_pricing` so reports clearly show
  the source of the number.
  
  Validation on real local store:
    Before fix: 30d Anthropic billing = $6,275.23
    After fix:  30d Anthropic billing =    $62.59
    Before fix: 30d OpenAI billing    =     $0.55
    After fix:  30d OpenAI billing    =     $0.35
  Users must re-ingest anthropic_usage and openai_usage to back-fill.

- **Anthropic billing cost was silently $0 on every billing snapshot.**
  The Admin API `cost_report` endpoint changed its response shape in 2026
  to carry `amount` as a numeric string at result root
  (`"amount": "20.394575"`, `"currency": "USD"`) instead of the legacy
  `{"amount": {"value": ..., "currency": ...}}` object. The parser still
  looked for `amount.value` and silently fell back to 0, causing every
  `anthropic_usage` event to land in the store with `cost_estimated=0`.
  Combined with a `try/except Exception: pass` around the cost extraction
  block, the bug was invisible. Fixed with a new `_extract_amount_usd`
  helper that supports the current 2026 numeric-string shape, the legacy
  object shape, `cost_usd` / `amount_usd` fallbacks, and non-USD
  currency rejection. Network/HTTP errors from `_fetch_cost_report` are
  now raised and logged instead of silently returning an empty list, so
  a misconfigured admin key or expired token does not leave billing at 0
  forever. Validated on the real local store: 28 days, $6 310.57 of
  Anthropic billing now surfaced where SHIP previously reported $0.
- **Codex CLI tokens were silently zero on every 2026+ rollout.** The
  parser only recognized the legacy top-level `type: "token_count"` record,
  but Codex 2026+ writes token snapshots as
  `type: "event_msg"` + `payload.type: "token_count"` +
  `payload.info.total_token_usage.{input_tokens, output_tokens,
  cached_input_tokens, reasoning_output_tokens}`. The parser now reads the
  2026+ shape in addition to the legacy one, and a defensive scan of
  `total_token_usage` at record/payload root is kept as last resort. Real
  Codex CLI sessions that previously showed `tokens=0`, `cost=$0` now
  report accurate input/output/cache/reasoning tokens and a factual cost.
- **Codex CLI model was always mistaken for `gpt-5`** when the actual model
  was something else (`gpt-5.5`, `gpt-5-codex`). The parser only looked at
  the persona-time regex in `session_meta.base_instructions.text`. It now
  also reads `turn_context.payload.model`, which Codex 2026+ emits per
  turn and which carries the precise model. `turn_context` overrides the
  legacy regex when both are present. The conservative `gpt-5` fallback is
  preserved for rollouts that expose neither field.

### Changed

- Added `gpt-5.5` to `OPENAI_PRICING` (same per-token rates as `gpt-5`) and
  to `MODEL_ALIASES` so `model_canonical` stays exact instead of collapsing
  to `gpt-5` via substring matching.
- Claude Code `session_day` events now include safe `raw_meta.usage` metadata
  with uncached input, output, cache read, cache write, pricing version,
  provenance, and factual/defensible quality labels.
- Anthropic and OpenAI billing snapshots now include safe `raw_meta.usage`
  metadata so official API/billing truth can be compared with local collectors
  without exposing raw request content.
- Cline task events and Cursor AI block events now include safe
  `raw_meta.usage` metadata with explicit `tokens=unknown` and `cost=unknown`
  instead of ambiguous zeroes.
- Codex CLI, Codex MacApp, and Codex Desktop events now include safe
  `raw_meta.usage` metadata distinguishing factual tokens, defensible active
  time, indicative heuristic cost, and unknown values.
- Privacy sanitization now recursively drops nested content-like keys from
  allowed metadata maps before storage.
- Trust Score robustness checks now include the source quality audit so high
  `confidence_flag` values cannot hide fragile, partial, unknown, or
  missing-usage sources.
- Dashboard `/api/highlights` now computes `cost_factual` from
  `raw_meta.usage` cost quality instead of a hard-coded source allowlist.

## [0.5.0] — 2026-05-15 — Trust Score honesty fix + 3-ratios overview redesign

### Added — Web dashboard overview restructured around 3 explicit ratios
- **Pedagogical banner** at the top : "Ship1000x reports three distinct ratios
  of AI leverage — most dashboards mix them silently. Here they're separated,
  with their confidence label."
- **Section Engagement** (Factual) : your active hours · wall hours ·
  person-days equivalent. The raw inputs.
- **Section R1 · Time leverage** (Factual) : `time leverage ×N` (cumulated
  work / your active) + `parallel agents` (avg simultaneous IA sessions),
  with explicit "ℹ For each hour you're active, this much total work happens".
- **Section R2 · Agent efficiency** (Indicative) : pedagogical block stating
  "10× — 50× per task (refactor ~30×, boilerplate ~50×, debug ~5×). Not
  measured directly by Ship1000x — task-dependent." Honest about the limit.
- **Section R3 · Project output vs human team** (Estimative · your input) :
  project selector (top 10 by hours in window) + 2 inputs (people, months) +
  computed leverage `×N` with the formula shown explicitly. Per-project
  estimates persist in localStorage. Disclaimer : "Self-reported. Ship1000x
  cannot estimate this for you — only YOU know your project's true scope."
- **Section Cost · Output · Trust** : real lines · total cost · trust score
  in one row, each with their own confidence label.

### Why this redesign
The previous overview presented "AI Leverage ×3" as a single headline number
without saying which ratio it represented. Three different ratios were being
conflated across LinkedIn, vendor decks, and our own dashboard. The fix is
to separate them, label each one's confidence (Factual / Indicative /
Estimative), and let the user input the part Ship1000x cannot measure
(R3 — project scope vs equivalent human team).



### Changed — Trust Score is now the raw weighted average per source
- **No additive bonuses, no silent cap.** Previously the global score was
  computed as `min(100, base + bonuses - penalties)`, which meant a base
  of 96 + 8 bonuses showed as 100/100 — visually identical to a true
  100/100, with the cap absorbing the inflation invisibly. This is exactly
  the kind of opaque metric Ship1000x is supposed to denounce.
- **New shape :** `compute_global_score()` returns
  `{score, label, breakdown, robustness_checks}`. The `score` is the raw
  weighted-average of per-source confidence (data quality). The
  `robustness_checks` list reports independent qualitative signals about
  the measurement setup — they NEVER alter the score.
- **3 robustness checks reported :**
  - *Cadence calibrated* : user has a personal P95 profile (sample ≥ 100)
  - *Cross-source unified* : `daily_unified` populated (multi-agent dedup)
  - *Critical sources present* : `claude_code` AND `git` both have events

### Breaking — JSON API + dict shape
- `/api/highlights` : removed `trust_base`, `trust_bonus`. Added
  `trust_robustness` (list of `{name, passed, detail}`).
- `compute_global_score()` return dict : removed `base`, `bonus`, `penalty`,
  `bonus_reasons`, `penalty_reasons`. Added `robustness_checks`.

### Updated — UI surfaces
- **Web dashboard `overview.html`** : Trust card simplified to `score/100 ·
  label`. New "Robustness checks" section below with ✓/✗ icons.
- **CLI `highlights`** : Trust line shows `score/100 · label · weighted avg
  per source (raw)` then a "Robustness checks" sub-block.
- **CLI `insights`** : same — robustness checks replace the old
  base+bonuses breakdown.

### Documentation
- `docs/TRUST_SCORE.md` : composite section rewritten with explicit
  rationale for dropping additive bonuses (the 96+8=100 incident).

### Tests
- `tests/test_trust_score.py` : 9 new tests on `robustness_checks`,
  removed asserts on `bonus`/`penalty`/`base`. Added test verifying score
  is the raw weighted average (never capped, never inflated).
- `tests/test_dashboard_smoke.py` : asserts `trust_robustness` shape.
- Total : 130 tests pass.

## [0.4.0] — 2026-05-15 — Local web dashboard MVP

### Added
- **`ship1000x dashboard` command** : launches a local Flask web app at
  `http://localhost:10000` with auto-open in browser (opt-out via `--no-open`).
  Bound to `127.0.0.1` only (refuses external connections), no auth needed
  (local user), all queries read-only.
- **Overview page** : 6 metric cards (AI Leverage, Sessions in parallel,
  Person-days equivalent, Real lines, Total cost, Trust Score) + daily
  activity trend chart (Chart.js) + Trust Score breakdown per source.
- **Projects page** : sortable, filterable cross-tab matrix per project ×
  dominant tool × cost. Footer totals.
- **JSON API** : `/api/highlights`, `/api/trend`, `/api/projects`, `/api/trust`.
- **Premium neutral design** : ink/paper palette + ambre accent (aged gold),
  Inter typography (Linear/Vercel/Stripe reference), hairline borders, no
  gradients, dark mode auto + manual toggle. Footer signature
  "Built with ♥ by Mr1000xGrowth".
- **Window selector** : 7 / 14 / 30 / 60 / 90 / 180 / 365 days, synced via
  localStorage across pages.

### Tests
- `tests/test_dashboard_smoke.py` : 7 new tests (app factory + 6 routes).
- Total : 61 tests pass.

## [0.3.0] — 2026-05-15 — V1.1 quick wins

### Added
- **`ship1000x pulse` command** : one-line daily check. Shows today's
  hours + cost + commits + active sources + trend arrow vs 7-day average.
- **`ship1000x discover --github <owner>`** : queries `gh repo list` and
  suggests aliases for local project_ids that look like a GitHub repo
  name. Ready-to-copy YAML snippet output.
- **`MAX_ACTIVE_SEC_PER_SESSION` configurable** via env var
  `SHIP1000X_MAX_SESSION_HOURS` (default raised 12h → 16h).

### Documentation
- `docs/METHODOLOGY.md` adds sections 6.bis (wall_brut × 5 cap) and
  6.ter (MAX_ACTIVE rationale).

### Tests
- `tests/test_caps.py` : 7 regression tests on hardcoded caps.
- Total : 54 tests pass.

## [0.2.0] — 2026-05-15 — V1 hardening release

### Added — UX showcase + first-launch experience
- **`ship1000x highlights` command** : the WOW pitch in 30 seconds.
  Audit-ready numbers with explicit confidence labels per metric (Factual /
  Defensible / Indicative). Trust Score base + bonuses transparently
  displayed. Wall_clock capped at 5× duration_sec per source (anti-inflation).
- **First-launch UX** : `ship1000x init` now optionally chains
  `ingest → rollup → calibrate → highlights` so the user sees value
  immediately. Each step wrapped in try/except for graceful degradation.
- **`ship1000x summary` command** : cross-tabulated matrix per project ×
  tool (dominant tool with %, sessions IA, commits git, cost). Filter by
  `--client <name>` if projects.yaml has `client:` tags.
- **`ship1000x today --compare-modes`** : 5 active-time modes side by
  side (strict 5min / auto P95 / loose 15min / agent IA estimated /
  wall-clock) with arithmetic verification.
- **Sessions IA / Commits git split** in `tracker project` table —
  removes the ambiguity that made days with only git activity look like
  data was missing.

### Added — Aliases for project consolidation
- **`projects.yaml > aliases:` map** : merge multiple project_ids into a
  single canonical id (e.g. local folder name + git remote = one project).
  Applied transitively up to 5 hops. Resolves the user-reported issue of
  same logical project being fragmented into 5+ entries.
- New method `Classifier.resolve_alias()` applied automatically by
  `classify_session()`.

### Added — Cost & accuracy fixes
- **Claude Code SSE chunks dedup** : events `assistant` are now deduplicated
  by `message.id`, fixing a ~×2.49 overcount of output tokens and turns.
- **Claude Code cache tokens captured** : `cache_read_input_tokens` and
  `cache_creation_input_tokens` are now read and added to the cost
  computation. On a tested 4MB JSONL session, this captured 99M tokens
  previously ignored (vs 27K captured pre-fix = 0.03%).
- **Productivity ratios use `lines_real_added`** (V2 breakdown) :
  `lines_per_hour`, `lines_per_typed`, `cost_per_line_net` now exclude
  vendored / generated / seed code. Defensible vs audit. Raw versions kept
  as `_raw` aliases for retro-compat dashboards.

### Added — Multi-agent fix : unified active time cross-source
- **New `core/cadence.py`** : computes the user's personal P95 threshold
  for active time from inter-prompt intervals over a 14-day window. Stored
  in `user_cadence_profile` table.
- **New `core/unified_metrics.py`** : merges human events from all sources
  into a single sorted timeline, applies 4 thresholds (strict 5min / auto
  P95 / loose 15min / unified = P95), and exposes 5 metrics per day.
  Persisted in new `daily_unified` table. Resolves the multi-agent overcount
  bug (×2.85 measured on a 60-day real DB : 394h raw → 138h unified).
- **New `tracker calibrate` command** : displays the user's cadence profile
  (P50/P75/P90/P95/P99) and persists it.
- **New `tracker today --compare-modes` command** : displays the 5 modes
  side-by-side with arithmetic verification.

### Added — Trust Score
- **New `insights/trust_score.py`** : per-source confidence score (weighted
  average of event-level `confidence_flag` : high=100 / medium=70 / low=40)
  and global composite score with bonuses (cadence calibrated +3, unified
  populated +5) and penalties (critical sources missing -10).
- **`tracker insights` displays Trust Score** : per-source breakdown table +
  GLOBAL composite + bonus/penalty rationale.

### Fixed — Privacy hardening
- **Privacy filter no longer bypassed** : `sanitize_event` now deserializes
  `raw_meta` JSON before whitelist filtering (was silently bypassed for
  collectors passing JSON strings).
- **Whitelist aligned to 46 real keys** used by collectors (vs 14 outdated
  before). No metadata loss.
- **Recursive path anonymization** for `paths_sampled`, `files_touched`,
  `log_file`, `primary_project` (lists and nested dicts).
- **Central guardrail** : `sanitize_event` now called automatically in
  `storage.upsert_event`, even if the collector forgot it (idempotent).
- **`insights_push` share_config filter** : conservative defaults — email
  hashed (SHA256:16), financials stripped, project_ids hashed by default.

### Added — New collectors
- **`collectors/openclaw.py`** : OpenClaw integration (lobster way 🦞).
- **`collectors/anthropic_usage.py`** : Anthropic Admin API for official
  invoice cross-validation (Factual 100%).
- **`collectors/openai_usage.py`** : OpenAI usage API (Factual 100%).

### Added — Documentation
- **6 new public docs in English** : COVERAGE, METHODOLOGY, PRIVACY,
  TRUST_SCORE, COLLECTORS, QUICKSTART.

### Added — Pre-V1 hardening (already in [Unreleased] before this session)
- **Per-project consent wizard** : `ship1000x init` and the new
  `ship1000x projects --select` flag prompt for the share level
  (`aggregated` / `private` / `disabled`) of each detected project.
- **Unclassified projects warning** : `ship1000x daily` lists projects
  present in DB but absent from `share` map.
- **`core/consent_wizard` module** : reusable helpers covered by 14 unit
  tests.

### Planned for v0.2.0 (this release)
- This release ships all the V1 hardening above
- First PyPI release after tagging
- Continue.dev / Aider / Antigravity collectors deferred to v0.3.0
  (community contributions welcome — see CONTRIBUTING.md)

## [0.1.0] — 2026-04-21

### Added
- **11 collectors** : Claude Code, Codex CLI, Codex Desktop, Codex macOS app,
  Cursor, Cline, git (multi-repo), shell (zsh), macOS system, web exports
  (ZIP drop-in), Codex SQLite (legacy)
- **40+ CLI commands** : `init`, `setup`, `ingest`, `today`, `week`,
  `project`, `insights`, `multiplier`, `profile`, `signals`, `compare`,
  `export`, `rollup`, `push`, `daily`, `doctor`, `discover`, `reclassify`,
  `audit`, `backfill-machine-id`, `rename-machine`, `rename-user`,
  `install-scheduler`, `privacy`, `status`, `health`, `benchmark`, and more.
- **Line classification** : real / seed / vendored / generated, configurable
  via `config/line_classification.yaml` + per-project overrides, 36 unit
  tests covering glob matching, `.gitattributes` parsing, seed commit
  heuristics.
- **LLM cost estimation** : token-based for Anthropic (Claude) and OpenAI
  (Codex/GPT-5), pricing centralized in `core/pricing.py`. Heuristic fallback
  for Codex macOS app (flagged `is_estimated: true`).
- **Multi-machine support** : `machine_id` column on events +
  `unique_commit_hashes` / `machine_origin` in rollups. Dedup commits across
  laptop/desktop for the same user. `rename-machine` command for merging
  legacy entries.
- **Privacy layer** : three-level share (disabled / private / aggregated),
  path anonymization, keyword scrubbing. All ingestion read-only.
- **S3 push (opt-in)** : gzipped JSONL rollups partitioned by month/user/
  machine. Compatible with AWS S3, Backblaze B2, Cloudflare R2, Garage,
  MinIO.
- **Markdown report exporter** : `ship1000x export` generates a structured
  report suitable for reviews or PRs.
- **Auto-classification** : `resolve_repo_uid()` finds project_id via git
  remote or first commit hash, with `$HOME`-segment fallback. No manual
  `projects.yaml` required for 95% of cases.
- **48 unit tests**, all passing. AST-validated across the codebase.
- **MIT License**.

### Known limitations
- Codex macOS app cost is a heuristic (no token data exposed by the
  rollouts). Flagged in output. See `docs/ARCHITECTURE.md#llm-cost-estimation`.
- `machine_id = platform.node()` — renaming your Mac creates a new machine
  entry. Use `ship1000x rename-machine` to merge.
- No Windows support (Linux/macOS only, Windows untested).
- No built-in web dashboard yet — terminal views + Markdown export cover
  the v0.1.0 scope.
