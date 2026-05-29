"""Tests for historical recoverability audit."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from click.testing import CliRunner

import ship1000x.cli as cli_mod
from ship1000x.core.history_recoverability import PROFILES, build_history_recoverability_report
from ship1000x.core.source_inventory import PRODUCTION_EVENT_SOURCES
from ship1000x.core.storage import Storage


def _insert_event(storage: Storage, *, event_id: str, source: str, raw_meta: dict | str | None) -> None:
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec,
                token_input, token_output, cost_estimated,
                confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                source,
                "session",
                datetime.now(timezone.utc).isoformat(),
                60,
                10,
                5,
                0.001,
                "high",
                raw_meta if isinstance(raw_meta, str) else json.dumps(raw_meta) if raw_meta is not None else None,
                "test-machine",
            ),
        )


def test_history_recoverability_report_is_safe_and_actionable(tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(
        storage,
        event_id="claude-ok",
        source="claude_code",
        raw_meta={
            "usage": {
                "auth_mode": "oauth",
                "tokens": {"input_tokens": 10, "output_tokens": 5},
                "cost": {"estimated_usd": 0.001, "billed_estimated_usd": 0.0},
                "quality": {"tokens": "factual", "cost": "factual"},
            },
            "prompt": "SECRET PROMPT SHOULD NOT LEAK",
            "path": "/Users/example/private",
        },
    )
    _insert_event(storage, event_id="mystery", source="mystery_tool", raw_meta=None)

    report = build_history_recoverability_report(storage, window_days=365)

    assert report["schema_version"] == "ship1000x.history_recoverability_report.v1"
    assert report["summary"]["observed_sources"] == 2
    assert report["summary"]["recoverable_sources"] == 1
    assert report["summary"]["not_recoverable_from_ship_sources"] == 1
    assert report["summary"]["strict_history_blocking_sources"] == 1
    assert report["strict_history_failures"] == [
        "mystery_tool: not recoverable from SHIP semantics alone"
    ]
    claude = next(row for row in report["rows"] if row["source"] == "claude_code")
    assert claude["recoverability"] == "recoverable"
    assert claude["strict_history_blocking"] is False
    assert claude["strict_history_blocking_reasons"] == []
    assert claude["repair_scope"] == "listed-fields-from-retained-source"
    assert "original local/source evidence still exists" in claude["claim_boundary"]
    assert claude["usage_events"] == 1
    assert "native tokens" in claude["repairable_fields"]
    assert "original Claude Code JSONL with message.usage" in claude["unrecoverable_truth_fields"]
    mystery = next(row for row in report["rows"] if row["source"] == "mystery_tool")
    assert mystery["recoverability"] == "not-recoverable-from-ship"
    assert mystery["strict_history_blocking"] is True
    assert mystery["strict_history_blocking_reasons"] == [
        "not recoverable from SHIP semantics alone"
    ]
    assert mystery["repair_scope"] == "visible-evidence-only"
    assert "Do not claim repair/backfill from SHIP alone" in mystery["claim_boundary"]

    rendered = json.dumps(report, sort_keys=True)
    assert "SECRET PROMPT" not in rendered
    assert "/Users/example" not in rendered


def test_ship_emitted_non_usage_sources_have_recoverability_profiles(tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(storage, event_id="web-export", source="web_export", raw_meta=None)
    _insert_event(storage, event_id="secret-alert", source="git_secret_alert", raw_meta=None)

    report = build_history_recoverability_report(storage, window_days=365)
    web_export = next(row for row in report["rows"] if row["source"] == "web_export")
    secret_alert = next(row for row in report["rows"] if row["source"] == "git_secret_alert")

    assert web_export["recoverability"] == "partial"
    assert secret_alert["recoverability"] == "recoverable"
    assert report["summary"]["not_recoverable_from_ship_sources"] == 0


def test_production_event_sources_have_recoverability_profiles():
    missing = sorted(PRODUCTION_EVENT_SOURCES - set(PROFILES))

    assert missing == []


def test_production_sources_are_not_default_not_recoverable(tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    for source in sorted(PRODUCTION_EVENT_SOURCES):
        _insert_event(storage, event_id=f"{source}-event", source=source, raw_meta=None)

    report = build_history_recoverability_report(storage, window_days=365)

    assert report["summary"]["not_recoverable_from_ship_sources"] == 0
    assert {row["source"] for row in report["rows"]} == PRODUCTION_EVENT_SOURCES


def test_history_audit_cli_json(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(storage, event_id="codex-old", source="codex", raw_meta={"usage": {}})
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["history-audit", "--since", "365d", "--json"])

    assert result.exit_code == 0
    report = json.loads(result.output)
    assert report["summary"]["observed_sources"] == 1
    assert report["summary"]["strict_history_blocking_sources"] == 0
    assert report["strict_history_failures"] == []
    row = report["rows"][0]
    assert row["source"] == "codex"
    assert row["recommendation"].startswith("Run reclassify only if original source files still exist")


def test_history_audit_cli_human_output(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(storage, event_id="bad-meta", source="claude_code", raw_meta="{not-json")
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["history-audit", "--since", "365d"])

    assert result.exit_code == 0
    assert "HISTORY RECOVERABILITY AUDIT" in result.output
    assert "claude_code" in result.output
    assert "malformed metadata" in result.output.lower()
    assert "Malformed" in result.output
    assert "Repair details" in result.output
    assert "scope:" in result.output
    assert "cannot repair:" in result.output
    assert "boundary:" in result.output
    assert "Inspect collector/version first" in result.output
    assert "Recoverable only while local Claude Code history files still exist" in result.output


def test_history_audit_marks_malformed_recoverable_rows_as_strict_blocking(tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(storage, event_id="bad-meta", source="claude_code", raw_meta="{not-json")

    report = build_history_recoverability_report(storage, window_days=365)
    row = next(row for row in report["rows"] if row["source"] == "claude_code")

    assert row["recoverability"] == "recoverable"
    assert row["malformed_meta_events"] == 1
    assert row["strict_history_blocking"] is True
    assert row["strict_history_blocking_reasons"] == ["malformed metadata"]
    assert report["summary"]["strict_history_blocking_sources"] == 1
    assert report["strict_history_failures"] == ["claude_code: malformed metadata"]
