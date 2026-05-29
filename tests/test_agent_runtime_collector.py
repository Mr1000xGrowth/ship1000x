"""Tests for the agent_runtime collector (Wave 3 / Day 3-5).

Verifies the per-(process, minute) aggregation, idempotency,
fail-safe behaviour on malformed records, and no-op when the drop
directory is absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ship1000x.collectors import agent_runtime as collector
from ship1000x.core.storage import Storage


class _StubClassifier:
    rules: list = []

    def classify_session(self, paths=None, cwd=None, git_remote=None):
        return ("unclassified", 0.0)


def _write_drop_file(tmp_path: Path, lines: list[dict]) -> Path:
    drop_dir = tmp_path / "drop" / "watch"
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


def test_aggregates_ticks_per_process_per_minute(storage, tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "watch")
    _write_drop_file(tmp_path, [
        # 10:30 — claude with 2 PIDs, port 8000, no MCP
        {"captured_at": "2026-05-23T10:30:00Z", "processes": [
            {"name": "claude", "pid_count": 2, "listening_ports": [8000], "mcp_likely": False},
        ]},
        # 10:30 — claude with 3 PIDs (max bumps to 3), port 8001
        {"captured_at": "2026-05-23T10:30:30Z", "processes": [
            {"name": "claude", "pid_count": 3, "listening_ports": [8001], "mcp_likely": False},
        ]},
        # 10:31 — claude back to 2 PIDs, MCP sibling appears
        {"captured_at": "2026-05-23T10:31:10Z", "processes": [
            {"name": "claude", "pid_count": 2, "listening_ports": [8000], "mcp_likely": False},
            {"name": "mcp-server-fs", "pid_count": 1, "listening_ports": [], "mcp_likely": True},
        ]},
    ])
    stats = collector.collect(storage, _StubClassifier(), privacy_config={})
    # Expect 3 events: claude@10:30, claude@10:31, mcp-server-fs@10:31
    assert stats["events_ingested"] == 3
    assert stats["ticks_aggregated"] == 4  # claude appears 3x + mcp 1x

    rows = storage.query(
        "SELECT raw_meta FROM events WHERE source = 'agent_runtime' ORDER BY started_at"
    )
    metas = [json.loads(r["raw_meta"]) for r in rows]
    by_proc_min = {(m["process_name"], i): m for i, m in enumerate(metas)}
    _ = by_proc_min  # for debugging

    # Find claude minute1: max_concurrent should be 3, ports = {8000, 8001}
    claude_min1 = next(m for m in metas if m["process_name"] == "claude" and m["process_tick_count"] == 2)
    assert claude_min1["max_concurrent_processes"] == 3
    assert claude_min1["open_ports_count"] == 2
    assert claude_min1["mcp_servers_detected"] == 0

    # Find claude minute2 (10:31): single tick, 1 port, no MCP itself
    claude_min2 = next(m for m in metas if m["process_name"] == "claude" and m["process_tick_count"] == 1)
    assert claude_min2["max_concurrent_processes"] == 2
    assert claude_min2["open_ports_count"] == 1
    assert claude_min2["mcp_servers_detected"] == 0

    # MCP server bucket has mcp_servers_detected == 1
    mcp_meta = next(m for m in metas if m["process_name"] == "mcp-server-fs")
    assert mcp_meta["mcp_servers_detected"] == 1


def test_collector_is_idempotent(storage, tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "watch")
    _write_drop_file(tmp_path, [
        {"captured_at": "2026-05-23T10:30:00Z", "processes": [
            {"name": "claude", "pid_count": 1, "listening_ports": [],
             "mcp_likely": False},
        ]},
    ])
    collector.collect(storage, _StubClassifier(), privacy_config={})
    collector.collect(storage, _StubClassifier(), privacy_config={})
    rows = storage.query(
        "SELECT id FROM events WHERE source = 'agent_runtime'"
    )
    assert len(rows) == 1


def test_collector_skips_invalid_records(storage, tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "watch")
    drop_dir = tmp_path / "drop" / "watch"
    drop_dir.mkdir(parents=True)
    (drop_dir / "2026-05-23.jsonl").write_text(
        # Good record
        '{"captured_at": "2026-05-23T10:30:00Z", "processes": ['
        '{"name": "claude", "pid_count": 1, "listening_ports": [], "mcp_likely": false}'
        ']}\n'
        # Malformed JSON
        '{ broken\n'
        # Not a dict
        '"not a dict"\n'
        # Missing captured_at
        '{"processes": [{"name": "x", "pid_count": 1}]}\n'
        # processes not a list
        '{"captured_at": "2026-05-23T10:31:00Z", "processes": "wrong"}\n'
        # Process without name — ignored
        '{"captured_at": "2026-05-23T10:32:00Z", "processes": ['
        '{"pid_count": 1, "listening_ports": [], "mcp_likely": false}'
        ']}\n',
        encoding="utf-8",
    )
    stats = collector.collect(storage, _StubClassifier(), privacy_config={})
    # Only the first record produces an event.
    assert stats["events_ingested"] == 1


def test_collector_no_op_when_drop_dir_missing(storage, tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "absent")
    stats = collector.collect(storage, _StubClassifier(), privacy_config={})
    assert stats["events_ingested"] == 0
    assert stats["files_seen"] == 0


def test_collector_ports_union_caps_correctly(storage, tmp_path, monkeypatch):
    """Port count in the event = size of the union across ticks in a minute."""
    monkeypatch.setattr(collector, "DROP_DIR", tmp_path / "drop" / "watch")
    _write_drop_file(tmp_path, [
        {"captured_at": "2026-05-23T10:30:00Z", "processes": [
            {"name": "ollama", "pid_count": 1, "listening_ports": [11434, 11435], "mcp_likely": False},
        ]},
        {"captured_at": "2026-05-23T10:30:30Z", "processes": [
            {"name": "ollama", "pid_count": 1, "listening_ports": [11434, 11436], "mcp_likely": False},
        ]},
    ])
    collector.collect(storage, _StubClassifier(), privacy_config={})
    rows = storage.query(
        "SELECT raw_meta FROM events WHERE source = 'agent_runtime'"
    )
    meta = json.loads(rows[0]["raw_meta"])
    # Union: {11434, 11435, 11436} => 3 distinct ports
    assert meta["open_ports_count"] == 3
