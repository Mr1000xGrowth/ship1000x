# Coverage — Tracked sources and confidence levels

Reference document explaining **what Ship1000x measures**, **with what precision**, and **what is intentionally out of scope**.

Last updated: 2026-05-23 (V1 source truth hardening).

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

→ Ship1000x displays confidence per metric and exposes source/capability gaps
through `ship1000x source-audit` and `ship1000x observation-audit`.

---

## V1 sources matrix

### Production sources (default or opt-in ingest)

| Collector | Physical source | Tokens | Cost | Active time | Lines | Global level |
|---|---|---|---|---|---|---|
| `claude_code` | `~/.claude/projects/*.jsonl` | ✅ Factual (post-dedup msg.id + cache_read/cache_creation) | ✅ API-equivalent factual when native tokens + pricing snapshot are present | ✅ Defensible (inter-event human intervals ≤ P95) | n/a | **Factual ~95%** |
| `codex` | `~/.codex/sessions/**/rollout-*.jsonl` | ✅ Factual when `total_token_usage` exists, otherwise Unknown | ✅ Factual when token-based, otherwise Unknown | ✅ Defensible | n/a | **Factual ~85%** |
| `codex_macapp` | Codex MacApp application logs | ❌ Unknown (not exposed) | ⚠️ Indicative (hourly active-time heuristic) | ✅ Defensible | n/a | **Defensible ~70%** |
| `codex_desktop` | `~/.codex/state_5.sqlite` SSE logs | ❌ Unknown (not exposed) | ⚠️ Indicative (hourly active-time heuristic) | ✅ Defensible | n/a | **Indicative ~50%** |
| `cline` | `~/Library/.../tasks/*/task_metadata.json` | ❌ Unknown (not exposed by task metadata) | ❌ Unknown (not exposed by task metadata) | ✅ Defensible (UI message timestamps) | n/a | **Defensible ~60%** |
| `roo_kilo_code` | Roo/Kilo Code extension task metadata | ❌ Unknown (not exposed by task metadata) | ❌ Unknown (not exposed by task metadata) | ✅ Defensible (UI message timestamps) | n/a | **Partial / Defensible** |
| `openclaw` | local SQLite (lobster way 🦞) | ✅ Factual | ✅ Factual | ✅ Defensible | n/a | **Factual ~90%** |
| `git_multi` | `git log` + `core.line_classifier` | n/a | n/a | n/a | ✅ Factual (real / seed / vendored / generated breakdown V2) | **Factual ~95%** |
| `shell` | shell history (zsh/bash) | n/a | n/a | ⚠️ Indicative (timestamps approximate) | n/a | **Indicative ~50%** |
| `mac_system` | macOS focus apps (pmset) | n/a | n/a | ⚠️ Indicative | n/a | **Indicative ~60%** |
| `web_exports` / `web_export` events | drop folder `~/ship1000x/imports/` | ❌ Unknown (current parser stores aggregate metadata only) | ❌ Unknown | n/a | n/a | **Partial / aggregate history** |
| `anthropic_usage` | Anthropic Admin API | ✅ Billing-side factual for returned buckets | ✅ Billing-side factual when cost report is available | n/a | n/a | **Provider-export validation only** |
| `openai_usage` | OpenAI API | ✅ Billing-side factual for returned buckets | ✅ Billing-side factual when costs are available | n/a | n/a | **Provider-export validation only** |
| `claude_statusline` | `~/.ship1000x/drop/statusline/` | ⚠️ Defensible context-window metadata | ⚠️ Indicative | ✅ Defensible | n/a | **Partial / opt-in drop import** |
| `agent_runtime` | `~/.ship1000x/drop/watch/` | n/a | n/a | ⚠️ Indicative runtime presence | n/a | **Partial / opt-in drop import** |
| `trace` | `~/.ship1000x/drop/trace/` | ✅ Factual when the proxy records usage | ⚠️ Defensible; reconcile with billing | n/a | n/a | **Partial / opt-in drop import** |

### Sources disabled by default in V1 (advanced opt-in via `privacy.yaml`)

| Collector | Reason | How to enable |
|---|---|---|
| `cursor` | Lightweight AI block metadata is supported, but token/cost claims remain unknown; full composer parsing is deferred. | `privacy.yaml: sources.cursor: enabled` |
| `claude_statusline` | Requires an explicit local statusline/drop writer consent path. | Explicit `ship1000x ingest --source claude_statusline` or `privacy.yaml: sources.claude_statusline: enabled` |
| `agent_runtime` | Reads opt-in foreground daemon drops only; not scheduled by default. | Explicit `ship1000x ingest --source agent_runtime` or `privacy.yaml: sources.agent_runtime: enabled` |
| `trace` | Reads opt-in local usage-proxy drops only; never starts or configures the proxy. | Explicit `ship1000x ingest --source trace` or `privacy.yaml: sources.trace: enabled` |

### Sources planned for V1 (sprint 4 — not yet shipped)

| Collector | Physical source | Effort | Status |
|---|---|---|---|
| `gemini_cli` | TBD local Gemini CLI session/usage metadata | Unknown until real format validation | Fixture-only parser; collector not promoted |
| `copilot_agents` | TBD Copilot/Copilot Chat local metadata | Unknown until real format validation | Fixture-only parser; collector not promoted |
| `opencode` | TBD OpenCode session metadata | Unknown until real format validation | Fixture-only parser; collector not promoted |
| `cursor_agent` | TBD Cursor Agent-specific local metadata | Unknown until real format validation | Fixture-only parser; collector not promoted |
| `antigravity` | `~/Library/Application Support/Antigravity/User/globalStorage/state.vscdb` | ~4h (fork structure of cursor.py) | Planned V1 |
| `gemini_usage` | Google Cloud Billing API | ~6h | Planned V1 |
| `continue_dev` | `~/.continue/sessions/*.json` | ~6h | Planned V1 |

`gemini_cli`, `opencode`, and `cursor_agent` have fixture-only parsers in
`ship1000x.collectors`. They are not wired to `ingest` and do not read real
logs; they lock the safe metadata contract that a future collector must
satisfy. Roo/Kilo Code has moved out of this bucket: `ship1000x ingest
--source roo_kilo_code` is routed, but token/cost truth remains partial.

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
ship1000x insights --since 7d

# 2. Compare with provider billing-side exports
export ANTHROPIC_ADMIN_KEY="sk-ant-admin-..."
ship1000x reconcile --since 7d

# 3. See the 5 modes of active time computation (full transparency)
ship1000x today --compare-modes

# 4. Calibrate your personal cadence profile (P95 adapted to YOUR rhythm)
ship1000x calibrate
```

### Understanding gaps

If your reconciliation verdict shows a gap > 15% against a provider billing-side export:

1. Check that all Anthropic workspaces are scanned (multi-key?)
2. Check that Claude Code sessions via Max subscription are not counted in Admin API (they don't appear there)
3. Run `ship1000x history-audit --since 365d` to see which historical rows are recoverable, partial, external-source-dependent, or not repairable from SHIP alone.
4. Run `ship1000x reclassify --since 365d --dry-run` to preview offsets, events, rollups, collectors, and source counts before mutation.
5. Run `ship1000x reclassify --since 365d` only after confirming the original local source files still exist.

See also:
- [METHODOLOGY.md](METHODOLOGY.md): detailed explanation of heuristics
- [PRIVACY.md](PRIVACY.md): what is never stored
- [TRUST_SCORE.md](TRUST_SCORE.md): how the confidence score is computed per module

### Usage quality metadata

AI collectors may add a safe `raw_meta.usage` object. It is metadata-only and
does not contain prompt, response, command, diff, or file content.

The important quality labels are:

- `factual`: native source counter or provider billing-side export for that dimension;
- `defensible`: documented interval/cadence model;
- `indicative`: heuristic useful for trend comparison, not absolute claims;
- `unknown`: source does not expose the value.

Codex therefore has three distinct reliability modes:

- Codex CLI rollout JSONL can be token-factual when `total_token_usage` exists.
- Codex MacApp can be active-time defensible, but token-unknown and
  cost-indicative.
- Codex Desktop can be active-time defensible, but token-unknown and
  cost-indicative.

Cline and Cursor are intentionally more conservative: current local metadata can
be useful for activity/source coverage, but token and cost quality are recorded
as `unknown` until a fixture-backed, scalable source exposes those values.

### Source quality audit

An internal observability review showed that source coverage
is not enough: a source can be present but still incomplete, heuristic, or
semantically weak. Ship1000x now exposes that distinction with:

```bash
ship1000x source-audit --since 30d
ship1000x source-audit --since 30d --json
```

The audit compares already-stored events with a source quality registry. It
flags:

- observed sources that are not in the registry;
- LLM/API sources missing normalized `raw_meta.usage`;
- expected vs observed token/cost/active-time quality;
- source-specific caveats and next actions.

This is the quality gate that should feed dashboard improvements. The
dashboard should never claim strong confidence for a source that `source-audit`
marks `fragile`, `partial`, or `unknown-source`.

### Observation capability audit

`source-audit` answers "can I trust the sources already observed in my local
database?". `observation-audit` answers the broader product question:
"which observation capabilities exist, which are partial, and which belong
to a future capability?"

```bash
ship1000x observation-audit --since 30d
ship1000x observation-audit --since 30d --json
```

The audit is deliberately metadata-only. It returns capability statuses such as
`implemented`, `partial`, `planned`, and `deferred` for batch/local usage,
provider coverage, live runtime, OS context, API truth, and cross-system
correlation. It never prints raw prompts, responses, local paths, diffs, logs,
or provider payload rows.

The same report now includes a provider capability gap registry. It names
providers and tools that appeared in the open-source audit but are not yet
fixture-backed SHIP sources, including Gemini CLI, GitHub Copilot / Copilot
Chat, OpenCode, Roo/Kilo Code variants, Cursor Agent, Continue, Aider,
Antigravity, Goose, Crush, Kimi, and Qwen. The registry is not a collector. It
is claim hygiene: public dashboards and reports should show these gaps instead
of implying complete provider coverage.

---

## Planned evolution

| Version | Coverage | Notes |
|---|---|---|
| V1.0 (this) | ~50% (12 sources) | Hardening patches batch 1+2+3 (privacy + cost + multi-agent) |
| V1.0 + drop folder | ~65% | + ChatGPT/Claude/Gemini manual exports |
| V1.1 (sprint 4) | ~70-75% | + Antigravity + Gemini + Continue + cursor parsing |
| V1.2 | ~80%+ | + Aider + Cline tokens + Codex Desktop OTEL parsing |

Public roadmap: `~/ship1000x-roadmap.md` (internal for now, public at sprint 5).
