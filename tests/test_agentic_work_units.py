"""Tests for the agentic-work-unit additions (2026-07-01).

Covers three gaps found while cross-checking ship1000x against the raw
Codex/Claude sources (memory: project-activity-tracking-ship-sight1000x):

1. Claude Code sub-agent journals (``<project>/subagents/**/*.jsonl``) were
   never scanned — ``iter_session_files`` only globbed one level deep, so
   ~86% of real JSONL files (agentId/isSidechain Task-tool spawns) were
   silently skipped.
2. Codex thread role (root vs sub-agent) via ``thread_spawn_edges`` in
   ``~/.codex/state_5.sqlite`` was never read.
3. Cost estimators had no "no-cache" bound (cost as if the prompt-cache
   discount never applied) to size the cache's real savings.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ship1000x.collectors.claude_code import iter_session_files, parse_session_file
from ship1000x.collectors.codex_sqlite import (
    compute_thread_roles,
    list_thread_spawn_edges,
    list_threads,
)
from ship1000x.core.pricing import estimate_anthropic_cost, estimate_openai_cost


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


# ─── 1. Claude Code sub-agent discovery ──────────────────────────────


def test_iter_session_files_discovers_nested_subagent_journals(tmp_path):
    """Real layout: subagents/ lives under the session that spawned it —
    <slug>/<session-uuid>/subagents/**/*.jsonl — not directly under <slug>/."""
    top = tmp_path / "-Users-x-project" / "0001.jsonl"
    _write_jsonl(top, [{"type": "user", "timestamp": "2026-07-01T10:00:00Z"}])
    nested = (
        tmp_path
        / "-Users-x-project"
        / "0001"
        / "subagents"
        / "workflows"
        / "wf_123"
        / "journal.jsonl"
    )
    _write_jsonl(nested, [{"type": "user", "timestamp": "2026-07-01T10:05:00Z"}])
    flat_subagent = tmp_path / "-Users-x-project" / "0001" / "subagents" / "agent-abc.jsonl"
    _write_jsonl(flat_subagent, [{"type": "user", "timestamp": "2026-07-01T10:10:00Z"}])

    found = {p for p in iter_session_files(tmp_path)}
    assert top in found
    assert nested in found
    assert flat_subagent in found


def test_parse_session_file_tags_subagent_and_derives_stable_id(tmp_path):
    """Nested workflow journals all share the stem 'journal' — the parser
    must fall back to agentId (or a path hash) so two different sub-agents
    never collide on the same session_id."""
    session = tmp_path / "0001" / "subagents" / "workflows" / "wf_123" / "journal.jsonl"
    _write_jsonl(
        session,
        [
            {
                "type": "user",
                "agentId": "aabbccdd",
                "sessionId": "parent-session-1",
                "isSidechain": True,
                "cwd": "/Users/x/project",
                "timestamp": "2026-07-01T10:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "go"}]},
            },
        ],
    )
    parsed = parse_session_file(session)
    assert parsed["is_subagent"] is True
    assert parsed["agent_id"] == "aabbccdd"
    assert parsed["parent_session_id"] == "parent-session-1"
    assert parsed["session_id"] == "aabbccdd"


def test_two_nested_journals_get_distinct_session_ids(tmp_path):
    a = tmp_path / "0001" / "subagents" / "workflows" / "wf_a" / "journal.jsonl"
    b = tmp_path / "0001" / "subagents" / "workflows" / "wf_b" / "journal.jsonl"
    _write_jsonl(a, [{"type": "user", "agentId": "agent-a", "isSidechain": True, "timestamp": "t"}])
    _write_jsonl(b, [{"type": "user", "agentId": "agent-b", "isSidechain": True, "timestamp": "t"}])
    assert parse_session_file(a)["session_id"] != parse_session_file(b)["session_id"]


def test_top_level_session_is_not_flagged_subagent(tmp_path):
    session = tmp_path / "0001.jsonl"
    _write_jsonl(
        session,
        [{"type": "user", "timestamp": "2026-07-01T10:00:00Z", "sessionId": "s1"}],
    )
    parsed = parse_session_file(session)
    assert parsed["is_subagent"] is False
    assert parsed["session_id"] == "0001"


# ─── 2. Codex thread roles via thread_spawn_edges ────────────────────


def _make_state_db(path: Path, threads: list[dict], edges: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE threads (
            id TEXT, rollout_path TEXT, created_at TEXT, updated_at TEXT,
            source TEXT, model_provider TEXT, cwd TEXT, title TEXT,
            tokens_used INTEGER, git_sha TEXT, git_branch TEXT,
            git_origin_url TEXT, cli_version TEXT, first_user_message TEXT,
            archived INTEGER
        )"""
    )
    conn.execute(
        """CREATE TABLE thread_spawn_edges (
            parent_thread_id TEXT, child_thread_id TEXT, status TEXT
        )"""
    )
    for t in threads:
        conn.execute(
            """INSERT INTO threads (id, rollout_path, cwd, title, git_origin_url)
               VALUES (?, ?, ?, ?, ?)""",
            (t["id"], t.get("rollout_path", ""), t.get("cwd", ""), t.get("title", ""), t.get("git_origin_url", "")),
        )
    for e in edges:
        conn.execute(
            "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status) VALUES (?, ?, ?)",
            (e["parent_thread_id"], e["child_thread_id"], e.get("status", "open")),
        )
    conn.commit()
    conn.close()


def test_compute_thread_roles_flags_children_as_subagent(tmp_path):
    db = tmp_path / "state_5.sqlite"
    _make_state_db(
        db,
        threads=[{"id": "t-root"}, {"id": "t-child"}, {"id": "t-lone"}],
        edges=[{"parent_thread_id": "t-root", "child_thread_id": "t-child"}],
    )
    threads = list_threads(db)
    edges = list_thread_spawn_edges(db)
    roles = compute_thread_roles(threads, edges)

    assert roles["t-root"] == {"role": "root", "parent_thread_id": None}
    assert roles["t-child"] == {"role": "subagent", "parent_thread_id": "t-root"}
    assert roles["t-lone"] == {"role": "root", "parent_thread_id": None}


def test_list_thread_spawn_edges_missing_db_returns_empty(tmp_path):
    assert list_thread_spawn_edges(tmp_path / "does-not-exist.sqlite") == []


# ─── 3. No-cache cost bound ───────────────────────────────────────────


def test_anthropic_no_cache_cost_is_at_least_with_cache_cost():
    with_cache = estimate_anthropic_cost(
        "claude-opus-4-8", tokens_input=0, tokens_output=0,
        cache_read_tokens=100_000, cache_write_tokens=0,
    )
    no_cache = estimate_anthropic_cost(
        "claude-opus-4-8", tokens_input=0, tokens_output=0,
        cache_read_tokens=100_000, cache_write_tokens=0,
        cache_discount=False,
    )
    assert no_cache > with_cache >= 0


def test_openai_no_cache_cost_is_at_least_with_cache_cost():
    with_cache = estimate_openai_cost(
        "gpt-5", tokens_input=100_000, tokens_output=0, cached_input_tokens=100_000,
    )
    no_cache = estimate_openai_cost(
        "gpt-5", tokens_input=100_000, tokens_output=0, cached_input_tokens=100_000,
        cache_discount=False,
    )
    assert no_cache > with_cache >= 0


def test_no_cache_cost_equals_with_cache_when_no_cache_tokens():
    a = estimate_anthropic_cost("claude-opus-4-8", tokens_input=1000, tokens_output=200)
    b = estimate_anthropic_cost(
        "claude-opus-4-8", tokens_input=1000, tokens_output=200, cache_discount=False
    )
    assert a == b
