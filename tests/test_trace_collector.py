"""Tests for the trace bridge collector.

The collector reads ``~/.ship1000x/drop/trace/<date>.jsonl`` files
written by an optional local usage proxy and emits one ``api_call`` event per
drop line. These tests build synthetic drop files and assert the
collector produces correctly-shaped, idempotent events.

No real proxy or network involvement — pure filesystem in / SHIP
store out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ship1000x.collectors import trace as collector
from ship1000x.core.storage import Storage


class _StubClassifier:
    rules: list = []

    def classify_session(self, paths=None, cwd=None, git_remote=None):
        return ("unclassified", 0.0)


def _drop_line(
    *,
    provider="anthropic",
    model="claude-opus-4-7",
    captured_at="2026-05-23T22:00:00+00:00",
    endpoint="api.anthropic.com/v1/messages",
    input_tokens=15,
    output_tokens=50,
    cache_read=0,
    cache_creation=0,
    usage_quality="factual",
):
    return {
        "provider": provider,
        "model": model,
        "captured_at": captured_at,
        "endpoint": endpoint,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "usage_quality": usage_quality,
    }


def _write_drop_file(tmp_path: Path, lines: list[dict]) -> Path:
    drop_dir = tmp_path / "drop" / "trace"
    drop_dir.mkdir(parents=True, exist_ok=True)
    f = drop_dir / "2026-05-23.jsonl"
    f.write_text(
        "\n".join(json.dumps(line) for line in lines),
        encoding="utf-8",
    )
    return f


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "store.sqlite")
    s.init_schema()
    return s


class TestCollectHappyPath:
    def test_writes_one_event_per_drop_line(self, storage, tmp_path, monkeypatch):
        monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "trace")
        _write_drop_file(tmp_path, [
            _drop_line(input_tokens=10, output_tokens=20),
            _drop_line(captured_at="2026-05-23T22:01:00+00:00",
                       input_tokens=5, output_tokens=15),
        ])
        stats = collector.collect(storage, _StubClassifier(), privacy_config={})
        assert stats["events_ingested"] == 2
        assert stats["files_seen"] == 1
        assert stats["skipped"] == 0
        rows = storage.query("SELECT * FROM events WHERE source = 'trace'")
        assert len(rows) == 2

    def test_event_carries_token_counts_and_provider(self, storage, tmp_path, monkeypatch):
        monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "trace")
        _write_drop_file(tmp_path, [
            _drop_line(
                provider="openai", model="gpt-5.5",
                input_tokens=100, output_tokens=200,
            ),
        ])
        collector.collect(storage, _StubClassifier(), privacy_config={})
        row = storage.query("SELECT * FROM events WHERE source = 'trace'")[0]
        assert row["token_input"] == 100
        assert row["token_output"] == 200
        meta = json.loads(row["raw_meta"])
        assert meta["provider"] == "openai"
        assert meta["model"] == "gpt-5.5"
        assert meta["usage_quality"] == "factual"

    def test_cache_tokens_in_raw_meta(self, storage, tmp_path, monkeypatch):
        monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "trace")
        _write_drop_file(tmp_path, [
            _drop_line(cache_read=500, cache_creation=42),
        ])
        collector.collect(storage, _StubClassifier(), privacy_config={})
        row = storage.query("SELECT * FROM events WHERE source = 'trace'")[0]
        meta = json.loads(row["raw_meta"])
        assert meta["cache_read_input_tokens"] == 500
        assert meta["cache_creation_input_tokens"] == 42

    def test_confidence_flag_maps_from_usage_quality(self, storage, tmp_path, monkeypatch):
        monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "trace")
        _write_drop_file(tmp_path, [
            _drop_line(usage_quality="factual", captured_at="2026-05-23T22:00:00+00:00"),
            _drop_line(usage_quality="estimated", captured_at="2026-05-23T22:01:00+00:00"),
            _drop_line(usage_quality="unknown_label", captured_at="2026-05-23T22:02:00+00:00"),
        ])
        collector.collect(storage, _StubClassifier(), privacy_config={})
        rows = storage.query(
            "SELECT confidence_flag FROM events "
            "WHERE source = 'trace' ORDER BY started_at"
        )
        confs = [r["confidence_flag"] for r in rows]
        assert confs == ["high", "medium", "low"]


class TestCollectIdempotency:
    def test_re_running_does_not_duplicate(self, storage, tmp_path, monkeypatch):
        monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "trace")
        _write_drop_file(tmp_path, [
            _drop_line(captured_at="2026-05-23T22:00:00+00:00"),
        ])
        collector.collect(storage, _StubClassifier(), privacy_config={})
        collector.collect(storage, _StubClassifier(), privacy_config={})
        rows = storage.query("SELECT id FROM events WHERE source = 'trace'")
        assert len(rows) == 1

    def test_stable_id_across_different_runs_with_same_metadata(
        self, storage, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "trace")
        _write_drop_file(tmp_path, [
            _drop_line(provider="anthropic", model="claude-opus-4-7",
                       captured_at="2026-05-23T22:00:00+00:00"),
        ])
        collector.collect(storage, _StubClassifier(), privacy_config={})
        first_id = storage.query(
            "SELECT id FROM events WHERE source = 'trace'"
        )[0]["id"]
        # Different drop file (different mtime), same record content
        # -> same stable id, no duplicate.
        new_drop = tmp_path / "drop" / "trace" / "2026-05-23.jsonl"
        new_drop.write_text(
            json.dumps(_drop_line(
                provider="anthropic", model="claude-opus-4-7",
                captured_at="2026-05-23T22:00:00+00:00",
            )) + "\n",
            encoding="utf-8",
        )
        # Tweak mtime to force the file to be re-read
        import os
        import time
        os.utime(new_drop, (time.time() + 1, time.time() + 1))
        collector.collect(storage, _StubClassifier(), privacy_config={})
        rows = storage.query("SELECT id FROM events WHERE source = 'trace'")
        assert len(rows) == 1
        assert rows[0]["id"] == first_id


class TestCollectMalformed:
    def test_skips_lines_without_required_fields(
        self, storage, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "trace")
        drop_dir = tmp_path / "drop" / "trace"
        drop_dir.mkdir(parents=True)
        (drop_dir / "2026-05-23.jsonl").write_text(
            # Good
            json.dumps(_drop_line()) + "\n"
            # Malformed JSON
            + "{ broken\n"
            # Not a dict
            + json.dumps("string only") + "\n"
            # Missing provider
            + json.dumps({
                "model": "x", "captured_at": "2026-05-23T22:00:00+00:00",
            }) + "\n"
            # Missing model
            + json.dumps({
                "provider": "x", "captured_at": "2026-05-23T22:00:00+00:00",
            }) + "\n"
            # Missing captured_at
            + json.dumps({"provider": "x", "model": "y"}) + "\n",
            encoding="utf-8",
        )
        stats = collector.collect(storage, _StubClassifier(), privacy_config={})
        assert stats["events_ingested"] == 1
        assert stats["skipped"] >= 4

    def test_no_op_when_drop_dir_missing(self, storage, tmp_path, monkeypatch):
        monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "no" / "such" / "dir")
        stats = collector.collect(storage, _StubClassifier(), privacy_config={})
        assert stats["events_ingested"] == 0
        assert stats["files_seen"] == 0


class TestPrivacyPassthrough:
    def test_raw_meta_after_sanitize_preserves_provider(
        self, storage, tmp_path, monkeypatch,
    ):
        """Defence-in-depth: the proxy already redacted; SHIP's
        sanitize_event runs again at storage time. The new
        ALLOWED_META_KEYS entries (provider, endpoint, usage_quality,
        cache_*_input_tokens) must survive."""
        monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "trace")
        _write_drop_file(tmp_path, [_drop_line(
            provider="anthropic", endpoint="api.anthropic.com/v1/messages",
            cache_read=10, cache_creation=20, usage_quality="factual",
        )])
        collector.collect(storage, _StubClassifier(), privacy_config={})
        row = storage.query("SELECT raw_meta FROM events WHERE source = 'trace'")[0]
        meta = json.loads(row["raw_meta"])
        assert "provider" in meta
        assert "endpoint" in meta
        assert "usage_quality" in meta
        assert "cache_read_input_tokens" in meta
        assert "cache_creation_input_tokens" in meta
