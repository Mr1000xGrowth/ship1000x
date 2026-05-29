"""Tests for the cost-audit drill-down module."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

import pytest

from ship1000x.core.cost_audit import (
    PerModelBreakdown,
    audit_source,
    render_markdown_report,
)


class InMemoryStorage:
    """Storage stub mirroring what `_get_storage().query()` returns."""

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
                cost_estimated REAL,
                raw_meta TEXT
            )
            """
        )

    def insert(
        self,
        *,
        id: str,
        source: str,
        started_at: str,
        cost_estimated: float,
        raw_meta: dict | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events (id, source, event_type, started_at, "
            "cost_estimated, raw_meta) VALUES (?, ?, 'session', ?, ?, ?)",
            (id, source, started_at, cost_estimated, json.dumps(raw_meta or {})),
        )
        self.conn.commit()

    def insert_many(self, rows: Iterable[dict]) -> None:
        for r in rows:
            self.insert(**r)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))


def _today_iso(days_ago: int = 0) -> str:
    from datetime import datetime, timedelta, timezone

    base = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return base.strftime("%Y-%m-%dT%H:00:00")


def _claude_meta(
    model: str,
    *,
    uncached: int,
    cache_read: int,
    cache_write: int,
    output: int,
    cost: float,
) -> dict:
    return {
        "session_id": "session-x",
        "model_stats": {
            model: {
                "tokens_in": uncached + cache_read + cache_write,
                "tokens_out": output,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "cost": cost,
                "turns": 1,
            }
        },
        "usage": {
            "auth_mode": "oauth",
            "tokens": {
                "input_tokens": uncached,
                "output_tokens": output,
                "cached_input_tokens": cache_read,
                "cache_write_tokens": cache_write,
            },
            "cost": {"estimated_usd": cost, "quality": "factual"},
        },
    }


def test_per_model_breakdown_computes_each_cost_component():
    breakdown = PerModelBreakdown(
        model="claude-opus-4-7",
        rates={"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
        uncached_input_tokens=10_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=500_000,
        output_tokens=20_000,
    )
    # Expected:
    # uncached: 10_000 * 15 / 1e6 = 0.15
    # cache_read: 1_000_000 * 1.5 / 1e6 = 1.5
    # cache_write: 500_000 * 18.75 / 1e6 = 9.375
    # output: 20_000 * 75 / 1e6 = 1.5
    # total: 12.525
    assert breakdown.cost_uncached == pytest.approx(0.15)
    assert breakdown.cost_cache_read == pytest.approx(1.5)
    assert breakdown.cost_cache_write == pytest.approx(9.375)
    assert breakdown.cost_output == pytest.approx(1.5)
    assert breakdown.cost_total == pytest.approx(12.525)


def test_audit_source_recomputes_stored_cost_within_floating_point_error():
    """The whole point of the audit: stored cost must equal recomputed
    from the same pricing.py rates. A divergence > $0.01 on a typical
    Opus event signals either a stale pricing entry, a model_stats
    schema mismatch, or a collector bug."""
    storage = InMemoryStorage()
    # Real-shape Claude Code event from the user's store: a typical
    # heavy-cache Opus day. cost is what the collector computed and
    # what audit will recompute from the same tokens.
    storage.insert(
        id="e1",
        source="claude_code",
        started_at=_today_iso(1),
        cost_estimated=1125.69,
        raw_meta=_claude_meta(
            "claude-opus-4-7",
            uncached=18_014,
            cache_read=497_748_503,
            cache_write=16_196_709,
            output=1_001_444,
            cost=1125.69,
        ),
    )
    report = audit_source(storage, "claude_code", window_days=7, top=10)

    assert report.summary.total_events == 1
    assert report.summary.total_cost_stored == pytest.approx(1125.69)
    # Recomputed must match the stored cost to within rounding noise.
    assert report.summary.total_cost_recomputed == pytest.approx(1125.69, abs=0.01)
    assert abs(report.summary.max_abs_delta) < 0.01
    assert report.summary.auth_mode_counts.get("oauth") == 1


def test_audit_source_returns_empty_when_window_has_no_events():
    storage = InMemoryStorage()
    report = audit_source(storage, "claude_code", window_days=7, top=5)
    assert report.summary.total_events == 0
    assert report.events == []


def test_audit_source_rejects_bad_inputs():
    storage = InMemoryStorage()
    with pytest.raises(ValueError):
        audit_source(storage, "claude_code", window_days=0)
    with pytest.raises(ValueError):
        audit_source(storage, "claude_code", window_days=7, top=0)


def test_render_markdown_report_includes_required_sections_and_is_safe():
    storage = InMemoryStorage()
    storage.insert(
        id="e1",
        source="claude_code",
        started_at=_today_iso(1),
        cost_estimated=12.5,
        raw_meta=_claude_meta(
            "claude-opus-4-7",
            uncached=1_000,
            cache_read=1_000_000,
            cache_write=100_000,
            output=10_000,
            cost=12.5,
        ),
    )
    report = audit_source(storage, "claude_code", window_days=7, top=5)
    md = render_markdown_report([report])

    assert md.startswith("# SHIP cost audit")
    assert "## claude_code" in md
    assert "### Summary" in md
    assert "Total API-equivalent cost stored" in md
    assert "Total API-equivalent cost recomputed" in md
    assert "does not claim invoice-grade billed cost" in md
    assert "claude-opus-4-7" in md
    # Privacy scan on the rendered output: no event payload markers leak.
    lower = md.lower()
    for marker in ("prompt", "response", "diff --git", "/users/", "secret="):
        assert marker not in lower
