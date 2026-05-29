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

## Active V1 collectors (19)

### `claude_code`

**Source**: `~/.claude/projects/<slug>/*.jsonl` — one JSONL file per session, line-per-event format

**What we extract**:
- User events (typed, approval, paste, tool_result, system) with msg_type and wordcount
- Assistant events with model, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens
- Tool uses with name and extracted paths
- Active time via inter-user-event intervals ≤ P95 threshold
- API-equivalent cost via `core.pricing` (local snapshot of Anthropic rate cards)
- Safe `raw_meta.usage` quality metadata:
  - `tokens=factual` when message usage exposes native tokens;
  - `cost=factual` when cost is derived from native tokens and pricing;
  - `active_time=defensible` because active time is interval-modeled;
  - cache read/write tokens are preserved separately from uncached input.

**Confidence**: typically 70-95% (Defensible to Factual)
- `high` when full JSONL parsed cleanly with cache tokens captured
- `medium` for older format JSONLs (pre-V1 hardening)
- `low` for parse errors or missing fields

**How it's enabled**: enabled by default in `config/privacy.yaml`

**Limitations**:
- Cache tokens require V1 hardening patches. For old events, run `ship1000x history-audit --since 365d`, preview with `ship1000x reclassify --since 365d --dry-run`, then reclassify only if the original local source files still exist.
- Doesn't capture sub-prompt SSE chunks (deduped by `message.id` to avoid ×2.49 overcount bug)
- If a Claude Code JSONL lacks `message.usage`, `source-audit` will mark the
  source as missing normalized usage metadata for that window.

**Privacy notes**: paths in tool_uses are anonymized via `_extract_tool_paths`. No prompt content is stored.

**Tests**: `tests/test_claude_usage_metadata.py`, plus interval and insight tests.

---

### `codex` (Codex CLI)

**Source**: `~/.codex/sessions/**/rollout-*.jsonl`

**What we extract**:
- Sessions with cwd, model, started/ended timestamps
- Tokens (input, output, cached_input, reasoning_output)
- API-equivalent cost via `core.pricing` (local snapshot of OpenAI rate cards)
- Active time + tool_calls count
- Safe `raw_meta.usage` quality metadata:
  - `tokens=factual` when native `total_token_usage` exists;
  - `cost=factual` when cost is derived from native tokens;
  - `active_time=defensible` because active time is interval-modeled.

**Confidence**: typically 80-90% (Defensible to Factual)

**How it's enabled**: enabled by default

**Limitations**:
- Doesn't capture cross-session continuations (each session = independent rollup)
- If a rollout lacks token counters, token quality is `unknown` and cost quality
  is `unknown`; the event is still useful for coverage and active-time context.

**Tests**: `tests/test_codex_usage_metadata.py`

---

### `codex_macapp` (Codex MacApp)

**Source**: SSE logs in `~/Library/Logs/com.openai.codex/`

**What we extract**:
- Turn count via `response.created` events
- Active time via interval heuristic between turns
- Cost via hourly active-time heuristic
- Safe `raw_meta.usage` quality metadata:
  - `tokens=unknown` because the app logs do not expose native tokens;
  - `cost=indicative` because cost is modeled from active time;
  - `active_time=defensible` because active time is interval-modeled.

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
- Cost via hourly active-time heuristic
- Safe `raw_meta.usage` quality metadata:
  - `tokens=unknown` because `state_5.sqlite` does not expose native tokens;
  - `cost=indicative` because cost is modeled from active time;
  - `active_time=defensible` because active time is interval-modeled.

**Confidence**: typically 50-70% (Indicative to Defensible)

**How it's enabled**: enabled by default

**Limitations**:
- `tokens=unknown` by design — Codex Desktop does not expose native token counts in `state_5.sqlite`
- We attempted parsing `logs_2.sqlite` (212 MB OTEL trace logs) but only ~10 logs out of 11 530 had useful token data → not worth the parsing complexity. Marked Indicative.

**Tests**: TBD

---

### Provider expansion fixtures (`gemini_cli`, `opencode`, `cursor_agent`)

**Source**: synthetic files under `tests/fixtures/provider_expansion/`

**What we extract**:
- Redacted session metadata: session id, timestamps, active/wall-clock seconds
- Safe provider/client/model labels
- Token breakdown when the fixture exposes it
- Agent/task counters such as turns, tool calls, files-in-context counts
- Safe `raw_meta.usage` quality metadata

**Confidence**:
- `gemini_cli`: tokens can be `factual` in the fixture contract, but cost stays
  `unknown` until Google pricing/model aliases are configured.
- `opencode`: token/cost quality stays `unknown` when the fixture does not
  expose native counters.
- `cursor_agent`: separated from generic Cursor metadata; token/cost quality
  remains `unknown`.

**How it's enabled**: not enabled. These parsers are fixture-only and are not
called by `ship1000x ingest`.

**Limitations**:
- No real Gemini CLI, OpenCode, or Cursor Agent logs are discovered or read.
- No prompt, response, path, diff, or raw provider payload is preserved.
- A future collector must first add redacted fixtures that document the real
  local format.

**Tests**: `tests/test_provider_expansion_fixtures.py`

### `roo_kilo_code`

**Source**: Roo Code / Kilo Code extension task metadata, parsed with explicit
variant identity instead of inheriting generic Cline confidence.

**What we extract**:
- Task duration and active-time estimate from the Cline-style task files
- Variant identity (`roo-code` or `kilo-code`)
- Files touched and message/task counters
- Safe `raw_meta.usage` metadata with token/cost quality kept `unknown`

**Confidence**: partial. Activity metadata is bounded and useful, but native
token and cost truth are not exposed by the task metadata.

**How it's enabled**: wired into normal ingest. It can also be run explicitly:
`ship1000x ingest --source roo_kilo_code`.

**Limitations**:
- Does not infer Roo/Kilo from generic Cline rows.
- Token/cost truth remains unknown until variant-specific usage and pricing
  provenance are validated.
- No prompt, response, path, diff, or raw provider payload is preserved.

**Tests**: `tests/test_provider_expansion_fixtures.py`,
`tests/test_ingest_cli.py`, `tests/test_all_collectors_implement_source_collector.py`

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
- Safe `raw_meta.usage` quality metadata:
  - `tokens=unknown` because task metadata does not expose native token counters;
  - `cost=unknown` because task metadata does not expose spend;
  - `active_time=defensible` when UI timestamps are available.

**Confidence**: typically 60-80% (Defensible)

**How it's enabled**: enabled by default

**Limitations**:
- **Tokens and cost are NOT captured**: Cline does not expose `input_tokens` / `output_tokens` / `total_cost` in `model_usage` entries (only `ts`, `model_id`, `mode`)
- Real tokens/cost are stored in the editor's `state.vscdb` extension storage (~10 GB on Cursor) — extraction deferred to V1.1+
- Active time uses `wall_clock × 0.5` heuristic when UI message timestamps are missing

**Tests**: `tests/test_proxy_collector_usage_metadata.py`

---

### `cursor`

**Source**: `~/.cursor/ai-tracking/ai-code-tracking.db`

**What we extract** (current limited scope):
- Scored commits with `composerLinesAdded` / `humanLinesAdded` (V2)
- Block count, file count, extensions touched
- Safe `raw_meta.usage` quality metadata on AI block events:
  - `tokens=unknown` because `ai_code_hashes` does not expose native token counters;
  - `cost=unknown` because the tracking database does not expose spend;
  - `active_time=defensible` only as a documented marker/proxy, not a precise work timer.

**Confidence**: typically 40-60% (Indicative)

**How it's enabled**: **DISABLED by default V1** (deferred V1.1)
- To enable: `config/privacy.yaml` → `sources.cursor: enabled`

**Limitations**:
- Full Cursor composer/state extraction remains deferred because it is large and
  expensive to scan on active users.
- Token / cost extraction is not claimed from the lightweight AI tracking DB.
- Scored commit events are code-output provenance signals, not LLM usage events.

**Roadmap**: V1.1 plans for incremental indexing or subset captures to make this collector scalable

**Tests**: `tests/test_proxy_collector_usage_metadata.py`

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
- No provider-native token or cost truth in the current parser

**Confidence**: partial. Useful for aggregate conversation history, not for
invoice-grade token/cost truth.

**How it's enabled**: enabled by default — just drop files in `~/ship1000x/drop/`

**Workflow**:
```bash
# 1. Export from claude.ai / chatgpt.com / gemini.google.com
# 2. Drop the ZIP/JSON in ~/ship1000x/drop/
# 3. ship1000x ingest — automatically scans and processes
```

**Privacy notes**: `web_exports` reads ONLY metadata (titles, timestamps, message counts, wordcounts). The original export files remain untouched in the drop folder.

**Tests**: TBD

---

### `anthropic_usage` (Anthropic Admin API — billing-side validation)

**Source**: Anthropic Admin API endpoint `/v1/organizations/usage_report`

**What we extract**:
- Daily aggregated token counts per workspace and model
- Daily aggregated cost per workspace
- Auth mode (API key vs OAuth Pro/Max/Teams)
- Safe `raw_meta.usage` quality metadata:
  - `tokens=factual` from the Admin Usage API;
  - `cost=factual` when `cost_report` is available, otherwise `unknown`;
  - `active_time=unknown` because billing snapshots do not prove local activity.

**Confidence**: billing-side factual for returned API/export buckets. It is not
local activity truth and not a universal invoice certificate.

**How it's enabled**: opt-in, requires `ANTHROPIC_ADMIN_KEY` env var

**Limitations**:
- Requires an Admin API key (`sk-ant-admin-...`), not a regular API key
- Available only for Build (Pay-as-you-go) and Scale (Enterprise) plans, not Pro/Max personal
- Claude Code via Max subscription is NOT counted here (max is OAuth-based, separate billing)

**Use case**: cross-validate SHIP's local API-equivalent measurements against
Anthropic billing-side exports via `ship1000x reconcile`.

**Tests**: `tests/test_billing_usage_metadata.py`

---

### `openai_usage` (OpenAI usage API)

**Source**: OpenAI usage API endpoint

**What we extract**: same structure as `anthropic_usage` (daily billing-side
aggregates), with safe `raw_meta.usage` metadata for provider, model, cached
input tokens, pricing provenance, and quality labels.

**Confidence**: billing-side factual for returned API/export buckets. It is not
local activity truth and not a universal invoice certificate.

**How it's enabled**: opt-in, requires OpenAI API key

**Tests**: `tests/test_billing_usage_metadata.py`

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

1. **Per-source score** in `ship1000x insights` — weighted average of confidence flags
2. **Raw global score** — weighted average across source scores
3. **Audit trail** for cross-validation against provider billing-side exports
   (via `ship1000x reconcile`)

See [`TRUST_SCORE.md`](TRUST_SCORE.md) for the scoring methodology.
