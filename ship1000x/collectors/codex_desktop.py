"""Collector Codex Desktop — exploit `~/.codex/state_5.sqlite` table `logs`.

Codex Desktop ne stocke PAS les conversations (c'est cote serveur OpenAI),
MAIS il log les SSE events + commandes executees dans une table `logs`
avec timestamps + process_uuid (= session).

Structure logs pertinente :
  - ts (unix seconds) + ts_nanos
  - target = "codex_api::sse::responses"
  - message = contient `SSE event: {"type":"response.created", ...}` etc.
  - process_uuid = identifiant de session

On extrait :
  - 1 session = 1 process_uuid
  - turns = nb de "response.created"
  - timestamps turns = les ts des "response.created" + "response.completed"
    -> active_sec via intervalles ponderes (idem claude_code)
  - paths = extraction regex des /Users/<username>/... dans les messages
    -> classification project_id + split multi-projets
  - wall_clock_sec = last_ts - first_ts par process_uuid
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CODEX_STATE_DB = Path.home() / ".codex" / "state_5.sqlite"

MAX_ACTIVE_SEC_PER_SESSION = 12 * 3600
ACTIVE_PAUSE_THRESHOLD_SEC = 5 * 60
# Gap au-dela duquel on considere que la session s'est mise en pause (l'app
# Codex Desktop reste ouverte entre les usages, les process_uuid peuvent
# durer plusieurs jours sans activite).
SEGMENT_GAP_SEC = 30 * 60

# Regex path extraction : garde les chemins absolus sous /Users/...
_PATH_RE = re.compile(r"/Users/[a-zA-Z0-9_-]+/[\w\-./]+")


def _extract_project_markers(message: str, markers: list[str]) -> list[str]:
    """Cherche les markers distinctifs (ids + segments projets) dans un message.

    Utile quand les paths sont fragmentes en deltas (Codex Desktop SSE events
    "response.function_call_arguments.delta"). Une mention d'un id projet
    (ex: "my-backend" ou "my-app") suffit a classifier l'event.
    """
    if not message or not markers:
        return []
    msg_lower = message.lower()
    found = []
    for m in markers:
        if len(m) >= 5 and m.lower() in msg_lower:
            # On conserve le marker matche pour pouvoir le passer au classifier
            found.append(m)
    return found


def _stable_event_id(process_uuid: str, day_key: str, project_id: str) -> str:
    raw = f"codex_desktop|{process_uuid}|{day_key}|{project_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _segmented_wall_clock(timestamps: list[int]) -> int:
    """Somme des intervalles entre events < SEGMENT_GAP_SEC (30 min).

    Quand Codex Desktop reste ouvert plusieurs jours sans activite, le wall
    brut (last-first) est trompeur. On ne compte que les intervalles "vivants".
    """
    if len(timestamps) < 2:
        return 0
    ts = sorted(timestamps)
    total = 0
    for i in range(1, len(ts)):
        delta = ts[i] - ts[i - 1]
        if 0 < delta <= SEGMENT_GAP_SEC:
            total += delta
    return total


def _estimate_active_sec(user_timestamps: list[int]) -> int:
    """Meme regle que claude_code : intervalles ponderes."""
    if len(user_timestamps) < 2:
        return 0
    total = 0.0
    prev = None
    for ts in sorted(user_timestamps):
        if prev is None:
            prev = ts
            continue
        delta = ts - prev
        if delta <= 0:
            pass
        elif delta <= ACTIVE_PAUSE_THRESHOLD_SEC:
            total += delta
        elif delta <= ACTIVE_PAUSE_THRESHOLD_SEC * 3:
            total += delta * 0.5
        elif delta <= ACTIVE_PAUSE_THRESHOLD_SEC * 6:
            total += delta * 0.25
        prev = ts
    return int(total)


def _extract_paths(message: str) -> list[str]:
    if not message or "/Users/" not in message:
        return []
    # Cap la longueur pour eviter les regex qui explosent sur de gros dumps JSON
    clipped = message[:10000]
    return [m.group(0) for m in _PATH_RE.finditer(clipped)]


def _daily_split(process_logs: list[dict], classifier, markers: list[str] | None = None) -> dict[str, dict]:
    """Agrege les logs d'une session par jour UTC.

    On collecte TOUS les timestamps des events SSE (pas juste response.created)
    pour que active_sec reflete bien l'activite continue. Un intervalle court
    entre events = utilisateur en train d'interagir avec l'app.
    """
    from collections import defaultdict
    daily: dict[str, dict] = defaultdict(lambda: {
        "turns": 0,
        "tool_calls": 0,
        "paths": [],
        "first_ts": None,
        "last_ts": None,
        "event_timestamps": [],
    })

    for row in process_logs:
        ts = row["ts"]
        msg = row["message"] or ""
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        d = daily[day]
        if d["first_ts"] is None:
            d["first_ts"] = ts
        d["last_ts"] = ts

        # Tous les events significatifs participent au calcul d'active_sec
        # (response.created/completed/in_progress, function_call.*, etc.)
        d["event_timestamps"].append(ts)

        if "response.created" in msg:
            d["turns"] += 1
        if "function_call_arguments.done" in msg or "function_call.done" in msg:
            d["tool_calls"] += 1

        paths = _extract_paths(msg)
        if paths:
            d["paths"].extend(paths)
        # Fallback : si aucun path absolu dans le message, cherche les markers
        # projets (utile pour les SSE event deltas fragmentes).
        if not paths and markers:
            marker_hits = _extract_project_markers(msg, markers)
            d["paths"].extend(marker_hits)

    return daily


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Ingestion Codex Desktop via state_5.sqlite logs.

    Idempotent : ingestion_state offset = max(ts) deja ingere.
    """
    from ship1000x.core.privacy import is_excluded_path, sanitize_event

    stats = {"files_seen": 0, "sessions_ingested": 0, "events_ingested": 0, "skipped": 0}
    if not CODEX_STATE_DB.exists():
        return stats

    stats["files_seen"] = 1
    try:
        con = sqlite3.connect(f"file:{CODEX_STATE_DB}?mode=ro&immutable=1", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return stats

    # Verifie que la table existe
    has_logs = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='logs'"
    ).fetchone()
    if not has_logs:
        con.close()
        return stats

    last_ts = storage.get_ingestion_offset("codex_desktop", "logs_ts_max")
    exclude_paths = privacy_config.get("exclude_paths", []) or []

    # DEDUP : le collector codex_macapp (~/Library/Logs/com.openai.codex/) est
    # plus precis (turn/start = vrais prompts user) que les logs SSE du
    # state_5.sqlite. Quand les 2 sources couvrent le meme jour ET le meme
    # projet, on skip l'event codex_desktop pour eviter le double compte.
    # Pas de linkage UUID possible entre les 2 sources (session_uuid frontend
    # vs process_uuid backend), donc on dedup au niveau (day, project).
    macapp_covered: set[tuple[str, str]] = set()
    try:
        rows_macapp = storage.query(
            """
            SELECT DISTINCT substr(started_at, 1, 10) AS day, project_id
            FROM events
            WHERE source = 'codex_macapp'
            """
        )
        for r in rows_macapp:
            macapp_covered.add((r["day"], r["project_id"]))
    except Exception:
        # Si le query echoue (schema different, etc.), on continue sans dedup
        pass

    # Construit la liste des markers de projet (ids + segments distinctifs) pour
    # detecter les mentions dans les messages Codex Desktop meme quand les paths
    # sont fragmentes (SSE function_call_arguments.delta).
    project_markers: list[str] = []
    for rule in classifier.rules:
        if rule.id not in project_markers:
            project_markers.append(rule.id)
        for pattern in rule.paths:
            for segment in pattern.split("/"):
                if segment and "*" not in segment and len(segment) >= 5:
                    if segment not in project_markers:
                        project_markers.append(segment)

    # On ne lit que les logs pertinents (SSE events avec contenu) pour limiter
    # le volume. On exclut les traces debug pure (opentelemetry, hyper_util).
    rows = con.execute(
        """
        SELECT ts, process_uuid, message
        FROM logs
        WHERE ts > ?
          AND process_uuid IS NOT NULL
          AND (
              message LIKE '%response.created%'
              OR message LIKE '%response.completed%'
              OR message LIKE '%function_call%'
              OR message LIKE '/Users/%'
              OR message LIKE '%delta%'
          )
        ORDER BY process_uuid, ts
        """,
        (last_ts,),
    ).fetchall()

    # Group par process_uuid = sessions
    from collections import defaultdict
    sessions: dict[str, list[dict]] = defaultdict(list)
    max_ts = last_ts
    for r in rows:
        sessions[r["process_uuid"]].append({"ts": r["ts"], "message": r["message"]})
        if r["ts"] > max_ts:
            max_ts = r["ts"]

    for process_uuid, logs in sessions.items():
        if not logs:
            continue
        daily = _daily_split(logs, classifier, markers=project_markers)

        for day_key, d in daily.items():
            if not d["first_ts"]:
                continue

            # Filter exclude_paths
            paths_filtered = [p for p in d["paths"] if not is_excluded_path(p, exclude_paths)]

            # Classification multi-projets via paths
            distribution = classifier.paths_distribution(paths_filtered)
            if not distribution:
                primary, _conf = classifier.classify_session(paths=paths_filtered)
                distribution = {primary or "unclassified": 1.0}

            # Active sec via tous les timestamps events (densite elevee)
            active_sec = _estimate_active_sec(d["event_timestamps"])
            # Wall-clock segmente : exclut les longs gaps d'inactivite.
            # Codex Desktop reste ouvert H24, last-first peut faire 70h sans
            # activite reelle. On ne garde que les intervalles < 30 min.
            wall_sec = _segmented_wall_clock(d["event_timestamps"])

            # Floor par turns : chaque turn user = au minimum 60s de reflexion
            # reelle (preparer prompt + lire response). C'est un minimum
            # absolu, independant de la granularite des logs SSE.
            turns_floor = d["turns"] * 60

            # Floor wall-clock : les logs Codex Desktop sont echantillonnes
            # par le serveur OpenAI, beaucoup plus sparse que les JSONL
            # Claude Code. Sur une session avec >= 3 turns reels, on plancher
            # a 50% du wall_clock segmente (ratio reviewed apres feedback
            # user 2026-04-20 : 30% sous-estimait massivement).
            if d["turns"] >= 3 and wall_sec > 0:
                wall_floor = int(wall_sec * 0.50)
                active_sec = max(active_sec, wall_floor)

            # Applique le floor par turns (garantit qu'on ne rate pas
            # l'activite reelle meme si _estimate_active_sec sous-sample).
            active_sec = max(active_sec, turns_floor)

            # Cap : ne depasse jamais le wall_clock segmente (physiquement
            # impossible d'etre actif plus longtemps que la session elle-meme).
            # Ancien cap a 60% trop restrictif : un user intensif peut etre
            # actif 80-90% du wall_clock segmente.
            if wall_sec > 0:
                active_sec = min(active_sec, wall_sec)
            if active_sec > MAX_ACTIVE_SEC_PER_SESSION:
                active_sec = MAX_ACTIVE_SEC_PER_SESSION

            started_iso = datetime.fromtimestamp(d["first_ts"], tz=timezone.utc).isoformat()
            ended_iso = datetime.fromtimestamp(d["last_ts"], tz=timezone.utc).isoformat()

            for project_id, ratio in distribution.items():
                # Skip si codex_macapp couvre deja ce (day, project) — evite
                # le double compte quand Codex.app native tourne sur la meme
                # machine (cf. bloc DEDUP en debut de collect()).
                if (day_key, project_id) in macapp_covered:
                    stats["skipped"] += 1
                    continue

                event = {
                    "id": _stable_event_id(process_uuid, day_key, project_id),
                    "source": "codex_desktop",
                    "event_type": "session_day",
                    "started_at": started_iso,
                    "ended_at": ended_iso,
                    "duration_sec": int(active_sec * ratio),
                    "wall_clock_sec": int(wall_sec * ratio),
                    "cwd": None,
                    "project_id": project_id,
                    "project_conf": 0.80 if paths_filtered else 0.50,
                    "tool_or_action": "codex_desktop_session",
                    "token_input": 0,
                    "token_output": 0,
                    # Codex Desktop : logs SSE dans state_5.sqlite, tokens
                    # non exposes. Estimation par heure active (meme base que
                    # codex_macapp : ~10$/h = GPT-5 API usage typique).
                    "cost_estimated": (active_sec * ratio / 3600) * 10.0,
                    "user_msg_type": None,
                    "wordcount": 0,
                    "confidence_flag": "high" if paths_filtered and ratio >= 0.5 else "medium",
                    "raw_meta": json.dumps({
                        "process_uuid": process_uuid,
                        "turn_count": d["turns"],
                        "tool_call_count": d["tool_calls"],
                        "paths_sampled": len(paths_filtered),
                        "split_ratio": round(ratio, 3),
                    }),
                }
                safe = sanitize_event(event)
                storage.upsert_event(safe, replace=True)
                stats["events_ingested"] += 1
            stats["sessions_ingested"] += 1

    con.close()

    if max_ts > last_ts:
        storage.set_ingestion_offset(
            "codex_desktop", "logs_ts_max",
            max_ts, datetime.utcnow().isoformat(),
        )

    return stats
