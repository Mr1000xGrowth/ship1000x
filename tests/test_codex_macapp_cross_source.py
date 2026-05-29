"""Tests for the cross-source coding-model resolver (MacApp ↔ logs_2.sqlite).

Codex MacApp 2026+ logs only carry the auxiliary `thread_title`
sub-model, never the real coding model. The coding model lives in
`~/.codex/logs_2.sqlite` OTEL spans, tagged with `thread.id=<UUID>`
that matches the MacApp `conversationId=<UUID>` for the same coding
session. These tests synthesize both sides without touching real data.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ship1000x.collectors.codex_macapp import (
    _daily_split,
    _parse_log_file,
    _resolve_thread_models,
)


def _make_logs_db(path: Path, rows: list[str]) -> None:
    """Create a SQLite DB mimicking the logs_2.sqlite schema with the
    bodies we need to test the resolver against."""
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            ts_nanos INTEGER NOT NULL,
            level TEXT NOT NULL,
            target TEXT NOT NULL,
            feedback_log_body TEXT,
            module_path TEXT,
            file TEXT,
            line INTEGER,
            thread_id TEXT,
            process_uuid TEXT,
            estimated_bytes INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    for body in rows:
        con.execute(
            "INSERT INTO logs (ts, ts_nanos, level, target, feedback_log_body) VALUES (?, ?, ?, ?, ?)",
            (1779000000, 0, "INFO", "codex_otel.log_only", body),
        )
    con.commit()
    con.close()


def _write_macapp_log(tmp_path: Path, lines: list[str]) -> Path:
    log = tmp_path / "codex-desktop-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee-12345-t0-i1-000000-0.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


class TestResolveThreadModels:
    def test_returns_empty_when_db_absent(self, tmp_path):
        absent = tmp_path / "does_not_exist.sqlite"
        result = _resolve_thread_models(absent, {"019e3a10-26f0-7be1-8c51-9b0c9b09c4c7"})
        assert result == {}

    def test_returns_empty_when_no_thread_ids_requested(self, tmp_path):
        db = tmp_path / "logs.sqlite"
        _make_logs_db(db, ["thread.id=019e3a10-26f0-7be1-8c51-9b0c9b09c4c7 model=gpt-5.5 turn"])
        assert _resolve_thread_models(db, set()) == {}

    def test_returns_dominant_model_per_thread(self, tmp_path):
        """When the same thread has multiple model mentions, the most
        frequent one wins. This matches the per-day dominant-model rule
        used elsewhere in the collector."""
        db = tmp_path / "logs.sqlite"
        _make_logs_db(
            db,
            [
                "thread.id=019e3a10-26f0-7be1-8c51-9b0c9b09c4c7 model=gpt-5.5 turn=1",
                "thread.id=019e3a10-26f0-7be1-8c51-9b0c9b09c4c7 model=gpt-5.5 turn=2",
                "thread.id=019e3a10-26f0-7be1-8c51-9b0c9b09c4c7 model=gpt-5-codex turn=3",
            ],
        )
        result = _resolve_thread_models(
            db, {"019e3a10-26f0-7be1-8c51-9b0c9b09c4c7"}
        )
        assert result == {"019e3a10-26f0-7be1-8c51-9b0c9b09c4c7": "gpt-5.5"}

    def test_filters_unknown_thread_ids(self, tmp_path):
        """Thread ids that don't appear in the requested set are not in
        the result, even if they have model entries in the DB."""
        db = tmp_path / "logs.sqlite"
        _make_logs_db(
            db,
            [
                "thread.id=11111111-1111-1111-1111-111111111111 model=gpt-5.5",
                "thread.id=22222222-2222-2222-2222-222222222222 model=gpt-5-codex",
            ],
        )
        result = _resolve_thread_models(
            db, {"11111111-1111-1111-1111-111111111111"}
        )
        assert result == {"11111111-1111-1111-1111-111111111111": "gpt-5.5"}

    def test_skips_thread_title_subcall_rows(self, tmp_path):
        """The same auxiliary marker filter that hides the thread_title
        sub-model in the local MacApp parser also applies to rows
        pulled from logs_2.sqlite — otherwise the cross-source join
        would just re-introduce the wrong model via a different path."""
        db = tmp_path / "logs.sqlite"
        _make_logs_db(
            db,
            [
                "thread.id=11111111-1111-1111-1111-111111111111 ephemeral_generation_token_usage feature=thread_title model=gpt-5.4-mini",
            ],
        )
        result = _resolve_thread_models(
            db, {"11111111-1111-1111-1111-111111111111"}
        )
        assert result == {}

    def test_threads_without_model_are_absent_from_result(self, tmp_path):
        """A thread with no `model=<id>` in any OTEL row must not appear
        in the result. Callers treat absence as `model_raw=unknown`."""
        db = tmp_path / "logs.sqlite"
        _make_logs_db(
            db,
            [
                # Has thread.id but no model=
                "thread.id=11111111-1111-1111-1111-111111111111 turn=1",
            ],
        )
        result = _resolve_thread_models(
            db, {"11111111-1111-1111-1111-111111111111"}
        )
        assert result == {}


class TestParseLogFileCapturesConversationEvents:
    def test_captures_conversation_id_from_turn_start_line(self, tmp_path):
        log = _write_macapp_log(
            tmp_path,
            [
                "2026-05-22T10:00:00.000Z info [AppServerConnection] response_routed broadcastFallback=false conversationId=019e3a10-26f0-7be1-8c51-9b0c9b09c4c7 method=turn/start",
            ],
        )
        parsed = _parse_log_file(log)
        assert parsed["conversation_events"] == [
            (
                parsed["all_timestamps"][0],
                "019e3a10-26f0-7be1-8c51-9b0c9b09c4c7",
            )
        ]

    def test_ignores_non_uuid_conversation_id_shapes(self, tmp_path):
        """The regex requires a strict UUID v4-shaped 8-4-4-4-12 hex
        layout; lines that mention `conversationId=null` or
        `conversationId=foo` must not pollute the result."""
        log = _write_macapp_log(
            tmp_path,
            [
                "2026-05-22T10:00:00.000Z info [AppServerConnection] response_routed conversationId=null method=model/list",
                "2026-05-22T10:00:01.000Z info [AppServerConnection] response_routed conversationId=foo method=app/list",
            ],
        )
        parsed = _parse_log_file(log)
        assert parsed["conversation_events"] == []


class _StubClassifier:
    rules: list = []


class TestDailySplitBackFillsModelFromCrossSourceJoin:
    def test_backfills_model_when_local_model_events_empty(self):
        """A day with conversation events but no local model events
        gets its model from the cross-source join."""
        ts = 1779000000  # 2026-05-13 06:40:00 UTC
        parsed = {
            "session_uuid": "abc",
            "pid": "1",
            "all_timestamps": [ts],
            "turn_timestamps": [ts],
            "cwds": [],
            "paths": [],
            "first_ts": ts,
            "last_ts": ts,
            "model_events": [],
            "conversation_events": [(ts, "019e3a10-26f0-7be1-8c51-9b0c9b09c4c7")],
        }
        thread_models = {"019e3a10-26f0-7be1-8c51-9b0c9b09c4c7": "gpt-5.5"}
        daily = _daily_split(parsed, _StubClassifier(), thread_models=thread_models)
        day = next(iter(daily.values()))
        assert dict(day["model_counts"]) == {"gpt-5.5": 1}
        assert day["model_source"] == "logs_2_sqlite_join"

    def test_keeps_local_model_over_join(self):
        """If the local MacApp log already exposed a real (non-auxiliary)
        coding model, that takes precedence over the cross-source join —
        the local source is authoritative."""
        ts = 1779000000
        parsed = {
            "session_uuid": "abc",
            "pid": "1",
            "all_timestamps": [ts],
            "turn_timestamps": [ts],
            "cwds": [],
            "paths": [],
            "first_ts": ts,
            "last_ts": ts,
            "model_events": [(ts, "gpt-5-codex")],
            "conversation_events": [(ts, "019e3a10-26f0-7be1-8c51-9b0c9b09c4c7")],
        }
        thread_models = {"019e3a10-26f0-7be1-8c51-9b0c9b09c4c7": "gpt-5.5"}
        daily = _daily_split(parsed, _StubClassifier(), thread_models=thread_models)
        day = next(iter(daily.values()))
        assert dict(day["model_counts"]) == {"gpt-5-codex": 1}
        assert day["model_source"] == "macapp_log"

    def test_missing_thread_id_attributes_to_logs_2_sqlite_expired(self):
        """When the conversationId is present in the MacApp log but the
        resolver has no model for it, the day is flagged as
        `logs_2_sqlite_expired` — the OTEL spans rotate after ~15 days,
        and we want the report to tell that apart from a session that
        never had a conversationId at all."""
        ts = 1779000000
        parsed = {
            "session_uuid": "abc",
            "pid": "1",
            "all_timestamps": [ts],
            "turn_timestamps": [ts],
            "cwds": [],
            "paths": [],
            "first_ts": ts,
            "last_ts": ts,
            "model_events": [],
            "conversation_events": [(ts, "deadbeef-dead-beef-dead-beefdeadbeef")],
        }
        daily = _daily_split(parsed, _StubClassifier(), thread_models={})
        day = next(iter(daily.values()))
        assert dict(day["model_counts"]) == {}
        assert day["model_source"] == "logs_2_sqlite_expired"

    def test_no_conversation_id_keeps_unknown(self):
        """A day with no conversationId at all stays plain `unknown`."""
        ts = 1779000000
        parsed = {
            "session_uuid": "abc",
            "pid": "1",
            "all_timestamps": [ts],
            "turn_timestamps": [ts],
            "cwds": [],
            "paths": [],
            "first_ts": ts,
            "last_ts": ts,
            "model_events": [],
            "conversation_events": [],
        }
        daily = _daily_split(parsed, _StubClassifier(), thread_models={})
        day = next(iter(daily.values()))
        assert dict(day["model_counts"]) == {}
        assert day["model_source"] == "unknown"

    def test_works_without_thread_models_argument(self):
        """Back-compat: callers that don't pass `thread_models` still
        get a valid daily split — only the cross-source back-fill is
        skipped."""
        ts = 1779000000
        parsed = {
            "session_uuid": "abc",
            "pid": "1",
            "all_timestamps": [ts],
            "turn_timestamps": [ts],
            "cwds": [],
            "paths": [],
            "first_ts": ts,
            "last_ts": ts,
            "model_events": [(ts, "gpt-5.5")],
            "conversation_events": [(ts, "019e3a10-26f0-7be1-8c51-9b0c9b09c4c7")],
        }
        daily = _daily_split(parsed, _StubClassifier())
        day = next(iter(daily.values()))
        assert dict(day["model_counts"]) == {"gpt-5.5": 1}
        assert day["model_source"] == "macapp_log"
