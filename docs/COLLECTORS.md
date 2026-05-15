# Collectors — Detailed reference per data source

Reference document for each collector: where it reads, what it extracts, what confidence level the data carries, and known limitations.

For the high-level coverage matrix, see [`COVERAGE.md`](COVERAGE.md). For the philosophy of confidence levels, see [`TRUST_SCORE.md`](TRUST_SCORE.md).

Last updated: 2026-05-15 (V1).

---

## How to read this document

Each collector section follows the same structure:

```
### <collector_name>

**Source**: where it reads (path, format)
**What we extract**: tokens, cost, active time, etc.
**Confidence**: typical confidence_flag distribution + global score range
**How it's enabled**: default or opt-in, config key
**Limitations**: what we don't capture and why
**Privacy notes**: any specific privacy consideration
**Tests**: which test file covers it
```

---

## Active V1 collectors (12)

### `claude_code`

**Source**: `~/.claude/projects/<slug>/*.jsonl` — one JSONL file per session, line-per-event format

**What we extract**:
- User events (typed, approval, paste, tool_result, system) with msg_type and wordcount
- Assistant events with model, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens
- Tool uses with name and extracted paths
- Active time via inter-user-event intervals ≤ P95 threshold
- Cost via `core.pricing` (Anthropic official pricing)

**Confidence**: typically 70-95% (Defensible to Factual)
- `high` when full JSONL parsed cleanly with cache tokens captured
- `medium` for older format JSONLs (pre-V1 hardening)
- `low` for parse errors or missing fields

**How it's enabled**: enabled by default in `config/privacy.yaml`

**Limitations**:
- Cache tokens require V1 hardening patches (run `tracker reclassify --since 365d` to lift old events from `medium` to `high`)
- Doesn't capture sub-prompt SSE chunks (deduped by `message.id` to avoid ×2.49 overcount bug)

**Privacy notes**: paths in tool_uses are anonymized via `_extract_tool_paths`. No prompt content is stored.

**Tests**: indirectly via `tests/test_intervals.py` and `tests/test_insights.py`

---

### `codex` (Codex CLI)

**Source**: `~/.codex/codex.db` SQLite database

**What we extract**:
- Sessions with cwd, model, started/ended timestamps
- Tokens (input, output, cached_input, reasoning_output)
- Cost via `core.pricing` (OpenAI official pricing)
- Active time + tool_calls count

**Confidence**: typically 80-90% (Defensible to Factual)

**How it's enabled**: enabled by default

**Limitations**:
- Doesn't capture cross-session continuations (each session = independent rollup)

**Tests**: TBD — contributions welcome

---

### `codex_macapp` (Codex MacApp)

**Source**: SSE logs in `~/Library/Logs/com.openai.codex/`

**What we extract**:
- Turn count via `response.created` events
- Active time via interval heuristic between turns
- Cost via heuristic (`turn_count × average_cost_per_turn[model]`)

**Confidence**: typically 70-85% (Defensible)
- `high` when paths classified and ratio ≥ 0.5
- `medium` otherwise (cost via heuristic, tokens often 0)

**How it's enabled**: enabled by default

**Limitations**:
- Tokens usually 0 (not exposed by Codex MacApp SSE format)
- Cost is heuristic — cross-validate with `openai_usage` for absolute claims

**Tests**: TBD — contributions welcome

---

### `codex_desktop` (Codex Desktop electron app)

**Source**: `~/.codex/state_5.sqlite` — threads + tool definitions metadata

**What we extract**:
- Threads with cwd, model_provider, source, title, timestamps
- Active time via timeline of user inputs (when present)
- Cost via heuristic (turn_count × per-turn estimate)

**Confidence**: typically 50-70% (Indicative to Defensible)

**How it's enabled**: enabled by default

**Limitations**:
- `tokens=0 by design` — Codex Desktop does not expose token counts in `state_5.sqlite`
- We attempted parsing `logs_2.sqlite` (212 MB OTEL trace logs) but only ~10 logs out of 11 530 had useful token data → not worth the parsing complexity. Marked Indicative.

**Tests**: TBD

---

### `cline`

**Source**: `~/Library/Application Support/<editor>/User/globalStorage/saoudrizwan.claude-dev/tasks/<task_id>/`
- `task_metadata.json` — task metadata + model_usage entries (only ts/model/mode)
- `api_conversation_history.json` — message structure (no usage stats)
- `ui_messages.json` — UI events with timestamps

**What we extract**:
- Task duration via UI message timestamps (when present) or wall-clock fallback
- Files touched count
- API turn count
- Mode (act / plan)

**Confidence**: typically 60-80% (Defensible)

**How it's enabled**: enabled by default

**Limitations**:
- **Tokens and cost are NOT captured**: Cline does not expose `input_tokens` / `output_tokens` / `total_cost` in `model_usage` entries (only `ts`, `model_id`, `mode`)
- Real tokens/cost are stored in the editor's `state.vscdb` extension storage (~10 GB on Cursor) — extraction deferred to V1.1+
- Active time uses `wall_clock × 0.5` heuristic when UI message timestamps are missing

**Tests**: TBD

---

### `cursor`

**Source**: `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` (~10 GB) + `~/Library/Application Support/Cursor/User/scored_commits.json`

**What we extract** (current limited scope):
- Scored commits with `composerLinesAdded` / `humanLinesAdded` (V2)
- Block count, file count, extensions touched

**Confidence**: typically 40-60% (Indicative)

**How it's enabled**: **DISABLED by default V1** (deferred V1.1)
- To enable: `config/privacy.yaml` → `sources.cursor: enabled`

**Limitations**:
- `state.vscdb` is ~10 GB on active Cursor users — full parsing of composer bubbles would degrade UX (~30s per ingest scan)
- Token / cost extraction blocked on this parsing
- Only scored_commits is read in V1

**Roadmap**: V1.1 plans for incremental indexing or subset captures to make this collector scalable

**Tests**: TBD

---

### `openclaw` (lobster way 🦞)

**Source**: `~/.openclaw/` — local SQLite database (MIT-licensed OpenClaw OSS)

**What we extract**:
- Sessions with cwd, agent_name, model_stats
- Tokens (full breakdown), cost (factual)
- Active time + event timeline
- Project classification via cwd + agent_name hint

**Confidence**: typically 85-95% (Factual)

**How it's enabled**: enabled by default if `~/.openclaw/` exists

**Limitations**: none significant (OpenClaw exposes a clean structured format)

**Tests**: TBD

---

### `git_multi`

**Source**: scans configured repos via `git log --numstat` subprocess

**What we extract**:
- Commit hash, author, timestamp, message
- Lines added/deleted with V2 breakdown via `core.line_classifier`:
  - `lines_real_*` (real code)
  - `lines_seed_*` (initial scaffolding)
  - `lines_vendored_*` (npm install, lockfiles)
  - `lines_generated_*` (codegen, builds)
- Files changed count
- Project classification via remote URL + local path

**Confidence**: typically 95-100% (Factual)

**How it's enabled**: enabled by default

**Limitations**:
- The classifier needs proper rules in `config/projects.yaml` to attribute commits — see `core.line_classifier.LineClassifier` for default patterns
- Doesn't currently detect `Co-Authored-By: Claude/Codex` trailers (V1.1 to add an `ai_generated` category)

**Tests**: covered by `tests/test_line_classifier.py` and `tests/test_rollup_intervals.py`

---

### `shell` (zsh / bash history)

**Source**: shell history file (`~/.zsh_history` or `~/.bash_history`) — requires EXTENDED_HISTORY enabled

**What we extract**:
- Command timestamps + duration
- Project classification via cwd at time of command (when EXTENDED_HISTORY enabled)

**Confidence**: typically 40-60% (Indicative)

**How it's enabled**: **DISABLED by default**
- Requires manual setup: enable `setopt EXTENDED_HISTORY` in `~/.zshrc`
- Then `config/privacy.yaml` → `sources.shell: enabled`

**Limitations**:
- Shell history doesn't include real session duration; we approximate
- No content stored (just command names + timing)

**Tests**: TBD

---

### `mac_system` (macOS focus apps)

**Source**: `pmset -g log` (sleep/wake events) + active app focus events

**What we extract**:
- App activation intervals (when permitted by macOS)
- Sleep/wake gaps (used to refine active time computation)

**Confidence**: typically 50-70% (Indicative)

**How it's enabled**: **DISABLED by default**
- Requires manual permission setup (Full Disk Access for `pmset` log access)
- Then `config/privacy.yaml` → `sources.mac_system: enabled`

**Limitations**:
- macOS API restrictions on app focus monitoring
- Approximate timing only

**Tests**: TBD

---

### `web_exports` (drop folder for manual ChatGPT/Claude/Gemini exports)

**Source**: `~/ship1000x/drop/` — surveillance folder for manual file imports

**Supported formats**:
- Claude.ai ZIP exports → `conversations.json` inside
- ChatGPT JSON exports → standalone `conversations.json`
- Gemini Takeout exports → various structures
- Generic conversations.json files

**What we extract**:
- Conversation count, title (anonymized), timestamps
- Wordcount per message (no content)
- Model when exposed by export
- Cost via `core.pricing` when tokens are present in export

**Confidence**: typically 90-95% (Factual)

**How it's enabled**: enabled by default — just drop files in `~/ship1000x/drop/`

**Workflow**:
```bash
# 1. Export from claude.ai / chatgpt.com / gemini.google.com
# 2. Drop the ZIP/JSON in ~/ship1000x/drop/
# 3. tracker ingest — automatically scans and processes
```

**Privacy notes**: `web_exports` reads ONLY metadata (titles, timestamps, message counts, wordcounts). The original export files remain untouched in the drop folder.

**Tests**: TBD

---

### `anthropic_usage` (Anthropic Admin API — official validation)

**Source**: Anthropic Admin API endpoint `/v1/organizations/usage_report`

**What we extract**:
- Daily aggregated token counts per workspace and model
- Daily aggregated cost per workspace
- Auth mode (API key vs OAuth Pro/Max/Teams)

**Confidence**: typically 100% (Factual — this is the official invoice)

**How it's enabled**: opt-in, requires `ANTHROPIC_ADMIN_KEY` env var

**Limitations**:
- Requires an Admin API key (`sk-ant-admin-...`), not a regular API key
- Available only for Build (Pay-as-you-go) and Scale (Enterprise) plans, not Pro/Max personal
- Claude Code via Max subscription is NOT counted here (max is OAuth-based, separate billing)

**Use case**: cross-validate Ship1000x's local measurements vs Anthropic's official invoice via `ship1000x-reconciliation/reconcile.py`

**Tests**: TBD

---

### `openai_usage` (OpenAI usage API)

**Source**: OpenAI usage API endpoint

**What we extract**: same structure as `anthropic_usage` (daily aggregates, official validation)

**Confidence**: typically 100% (Factual)

**How it's enabled**: opt-in, requires OpenAI API key

**Tests**: TBD

---

## Disabled / deferred V1 collectors

### `cursor` (deferred V1.1)

See dedicated section above. Disabled by default due to ~10 GB `state.vscdb` parsing cost.

To enable manually: `config/privacy.yaml` → `sources.cursor: enabled`

---

## Planned V1 / V1.1 collectors (not shipped)

These collectors are documented as targets for community contributions. See [`CONTRIBUTING.md`](../CONTRIBUTING.md#adding-a-new-collector) for the anatomy and priority list.

### `continue_dev` — V1 target

**Source**: `~/.continue/sessions/*.json`
**Estimated effort**: ~6h
**Status**: needs an external contributor (the maintainer doesn't use Continue.dev)

### `aider` — V1.1

**Source**: `~/.aider.chat.history.md` (markdown format)
**Estimated effort**: ~4h
**Status**: simple format, defer until usage warrants

### `antigravity` — V2

**Source**: TBD — Google Antigravity stores nothing locally as of audit (2026-05-15)
**Status**: blocked on Google publishing a local data export spec or API

### `gemini_usage` — V1 target

**Source**: Google Cloud Billing API
**Estimated effort**: ~6h
**Status**: needs Google Cloud setup (project ID + service account)

### `windsurf` (local mode) — V1.1

**Status**: TBD pending audit of its local data structure

### `copilot_chat` — V1.1

**Status**: TBD pending audit of `~/Library/.../copilot-chat/...` structure

---

## Adding a new collector

See [`CONTRIBUTING.md`](../CONTRIBUTING.md#adding-a-new-collector) for the detailed anatomy:
- Event dict structure expected by `core/storage.upsert_event`
- Idempotency, read-only, no-content-stored rules
- Confidence flag heuristics
- Reference implementations to copy from

---

## How collectors interact with the Trust Score

Each collector sets a `confidence_flag` on every event ("high" / "medium" / "low") which feeds into:

1. **Per-source score** in `tracker insights` — weighted average of confidence flags
2. **Global composite score** — bonuses and penalties
3. **Audit trail** for cross-validation against official APIs (via `ship1000x-reconciliation/`)

See [`TRUST_SCORE.md`](TRUST_SCORE.md) for the scoring methodology.
