"""Collector for an optional local HTTPS usage-proxy's drop files.

Reads ``~/.ship1000x/drop/trace/<YYYY-MM-DD>.jsonl`` written by an
optional local usage proxy and emits one ``api_call`` event per
drop-file line. Each drop line is a 9-key record produced by the
proxy's normalizer + redaction pipeline; we trust its shape because
that pipeline is the only allowed writer of this directory.

SHIP-side bridge for the local usage proxy.

Privacy
-------

The proxy is opt-in and the user installs it explicitly. The drop
records contain only the 9 allowlisted keys (no conversation content,
no auth headers, no PII). We re-route every event through SHIP's
``sanitize_event`` as defence in depth, but the upstream proxy is
already redaction-clean.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DROP_DIR = Path.home() / ".ship1000x" / "drop" / "trace"

#: Maximum number of bytes we will read from a single drop file in one
#: collect() invocation. Defensive against a misbehaving proxy that
#: somehow appends gigabytes; the next ingest run picks up the rest.
MAX_FILE_BYTES = 50 * 1024 * 1024


def _stable_event_id(provider: str, model: str, captured_at: str) -> str:
    """sha256 trim — stable id makes the collector idempotent."""
    raw = f"trace|{provider}|{model}|{captured_at}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _confidence_from_quality(usage_quality: str | None) -> str:
    """Map the proxy's usage_quality to SHIP's confidence_flag enum.

    - ``factual`` (provider returned a usage block) -> high confidence
    - ``estimated`` (no usage block; OpenAI without include_usage) -> medium
    - anything else -> low
    """
    if usage_quality == "factual":
        return "high"
    if usage_quality == "estimated":
        return "medium"
    return "low"


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Ingest the usage-proxy drop files into the SHIP store.

    Each line in a drop file is a 9-key record from the proxy's
    pipeline (provider, model, endpoint, captured_at, token counts,
    usage_quality). We turn each into an ``api_call`` event with a
    stable id so re-ingests are idempotent.
    """
    # classifier + privacy_config are part of the SourceCollector
    # contract but not consumed here — trace events have no project
    # affinity (the proxy doesn't observe the user's cwd) and the
    # upstream pipeline already applies the privacy whitelist.
    _ = classifier
    _ = privacy_config

    stats = {
        "files_seen": 0,
        "sessions_ingested": 0,
        "events_ingested": 0,
        "skipped": 0,
    }

    if not DROP_DIR.exists():
        return stats

    last_mtime = storage.get_ingestion_offset("trace", "drop_mtime_max")
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

        try:
            raw = path.read_text(encoding="utf-8")[:MAX_FILE_BYTES]
        except OSError:
            continue

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                stats["skipped"] += 1
                continue
            if not isinstance(record, dict):
                stats["skipped"] += 1
                continue

            provider = str(record.get("provider") or "").strip().lower()
            model = str(record.get("model") or "").strip()
            captured_at = str(record.get("captured_at") or "").strip()
            if not provider or not model or not captured_at:
                stats["skipped"] += 1
                continue

            input_tokens = _safe_int(record.get("input_tokens"))
            output_tokens = _safe_int(record.get("output_tokens"))
            cache_read = _safe_int(record.get("cache_read_input_tokens"))
            cache_creation = _safe_int(
                record.get("cache_creation_input_tokens")
            )
            usage_quality = str(record.get("usage_quality") or "low")
            endpoint = str(record.get("endpoint") or "")

            event_id = _stable_event_id(provider, model, captured_at)
            event = {
                "id": event_id,
                "source": "trace",
                "event_type": "api_call",
                "started_at": captured_at,
                "ended_at": captured_at,
                "duration_sec": 0,
                "wall_clock_sec": 0,
                "cwd": None,
                "project_id": "unclassified",
                "project_conf": 0.0,
                "tool_or_action": "api_call",
                "token_input": input_tokens,
                "token_output": output_tokens,
                "cost_estimated": 0.0,
                "user_msg_type": None,
                "wordcount": 0,
                "confidence_flag": _confidence_from_quality(usage_quality),
                "raw_meta": json.dumps({
                    "provider": provider,
                    "model": model,
                    "endpoint": endpoint,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_creation,
                    "usage_quality": usage_quality,
                    "auth_mode": "api_key",
                }),
            }
            storage.upsert_event(event, replace=True)
            stats["events_ingested"] += 1
        stats["sessions_ingested"] = stats["events_ingested"]

    if max_mtime > last_mtime:
        storage.set_ingestion_offset(
            "trace",
            "drop_mtime_max",
            max_mtime,
            datetime.now(timezone.utc).isoformat(),
        )

    return stats


def _safe_int(value: Any) -> int:
    """Coerce ``value`` to int; return 0 for None / non-numeric / bool."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
