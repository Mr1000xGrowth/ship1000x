"""Collector Claude Desktop session sidecars.

Claude Desktop (the Anthropic Mac app that hosts Claude Code) writes
one JSON file per Claude Code session under
``~/Library/Application Support/Claude/claude-code-sessions/<org-id>/<user-id>/local_<session-uuid>.json``.
These sidecars carry **Desktop-only** metadata that the JSONL log on
the CLI side (``~/.claude/projects/...``) does not expose:

- ``title``: an auto-generated human-readable title for the session
  (e.g. "auth refactor PR continuation"). Much more useful for
  triage than the raw session UUID.
- ``model``: the exact model string Desktop chose, including any context
  window suffix (e.g. ``claude-opus-4-7[1m]``). The JSONL only carries
  ``claude-opus-4-7`` because per-message ``message.model`` strips the
  suffix.
- ``effort``: the reasoning effort knob (``max``/``medium``/``min``).
- ``prNumber`` / ``prUrl`` / ``prRepository`` / ``prState``: the GitHub
  pull request Desktop linked to the session, when there is one.
- ``completedTurns``: a Desktop-side turn counter used for resume hints.

The collector emits **one event per session sidecar**. The JSONL-side
collector (``claude_code``) keeps producing the per-day session_day
event with tokens/cost as before; the Desktop sidecar source is a
metadata-only join layer addressable via ``cli_session_id`` (= the
JSONL session_id).

Privacy boundary:
- Local paths (``cwd``, ``planPath``) are anonymized through the
  standard ``ship1000x.core.privacy`` helpers before storage.
- Auto-generated titles are kept as-is. They are produced by Claude
  from the session content, so they can leak topic hints. They never
  contain message bodies, code diffs, or credentials, and the user
  saw and accepted the title in the Desktop UI; we treat them like
  commit messages.
- ``planPath`` is only stored as an anonymized path; the plan file
  itself is never read.
- MCP tool names and permission flags are kept (categorical labels).
- No prompt, response, message content, file diff, or tool I/O is
  read or stored.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ship1000x.core.auth_mode import detect_claude_auth_mode
from ship1000x.core.usage import build_unknown_usage_metadata

CLAUDE_DESKTOP_SESSIONS_DIR = (
    Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
)


def _stable_event_id(session_id: str) -> str:
    raw = f"claude_desktop_sessions|{session_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _iso_from_ms(timestamp_ms: int | float | None) -> str | None:
    if timestamp_ms is None:
        return None
    try:
        return datetime.fromtimestamp(
            int(timestamp_ms) / 1000.0, tz=timezone.utc
        ).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def iter_session_files(base_dir: Path | None = None):
    """Yield every ``local_*.json`` session sidecar Claude Desktop writes.

    ``base_dir`` defaults to ``CLAUDE_DESKTOP_SESSIONS_DIR`` resolved at
    call time so tests can ``monkeypatch.setattr`` the module constant
    and still take effect (a function default argument would freeze the
    original path at import time).
    """
    actual = base_dir if base_dir is not None else CLAUDE_DESKTOP_SESSIONS_DIR
    if not actual.exists():
        return
    yield from actual.glob("*/*/local_*.json")


def parse_session_file(path: Path) -> dict[str, Any] | None:
    """Parse one Claude Desktop session sidecar into a SHIP event payload.

    Returns ``None`` if the file is unreadable or has no usable
    ``sessionId`` (defensive: Desktop occasionally writes partial files
    during shutdown). All paths are anonymized via the standard SHIP
    privacy helper; raw content fields are never read or returned.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None

    session_id = raw.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return None

    cli_session_id = raw.get("cliSessionId") if isinstance(raw.get("cliSessionId"), str) else None
    model = (raw.get("model") or "").strip() if isinstance(raw.get("model"), str) else ""
    effort = (raw.get("effort") or "").strip() if isinstance(raw.get("effort"), str) else ""
    title = (raw.get("title") or "").strip() if isinstance(raw.get("title"), str) else ""
    title_source = raw.get("titleSource") if isinstance(raw.get("titleSource"), str) else None
    permission_mode = raw.get("permissionMode") if isinstance(raw.get("permissionMode"), str) else None
    completed_turns = int(raw.get("completedTurns") or 0)
    is_archived = bool(raw.get("isArchived"))

    pr_number = raw.get("prNumber")
    pr_url = raw.get("prUrl") if isinstance(raw.get("prUrl"), str) else None
    pr_repository = raw.get("prRepository") if isinstance(raw.get("prRepository"), str) else None
    pr_state = raw.get("prState") if isinstance(raw.get("prState"), str) else None

    cwd = raw.get("cwd") if isinstance(raw.get("cwd"), str) else None
    origin_cwd = raw.get("originCwd") if isinstance(raw.get("originCwd"), str) else None
    plan_path = raw.get("planPath") if isinstance(raw.get("planPath"), str) else None

    started_at = _iso_from_ms(raw.get("createdAt"))
    ended_at = _iso_from_ms(raw.get("lastActivityAt") or raw.get("lastFocusedAt"))

    # MCP tool count is a useful categorical signal without leaking values.
    mcp_tools = raw.get("enabledMcpTools")
    mcp_tool_count = len(mcp_tools) if isinstance(mcp_tools, dict) else 0
    remote_mcp = raw.get("remoteMcpServersConfig")
    remote_mcp_count = len(remote_mcp) if isinstance(remote_mcp, list) else 0

    # Claude Desktop is always OAuth (no API-key entry path), so we lock
    # the auth_mode to oauth via the centralized helper.
    auth_mode = detect_claude_auth_mode("claude-desktop")

    return {
        "session_id": session_id,
        "cli_session_id": cli_session_id,
        "cwd": cwd,
        "origin_cwd": origin_cwd,
        "plan_path": plan_path,
        "model": model,
        "effort": effort,
        "title": title,
        "title_source": title_source,
        "permission_mode": permission_mode,
        "completed_turns": completed_turns,
        "is_archived": is_archived,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "pr_repository": pr_repository,
        "pr_state": pr_state,
        "started_at": started_at,
        "ended_at": ended_at,
        "mcp_tool_count": mcp_tool_count,
        "remote_mcp_count": remote_mcp_count,
        "auth_mode": auth_mode,
    }


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Ingest every Claude Desktop session sidecar.

    Idempotent: events are keyed by ``sessionId`` and re-upserted on
    every run so Desktop updates (title regenerated, PR linkage added
    after merge) are reflected without manual reset.
    """
    from ship1000x.core.privacy import is_excluded_path, sanitize_event

    stats = {
        "files_seen": 0,
        "files_parsed": 0,
        "sessions_ingested": 0,
        "events_ingested": 0,
        "skipped": 0,
    }
    # Resolve the module constant at call time so tests can patch it.
    if not CLAUDE_DESKTOP_SESSIONS_DIR.exists():
        return stats
    base_dir = CLAUDE_DESKTOP_SESSIONS_DIR

    exclude_paths = privacy_config.get("exclude_paths", []) or []

    for path in iter_session_files(base_dir):
        stats["files_seen"] += 1
        parsed = parse_session_file(path)
        if parsed is None:
            stats["skipped"] += 1
            continue
        stats["files_parsed"] += 1

        cwd = parsed.get("cwd") or ""
        if is_excluded_path(cwd, exclude_paths):
            stats["skipped"] += 1
            continue

        project_id, conf = classifier.classify_session(cwd=cwd, paths=[])

        usage = build_unknown_usage_metadata(
            provider="anthropic",
            client="claude-desktop",
            model_raw=parsed["model"] or None,
            cost_estimated=0.0,
            cost_quality="unknown",
            cost_basis="session_metadata_only",
            auth_mode=parsed["auth_mode"],
        )

        event = {
            "id": _stable_event_id(parsed["session_id"]),
            "source": "claude_desktop",
            "event_type": "session_metadata",
            "started_at": parsed["started_at"] or "1970-01-01T00:00:00+00:00",
            "ended_at": parsed["ended_at"] or parsed["started_at"]
            or "1970-01-01T00:00:00+00:00",
            "duration_sec": 0,
            "wall_clock_sec": 0,
            "cwd": cwd,
            "project_id": project_id,
            "project_conf": conf,
            "tool_or_action": "claude_desktop_session",
            "token_input": 0,
            "token_output": 0,
            "cost_estimated": 0.0,
            "user_msg_type": None,
            "wordcount": 0,
            "confidence_flag": "high",
            "raw_meta": json.dumps(
                {
                    "session_id": parsed["session_id"],
                    "cli_session_id": parsed["cli_session_id"],
                    "model": parsed["model"],
                    "mode": parsed["effort"],
                    "title": parsed["title"],
                    "title_source": parsed["title_source"],
                    "permission_mode": parsed["permission_mode"],
                    "turn_count": parsed["completed_turns"],
                    "is_archived": parsed["is_archived"],
                    "pr_number": parsed["pr_number"],
                    "pr_url": parsed["pr_url"],
                    "pr_repository": parsed["pr_repository"],
                    "pr_state": parsed["pr_state"],
                    "tool_call_count": parsed["mcp_tool_count"],
                    "remote_mcp_count": parsed["remote_mcp_count"],
                    "auth_mode": parsed["auth_mode"],
                    "source_api": "claude_desktop_session_sidecar",
                    "usage": usage,
                }
            ),
        }
        safe = sanitize_event(event)
        storage.upsert_event(safe, replace=True)
        stats["events_ingested"] += 1
        stats["sessions_ingested"] += 1

    return stats
