"""Tests for the Claude Desktop session sidecar collector.

These tests never read the real ~/Library/Application Support/Claude
folder. They build synthetic sidecar JSON files under a tmp tree, run
the parser/collector against them, and inspect the resulting event.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ship1000x.collectors import claude_desktop_sessions


class _NoopClassifier:
    """Minimal classifier double matching the collector's usage."""

    def classify_session(self, cwd: str | None = None, paths=None):
        return "unclassified", 0.5


class _Storage:
    """In-memory SQLite storage stub mirroring ship1000x.core.storage."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE events (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                event_type TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                duration_sec INTEGER,
                wall_clock_sec INTEGER,
                cwd TEXT,
                project_id TEXT,
                project_conf REAL,
                tool_or_action TEXT,
                token_input INTEGER,
                token_output INTEGER,
                cost_estimated REAL,
                user_msg_type TEXT,
                wordcount INTEGER,
                payload_hash TEXT,
                confidence_flag TEXT,
                raw_meta TEXT
            )
            """
        )

    def upsert_event(self, event, replace=False):
        cols = ", ".join(event.keys())
        ph = ", ".join("?" for _ in event)
        self.conn.execute(
            f"INSERT OR REPLACE INTO events ({cols}) VALUES ({ph})",
            tuple(event.values()),
        )
        self.conn.commit()

    def query(self, sql, params=()):
        return list(self.conn.execute(sql, params))


def _write_sidecar(base: Path, payload: dict) -> Path:
    """Write a synthetic sidecar at base/org/user/local_<id>.json."""
    target = base / "orgA" / "userB" / f"local_{payload['sessionId']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _full_sidecar_payload(
    *,
    session_id: str = "abc-1",
    cli_session_id: str = "def-2",
    title: str = "Refactor session",
    model: str = "claude-opus-4-7[1m]",
    effort: str = "max",
    completed_turns: int = 12,
    pr_number: int | None = 42,
    cwd: str = "/Users/dev/work/project",
) -> dict:
    return {
        "sessionId": session_id,
        "cliSessionId": cli_session_id,
        "cwd": cwd,
        "originCwd": cwd,
        "lastFocusedAt": 1779533643860,
        "createdAt": 1779458795663,
        "lastActivityAt": 1779533695112,
        "model": model,
        "effort": effort,
        "isArchived": False,
        "title": title,
        "titleSource": "auto",
        "permissionMode": "auto",
        "planPath": "/Users/dev/.claude/plans/plan.md",
        "enabledMcpTools": {"toolA": True, "toolB": True},
        "remoteMcpServersConfig": [{"uuid": "srv-1", "name": "mcp1", "tools": []}],
        "prNumber": pr_number,
        "prUrl": f"https://github.com/o/r/pull/{pr_number}" if pr_number else None,
        "prRepository": "o/r" if pr_number else None,
        "prState": "MERGED" if pr_number else None,
        "completedTurns": completed_turns,
        "chromePermissionMode": "skip_all_permission_checks",
    }


def test_parse_session_file_extracts_all_safe_metadata(tmp_path):
    payload = _full_sidecar_payload()
    path = _write_sidecar(tmp_path, payload)

    parsed = claude_desktop_sessions.parse_session_file(path)
    assert parsed is not None
    assert parsed["session_id"] == "abc-1"
    assert parsed["cli_session_id"] == "def-2"
    assert parsed["model"] == "claude-opus-4-7[1m]"
    assert parsed["effort"] == "max"
    assert parsed["title"] == "Refactor session"
    assert parsed["title_source"] == "auto"
    assert parsed["completed_turns"] == 12
    assert parsed["pr_number"] == 42
    assert parsed["pr_state"] == "MERGED"
    assert parsed["mcp_tool_count"] == 2
    assert parsed["remote_mcp_count"] == 1
    # auth_mode is locked to oauth for Desktop entrypoint.
    assert parsed["auth_mode"] == "oauth"


def test_parse_session_file_skips_invalid_payloads(tmp_path):
    # Missing sessionId -> skip.
    bad = tmp_path / "local_bad.json"
    bad.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
    assert claude_desktop_sessions.parse_session_file(bad) is None

    # Malformed JSON -> skip without raising.
    bad2 = tmp_path / "local_bad2.json"
    bad2.write_text("not-json", encoding="utf-8")
    assert claude_desktop_sessions.parse_session_file(bad2) is None


def test_collect_emits_one_event_per_sidecar(tmp_path, monkeypatch):
    _write_sidecar(tmp_path, _full_sidecar_payload(session_id="s-1"))
    _write_sidecar(tmp_path, _full_sidecar_payload(session_id="s-2", pr_number=None))

    monkeypatch.setattr(
        claude_desktop_sessions, "CLAUDE_DESKTOP_SESSIONS_DIR", tmp_path
    )

    storage = _Storage()
    stats = claude_desktop_sessions.collect(storage, _NoopClassifier(), {})

    assert stats["events_ingested"] == 2
    assert stats["sessions_ingested"] == 2

    rows = storage.query("SELECT source, raw_meta FROM events")
    assert len(rows) == 2
    for row in rows:
        meta = json.loads(row["raw_meta"])
        assert row["source"] == "claude_desktop"
        assert meta["source_api"] == "claude_desktop_session_sidecar"
        assert meta["auth_mode"] == "oauth"
        assert "title" in meta
        assert "cli_session_id" in meta
        assert meta["usage"]["auth_mode"] == "oauth"
        assert meta["usage"]["client"] == "claude-desktop"


def test_collect_returns_zero_stats_when_directory_is_absent(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(
        claude_desktop_sessions, "CLAUDE_DESKTOP_SESSIONS_DIR", missing
    )
    storage = _Storage()
    stats = claude_desktop_sessions.collect(storage, _NoopClassifier(), {})
    assert stats == {
        "files_seen": 0,
        "files_parsed": 0,
        "sessions_ingested": 0,
        "events_ingested": 0,
        "skipped": 0,
    }


def test_collect_skips_excluded_paths(tmp_path, monkeypatch):
    _write_sidecar(
        tmp_path,
        _full_sidecar_payload(session_id="s-excl", cwd="/Users/dev/secret/area"),
    )
    monkeypatch.setattr(
        claude_desktop_sessions, "CLAUDE_DESKTOP_SESSIONS_DIR", tmp_path
    )

    storage = _Storage()
    stats = claude_desktop_sessions.collect(
        storage,
        _NoopClassifier(),
        {"exclude_paths": ["/Users/dev/secret*"]},
    )
    assert stats["events_ingested"] == 0
    assert stats["skipped"] >= 1
