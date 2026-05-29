"""Fixture-only GitHub Copilot agents session parser.

GitHub Copilot exposes several surfaces (Copilot Chat, inline completions,
Copilot Workspace / agent-mode). Their client-visible logs rarely break down
token usage and never expose deterministic per-call cost; provider truth lives
inside GitHub's billing. This parser therefore captures only safe metadata for
a synthetic, redacted session and never discovers real Copilot logs. It is not
wired to `ship1000x ingest`.

The fixture-first contract lets SHIP represent Copilot agent activity in the
provider expansion lane (`copilot_agents` source profile) without making any
unverified token/cost claim.
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

SOURCE = "copilot_agents"
SCHEMA_VERSION = "ship1000x.copilot_agents.session.v1"

# Modes accepted by the schema. Anything else maps to "unknown" so the parser
# stays forward-compatible without silently absorbing new Copilot surfaces.
_KNOWN_AGENT_MODES = frozenset({"chat", "inline", "agent_task", "unknown"})


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
    """Parse a redacted GitHub Copilot agents fixture into safe event metadata.

    The returned event keeps the same shape as other provider-expansion
    parsers (`gemini_cli`, `opencode`, `cursor_agent`, `roo_kilo_code`):
    minimal session metrics, normalized usage metadata, and `raw_meta`
    limited to derived counters. No prompt, response, file path, diff, or
    raw payload is preserved.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported GitHub Copilot agents fixture schema: {payload.get('schema_version')}"
        )

    session_id = str(payload.get("session_id") or path.stem)
    started_at = str(payload.get("started_at") or "")
    ended_at = str(payload.get("ended_at") or started_at)
    model = str(payload.get("model") or "unknown")
    agent_mode_raw = str(payload.get("agent_mode") or "unknown")
    agent_mode = agent_mode_raw if agent_mode_raw in _KNOWN_AGENT_MODES else "unknown"

    tokens = _tokens_from_payload(payload)
    if tokens.has_native_tokens:
        usage = build_usage_metadata(
            provider="github",
            client="copilot-agents",
            model_raw=model,
            tokens=tokens,
            cost_estimated=0.0,
            cost_quality="unknown",
            cost_basis="pricing_not_configured",
            token_source="copilot_agents_fixture_usage",
        )
    else:
        usage = build_unknown_usage_metadata(
            provider="github",
            client="copilot-agents",
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
        "tool_or_action": "agent_session",
        "raw_meta": {
            "schema_version": SCHEMA_VERSION,
            "provider": "github",
            "client": "copilot-agents",
            "model": model,
            "agent_mode": agent_mode,
            "tool_calls": int(payload.get("tool_calls") or 0),
            "user_messages": int(payload.get("user_messages") or 0),
            "edits_applied": int(payload.get("edits_applied") or 0),
            "usage": usage,
        },
    }
