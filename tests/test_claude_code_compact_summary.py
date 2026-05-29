"""Tests for Claude Code /compact event tracking.

Claude Code emits a `user` record with `isCompactSummary: true` every
time the session is `/compact`-ed (manually, or automatically by the
runtime when the context fills up). Tracking these lets a downstream
report tell pre-compact vs post-compact work apart and surfaces when a
long session was forced to summarise its own context.

These tests synthesize JSONL files (no real `~/.claude` read) and
verify the parser increments `compact_count` + records timestamps.
"""

from __future__ import annotations

import json
from pathlib import Path

from ship1000x.collectors.claude_code import parse_session_file


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_compact_summary_increments_count_and_records_timestamp(tmp_path):
    """One compact event produces compact_count=1 with first==last ts."""
    session = tmp_path / "0001.jsonl"
    _write_jsonl(
        session,
        [
            {
                "type": "user",
                "isCompactSummary": True,
                "timestamp": "2026-05-14T10:24:31.186Z",
                "sessionId": "abc",
                "uuid": "u1",
            },
        ],
    )
    parsed = parse_session_file(session)
    # parse_session_file returns a 'daily' breakdown — pull the first day
    daily = parsed["daily"]
    day = next(iter(daily.values()))
    assert day["compact_count"] == 1
    assert day["compact_timestamps"] == ["2026-05-14T10:24:31.186Z"]


def test_multiple_compact_events_aggregated_per_day(tmp_path):
    """Several compacts on the same day accumulate."""
    session = tmp_path / "0002.jsonl"
    _write_jsonl(
        session,
        [
            {
                "type": "user",
                "isCompactSummary": True,
                "timestamp": "2026-05-14T10:24:31.186Z",
                "sessionId": "abc",
                "uuid": "u1",
            },
            {
                "type": "user",
                "isCompactSummary": True,
                "timestamp": "2026-05-14T15:00:00.000Z",
                "sessionId": "abc",
                "uuid": "u2",
            },
        ],
    )
    parsed = parse_session_file(session)
    day = next(iter(parsed["daily"].values()))
    assert day["compact_count"] == 2
    assert day["compact_timestamps"][0] == "2026-05-14T10:24:31.186Z"
    assert day["compact_timestamps"][-1] == "2026-05-14T15:00:00.000Z"


def test_compact_event_does_not_count_as_user_typed_message(tmp_path):
    """A compact summary record must not bleed into the regular
    typed/paste/tool_result distribution — it is a synthetic event
    emitted by the runtime, not a user input."""
    session = tmp_path / "0003.jsonl"
    _write_jsonl(
        session,
        [
            {
                "type": "user",
                "isCompactSummary": True,
                "timestamp": "2026-05-14T10:24:31.186Z",
                "sessionId": "abc",
                "uuid": "u1",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "summary…"}
                    ],
                },
            },
        ],
    )
    parsed = parse_session_file(session)
    day = next(iter(parsed["daily"].values()))
    assert day["compact_count"] == 1
    # No regular user msg counted from the compact record.
    assert all(
        count == 0 for count in day["user_msg_counts"].values()
    ), day["user_msg_counts"]


def test_sessions_without_compact_keep_zero(tmp_path):
    """Regression: parsing a normal session must leave compact_count
    at zero and the timestamps list empty (no AttributeError)."""
    session = tmp_path / "0004.jsonl"
    _write_jsonl(
        session,
        [
            {
                "type": "user",
                "timestamp": "2026-05-14T10:00:00.000Z",
                "sessionId": "abc",
                "uuid": "u1",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "hi"}],
                },
            },
        ],
    )
    parsed = parse_session_file(session)
    day = next(iter(parsed["daily"].values()))
    assert day["compact_count"] == 0
    assert day["compact_timestamps"] == []
