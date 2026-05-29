"""Rollup aggregator — agrege events bruts en `daily_rollup`.

Les rollups sont les SEULES donnees pushees vers le bucket equipe.
Aucune donnee event-level ne quitte la machine.

Schema rollup :
  (date, project_id, source, machine_id) ->
    duration_sec, active_sec, event_count, user_msg_count, cost_estimated,
    lines_<categorie>_<direction>, unique_commit_hashes_json, machine_origin

V2 multi-machines : le rollup porte desormais le machine_id pour permettre
la desambiguisation cross-Mac dans le dashboard (Phase 2). Les rollups git
portent en plus `unique_commit_hashes_json` (la liste des hashes du jour)
qui permet le dedup cross-machines cote reader (Phase 1 du plan).

`machine_origin` est l'attribution finale hybride A+C :
  - Si une session IA active est trouvee sur la meme date pour une seule
    machine → machine_origin = machine_id de cette machine
  - Sinon (ambiguite ou absence) → machine_origin = "shared"
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any


def _compute_machine_origin_for_git_commits(
    storage, conn, since_iso: str, date: str, project_id: str
) -> str | None:
    """Attribution hybride A+C pour les commits git d'une date/projet.

    Regle :
      - Si 1 seule machine a une session IA (source != git) sur la meme date
        et le meme project_id → retourne le machine_id de cette machine
      - Si 0 ou 2+ machines → retourne "shared" (indetermine)

    `project_id` peut etre "unclassified" pour les commits non classifies,
    dans ce cas on cherche toutes les sessions IA du jour (pas filtre projet).

    Events legacy (pre-V2, machine_id NULL) sont consideres comme venant de
    la machine qui a ingere les data (= machine courante via platform.node).
    Sinon 100% des rollups git legacy remonteraient "shared" alors qu'ils
    ont ete produits sur une machine connue.
    """
    from ship1000x.core.storage import _current_machine_id
    fallback_machine = _current_machine_id()

    if project_id == "unclassified":
        rows = conn.execute(
            """
            SELECT DISTINCT COALESCE(machine_id, ?) AS machine_id FROM events
            WHERE DATE(started_at) = ?
              AND source != 'git'
              AND started_at >= ?
            """,
            (fallback_machine, date, since_iso),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT DISTINCT COALESCE(machine_id, ?) AS machine_id FROM events
            WHERE DATE(started_at) = ?
              AND project_id = ?
              AND source != 'git'
              AND started_at >= ?
            """,
            (fallback_machine, date, project_id, since_iso),
        ).fetchall()
    machines = [r["machine_id"] for r in rows if r["machine_id"]]
    if len(machines) == 1:
        return machines[0]
    if len(machines) == 0:
        # Aucune session IA du tout : on attribue au machine_id "fallback"
        # (la machine qui a ingere). Plus honnete que "shared" qui suggere
        # qu'il y aurait conflit.
        return fallback_machine
    # 2+ machines : vraie ambiguite → partage
    return "shared"


def rebuild_rollups(storage, since: datetime | None = None) -> dict[str, int]:
    """Recalcule les daily_rollup a partir des events.

    Purge + recreate pour la fenetre donnee (idempotent).
    """
    if since is None:
        # Par defaut : 90 derniers jours
        since = datetime.now(timezone.utc) - timedelta(days=180)

    stats = {"rollups_created": 0, "days": 0}

    with storage.conn() as c:
        # Purge fenetre
        c.execute(
            "DELETE FROM daily_rollup WHERE date >= ?",
            (since.date().isoformat(),),
        )

        # Recalcule. Note : `active_sec` est conserve comme alias historique
        # de `duration_sec` (V1). La vraie valeur complementaire est
        # `wall_clock_sec` (V2 Phase B) = first_event -> last_event.
        # V2 : nouvelles colonnes lines_<categorie>_<direction> derivees du
        # classifier (real/seed/vendored/generated) stocke dans raw_meta.
        # V2 multi-Mac : GROUP BY inclut machine_id pour permettre l'attribution
        # machine par machine dans le dashboard.
        rows = c.execute(
            """
            SELECT
                DATE(started_at) AS date,
                COALESCE(project_id, 'unclassified') AS project_id,
                source,
                COALESCE(machine_id, 'unknown-machine') AS machine_id,
                SUM(duration_sec) AS duration_sec,
                SUM(duration_sec) AS active_sec,
                SUM(wall_clock_sec) AS wall_clock_sec,
                SUM(CAST(COALESCE(json_extract(raw_meta, '$.lines_added'), 0) AS INTEGER)) AS lines_added,
                SUM(CAST(COALESCE(json_extract(raw_meta, '$.lines_deleted'), 0) AS INTEGER)) AS lines_deleted,
                -- V2 : breakdown par categorie (real / seed / vendored / generated)
                SUM(CAST(COALESCE(json_extract(raw_meta, '$.lines_real_added'), 0) AS INTEGER)) AS lines_real_added,
                SUM(CAST(COALESCE(json_extract(raw_meta, '$.lines_real_deleted'), 0) AS INTEGER)) AS lines_real_deleted,
                SUM(CAST(COALESCE(json_extract(raw_meta, '$.lines_seed_added'), 0) AS INTEGER)) AS lines_seed_added,
                SUM(CAST(COALESCE(json_extract(raw_meta, '$.lines_seed_deleted'), 0) AS INTEGER)) AS lines_seed_deleted,
                SUM(CAST(COALESCE(json_extract(raw_meta, '$.lines_vendored_added'), 0) AS INTEGER)) AS lines_vendored_added,
                SUM(CAST(COALESCE(json_extract(raw_meta, '$.lines_vendored_deleted'), 0) AS INTEGER)) AS lines_vendored_deleted,
                SUM(CAST(COALESCE(json_extract(raw_meta, '$.lines_generated_added'), 0) AS INTEGER)) AS lines_generated_added,
                SUM(CAST(COALESCE(json_extract(raw_meta, '$.lines_generated_deleted'), 0) AS INTEGER)) AS lines_generated_deleted,
                -- V2 multi-Mac : concat des hashes de commits git pour dedup cross-machines
                -- (GROUP_CONCAT est SQLite natif, retourne une string CSV qu'on convertit en JSON array)
                CASE WHEN source = 'git' THEN
                    GROUP_CONCAT(DISTINCT json_extract(raw_meta, '$.commit_hash'))
                ELSE NULL END AS commit_hashes_csv,
                COUNT(*) AS event_count,
                SUM(CASE WHEN user_msg_type = 'typed' THEN 1 ELSE 0 END) AS user_msg_count,
                SUM(cost_estimated) AS cost_estimated
            FROM events
            WHERE started_at >= ? AND started_at IS NOT NULL
            GROUP BY date, project_id, source, machine_id
            """,
            (since.isoformat(),),
        ).fetchall()

        days = set()
        for r in rows:
            # Conversion CSV → JSON array (null si aucun hash ou source != git)
            unique_hashes_json: str | None = None
            if r["source"] == "git" and r["commit_hashes_csv"]:
                hashes = [
                    h.strip()
                    for h in r["commit_hashes_csv"].split(",")
                    if h and h.strip() and h.strip() != "null"
                ]
                if hashes:
                    unique_hashes_json = json.dumps(sorted(set(hashes)))

            # Machine_origin hybride A+C : pour les commits git, on cherche
            # quelle machine avait des sessions IA sur la meme date/projet.
            machine_origin: str | None = None
            if r["source"] == "git":
                machine_origin = _compute_machine_origin_for_git_commits(
                    storage, c, since.isoformat(), r["date"], r["project_id"]
                )
            else:
                # Pour les events IA, machine_origin = machine_id (direct)
                machine_origin = r["machine_id"]

            c.execute(
                """
                INSERT INTO daily_rollup
                    (date, project_id, source, machine_id, duration_sec, active_sec,
                     wall_clock_sec, lines_added, lines_deleted,
                     lines_real_added, lines_real_deleted,
                     lines_seed_added, lines_seed_deleted,
                     lines_vendored_added, lines_vendored_deleted,
                     lines_generated_added, lines_generated_deleted,
                     unique_commit_hashes_json, machine_origin,
                     event_count, user_msg_count, cost_estimated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["date"], r["project_id"], r["source"], r["machine_id"],
                    r["duration_sec"] or 0, r["active_sec"] or 0,
                    r["wall_clock_sec"] or 0,
                    r["lines_added"] or 0, r["lines_deleted"] or 0,
                    r["lines_real_added"] or 0, r["lines_real_deleted"] or 0,
                    r["lines_seed_added"] or 0, r["lines_seed_deleted"] or 0,
                    r["lines_vendored_added"] or 0, r["lines_vendored_deleted"] or 0,
                    r["lines_generated_added"] or 0, r["lines_generated_deleted"] or 0,
                    unique_hashes_json, machine_origin,
                    r["event_count"] or 0, r["user_msg_count"] or 0,
                    r["cost_estimated"] or 0.0,
                ),
            )
            stats["rollups_created"] += 1
            days.add(r["date"])
        stats["days"] = len(days)

    return stats


def get_rollups_for_push(
    storage,
    since_date: str | None = None,
    share_config: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Retourne les rollups eligibles au partage selon la privacy config.

    Filtre :
      - Uniquement les projets marques `aggregated` dans share
      - Tous les projets si share.default == "aggregated"
    """
    share_config = share_config or {}
    default_level = share_config.get("_default", "private")

    where = ["1=1"]
    params: list[Any] = []

    if since_date:
        where.append("date >= ?")
        params.append(since_date)

    rows = storage.query(
        f"""
        SELECT date, project_id, source, machine_id, duration_sec, active_sec,
               wall_clock_sec, lines_added, lines_deleted,
               lines_real_added, lines_real_deleted,
               lines_seed_added, lines_seed_deleted,
               lines_vendored_added, lines_vendored_deleted,
               lines_generated_added, lines_generated_deleted,
               unique_commit_hashes_json, machine_origin,
               event_count, user_msg_count, cost_estimated
        FROM daily_rollup
        WHERE {" AND ".join(where)}
        ORDER BY date DESC, project_id, machine_id
        """,
        tuple(params),
    )

    def _col(row, key, default=0):
        try:
            return row[key] if key in row.keys() else default
        except Exception:
            return default

    result = []
    for r in rows:
        level = share_config.get(r["project_id"], default_level)
        if level != "aggregated":
            continue
        # V2 multi-Mac : on serialise les hashes en list directement (pas string JSON)
        # pour que le push JSONL ait un vrai array cote reader. Si null en DB,
        # on envoie null (retrocompat : reader fallback sur count non-dedupe).
        hashes_json = _col(r, "unique_commit_hashes_json", None)
        unique_hashes: list[str] | None = None
        if hashes_json:
            try:
                unique_hashes = json.loads(hashes_json)
            except (json.JSONDecodeError, TypeError):
                unique_hashes = None

        result.append({
            "date": r["date"],
            "project_id": r["project_id"],
            "source": r["source"],
            # V2 multi-Mac
            "machine_id": _col(r, "machine_id", "unknown-machine"),
            "machine_origin": _col(r, "machine_origin", None),
            "unique_commit_hashes": unique_hashes,
            "duration_sec": r["duration_sec"],
            "active_sec": r["active_sec"],
            "wall_clock_sec": _col(r, "wall_clock_sec"),
            "lines_added": _col(r, "lines_added"),
            "lines_deleted": _col(r, "lines_deleted"),
            # V2 : breakdown par categorie
            "lines_real_added": _col(r, "lines_real_added"),
            "lines_real_deleted": _col(r, "lines_real_deleted"),
            "lines_seed_added": _col(r, "lines_seed_added"),
            "lines_seed_deleted": _col(r, "lines_seed_deleted"),
            "lines_vendored_added": _col(r, "lines_vendored_added"),
            "lines_vendored_deleted": _col(r, "lines_vendored_deleted"),
            "lines_generated_added": _col(r, "lines_generated_added"),
            "lines_generated_deleted": _col(r, "lines_generated_deleted"),
            "event_count": r["event_count"],
            "user_msg_count": r["user_msg_count"],
            "cost_estimated": r["cost_estimated"],
        })
    return result
