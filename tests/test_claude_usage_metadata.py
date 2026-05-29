"""Tests for Claude Code normalized usage metadata."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ship1000x.collectors import claude_code
from ship1000x.core.classifier import Classifier
from ship1000x.core.storage import Storage


def _write_session(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-05-22T10:00:00Z",
            "type": "user",
            "cwd": str(path.parent),
            "message": {
                "content": "Please inspect the source quality contract and add safe metadata."
            },
        },
        {
            "timestamp": "2026-05-22T10:01:00Z",
            "type": "assistant",
            "cwd": str(path.parent),
            "message": {
                "id": "msg-1",
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 250,
                    "cache_read_input_tokens": 300,
                    "cache_creation_input_tokens": 40,
                },
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": str(path.parent / "src" / "app.py")},
                    }
                ],
            },
        },
        {
            "timestamp": "2026-05-22T10:01:05Z",
            "type": "assistant",
            "cwd": str(path.parent),
            "message": {
                "id": "msg-1",
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 999999,
                    "output_tokens": 999999,
                },
                "content": [],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")


def test_parse_session_file_preserves_claude_cache_breakdown(tmp_path):
    session_file = tmp_path / "session.jsonl"
    _write_session(session_file)

    parsed = claude_code.parse_session_file(session_file)
    day = parsed["daily"]["2026-05-22"]

    assert parsed["tokens_input"] == 1340
    assert parsed["tokens_output"] == 250
    assert parsed["cache_read_tokens"] == 300
    assert parsed["cache_write_tokens"] == 40
    assert day["tokens_input_uncached"] == 1000
    assert day["tokens_input"] == 1340
    assert day["tokens_output"] == 250
    assert day["cache_read_tokens"] == 300
    assert day["cache_write_tokens"] == 40
    assert day["model_stats"]["claude-sonnet-4-6"]["cache_read_tokens"] == 300
    assert day["model_stats"]["claude-sonnet-4-6"]["cache_write_tokens"] == 40


def test_collect_writes_safe_claude_usage_metadata(monkeypatch):
    with TemporaryDirectory(dir=Path.cwd()) as tmp:
        root = Path(tmp)
        session_file = root / "claude-project" / "session.jsonl"
        _write_session(session_file)
        db_path = root / "tracker.sqlite"
        storage = Storage(db_path)
        storage.init_schema()
        classifier = Classifier.from_yaml_config(
            {
                "projects": [
                    {
                        "id": "demo-project",
                        "paths": [str(session_file.parent) + "/*"],
                    }
                ]
            }
        )

        monkeypatch.setattr(claude_code, "iter_session_files", lambda: iter([session_file]))

        stats = claude_code.collect(storage, classifier, {})

        assert stats["events_ingested"] == 1
        row = storage.query("SELECT raw_meta FROM events WHERE source = 'claude_code'")[0]
        meta = json.loads(row["raw_meta"])
        usage = meta["usage"]
        assert usage["provider"] == "anthropic"
        assert usage["client"] == "claude-code"
        assert usage["model_canonical"] == "claude-sonnet-4-6"
        assert usage["tokens"]["input_tokens"] == 1000
        assert usage["tokens"]["output_tokens"] == 250
        assert usage["tokens"]["cached_input_tokens"] == 300
        assert usage["tokens"]["cache_write_tokens"] == 40
        assert usage["quality"]["tokens"] == "factual"
        assert usage["quality"]["cost"] == "factual"
        assert usage["quality"]["active_time"] == "defensible"
        assert usage["provenance"]["token_source"] == "claude_message_usage"
        assert "prompt" not in json.dumps(meta).lower()
        assert "response" not in json.dumps(meta).lower()
