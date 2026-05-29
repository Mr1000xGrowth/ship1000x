"""Dry-run planning for `ship1000x reclassify`.

The real reclassify command resets ingestion offsets, deletes events in a
window, re-runs collectors, then rebuilds rollups. This module produces the
operator-facing impact plan without mutating the local database or reading any
raw provider/source files.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ship1000x.core.cost_truth import api_equivalent_cost_from_row
from ship1000x.core.history_recoverability import build_history_recoverability_report
from ship1000x.core.source_inventory import RECLASSIFY_COLLECTORS


def _is_enabled(sources_enabled: dict[str, str], key: str) -> bool:
    return sources_enabled.get(key, "enabled") == "enabled"


def build_reclassify_plan(
    storage,
    *,
    since: str,
    cutoff: datetime,
    sources_enabled: dict[str, str] | None = None,
    window_days: int,
) -> dict[str, Any]:
    """Build a safe dry-run plan for `reclassify`.

    The returned shape intentionally contains counts, source names, collector
    names, and guidance only. It does not include raw metadata, paths, prompts,
    commands, diffs, provider payloads, or source file names.
    """
    source_config = sources_enabled or {}
    cutoff_iso = cutoff.isoformat()
    cutoff_date = cutoff.date().isoformat()

    event_rows = storage.query(
        """SELECT source, cost_estimated, raw_meta
             FROM events
             WHERE started_at >= ?
             ORDER BY source ASC""",
        (cutoff_iso,),
    )
    rollup_rows = storage.query(
        """SELECT COUNT(*) AS n
             FROM daily_rollup
             WHERE date >= ?""",
        (cutoff_date,),
    )
    ingestion_rows = storage.query("SELECT COUNT(*) AS n FROM ingestion_state")
    history = build_history_recoverability_report(storage, window_days=window_days)
    history_by_source = {row["source"]: row for row in history["rows"]}
    history_summary = history["summary"]

    collectors = [
        {
            "collector": collector,
            "source_key": source_key,
            "enabled": _is_enabled(source_config, source_key),
        }
        for collector, source_key in RECLASSIFY_COLLECTORS
    ]

    by_source: dict[str, dict[str, Any]] = {}
    for row in event_rows:
        source = row["source"] or "unknown"
        bucket = by_source.setdefault(source, {"events": 0, "api_equivalent_cost": 0.0})
        bucket["events"] += 1
        bucket["api_equivalent_cost"] += api_equivalent_cost_from_row(row)

    source_rows = [
        {
            "source": source,
            "events_to_delete": int(bucket["events"] or 0),
            "api_equivalent_cost_usd": round(float(bucket["api_equivalent_cost"] or 0.0), 6),
            # Backward-compatible alias for existing JSON consumers. The value is
            # API-equivalent operator cost, not invoice-grade paid cost.
            "cost_estimated_usd": round(float(bucket["api_equivalent_cost"] or 0.0), 6),
            "recoverability": history_by_source.get(source, {}).get(
                "recoverability",
                "not-recoverable-from-ship",
            ),
            "repair_scope": history_by_source.get(source, {}).get(
                "repair_scope",
                "visible-evidence-only",
            ),
            "claim_boundary": history_by_source.get(source, {}).get(
                "claim_boundary",
                (
                    "Do not claim repair/backfill from SHIP alone; this source is visible "
                    "evidence until a governed profile and fixture-backed collector contract exist."
                ),
            ),
            "unrecoverable_truth_fields": history_by_source.get(source, {}).get(
                "unrecoverable_truth_fields",
                ["registered source profile", "fixture-backed collector contract"],
            ),
            "repair_recommendation": history_by_source.get(
                source,
                {},
            ).get(
                "recommendation",
                "Treat as visible evidence only; add a profile before making public claims.",
            ),
        }
        for source, bucket in sorted(
            by_source.items(),
            key=lambda item: (-int(item[1]["events"] or 0), item[0]),
        )
    ]
    history_warnings = []
    if history_summary["partial_sources"]:
        history_warnings.append(
            f"{history_summary['partial_sources']} source(s) are only partially recoverable."
        )
    if history_summary["external_source_required_sources"]:
        history_warnings.append(
            f"{history_summary['external_source_required_sources']} source(s) require approved provider exports/API access."
        )
    if history_summary["not_recoverable_from_ship_sources"]:
        history_warnings.append(
            f"{history_summary['not_recoverable_from_ship_sources']} source(s) are not recoverable from SHIP semantics alone."
        )
    if history_summary["malformed_meta_events"]:
        history_warnings.append(
            f"{history_summary['malformed_meta_events']} event(s) have malformed metadata."
        )

    return {
        "schema_version": "ship1000x.reclassify_plan.v1",
        "dry_run": True,
        "since": since,
        "cutoff": cutoff_iso,
        "impact": {
            "ingestion_offsets_to_reset": int(ingestion_rows[0]["n"] if ingestion_rows else 0),
            "events_to_delete": sum(row["events_to_delete"] for row in source_rows),
            "daily_rollup_rows_to_rebuild": int(rollup_rows[0]["n"] if rollup_rows else 0),
            "enabled_collectors": len([row for row in collectors if row["enabled"]]),
            "disabled_collectors": len([row for row in collectors if not row["enabled"]]),
        },
        "sources": source_rows,
        "collectors": collectors,
        "history_recoverability_summary": history_summary,
        "history_recoverability_warnings": history_warnings,
        "warnings": [
            "Dry-run only: no ingestion offsets, events, rollups, or source files were changed.",
            "Run history-audit first when recoverability is uncertain.",
            "Run without --dry-run only after confirming original local source files or approved provider exports still exist.",
        ]
        + history_warnings,
    }
