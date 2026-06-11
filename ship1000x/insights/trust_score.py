"""Trust Score computation: per-source and global composite confidence.

See docs/TRUST_SCORE.md for the full methodology.

Each event carries a `confidence_flag` (high/medium/low) set by its collector.
The per-source score is a weighted average over the window. The global
composite weights each source by its API-equivalent cost (not raw event count)
so cost-free, high-volume sources like git can't dominate a score that backs
cost/value claims; it falls back to event weighting when no cost is observed.
`score_by_events` keeps the volume-weighted view for coverage. Robustness checks
report setup gaps separately, including source quality audit gaps.

The Trust Score is Ship1000x's main differentiator: every metric exposes
its own confidence rather than presenting all numbers as equally reliable.
"""

from __future__ import annotations

from ship1000x.core.storage import Storage

CONFIDENCE_WEIGHTS = {"high": 100, "medium": 70, "low": 40}

# Sources considered critical: their absence triggers a penalty in global score.
CRITICAL_SOURCES = {"claude_code", "git"}


def compute_source_score(
    storage: Storage, source: str, window_days: int = 30,
) -> int:
    """Returns 0-100 score for one source over the window.

    Weighted average of CONFIDENCE_WEIGHTS over event count. Returns 0 if
    no event found (source absent or muted).
    """
    with storage.conn() as conn:
        rows = conn.execute(
            """
            SELECT confidence_flag, COUNT(*) AS n
            FROM events
            WHERE source = ?
              AND date(started_at) >= date('now', ? || ' days')
            GROUP BY confidence_flag
            """,
            (source, f"-{window_days}"),
        ).fetchall()
    total_n = sum(r["n"] for r in rows)
    if total_n == 0:
        return 0
    weighted = sum(
        CONFIDENCE_WEIGHTS.get(r["confidence_flag"], 0) * r["n"]
        for r in rows
    )
    return weighted // total_n


def get_all_source_scores(
    storage: Storage, window_days: int = 30,
) -> dict[str, dict]:
    """Returns {source: {score, event_count, cost}} for sources active in window.

    ``cost`` is the summed API-equivalent ``cost_estimated`` over the window —
    used as the global weighting basis so a source that emits many $0 events
    (git commits) cannot dominate a score meant to back cost/value claims.
    """
    with storage.conn() as conn:
        rows = conn.execute(
            """
            SELECT source, COUNT(*) AS n, COALESCE(SUM(cost_estimated), 0) AS cost
            FROM events
            WHERE date(started_at) >= date('now', ? || ' days')
            GROUP BY source
            ORDER BY n DESC
            """,
            (f"-{window_days}",),
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        out[r["source"]] = {
            "score": compute_source_score(storage, r["source"], window_days),
            "event_count": r["n"],
            "cost": max(float(r["cost"] or 0.0), 0.0),
        }
    return out


def compute_global_score(
    storage: Storage,
    window_days: int = 30,
    user_email: str | None = None,
) -> dict:
    """Composite global score = raw weighted average per-source.

    Returns {score, label, breakdown, robustness_checks}.

    The score is the weighted average of per-source confidence (0-100). It is
    NOT inflated by additive bonuses and NOT silently capped — what you see
    is the raw quality of the underlying data.

    `robustness_checks` are independent qualitative signals about the
    measurement setup (cadence calibration, cross-source unified merge,
    critical sources presence, source quality audit). They do NOT alter the
    score; they tell the reader whether the setup is robust enough to trust the
    score.
    """
    source_scores = get_all_source_scores(storage, window_days)
    if not source_scores:
        return {
            "score": 0,
            "label": "No data",
            "breakdown": {},
            "robustness_checks": [],
        }

    # Headline weights each source by its API-equivalent cost, not raw event
    # volume: otherwise a source emitting thousands of $0 events (git commits)
    # dominates a score meant to back cost/value claims. Fall back to event
    # weighting when no cost is observed yet (fresh setup, cost-free sources),
    # so the score stays meaningful before any priced usage lands.
    total_events = sum(s["event_count"] for s in source_scores.values())
    total_cost = sum(s["cost"] for s in source_scores.values())
    score_by_events = (
        sum(s["score"] * s["event_count"] for s in source_scores.values())
        // total_events
        if total_events
        else 0
    )
    if total_cost > 0:
        weighting = "cost"
        score = round(
            sum(s["score"] * s["cost"] for s in source_scores.values()) / total_cost
        )
    else:
        weighting = "event_count"
        score = score_by_events
    label, _ = get_score_label(score)

    checks: list[dict] = []

    # Cadence calibrated (P95 personal threshold computed from real data)
    cadence_passed = False
    cadence_detail = "Run `ship1000x calibrate` to compute personal P95 threshold"
    if user_email:
        from ship1000x.core.cadence import get_cadence_profile
        prof = get_cadence_profile(storage, user_email)
        if prof and prof.get("sample_size", 0) >= 100:
            cadence_passed = True
            p95_min = (prof.get("p95") or 0) / 60
            cadence_detail = f"P95 = {p95_min:.1f} min (sample {prof['sample_size']})"
    checks.append({
        "name": "Cadence calibrated",
        "passed": cadence_passed,
        "detail": cadence_detail,
    })

    # Cross-source unified merge populated (anti multi-agent overcount)
    with storage.conn() as conn:
        n_unified = conn.execute(
            "SELECT COUNT(*) AS n FROM daily_unified WHERE date >= date('now', ? || ' days')",
            (f"-{window_days}",),
        ).fetchone()["n"]
    checks.append({
        "name": "Cross-source unified",
        "passed": n_unified > 0,
        "detail": (
            f"{n_unified} daily rows merged"
            if n_unified > 0
            else "Run `ship1000x rollup --since 60d` to populate daily_unified"
        ),
    })

    # Critical sources present (claude_code + git)
    missing_critical = sorted(s for s in CRITICAL_SOURCES if s not in source_scores)
    checks.append({
        "name": "Critical sources present",
        "passed": len(missing_critical) == 0,
        "detail": (
            f"All present: {', '.join(sorted(CRITICAL_SOURCES))}"
            if not missing_critical
            else f"Missing: {', '.join(missing_critical)}"
        ),
    })

    # Source quality audit: confidence_flag alone cannot distinguish "measured"
    # from "metadata present but tokens/cost unknown".
    from ship1000x.core.source_quality import build_source_quality_report

    quality = build_source_quality_report(storage, window_days=window_days)
    quality_summary = quality["summary"]
    fragile = int(quality_summary["fragile_sources"])
    partial = int(quality_summary["partial_sources"])
    unknown = int(quality_summary["observed_unknown_sources"])
    missing_usage = int(quality_summary["sources_missing_usage_metadata"])
    quality_passed = (fragile + partial + unknown + missing_usage) == 0
    checks.append({
        "name": "Source quality audit",
        "passed": quality_passed,
        "detail": (
            "All observed sources have normalized usage metadata and audit-grade quality"
            if quality_passed
            else (
                f"fragile={fragile}, partial={partial}, "
                f"unknown_sources={unknown}, missing_usage={missing_usage}"
            )
        ),
    })

    return {
        "score": score,
        "label": label,
        "weighting": weighting,
        "score_by_events": score_by_events,
        "breakdown": source_scores,
        "robustness_checks": checks,
    }


def get_score_label(score: int) -> tuple[str, str]:
    """Returns (label, suggested_color) for a 0-100 score."""
    if score >= 90:
        return ("Factual", "green")
    if score >= 70:
        return ("Defensible", "cyan")
    if score >= 40:
        return ("Indicative", "yellow")
    return ("Low", "red")
