# Changelog

All notable changes to Ship1000x are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Per-project consent wizard** : `ship1000x init` and the new
  `ship1000x projects --select` flag prompt for the share level
  (`aggregated` / `private` / `disabled`) of each detected project, instead
  of defaulting every git repo to `aggregated` silently.
- **Unclassified projects warning** : `ship1000x daily` now lists projects
  present in the local DB but absent from the `share` map in `privacy.yaml`,
  pointing the user at `ship1000x projects --select`. New projects still
  fall back to `_default` (private by default) so nothing leaks
  unintentionally.
- **`core/consent_wizard` module** : reusable helpers
  (`prompt_share_levels`, `find_unclassified_projects`,
  `collect_db_projects`, `collect_detected_repos`) covered by 14 new
  unit tests.

### Planned for v0.2.0
- Local Flask web dashboard (`ship1000x dashboard` → `localhost:8765`)
- First PyPI release
- GitHub API enrichment (CI status, PR reviews)
- Aider + Zed collectors

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
