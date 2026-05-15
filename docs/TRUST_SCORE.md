# Trust Score — How Ship1000x quantifies its own reliability

Reference document explaining **how the trust score is computed per module**, **how to display it in reports**, and **how to act on a low score**.

The Trust Score is Ship1000x's main differentiator: rather than presenting all metrics as equally reliable, every measurement carries its own confidence label. This is what makes Ship1000x **audit-ready** rather than just another dashboard.

Last updated: 2026-05-15 (V1).

---

## Why a Trust Score

A typical productivity tracker reports a single number ("you worked 8h12 today, 78% AI-augmented"). The user has no way to know:

- How was that 8h12 measured? (wall-clock? active intervals? unified across sources?)
- How accurate is that 78%? (is it factual, or extrapolated?)
- Could it be wrong by ±5%? ±50%?

Ship1000x answers these questions explicitly, per metric, in every report.

---

## The 4 levels (recap from COVERAGE.md)

| Score range | Label | Meaning |
|---|---|---|
| 90-100 | **Factual** | Direct measurement of native data. Audit-ready. |
| 70-89 | **Defensible** | Modeled heuristic with explicit, verifiable assumptions. |
| 40-69 | **Indicative** | Extrapolation from indirect proxies. Order of magnitude only. |
| 0-39 | **Not trackable** | Data structurally inaccessible. Suggest manual import or accept the gap. |

---

## How the score is computed per source

Each collector exposes a `confidence_flag` (high / medium / low) at the event level, and a `global_score` (0-100) at the source level.

### Per-event confidence flag

Set by the collector at ingestion time:

```python
event = {
    ...
    "confidence_flag": "high" | "medium" | "low",
    ...
}
```

Decision rules (per collector):

| Collector | "high" when | "medium" when | "low" when |
|---|---|---|---|
| `claude_code` | tokens captured + cache_tokens captured + msg.id deduped | partial JSONL or older format | parse error / missing fields |
| `codex` | SQLite reads cleanly + tokens > 0 | tokens=0 (heuristic cost) | DB locked / corrupt |
| `git_multi` | line classifier classified all files | partial classification (unknown extensions) | git error / shallow clone |
| `anthropic_usage` | response 200 + valid JSON + complete period | response 200 + partial period (cap ~31 days) | API error / missing key |
| `web_exports` | format recognized + structured | format heuristic (HTML scrape) | unknown format |
| `mac_system` | pmset returned data | pmset partial | pmset failed |

### Per-source `global_score`

A weighted average of confidence flags over the last N events:

```python
def compute_source_score(storage, source: str, window_days: int = 30) -> int:
    """Returns 0-100 score for a source over the window."""
    events = storage.query("""
        SELECT confidence_flag, COUNT(*) AS n
        FROM events
        WHERE source = ?
          AND date(started_at) >= date('now', ? || ' days')
        GROUP BY confidence_flag
    """, (source, f"-{window_days}"))
    
    weights = {"high": 100, "medium": 70, "low": 40}
    total_n = sum(e["n"] for e in events)
    if total_n == 0:
        return 0  # No data → no score
    weighted = sum(weights[e["confidence_flag"]] * e["n"] for e in events)
    return weighted // total_n
```

### Per-metric score

For derived metrics (ratios, multipliers), the score is the **minimum** of the input source scores (weakest link):

```python
score(lines_per_hour) = min(
    score(active_time),    # depends on which sources captured human events
    score(git_real_added), # depends on git_multi quality
)
```

→ A ratio is never more reliable than its weakest input.

---

## Composite global score

The global Ship1000x score for a user is a weighted aggregation:

```
global_score = (
    score(claude_code)        * weight_by_event_count
    + score(codex)            * weight_by_event_count
    + score(git_multi)        * weight_by_event_count
    + ... 
    + bonus(reconciliation_passed)        # +5 if Anthropic Admin API matches ±15%
    + bonus(cadence_calibrated)           # +3 if user has run `tracker calibrate`
    + bonus(daily_unified_populated)      # +5 if cross-source merge active
    - penalty(missing_critical_source)    # -10 if claude_code or git missing
)
```

Capped at 100.

### Interpretation

| Global score | Interpretation | What to do |
|---|---|---|
| 95-100 | Audit-ready against a demanding client | Ship insights, present to stakeholders |
| 80-94 | Defensible for internal use | Use with caveats noted |
| 60-79 | Useful for personal trends | Don't use for absolute claims |
| < 60 | Calibration needed | Run `tracker calibrate`, check missing sources, run reconciliation |

---

## How it appears in reports

### `tracker insights` (V1.1)

```
Insights — Last 30 days
═══════════════════════════════════════════════════════════════════════
                              Value          Confidence
─────────────────────────────────────────────────────────────────────
Active hours (unified)        138.3h         ████████░░  Defensible (82)
  ↳ source breakdown:
    claude_code               118.4h         ██████████  Factual (95)
    codex                      14.2h         ████████░░  Defensible (78)
    codex_macapp                5.7h         ██████░░░░  Defensible (70)

Tokens (total)               2,341 M         ██████████  Factual (98)
Cost                         $1,247          ██████████  Factual (97)
Cost vs Anthropic invoice    ±3.2%           ██████████  Reconciled

Lines real (defensible)      607 K           ██████████  Factual (95)
Lines per hour (real)        2,350           ████████░░  Defensible (85)
                                              [min(active=82, git=95)]

GLOBAL SCORE                                  ████████░░  87 / 100
```

### `tracker today --compare-modes`

Already implemented. Shows the 5 active time modes side by side. Each mode carries its own implicit confidence:

- `strict`: highest confidence (no heuristics)
- `auto P95`: defensible (explicit cadence-based)
- `loose`: indicative (loose threshold)
- `agent_estimated`: indicative (best-effort wall - unified)
- `wall_clock`: factual (raw first→last event)

### `tracker calibrate` output

Already implemented. Shows the user's cadence profile + classification. The calibration itself contributes to the global score (`bonus(cadence_calibrated) = +3`).

---

## What lowers your score (and how to fix)

| Symptom | Cause | Fix |
|---|---|---|
| `claude_code` score < 90 | Cache tokens missing in old events (pre-V1 patch) | `tracker reclassify --since 365d` |
| `git` score < 80 | Many files unclassified by line_classifier | Review `~/.config/ship1000x/line_classifier.yaml` patterns |
| `codex_macapp` score = 70 | Cost via heuristic (tokens=0 by design) | Cross-check with `openai_usage` for validation |
| Reconciliation = ±20% gap | Workspaces missing or pricing outdated | Check `core/pricing.py` is current; check Admin API key has access to all workspaces |
| `cadence_calibrated` bonus missing | Profile not yet computed | `tracker calibrate` |
| `daily_unified_populated` missing | Rollups not rebuilt post-V1 | `tracker rollup --since 60d` |

---

## Why this matters strategically

Most productivity trackers ship a single confidence-less number. Ship1000x ships:

1. **A number** + 
2. **Its confidence band** + 
3. **The exact heuristic that produced it** + 
4. **A way to cross-check it** (reconcile with official invoice) + 
5. **A way to improve it** (calibrate, reclassify, rollup)

This is the difference between a **demo metric** and an **audit-ready metric**. It's also what makes Ship1000x quotable in B2B contexts where claims must be defensible.

---

## Roadmap

| Version | Trust Score features |
|---|---|
| V1 (this) | `confidence_flag` populated by all collectors + `daily_unified` confidence stored + reconcile.py POC |
| V1.1 | `tracker insights` displays score per metric + global composite score |
| V1.2 | Per-metric explanation (`tracker explain lines_per_hour`) shows the heuristic used + sample data |
| V2 | Score history over time (regression tracking) + automatic alerting on score drops |

---

See also:
- [COVERAGE.md](COVERAGE.md): per-source matrix with confidence levels
- [METHODOLOGY.md](METHODOLOGY.md): heuristics behind each score
- [PRIVACY.md](PRIVACY.md): what data backs the scoring (no content, only metadata)
