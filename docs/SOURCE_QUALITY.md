# Source quality audit

Ship1000x is useful only if it says clearly which numbers are measured, which
are modeled, and which are missing. This document turns an internal
observability audit's findings into a Ship1000x-facing contract.

## Audit inputs

This contract is derived from an internal observability gap-and-benchmark
review (gap matrix, open-source benchmark, build-readiness map).

The important conclusions for Ship1000x are:

- source presence is not enough;
- token/cost fields must distinguish input, output, cache, reasoning, pricing
  source, pricing version, model raw, and model canonical;
- Codex CLI, Codex Desktop, and Codex macOS app are not equally reliable;
- billing/API sources can validate cost but do not prove local active time;
- dashboard claims must follow source quality, not the other way around.

## Command

```bash
ship1000x source-audit --since 30d
ship1000x source-audit --since 30d --json
ship1000x source-audit --since 30d --strict-public
```

The command reads only the local Ship1000x SQLite store that the user already
owns. It does not read new Codex, Claude, Cursor, shell, Git, or system logs.

The JSON output includes:

- observed and absent source counts;
- missing normalized usage metadata;
- risk state per source;
- quality scores for tokens, cost, active time, and overall source truth;
- pricing versions, clients, and canonical models observed through safe usage
  metadata;
- auth-mode counts (`oauth`, `api_key`, `unknown`, or collector-specific
  routes);
- cost-truth split: API-equivalent estimated cost, billed-estimated cost, and
  subscription-absorbed cost when collectors provide those fields.

## Risk states

| Risk | Meaning |
|---|---|
| `ok` | Observed source matches the quality contract and usage metadata is present when expected. |
| `partial` | Source is useful but has unknown/indicative token or cost quality. |
| `fragile` | Source is observed but missing normalized usage metadata that the registry expects. |
| `unknown-source` | Source is observed but not registered in the quality contract. |
| `absent` | Known source is not observed in the selected window. |

## Quality score

The score is not a productivity score. It is a truthfulness score for the
numbers SHIP is about to show.

| Quality | Weight |
|---|---:|
| `factual` | 1.00 |
| `defensible` | 0.80 |
| `indicative` | 0.45 |
| `unknown` | 0.00 |

Tokens, cost, and active time are scored separately when the dimension applies
to a source. `quality_scores.overall` is the average of those applicable
dimensions.

| Band | Score |
|---|---|
| `high` | `>= 90` |
| `medium` | `>= 70` |
| `partial` | `>= 40` |
| `low` | `< 40` |
| `not_measured` | no applicable quality dimension |

A low score means "quote with caveats or fix the collector first", not "the
developer did poor work".

The dashboard overview uses the same `/api/source-quality` report to render the
`Measurement quality` scorecard. There is no separate dashboard-only truth: if
the source audit changes, the public view changes with it.

## Public gate

Use `--strict-public` before a public demo, README claim, investor screenshot,
or dashboard review. It exits non-zero when:

- the average observed source quality score is below `70`;
- an observed source is `fragile`;
- an observed source is `unknown-source`.

For custom automation, use:

```bash
ship1000x source-audit --since 30d --fail-under 80
ship1000x source-audit --since 30d --fail-on-risk fragile --fail-on-risk partial
```

The gate does not read new logs. It only evaluates the already-stored quality
audit report.

`ship1000x public-check` is the one-command version for demos and reports. It
combines the same strict source-quality gate with `observation-audit` so the
operator sees both blocking source failures and non-blocking planned/deferred
observation gaps. It also includes the read-only history recoverability summary
so public reports can disclose whether historical rows are recoverable,
partial, external-source-dependent, or not repairable from SHIP semantics
alone.

Use `ship1000x public-check --strict-history` when the public claim depends on
repairing or defending historical rows. In that mode, partial recoverability,
external-source-required history, not-recoverable sources, and malformed
metadata become blocking failures instead of caveats.

`public-check --json` also includes `public_claim_readiness`. This add-on does
not change the stored data or source-quality schema; it translates the audits
into explicit public claim boundaries:

- `allowed` claims are safe to quote with their caveats;
- `conditional` claims can be quoted only with the named preconditions and
  source caveats;
- `blocked` claims must stay visible as caveats until fixtures, source quality,
  or billing reconciliation catch up.

In particular, a green source gate does **not** mean complete provider coverage,
invoice-grade cost truth, or live runtime truth. Those claims remain blocked
unless the relevant audits say otherwise. `public_claim_readiness` includes the
aggregate `unknown_billing_basis_usd` and event count in the blocked
invoice-grade claim evidence so legacy/incomplete costs remain visible.
It also blocks provider-policy/auth-route compliance claims: SHIP can expose
local `auth_mode` evidence, but it does not certify provider terms,
subscription eligibility, proxy policy, or quota compliance.
That blocked claim now includes `auth_mode_events`,
`missing_or_unknown_auth_mode_events`, and `nonstandard_auth_modes`, so an
operator can see whether the blocker is merely missing policy snapshots or also
weak local route evidence.
It also reports `provider_policy_snapshots=X/Y` and
`invalid_provider_policy_snapshots=N` plus
`missing_provider_policy_snapshots=N`. These are aggregate local evidence counts
only: SHIP does not fetch provider terms, validate policy text, certify
subscription eligibility, or bless proxy routing.
`source-audit --json` exposes the same semantics as explicit counters:
`provider_policy_snapshot_events`, `invalid_provider_policy_snapshot_events`,
`missing_provider_policy_snapshot_events`, and
`provider_policy_snapshot_coverage_pct`. The denominator is normalized
`usage_events`, not all events, so rows without usage metadata remain a
separate source-quality problem.
Collectors that have an owner-approved policy review can attach a minimal
`usage.provider_policy_snapshot` via `build_usage_metadata`; the helper keeps
only short categorical fields such as provider, checked date, status/risk label,
allowed-usage claim, and policy URL.
`source-audit --json` also exposes these aggregate totals under
`summary.cost_truth` and summary policy-snapshot counters, so public claim and
dashboard surfaces do not need to recompute them from individual source rows.
The human `public-check` output prints claim evidence before caveats, so blocked
claims show their aggregate reason without requiring JSON inspection.
`public-check` also emits a `public_proof_sequence` block in JSON and prints
the same sequence in human output. It names the exact safe commands to rerun
(`source-audit`, `observation-audit`, `history-audit`, and `public-check`),
their pass/fail/caveat status, aggregate evidence, and quote policy. This keeps
public demos from treating a green command as permission to quote blocked
coverage, billing, live-runtime, or policy-compliance claims.
The sequence uses `caveat` when the gate is usable but still contains visible
coverage gaps, conditional history, or blocked public claims.
The human `public-check` header mirrors that distinction: `PASS` means no
proof caveats, `CAVEAT` means usable with visible caveats, and `NO GO` means
blocking failures.
The JSON report also exposes top-level `proof_status` and
`presentation_status` fields with the same `pass` / `caveat` / `fail`
semantics. The older `passed` boolean remains an exit-code/gate compatibility
field and can be true while `proof_status` is `caveat`.

When `public-check` includes `history-audit`, `public_claim_readiness` also
names whether historical repair/backfill is `allowed`, `conditional`, or
`blocked`. Partial recoverability or required provider exports make the claim
conditional; malformed metadata or not-recoverable sources block it. The same
section exposes `history_repair_risks`, a source-name-only list of the rows
that make historical repair conditional or blocked. Each history risk includes
a `repair_scope`, `claim_boundary`, and `unrecoverable_truth_fields` list so a
partial source cannot be mistaken for full token/cost/billing repair.
`history-audit --json` exposes the same strict gate boundary directly:
per-source rows include `strict_history_blocking` and
`strict_history_blocking_reasons`, the summary includes
`strict_history_blocking_sources`, and the report includes
`strict_history_failures`. `public-check --strict-history` uses those failures
instead of recomputing a separate history policy, so the proof gate and the
history audit speak the same language.
`reclassify --dry-run` also carries recoverability per source and warning
summaries, so the operator sees repair risk before resetting offsets or
deleting/rebuilding rows.

The cost-truth split prevents a common dashboard mistake: API-equivalent token
cost can be useful for value comparison, while billed-estimated cost is SHIP's
local route estimate for the per-token share that may be billed.
OAuth/subscription activity can legitimately have high API-equivalent cost and zero
billed-estimated cost; both numbers must stay labelled. Legacy or incomplete
rows are surfaced as `unknown_billing_basis_usd` instead of being silently folded
into either side of the split.
`ship1000x audit-cost` is an API-equivalent drill-down: it recomputes stored
local estimates from tokens and pricing, but it is not invoice-grade billed
cost reconciliation.
Cost quality also depends on pricing provenance. Native token counters can make
token usage factual, but if the model only resolves through default/fallback
pricing, SHIP downgrades cost quality to `indicative` instead of presenting the
computed dollar amount as factual.
Operator-facing CLI tables and Markdown exports should label summed
`cost_estimated` values as API-equivalent cost, not generic cost or invoice
truth.

## Registry scope

The registry lives in `ship1000x.core.source_quality`.

It covers:

- Claude Code;
- Codex CLI;
- Codex Desktop;
- Codex macOS app;
- Cursor;
- Cursor Agent;
- Cline;
- Gemini CLI;
- GitHub Copilot / Copilot Chat;
- OpenCode;
- Roo/Kilo Code variants;
- OpenClaw;
- Anthropic usage API;
- OpenAI usage API;
- web exports;
- Git;
- shell;
- Mac system.

The registry is intentionally conservative. If a source is incomplete, the
audit should say so. Public reports and dashboard views should never promote a
`fragile`, `partial`, or `unknown-source` source as audit-grade.

`source-audit --json` includes `collector_stage` on every row and
`summary.collector_stage_counts` for the observed window. The stage answers a
different question than risk: not just "how good is the token/cost data?", but
"is SHIP really collecting this source through a public-proof path?" Current
stages include:

- `default_ingest`: wired into normal local ingest.
- `opt_in_ingest`: real collector, but disabled unless the user opts in.
- `billing_api`: official usage/billing import, not local activity truth.
- `drop_import`: opt-in local drop importer written by another SHIP or
  local usage-proxy component.
- `metadata_enrichment`: enriches or sidecars metadata, not token/cost truth.
- `collector_module_only`: parser/collector code exists but the public ingest
  path is not proofed.
- `fixture_only`: synthetic fixture parser only; no real local files read.
- `registry_only`: known profile only; no parser/collector.
- `unknown`: observed source not in the quality registry.

`collector_module_only`, `fixture_only`, and `registry_only` are
public-untrusted stages. `source-audit --strict-public` and `public-check` fail
when an observed row belongs to one of them, even if the row has normalized
`raw_meta.usage`.

Gemini CLI, Copilot agents, OpenCode, Cursor Agent, Continue, and Aider are
fixture-only in this release. These rows are useful for fixture contract
development, but must not be marketed as live local coverage. Roo/Kilo Code is
wired into normal ingest with separate `roo_kilo_code` identity; its source
quality remains partial until token/cost truth is validated.

## Relationship to the dashboard

The dashboard is a public showcase, but it must not be the source of truth.

Correct order:

1. harden source collectors and fixtures;
2. expose source quality through `source-audit`;
3. update dashboard labels, warnings, and panels from that report.

Dashboard and audit cost labels must keep billing semantics visible. The
overview and project dashboard use `API-equivalent cost` for the pay-per-token
estimate, while `source-audit --json`, `/api/highlights`, and `/api/projects`
expose or consume the additive `cost_truth` split:

- `api_equivalent_usd`: what the same usage would cost on catalog API pricing;
- `billed_estimated_usd`: conservative estimate of the per-token share likely
  billed by the observed route, not invoice truth;
- `subscription_absorbed_usd`: API-equivalent usage likely absorbed by an
  OAuth/subscription route;
- `unknown_billing_basis_usd`: legacy or incomplete rows whose billing route
  is not known;
- `native_token_pricing_usd`: subset of API-equivalent cost backed by native
  token/pricing evidence and used for dashboard coverage labels;
- `events_with_cost_truth`: number of rows whose normalized usage metadata,
  explicit cost block, or auth mode lets SHIP classify billing semantics;
- `events_with_unknown_billing_basis`: number of rows whose billing route is
  unknown and must not be hidden behind a polished total.
- `presentation_status` / `presentation_label`: dashboard-ready cost wording
  that says whether API-equivalent cost is clean, subscription-absorbed,
  unknown-basis, or absent. These labels are intentionally explicit that the
  dashboard number is not provider invoice truth.

For backward compatibility, dashboard JSON still includes `cost_total` and
project `total_cost`. These aliases are API-equivalent totals, not paid-cost
truth, and are accompanied by `cost_total_basis` / `total_cost_basis` set to
`api_equivalent_legacy_alias`.

Project rows also expose per-project `cost_truth`,
`total_billed_estimated_cost`, `total_subscription_absorbed_cost`,
`unknown_billing_basis_cost`, and `events_with_unknown_billing_basis` so a
project dashboard or downstream importer does not have to infer paid-cost semantics
from the legacy API-equivalent aliases.

The dashboard API contract is documented in
[`DASHBOARD_API.md`](DASHBOARD_API.md). `/api/highlights` returns
`schema_version = ship1000x.dashboard.highlights.v1`; each `/api/projects` row
returns `schema_version = ship1000x.dashboard.project.v1`.

This prevents a polished UI from hiding incomplete or misleading measurements.
