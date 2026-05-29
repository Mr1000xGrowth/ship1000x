"""Tests for the coverage health report.

These exercise the static registry, the per-source health computation
against a synthetic SQLite store, and the Markdown / JSON renderers.
No real `~/.config/ship1000x` data is read.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ship1000x.core.coverage import (
    SOURCE_REGISTRY,
    SOURCE_STATUS_ACTIVE,
    SOURCE_STATUS_NOT_SUPPORTED,
    SOURCE_STATUS_SUPPORTED,
    SourceEntry,
    compute_coverage,
    compute_source_health,
    render_json_report,
    render_markdown_report,
)
from ship1000x.core.storage import Storage


@pytest.fixture
def storage(tmp_path):
    """Empty SHIP store with the canonical schema."""
    db = Storage(tmp_path / "test.sqlite")
    db.init_schema()
    return db


def _insert_event(storage, source: str, started_at: str, raw_meta: dict | None = None):
    """Minimal event insert for the coverage tests.

    The columns mirror what `sanitize_event` would produce so the
    coverage logic sees realistic rows.
    """
    event = {
        "id": f"{source}-{started_at}",
        "source": source,
        "event_type": "session_day",
        "started_at": started_at,
        "ended_at": started_at,
        "duration_sec": 60,
        "wall_clock_sec": 60,
        "cwd": None,
        "project_id": "test",
        "project_conf": 0.9,
        "tool_or_action": f"{source}_session",
        "token_input": 0,
        "token_output": 0,
        "cost_estimated": 0.0,
        "user_msg_type": None,
        "wordcount": 0,
        "confidence_flag": "high",
        "raw_meta": json.dumps(raw_meta) if raw_meta else None,
    }
    storage.upsert_event(event)


def _now_iso(days_ago: int = 0) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).isoformat()


class TestSourceRegistry:
    def test_registry_is_non_empty_and_distinct(self):
        ids = [entry.source_id for entry in SOURCE_REGISTRY]
        assert len(ids) >= 20
        assert len(ids) == len(set(ids)), "duplicate source_id in registry"

    def test_every_entry_has_required_status(self):
        valid_statuses = {
            SOURCE_STATUS_ACTIVE,
            SOURCE_STATUS_SUPPORTED,
            SOURCE_STATUS_NOT_SUPPORTED,
        }
        for entry in SOURCE_REGISTRY:
            assert entry.status in valid_statuses, entry

    def test_not_supported_entries_have_audit_ref_and_reason(self):
        """Every audit-flagged gap must justify why we say so."""
        for entry in SOURCE_REGISTRY:
            if entry.status == SOURCE_STATUS_NOT_SUPPORTED:
                assert entry.audit_ref, f"{entry.source_id} missing audit_ref"
                assert entry.reason, f"{entry.source_id} missing reason"

    def test_openai_category_covers_core_codex_sources(self):
        """Regression: the OpenAI section must surface every Codex
        source SHIP collects, plus the known gaps."""
        openai_ids = {
            entry.source_id
            for entry in SOURCE_REGISTRY
            if entry.category == "openai"
        }
        assert {"codex", "codex_desktop", "codex_macapp", "openai_usage"} <= openai_ids
        # And the documented gaps are still listed as not_supported
        gaps = {
            entry.source_id
            for entry in SOURCE_REGISTRY
            if entry.category == "openai"
            and entry.status == SOURCE_STATUS_NOT_SUPPORTED
        }
        assert {"chatgpt_desktop", "chatgpt_web", "openai_api_trace"} <= gaps


class TestComputeSourceHealth:
    def test_not_supported_source_returns_zero_metrics(self, storage):
        entry = SourceEntry(
            source_id="chatgpt_desktop",
            display_name="ChatGPT Desktop standalone (macOS)",
            category="openai",
            status=SOURCE_STATUS_NOT_SUPPORTED,
            audit_ref="08",
            reason="not_yet",
        )
        h = compute_source_health(storage, entry, since_days=30)
        assert h.status == SOURCE_STATUS_NOT_SUPPORTED
        assert h.event_count == 0
        assert h.first_event is None

    def test_supported_with_no_events_stays_supported(self, storage):
        entry = SourceEntry(
            source_id="cursor",
            display_name="Cursor",
            category="coding_ide",
            status=SOURCE_STATUS_SUPPORTED,
        )
        h = compute_source_health(storage, entry, since_days=30)
        assert h.status == SOURCE_STATUS_SUPPORTED
        assert h.event_count == 0

    def test_active_source_aggregates_event_counts_and_dates(self, storage):
        entry = SourceEntry(
            source_id="claude_code",
            display_name="Claude Code (CLI / Desktop / SDK)",
            category="anthropic",
            status=SOURCE_STATUS_SUPPORTED,
        )
        _insert_event(storage, "claude_code", _now_iso(days_ago=5))
        _insert_event(storage, "claude_code", _now_iso(days_ago=2))
        _insert_event(storage, "claude_code", _now_iso(days_ago=1))
        h = compute_source_health(storage, entry, since_days=30)
        assert h.status == SOURCE_STATUS_ACTIVE
        assert h.event_count == 3
        assert h.days_covered == 3

    def test_active_source_counts_unknown_models(self, storage):
        entry = SourceEntry(
            source_id="codex_macapp",
            display_name="Codex.app macOS",
            category="openai",
            status=SOURCE_STATUS_SUPPORTED,
        )
        _insert_event(
            storage, "codex_macapp", _now_iso(days_ago=1),
            raw_meta={"usage": {"model_raw": "gpt-5.5", "auth_mode": "oauth"}},
        )
        _insert_event(
            storage, "codex_macapp", _now_iso(days_ago=2),
            raw_meta={"usage": {"model_raw": "unknown", "auth_mode": "unknown"}},
        )
        _insert_event(
            storage, "codex_macapp", _now_iso(days_ago=3),
            raw_meta={"usage": {"auth_mode": "unknown"}},
        )
        h = compute_source_health(storage, entry, since_days=30)
        # 1 event has a real model, 2 are unknown (one explicit "unknown",
        # one with no model_raw key at all)
        assert h.unknown_model_count == 2
        assert h.auth_mode_counts == {"oauth": 1, "unknown": 2}

    def test_active_source_accumulates_cost_totals(self, storage):
        entry = SourceEntry(
            source_id="claude_code",
            display_name="Claude Code",
            category="anthropic",
            status=SOURCE_STATUS_SUPPORTED,
        )
        _insert_event(
            storage, "claude_code", _now_iso(days_ago=1),
            raw_meta={
                "usage": {
                    "model_raw": "claude-opus-4-7",
                    "auth_mode": "oauth",
                    "cost": {
                        "api_equivalent_usd": 100.0,
                        "billed_estimated_usd": 0.0,
                        "basis": "native_message_usage",
                        "quality": "factual",
                    },
                    "quality": {"cost": "factual"},
                }
            },
        )
        _insert_event(
            storage, "claude_code", _now_iso(days_ago=2),
            raw_meta={
                "usage": {
                    "model_raw": "claude-opus-4-7",
                    "auth_mode": "api_key",
                    "cost": {
                        "api_equivalent_usd": 50.0,
                        "billed_estimated_usd": 50.0,
                        "basis": "native_message_usage",
                        "quality": "factual",
                    },
                }
            },
        )
        h = compute_source_health(storage, entry, since_days=30)
        assert h.api_equivalent_usd == 150.0
        assert h.billed_estimated_usd == 50.0
        assert h.cost_basis_counts == {"native_message_usage": 2}

    def test_window_excludes_old_events(self, storage):
        entry = SourceEntry(
            source_id="claude_code",
            display_name="Claude Code",
            category="anthropic",
            status=SOURCE_STATUS_SUPPORTED,
        )
        _insert_event(storage, "claude_code", _now_iso(days_ago=1))
        _insert_event(storage, "claude_code", _now_iso(days_ago=45))
        h = compute_source_health(storage, entry, since_days=7)
        assert h.event_count == 1


class TestComputeCoverage:
    def test_returns_one_entry_per_registry_row(self, storage):
        result = compute_coverage(storage, since_days=30)
        assert len(result) == len(SOURCE_REGISTRY)


class TestRenderMarkdownReport:
    def test_markdown_lists_summary_counts(self, storage):
        healths = compute_coverage(storage, since_days=30)
        md = render_markdown_report(healths, since_days=30)
        assert "# SHIP1000x Coverage Report" in md
        assert "Sources known to SHIP" in md
        assert "## Anthropic / Claude" in md
        assert "## OpenAI / Codex / ChatGPT" in md

    def test_markdown_marks_uncovered_sources_explicitly(self, storage):
        healths = compute_coverage(storage, since_days=30)
        md = render_markdown_report(healths, since_days=30)
        # Every NOT_SUPPORTED entry must surface its audit ref so the
        # report can't quietly hide a known gap.
        for h in healths:
            if h.status == SOURCE_STATUS_NOT_SUPPORTED and h.entry.audit_ref:
                assert h.entry.audit_ref in md
                assert h.entry.reason in md

    def test_active_source_block_includes_metrics(self, storage):
        _insert_event(
            storage, "claude_code", _now_iso(days_ago=1),
            raw_meta={"usage": {"model_raw": "claude-opus-4-7", "auth_mode": "oauth"}},
        )
        healths = compute_coverage(storage, since_days=30)
        md = render_markdown_report(healths, since_days=30)
        # The claude_code section is rendered as ✅ active with at least
        # one event captured.
        assert "✅ active" in md
        assert "claude_code" in md


class TestRenderJsonReport:
    def test_json_payload_shape(self, storage):
        healths = compute_coverage(storage, since_days=30)
        payload = json.loads(render_json_report(healths, since_days=30))
        assert "summary" in payload
        assert payload["summary"]["total"] == len(SOURCE_REGISTRY)
        assert "sources" in payload
        assert len(payload["sources"]) == len(SOURCE_REGISTRY)
        # Every entry carries its status string
        for row in payload["sources"]:
            assert row["status"] in {
                SOURCE_STATUS_ACTIVE,
                SOURCE_STATUS_SUPPORTED,
                SOURCE_STATUS_NOT_SUPPORTED,
            }


class TestDaemonHealthSurface:
    """Wave 3 / Day 8 — coverage shows drop file freshness for runtime
    sources (claude_statusline, agent_runtime) so the user can tell
    apart "no daemon running" vs "daemon ran but no ingest yet"."""

    def test_obsolete_claude_code_statusline_entry_removed(self):
        """The historical NOT_SUPPORTED entry was superseded by the
        Wave 3 / D1-2 claude_statusline implementation."""
        ids = {entry.source_id for entry in SOURCE_REGISTRY}
        assert "claude_code_statusline" not in ids
        # And the replacement is present + supported.
        assert "claude_statusline" in ids

    def test_wave3_runtime_sources_declare_drop_subpath(self):
        """Every Wave 3 foreground writer source has a drop_subpath
        pointing under ~/.ship1000x/."""
        by_id = {entry.source_id: entry for entry in SOURCE_REGISTRY}
        assert by_id["claude_statusline"].drop_subpath == "drop/statusline"
        assert by_id["agent_runtime"].drop_subpath == "drop/watch"

    def test_probe_drop_freshness_returns_none_when_dir_missing(
        self, tmp_path, monkeypatch
    ):
        from ship1000x.core import coverage as cov
        monkeypatch.setenv("HOME", str(tmp_path))
        # Path.home() reads HOME on macOS / Linux.
        result = cov._probe_drop_freshness("drop/watch")
        assert result is None

    def test_probe_drop_freshness_reports_count_and_newest_iso(
        self, tmp_path, monkeypatch
    ):
        from ship1000x.core import coverage as cov
        monkeypatch.setenv("HOME", str(tmp_path))
        drop = tmp_path / ".ship1000x" / "drop" / "watch"
        drop.mkdir(parents=True)
        (drop / "2026-05-20.jsonl").write_text('{}\n')
        (drop / "2026-05-23.jsonl").write_text('{}\n')
        result = cov._probe_drop_freshness("drop/watch")
        assert result is not None
        assert result["file_count"] == 2
        # newest_iso is a non-empty ISO 8601 string with +00:00.
        assert "T" in result["newest_iso"]
        assert result["newest_iso"].endswith("+00:00")

    def test_probe_drop_freshness_returns_none_for_empty_subpath(self):
        from ship1000x.core import coverage as cov
        assert cov._probe_drop_freshness("") is None

    def test_markdown_surfaces_daemon_health_for_runtime_source(
        self, storage, tmp_path, monkeypatch
    ):
        """When the daemon has produced drop files but ingest hasn't
        run, the report shows a 'run ship1000x ingest' hint."""
        monkeypatch.setenv("HOME", str(tmp_path))
        drop = tmp_path / ".ship1000x" / "drop" / "watch"
        drop.mkdir(parents=True)
        (drop / "2026-05-23.jsonl").write_text('{}\n')

        healths = compute_coverage(storage, since_days=30)
        md = render_markdown_report(healths, since_days=30)
        assert "Daemon health:" in md
        # Surfaces the actionable next step.
        assert "ship1000x ingest --source agent_runtime" in md

    def test_markdown_falls_back_to_generic_note_when_drop_empty(
        self, storage, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        # No drop dir / files exist.
        healths = compute_coverage(storage, since_days=30)
        md = render_markdown_report(healths, since_days=30)
        # The agent_runtime entry exists; its block is rendered with
        # the generic "Either the user does not use this source..." note
        # rather than the daemon-health surface.
        assert "agent_runtime" in md
        # The generic note appears at least once across the report.
        assert "ingestion has not run yet" in md

    def test_json_includes_daemon_health_field_for_runtime_sources(
        self, storage, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        drop = tmp_path / ".ship1000x" / "drop" / "watch"
        drop.mkdir(parents=True)
        (drop / "2026-05-23.jsonl").write_text('{}\n')

        healths = compute_coverage(storage, since_days=30)
        payload = json.loads(render_json_report(healths, since_days=30))
        runtime = next(
            row for row in payload["sources"]
            if row["source_id"] == "agent_runtime"
        )
        assert runtime["daemon_health"] is not None
        assert runtime["daemon_health"]["file_count"] == 1

        # Sources without a drop_subpath always carry daemon_health = None.
        claude_code = next(
            row for row in payload["sources"]
            if row["source_id"] == "claude_code"
        )
        assert claude_code["daemon_health"] is None
