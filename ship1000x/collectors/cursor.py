"""Collector Cursor — exploit `~/.cursor/ai-tracking/ai-code-tracking.db`.

Cursor stocke deja :
  - `ai_code_hashes` : chaque block de code genere IA avec fileName + timestamp
  - `scored_commits` : commits avec %AI calcule (humanLines/composerLines/v1+v2AiPercentage)

V1 : agregation par jour x projet via classification path sur fileName.
Pas de "temps actif" fiable depuis Cursor — on stocke le signal "AI block produit".
Duration fictive de 60s par block AI (marker, pas mesure de temps).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CURSOR_DB = Path.home() / ".cursor" / "ai-tracking" / "ai-code-tracking.db"
# Marker pour dire "cet event est un proxy, pas une mesure de temps"
AI_BLOCK_MARKER_SEC = 60


def _stable_event_id(day: str, project_id: str | None, source_key: str) -> str:
    raw = f"cursor|{day}|{project_id or 'unk'}|{source_key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Ingestion Cursor.

    Strategie :
      1. Lire ai_code_hashes (sans contenu, juste meta)
      2. Classifier chaque block via fileName path
      3. Agreger par (day, project_id) → 1 event synthetique
      4. Lire scored_commits pour enrichir lines_added/deleted (future)
    """
    from ship1000x.core.privacy import sanitize_event, anonymize_path, is_excluded_path

    stats = {"files_seen": 0, "sessions_ingested": 0, "events_ingested": 0, "skipped": 0}
    exclude_paths = privacy_config.get("exclude_paths", []) or []

    if not CURSOR_DB.exists():
        return stats

    stats["files_seen"] = 1

    # Check idempotence via ingestion_state (on utilise mtime comme offset)
    mtime_key = "ai-code-tracking"
    current_mtime = int(CURSOR_DB.stat().st_mtime)
    last_mtime = storage.get_ingestion_offset("cursor", mtime_key)
    if current_mtime <= last_mtime:
        return stats

    # Clone DB en read-only (safe si Cursor tourne en write)
    try:
        conn = sqlite3.connect(f"file:{CURSOR_DB}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return stats

    try:
        # Agregation par jour x fileName puis classification
        rows = conn.execute(
            """
            SELECT
                DATE(createdAt/1000, 'unixepoch') AS day,
                fileName,
                fileExtension,
                COUNT(*) AS block_count,
                MIN(createdAt) AS first_ts,
                MAX(createdAt) AS last_ts
            FROM ai_code_hashes
            WHERE fileName IS NOT NULL
              AND createdAt > 0
            GROUP BY day, fileName
            """
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return stats

    # Regrouper par (day, project_id)
    aggregation: dict[tuple[str, str | None], dict] = {}

    for row in rows:
        day = row["day"]
        file_name = row["fileName"] or ""
        if is_excluded_path(file_name, exclude_paths):
            stats["skipped"] += 1
            continue

        project_id, conf = classifier.classify_session(paths=[file_name])
        key = (day, project_id)
        if key not in aggregation:
            aggregation[key] = {
                "day": day,
                "project_id": project_id,
                "project_conf": conf,
                "block_count": 0,
                "file_count": 0,
                "extensions": set(),
                "first_ts": row["first_ts"],
                "last_ts": row["last_ts"],
            }
        agg = aggregation[key]
        agg["block_count"] += row["block_count"]
        agg["file_count"] += 1
        if row["fileExtension"]:
            agg["extensions"].add(row["fileExtension"])
        agg["first_ts"] = min(agg["first_ts"], row["first_ts"])
        agg["last_ts"] = max(agg["last_ts"], row["last_ts"])

    # Store un event par (day, project)
    for (day, project_id), agg in aggregation.items():
        started = datetime.fromtimestamp(agg["first_ts"] / 1000, tz=timezone.utc).isoformat()
        ended = datetime.fromtimestamp(agg["last_ts"] / 1000, tz=timezone.utc).isoformat()

        event = {
            "id": _stable_event_id(day, project_id, "ai_blocks"),
            "source": "cursor",
            "event_type": "ai_blocks_daily",
            "started_at": started,
            "ended_at": ended,
            "duration_sec": agg["block_count"] * AI_BLOCK_MARKER_SEC,
            "cwd": None,
            "project_id": project_id,
            "project_conf": agg["project_conf"],
            "tool_or_action": "cursor_ai_block",
            "token_input": 0,
            "token_output": 0,
            "cost_estimated": 0.0,
            "user_msg_type": None,
            "wordcount": 0,
            "confidence_flag": "high" if agg["project_conf"] >= 0.8 else "medium",
            "raw_meta": json.dumps({
                "block_count": agg["block_count"],
                "file_count": agg["file_count"],
                "extensions": sorted(agg["extensions"]),
                "marker_duration": True,  # Flag : duration n'est pas mesure de temps
            }),
        }
        safe = sanitize_event(event)
        storage.upsert_event(safe)
        stats["events_ingested"] += 1

    # Enrichissement via scored_commits (stats commits avec %AI)
    try:
        commit_rows = conn.execute(
            """
            SELECT
                commitHash, commitDate, commitMessage,
                linesAdded, linesDeleted,
                composerLinesAdded, humanLinesAdded,
                v1AiPercentage, v2AiPercentage
            FROM scored_commits
            WHERE commitDate IS NOT NULL
            """
        ).fetchall()
    except sqlite3.Error:
        commit_rows = []

    for c in commit_rows:
        event = {
            "id": "cursor-commit-" + (c["commitHash"] or "")[:16],
            "source": "cursor",
            "event_type": "scored_commit",
            "started_at": c["commitDate"],
            "ended_at": c["commitDate"],
            "duration_sec": 0,
            "cwd": None,
            "project_id": None,  # A enrichir en V2 via git_multi cross-ref
            "project_conf": 0.0,
            "tool_or_action": "cursor_commit",
            "token_input": 0,
            "token_output": 0,
            "cost_estimated": 0.0,
            "user_msg_type": None,
            "wordcount": 0,
            "confidence_flag": "medium",
            "raw_meta": json.dumps({
                "lines_added": c["linesAdded"],
                "lines_deleted": c["linesDeleted"],
                "composer_lines_added": c["composerLinesAdded"],
                "human_lines_added": c["humanLinesAdded"],
                "v1_ai_pct": c["v1AiPercentage"],
                "v2_ai_pct": c["v2AiPercentage"],
            }),
        }
        # Privacy : ne pas stocker commitMessage (peut contenir secrets/paths)
        safe = sanitize_event(event)
        storage.upsert_event(safe)
        stats["events_ingested"] += 1

    conn.close()

    storage.set_ingestion_offset(
        "cursor",
        mtime_key,
        current_mtime,
        datetime.utcnow().isoformat(),
    )
    stats["sessions_ingested"] = len(aggregation)
    return stats
