"""Tests for the Claude Code statusline live collector.

Tests both the writer (statusline tick → drop file) and the collector
(drop files → aggregated session_live_tick events). No real Claude
Code interaction.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from ship1000x.collectors import claude_statusline as collector
from ship1000x.core.storage import Storage
from ship1000x.runtime import statusline_writer

# --- Statusline writer -------------------------------------------------------


class TestStatuslineWriter:
    def test_extracts_only_tracked_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr(statusline_writer, "DROP_DIR", tmp_path / "drop")
        stdin = io.StringIO(json.dumps({
            "session_id": "abc-123",
            "model": "claude-opus-4-7",
            "cwd": "/Users/alice/project-x",
            "transcript_path": "/Users/alice/.claude/projects/x.jsonl",
            "version": "2.1.128",
            "context": {"used_percent": 47.5},
            "compact": {"triggered": False},
            # Fields NOT in the whitelist must be dropped
            "secret_key": "should-not-leak",
            "raw_transcript": "leaky content",
        }))
        monkeypatch.setattr("sys.stdin", stdin)
        stdout = io.StringIO()
        monkeypatch.setattr("sys.stdout", stdout)
        rc = statusline_writer.main()
        assert rc == 0
        # File created
        day_files = list((tmp_path / "drop").glob("*.jsonl"))
        assert len(day_files) == 1
        record = json.loads(day_files[0].read_text(encoding="utf-8").strip())
        # Tracked fields present
        for field in ("session_id", "model", "cwd", "context", "compact"):
            assert field in record
        # Forbidden fields stripped
        assert "secret_key" not in record
        assert "raw_transcript" not in record
        # Captured timestamp added
        assert "captured_at" in record

    def test_writes_statusline_text_to_stdout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(statusline_writer, "DROP_DIR", tmp_path / "drop")
        stdin = io.StringIO(json.dumps({
            "session_id": "abc-123",
            "model": "gpt-5.5",
            "cwd": "/Users/alice/work/foo",
            "context": {"used_percent": 30},
        }))
        monkeypatch.setattr("sys.stdin", stdin)
        stdout = io.StringIO()
        monkeypatch.setattr("sys.stdout", stdout)
        statusline_writer.main()
        text = stdout.getvalue()
        assert "ctx 30%" in text
        assert "gpt-5.5" in text
        assert "foo" in text  # basename of cwd

    def test_empty_stdin_does_not_raise(self, tmp_path, monkeypatch):
        monkeypatch.setattr(statusline_writer, "DROP_DIR", tmp_path / "drop")
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        monkeypatch.setattr("sys.stdout", io.StringIO())
        rc = statusline_writer.main()
        assert rc == 0
        # No file should have been created on empty input
        assert not (tmp_path / "drop").exists() or not list(
            (tmp_path / "drop").glob("*.jsonl")
        )

    def test_malformed_json_does_not_raise(self, tmp_path, monkeypatch):
        monkeypatch.setattr(statusline_writer, "DROP_DIR", tmp_path / "drop")
        monkeypatch.setattr("sys.stdin", io.StringIO("{ broken json"))
        monkeypatch.setattr("sys.stdout", io.StringIO())
        rc = statusline_writer.main()
        assert rc == 0


# --- Statusline collector ----------------------------------------------------


class _StubClassifier:
    rules: list = []

    def classify_session(self, paths=None, cwd=None, git_remote=None):
        return ("project-x", 0.9)


def _write_drop_file(tmp_path: Path, lines: list[dict]) -> Path:
    drop_dir = tmp_path / "drop" / "statusline"
    drop_dir.mkdir(parents=True, exist_ok=True)
    f = drop_dir / "2026-05-23.jsonl"
    f.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return f


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "store.sqlite")
    s.init_schema()
    return s


def test_collector_aggregates_ticks_to_one_event_per_minute(storage, tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "statusline")
    _write_drop_file(tmp_path, [
        {"session_id": "S1", "captured_at": "2026-05-23T10:30:01.000Z",
         "model": "claude-opus-4-7", "cwd": "/Users/x/proj",
         "context": {"used_percent": 50}, "compact": {"triggered": False}},
        {"session_id": "S1", "captured_at": "2026-05-23T10:30:45.000Z",
         "model": "claude-opus-4-7", "cwd": "/Users/x/proj",
         "context": {"used_percent": 70}, "compact": {"triggered": False}},
        {"session_id": "S1", "captured_at": "2026-05-23T10:31:10.000Z",
         "model": "claude-opus-4-7", "cwd": "/Users/x/proj",
         "context": {"used_percent": 80}, "compact": {"triggered": True}},
    ])
    stats = collector.collect(storage, _StubClassifier(), privacy_config={})
    assert stats["events_ingested"] == 2  # one per minute
    assert stats["ticks_aggregated"] == 3
    rows = storage.query(
        "SELECT raw_meta FROM events WHERE source = 'claude_statusline' ORDER BY started_at"
    )
    assert len(rows) == 2
    minute1 = json.loads(rows[0]["raw_meta"])
    minute2 = json.loads(rows[1]["raw_meta"])
    assert minute1["statusline_tick_count"] == 2
    assert minute1["context_used_percent_max"] == 70.0
    assert minute1["compact_triggered"] is False
    assert minute2["statusline_tick_count"] == 1
    assert minute2["context_used_percent_max"] == 80.0
    assert minute2["compact_triggered"] is True


def test_collector_picks_dominant_model_per_minute(storage, tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "statusline")
    _write_drop_file(tmp_path, [
        {"session_id": "S1", "captured_at": "2026-05-23T10:30:00Z",
         "model": "claude-opus-4-7", "cwd": "/Users/x/p"},
        {"session_id": "S1", "captured_at": "2026-05-23T10:30:30Z",
         "model": "claude-opus-4-7", "cwd": "/Users/x/p"},
        {"session_id": "S1", "captured_at": "2026-05-23T10:30:50Z",
         "model": "claude-sonnet-4-7", "cwd": "/Users/x/p"},
    ])
    collector.collect(storage, _StubClassifier(), privacy_config={})
    rows = storage.query(
        "SELECT raw_meta FROM events WHERE source = 'claude_statusline'"
    )
    meta = json.loads(rows[0]["raw_meta"])
    assert meta["model"] == "claude-opus-4-7"


def test_collector_is_idempotent(storage, tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "statusline")
    _write_drop_file(tmp_path, [
        {"session_id": "S1", "captured_at": "2026-05-23T10:30:00Z",
         "model": "claude-opus-4-7", "cwd": "/Users/x/p",
         "context": {"used_percent": 50}},
    ])
    collector.collect(storage, _StubClassifier(), privacy_config={})
    collector.collect(storage, _StubClassifier(), privacy_config={})
    rows = storage.query(
        "SELECT id FROM events WHERE source = 'claude_statusline'"
    )
    assert len(rows) == 1  # idempotent thanks to stable event id


def test_collector_skips_invalid_records(storage, tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "statusline")
    drop_dir = tmp_path / "drop" / "statusline"
    drop_dir.mkdir(parents=True)
    (drop_dir / "2026-05-23.jsonl").write_text(
        '{"session_id": "S1", "captured_at": "2026-05-23T10:30:00Z", "model": "x"}\n'
        '{ malformed json\n'
        '"not a dict"\n'
        '{"no_session_id": true, "captured_at": "2026-05-23T10:30:00Z"}\n'
        '{"session_id": "S2", "captured_at": "2026-05-23T10:30:00Z", "model": "y"}\n',
        encoding="utf-8",
    )
    stats = collector.collect(storage, _StubClassifier(), privacy_config={})
    # Only the two well-formed records with session_id and captured_at
    # should produce events.
    assert stats["events_ingested"] == 2
    assert stats["ticks_aggregated"] == 2


def test_collector_no_op_when_drop_dir_missing(storage, tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "absent")
    stats = collector.collect(storage, _StubClassifier(), privacy_config={})
    assert stats["events_ingested"] == 0
    assert stats["files_seen"] == 0
