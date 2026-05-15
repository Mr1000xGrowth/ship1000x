# Coverage — Tracked sources and confidence levels

Reference document explaining **what Ship1000x measures**, **with what precision**, and **what is intentionally out of scope**.

Last updated: 2026-05-15 (V1).

---

## TL;DR

```
Sources with FACTUAL measurement      :  7  ─ high reliability, audit-ready
Sources with DEFENSIBLE measurement   :  3  ─ documented heuristics
Sources with INDICATIVE measurement   :  2  ─ ballpark only, interpret with care
Sources collectible V1 (sprint 4)     :  3  ─ Antigravity / Gemini / Continue
Sources deferred V1.1                 :  2  ─ Cursor (state.vscdb) / Aider
Sources NOT TRACKABLE (architecture)  : ~7-10 tools (web/cloud only)

→ Ship1000x V1 covers ~50% of mainstream AI dev tools.
→ With drop folder manual import: ~65%.
```

---

## 4 confidence levels

| Level | Definition | When to use in a report |
|---|---|---|
| **Factual** | Direct measurement of native data (API tokens, git lines, real timestamps). Typical accuracy 95-100%. | Always. This is the reference. |
| **Defensible** | Modeled heuristic (e.g. active time via P95 threshold). Relies on explicit, verifiable assumptions. Typical accuracy 70-85%. | When native data does not expose the requested metric. Document in the report legend. |
| **Indicative** | Extrapolation from indirect proxies (e.g. cost based on turn_count when tokens are absent). Order of magnitude. Typical accuracy 40-60%. | For relative comparisons (trends), not for absolute claims. |
| **Not trackable** | Data structurally inaccessible from the local machine (pure web, proprietary SaaS without export). | Documented as such. Suggestion: drop folder manual import. |

→ Ship1000x displays the confidence level per metric in `tracker insights` (V1.1, sprint 3 of the roadmap).

---

## V1 sources matrix

### Active sources (read by `tracker ingest --source all`)

| Collector | Physical source | Tokens | Cost | Active time | Lines | Global level |
|---|---|---|---|---|---|---|
| `claude_code` | `~/.claude/projects/*.jsonl` | ✅ Factual (post-dedup msg.id + cache_read/cache_creation) | ✅ Factual (official Anthropic pricing) | ✅ Defensible (inter-event human intervals ≤ P95) | n/a | **Factual ~95%** |
| `codex` | `~/.codex/codex.db` SQLite | ✅ Factual | ✅ Factual | ✅ Defensible | n/a | **Factual ~85%** |
| `codex_macapp` | Codex MacApp SSE logs | ⚠️ Indicative (tokens often 0) | ⚠️ Defensible (heuristic turn_count + model) | ✅ Defensible | n/a | **Defensible ~70%** |
| `codex_desktop` | `~/.codex/state_5.sqlite` (threads + tools) | ❌ Indicative (tokens=0 by design — not exposed) | ⚠️ Indicative (heuristic turn_count) | ✅ Defensible | n/a | **Indicative ~50%** |
| `cline` | `~/Library/.../tasks/*/task_metadata.json` | ❌ Indicative (model_usage only exposes ts/model/mode) | ❌ Indicative (cost in state.vscdb not read) | ✅ Defensible (UI message timestamps) | n/a | **Defensible ~60%** |
| `openclaw` | local SQLite (lobster way 🦞) | ✅ Factual | ✅ Factual | ✅ Defensible | n/a | **Factual ~90%** |
| `git_multi` | `git log` + `core.line_classifier` | n/a | n/a | n/a | ✅ Factual (real / seed / vendored / generated breakdown V2) | **Factual ~95%** |
| `shell` | shell history (zsh/bash) | n/a | n/a | ⚠️ Indicative (timestamps approximate) | n/a | **Indicative ~50%** |
| `mac_system` | macOS focus apps (pmset) | n/a | n/a | ⚠️ Indicative | n/a | **Indicative ~60%** |
| `web_exports` | drop folder `~/ship1000x/imports/` | ✅ Factual (depends on export) | ✅ Factual | ⚠️ Defensible | n/a | **Factual ~95%** |
| `anthropic_usage` | Anthropic Admin API | ✅ Factual (official invoice validation) | ✅ Factual | n/a | n/a | **Factual 100%** |
| `openai_usage` | OpenAI API | ✅ Factual | ✅ Factual | n/a | n/a | **Factual 100%** |

### Sources disabled by default in V1 (advanced opt-in via `privacy.yaml`)

| Collector | Reason | How to enable |
|---|---|---|
| `cursor` | `state.vscdb` ~10 GB, parsing composer bubbles = ~1 day of dev for marginal gain. Deferred to V1.1. | `privacy.yaml: sources.cursor: enabled` |

### Sources planned for V1 (sprint 4 — not yet shipped)

| Collector | Physical source | Effort | Status |
|---|---|---|---|
| `antigravity` | `~/Library/Application Support/Antigravity/User/globalStorage/state.vscdb` | ~4h (fork structure of cursor.py) | Planned V1 |
| `gemini_usage` | Google Cloud Billing API | ~6h | Planned V1 |
| `continue_dev` | `~/.continue/sessions/*.json` | ~6h | Planned V1 |

### Sources deferred to V1.1+

| Tool | Why deferred |
|---|---|
| Cursor (full state.vscdb parsing) | 10 GB parsing cost. Solution: incremental pre-index or subset captures |
| Cline (tokens/cost) | Data lives in `state.vscdb` extension storage of VS Code/Cursor. Extraction = 1-2 days |
| Aider | Simple history format (`~/.aider.chat.history.md`), trivial but low usage |
| Codex Desktop tokens (logs_2.sqlite OTEL) | Fragile parsing (string spans), marginal gain |

---

## AI dev tools NOT TRACKABLE by architecture

These tools store **nothing locally** (or only ephemeral UI state). To track them, the only path = **drop folder manual import** from the user's export.

| Tool | Reason | Alternative |
|---|---|---|
| ChatGPT web | Everything server-side (OpenAI) | Export ChatGPT → drop folder |
| Claude.ai web | Everything server-side (Anthropic) | Export Claude → drop folder |
| Gemini web | Everything server-side (Google) | Export Google → drop folder |
| Manus | Web only, no public API | None |
| Perplexity (no API) | Web only | Manual export if available |
| Devin | Proprietary cloud SaaS | None (V1.1 if SaaS API published) |
| v0.dev | Web only | None |
| Bolt.new | Web only | None |
| Lovable | Web only | None |
| Windsurf (cloud mode) | Proprietary cloud SaaS | Enable local mode or installed IDE |

---

## Honest global percentages

Out of the ~25 most-used AI dev tools in 2026 (top by adoption):

```
Tracked Factual V1                  : 7 sources, ~28%
Tracked Defensible/Indicative V1    : 5 sources, ~20%
Trackable V1 (sprint 4)             : 3 sources, ~12%
Trackable V1.1                      : 2 sources, ~8%
Not trackable (except manual import): ~7-10 tools, ~35%

→ Ship1000x V1 = ~50% coverage (Factual + Defensible + Indicative)
→ Ship1000x V1 + drop folder = ~65%
→ Ship1000x V1.1 target = ~70-75%
```

---

## How to verify precision for YOUR setup

### Cross-check validation

```bash
# 1. See what Ship1000x measured
tracker insights --since 7d

# 2. Compare with the official Anthropic invoice
export ANTHROPIC_ADMIN_KEY="sk-ant-admin-..."
python ship1000x-reconciliation/reconcile.py --days 7

# 3. See the 5 modes of active time computation (full transparency)
tracker today --compare-modes

# 4. Calibrate your personal cadence profile (P95 adapted to YOUR rhythm)
tracker calibrate
```

### Understanding gaps

If your reconciliation verdict shows a gap > 15% with the Anthropic invoice:

1. Check that all Anthropic workspaces are scanned (multi-key?)
2. Check that Claude Code sessions via Max subscription are not counted in Admin API (they don't appear there)
3. Run `tracker reclassify --since 365d` to propagate V1 fixes to historical data

See also:
- [METHODOLOGY.md](METHODOLOGY.md): detailed explanation of heuristics
- [PRIVACY.md](PRIVACY.md): what is never stored
- [TRUST_SCORE.md](TRUST_SCORE.md): how the confidence score is computed per module

---

## Planned evolution

| Version | Coverage | Notes |
|---|---|---|
| V1.0 (this) | ~50% (12 sources) | Hardening patches batch 1+2+3 (privacy + cost + multi-agent) |
| V1.0 + drop folder | ~65% | + ChatGPT/Claude/Gemini manual exports |
| V1.1 (sprint 4) | ~70-75% | + Antigravity + Gemini + Continue + cursor parsing |
| V1.2 | ~80%+ | + Aider + Cline tokens + Codex Desktop OTEL parsing |

Public roadmap: `~/ship1000x-roadmap.md` (internal for now, public at sprint 5).
