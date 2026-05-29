# Dashboard API contract

The local dashboard exposes read-only JSON endpoints for browser views and
downstream local importers. These endpoints read only the local
SHIP store. They do not call provider APIs and do not expose prompts,
responses, diffs, raw payloads, credential markers, or local file paths.

## `/api/highlights`

Schema: `ship1000x.dashboard.highlights.v1`

This endpoint returns one aggregate object for the selected window.

Stable fields for downstream consumers:

- `schema_version`: contract id for the response shape.
- `window_days`: selected lookback window.
- `cost_api_equivalent`: pay-per-token catalog-equivalent cost.
- `cost_total`: legacy display alias for `cost_api_equivalent`.
- `cost_total_basis`: always `api_equivalent_legacy_alias`.
- `cost_truth`: additive cost semantics block.

`cost_truth` fields:

- `api_equivalent_usd`: what the same usage would cost on catalog API pricing.
- `billed_estimated_usd`: local estimate of per-token billed share, not invoice
  truth.
- `subscription_absorbed_usd`: API-equivalent usage likely absorbed by an OAuth
  or subscription route.
- `unknown_billing_basis_usd`: legacy or incomplete rows whose billing route is
  unknown.
- `native_token_pricing_usd`: subset backed by native token/pricing evidence.
- `events_with_cost_truth`: rows with enough metadata to classify billing
  semantics.
- `events_with_unknown_billing_basis`: rows whose billing route is unknown.
- `presentation_status`: machine-readable cost presentation state.
- `presentation_label`: human wording that keeps invoice-truth caveats visible.

Consumers must treat `cost_total` as a compatibility alias only. Use
`cost_truth` for any paid-vs-equivalent interpretation.

## `/api/projects`

Schema per row: `ship1000x.dashboard.project.v1`

This endpoint returns a list of project aggregate rows. It remains a list for
backward compatibility.

Stable cost fields per project row:

- `schema_version`: contract id for the project row shape.
- `total_api_equivalent_cost`: pay-per-token catalog-equivalent cost.
- `total_cost`: legacy display alias for `total_api_equivalent_cost`.
- `total_cost_basis`: always `api_equivalent_legacy_alias`.
- `total_billed_estimated_cost`: project billed-estimated share, not invoice
  truth.
- `total_subscription_absorbed_cost`: project subscription-absorbed share.
- `unknown_billing_basis_cost`: project unknown-billing-basis share.
- `events_with_unknown_billing_basis`: project event count whose billing route
  is unknown.
- `cost_truth`: same additive semantics as `/api/highlights`, scoped to the
  project.

Consumers must not infer paid cost from `total_cost` or
`total_api_equivalent_cost`. Paid-cost interpretation must read
`cost_truth.presentation_status`, `total_billed_estimated_cost`, and
`unknown_billing_basis_cost`.

## `/api/trend`

Schema per row: `ship1000x.dashboard.trend_point.v1`

This endpoint returns a list of daily activity points for the selected window.
It remains a list for backward compatibility.

Stable fields per trend row:

- `schema_version`: contract id for the trend point shape.
- `date`: local daily bucket.
- `active_hours`: unified active-time hours for the day.
- `wall_hours`: wall-clock hours for the day.

Consumers must treat this as activity-time telemetry, not proof of full working
time or complete device presence.

## `/api/trust`

Schema: `ship1000x.dashboard.trust.v1`

Schema per source row: `ship1000x.dashboard.trust_source.v1`

This endpoint returns the dashboard Trust Score object and per-source score
rows for the selected window.

Stable fields:

- `schema_version`: contract id for the response shape.
- `global`: Trust Score aggregate object.
- `per_source`: source score rows.

Stable fields per source row:

- `schema_version`: contract id for the per-source row shape.
- `source`: normalized SHIP source id.
- `score`: numeric source score.
- `event_count`: count of source events in the selected window.
- `label`: score label.

Consumers must not treat Trust Score as provider invoice truth, token truth, or
complete source coverage. It is a measurement-quality signal.

## `/api/source-quality`

Schema: `ship1000x.source_quality_report.v1`

This endpoint returns the same source-quality audit shape used by
`ship1000x source-audit --json`, scoped to the selected dashboard window.

Stable top-level fields:

- `schema_version`: contract id for the source-quality report shape.
- `summary`: aggregate measurement-quality summary.
- `rows`: per-source source-quality rows.

Stable policy-evidence fields in `summary`:

- `provider_policy_snapshot_events`: usage rows with a minimal valid local
  policy-snapshot marker.
- `invalid_provider_policy_snapshot_events`: usage rows carrying a malformed
  local policy-snapshot marker.
- `missing_provider_policy_snapshot_events`: usage rows with no local
  policy-snapshot marker.
- `provider_policy_snapshot_coverage_pct`: valid snapshot rows divided by
  normalized `usage_events`.

Consumers should use this endpoint to decide which dashboard numbers need
visible caveats before quoting them publicly or importing them downstream.
