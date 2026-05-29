"""Fixture-only Aider session parser.

Aider is a git-aware AI pair programming CLI. It logs chat history and input
history locally and prints token/cost lines after each turn. Those local
files contain raw conversation content, so a real discovery layer must
extract only safe aggregates and never preserve the chat body. This parser
operates on a synthetic, redacted session fixture and never discovers real
Aider files. It is not wired to `ship1000x ingest`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ship1000x.core.usage import (
    TokenBreakdown,
    build_unknown_usage_metadata,
    build_usage_metadata,
)

SOURCE = "aider"
SCHEMA_VERSION = "ship1000x.aider.session.v1"


def _stable_event_id(session_id: str, started_at: str) -> str:
    raw = f"{SOURCE}|{session_id}|{started_at}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _tokens_from_payload(payload: dict[str, Any]) -> TokenBreakdown:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return TokenBreakdown(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_write_tokens") or 0),
        reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
    )


def parse_session_file(path: Path) -> dict[str, Any]:
    """Parse a redacted Aider session fixture into safe event metadata."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported Aider fixture schema: {payload.get('schema_version')}"
        )

    session_id = str(payload.get("session_id") or path.stem)
    started_at = str(payload.get("started_at") or "")
    ended_at = str(payload.get("ended_at") or started_at)
    provider = str(payload.get("provider") or "unknown")
    model = str(payload.get("model") or "unknown")
    tokens = _tokens_from_payload(payload)
    if tokens.has_native_tokens:
        usage = build_usage_metadata(
            provider=provider,
            client="aider",
            model_raw=model,
            tokens=tokens,
            cost_estimated=0.0,
            cost_quality="unknown",
            cost_basis="pricing_not_configured",
            token_source="aider_fixture_usage",
        )
    else:
        usage = build_unknown_usage_metadata(
            provider=provider,
            client="aider",
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
        "token_input": tokens.input_tokens,
        "token_output": tokens.output_tokens,
        "cost_estimated": 0.0,
        "confidence_flag": "medium",
        "tool_or_action": "session",
        "raw_meta": {
            "schema_version": SCHEMA_VERSION,
            "provider": provider,
            "client": "aider",
            "model": model,
            "commits_created": int(payload.get("commits_created") or 0),
            "files_added_to_chat": int(payload.get("files_added_to_chat") or 0),
            "user_messages": int(payload.get("user_messages") or 0),
            "usage": usage,
        },
    }
