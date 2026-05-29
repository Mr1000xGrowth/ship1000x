"""Tests for metadata-only/proxy collectors usage quality.

These fixtures do not read real Cline tasks, Cursor databases, prompts, or
responses. They prove that proxy sources now say "unknown/not_exposed" instead
of silently storing zero tokens/cost as if they were factual.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ship1000x.collectors import cline, cursor
from ship1000x.core.classifier import Classifier
from ship1000x.core.source_quality import build_source_quality_report
from ship1000x.core.storage import Storage


def _storage(tmp_path) -> Storage:
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    return storage


def _classifier_for(project_dir) -> Classifier:
    return Classifier.from_yaml_config(
        {
            "projects": [
                {
                    "id": "demo-project",
                    "paths": [str(project_dir) + "/*"],
                }
            ]
        }
    )


def test_cline_writes_explicit_unknown_usage_metadata(monkeypatch, tmp_path):
    project_dir = tmp_path / "demo-project"
    project_dir.mkdir()
    source_file = project_dir / "app.py"
    source_file.write_text("print('metadata only fixture')\n", encoding="utf-8")
    task_id = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    task_dir = tmp_path / "cline-tasks" / task_id
    task_dir.mkdir(parents=True)
    task_dir.joinpath("task_metadata.json").write_text(
        json.dumps(
            {
                "files_in_context": [{"path": str(source_file)}],
                "model_usage": [
                    {
                        "ts": int(task_id) + 60_000,
                        "model_id": "claude-sonnet-4-6",
                        "mode": "act",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    task_dir.joinpath("api_conversation_history.json").write_text(
        json.dumps(
            [
                {"role": "user", "content": "SECRET PROMPT SHOULD NOT BE STORED"},
                {"role": "assistant", "content": "SECRET RESPONSE SHOULD NOT BE STORED"},
            ]
        ),
        encoding="utf-8",
    )
    task_dir.joinpath("ui_messages.json").write_text(
        json.dumps(
            [
                {"type": "ask", "ts": int(task_id)},
                {"type": "ask", "ts": int(task_id) + 120_000},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cline, "CLINE_TASKS_DIR", task_dir.parent)

    storage = _storage(tmp_path)
    stats = cline.collect(storage, _classifier_for(project_dir), {})

    assert stats["events_ingested"] == 1
    row = storage.query("SELECT raw_meta FROM events WHERE source = 'cline'")[0]
    meta = json.loads(row["raw_meta"])
    usage = meta["usage"]
    assert usage["provider"] == "anthropic"
    assert usage["client"] == "cline"
    assert usage["model_canonical"] == "claude-sonnet-4-6"
    assert usage["quality"]["tokens"] == "unknown"
    assert usage["quality"]["cost"] == "unknown"
    assert usage["quality"]["active_time"] == "defensible"
    assert usage["provenance"]["token_source"] == "not_exposed"
    assert usage["cost"]["basis"] == "not_exposed_by_task_metadata"

    report = build_source_quality_report(storage, window_days=30)
    cline_row = next(item for item in report["rows"] if item["source"] == "cline")
    assert cline_row["risk"] == "partial"
    assert "cline" not in report["missing_usage_metadata_sources"]

    serialized = json.dumps(meta).lower()
    assert "secret prompt" not in serialized
    assert "secret response" not in serialized
    assert str(source_file).lower() not in serialized


def test_cursor_ai_blocks_write_explicit_unknown_usage_metadata(monkeypatch, tmp_path):
    project_dir = tmp_path / "demo-project"
    source_dir = project_dir / "src"
    source_dir.mkdir(parents=True)
    source_file = source_dir / "app.ts"
    source_file.write_text("export const fixture = true;\n", encoding="utf-8")
    cursor_db = tmp_path / "ai-code-tracking.db"
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    conn = sqlite3.connect(cursor_db)
    conn.execute(
        """
        CREATE TABLE ai_code_hashes (
            createdAt INTEGER,
            fileName TEXT,
            fileExtension TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO ai_code_hashes (createdAt, fileName, fileExtension) VALUES (?, ?, ?)",
        (now_ms, str(source_file), ".ts"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(cursor, "CURSOR_DB", cursor_db)

    storage = _storage(tmp_path)
    stats = cursor.collect(storage, _classifier_for(project_dir), {})

    assert stats["events_ingested"] == 1
    row = storage.query("SELECT raw_meta FROM events WHERE source = 'cursor'")[0]
    meta = json.loads(row["raw_meta"])
    usage = meta["usage"]
    assert usage["provider"] == "unknown"
    assert usage["client"] == "cursor-ai-tracking"
    assert usage["model_canonical"] == "unknown"
    assert usage["quality"]["tokens"] == "unknown"
    assert usage["quality"]["cost"] == "unknown"
    assert usage["quality"]["active_time"] == "defensible"
    assert usage["provenance"]["token_source"] == "not_exposed"
    assert usage["cost"]["basis"] == "not_exposed_by_ai_tracking_db"

    report = build_source_quality_report(storage, window_days=30)
    cursor_row = next(item for item in report["rows"] if item["source"] == "cursor")
    assert cursor_row["risk"] == "partial"
    assert "cursor" not in report["missing_usage_metadata_sources"]

    serialized = json.dumps(meta).lower()
    assert str(source_file).lower() not in serialized
    assert "prompt" not in serialized
    assert "response" not in serialized
