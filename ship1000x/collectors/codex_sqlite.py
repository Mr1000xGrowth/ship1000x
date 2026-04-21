"""Collector Codex SQLite — enrichissement via ~/.codex/state_5.sqlite.

Table `threads` expose des metadata non disponibles dans les rollout JSONL :
- title (= premier prompt user, utile pour classification keyword)
- git_origin_url (classification par remote match projects.yaml)
- tokens_used (cumul par thread, plus precis que sommer les events)
- cli_version, source, model_provider

Ne cree PAS d'events (les JSONL de codex.py couvrent deja). Ce collector
enrichit les events existants en leur ajoutant la classification par
git_origin_url quand cwd n'a pas matche.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

CODEX_STATE_DB = Path.home() / ".codex" / "state_5.sqlite"


def list_threads(db_path: Path = CODEX_STATE_DB) -> list[dict[str, Any]]:
    """Liste tous les threads Codex avec leur metadata."""
    if not db_path.exists():
        return []
    try:
        # Ouverture read-only immutable pour eviter conflit avec Codex actif
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []

    try:
        rows = conn.execute(
            """
            SELECT id, rollout_path, created_at, updated_at,
                   source, model_provider, cwd, title, tokens_used,
                   git_sha, git_branch, git_origin_url, cli_version,
                   first_user_message, archived
            FROM threads
            ORDER BY updated_at DESC
            """
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return []
    conn.close()
    return [dict(r) for r in rows]


def enrich_codex_events(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Enrichit les events codex existants avec les metadata state_5.

    Reclassifie les events sans project_id en utilisant git_origin_url
    du thread correspondant. Met aussi a jour tokens si la DB a une
    valeur plus fiable que l'agregation JSONL.
    """

    stats = {"threads_seen": 0, "events_enriched": 0, "reclassified": 0}

    threads = list_threads()
    if not threads:
        return stats
    stats["threads_seen"] = len(threads)

    # Map : rollout_filename (basename) -> thread metadata
    by_rollout: dict[str, dict[str, Any]] = {}
    for t in threads:
        rp = t.get("rollout_path") or ""
        if rp:
            # rollout_path = /path/to/rollout-xxx.jsonl → basename sans .jsonl
            basename = Path(rp).stem
            by_rollout[basename] = t

    if not by_rollout:
        return stats

    # Cherche les events codex dans la DB dont le session_id match un rollout
    # et qui n'ont pas de project_id clair (conf < 0.8)
    event_rows = storage.query(
        """
        SELECT id, cwd, project_id, project_conf, raw_meta
        FROM events
        WHERE source = 'codex' AND (project_id IS NULL OR project_conf < 0.8)
        """
    )

    with storage.conn() as c:
        for ev in event_rows:
            # Essai de match via session_id dans raw_meta ou cwd
            try:
                meta = json.loads(ev["raw_meta"] or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            session_id = meta.get("session_id") or ""
            thread = None
            # Match par session_id (partiel sur le rollout file basename)
            for basename, t in by_rollout.items():
                if session_id and session_id in basename:
                    thread = t
                    break
            if thread is None:
                continue

            # Reclassifie si git_origin_url match un projet
            new_project = ev["project_id"]
            new_conf = ev["project_conf"] or 0.0
            if thread.get("git_origin_url"):
                pid, conf = classifier.classify_session(
                    cwd=thread.get("cwd"),
                    git_remote=thread.get("git_origin_url"),
                    title=thread.get("title"),
                )
                if pid and conf > new_conf:
                    new_project = pid
                    new_conf = conf
                    stats["reclassified"] += 1

            # Update l'event en place
            c.execute(
                """UPDATE events SET project_id = ?, project_conf = ?
                   WHERE id = ?""",
                (new_project, new_conf, ev["id"]),
            )
            stats["events_enriched"] += 1

    return stats


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Point d'entree pour l'ingestion : enrichit les codex events existants."""
    return enrich_codex_events(storage, classifier, privacy_config)
