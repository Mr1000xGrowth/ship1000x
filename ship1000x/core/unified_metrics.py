"""Calcul du temps actif unifie cross-sources.

Resout le bug multi-agents : si l'user fait Claude Code + Codex + Cursor en
parallele de 10:00 a 10:05, daily_rollup compte 3x5min (1 row par source)
alors que la realite est 1 seule presence humaine de 5 min.

Ce module fournit le calcul correct :
1. Recupere les events humains (typed/approval/paste) de toutes sources via
   le champ event_timeline JSON dans events.raw_meta.
2. Fusionne par timestamp et deduplique (~+/- 1s d'ecart = meme event).
3. Calcule les intervalles inter-events successifs.
4. Applique 4 thresholds (strict 5min / P95 user / loose 15min / unified=P95).
5. Retourne les 4 sommes d'actif + agent_sec_estimated + wall_clock_sec.

Le resultat est destine a etre persiste dans la table daily_unified par
core.rollup, puis lu directement par tracker.py et exporters/insights_push.

Decision Charles 2026-05-15 : "B donne la meilleure data" -> on calcule
une seule fois en post-process, on stocke, tous les consumers lisent la
meme valeur. Pas de recalcul a la volee.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from ship1000x.core.cadence import HUMAN_CODES, get_cadence_profile
from ship1000x.core.storage import Storage

# Sources qui emettent un event_timeline humain dans raw_meta.
# Sync avec core.cadence._SOURCES_WITH_TIMELINE.
SOURCES_WITH_HUMAN_TIMELINE = (
    "claude_code", "codex", "codex_macapp", "codex_desktop",
    "openclaw", "cline", "cursor",
)

# Threshold defaults (en secondes). Mode "unified" prend la valeur P95 du user
# si dispo, sinon fallback sur strict.
THRESHOLD_STRICT_SEC = 5 * 60       # mode --strict (hardcode conservateur)
THRESHOLD_LOOSE_SEC = 15 * 60       # mode --loose (genereux)
FALLBACK_P95_SEC = THRESHOLD_STRICT_SEC  # si user_cadence_profile vide

# Tolerance de dedup quand 2 sources voient le "meme" event humain a +/- N sec.
# Utile car un prompt typé dans Claude Code declenche parfois un event
# observable dans plusieurs sources en parallele (ex: openclaw mirror).
DEDUP_TOLERANCE_SEC = 2


def _fetch_event_timelines_for_day(
    storage: Storage, day: str, machine_id: str | None = None,
) -> list[tuple[str, list[list[int]]]]:
    """Recupere les event_timeline JSON de tous les events d'un jour.

    Returns: liste de (source, timeline) ou timeline = list[[ts_epoch, type_code]].
    """
    where = "date(started_at) = ? AND raw_meta IS NOT NULL"
    params: list[Any] = [day]
    if machine_id is not None:
        where += " AND machine_id = ?"
        params.append(machine_id)
    sql = f"""
        SELECT source, raw_meta
        FROM events
        WHERE {where}
          AND source IN ({",".join("?" * len(SOURCES_WITH_HUMAN_TIMELINE))})
    """
    params += list(SOURCES_WITH_HUMAN_TIMELINE)
    out: list[tuple[str, list[list[int]]]] = []
    with storage.conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    for r in rows:
        meta = r["raw_meta"]
        if not meta:
            continue
        try:
            parsed = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            continue
        timeline = parsed.get("event_timeline") or parsed.get("event_markers")
        if isinstance(timeline, list) and timeline:
            out.append((r["source"], timeline))
    return out


def merge_human_events_cross_sources(
    timelines: Iterable[tuple[str, list[list[int]]]],
    dedup_tolerance_sec: int = DEDUP_TOLERANCE_SEC,
) -> tuple[list[int], int]:
    """Fusionne tous les events humains de toutes sources en une serie triee.

    - Garde uniquement les events humains (codes 0/1/2 = typed/approval/paste).
    - Trie par timestamp.
    - Dedupe les events a +/- dedup_tolerance_sec (evite double-comptage quand
      2 sources voient le meme prompt).

    Returns: (sorted_timestamps_epoch, sources_count)
    """
    all_ts: list[int] = []
    sources_seen = set()
    for source, timeline in timelines:
        any_human = False
        for entry in timeline:
            if not (isinstance(entry, list) and len(entry) >= 2):
                continue
            try:
                ts = int(entry[0])
                code = int(entry[1])
            except (TypeError, ValueError):
                continue
            if code in HUMAN_CODES and ts > 0:
                all_ts.append(ts)
                any_human = True
        if any_human:
            sources_seen.add(source)
    if not all_ts:
        return [], 0
    all_ts.sort()
    # Dedup avec tolerance
    deduped: list[int] = [all_ts[0]]
    for ts in all_ts[1:]:
        if ts - deduped[-1] > dedup_tolerance_sec:
            deduped.append(ts)
    return deduped, len(sources_seen)


def compute_active_sec_with_threshold(
    sorted_ts: list[int], threshold_sec: int,
) -> int:
    """Somme les intervalles consecutifs <= threshold (=focus continu).

    Intervalles > threshold = vraies pauses, exclus. C'est la regle simple
    et defendable du V1 (pas de ponderation 50%/25%, retire 2026-04-25).
    """
    if len(sorted_ts) < 2 or threshold_sec <= 0:
        return 0
    total = 0
    for i in range(1, len(sorted_ts)):
        delta = sorted_ts[i] - sorted_ts[i - 1]
        if 0 < delta <= threshold_sec:
            total += delta
    return total


def compute_unified_metrics(
    storage: Storage,
    day: str,
    user_email: str | None = None,
    machine_id: str | None = None,
) -> dict | None:
    """Calcule les 5 metriques unifiees pour un jour donne.

    Returns dict avec :
      - active_sec_strict, active_sec_p95, active_sec_loose, active_sec_unified
      - agent_sec_estimated (best effort = wall - unified)
      - wall_clock_sec (last_event - first_event cross-sources)
      - threshold_used_sec, sample_size, sources_count
    Returns None si aucun event humain ce jour.
    """
    timelines = _fetch_event_timelines_for_day(storage, day, machine_id=machine_id)
    sorted_ts, sources_count = merge_human_events_cross_sources(timelines)
    if not sorted_ts:
        return None

    # Threshold P95 du user via cadence (fallback strict si profil absent)
    threshold_p95 = FALLBACK_P95_SEC
    if user_email:
        prof = get_cadence_profile(storage, user_email)
        if prof and prof.get("sample_size", 0) >= 100:
            threshold_p95 = int(prof["p95"])

    active_strict = compute_active_sec_with_threshold(sorted_ts, THRESHOLD_STRICT_SEC)
    active_p95 = compute_active_sec_with_threshold(sorted_ts, threshold_p95)
    active_loose = compute_active_sec_with_threshold(sorted_ts, THRESHOLD_LOOSE_SEC)

    wall_clock = sorted_ts[-1] - sorted_ts[0] if len(sorted_ts) >= 2 else 0
    # Estimation agent IA = temps total - presence humaine. Inclut les
    # micro-pauses humain non detectees (best effort, documente comme tel).
    agent_estimated = max(0, wall_clock - active_p95)

    return {
        "date": day,
        "machine_id": machine_id or "unknown-machine",
        "user_email": user_email,
        "active_sec_strict": active_strict,
        "active_sec_p95": active_p95,
        "active_sec_loose": active_loose,
        "active_sec_unified": active_p95,  # alias canonique V1
        "agent_sec_estimated": agent_estimated,
        "wall_clock_sec": wall_clock,
        "threshold_used_sec": threshold_p95,
        "sample_size": len(sorted_ts),
        "sources_count": sources_count,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def upsert_daily_unified(storage: Storage, metrics: dict) -> None:
    """Persiste les metriques dans daily_unified (1 row par date+machine)."""
    sql = """
        INSERT INTO daily_unified (
            date, machine_id, user_email,
            active_sec_strict, active_sec_p95, active_sec_loose, active_sec_unified,
            agent_sec_estimated, wall_clock_sec,
            threshold_used_sec, sample_size, sources_count, computed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, machine_id) DO UPDATE SET
            user_email          = excluded.user_email,
            active_sec_strict   = excluded.active_sec_strict,
            active_sec_p95      = excluded.active_sec_p95,
            active_sec_loose    = excluded.active_sec_loose,
            active_sec_unified  = excluded.active_sec_unified,
            agent_sec_estimated = excluded.agent_sec_estimated,
            wall_clock_sec      = excluded.wall_clock_sec,
            threshold_used_sec  = excluded.threshold_used_sec,
            sample_size         = excluded.sample_size,
            sources_count       = excluded.sources_count,
            computed_at         = excluded.computed_at
    """
    with storage.conn() as conn:
        conn.execute(sql, (
            metrics["date"], metrics["machine_id"], metrics.get("user_email"),
            metrics["active_sec_strict"], metrics["active_sec_p95"],
            metrics["active_sec_loose"], metrics["active_sec_unified"],
            metrics["agent_sec_estimated"], metrics["wall_clock_sec"],
            metrics["threshold_used_sec"], metrics["sample_size"],
            metrics["sources_count"], metrics["computed_at"],
        ))


def get_daily_unified(
    storage: Storage, day: str, machine_id: str | None = None,
) -> dict | None:
    """Lit les metriques unifiees d'un jour. Returns None si jamais calcule."""
    where = "date = ?"
    params: list[Any] = [day]
    if machine_id is not None:
        where += " AND machine_id = ?"
        params.append(machine_id)
    with storage.conn() as conn:
        row = conn.execute(
            f"SELECT * FROM daily_unified WHERE {where} LIMIT 1", params,
        ).fetchone()
    return dict(row) if row else None
