# Architecture

> **⚠️ STATE : partial — covers v0.1.0 baseline. The V1 hardening release
> (v0.2.0) added modules not yet documented here :**
>
> - `core/cadence.py` — personal P95 threshold computation
> - `core/unified_metrics.py` — cross-source unified active time +
>   new `daily_unified` table (5 modes pre-computed per date+machine)
> - `insights/trust_score.py` — per-source confidence + global composite
> - `collectors/openclaw.py`, `anthropic_usage.py`, `openai_usage.py`
>
> Refer to [`COVERAGE.md`](COVERAGE.md), [`METHODOLOGY.md`](METHODOLOGY.md),
> and [`TRUST_SCORE.md`](TRUST_SCORE.md) for the V1 specifics. Full
> ARCHITECTURE update planned for v0.3.0.

## Overview

Ship1000x is a three-layer tool :

```
┌─────────────────────────────────────────────────────────────┐
│  CLI (ship1000x)                                            │
│  40+ commands : init, ingest, today, insights, push, ...    │
└─────────────────────────────┬───────────────────────────────┘
                              │
           ┌──────────────────┼──────────────────┐
           ▼                  ▼                  ▼
   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
   │  Collectors  │   │     Core     │   │   Exporters  │
   │  (11 sources)│   │  (storage,   │   │  (S3, MD)    │
   │              │   │   classifier,│   │              │
   │              │   │   pricing,   │   │              │
   │              │   │   privacy)   │   │              │
   └──────────────┘   └──────┬───────┘   └──────┬───────┘
                              │                  │
                              ▼                  ▼
                      ┌──────────────┐   ┌──────────────┐
                      │  SQLite DB   │   │  Your own S3 │
                      │  (local)     │   │  bucket      │
                      └──────────────┘   │  (opt-in)    │
                                         └──────────────┘
```

Local-first : everything lives in `~/.config/ship1000x/` unless you
explicitly enable cloud push.

## Data flow

1. **Ingest** : `ship1000x ingest` runs each enabled collector. Each reads
   its source (log files, SQLite DBs, `git log` output) and emits normalized
   events into `state.sqlite`.
2. **Rollup** : `ship1000x rollup` aggregates events by `(date, project, source, machine_id)` into `daily_rollup`. This is the push-eligible format.
3. **Insights** : `ship1000x insights` computes ratios, multiplier, signals
   from events + rollups (no push required).
4. **Push** (opt-in) : `ship1000x push` sends gzipped JSONL of rollups to
   your configured S3 bucket. Same for `ship1000x push-insights`.

## SQLite schema

Single file, incremental migrations tracked via `schema_version`. Main tables :

- `events` : one row per collected activity. Columns : `id` (hash), `source`,
  `session_id`, `project_id`, `started_at`, `ended_at`, `duration_sec`,
  `token_input`, `token_output`, `cost_estimated`, `raw_meta` (JSON),
  `machine_id`.
- `sessions` : session-level aggregation of events by `(source, session_id)`.
- `daily_rollup` : pre-aggregated metrics. Primary key
  `(date, project_id, source, machine_id)`.
- `ingestion_state` : per-collector offsets for idempotent re-runs
  (file mtimes, last ingested timestamps).

## Project classification (the hard part)

A path like `/Users/alice/code/my-app/src/index.ts` has to resolve to a
`project_id` so reports aggregate correctly.

Classifier strategy, in order :

1. **Explicit rule** via `config/projects.yaml` (if user added overrides)
2. **Git remote** : walk up the path to find `.git`, read `origin/url`,
   normalize to `github.com/user/repo`. Same repo across machines =
   same project_id. Cached via `lru_cache`.
3. **Git first-commit hash** : for repos without remote, use the first
   commit SHA as a stable local ID.
4. **Fallback** : use the first segment under `$HOME` (e.g.
   `~/code/my-app/...` → `my-app`). Bounded, never leaks absolute paths.
5. **Unclassified** : path outside `$HOME`, no git, no fallback match.
   Kept in a dedicated bucket so stats stay honest (usually ~5% of events).

See `ship1000x/core/classifier.py::resolve_repo_uid()`.

## Line classification

Git commits have four line categories :

- **real** : genuine code changes (the number you want to brag about)
- **seed** : initial scaffolding, generated from templates (`create-next-app`,
  `cargo init`, etc.) — detected via commit message + size heuristic
- **vendored** : dependencies committed (node_modules, vendor/) — detected
  via glob rules + `.gitattributes`
- **generated** : lockfiles, build outputs, migrations — glob rules +
  `.gitattributes`

Config in `config/line_classification.yaml`, with per-project overrides
supported. 36 dedicated unit tests in `tests/test_line_classifier.py`.

See `ship1000x/core/line_classifier.py`.

## LLM cost estimation

Tokens are extracted from rollouts when available :

- **Claude Code** : `summary.tokens.*` from history.jsonl (input, output,
  cache_read, cache_write)
- **Codex CLI** : `total_token_usage` from rollout.jsonl (input,
  cached_input, output, reasoning_output)
- **Codex macOS app** : **heuristic** (10 $/h of active time). Flagged
  `is_estimated: true` in payloads because there's no token data.
- **Cursor / Cline** : no token data exposed, cost = 0 (honest)

Pricing per model in `ship1000x/core/pricing.py` :

```python
ANTHROPIC_PRICING = {
    "claude-opus-4-6":  {"input": 15.0, "output": 75.0, ...},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, ...},
    ...
}
OPENAI_PRICING = {
    "gpt-5":        {"input": 1.25, "output": 10.0, ...},
    "gpt-5-codex":  {"input": 1.25, "output": 10.0, ...},
    ...
}
```

Update when providers change prices. PRs welcome.

## Multi-machine dedup

Users with multiple Macs (laptop + desktop) would double-count commits
shared across machines. Mitigation :

1. Each event carries `machine_id = platform.node()` (Mac hostname).
2. Rollup computes `unique_commit_hashes` per `(date, project)` across
   all machines, and picks `machine_origin` (first-machine-to-see).
3. Downstream readers (Markdown report, S3 consumer) should count by
   `unique_commit_hashes` size, not raw event count.

Caveat : if you rename your Mac, `platform.node()` changes and a new
machine_id appears. Use `ship1000x rename-machine <old> <new>` to merge
back.

## Privacy layer

Every collector writes events through `core/privacy.py::sanitize_event()` :

1. `is_excluded_path(path)` : drop events whose paths match
   `config/privacy.yaml exclude_paths`
2. `anonymize_path(path)` : strip user home prefix (`/Users/alice/x` →
   `~/x`). Keeps project structure, hides identity.
3. Scrub `raw_meta` for keywords (`password`, `secret`, `credentials`).

Privacy rules are **defense in depth**. Main protection is : nothing
leaves your machine unless you opt in to cloud push.

## Cloud push (opt-in)

Gzipped JSONL per month per user per machine :

```
s3://<bucket>/rollups/<YYYY-MM>/<user-slug>/<machine-slug>.jsonl.gz
s3://<bucket>/insights/<YYYY-MM>/<user-slug>/<machine-slug>.json
s3://<bucket>/health/<user-slug>.json
```

Compatible with any S3-API provider. For AWS SDK JS v3 readers, don't set
`ContentEncoding: gzip` (the SDK auto-decodes, breaks explicit gunzip).
Current implementation already handles this.

## Schema versioning

- `storage.py::SCHEMA_VERSION` : integer, incremented on breaking migrations
- Migrations applied at Storage init, idempotent
- When migrating rollups (adding columns to PK), old rollups are dropped and
  regenerated from `events` via `ship1000x rollup` — no data loss since
  events are the source of truth.

## Non-goals

- **Multi-user team aggregation** inside the tracker itself. Each user runs
  their own ship1000x instance. Team views are the job of a separate consumer
  reading the S3 bucket (not part of this OSS project).
- **Cloud-hosted dashboard**. Local-first by design.
- **Real-time tracking**. Ingestion is batch (run on cron or manually).
