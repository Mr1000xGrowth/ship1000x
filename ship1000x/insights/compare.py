"""Comparaisons — D5.

Comparaisons temporelles et entre projets :
- Projet A vs projet B sur meme fenetre
- Semaine N vs semaine N-1 (meme projet)
- Evolution multi-semaines
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ship1000x.insights.engine import Window, compute_overview


def compare_projects(
    storage,
    project_a: str,
    project_b: str,
    since_days: int = 30,
) -> dict[str, Any]:
    """Compare 2 projets sur la meme fenetre temporelle."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=since_days)
    win_a = Window(since=since, until=now, project=project_a)
    win_b = Window(since=since, until=now, project=project_b)

    ov_a = compute_overview(storage, win_a)
    ov_b = compute_overview(storage, win_b)

    return {
        "window": {"since": since.isoformat(), "until": now.isoformat(), "days": since_days},
        "a": {"project": project_a, "totals": ov_a["totals"], "ratios": ov_a["ratios"]},
        "b": {"project": project_b, "totals": ov_b["totals"], "ratios": ov_b["ratios"]},
    }


def compare_periods(
    storage,
    project: str | None,
    window_days: int = 7,
    offset_days: int = 7,
) -> dict[str, Any]:
    """Compare la fenetre recente vs la precedente (ex: cette semaine vs semaine derniere)."""
    now = datetime.now(timezone.utc)
    win_current = Window(
        since=now - timedelta(days=window_days),
        until=now,
        project=project,
    )
    win_prev = Window(
        since=now - timedelta(days=window_days + offset_days),
        until=now - timedelta(days=offset_days),
        project=project,
    )
    ov_current = compute_overview(storage, win_current)
    ov_prev = compute_overview(storage, win_prev)

    # Deltas
    def _delta(a: float | None, b: float | None) -> dict[str, Any]:
        if a is None or b is None:
            return {"abs": None, "pct": None, "direction": None}
        diff = a - b
        pct = (diff / b * 100) if b else None
        direction = "up" if diff > 0 else ("down" if diff < 0 else "flat")
        return {"abs": round(diff, 2), "pct": round(pct, 1) if pct is not None else None, "direction": direction}

    deltas = {}
    for key in ["active_hours", "typed", "commits", "lines_added", "cost"]:
        a = ov_current["totals"].get(key)
        b = ov_prev["totals"].get(key)
        deltas[key] = _delta(a, b) if a is not None and b is not None else {"abs": None, "pct": None, "direction": None}
    for key in ["lines_per_hour", "typed_per_hour", "tool_per_typed", "lines_per_typed"]:
        a = ov_current["ratios"].get(key)
        b = ov_prev["ratios"].get(key)
        deltas[key] = _delta(a, b)

    return {
        "current": {
            "window": ov_current["window"],
            "totals": ov_current["totals"],
            "ratios": ov_current["ratios"],
        },
        "previous": {
            "window": ov_prev["window"],
            "totals": ov_prev["totals"],
            "ratios": ov_prev["ratios"],
        },
        "deltas": deltas,
    }


def compare_trend(
    storage,
    project: str | None,
    n_weeks: int = 4,
) -> list[dict[str, Any]]:
    """Retourne une serie temporelle hebdomadaire (n_weeks semaines)."""
    now = datetime.now(timezone.utc)
    series = []
    for i in range(n_weeks):
        end = now - timedelta(days=7 * i)
        start = end - timedelta(days=7)
        win = Window(since=start, until=end, project=project)
        ov = compute_overview(storage, win)
        series.append({
            "week_end": end.strftime("%Y-%m-%d"),
            "active_hours": ov["totals"]["active_hours"],
            "typed": ov["totals"]["typed"],
            "commits": ov["totals"]["commits"],
            "lines_added": ov["totals"]["lines_added"],
            "cost": ov["totals"]["cost"],
            "lines_per_hour": ov["ratios"]["lines_per_hour"],
            "tool_per_typed": ov["ratios"]["tool_per_typed"],
        })
    return list(reversed(series))  # plus ancien -> plus recent
