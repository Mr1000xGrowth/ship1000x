"""Signaux faibles — D6.

Detection automatique d'alertes :
- Burnout : sessions longues repetees, heures nuit frequentes, jours consecutifs sans pause
- Derives : delta estime/reel, projet secondaire qui prend 40% du temps
- Blocages : prompts sans commits, output qui chute

Chaque signal a :
- level : info | warning | critical
- confidence : low | medium | high
- description : message lisible
- data : donnees brutes pour le user
"""

from __future__ import annotations

from typing import Any

from ship1000x.insights.benchmarks import load_benchmarks
from ship1000x.insights.engine import (
    Window,
    get_consecutive_active_days,
    get_night_active_pct,
    get_sessions_long,
    get_total_active_sec,
)


def detect_burnout(storage, window: Window) -> list[dict[str, Any]]:
    """Detecte les patterns de burnout."""
    b = load_benchmarks()
    signals: list[dict[str, Any]] = []

    # 1. Sessions longues repetees
    long_sessions = get_sessions_long(storage, window, min_hours=b["burnout_long_session_h"])
    if len(long_sessions) >= b["burnout_long_session_count_7d"]:
        signals.append({
            "category": "burnout",
            "type": "long_sessions",
            "level": "warning",
            "confidence": "high",
            "description": (
                f"{len(long_sessions)} sessions > {b['burnout_long_session_h']}h detectees "
                f"sur la periode. Surveiller le rythme."
            ),
            "data": {
                "count": len(long_sessions),
                "threshold_h": b["burnout_long_session_h"],
                "sessions": long_sessions[:5],
            },
        })

    # 2. Heures nuit frequentes
    night_pct = get_night_active_pct(
        storage,
        window,
        night_start=b["burnout_night_hour_start"],
        night_end=b["burnout_night_hour_end"],
    )
    if night_pct >= b["burnout_night_ratio_pct"]:
        signals.append({
            "category": "burnout",
            "type": "night_hours",
            "level": "warning",
            "confidence": "medium",
            "description": (
                f"{night_pct:.0f}% du temps actif entre {b['burnout_night_hour_start']}h "
                f"et {b['burnout_night_hour_end']}h. Equilibre a verifier."
            ),
            "data": {
                "night_pct": round(night_pct, 1),
                "threshold_pct": b["burnout_night_ratio_pct"],
            },
        })

    # 3. Jours consecutifs sans pause
    streak = get_consecutive_active_days(storage, window)
    if streak >= b["burnout_consecutive_days"]:
        signals.append({
            "category": "burnout",
            "type": "no_rest",
            "level": "critical" if streak >= 14 else "warning",
            "confidence": "high",
            "description": (
                f"{streak} jours consecutifs avec activite. "
                f"Pas de jour off dans la periode."
            ),
            "data": {"consecutive_days": streak, "threshold": b["burnout_consecutive_days"]},
        })

    return signals


def detect_project_drift(storage, window: Window) -> list[dict[str, Any]]:
    """Detecte les derives projet (si on a des estimations ref)."""
    # V1 : on detecte juste les projets qui prennent > 30% du temps sans etre declares
    # principaux. On ne charge pas encore les estimations Google Sheet
    # (ca viendra via `ship1000x derive --estimate my-app=30h ...`).
    b = load_benchmarks()
    signals: list[dict[str, Any]] = []

    total = get_total_active_sec(storage, window)
    if not total:
        return signals

    rows = storage.query(
        """SELECT COALESCE(project_id, 'unclassified') AS p, SUM(duration_sec) AS s
             FROM events WHERE started_at >= ? AND started_at < ?
             GROUP BY p ORDER BY s DESC""",
        (window.since_iso, window.until_iso),
    )
    for r in rows:
        pct = (r["s"] / total) * 100
        if pct >= b["derive_secondary_project_pct"] and r["p"] == "unclassified":
            signals.append({
                "category": "drift",
                "type": "unclassified_large",
                "level": "warning",
                "confidence": "high",
                "description": (
                    f"{pct:.0f}% du temps est non-classe (unclassified). "
                    f"Verifier la config projects.yaml."
                ),
                "data": {"pct": round(pct, 1), "active_sec": r["s"]},
            })
    return signals


def detect_blocages(storage, window: Window) -> list[dict[str, Any]]:
    """Detecte les blocages : beaucoup de prompts, peu de commits."""
    import json
    load_benchmarks()  # Warm cache; thresholds inlined below.
    signals: list[dict[str, Any]] = []

    # Pour chaque jour actif avec Claude Code, count typed et commits.
    rows = storage.query(
        """SELECT DATE(started_at) AS d, source, raw_meta
             FROM events WHERE started_at >= ? AND started_at < ?
               AND source IN ('claude_code', 'git')
             ORDER BY d""",
        (window.since_iso, window.until_iso),
    )
    by_day: dict[str, dict[str, int]] = {}
    for r in rows:
        d = r["d"]
        if not d:
            continue
        if d not in by_day:
            by_day[d] = {"typed": 0, "commits": 0}
        try:
            meta = json.loads(r["raw_meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if r["source"] == "claude_code":
            by_day[d]["typed"] += (meta.get("user_msg_counts") or {}).get("typed", 0) or 0
        elif r["source"] == "git":
            by_day[d]["commits"] += 1

    # Jours avec > 20 typed mais 0 commit = signal de blocage
    stuck_days = [
        (d, v) for d, v in by_day.items()
        if v["typed"] >= 20 and v["commits"] == 0
    ]
    if stuck_days:
        signals.append({
            "category": "blocage",
            "type": "prompts_without_commits",
            "level": "info",
            "confidence": "medium",
            "description": (
                f"{len(stuck_days)} journee(s) avec > 20 prompts mais 0 commit. "
                f"Iteration sans livrable ? Exploration/recherche ?"
            ),
            "data": {
                "days": [
                    {"day": d, "typed": v["typed"]} for d, v in sorted(stuck_days)[:5]
                ],
            },
        })
    return signals


def detect_fragmentation(storage, window: Window) -> list[dict[str, Any]]:
    """Journee avec > 5 projets differents touches = perte de focus."""
    signals: list[dict[str, Any]] = []
    rows = storage.query(
        """SELECT DATE(started_at) AS d, COUNT(DISTINCT project_id) AS n
             FROM events
             WHERE started_at >= ? AND started_at < ?
               AND project_id IS NOT NULL
             GROUP BY d""",
        (window.since_iso, window.until_iso),
    )
    fragmented = [(r["d"], r["n"]) for r in rows if r["n"] > 5]
    if fragmented:
        signals.append({
            "category": "focus",
            "type": "fragmentation",
            "level": "info",
            "confidence": "medium",
            "description": (
                f"{len(fragmented)} journee(s) avec > 5 projets touches. "
                f"Possible perte de focus par context-switching."
            ),
            "data": {
                "days": [{"day": d, "project_count": n} for d, n in fragmented[:5]],
            },
        })
    return signals


def detect_cost_spike(storage, window: Window) -> list[dict[str, Any]]:
    """Cost journalier > 2x moyenne de la fenetre."""
    signals: list[dict[str, Any]] = []
    rows = storage.query(
        """SELECT DATE(started_at) AS d, SUM(cost_estimated) AS c
             FROM events
             WHERE started_at >= ? AND started_at < ?
             GROUP BY d HAVING c > 0""",
        (window.since_iso, window.until_iso),
    )
    if len(rows) < 3:
        return signals
    costs = [r["c"] for r in rows]
    avg = sum(costs) / len(costs)
    spikes = [(r["d"], r["c"]) for r in rows if r["c"] > 2 * avg]
    if spikes:
        signals.append({
            "category": "cost",
            "type": "cost_spike",
            "level": "info",
            "confidence": "high",
            "description": (
                f"{len(spikes)} jour(s) avec cout IA > 2x moyenne (${avg:.2f}/j)."
            ),
            "data": {
                "avg_daily_cost": round(avg, 2),
                "spikes": [
                    {"day": d, "cost": round(c, 2)} for d, c in spikes[:5]
                ],
            },
        })
    return signals


def detect_productivity_drop(storage, window: Window) -> list[dict[str, Any]]:
    """Ratio lignes/typed recent (7j) < 50% de la moyenne window."""
    import json
    from datetime import datetime, timedelta

    signals: list[dict[str, Any]] = []
    now = datetime.fromisoformat(window.until_iso.replace("Z", "+00:00"))
    recent_since = (now - timedelta(days=7)).isoformat()

    def _ratio(start: str, end: str) -> float | None:
        git_rows = storage.query(
            """SELECT raw_meta FROM events
                 WHERE source = 'git' AND started_at >= ? AND started_at < ?""",
            (start, end),
        )
        lines = 0
        for r in git_rows:
            try:
                m = json.loads(r["raw_meta"] or "{}")
            except (json.JSONDecodeError, TypeError):
                m = {}
            lines += m.get("lines_added", 0) or 0

        cc_rows = storage.query(
            """SELECT raw_meta FROM events
                 WHERE source = 'claude_code' AND started_at >= ? AND started_at < ?""",
            (start, end),
        )
        typed = 0
        for r in cc_rows:
            try:
                m = json.loads(r["raw_meta"] or "{}")
            except (json.JSONDecodeError, TypeError):
                m = {}
            typed += (m.get("user_msg_counts") or {}).get("typed", 0) or 0
        if not typed:
            return None
        return lines / typed

    ref = _ratio(window.since_iso, window.until_iso)
    recent = _ratio(recent_since, window.until_iso)
    if ref is None or recent is None or ref <= 0:
        return signals
    if recent < 0.5 * ref:
        signals.append({
            "category": "productivity",
            "type": "productivity_drop",
            "level": "warning",
            "confidence": "medium",
            "description": (
                f"Ratio lignes/prompt recent ({recent:.0f}) < 50% de la moyenne "
                f"({ref:.0f}). Blocage technique ou phase exploration ?"
            ),
            "data": {
                "recent_lines_per_typed": round(recent, 1),
                "reference_lines_per_typed": round(ref, 1),
            },
        })
    return signals


def detect_stuck_thinking(storage, window: Window) -> list[dict[str, Any]]:
    """> 3h active + > 30 typed prompts sans commit ce jour = deep-thinking bloque."""
    import json
    from collections import defaultdict

    signals: list[dict[str, Any]] = []
    by_day: dict[str, dict[str, int]] = defaultdict(
        lambda: {"active_sec": 0, "typed": 0, "commits": 0}
    )
    rows = storage.query(
        """SELECT DATE(started_at) AS d, source, duration_sec, raw_meta
             FROM events WHERE started_at >= ? AND started_at < ?""",
        (window.since_iso, window.until_iso),
    )
    for r in rows:
        if not r["d"]:
            continue
        entry = by_day[r["d"]]
        try:
            meta = json.loads(r["raw_meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if r["source"] == "claude_code":
            entry["active_sec"] += r["duration_sec"] or 0
            entry["typed"] += (meta.get("user_msg_counts") or {}).get("typed", 0) or 0
        elif r["source"] == "git":
            entry["commits"] += 1

    stuck = [
        (d, e) for d, e in by_day.items()
        if e["active_sec"] > 3 * 3600 and e["typed"] > 30 and e["commits"] == 0
    ]
    if stuck:
        signals.append({
            "category": "blocage",
            "type": "stuck_thinking",
            "level": "info",
            "confidence": "medium",
            "description": (
                f"{len(stuck)} journee(s) > 3h active + > 30 typed sans commit. "
                f"Deep-thinking prolonge ? Blocage strategique ?"
            ),
            "data": {
                "days": [
                    {"day": d, "hours": round(e["active_sec"] / 3600, 1), "typed": e["typed"]}
                    for d, e in stuck[:5]
                ],
            },
        })
    return signals


def detect_morcellement(storage, window: Window) -> list[dict[str, Any]]:
    """> 10 sessions < 1h sur les 7 derniers jours = morcellement."""
    from datetime import datetime, timedelta

    signals: list[dict[str, Any]] = []
    now = datetime.fromisoformat(window.until_iso.replace("Z", "+00:00"))
    recent_since = (now - timedelta(days=7)).isoformat()

    rows = storage.query(
        """SELECT duration_sec FROM events
             WHERE event_type = 'session_day'
               AND started_at >= ? AND started_at < ?
               AND duration_sec > 0""",
        (recent_since, window.until_iso),
    )
    short = [r for r in rows if (r["duration_sec"] or 0) < 3600]
    if len(short) >= 10:
        signals.append({
            "category": "focus",
            "type": "morcellement",
            "level": "info",
            "confidence": "medium",
            "description": (
                f"{len(short)} sessions < 1h sur 7j. Journees morcellees, "
                f"peu de deep work soutenu."
            ),
            "data": {"short_sessions_count": len(short)},
        })
    return signals


def compute_all_signals(storage, window: Window) -> list[dict[str, Any]]:
    """Lance tous les detecteurs + tri par severite.

    Signaux equipe (solitude, imbalance) sont calcules cote dashboard
    un dashboard externe qui voit tous les users, pas cote tracker local.
    """
    signals = []
    signals.extend(detect_burnout(storage, window))
    signals.extend(detect_project_drift(storage, window))
    signals.extend(detect_blocages(storage, window))
    signals.extend(detect_fragmentation(storage, window))
    signals.extend(detect_cost_spike(storage, window))
    signals.extend(detect_productivity_drop(storage, window))
    signals.extend(detect_stuck_thinking(storage, window))
    signals.extend(detect_morcellement(storage, window))
    # Tri : critical > warning > info
    order = {"critical": 0, "warning": 1, "info": 2}
    signals.sort(key=lambda s: order.get(s["level"], 99))
    return signals
