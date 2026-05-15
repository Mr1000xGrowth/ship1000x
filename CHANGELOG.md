# Changelog

All notable changes to Ship1000x are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — V1 hardening (2026-05-15)

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
