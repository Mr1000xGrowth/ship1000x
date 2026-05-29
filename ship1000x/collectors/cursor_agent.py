"""Fixture-only Cursor Agent session parser.

Cursor Agent is intentionally separated from generic Cursor AI-block metadata.
This parser documents the safe metadata shape only; it is not wired to ingest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ship1000x.core.usage import build_unknown_usage_metadata

SOURCE = "cursor_agent"
SCHEMA_VERSION = "ship1000x.cursor_agent.session.v1"


def _stable_event_id(session_id: str, started_at: str) -> str:
    raw = f"{SOURCE}|{session_id}|{started_at}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def parse_session_file(path: Path) -> dict[str, Any]:
    """Parse a redacted Cursor Agent fixture into safe event metadata."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported Cursor Agent fixture schema: {payload.get('schema_version')}")

    session_id = str(payload.get("session_id") or path.stem)
    started_at = str(payload.get("started_at") or "")
    ended_at = str(payload.get("ended_at") or started_at)
    model = str(payload.get("model") or "unknown")
    usage = build_unknown_usage_metadata(
        provider="cursor",
        client="cursor-agent",
        model_raw=model,
        cost_estimated=0.0,
        cost_quality="unknown",
        cost_basis="not_exposed",
    )

    return {
        "id": _stable_event_id(session_id, started_at),
        "source": SOURCE,
        "event_type": "session",
        "session_id": session_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_sec": int(payload.get("active_sec") or 0),
        "wall_clock_sec": int(payload.get("wall_clock_sec") or 0),
        "model": model,
        "token_input": 0,
        "token_output": 0,
        "cost_estimated": 0.0,
        "confidence_flag": "medium",
        "tool_or_action": "agent_session",
        "raw_meta": {
            "schema_version": SCHEMA_VERSION,
            "provider": "cursor",
            "client": "cursor-agent",
            "model": model,
            "agent_turns": int(payload.get("agent_turns") or 0),
            "tool_calls": int(payload.get("tool_calls") or 0),
            "files_touched_count": int(payload.get("files_touched_count") or 0),
            "usage": usage,
        },
    }
