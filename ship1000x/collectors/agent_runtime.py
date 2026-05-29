"""Collector for the ABTop-style runtime daemon drop files.

Reads ``~/.ship1000x/drop/watch/<YYYY-MM-DD>.jsonl`` written by
:mod:`ship1000x.runtime.watch_daemon` and emits one
``agent_runtime_tick`` event per (process_name, minute) — aggregating
the many polling ticks within a minute into a single event so the
store doesn't explode (default polling is 30 s, so two ticks per
minute per process, but a tighter ``--interval`` can produce many
more).

Wave 3 / Day 3-5 of the SHIP1000X roadmap.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DROP_DIR = Path.home() / ".ship1000x" / "drop" / "watch"


def _stable_event_id(process_name: str, minute_key: str) -> str:
    raw = f"agent_runtime|{process_name}|{minute_key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _parse_iso_minute(iso: str) -> str | None:
    if not iso:
        return None
    return iso[:16] if len(iso) >= 16 else None


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Aggregate runtime ticks into per-minute agent_runtime_tick events."""
    # classifier and privacy_config are part of the SourceCollector
    # contract but not used here — process names don't have a project
    # affinity (a daemon may serve many projects), and the daemon
    # already applies the privacy whitelist at write time.
    _ = classifier
    _ = privacy_config

    stats = {
        "files_seen": 0,
        "sessions_ingested": 0,
        "events_ingested": 0,
        "skipped": 0,
        "ticks_aggregated": 0,
    }

    if not DROP_DIR.exists():
        return stats

    last_mtime = storage.get_ingestion_offset("agent_runtime", "drop_mtime_max")
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

        # bucket = {(process_name, minute): {ticks, max_pid_count,
        #           ports (set), mcp_seen, last_seen_iso}}
        bucket: dict[tuple[str, str], dict[str, Any]] = defaultdict(
            lambda: {
                "ticks": 0,
                "max_pid_count": 0,
                "ports": set(),
                "mcp_seen": False,
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

                ts_minute = _parse_iso_minute(str(record.get("captured_at") or ""))
                if not ts_minute:
                    continue

                processes = record.get("processes") or []
                if not isinstance(processes, list):
                    continue

                for proc in processes:
                    if not isinstance(proc, dict):
                        continue
                    name = str(proc.get("name") or "").strip().lower()
                    if not name:
                        continue
                    key = (name, ts_minute)
                    b = bucket[key]
                    b["ticks"] += 1
                    stats["ticks_aggregated"] += 1
                    pid_count = proc.get("pid_count")
                    if isinstance(pid_count, int) and pid_count > b["max_pid_count"]:
                        b["max_pid_count"] = pid_count
                    ports = proc.get("listening_ports") or []
                    if isinstance(ports, list):
                        for port in ports:
                            if isinstance(port, int):
                                b["ports"].add(port)
                    if proc.get("mcp_likely"):
                        b["mcp_seen"] = True
        except OSError:
            continue

        # Emit one event per (process_name, minute) bucket.
        seen_minutes: set[str] = set()
        for (process_name, minute_key), b in bucket.items():
            started = minute_key + ":00+00:00"
            event_id = _stable_event_id(process_name, minute_key)
            ports_sorted = sorted(b["ports"])
            event = {
                "id": event_id,
                "source": "agent_runtime",
                "event_type": "agent_runtime_tick",
                "started_at": started,
                "ended_at": started,
                "duration_sec": 60,
                "wall_clock_sec": 60,
                "cwd": None,
                "project_id": "unclassified",
                "project_conf": 0.0,
                "tool_or_action": "runtime_observed",
                "token_input": 0,
                "token_output": 0,
                "cost_estimated": 0.0,
                "user_msg_type": None,
                "wordcount": 0,
                "confidence_flag": "medium",
                "raw_meta": json.dumps({
                    "process_name": process_name,
                    "process_tick_count": b["ticks"],
                    "max_concurrent_processes": b["max_pid_count"],
                    "open_ports_count": len(ports_sorted),
                    "mcp_servers_detected": 1 if b["mcp_seen"] else 0,
                }),
            }
            storage.upsert_event(event, replace=True)
            stats["events_ingested"] += 1
            seen_minutes.add(minute_key)
        stats["sessions_ingested"] += len(seen_minutes)

    if max_mtime > last_mtime:
        storage.set_ingestion_offset(
            "agent_runtime",
            "drop_mtime_max",
            max_mtime,
            datetime.now(timezone.utc).isoformat(),
        )

    return stats
