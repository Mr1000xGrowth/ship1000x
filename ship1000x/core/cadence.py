"""Calcule le profil de cadence personnel d'un user (distribution des deltas
inter-prompts humains).

Permet a chaque user d'avoir un cap auto calé sur SA realite plutot qu'un
cap arbitraire universel. Le 95e percentile est le default recommande
(capture 95% de l'activite, coupe 5% comme pauses).

Decision Charles 2026-04-25 : "stop les approximations, on fait exact"
appliquee aussi au choix du cap.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ship1000x.core.storage import Storage

# Codes events humains (typed=0, approval=1, paste=2). Sync avec
# active-sec.ts et day-timeline.ts cote front-end.
HUMAN_CODES = (0, 1, 2)


def compute_cadence_profile(
    storage: Storage,
    user_email: str,
    window_days: int = 14,
) -> dict | None:
    """Calcule le profil de cadence (percentiles des deltas) sur N jours.

    Lit les `event_timeline` JSON dans `events.raw_meta`, extrait les
    timestamps humains, calcule les deltas inter-events successifs par jour,
    et retourne les percentiles classiques.

    Returns None si pas assez de data (sample_size < 50) car les percentiles
    seraient instables. Le caller doit gerer ce cas (fallback DEFAULT cap).
    """
    sql = """
        SELECT raw_meta
        FROM events
        WHERE date(started_at) >= date('now', ? || ' days')
          AND source IN ('claude_code','codex_macapp','codex_desktop','openclaw','cline','cursor')
          AND raw_meta IS NOT NULL
    """
    with storage.conn() as conn:
        rows = conn.execute(sql, (f"-{window_days}",)).fetchall()

    # Regrouper les timestamps humains par date (UTC) pour ne pas mesurer
    # des deltas entre 2 jours differents (vraies pauses nocturnes).
    humans_per_day: dict[str, list[int]] = {}
    for row in rows:
        meta = row["raw_meta"]
        if not meta:
            continue
        try:
            parsed = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            continue
        timeline = parsed.get("event_timeline") or parsed.get("event_markers")
        if not isinstance(timeline, list):
            continue
        for entry in timeline:
            if not (isinstance(entry, list) and len(entry) >= 2):
                continue
            ts, code = int(entry[0]), int(entry[1])
            if code not in HUMAN_CODES:
                continue
            day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            humans_per_day.setdefault(day, []).append(ts)

    # Calcul des deltas inter-events par jour
    all_deltas: list[int] = []
    for tss in humans_per_day.values():
        tss.sort()
        for i in range(1, len(tss)):
            d = tss[i] - tss[i - 1]
            if d > 0:
                all_deltas.append(d)

    if len(all_deltas) < 50:
        return None  # Trop peu de data pour des percentiles stables

    all_deltas.sort()
    n = len(all_deltas)

    def pct(p: float) -> int:
        idx = min(int(p * n), n - 1)
        return all_deltas[idx]

    return {
        "user_email": user_email,
        "p50": pct(0.50),
        "p75": pct(0.75),
        "p90": pct(0.90),
        "p95": pct(0.95),
        "p99": pct(0.99),
        "sample_size": n,
        "window_days": window_days,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def upsert_cadence_profile(storage: Storage, profile: dict) -> None:
    """Persist le profil dans la table user_cadence_profile (1 row par user)."""
    sql = """
        INSERT INTO user_cadence_profile
            (user_email, p50, p75, p90, p95, p99, sample_size, window_days, computed_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_email) DO UPDATE SET
            p50 = excluded.p50,
            p75 = excluded.p75,
            p90 = excluded.p90,
            p95 = excluded.p95,
            p99 = excluded.p99,
            sample_size = excluded.sample_size,
            window_days = excluded.window_days,
            computed_at = excluded.computed_at
    """
    with storage.conn() as conn:
        conn.execute(
            sql,
            (
                profile["user_email"],
                profile["p50"],
                profile["p75"],
                profile["p90"],
                profile["p95"],
                profile["p99"],
                profile["sample_size"],
                profile["window_days"],
                profile["computed_at"],
            ),
        )


def get_cadence_profile(storage: Storage, user_email: str) -> dict | None:
    """Lit le dernier profil persiste pour un user. Return None si jamais calcule."""
    sql = """
        SELECT user_email, p50, p75, p90, p95, p99, sample_size, window_days, computed_at
        FROM user_cadence_profile
        WHERE user_email = ?
    """
    with storage.conn() as conn:
        row = conn.execute(sql, (user_email,)).fetchone()
    if not row:
        return None
    return dict(row)


def refresh_user_cadence(
    storage: Storage,
    user_email: str,
    window_days: int = 14,
) -> dict | None:
    """Calcule + persiste le profil pour un user. Util pour `tracker.py daily`.

    Le tracker local ne traque qu'un user (l'owner de la machine), donc
    pas besoin de boucle multi-users ici. Le user_email est lu depuis
    privacy.yaml (consent.user_email).
    """
    profile = compute_cadence_profile(storage, user_email, window_days)
    if profile:
        upsert_cadence_profile(storage, profile)
    return profile
