"""Collector Codex SQLite — enrichissement via ~/.codex/state_5.sqlite.

Table `threads` expose des metadata non disponibles dans les rollout JSONL :
- title (= premier prompt user, utile pour classification keyword)
- git_origin_url (classification par remote match projects.yaml)
- tokens_used (cumul par thread, plus precis que sommer les events)
- cli_version, source, model_provider

Table `thread_spawn_edges` (parent_thread_id/child_thread_id) expose la
structure parent → sous-agent : un thread Codex peut spawner d'autres threads
("sub-agents", ex. guardian/explorer/worker). Sans cette table, ship1000x ne
distingue pas un thread "racine" (lance par l'humain) d'un thread sous-agent
(lance par un autre thread) — cf memory
project-activity-tracking-ship-sight1000x, "agentic work unit".

Ne cree PAS d'events (les JSONL de codex.py couvrent deja). Ce collector
enrichit les events existants : (1) classification par git_origin_url quand
cwd n'a pas matche, (2) role agentic_unit (root vs subagent) + lien parent.
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


def list_thread_spawn_edges(db_path: Path = CODEX_STATE_DB) -> list[dict[str, Any]]:
    """Liste les liens parent → sous-agent (`thread_spawn_edges`)."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []

    try:
        rows = conn.execute(
            "SELECT parent_thread_id, child_thread_id, status FROM thread_spawn_edges"
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return []
    conn.close()
    return [dict(r) for r in rows]


def compute_thread_roles(
    threads: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Classe chaque thread comme "root" (lance par l'humain) ou "subagent".

    Returns un dict thread_id -> {"role": "root"|"subagent",
    "parent_thread_id": str|None}. Un thread absent de `edges` en tant
    qu'enfant est "root". Un thread ne figurant dans aucune des deux listes
    n'apparait pas dans le resultat (appelant doit fallback sur "root").
    """
    parent_by_child: dict[str, str] = {
        e["child_thread_id"]: e["parent_thread_id"]
        for e in edges
        if e.get("child_thread_id") and e.get("parent_thread_id")
    }
    roles: dict[str, dict[str, Any]] = {}
    for t in threads:
        tid = t.get("id")
        if not tid:
            continue
        parent = parent_by_child.get(tid)
        roles[tid] = {
            "role": "subagent" if parent else "root",
            "parent_thread_id": parent,
        }
    return roles


def enrich_codex_events(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Enrichit les events codex existants avec les metadata state_5.

    Reclassifie les events sans project_id clair en utilisant git_origin_url
    du thread correspondant, et tague chaque event matche avec son
    "agentic_unit" (role root/subagent + parent_thread_id via
    `thread_spawn_edges`) — sur TOUS les events codex, pas seulement ceux mal
    classifies, puisque le role n'a rien a voir avec la classification projet.
    """

    stats = {
        "threads_seen": 0,
        "events_enriched": 0,
        "reclassified": 0,
        "root_threads_seen": 0,
        "subagent_threads_seen": 0,
    }

    threads = list_threads()
    if not threads:
        return stats
    stats["threads_seen"] = len(threads)

    edges = list_thread_spawn_edges()
    roles = compute_thread_roles(threads, edges)
    for role_info in roles.values():
        if role_info["role"] == "subagent":
            stats["subagent_threads_seen"] += 1
        else:
            stats["root_threads_seen"] += 1

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

    # Tous les events codex : le role agentic_unit s'applique independamment
    # du niveau de confiance de classification projet.
    event_rows = storage.query(
        """
        SELECT id, cwd, project_id, project_conf, raw_meta
        FROM events
        WHERE source = 'codex'
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

            # Reclassifie si le projet actuel est peu fiable, via
            # git_origin_url OU les mots-cles du titre. `title` (souvent un
            # resume genere) ET `first_user_message` (le prompt brut) sont
            # combines pour le keyword match : mesure 2026-07-01 sur un
            # projet reel (Sabaca, 77 threads matches par mot-cle) — 26 des
            # 77 (34%) n'avaient le mot-cle QUE dans first_user_message, pas
            # dans title. Le titre seul suffit : beaucoup de threads Codex
            # n'ont qu'un cwd generique ("~/Developer", pas le vrai
            # sous-dossier — meme defaut que le bug codex_macapp deja
            # documente) et aucun git_origin_url capture, donc exiger
            # git_origin_url ici empechait toute reclassification pour ces
            # threads.
            new_project = ev["project_id"]
            new_conf = ev["project_conf"] or 0.0
            if new_conf < 0.8 or not new_project:
                combined_title = " ".join(
                    filter(None, [thread.get("title"), thread.get("first_user_message")])
                )
                pid, conf = classifier.classify_session(
                    cwd=thread.get("cwd"),
                    git_remote=thread.get("git_origin_url"),
                    title=combined_title or None,
                )
                if pid and conf > new_conf:
                    new_project = pid
                    new_conf = conf
                    stats["reclassified"] += 1

            thread_id = thread.get("id")
            role_info = roles.get(thread_id, {"role": "root", "parent_thread_id": None})
            meta["agentic_unit"] = {
                "kind": "codex_subagent" if role_info["role"] == "subagent" else "codex_thread",
                "thread_id": thread_id,
                "parent_thread_id": role_info.get("parent_thread_id"),
            }

            # Update l'event en place (classification + tag agentic_unit)
            c.execute(
                """UPDATE events SET project_id = ?, project_conf = ?, raw_meta = ?
                   WHERE id = ?""",
                (new_project, new_conf, json.dumps(meta), ev["id"]),
            )
            stats["events_enriched"] += 1

    return stats


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Point d'entree pour l'ingestion : enrichit les codex events existants."""
    return enrich_codex_events(storage, classifier, privacy_config)
