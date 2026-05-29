"""Tests for observation capability audit."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from click.testing import CliRunner

import ship1000x.cli as cli_mod
from ship1000x.core.observation_coverage import build_observation_audit_report
from ship1000x.core.storage import Storage


def _storage_with_events(tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec,
                cost_estimated, confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "codex-1",
                "codex",
                "session",
                datetime.now(timezone.utc).isoformat(),
                60,
                0.01,
                "high",
                json.dumps({"usage": {"quality": {"cost": "factual"}}}),
                "test-machine",
            ),
        )
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec,
                cost_estimated, confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "mystery-1",
                "mystery_tool",
                "session",
                datetime.now(timezone.utc).isoformat(),
                30,
                0,
                "medium",
                json.dumps({"prompt": "SECRET PROMPT SHOULD NOT LEAK"}),
                "test-machine",
            ),
        )
    return storage


def test_observation_audit_report_is_redacted_and_counts_capabilities(tmp_path):
    storage = _storage_with_events(tmp_path)

    report = build_observation_audit_report(storage, window_days=30)

    assert report["schema_version"] == "ship1000x.observation_audit_report.v1"
    assert report["summary"]["implemented"] >= 1
    assert report["summary"]["partial"] >= 1
    assert report["summary"]["planned"] >= 1
    assert report["summary"]["provider_capabilities"] >= 10
    assert report["summary"]["provider_needs_fixture"] >= 1
    assert "mystery_tool" in report["unknown_observed_sources"]
    provider_keys = {row["key"] for row in report["provider_capabilities"]}
    assert {"gemini_cli", "copilot_agents", "opencode"} <= provider_keys
    roo = next(row for row in report["provider_capabilities"] if row["key"] == "roo_kilo_code")
    assert roo["status"] == "partial"
    assert "not invoice-grade" in roo["public_claim"]
    rendered = json.dumps(report, sort_keys=True)
    assert "SECRET PROMPT" not in rendered


def test_observation_audit_cli_json_is_machine_readable(monkeypatch, tmp_path):
    storage = _storage_with_events(tmp_path)
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["observation-audit", "--since", "30d", "--json"])

    assert result.exit_code == 0
    report = json.loads(result.output)
    assert report["audit_source"].endswith("OBSERVATION_AUDIT.md")
    assert any(row["key"] == "live_runtime_observability" for row in report["rows"])
    assert any(row["key"] == "gemini_cli" for row in report["provider_capabilities"])
    assert "SECRET PROMPT" not in result.output
