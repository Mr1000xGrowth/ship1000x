"""Collector for Claude Code statusline drop files.

Reads ``~/.ship1000x/drop/statusline/<YYYY-MM-DD>.jsonl`` written by
:mod:`ship1000x.runtime.statusline_writer` and emits one
``session_live_tick`` event per minute per session — aggregating the
many statusline ticks per minute into a single event so the store
doesn't explode (Claude Code may render the statusline tens of times
per minute).

Wave 3 / Day 1-2 of the SHIP1000X roadmap.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DROP_DIR = Path.home() / ".ship1000x" / "drop" / "statusline"


def _stable_event_id(session_id: str, minute_key: str) -> str:
    raw = f"claude_statusline|{session_id}|{minute_key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _parse_iso_minute(iso: str) -> str | None:
    """Truncate an ISO timestamp to YYYY-MM-DDTHH:MM (minute granularity)."""
    if not iso:
        return None
    return iso[:16] if len(iso) >= 16 else None


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Aggregate statusline ticks into per-minute live events."""
    from ship1000x.core.privacy import anonymize_path, is_excluded_path

    stats = {
        "files_seen": 0,
        "sessions_ingested": 0,
        "events_ingested": 0,
        "skipped": 0,
        "ticks_aggregated": 0,
    }

    if not DROP_DIR.exists():
        return stats

    exclude_paths = privacy_config.get("exclude_paths", []) or []
    last_mtime = storage.get_ingestion_offset("claude_statusline", "drop_mtime_max")
    max_mtime = last_mtime

    for path in sorted(DROP_DIR.glob("*.jsonl")):
        stats["files_seen"] += 1
        try:
            mtime = int(path.stat().st_mtime)
        except OSError:
            continue
        if mtime <= last_mtime:
            continue
        if mtime > max_mtime:
            max_mtime = mtime

        # Aggregate ticks by (session_id, minute).
        # bucket = {(session_id, minute): {ticks, model, cwd, ctx_max, compact_seen}}
        bucket: dict[tuple[str, str], dict[str, Any]] = defaultdict(
            lambda: {
                "ticks": 0,
                "model_counts": {},
                "cwds": set(),
                "ctx_max": 0.0,
                "compact_seen": False,
            }
        )

        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue

                session_id = str(record.get("session_id") or "").strip()
                if not session_id:
                    continue
                ts_minute = _parse_iso_minute(str(record.get("captured_at") or ""))
                if not ts_minute:
                    continue

                cwd = str(record.get("cwd") or "")
                if cwd and is_excluded_path(cwd, exclude_paths):
                    stats["skipped"] += 1
                    continue

                key = (session_id, ts_minute)
                b = bucket[key]
                b["ticks"] += 1
                stats["ticks_aggregated"] += 1

                model = str(record.get("model") or "").strip()
                if model:
                    b["model_counts"][model] = b["model_counts"].get(model, 0) + 1

                if cwd:
                    # Store only the anonymised path basename in the
                    # aggregation. Privacy of full cwd is enforced by
                    # sanitize_event at storage time anyway.
                    b["cwds"].add(anonymize_path(cwd))

                ctx = record.get("context") or {}
                if isinstance(ctx, dict):
                    pct = ctx.get("used_percent")
                    if isinstance(pct, (int, float)) and pct > b["ctx_max"]:
                        b["ctx_max"] = float(pct)

                compact = record.get("compact")
                if isinstance(compact, dict):
                    if compact.get("triggered") or compact.get("active"):
                        b["compact_seen"] = True

        except OSError:
            continue

        # Emit one event per (session, minute) bucket.
        for (session_id, minute_key), b in bucket.items():
            # Pick dominant model in this bucket.
            dominant_model = None
            if b["model_counts"]:
                dominant_model = max(
                    b["model_counts"].items(), key=lambda kv: kv[1]
                )[0]

            # Project classification via observed cwd(s).
            cwd_list = sorted(b["cwds"])
            if cwd_list:
                project_id, conf = classifier.classify_session(
                    paths=cwd_list,
                )
            else:
                project_id, conf = "unclassified", 0.0

            started = minute_key + ":00+00:00"
            event_id = _stable_event_id(session_id, minute_key)
            event = {
                "id": event_id,
                "source": "claude_statusline",
                "event_type": "session_live_tick",
                "started_at": started,
                "ended_at": started,
                "duration_sec": 60,
                "wall_clock_sec": 60,
                "cwd": cwd_list[0] if cwd_list else None,
                "project_id": project_id,
                "project_conf": conf,
                "tool_or_action": "statusline_tick",
                "token_input": 0,
                "token_output": 0,
                "cost_estimated": 0.0,
                "user_msg_type": None,
                "wordcount": 0,
                # A1-cont: a statusline tick measures no tokens and no cost, so
                # its confidence must not ride on project attribution (kept in
                # project_conf). No hard measured dimension → medium, never high.
                "confidence_flag": "medium",
                "raw_meta": json.dumps({
                    "session_id": session_id[:36],
                    "model": dominant_model,
                    "statusline_tick_count": b["ticks"],
                    "context_used_percent_max": round(b["ctx_max"], 2),
                    "compact_triggered": bool(b["compact_seen"]),
                }),
            }
            storage.upsert_event(event, replace=True)
            stats["events_ingested"] += 1
        stats["sessions_ingested"] += len(
            {key[0] for key in bucket}
        )

    if max_mtime > last_mtime:
        storage.set_ingestion_offset(
            "claude_statusline",
            "drop_mtime_max",
            max_mtime,
            datetime.now(timezone.utc).isoformat(),
        )

    return stats
