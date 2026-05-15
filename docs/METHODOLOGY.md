# Methodology — How Ship1000x computes what it measures

Reference document explaining **the heuristics in use**, **the methodological choices**, and **the assumed limitations** of each measurement.

Ship1000x aims to be **rigorous and methodologically defensible** rather than aggressively optimistic. All approximations are documented in the open.

Last updated: 2026-05-15 (V1).

---

## Contents

1. [Active time — unified cross-source calculation](#1-active-time--unified-cross-source-calculation)
2. [Personal P95 threshold (adaptive cadence)](#2-personal-p95-threshold-adaptive-cadence)
3. [Cost — official pricing + cache tokens](#3-cost--official-pricing--cache-tokens)
4. [Lines of code — V2 breakdown (real / seed / vendored / generated)](#4-lines-of-code--v2-breakdown)
5. [AI-augmented output multiplier](#5-ai-augmented-output-multiplier)
6. [Privacy filter (sanitize_event)](#6-privacy-filter-sanitize_event)
7. [Explicit methodological choices](#7-explicit-methodological-choices)

---

## 1. Active time — unified cross-source calculation

### The problem

If you run Claude Code + Codex + Cursor in parallel from 10:00 to 10:05, the naive count records **3 × 5 min = 15 min** of active time (one row per source in `daily_rollup`). But reality is **5 min**: one person, one presence.

### V1 solution: `core/intervals.py` module

4-step pipeline:

```
1. _fetch_event_timelines_for_day()
   Fetches all human events (typed/approval/paste = codes 0/1/2) stored in
   events.raw_meta.event_timeline for the given day and machine.

2. merge_human_events_cross_sources()
   Merges by timestamp, sorts chronologically, deduplicates events seen in
   parallel by 2+ sources (tolerance ±2 sec).

3. compute_active_sec_with_threshold(sorted_ts, threshold_sec)
   Sums consecutive intervals ≤ threshold = continuous focus.
   Intervals > threshold = real breaks, excluded.

4. compute_unified_metrics(storage, day, user_email, machine_id)
   Computes 5 modes in one pass:
     - active_sec_strict   (5min hardcoded threshold)
     - active_sec_p95      (threshold = user's personal P95)
     - active_sec_loose    (15min threshold)
     - active_sec_unified  (canonical alias = active_sec_p95)
     - agent_sec_estimated = wall_clock - active_sec_unified
     - wall_clock_sec      = first_event → last_event cross-sources
```

Persisted in `daily_unified` (1 row per date+machine). All consumers (CLI, dashboard, S3 push, exports) read the same unified value.

### Validation

Measured on 60 days of real data on the development machine:

| Metric | Value |
|---|---|
| Raw per-source sum (`daily_rollup`) | 394h |
| Unified sum (`daily_unified`) | 138h |
| **Measured overcount ratio** | **×2.85** |

The multi-agent bug was therefore **real and significant**.

### Assumed limitations

- **No cross-machine merge**: if a user has 2 Macs and works on both in parallel, each machine has its own row. Not a common case for V1.
- **The ±2s dedup tolerance can miss pathological cases** where 2 sources observe the same prompt with > 2s offset. Measured impact: negligible in internal tests.
- **No exact agent-vs-human deconvolution**: `agent_sec_estimated` includes undetected human breaks (bathroom, coffee, distraction). This is a documented best effort.

---

## 2. Personal P95 threshold (adaptive cadence)

### The problem with a fixed threshold

Active time computation cuts intervals > threshold (= "real breaks"). Picking an **arbitrary** threshold (e.g. 5 min) creates 2 biases:

- **For a calm 9-to-5 dev**: threshold too wide, counts coffee breaks as active
- **For a multi-agent power user**: threshold too strict, cuts legitimate thinking intervals

### V1 solution: AUTO P95 personal threshold

`core/cadence.py` computes — over the user's last 14 days — the full distribution of inter-event human intervals, and exposes percentiles P50/P75/P90/P95/P99.

The applied AUTO threshold = **P95** = 95% of the user's intervals fall below this value. The remaining 5% = real breaks.

→ The threshold automatically adapts to each user's rhythm.

### Examples of measured profiles

| Profile | P50 | P95 | Interpretation |
|---|---|---|---|
| Classic 9-to-5 dev | ~1.5 min | ~6 min | regular rhythm, narrow threshold |
| Multi-agent power user | ~2.7 min | **~21 min** | long thinking gaps between prompts, wide threshold |
| Spread-out sessions (devops, reading) | ~5 min | ~45 min | many natural breaks |

### New-user fallback

If the user has < 100 intervals in their history (early days), fallback on `THRESHOLD_STRICT_SEC = 5 min` (conservative). Auto-recalibration once the sample reaches 100.

### Override

```bash
# See YOUR personal profile
tracker calibrate

# See the 5 modes side by side (strict / auto P95 / loose / agent / wall-clock)
tracker today --compare-modes
```

### Assumed limitations

- **Interval variability**: a user who radically changes their rhythm (vacation, new project) will see their P95 take 14 days to recalibrate.
- **Self-fulfilling effect**: if the user often has long agentic sessions, P95 increases, which validates these long sessions. This is not a bug, it's the design — we adapt to actual behavior.

---

## 3. Cost — official pricing + cache tokens

### The calculation

For sources that expose tokens (claude_code, codex, openclaw, anthropic_usage, openai_usage, web_exports):

```python
cost = (
    input_tokens * model_pricing[model]["input_per_m"] / 1_000_000
    + output_tokens * model_pricing[model]["output_per_m"] / 1_000_000
    + cache_read_tokens * model_pricing[model]["cache_read_per_m"] / 1_000_000
    + cache_creation_tokens * model_pricing[model]["cache_create_per_m"] / 1_000_000
)
```

Pricing maintained in `core/pricing.py`, source of truth = published official Anthropic / OpenAI / Google rates.

### Claude Code bug fixed in V1

Before V1, two major bugs drastically underestimated cost:

1. **×2.49 overcount**: Claude Code emits multiple `assistant` records per message (SSE chunks). Without dedup by `message.id`, output tokens and turns were multiplied.
2. **Cache tokens ignored**: `cache_read_input_tokens` and `cache_creation_input_tokens` not captured. On a tested 4MB session: **99,197,525 real cache tokens vs 27,213 captured** (0.03% captured before patch).

Patches `fix(claude_code) 2343626` correct these 2 bugs. See [migration cookbook](../README.md) to propagate the fix to historical data (`tracker reclassify --since 365d`).

### Cross-source validation

For reliability auditing, run the reconciliation POC:

```bash
export ANTHROPIC_ADMIN_KEY="sk-ant-admin-..."
python ship1000x-reconciliation/reconcile.py --days 30
```

It compares what Ship1000x measured locally with what the Anthropic Admin API reports officially (invoice side). Verdict in % gap per day.

### Sources with INDICATIVE cost (heuristic)

`codex_macapp` and `codex_desktop` do not store tokens in their local SQLite. Cost computed via heuristic:

```
cost_estimated = turn_count * average_cost_per_turn[model]
```

Documented as **Indicative** in `tracker insights`. For numerical validation, cross-reference with `openai_usage` (official API).

---

## 4. Lines of code — V2 breakdown

### The problem

`git log --numstat` returns raw `lines_added`. But this includes:

- `node_modules/` (npm install) → vendored
- `package-lock.json` (lockfiles) → generated
- Massive initial project commits → seed
- The **real manually-written code** → real

Without distinction, the "lines_per_hour" (productivity) metric is inflated by vendored and lockfiles.

### V2 solution: `core/line_classifier.py`

At git ingestion time, each commit is analyzed file by file:

```
generated : matches patterns (lockfiles, builds, caches)
            or .gitattributes "linguist-generated=true"
vendored  : matches patterns (node_modules/, vendor/, third_party/)
            or .gitattributes "linguist-vendored=true"
seed      : initial commit of a repo, or massive commit (>5000 lines / 50 files)
            or message matching /^(init|initial|scaffold|import|...)/
real      : everything else — the "real" code work
```

Stored in `events.raw_meta`:
- `lines_added` (raw sum, retro-compat)
- `lines_real_added`, `lines_real_deleted`
- `lines_seed_added`, `lines_seed_deleted`
- `lines_vendored_added`, `lines_vendored_deleted`
- `lines_generated_added`, `lines_generated_deleted`

### Productivity ratios based on `real`

`insights/engine.py` computes **2 versions** of each productivity ratio:

| Ratio | Real-based (defensible) | Raw-based (retro-compat) |
|---|---|---|
| `lines_per_hour` | `real_added / active_hours` | `lines_per_hour_raw = lines_added / active_hours` |
| `lines_per_typed` | `real_added / typed_msgs` | `lines_per_typed_raw` |
| `cost_per_line_net` | `cost / max(1, real_net)` | `cost_per_line_net_raw` |

The "real" ratio is the metric defensible against an audit. The "raw" version serves the dashboard for measuring total output (useful for tracking raw AI + human cadence).

### 30-day validation

| Category | Lines | % of raw |
|---|---|---|
| Total raw | 656 K | 100% |
| **Real (real code)** | **607 K** | **92.4%** |
| Generated (lockfiles) | 35 K | 5.4% |
| Seed (boilerplate) | 14 K | 2.1% |
| Vendored | 0 | 0% |

Bias of raw ratios: **+8%** in favor of raw (productivity overvalued). Modest but real.

---

## 5. AI-augmented output multiplier

### The calculation

`insights/multiplier.py` computes the ratio "real production / agentic cost" to measure the AI leverage:

```python
multiplier = {
    "lines_per_hour": real_added / active_hours,         # real productivity
    "tokens_per_hour": tokens_total / active_hours,      # agent cadence
    "cost_per_hour": cost / active_hours,                # financial pace
    "presence_multiplier": wall_clock / active_humain,   # ratio total time / pure human
    # With optional context TJM + produced value:
    "equivalent_value_eur": value_produit_eur,
    "ratio_value_per_cost": value_produit_eur / cost_eur,
}
```

### `presence_multiplier` ≠ productivity_multiplier

Important distinction (decided 2026-05-15):

- **`presence_multiplier`** = `wall_clock_sec / active_sec_unified` = temporal ratio "how long the AI agent works for 1h of human steering". This is a **utilization ratio**, **NOT a productivity ratio**.

- To measure **real productivity**: ratio output (real lines, delivered features) / total cost.

→ Never present `presence_multiplier` as an "output multiplier". It is an indicator of **temporal leverage**.

### Assumed limitations

- `equivalent_value_eur` is manually entered by the user (TJM + produced value): **subjective**, to use for calibrating your agentic vs traditional TJM, not for absolute claims to a client.
- Cross-user comparisons: impossible without normalizing TJMs and the nature of the work.

---

## 6. Privacy filter (sanitize_event)

See [PRIVACY.md](PRIVACY.md) for details. Summary:

- **No content** (prompt, file, diff, response, stdout) ever touches the DB.
- Strict whitelist: 46 keys allowed in `raw_meta`. Everything else is filtered out.
- Recursive path anonymization (`/Users/<name>/...` → `~/...`).
- Central guardrail in `core/storage.upsert_event`: `sanitize_event` always called, even if the collector forgot it.
- Cloud push: conservative `share_config` filter by default (email hashed, financials excluded, project_ids hashed).

---

## 7. Explicit methodological choices

### What we CHOSE NOT to do

| Choice | Reason |
|---|---|
| No 50%/25% interval weighting | Approximate heritage retired 2026-04-25. Binary YES/NO on P95 threshold = defensible and auditable |
| No implicit `max_session_hours` cap | You can work 14h, we capture 14h. Configurable if you want, but default = 0 (no cap) |
| No `ai_generated` category on commits | Every agentic commit is partially AI. Separate category has no clear meaning. V1.1 alternative: detect `Co-Authored-By` trailer |
| No automatic cross-machine merge | Rare case in V1. If needed: manual merge dashboard-side |
| No token estimation for Cline / Cursor / Codex Desktop | Data in `state.vscdb` or not exposed. Effort vs marginal gain for V1 |

### What we DISPLAY transparently

In every `tracker today` or `tracker insights` report:

1. The **active threshold applied** (e.g. "auto P95 = 21.6 min")
2. The **mode used** (strict / auto / loose / wall-clock)
3. Any **fallback** (if cadence profile not yet computed, warning message)
4. The **confidence level** per source (Factual / Defensible / Indicative) — V1.1

### Why this rigor

Ship1000x is designed to be **audit-ready against a demanding client**. The methodology must hold up against a CFO or an internal audit. That's why we:

- Document all heuristics (this document)
- Expose raw data for cross-check (`daily_rollup` stays intact alongside `daily_unified`)
- Allow official validation (`anthropic_usage` Admin API + reconcile.py)
- Display confidence levels per metric
- Provide the `--compare-modes` mode to visualize the 5 calculations side by side

See also:
- [COVERAGE.md](COVERAGE.md): sources × confidence level matrix
- [PRIVACY.md](PRIVACY.md): what is never stored
- [TRUST_SCORE.md](TRUST_SCORE.md): how the confidence score is computed per module
