"""Collector Cline — extension saoudrizwan.claude-dev pour Cursor/VS Code.

Chaque "tache" Cline vit dans :
    ~/Library/Application Support/Cursor/User/globalStorage/
    saoudrizwan.claude-dev/tasks/<task_id>/
        - task_metadata.json       : files_in_context + model_usage
        - api_conversation_history.json : messages API bruts
        - ui_messages.json         : format UI rendu

task_id = Unix ms timestamp de creation.

On extrait par task :
  - project_id via tool_paths (files_in_context[].path)
  - duration_sec via intervalles entre messages user API
  - wall_clock_sec = dernier ts - premier ts
  - model + mode (act | plan)
  - cost approx via tokens si presents

Privacy : aucun contenu de prompt/response stocke. Seuls metadata, paths
anonymises, counts.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CLINE_TASKS_DIR = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Cursor"
    / "User"
    / "globalStorage"
    / "saoudrizwan.claude-dev"
    / "tasks"
)

# Cap session (anti-aberration comme claude_code)
MAX_ACTIVE_SEC_PER_TASK = 12 * 3600
# Ponderation intervalles USER (cf claude_code regle)
ACTIVE_PAUSE_THRESHOLD_SEC = 5 * 60


def _stable_event_id(task_id: str, project_id: str) -> str:
    raw = f"cline|{task_id}|{project_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _count_user_messages(api_history: list[dict]) -> int:
    return sum(1 for m in api_history if m.get("role") == "user")


def _estimate_active_sec(user_timestamps_ms: list[int]) -> int:
    """Meme logique que claude_code : intervalles user ponderes."""
    if len(user_timestamps_ms) < 2:
        return 0
    total = 0.0
    prev = None
    for ts_ms in sorted(user_timestamps_ms):
        if prev is None:
            prev = ts_ms
            continue
        delta = (ts_ms - prev) / 1000.0
        if delta <= 0:
            pass
        elif delta <= ACTIVE_PAUSE_THRESHOLD_SEC:
            total += delta
        elif delta <= ACTIVE_PAUSE_THRESHOLD_SEC * 3:
            total += delta * 0.5
        elif delta <= ACTIVE_PAUSE_THRESHOLD_SEC * 6:
            total += delta * 0.25
        prev = ts_ms
    return int(total)


def _parse_task(task_dir: Path) -> dict[str, Any] | None:
    """Parse une tache Cline. Retourne dict ou None si invalide."""
    meta_file = task_dir / "task_metadata.json"
    api_file = task_dir / "api_conversation_history.json"
    ui_file = task_dir / "ui_messages.json"
    if not meta_file.exists():
        return None

    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    # task_id = nom du dossier = unix ms timestamp
    try:
        task_id = task_dir.name
        task_start_ms = int(task_id)
    except ValueError:
        return None

    files_in_context = meta.get("files_in_context", []) or []
    model_usage = meta.get("model_usage", []) or []

    # Collecte tous les timestamps connus
    all_timestamps = [task_start_ms]
    for u in model_usage:
        if ts := u.get("ts"):
            all_timestamps.append(ts)
    for f in files_in_context:
        for k in ("cline_read_date", "cline_edit_date", "user_edit_date"):
            if ts := f.get(k):
                all_timestamps.append(ts)

    tool_paths = [f["path"] for f in files_in_context if f.get("path")]

    # User messages timestamps : les UI messages ont un "ts"
    user_message_timestamps: list[int] = []
    if ui_file.exists():
        try:
            ui = json.loads(ui_file.read_text(encoding="utf-8"))
            for msg in ui:
                # Cline UI messages : "type": "say" ou "ask", avec "ts"
                if msg.get("type") in ("say", "ask") and msg.get("say") == "user_feedback":
                    if ts := msg.get("ts"):
                        user_message_timestamps.append(ts)
                elif msg.get("type") == "ask":
                    if ts := msg.get("ts"):
                        user_message_timestamps.append(ts)
        except (OSError, json.JSONDecodeError):
            pass

    # API conversation : comptes messages
    api_history = []
    if api_file.exists():
        try:
            api_history = json.loads(api_file.read_text(encoding="utf-8")) or []
        except (OSError, json.JSONDecodeError):
            api_history = []

    first_ts_ms = min(all_timestamps) if all_timestamps else task_start_ms
    last_ts_ms = max(all_timestamps) if all_timestamps else task_start_ms

    active_sec = _estimate_active_sec(user_message_timestamps)
    # Si on n'a pas de timestamps user (UI absent), fallback sur wall-clock capte a 50%
    if active_sec == 0 and last_ts_ms > first_ts_ms:
        wc = (last_ts_ms - first_ts_ms) / 1000
        active_sec = int(min(wc * 0.5, MAX_ACTIVE_SEC_PER_TASK))
    if active_sec > MAX_ACTIVE_SEC_PER_TASK:
        active_sec = MAX_ACTIVE_SEC_PER_TASK

    wall_clock_sec = max(0, (last_ts_ms - first_ts_ms) // 1000)

    # Model dominant (le premier est souvent le plus utilise)
    model = "unknown"
    mode = "unknown"
    if model_usage:
        model = model_usage[0].get("model_id", "unknown")
        mode = model_usage[0].get("mode", "unknown")

    return {
        "task_id": task_id,
        "started_at": datetime.fromtimestamp(first_ts_ms / 1000, tz=timezone.utc).isoformat(),
        "ended_at": datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc).isoformat(),
        "active_sec": active_sec,
        "wall_clock_sec": wall_clock_sec,
        "tool_paths": tool_paths,
        "user_msg_count": _count_user_messages(api_history),
        "api_turn_count": len(api_history),
        "model": model,
        "mode": mode,
        "files_touched": len({f["path"] for f in files_in_context if f.get("path")}),
    }


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Ingestion Cline. Idempotent via ingestion_state sur task_id."""
    from ship1000x.core.privacy import sanitize_event, is_excluded_path

    stats = {"files_seen": 0, "sessions_ingested": 0, "events_ingested": 0, "skipped": 0}
    if not CLINE_TASKS_DIR.exists():
        return stats

    exclude_paths = privacy_config.get("exclude_paths", []) or []

    for task_dir in CLINE_TASKS_DIR.iterdir():
        if not task_dir.is_dir():
            continue
        stats["files_seen"] += 1

        # Idempotence : offset = mtime max des 3 fichiers
        try:
            mtimes = [
                (task_dir / fn).stat().st_mtime
                for fn in ("task_metadata.json", "api_conversation_history.json", "ui_messages.json")
                if (task_dir / fn).exists()
            ]
            if not mtimes:
                continue
            current_mtime = int(max(mtimes))
        except OSError:
            continue

        last_mtime = storage.get_ingestion_offset("cline", task_dir.name)
        if current_mtime <= last_mtime:
            continue

        parsed = _parse_task(task_dir)
        if parsed is None:
            continue

        # Filtrage exclude_paths
        paths = parsed["tool_paths"]
        paths = [p for p in paths if not is_excluded_path(p, exclude_paths)]
        if not paths:
            stats["skipped"] += 1
            continue

        # Split multi-projets
        distribution = classifier.paths_distribution(paths)
        if not distribution:
            primary, conf = classifier.classify_session(paths=paths)
            distribution = {primary or "unclassified": 1.0}
            conf_used = conf
        else:
            conf_used = 0.80

        for project_id, ratio in distribution.items():
            event = {
                "id": _stable_event_id(parsed["task_id"], project_id),
                "source": "cline",
                "event_type": "cline_task",
                "started_at": parsed["started_at"],
                "ended_at": parsed["ended_at"],
                "duration_sec": int(parsed["active_sec"] * ratio),
                "wall_clock_sec": int(parsed["wall_clock_sec"] * ratio),
                "cwd": None,
                "project_id": project_id,
                "project_conf": conf_used,
                "tool_or_action": f"cline_{parsed['mode']}",
                "token_input": 0,
                "token_output": 0,
                "cost_estimated": 0.0,
                "user_msg_type": None,
                "wordcount": 0,
                "confidence_flag": "high" if conf_used >= 0.8 else "medium",
                "raw_meta": json.dumps({
                    "task_id": parsed["task_id"],
                    "model": parsed["model"],
                    "mode": parsed["mode"],
                    "files_touched": parsed["files_touched"],
                    "api_turn_count": parsed["api_turn_count"],
                    "user_msg_count": parsed["user_msg_count"],
                    "split_ratio": round(ratio, 3),
                }),
            }
            safe = sanitize_event(event)
            storage.upsert_event(safe, replace=True)
            stats["events_ingested"] += 1
        stats["sessions_ingested"] += 1

        storage.set_ingestion_offset(
            "cline", task_dir.name,
            current_mtime, datetime.utcnow().isoformat(),
        )

    return stats
