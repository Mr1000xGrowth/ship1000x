"""CLI tests for source-audit JSON output."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from click.testing import CliRunner

import ship1000x.cli as cli_mod
from ship1000x.core.storage import Storage


def test_source_audit_json_is_machine_readable_and_redacted(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec,
                token_input, token_output, cost_estimated,
                confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "codex-fixture",
                "codex",
                "session",
                datetime.now(timezone.utc).isoformat(),
                60,
                100,
                25,
                0.001,
                "high",
                json.dumps(
                    {
                        "usage": {
                            "provider": "openai",
                            "client": "codex-cli",
                            "model_canonical": "gpt-5-codex",
                            "quality": {
                                "tokens": "factual",
                                "cost": "factual",
                                "active_time": "defensible",
                            },
                            "cost": {
                                "estimated_usd": 0.001,
                                "billed_estimated_usd": 0.001,
                                "quality": "factual",
                                "pricing_version": "2026-04-21",
                            },
                            "auth_mode": "api_key",
                            "provider_policy_snapshot": {
                                "provider": "openai",
                                "checked_at": "2026-05-26",
                                "status": "local-evidence-only",
                            },
                        },
                        "prompt": "SECRET PROMPT SHOULD NOT LEAK",
                    }
                ),
                "test-machine",
            ),
        )
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["source-audit", "--since", "30d", "--json"])

    assert result.exit_code == 0
    report = json.loads(result.output)
    assert report["schema_version"] == "ship1000x.source_quality_report.v1"
    assert report["window_days"] == 30
    assert report["summary"]["average_observed_quality_score"] == 93.33
    assert report["summary"]["low_quality_sources"] == 0
    assert report["summary"]["provider_policy_snapshot_events"] == 1
    assert report["summary"]["invalid_provider_policy_snapshot_events"] == 0
    assert report["summary"]["missing_provider_policy_snapshot_events"] == 0
    assert report["summary"]["provider_policy_snapshot_coverage_pct"] == 100.0
    assert report["summary"]["cost_truth"] == {
        "api_equivalent_usd": 0.001,
        "billed_estimated_usd": 0.001,
        "subscription_absorbed_usd": 0.0,
        "unknown_billing_basis_usd": 0.0,
        "events_with_cost_truth": 1,
        "events_with_unknown_billing_basis": 0,
    }
    codex = next(row for row in report["rows"] if row["source"] == "codex")
    assert codex["risk"] == "ok"
    assert codex["collector_stage"] == "default_ingest"
    assert codex["usage_events"] == 1
    assert codex["quality_scores"]["overall"] == 93.33
    assert codex["quality_band"] == "high"
    assert codex["auth_mode_counts"] == {"api_key": 1}
    assert codex["provider_policy_snapshot_events"] == 1
    assert codex["invalid_provider_policy_snapshot_events"] == 0
    assert codex["missing_provider_policy_snapshot_events"] == 0
    assert codex["provider_policy_snapshot_coverage_pct"] == 100.0
    assert codex["cost_truth"]["api_equivalent_usd"] == 0.001
    assert codex["cost_truth"]["billed_estimated_usd"] == 0.001
    assert codex["cost_truth"]["unknown_billing_basis_usd"] == 0.0
    assert codex["cost_truth"]["events_with_cost_truth"] == 1
    assert codex["cost_truth"]["events_with_unknown_billing_basis"] == 0
    assert "SECRET PROMPT" not in result.output
    assert "prompt" not in result.output.lower()
    assert "local-evidence-only" not in result.output


def test_source_audit_counts_invalid_provider_policy_snapshots(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec,
                token_input, token_output, cost_estimated,
                confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "codex-invalid-policy-snapshot",
                "codex",
                "session",
                datetime.now(timezone.utc).isoformat(),
                60,
                100,
                25,
                0.001,
                "high",
                json.dumps(
                    {
                        "usage": {
                            "quality": {
                                "tokens": "factual",
                                "cost": "factual",
                                "active_time": "defensible",
                            },
                            "provider_policy_snapshot": {
                                "provider": "openai",
                            },
                        },
                    }
                ),
                "test-machine",
            ),
        )
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["source-audit", "--since", "30d", "--json"])

    assert result.exit_code == 0
    report = json.loads(result.output)
    codex = next(row for row in report["rows"] if row["source"] == "codex")
    assert report["summary"]["provider_policy_snapshot_events"] == 0
    assert report["summary"]["invalid_provider_policy_snapshot_events"] == 1
    assert report["summary"]["missing_provider_policy_snapshot_events"] == 0
    assert report["summary"]["provider_policy_snapshot_coverage_pct"] == 0.0
    assert codex["provider_policy_snapshot_events"] == 0
    assert codex["invalid_provider_policy_snapshot_events"] == 1
    assert codex["missing_provider_policy_snapshot_events"] == 0
    assert codex["provider_policy_snapshot_coverage_pct"] == 0.0
    assert '"provider":' not in result.output


def test_source_audit_surfaces_legacy_unknown_billing_basis(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec,
                token_input, token_output, cost_estimated,
                confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "codex-legacy-cost",
                "codex",
                "session",
                datetime.now(timezone.utc).isoformat(),
                60,
                0,
                0,
                2.5,
                "medium",
                json.dumps({"note": "legacy row without normalized usage"}),
                "test-machine",
            ),
        )
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["source-audit", "--since", "30d", "--json"])

    assert result.exit_code == 0
    report = json.loads(result.output)
    codex = next(row for row in report["rows"] if row["source"] == "codex")
    assert codex["cost_truth"]["api_equivalent_usd"] == 0.0
    assert codex["cost_truth"]["billed_estimated_usd"] == 0.0
    assert codex["cost_truth"]["subscription_absorbed_usd"] == 0.0
    assert codex["cost_truth"]["unknown_billing_basis_usd"] == 2.5
    assert codex["cost_truth"]["events_with_cost_truth"] == 0
    assert codex["cost_truth"]["events_with_unknown_billing_basis"] == 1
    assert report["summary"]["cost_truth"]["unknown_billing_basis_usd"] == 2.5
    assert report["summary"]["cost_truth"]["events_with_cost_truth"] == 0
    assert report["summary"]["cost_truth"]["events_with_unknown_billing_basis"] == 1
    assert "legacy row without normalized usage" not in result.output

    human = CliRunner().invoke(cli_mod.cli, ["source-audit", "--since", "30d"])
    assert human.exit_code == 0
    assert "unknown billing basis" in human.output
    assert "codex $2.50 (1 event)" in human.output
    assert "legacy row without normalized usage" not in human.output


def test_source_audit_derives_legacy_cost_split_from_auth_mode(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec,
                token_input, token_output, cost_estimated,
                confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "codex-oauth-cost",
                "codex",
                "session",
                datetime.now(timezone.utc).isoformat(),
                60,
                100,
                25,
                3.75,
                "high",
                json.dumps(
                    {
                        "usage": {
                            "auth_mode": "oauth",
                            "quality": {
                                "tokens": "factual",
                                "cost": "factual",
                                "active_time": "defensible",
                            },
                        }
                    }
                ),
                "test-machine",
            ),
        )
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["source-audit", "--since", "30d", "--json"])

    assert result.exit_code == 0
    report = json.loads(result.output)
    codex = next(row for row in report["rows"] if row["source"] == "codex")
    assert codex["auth_mode_counts"] == {"oauth": 1}
    assert codex["cost_truth"]["api_equivalent_usd"] == 3.75
    assert codex["cost_truth"]["billed_estimated_usd"] == 0.0
    assert codex["cost_truth"]["subscription_absorbed_usd"] == 3.75
    assert codex["cost_truth"]["unknown_billing_basis_usd"] == 0.0
    assert codex["cost_truth"]["events_with_cost_truth"] == 1
    assert codex["cost_truth"]["events_with_unknown_billing_basis"] == 0

    human = CliRunner().invoke(cli_mod.cli, ["source-audit", "--since", "30d"])
    assert human.exit_code == 0
    assert "Cost truth split" in human.output
    assert "API-equivalent $3.75" in human.output
    assert "billed-est. $0.00" in human.output
    assert "subscription $3.75" in human.output
    assert "unknown-basis $0.00 (0 unknown-basis events)" in human.output
    assert "Cost truth coverage" in human.output
    assert "1 cost-truth events" in human.output
    assert "Provider policy snapshots" in human.output
    assert "0/1 valid" in human.output
    assert "1 missing" in human.output
    assert "unknown $0.00" not in human.output


def test_source_audit_strict_public_gate_passes_for_high_quality_source(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec,
                token_input, token_output, cost_estimated,
                confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "codex-fixture",
                "codex",
                "session",
                datetime.now(timezone.utc).isoformat(),
                60,
                100,
                25,
                0.001,
                "high",
                json.dumps(
                    {
                        "usage": {
                            "quality": {
                                "tokens": "factual",
                                "cost": "factual",
                                "active_time": "defensible",
                            },
                        }
                    }
                ),
                "test-machine",
            ),
        )
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["source-audit", "--strict-public", "--json"])

    assert result.exit_code == 0
    report = json.loads(result.output)
    assert report["gate"]["passed"] is True
    assert report["gate"]["fail_under"] == 70.0
    assert report["gate"]["fail_on_collector_stage"] == [
        "collector_module_only",
        "fixture_only",
        "registry_only",
    ]
    assert report["gate"]["fail_on_risk"] == ["fragile", "unknown-source"]


def test_source_audit_strict_public_gate_fails_for_fragile_source(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec,
                token_input, token_output, cost_estimated,
                confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "claude-fragile",
                "claude_code",
                "session",
                datetime.now(timezone.utc).isoformat(),
                60,
                0,
                0,
                0.0,
                "high",
                json.dumps({"note": "no normalized usage metadata"}),
                "test-machine",
            ),
        )
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["source-audit", "--strict-public", "--json"])

    assert result.exit_code == 1
    report = json.loads(result.output)
    assert report["gate"]["passed"] is False
    assert any("claude_code risk is fragile" in failure for failure in report["gate"]["failures"])


def test_source_audit_fail_under_gate_fails_below_threshold(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec,
                token_input, token_output, cost_estimated,
                confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "codex-low",
                "codex",
                "session",
                datetime.now(timezone.utc).isoformat(),
                60,
                0,
                0,
                0.0,
                "medium",
                json.dumps(
                    {
                        "usage": {
                            "quality": {
                                "tokens": "unknown",
                                "cost": "unknown",
                                "active_time": "indicative",
                            },
                        }
                    }
                ),
                "test-machine",
            ),
        )
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["source-audit", "--fail-under", "40", "--json"])

    assert result.exit_code == 1
    report = json.loads(result.output)
    assert report["gate"]["passed"] is False
    assert any("below 40.00" in failure for failure in report["gate"]["failures"])


def test_source_audit_strict_public_gate_fails_for_fixture_only_source(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec,
                token_input, token_output, cost_estimated,
                confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "gemini-fixture",
                "gemini_cli",
                "session",
                datetime.now(timezone.utc).isoformat(),
                60,
                100,
                25,
                0.0,
                "medium",
                json.dumps(
                    {
                        "usage": {
                            "quality": {
                                "tokens": "factual",
                                "cost": "unknown",
                                "active_time": "defensible",
                            },
                        }
                    }
                ),
                "test-machine",
            ),
        )
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(
        cli_mod.cli,
        ["source-audit", "--strict-public", "--fail-under", "0", "--json"],
    )

    assert result.exit_code == 1
    report = json.loads(result.output)
    gemini = next(row for row in report["rows"] if row["source"] == "gemini_cli")
    assert gemini["collector_stage"] == "fixture_only"
    assert gemini["collector_stage_untrusted_for_public_claims"] is True
    assert report["gate"]["passed"] is False
    assert any("gemini_cli collector_stage is fixture_only" in failure for failure in report["gate"]["failures"])


def test_source_audit_strict_public_does_not_fail_on_roo_ingest_stage(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec,
                token_input, token_output, cost_estimated,
                confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "roo-observed",
                "roo_kilo_code",
                "task",
                datetime.now(timezone.utc).isoformat(),
                60,
                0,
                0,
                0.0,
                "medium",
                json.dumps(
                    {
                        "usage": {
                            "quality": {
                                "tokens": "unknown",
                                "cost": "unknown",
                                "active_time": "defensible",
                            },
                        }
                    }
                ),
                "test-machine",
            ),
        )
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(
        cli_mod.cli,
        ["source-audit", "--strict-public", "--fail-under", "0", "--json"],
    )

    assert result.exit_code == 0
    report = json.loads(result.output)
    row = next(row for row in report["rows"] if row["source"] == "roo_kilo_code")
    assert row["collector_stage"] == "default_ingest"
    assert row["collector_stage_untrusted_for_public_claims"] is False
