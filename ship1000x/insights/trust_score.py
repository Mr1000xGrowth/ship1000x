"""Trust Score computation: per-source and global composite confidence.

See docs/TRUST_SCORE.md for the full methodology.

Each event carries a `confidence_flag` (high/medium/low) set by its collector.
The per-source score is a weighted average over the window. The global
composite combines all source scores plus bonuses (cadence calibrated,
unified rollups populated) minus penalties (critical sources missing).

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
    """Returns {source: {score, event_count}} for all sources active in window."""
    with storage.conn() as conn:
        rows = conn.execute(
            """
            SELECT source, COUNT(*) AS n
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
        }
    return out


def compute_global_score(
    storage: Storage,
    window_days: int = 30,
    user_email: str | None = None,
) -> dict:
    """Composite global score with bonuses and penalties.

    Returns dict {score, base, bonus, penalty, breakdown, label}.
    Capped at [0, 100].
    """
    source_scores = get_all_source_scores(storage, window_days)
    if not source_scores:
        return {
            "score": 0, "base": 0, "bonus": 0, "penalty": 0,
            "breakdown": {}, "label": "No data",
        }

    # Base = weighted average over event count
    weighted_sum = sum(
        s["score"] * s["event_count"] for s in source_scores.values()
    )
    total_events = sum(s["event_count"] for s in source_scores.values())
    base = weighted_sum // total_events if total_events else 0

    # Bonuses
    bonus = 0
    bonus_reasons: list[str] = []

    # +3 if cadence profile calibrated (sample_size >= 100)
    if user_email:
        from ship1000x.core.cadence import get_cadence_profile
        prof = get_cadence_profile(storage, user_email)
        if prof and prof.get("sample_size", 0) >= 100:
            bonus += 3
            bonus_reasons.append("+3 cadence calibrated")

    # +5 if daily_unified is populated (cross-source merge active)
    with storage.conn() as conn:
        n_unified = conn.execute(
            "SELECT COUNT(*) AS n FROM daily_unified WHERE date >= date('now', ? || ' days')",
            (f"-{window_days}",),
        ).fetchone()["n"]
    if n_unified > 0:
        bonus += 5
        bonus_reasons.append("+5 daily_unified populated")

    # Penalties
    penalty = 0
    penalty_reasons: list[str] = []
    for critical in CRITICAL_SOURCES:
        if critical not in source_scores:
            penalty += 10
            penalty_reasons.append(f"-10 critical source missing: {critical}")

    final = max(0, min(100, base + bonus - penalty))
    label, _ = get_score_label(final)

    return {
        "score": final,
        "base": base,
        "bonus": bonus,
        "penalty": penalty,
        "bonus_reasons": bonus_reasons,
        "penalty_reasons": penalty_reasons,
        "breakdown": source_scores,
        "label": label,
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
