"""Engine — queries agregats de base pour tous les insights.

Toutes les fonctions retournent des dicts/lists Python standards pour etre
facilement consommees par le CLI, le rapport Markdown, le PDF, ou une
API externe.

Convention : `storage` est une instance Storage (core.storage). Les filtres
`project` (optionnel) et `since`/`until` (datetime) sont partout supportes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass
class Window:
    """Fenetre temporelle pour les agregats."""
    since: datetime
    until: datetime
    project: str | None = None

    @property
    def days(self) -> float:
        return max(1.0, (self.until - self.since).total_seconds() / 86400)

    @property
    def since_iso(self) -> str:
        return self.since.isoformat()

    @property
    def until_iso(self) -> str:
        return self.until.isoformat()


def make_window(since_days: int = 30, project: str | None = None) -> Window:
    """Cree une fenetre [now - N jours, now]."""
    now = datetime.now(timezone.utc)
    return Window(since=now - timedelta(days=since_days), until=now, project=project)


def _where_project(window: Window) -> tuple[str, tuple]:
    """Fragment SQL WHERE commun : started_at dans fenetre + project_id optionnel."""
    if window.project:
        return (
            "started_at >= ? AND started_at < ? AND project_id = ?",
            (window.since_iso, window.until_iso, window.project),
        )
    return (
        "started_at >= ? AND started_at < ?",
        (window.since_iso, window.until_iso),
    )


# ─── Agregats temporels & volume ────────────────────────────────────────

def get_total_active_sec(storage, window: Window) -> int:
    where, params = _where_project(window)
    r = storage.query(
        f"SELECT COALESCE(SUM(duration_sec), 0) AS s FROM events WHERE {where}",
        params,
    )
    return r[0]["s"] if r else 0


def get_total_cost(storage, window: Window) -> float:
    where, params = _where_project(window)
    r = storage.query(
        f"SELECT COALESCE(SUM(cost_estimated), 0) AS c FROM events WHERE {where}",
        params,
    )
    return r[0]["c"] if r else 0.0


def get_total_tokens(storage, window: Window) -> dict[str, int]:
    where, params = _where_project(window)
    r = storage.query(
        f"""SELECT COALESCE(SUM(token_input), 0) AS ti,
                   COALESCE(SUM(token_output), 0) AS to_
             FROM events WHERE {where}""",
        params,
    )
    if not r:
        return {"input": 0, "output": 0, "total": 0}
    row = r[0]
    return {
        "input": row["ti"],
        "output": row["to_"],
        "total": row["ti"] + row["to_"],
    }


def get_msg_counts(storage, window: Window) -> dict[str, int]:
    """Totaux des user_msg_counts extraits de raw_meta (claude_code uniquement)."""
    where, params = _where_project(window)
    rows = storage.query(
        f"""SELECT raw_meta FROM events
             WHERE source = 'claude_code' AND raw_meta IS NOT NULL AND {where}""",
        params,
    )
    totals = {"typed": 0, "approval": 0, "tool_result": 0, "system": 0, "paste": 0}
    for r in rows:
        try:
            meta = json.loads(r["raw_meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        counts = meta.get("user_msg_counts") or {}
        for key in totals:
            totals[key] += counts.get(key, 0) or 0
    return totals


def get_git_stats(storage, window: Window) -> dict[str, int]:
    """Agrege lignes +/- / commits / files via raw_meta des events source=git."""
    where, params = _where_project(window)
    rows = storage.query(
        f"""SELECT raw_meta FROM events
             WHERE source = 'git' AND raw_meta IS NOT NULL AND {where}""",
        params,
    )
    stats = {"commits": len(rows), "lines_added": 0, "lines_deleted": 0, "files_changed": 0}
    for r in rows:
        try:
            meta = json.loads(r["raw_meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        stats["lines_added"] += meta.get("lines_added", 0) or 0
        stats["lines_deleted"] += meta.get("lines_deleted", 0) or 0
        stats["files_changed"] += meta.get("files_changed", 0) or 0
    return stats


def get_active_sec_by_day(storage, window: Window) -> dict[str, int]:
    """Temps actif par jour (YYYY-MM-DD) dans la fenetre."""
    where, params = _where_project(window)
    rows = storage.query(
        f"""SELECT DATE(started_at) AS d, COALESCE(SUM(duration_sec), 0) AS s
             FROM events WHERE {where}
             GROUP BY d ORDER BY d""",
        params,
    )
    return {r["d"]: r["s"] for r in rows if r["d"]}


def get_active_sec_by_hour(storage, window: Window) -> dict[int, int]:
    """Temps actif par heure de la journee (0-23)."""
    where, params = _where_project(window)
    rows = storage.query(
        f"""SELECT CAST(strftime('%H', started_at) AS INT) AS h,
                   COALESCE(SUM(duration_sec), 0) AS s
             FROM events WHERE {where} AND started_at IS NOT NULL
             GROUP BY h ORDER BY h""",
        params,
    )
    return {r["h"]: r["s"] for r in rows if r["h"] is not None}


def get_active_sec_by_source(storage, window: Window) -> dict[str, int]:
    """Temps actif par source (claude_code / codex / cursor / git)."""
    where, params = _where_project(window)
    rows = storage.query(
        f"""SELECT source, COALESCE(SUM(duration_sec), 0) AS s
             FROM events WHERE {where}
             GROUP BY source""",
        params,
    )
    return {r["source"]: r["s"] for r in rows}


def get_active_sec_by_project(storage, window: Window) -> dict[str, int]:
    """Temps actif par project_id (fenetre, pas de filtre project)."""
    rows = storage.query(
        """SELECT COALESCE(project_id, 'unclassified') AS p,
                  COALESCE(SUM(duration_sec), 0) AS s
             FROM events
             WHERE started_at >= ? AND started_at < ?
             GROUP BY p ORDER BY s DESC""",
        (window.since_iso, window.until_iso),
    )
    return {r["p"]: r["s"] for r in rows}


def get_sessions_long(storage, window: Window, min_hours: float) -> list[dict]:
    """Sessions dont duration > min_hours (detection burnout)."""
    where, params = _where_project(window)
    rows = storage.query(
        f"""SELECT DATE(started_at) AS d, started_at, ended_at, duration_sec,
                   project_id
             FROM events
             WHERE event_type = 'session_day' AND {where}
               AND duration_sec > ?
             ORDER BY duration_sec DESC""",
        params + (int(min_hours * 3600),),
    )
    return [
        {
            "day": r["d"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "duration_sec": r["duration_sec"],
            "duration_h": round(r["duration_sec"] / 3600, 1),
            "project_id": r["project_id"],
        }
        for r in rows
    ]


def get_night_active_pct(
    storage, window: Window, night_start: int = 22, night_end: int = 6
) -> float:
    """% du temps actif entre night_start (inclus) et night_end (exclus, jour suivant)."""
    total = get_total_active_sec(storage, window)
    if not total:
        return 0.0
    by_hour = get_active_sec_by_hour(storage, window)
    night_sec = 0
    for h, s in by_hour.items():
        if night_start >= night_end:
            # ex: 22..6 (traverse minuit)
            if h >= night_start or h < night_end:
                night_sec += s
        else:
            if night_start <= h < night_end:
                night_sec += s
    return (night_sec / total) * 100.0


def get_consecutive_active_days(storage, window: Window) -> int:
    """Plus longue serie de jours consecutifs avec > 0s actives."""
    by_day = get_active_sec_by_day(storage, window)
    if not by_day:
        return 0
    # Parcours les jours consecutifs
    active_days = sorted([d for d, s in by_day.items() if s > 0])
    if not active_days:
        return 0

    max_streak = 1
    current = 1
    prev = datetime.fromisoformat(active_days[0])
    for d in active_days[1:]:
        dt = datetime.fromisoformat(d)
        if (dt - prev).days == 1:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 1
        prev = dt
    return max_streak


# ─── Metriques derivees top-level (utilisees par CLI/reports) ────────────

def compute_overview(storage, window: Window) -> dict[str, Any]:
    """Vue synthetique complete : totaux + ratios de base.

    Ne calcule PAS les signaux (separement dans signals.py).
    """
    active_sec = get_total_active_sec(storage, window)
    active_hours = active_sec / 3600.0
    msg_counts = get_msg_counts(storage, window)
    git = get_git_stats(storage, window)
    tokens = get_total_tokens(storage, window)
    cost = get_total_cost(storage, window)

    # Ratios-cles
    ratios: dict[str, float | None] = {}
    typed = msg_counts["typed"]
    ratios["lines_per_hour"] = (git["lines_added"] / active_hours) if active_hours else None
    ratios["typed_per_hour"] = (typed / active_hours) if active_hours else None
    ratios["tokens_per_hour"] = (tokens["total"] / active_hours) if active_hours else None
    ratios["commits_per_hour"] = (git["commits"] / active_hours) if active_hours else None
    ratios["lines_per_typed"] = (git["lines_added"] / typed) if typed else None
    ratios["tool_per_typed"] = (msg_counts["tool_result"] / typed) if typed else None
    ratios["approval_ratio"] = (
        msg_counts["approval"] / (typed + msg_counts["approval"])
        if (typed + msg_counts["approval"])
        else None
    )
    ratios["cost_per_commit"] = (cost / git["commits"]) if git["commits"] else None
    ratios["cost_per_line_net"] = (
        cost / max(1, git["lines_added"] - git["lines_deleted"])
    ) if (git["lines_added"] - git["lines_deleted"]) > 0 else None
    ratios["cost_per_hour"] = (cost / active_hours) if active_hours else None

    return {
        "window": {
            "since": window.since_iso,
            "until": window.until_iso,
            "days": window.days,
            "project": window.project,
        },
        "totals": {
            "active_sec": active_sec,
            "active_hours": active_hours,
            "typed": msg_counts["typed"],
            "approval": msg_counts["approval"],
            "tool_result": msg_counts["tool_result"],
            "paste": msg_counts["paste"],
            "system": msg_counts["system"],
            "commits": git["commits"],
            "lines_added": git["lines_added"],
            "lines_deleted": git["lines_deleted"],
            "files_changed": git["files_changed"],
            "tokens_input": tokens["input"],
            "tokens_output": tokens["output"],
            "tokens_total": tokens["total"],
            "cost": cost,
        },
        "ratios": ratios,
    }
