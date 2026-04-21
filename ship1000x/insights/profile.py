"""Profil d'usage individuel — D4.

Caracteristiques du style de travail :
- Heatmap 7x24 (jour de semaine x heure)
- Distribution duree sessions
- % journees mono-tache
- Switch cost estime
"""

from __future__ import annotations

import json
from typing import Any

from ship1000x.insights.engine import (
    Window,
    get_active_sec_by_day,
)


def compute_profile(storage, window: Window) -> dict[str, Any]:
    """Retourne un profil d'usage agrege."""
    # Heatmap 7x24 : temps actif (duration_sec) + temps IA autonome
    # (wall_clock_sec - duration_sec, = temps ou l'IA travaille sans que tu
    # prompt activement) par (day_of_week, hour).
    #
    # Repartition fine : chaque event est etale proportionnellement entre
    # started_at et ended_at sur les heures qu'il chevauche. Evite le biais
    # "tout attribue a l'heure de started_at" qui concentrait les session_day
    # JSONL (started_at = 00h UTC du premier message du jour).
    from datetime import datetime, timedelta, timezone as _tz
    heatmap = {dow: {h: 0 for h in range(24)} for dow in range(7)}
    heatmap_autonomous = {dow: {h: 0 for h in range(24)} for dow in range(7)}

    where = "started_at >= ? AND started_at < ?"
    params: list = [window.since_iso, window.until_iso]
    if window.project:
        where += " AND project_id = ?"
        params.append(window.project)

    event_rows = storage.query(
        f"""SELECT started_at, ended_at, duration_sec, wall_clock_sec
             FROM events
             WHERE {where} AND duration_sec IS NOT NULL AND duration_sec > 0""",
        tuple(params),
    )

    def _parse_iso(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    for r in event_rows:
        started = _parse_iso(r["started_at"])
        ended = _parse_iso(r["ended_at"]) or started
        if started is None:
            continue
        duration = r["duration_sec"] or 0
        wall = r["wall_clock_sec"] or 0
        autonomous = max(0, wall - duration)

        # Si la session ne chevauche qu'une heure (ou ended < started), attribue tout a started_at
        span_sec = max(1.0, (ended - started).total_seconds())
        wall_ratio_per_sec = autonomous / span_sec if span_sec > 0 else 0
        active_ratio_per_sec = duration / span_sec if span_sec > 0 else 0

        # Itere minute par minute (pas trop couteux pour des events de
        # max ~12h = 720 min) et attribue a la bonne (dow, hour) bucket
        cur = started
        step = timedelta(minutes=5)  # pas de 5min, bon compromis precision/cout
        step_sec = step.total_seconds()
        while cur < ended:
            dow = int(cur.strftime("%w"))
            hour = int(cur.strftime("%H"))
            heatmap[dow][hour] += active_ratio_per_sec * step_sec
            heatmap_autonomous[dow][hour] += wall_ratio_per_sec * step_sec
            cur += step
        # Si pas d'iteration (span trop petit), attribue au point de depart
        if started >= ended:
            dow = int(started.strftime("%w"))
            hour = int(started.strftime("%H"))
            heatmap[dow][hour] += duration
            heatmap_autonomous[dow][hour] += autonomous

    # Cast en int pour le JSON payload
    for dow in range(7):
        for hour in range(24):
            heatmap[dow][hour] = int(heatmap[dow][hour])
            heatmap_autonomous[dow][hour] = int(heatmap_autonomous[dow][hour])

    # Distribution duree sessions (session_day events)
    if window.project:
        dur_rows = storage.query(
            """SELECT duration_sec FROM events
                 WHERE event_type = 'session_day'
                   AND started_at >= ? AND started_at < ? AND project_id = ?""",
            (window.since_iso, window.until_iso, window.project),
        )
    else:
        dur_rows = storage.query(
            """SELECT duration_sec FROM events
                 WHERE event_type = 'session_day'
                   AND started_at >= ? AND started_at < ?""",
            (window.since_iso, window.until_iso),
        )
    buckets = {"lt_1h": 0, "1_3h": 0, "3_6h": 0, "gt_6h": 0}
    for r in dur_rows:
        h = (r["duration_sec"] or 0) / 3600
        if h < 1:
            buckets["lt_1h"] += 1
        elif h < 3:
            buckets["1_3h"] += 1
        elif h < 6:
            buckets["3_6h"] += 1
        else:
            buckets["gt_6h"] += 1

    # Journees mono-tache : un projet domine > 70% du temps actif du jour
    if not window.project:
        day_rows = storage.query(
            """SELECT DATE(started_at) AS d, COALESCE(project_id, 'unclassified') AS p,
                      COALESCE(SUM(duration_sec), 0) AS s
                 FROM events
                 WHERE started_at >= ? AND started_at < ?
                 GROUP BY d, p""",
            (window.since_iso, window.until_iso),
        )
        by_day: dict[str, dict[str, int]] = {}
        for r in day_rows:
            if not r["d"]:
                continue
            by_day.setdefault(r["d"], {})[r["p"]] = r["s"]
        mono_days = 0
        multi_days = 0
        for d, proj_map in by_day.items():
            total = sum(proj_map.values())
            if total == 0:
                continue
            top = max(proj_map.values())
            if top / total >= 0.70:
                mono_days += 1
            else:
                multi_days += 1
        switch_days = {"mono": mono_days, "multi": multi_days}
    else:
        switch_days = {"mono": 0, "multi": 0}

    # Wordcount distribution (dictee vs tape)
    if window.project:
        wc_rows = storage.query(
            """SELECT wordcount FROM events
                 WHERE source = 'claude_code' AND wordcount IS NOT NULL
                   AND started_at >= ? AND started_at < ? AND project_id = ?""",
            (window.since_iso, window.until_iso, window.project),
        )
    else:
        wc_rows = storage.query(
            """SELECT wordcount FROM events
                 WHERE source = 'claude_code' AND wordcount IS NOT NULL
                   AND started_at >= ? AND started_at < ?""",
            (window.since_iso, window.until_iso),
        )
    wordcount_buckets = {"typed_short": 0, "mixed": 0, "voice_dictation": 0}
    for r in wc_rows:
        wc = r["wordcount"] or 0
        if wc < 50:
            wordcount_buckets["typed_short"] += 1
        elif wc < 200:
            wordcount_buckets["mixed"] += 1
        else:
            wordcount_buckets["voice_dictation"] += 1

    # Activity par jour semaine (simple)
    by_day_sec = get_active_sec_by_day(storage, window)
    n_active_days = sum(1 for s in by_day_sec.values() if s > 0)
    n_total_days = max(1, int(window.days))

    return {
        "heatmap_dow_hour": heatmap,  # {0-6: {0-23: sec}} — temps actif humain
        "heatmap_dow_hour_autonomous": heatmap_autonomous,  # temps IA autonome
        "session_duration_buckets": buckets,
        "switch_days": switch_days,
        "wordcount_buckets": wordcount_buckets,
        "coverage": {
            "active_days": n_active_days,
            "window_days": n_total_days,
            "active_ratio": n_active_days / n_total_days,
        },
    }
