"""Tests for `ship1000x.core.reconcile`.

Pure tests: a temporary SQLite DB is built per test, no real local store is
read. No real provider API call is made.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

import pytest

from ship1000x.core.reconcile import (
    DEFAULT_RECONCILE_PAIRS,
    FALLBACK_MODEL_TOKENS,
    ReconcilePair,
    reconcile_pair,
    reconcile_pairs,
    render_markdown_report,
)


class InMemoryStorage:
    """Minimal Storage stub exposing the `query` API used by reconcile."""

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
        event_type: str = "session",
    ) -> None:
        self.conn.execute(
            "INSERT INTO events (id, source, event_type, started_at, cost_estimated, raw_meta) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                id,
                source,
                event_type,
                started_at,
                cost_estimated,
                json.dumps(raw_meta or {}),
            ),
        )
        self.conn.commit()

    def insert_many(self, rows: Iterable[dict]) -> None:
        for row in rows:
            self.insert(**row)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))


def _today_iso(day_offset: int = 0) -> str:
    """Return a 10-day-old ISO timestamp string. Using `now - 1 day` and back
    keeps tests deterministic against the substr(started_at, 1, 10) logic and
    SQLite `datetime('now', '-N days')` lookback.
    """
    from datetime import datetime, timedelta, timezone

    base = datetime.now(timezone.utc) - timedelta(days=day_offset)
    return base.strftime("%Y-%m-%dT%H:00:00")


def _factual_meta(cost_quality: str = "factual", model_raw: str = "claude-sonnet-4-7") -> dict:
    return {
        "usage": {
            "provider": "anthropic",
            "client": "claude-code",
            "model_raw": model_raw,
            "quality": {"tokens": "factual", "cost": cost_quality},
        }
    }


def test_default_pairs_covers_claude_and_codex():
    assert any(p.local == "claude_code" and p.billing == "anthropic_usage" for p in DEFAULT_RECONCILE_PAIRS)
    assert any(p.local == "codex" and p.billing == "openai_usage" for p in DEFAULT_RECONCILE_PAIRS)


def test_reconcile_pair_flags_warn_when_delta_above_threshold():
    storage = InMemoryStorage()
    day = _today_iso(1)
    # Local says $10, billing says $5 -> +100% delta, warn.
    storage.insert(
        id="local-1",
        source="claude_code",
        started_at=day,
        cost_estimated=10.0,
        raw_meta=_factual_meta(),
    )
    storage.insert(
        id="billing-1",
        source="anthropic_usage",
        started_at=day,
        cost_estimated=5.0,
        raw_meta=_factual_meta(),
    )

    report = reconcile_pair(
        storage,
        ReconcilePair(local="claude_code", billing="anthropic_usage", provider_label="anthropic"),
        window_days=7,
        threshold_pct=10.0,
    )

    assert len(report.days) == 1
    day_row = report.days[0]
    assert day_row.cost_local == pytest.approx(10.0)
    assert day_row.cost_billing == pytest.approx(5.0)
    assert day_row.status == "warn"
    assert day_row.delta_pct == pytest.approx(100.0)
    assert report.summary.days_warn == 1


def test_reconcile_pair_marks_missing_billing_when_only_local_has_data():
    storage = InMemoryStorage()
    storage.insert(
        id="local-1",
        source="claude_code",
        started_at=_today_iso(1),
        cost_estimated=4.0,
        raw_meta=_factual_meta(),
    )

    report = reconcile_pair(
        storage,
        ReconcilePair(local="claude_code", billing="anthropic_usage", provider_label="anthropic"),
        window_days=7,
    )

    assert report.summary.days_missing_billing == 1
    assert report.days[0].status == "missing_billing"
    assert report.days[0].delta_pct is None


def test_reconcile_pair_marks_missing_local_when_only_billing_has_data():
    storage = InMemoryStorage()
    storage.insert(
        id="billing-1",
        source="anthropic_usage",
        started_at=_today_iso(1),
        cost_estimated=4.0,
        raw_meta=_factual_meta(),
    )

    report = reconcile_pair(
        storage,
        ReconcilePair(local="claude_code", billing="anthropic_usage", provider_label="anthropic"),
        window_days=7,
    )

    assert report.summary.days_missing_local == 1
    assert report.days[0].status == "missing_local"


def test_reconcile_pair_quality_flags_non_factual_and_model_fallbacks():
    storage = InMemoryStorage()
    storage.insert(
        id="local-good",
        source="claude_code",
        started_at=_today_iso(1),
        cost_estimated=2.0,
        raw_meta=_factual_meta(cost_quality="factual", model_raw="claude-opus-4-7"),
    )
    storage.insert(
        id="local-unknown-cost",
        source="claude_code",
        started_at=_today_iso(1),
        cost_estimated=1.0,
        raw_meta=_factual_meta(cost_quality="unknown", model_raw="claude-opus-4-7"),
    )
    storage.insert(
        id="local-default-model",
        source="claude_code",
        started_at=_today_iso(2),
        cost_estimated=3.0,
        raw_meta=_factual_meta(cost_quality="factual", model_raw="default"),
    )

    report = reconcile_pair(
        storage,
        ReconcilePair(local="claude_code", billing="anthropic_usage", provider_label="anthropic"),
        window_days=7,
    )

    q = report.quality
    assert q.total_local_events == 3
    assert q.total_local_cost == pytest.approx(6.0)
    # Non-factual = the "unknown cost_quality" event.
    assert q.non_factual_count == 1
    assert q.non_factual_cost == pytest.approx(1.0)
    # Model fallback = the "default" model bucket.
    assert len(q.model_fallbacks) == 1
    assert q.model_fallbacks[0].model == "default"
    assert q.model_fallbacks[0].event_count == 1
    assert q.model_fallbacks[0].cost_total == pytest.approx(3.0)


def test_reconcile_pair_rejects_invalid_inputs():
    storage = InMemoryStorage()
    pair = ReconcilePair(local="claude_code", billing="anthropic_usage", provider_label="anthropic")
    with pytest.raises(ValueError):
        reconcile_pair(storage, pair, window_days=0)
    with pytest.raises(ValueError):
        reconcile_pair(storage, pair, window_days=7, threshold_pct=-1.0)


def test_fallback_model_tokens_includes_known_fallbacks():
    # Sanity check on the constant so future SHIP collectors that emit one of
    # these strings get reported automatically.
    assert "default" in FALLBACK_MODEL_TOKENS
    assert "all-models" in FALLBACK_MODEL_TOKENS
    assert "unknown" in FALLBACK_MODEL_TOKENS
    assert "" in FALLBACK_MODEL_TOKENS


def test_reconcile_pairs_runs_default_pairs_without_crashing_on_empty_store():
    storage = InMemoryStorage()
    reports = reconcile_pairs(storage, window_days=7)

    assert len(reports) == len(DEFAULT_RECONCILE_PAIRS)
    for report in reports:
        assert report.summary.total_local == 0.0
        assert report.summary.total_billing == 0.0
        assert report.summary.days_ok == 0


def test_render_markdown_report_is_safe_and_contains_required_sections():
    storage = InMemoryStorage()
    day = _today_iso(1)
    storage.insert(
        id="local-1",
        source="claude_code",
        started_at=day,
        cost_estimated=12.0,
        raw_meta=_factual_meta(cost_quality="unknown", model_raw="default"),
    )
    storage.insert(
        id="billing-1",
        source="anthropic_usage",
        started_at=day,
        cost_estimated=10.0,
        raw_meta=_factual_meta(),
    )

    report = reconcile_pair(
        storage,
        ReconcilePair(local="claude_code", billing="anthropic_usage", provider_label="anthropic"),
        window_days=7,
    )
    md = render_markdown_report([report])

    # Required structure.
    assert md.startswith("# SHIP cost reconciliation")
    assert "## claude_code vs anthropic_usage" in md
    assert "### Totals" in md
    assert "### Daily breakdown" in md
    assert "### Local-source quality issues" in md
    assert "#### Model fallbacks detected" in md
    # Forbidden markers must not leak from raw_meta into the report.
    lower = md.lower()
    for marker in ("prompt", "response", "diff --git", "/users/", "secret="):
        assert marker not in lower


def test_render_markdown_report_empty_input_returns_safe_placeholder():
    md = render_markdown_report([])
    assert "# SHIP cost reconciliation" in md
    assert "No pairs to report" in md


def test_reconcile_reads_model_stats_for_claude_code_shape():
    """Claude Code writes model_stats per day (no usage.model_raw).
    Reconcile must surface the model from model_stats keys instead of
    flagging the event as a `(empty)` model fallback.
    """
    storage = InMemoryStorage()
    storage.insert(
        id="cc-1",
        source="claude_code",
        started_at=_today_iso(1),
        cost_estimated=12.50,
        raw_meta={
            "session_id": "s1",
            "model_stats": {
                "claude-opus-4-7": {
                    "tokens_in": 500_000,
                    "tokens_out": 2_000,
                    "cache_read_tokens": 400_000,
                    "cache_write_tokens": 10_000,
                    "cost": 12.50,
                    "turns": 12,
                }
            },
            "usage": {
                "auth_mode": "oauth",
                "cost": {
                    "estimated_usd": 12.50,
                    "api_equivalent_usd": 12.50,
                    "billed_estimated_usd": 0.0,
                },
                "quality": {"cost": "factual"},
            },
        },
    )
    report = reconcile_pair(
        storage,
        ReconcilePair(local="claude_code", billing="anthropic_usage", provider_label="anthropic"),
        window_days=7,
    )
    # No model fallback bucket should appear because `claude-opus-4-7` is a
    # real model name, not in FALLBACK_MODEL_TOKENS.
    assert report.quality.model_fallbacks == []
    # The event is factual; no non-factual flag.
    assert report.quality.non_factual_count == 0
    assert report.quality.input_tokens == 500_000
    assert report.quality.output_tokens == 2_000
    assert report.quality.cached_input_tokens == 400_000
    assert report.quality.cache_write_tokens == 10_000
    assert report.quality.events_with_token_breakdown == 1


def test_reconcile_prefers_normalized_usage_tokens_over_model_stats():
    storage = InMemoryStorage()
    storage.insert(
        id="codex-1",
        source="codex",
        started_at=_today_iso(1),
        cost_estimated=1.5,
        raw_meta={
            "model_stats": {
                "gpt-5-codex": {
                    "tokens_in": 999_999,
                    "tokens_out": 999_999,
                    "cache_read_tokens": 999_999,
                    "cost": 1.5,
                }
            },
            "usage": {
                "auth_mode": "api_key",
                "tokens": {
                    "input_tokens": 12_000,
                    "output_tokens": 800,
                    "cached_input_tokens": 9_000,
                    "cache_write_tokens": 25,
                    "reasoning_tokens": 150,
                },
                "cost": {
                    "api_equivalent_usd": 1.5,
                    "billed_estimated_usd": 1.5,
                },
                "quality": {"cost": "factual"},
            },
        },
    )

    report = reconcile_pair(
        storage,
        ReconcilePair(local="codex", billing="openai_usage", provider_label="openai"),
        window_days=7,
    )

    assert report.quality.input_tokens == 12_000
    assert report.quality.output_tokens == 800
    assert report.quality.cached_input_tokens == 9_000
    assert report.quality.cache_write_tokens == 25
    assert report.quality.reasoning_tokens == 150
    assert report.quality.events_with_token_breakdown == 1


def test_reconcile_buckets_auth_modes_and_splits_cost_honestly():
    """Honest cost split must surface API-equivalent and billed-estimated
    separately, with OAuth events contributing 0 to billed_estimated."""
    storage = InMemoryStorage()
    storage.insert(
        id="oauth-1",
        source="claude_code",
        started_at=_today_iso(1),
        cost_estimated=10.0,
        raw_meta={
            "model_stats": {"claude-opus-4-7": {"cost": 10.0}},
            "usage": {
                "auth_mode": "oauth",
                "cost": {
                    "api_equivalent_usd": 10.0,
                    "billed_estimated_usd": 0.0,
                },
                "quality": {"cost": "factual"},
            },
        },
    )
    storage.insert(
        id="api-1",
        source="claude_code",
        started_at=_today_iso(1),
        cost_estimated=3.0,
        raw_meta={
            "model_stats": {"claude-sonnet-4-7": {"cost": 3.0}},
            "usage": {
                "auth_mode": "api_key",
                "cost": {
                    "api_equivalent_usd": 3.0,
                    "billed_estimated_usd": 3.0,
                },
                "quality": {"cost": "factual"},
            },
        },
    )
    report = reconcile_pair(
        storage,
        ReconcilePair(local="claude_code", billing="anthropic_usage", provider_label="anthropic"),
        window_days=7,
    )
    q = report.quality
    assert q.auth_mode_counts.get("oauth") == 1
    assert q.auth_mode_counts.get("api_key") == 1
    assert q.api_equivalent_cost == pytest.approx(13.0)
    assert q.billed_estimated_cost == pytest.approx(3.0)


def test_reconcile_markdown_report_includes_honest_split_and_auth_distribution():
    storage = InMemoryStorage()
    storage.insert(
        id="oauth-1",
        source="claude_code",
        started_at=_today_iso(1),
        cost_estimated=10.0,
        raw_meta={
            "model_stats": {"claude-opus-4-7": {"cost": 10.0}},
            "usage": {
                "auth_mode": "oauth",
                "tokens": {
                    "input_tokens": 10_000,
                    "output_tokens": 400,
                    "cached_input_tokens": 8_000,
                    "cache_write_tokens": 100,
                    "reasoning_tokens": 25,
                },
                "cost": {
                    "api_equivalent_usd": 10.0,
                    "billed_estimated_usd": 0.0,
                },
                "quality": {"cost": "factual"},
            },
        },
    )
    report = reconcile_pair(
        storage,
        ReconcilePair(local="claude_code", billing="anthropic_usage", provider_label="anthropic"),
        window_days=7,
    )
    md = render_markdown_report([report])
    assert "Cost honest split" in md
    assert "API-equivalent cost" in md
    assert "Billed-estimated cost" in md
    assert "Local API-equivalent total" in md
    assert "Billing-side total" in md
    assert "provider invoice" not in md
    assert "| Model | Events | Cost |" not in md
    assert "Subscription absorption" in md
    assert "Auth mode distribution" in md
    assert "`oauth`" in md
    assert "Token breakdown" in md
    assert "Input tokens (collector-reported)" in md
    assert "Cached input tokens" in md
    assert "Reasoning tokens" in md
    assert "8,000" in md
