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

Methode des ecarts vs union d'intervalles
------------------------------------------
La methode "ecarts entre events" (etapes 3-4) suppose un flux d'events DENSE :
elle ne compte un intervalle que si l'ecart entre 2 events consecutifs reste
sous le threshold. Claude Code logue des events denses -> OK. Mais Codex logue
des events tres ESPACES (1 session = 1 event, ~1 toutes les 30 min) : presque
tous les ecarts depassent le P95 et sont exclus -> le temps actif s'effondre
(une journee Codex de 17h wall-clock retombait a ~1h). Pire, les sources sans
event_timeline (Codex CLI/desktop/macapp) etaient totalement invisibles ici.

Correctif : on combine la methode des ecarts avec l'UNION D'INTERVALLES de
core.intervals (`[started_at, started_at+duration_sec]` par event, fusionnee
cross-sources). Chaque variante d'actif devient le MAX des deux mesures :
- sources denses (Claude Code) : la methode des ecarts domine et borne le P95
  qui ponte les pauses inter-sessions courtes (pas de regression) ;
- sources espacees (Codex) : l'union des durees de session domine et evite
  l'effondrement. duration_sec est deja l'actif par-session, deja plafonne
  (MAX_ACTIVE_SEC_PER_SESSION cote collectors), donc l'union ne sur-compte pas
  le temps idle a l'interieur d'une longue session.

Le resultat est destine a etre persiste dans la table daily_unified par
core.rollup, puis lu directement par tracker.py et exporters/insights_push.

Decision Charles 2026-05-15 : "B donne la meilleure data" -> on calcule
une seule fois en post-process, on stocke, tous les consumers lisent la
meme valeur. Pas de recalcul a la volee.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from ship1000x.core.cadence import HUMAN_CODES, get_cadence_profile
from ship1000x.core.intervals import (
    _parse_iso_to_epoch,
    union_active_sec_from_events,
    union_duration_sec,
)
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
    Chevauchement : inclut les sessions multi-jours qui traversent `day`
    (l'appelant borne ensuite les timestamps a la fenetre du jour).
    """
    where = (
        "date(started_at) <= ? AND date(COALESCE(ended_at, started_at)) >= ? "
        "AND raw_meta IS NOT NULL"
    )
    params: list[Any] = [day, day]
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


def _entry_is_human(entry: list) -> bool:
    """True si une entree de timeline [ts, code] est un event humain valide."""
    try:
        return int(entry[1]) in HUMAN_CODES and int(entry[0]) > 0
    except (TypeError, ValueError, IndexError):
        return False


def _day_window_epoch(day: str) -> tuple[int, int]:
    """Retourne [start, end) en epoch UTC pour la journee `day` (YYYY-MM-DD)."""
    d = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return int(d.timestamp()), int((d + timedelta(days=1)).timestamp())


def _fetch_session_intervals_for_day(
    storage: Storage, day: str, machine_id: str | None = None,
) -> list[dict]:
    """Recupere (source, started_at, duration_sec) des events chevauchant le jour.

    Sert a l'union d'intervalles. On prend toutes les sources sauf `git`
    (les commits n'ont pas de duree de presence) et duration_sec > 0. Meme
    perimetre que exporters/markdown_report (active_sec via union).

    Chevauchement : une session Codex peut durer plusieurs heures et franchir
    minuit. On la rattache a CHAQUE jour qu'elle traverse (start <= jour <= end),
    puis l'appelant borne l'intervalle a la fenetre du jour. Evite le double bug
    "session multi-jours comptee a 100% sur son jour de debut" (>24h) et
    "debut de journee perdu car la session a demarre la veille".
    """
    where = (
        "date(started_at) <= ? AND date(COALESCE(ended_at, started_at)) >= ? "
        "AND duration_sec > 0 AND source != 'git'"
    )
    params: list[Any] = [day, day]
    if machine_id is not None:
        where += " AND machine_id = ?"
        params.append(machine_id)
    with storage.conn() as conn:
        rows = conn.execute(
            f"SELECT source, started_at, duration_sec FROM events WHERE {where}",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


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
    # Fenetre stricte du jour : on borne tout (timeline ET intervalles de
    # session) a [00:00, 24:00) UTC. Sans ca, une session Codex multi-jours
    # rattachee a son jour de debut gonfle le total au-dela de 24h, et le debut
    # de journee d'une session demarree la veille serait perdu.
    day_start, day_end = _day_window_epoch(day)

    timelines = _fetch_event_timelines_for_day(storage, day, machine_id=machine_id)
    sorted_ts_all, _ = merge_human_events_cross_sources(timelines)
    sorted_ts = [t for t in sorted_ts_all if day_start <= t < day_end]

    # Union d'intervalles cross-sources sur la duree de session, bornee au jour :
    # capte les sources espacees / sans event_timeline (Codex) que la methode
    # des ecarts laisse tomber. Cf. docstring du module.
    session_rows = _fetch_session_intervals_for_day(storage, day, machine_id=machine_id)
    clamped_intervals: list[tuple[float, float]] = []
    sources_with_interval: set[str] = set()
    for r in session_rows:
        start = _parse_iso_to_epoch(r["started_at"])
        dur = r["duration_sec"]
        if start is None or not dur or dur <= 0:
            continue
        lo = max(start, day_start)
        hi = min(start + float(dur), day_end)
        if hi > lo:
            clamped_intervals.append((lo, hi))
            sources_with_interval.add(r["source"])
    union_active = union_duration_sec(clamped_intervals)

    if not sorted_ts and union_active == 0:
        return None

    # Sources distinctes = celles avec une timeline humaine DANS le jour + celles
    # avec une session mesuree chevauchant le jour. Union des deux ensembles.
    human_sources = {
        src for src, timeline in timelines
        if any(
            isinstance(e, list) and len(e) >= 2 and _entry_is_human(e)
            and day_start <= int(e[0]) < day_end for e in timeline
        )
    }
    sources_count = len(human_sources | sources_with_interval)

    # Threshold P95 du user via cadence (fallback strict si profil absent)
    threshold_p95 = FALLBACK_P95_SEC
    if user_email:
        prof = get_cadence_profile(storage, user_email)
        if prof and prof.get("sample_size", 0) >= 100:
            threshold_p95 = int(prof["p95"])

    # Chaque variante = MAX(methode des ecarts, union des durees de session).
    # Sur source dense, les ecarts dominent (et pontent les pauses courtes) ;
    # sur source espacee, l'union domine et evite l'effondrement.
    gap_strict = compute_active_sec_with_threshold(sorted_ts, THRESHOLD_STRICT_SEC)
    gap_p95 = compute_active_sec_with_threshold(sorted_ts, threshold_p95)
    gap_loose = compute_active_sec_with_threshold(sorted_ts, THRESHOLD_LOOSE_SEC)
    active_strict = max(gap_strict, union_active)
    active_p95 = max(gap_p95, union_active)
    active_loose = max(gap_loose, union_active)

    # Wall-clock : span de la timeline humaine, borne au minimum par l'actif
    # P95 (sinon agent_sec_estimated deviendrait negatif les jours Codex ou
    # l'union depasse le span timeline claude-only).
    timeline_span = sorted_ts[-1] - sorted_ts[0] if len(sorted_ts) >= 2 else 0
    wall_clock = max(timeline_span, active_p95)
    # Estimation agent IA = temps total - presence humaine. Inclut les
    # micro-pauses humain non detectees (best effort, documente comme tel).
    agent_estimated = max(0, wall_clock - active_p95)

    # Temps agents ADDITIF : somme des durees de session bornees au jour, SANS
    # dedup cross-sources. Quand l'humain pilote N sessions en parallele, leurs
    # durees s'additionnent -> peut depasser 24h (debit cumule, credible).
    # A l'inverse de active_sec_unified (presence humaine dedupliquee, <= 24h).
    # Le ratio additive / unified = facteur d'orchestration.
    #
    # Plancher = la presence humaine : le travail total ne peut pas etre
    # INFERIEUR au temps ou tu etais present (si tu es la 4h, au moins 4h de
    # travail ont eu lieu). Sans ce plancher, les jours peu paralleles ou
    # certaines sessions n'ont pas de duree mesuree donnent un cumule < presence
    # (artefact de mesure : la methode des ecarts ponte des prompts que la somme
    # des durees de session ne capte pas). Garantit cumule >= presence partout.
    agent_additive = max(int(sum(hi - lo for lo, hi in clamped_intervals)), active_p95)

    return {
        "date": day,
        "machine_id": machine_id or "unknown-machine",
        "user_email": user_email,
        "active_sec_strict": active_strict,
        "active_sec_p95": active_p95,
        "active_sec_loose": active_loose,
        "active_sec_unified": active_p95,  # alias canonique V1
        "agent_sec_estimated": agent_estimated,
        "agent_sec_additive": agent_additive,
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
            agent_sec_estimated, agent_sec_additive, wall_clock_sec,
            threshold_used_sec, sample_size, sources_count, computed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, machine_id) DO UPDATE SET
            user_email          = excluded.user_email,
            active_sec_strict   = excluded.active_sec_strict,
            active_sec_p95      = excluded.active_sec_p95,
            active_sec_loose    = excluded.active_sec_loose,
            active_sec_unified  = excluded.active_sec_unified,
            agent_sec_estimated = excluded.agent_sec_estimated,
            agent_sec_additive  = excluded.agent_sec_additive,
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
            metrics["agent_sec_estimated"], metrics.get("agent_sec_additive", 0),
            metrics["wall_clock_sec"],
            metrics["threshold_used_sec"], metrics["sample_size"],
            metrics["sources_count"], metrics["computed_at"],
        ))


def rebuild_unified_metrics(
    storage: Storage,
    since: datetime | None = None,
    user_email: str | None = None,
) -> dict[str, int]:
    """(Re)calcule daily_unified pour chaque (date, machine) depuis `since`.

    Purge + recompute la fenetre (idempotent). Appele par `ship1000x rollup`
    apres rebuild_rollups : daily_unified est la source de verite du graphe
    "Daily activity" du dashboard et du multiplicateur agent.
    """
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=180)
    since_date = since.date().isoformat()

    with storage.conn() as conn:
        conn.execute("DELETE FROM daily_unified WHERE date >= ?", (since_date,))
        pairs = conn.execute(
            """
            SELECT DISTINCT date(started_at) AS day, machine_id
            FROM events
            WHERE source != 'git'
              AND started_at IS NOT NULL
              AND date(started_at) >= ?
            """,
            (since_date,),
        ).fetchall()

    stats = {"unified_rows": 0, "days": 0}
    days_seen: set[str] = set()
    for r in pairs:
        day = r["day"]
        machine_id = r["machine_id"]
        metrics = compute_unified_metrics(
            storage, day, user_email=user_email, machine_id=machine_id,
        )
        if metrics is None:
            continue
        upsert_daily_unified(storage, metrics)
        stats["unified_rows"] += 1
        days_seen.add(day)
    stats["days"] = len(days_seen)
    return stats


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
