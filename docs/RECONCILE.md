# SHIP cost reconciliation

`ship1000x reconcile` compares the API-equivalent cost that SHIP local collectors compute
from JSONL/SQLite traces (`claude_code`, `codex`, `codex_macapp`,
`codex_desktop`) against the cost that billing-side adapters report from
official provider Admin/Usage APIs (`anthropic_usage`, `openai_usage`).

It is the first SHIP tool that answers the simple operator question
"does the local API-equivalent number reconcile with provider billing-side exports?" without
requiring spreadsheet work or eyeballing two dashboards.

## Why this exists

Local collectors are heuristic in places:

- model identity is extracted from log shape (Codex parses
  `base_instructions.text`, Claude Code reads `message.model`), and falls back
  to a default when no pattern matches;
- pricing is a local table that can drift from current provider pricing;
- token breakdown shape differs by collector: `input_tokens` can mean
  uncached input for some sources and provider-reported total input for others,
  while cache/reasoning counters are separate diagnostic fields when exposed;
- OAuth Claude Pro/Max/Teams seats consume billing-side credit that does not
  appear in local JSONL at all.

Billing snapshots are external validation rows, not local activity truth:

- `anthropic_usage` is built from the Anthropic Admin Usage Report API;
- `openai_usage` is built from the OpenAI Usage API.

When the two diverge persistently for the same day, either SHIP is wrong
somewhere in pricing, model detection, or token accounting, or the compared
surfaces do not represent the same billing route. `reconcile` makes that gap
explicit instead of letting it hide in averages.

## What it does NOT do

- It does not call provider APIs. It only reads SHIP's local store.
- It does not modify the store.
- It does not expose prompts, responses, paths, diffs, or raw payloads.
  The Markdown report contains only aggregates and model labels.
- It does not "fix" any cost. It only flags discrepancies for human triage.

## Quick start

```bash
# Default: claude_code vs anthropic_usage, codex vs openai_usage,
# 30 day window, 10% warning threshold, Markdown to stdout.
ship1000x reconcile

# Custom window and threshold:
ship1000x reconcile --since 7d --threshold-pct 5.0

# Save report to a file (safe to share with a privacy-aware reviewer):
ship1000x reconcile --output ./reports/reconcile.md

# Custom pair, e.g. an experimental local source vs a billing source:
ship1000x reconcile --pair my_local_collector:anthropic_usage
```

## What the report contains

For each `local` vs `billing` pair:

1. **Totals**: local API-equivalent total, billing-side total, delta absolute and percent, plus
   counts of `ok`, `warn`, `missing_billing`, `missing_local` days.
2. **Daily breakdown**: one row per calendar day with both costs, delta, and
   status (`ok` / `warn` / `missing_billing` / `missing_local`).
3. **Local-source quality issues**:
   - non-factual cost share (events where `usage.quality.cost != "factual"`);
   - model fallbacks detected (`default`, `all-models`, `unknown`, `mixed`,
     `fallback`, empty), with event count and cost.
4. **Local token breakdown** when available:
   - collector-reported input tokens;
   - output tokens;
   - cached input tokens;
   - cache write tokens;
   - reasoning tokens.

The token breakdown is a diagnostic aid for cache/reasoning-driven gaps, not
invoice truth. Provider billing-side rows remain the billing comparator.

## How to read the daily statuses

| Status | Meaning |
|---|---|
| `ok` | Cost gap within the threshold, or both sides at zero. |
| `warn` | Cost gap above the threshold. Investigate. |
| `missing_billing` | Local data exists but no billing snapshot for that day. Either the billing collector is not configured, or the day is too recent for the provider to have settled. |
| `missing_local` | Billing snapshot exists but SHIP did not capture local activity. The user may have used Claude Code via OAuth seat or Codex via Desktop on another machine; check coverage. |

## How to triage a `warn` day

1. Look at the per-day delta sign. Positive (local > billing) usually means
   SHIP is double-counting (often cache tokens) or applying an inflated
   per-token price. Negative (local < billing) usually means SHIP is missing
   a session, a workspace, or an OAuth seat.
2. Check the model-fallback table for that pair. If `default` or `gpt-5`
   buckets carry a large share of the cost, the model-detection layer is
   probably hiding the real model.
3. Check the non-factual cost share. A high share (> 20%) means most of the
   local cost is estimated, not factual, and the comparison itself is fuzzy.

`reconcile` does not propose fixes. The follow-up belongs in dedicated lots:
pricing/aliases hardening, cache-token semantics, model-detection robustness.

## Pairs covered by default

| Local source | Billing source | Provider |
|---|---|---|
| `claude_code` | `anthropic_usage` | Anthropic |
| `codex` | `openai_usage` | OpenAI |

Codex Desktop / Codex macOS app are not added by default because they share
the OpenAI billing surface with `codex` and would double-count if combined
naively. Use explicit `--pair codex_macapp:openai_usage` to inspect them
separately.

## Safety boundary

`reconcile` is a read-only audit. It does not:

- write events to the local store;
- read provider APIs;
- launch a proxy;
- expose prompts, responses, paths, diffs, or raw payloads.

The Markdown report is safe to share with a privacy-aware reviewer for
triage. Avoid pasting it into public channels because it still contains
your local cost totals.
