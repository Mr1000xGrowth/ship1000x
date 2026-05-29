"""CLI tests for public-check output and gate behavior."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from click.testing import CliRunner

import ship1000x.cli as cli_mod
from ship1000x.core.public_claims import (
    build_public_claim_readiness,
    build_public_proof_sequence,
)
from ship1000x.core.storage import Storage


def _insert_event(storage: Storage, *, event_id: str, source: str, raw_meta: dict | None) -> None:
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
                100,
                25,
                0.001,
                "high",
                json.dumps(raw_meta) if raw_meta is not None else None,
                "test-machine",
            ),
        )


def _proof_sequence_inputs(
    *,
    planned: int = 0,
    deferred: int = 0,
    partial_history: int = 0,
    blocked_claims: int = 0,
) -> dict:
    return {
        "since": "30d",
        "fail_under": 70.0,
        "strict_history": False,
        "source_report": {
            "summary": {
                "observed_sources": 1,
                "low_quality_sources": 0,
                "public_untrusted_stage_sources": 0,
            },
            "gate": {"passed": True},
        },
        "observation_report": {
            "summary": {
                "supported": 1,
                "partial": 0,
                "planned": planned,
                "deferred": deferred,
            },
        },
        "history_report": {
            "summary": {
                "recoverable_sources": 1,
                "partial_sources": partial_history,
                "external_source_required_sources": 0,
                "not_recoverable_from_ship_sources": 0,
                "malformed_meta_events": 0,
            },
        },
        "claim_readiness": {
            "summary": {
                "allowed": 1,
                "conditional": 0,
                "blocked": blocked_claims,
            },
        },
        "source_failures": [],
        "history_failures": [],
    }


def test_public_proof_sequence_passes_when_no_caveats():
    sequence = build_public_proof_sequence(**_proof_sequence_inputs())

    assert sequence["status"] == "pass"
    assert {step["status"] for step in sequence["commands"]} == {"pass"}


def test_public_proof_sequence_marks_visible_caveats_without_failing():
    sequence = build_public_proof_sequence(
        **_proof_sequence_inputs(planned=1, partial_history=1, blocked_claims=1)
    )
    steps = {step["key"]: step for step in sequence["commands"]}

    assert sequence["status"] == "caveat"
    assert steps["source_quality_gate"]["status"] == "pass"
    assert steps["observation_coverage"]["status"] == "caveat"
    assert steps["history_recoverability"]["status"] == "caveat"
    assert steps["public_claim_boundaries"]["status"] == "caveat"


def test_public_proof_sequence_fails_on_blocking_gate_failures():
    sequence = build_public_proof_sequence(
        **{
            **_proof_sequence_inputs(),
            "source_failures": ["codex risk is fragile"],
        }
    )
    steps = {step["key"]: step for step in sequence["commands"]}

    assert sequence["status"] == "fail"
    assert steps["source_quality_gate"]["status"] == "fail"
    assert steps["public_claim_boundaries"]["status"] == "fail"


def test_public_check_json_passes_for_high_quality_source(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(
        storage,
        event_id="codex-ok",
        source="codex",
        raw_meta={
            "usage": {
                "quality": {
                    "tokens": "factual",
                    "cost": "factual",
                    "active_time": "defensible",
                },
            },
            "prompt": "SECRET PROMPT SHOULD NOT LEAK",
        },
    )
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["public-check", "--since", "30d", "--json"])

    assert result.exit_code == 0
    report = json.loads(result.output)
    assert report["schema_version"] == "ship1000x.public_check_report.v1"
    assert report["passed"] is True
    assert report["proof_status"] == "caveat"
    assert report["presentation_status"] == "caveat"
    assert report["next_action"] == (
        "Public demo/report is usable only with visible caveats; quote allowed claims only."
    )
    assert report["source_quality"]["gate"]["passed"] is True
    assert report["source_quality"]["summary"]["public_untrusted_stage_sources"] == 0
    assert report["source_quality"]["summary"]["average_observed_quality_score"] == 93.33
    assert report["observation_audit"]["summary"]["planned"] >= 1
    assert report["provider_coverage"]["summary"]["provider_needs_fixture"] >= 1
    assert report["history_recoverability"]["summary"]["recoverable_sources"] == 1
    assert report["history_recoverability"]["summary"]["not_recoverable_from_ship_sources"] == 0
    assert any(gap["key"] == "gemini_cli" for gap in report["provider_coverage"]["gaps"])
    assert report["public_claim_readiness"]["schema_version"] == "ship1000x.public_claim_readiness.v1"
    assert report["public_proof_sequence"]["schema_version"] == "ship1000x.public_proof_sequence.v1"
    assert report["public_proof_sequence"]["status"] == "caveat"
    proof_steps = {
        step["key"]: step for step in report["public_proof_sequence"]["commands"]
    }
    assert proof_steps["source_quality_gate"]["status"] == "pass"
    assert proof_steps["observation_coverage"]["status"] == "caveat"
    assert proof_steps["source_quality_gate"]["command"] == (
        "ship1000x source-audit --strict-public --since 30d"
    )
    assert proof_steps["public_claim_boundaries"]["command"] == (
        "ship1000x public-check --since 30d --fail-under 70"
    )
    assert proof_steps["public_claim_boundaries"]["status"] == "caveat"
    assert "Quote only allowed claims" in report["public_proof_sequence"]["quote_policy"]
    assert report["public_claim_readiness"]["summary"]["allowed"] >= 1
    assert report["public_claim_readiness"]["summary"]["blocked"] >= 1
    claims = {claim["key"]: claim for claim in report["public_claim_readiness"]["claims"]}
    assert claims["batch_local_usage"]["status"] == "allowed"
    assert claims["provider_complete_coverage"]["status"] == "blocked"
    assert claims["invoice_grade_cost_truth"]["status"] == "blocked"
    assert "unknown_billing_basis_usd=" in claims["invoice_grade_cost_truth"]["evidence"]
    assert claims["provider_policy_compliance"]["status"] == "blocked"
    assert "provider_policy_snapshots=0/1" in claims["provider_policy_compliance"]["evidence"]
    assert "invalid_provider_policy_snapshots=0" in claims["provider_policy_compliance"]["evidence"]
    assert "missing_provider_policy_snapshots=1" in claims["provider_policy_compliance"]["evidence"]
    assert "missing_or_unknown_auth_mode_events=1" in claims["provider_policy_compliance"]["evidence"]
    assert claims["historical_repair_defensibility"]["status"] == "allowed"
    assert report["public_claim_readiness"]["history_repair_risks"] == []
    assert any(
        claim["key"] == "gemini_cli"
        for claim in report["public_claim_readiness"]["blocked_provider_claims"]
    )
    assert "SECRET PROMPT" not in result.output
    assert "prompt" not in result.output.lower()

    human_result = CliRunner().invoke(cli_mod.cli, ["public-check", "--since", "30d"])
    assert human_result.exit_code == 0
    assert "PUBLIC CHECK" in human_result.output
    assert "CAVEAT" in human_result.output
    assert "PASS" not in human_result.output
    assert "Public demo/report is usable only with visible caveats" in human_result.output


def test_public_check_json_fails_for_fragile_source(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(storage, event_id="claude-fragile", source="claude_code", raw_meta=None)
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["public-check", "--json"])

    assert result.exit_code == 1
    report = json.loads(result.output)
    assert report["passed"] is False
    assert any("claude_code risk is fragile" in failure for failure in report["failures"])


def test_public_check_claim_readiness_names_unknown_billing_basis(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(storage, event_id="codex-legacy-cost", source="codex", raw_meta=None)
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["public-check", "--json", "--fail-under", "0"])

    assert result.exit_code == 1
    report = json.loads(result.output)
    assert report["proof_status"] == "fail"
    assert report["presentation_status"] == "fail"
    claims = {claim["key"]: claim for claim in report["public_claim_readiness"]["claims"]}
    invoice = claims["invoice_grade_cost_truth"]
    assert invoice["status"] == "blocked"
    assert "unknown_billing_basis_usd=0.001" in invoice["evidence"]
    assert "unknown_billing_basis_events=1" in invoice["evidence"]


def test_public_claim_readiness_prefers_source_summary_cost_truth():
    source_report = {
        "summary": {
            "observed_sources": 1,
            "cost_truth": {
                "unknown_billing_basis_usd": 4.25,
                "events_with_unknown_billing_basis": 3,
            },
        },
        "gate": {"passed": True},
        "rows": [
            {
                "usage_events": 3,
                "cost_truth": {
                    "unknown_billing_basis_usd": 0.0,
                    "events_with_unknown_billing_basis": 0,
                },
                "auth_mode_counts": {"oauth": 2, "api_key": 1},
                "provider_policy_snapshot_events": 2,
                "invalid_provider_policy_snapshot_events": 1,
            }
        ],
    }
    observation_report = {
        "summary": {
            "provider_supported": 1,
            "provider_partial": 0,
            "provider_needs_fixture": 0,
            "provider_planned": 0,
            "provider_deferred": 0,
        },
        "provider_capabilities": [],
    }

    claims = build_public_claim_readiness(
        source_report=source_report,
        observation_report=observation_report,
    )["claims"]
    invoice = {claim["key"]: claim for claim in claims}["invoice_grade_cost_truth"]

    assert "unknown_billing_basis_usd=4.25" in invoice["evidence"]
    assert "unknown_billing_basis_events=3" in invoice["evidence"]
    policy = {claim["key"]: claim for claim in claims}["provider_policy_compliance"]
    assert "auth_modes=api_key:1, oauth:2" in policy["evidence"]
    assert "auth_mode_events=3" in policy["evidence"]
    assert "missing_or_unknown_auth_mode_events=0" in policy["evidence"]
    assert "nonstandard_auth_modes=none" in policy["evidence"]
    assert "provider_policy_snapshots=2/3" in policy["evidence"]
    assert "invalid_provider_policy_snapshots=1" in policy["evidence"]
    assert "missing_provider_policy_snapshots=0" in policy["evidence"]


def test_public_claim_readiness_names_nonstandard_auth_modes():
    source_report = {
        "summary": {"observed_sources": 1},
        "gate": {"passed": True},
        "rows": [
            {
                "usage_events": 4,
                "auth_mode_counts": {
                    "api_key": 1,
                    "openclaw_gateway": 2,
                    "unknown": 1,
                },
            }
        ],
    }
    observation_report = {
        "summary": {
            "provider_supported": 1,
            "provider_partial": 0,
            "provider_needs_fixture": 0,
            "provider_planned": 0,
            "provider_deferred": 0,
        },
        "provider_capabilities": [],
    }

    claims = build_public_claim_readiness(
        source_report=source_report,
        observation_report=observation_report,
    )["claims"]

    policy = {claim["key"]: claim for claim in claims}["provider_policy_compliance"]
    assert "auth_modes=api_key:1, openclaw_gateway:2, unknown:1" in policy["evidence"]
    assert "auth_mode_events=4" in policy["evidence"]
    assert "missing_or_unknown_auth_mode_events=1" in policy["evidence"]
    assert "nonstandard_auth_modes=openclaw_gateway:2" in policy["evidence"]
    assert "missing_provider_policy_snapshots=4" in policy["evidence"]


def test_public_claim_readiness_counts_provider_policy_snapshots_from_summary():
    source_report = {
        "summary": {
            "observed_sources": 1,
            "usage_events": 4,
            "provider_policy_snapshot_events": 3,
            "invalid_provider_policy_snapshot_events": 1,
        },
        "gate": {"passed": True},
        "rows": [
            {
                "usage_events": 4,
                "auth_mode_counts": {"oauth": 4},
            }
        ],
    }
    observation_report = {
        "summary": {
            "provider_supported": 1,
            "provider_partial": 0,
            "provider_needs_fixture": 0,
            "provider_planned": 0,
            "provider_deferred": 0,
        },
        "provider_capabilities": [],
    }

    claims = build_public_claim_readiness(
        source_report=source_report,
        observation_report=observation_report,
    )["claims"]

    policy = {claim["key"]: claim for claim in claims}["provider_policy_compliance"]
    assert policy["status"] == "blocked"
    assert "provider_policy_snapshots=3/4" in policy["evidence"]
    assert "invalid_provider_policy_snapshots=1" in policy["evidence"]
    assert "missing_provider_policy_snapshots=0" in policy["evidence"]


def test_public_check_json_fails_for_fixture_only_observed_source(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(
        storage,
        event_id="opencode-fixture",
        source="opencode",
        raw_meta={
            "usage": {
                "quality": {
                    "tokens": "unknown",
                    "cost": "unknown",
                    "active_time": "defensible",
                },
            },
        },
    )
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(
        cli_mod.cli,
        ["public-check", "--json", "--fail-under", "0"],
    )

    assert result.exit_code == 1
    report = json.loads(result.output)
    assert report["passed"] is False
    assert report["source_quality"]["summary"]["public_untrusted_stage_sources"] == 1
    assert any("opencode collector_stage is fixture_only" in failure for failure in report["failures"])


def test_public_check_strict_history_fails_for_partial_recoverability(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(
        storage,
        event_id="codex-macapp-partial",
        source="codex_macapp",
        raw_meta={
            "usage": {
                "quality": {
                    "tokens": "unknown",
                    "cost": "indicative",
                    "active_time": "defensible",
                },
            },
        },
    )
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    relaxed = CliRunner().invoke(
        cli_mod.cli,
        ["public-check", "--json", "--fail-under", "0"],
    )
    assert relaxed.exit_code == 0
    relaxed_report = json.loads(relaxed.output)
    assert relaxed_report["passed"] is True
    assert relaxed_report["history_recoverability"]["strict"] is False
    assert relaxed_report["history_recoverability"]["passed"] is True
    assert relaxed_report["history_recoverability"]["warnings"]
    relaxed_claims = {
        claim["key"]: claim
        for claim in relaxed_report["public_claim_readiness"]["claims"]
    }
    assert relaxed_claims["historical_repair_defensibility"]["status"] == "conditional"
    relaxed_risks = relaxed_report["public_claim_readiness"]["history_repair_risks"]
    assert len(relaxed_risks) == 1
    assert relaxed_risks[0]["source"] == "codex_macapp"
    assert relaxed_risks[0]["label"] == "Codex macOS app"
    assert relaxed_risks[0]["recoverability"] == "partial"
    assert relaxed_risks[0]["repair_scope"] == "partial-metadata-or-activity-only"
    assert relaxed_risks[0]["malformed_meta_events"] == 0
    assert relaxed_risks[0]["recommendation"] == (
        "Safe to attempt the listed repair path, then rerun source-audit and public-check."
    )
    assert "Do not claim full historical repair" in relaxed_risks[0]["claim_boundary"]
    assert "native token counters" in relaxed_risks[0]["unrecoverable_truth_fields"]
    guidance = relaxed_report["public_claim_readiness"]["operator_guidance"]
    assert "Conditional claims require" in guidance
    assert "source/history caveats" in guidance
    assert "history recoverability" in guidance
    relaxed_human = CliRunner().invoke(
        cli_mod.cli,
        ["public-check", "--fail-under", "0"],
    )
    assert relaxed_human.exit_code == 0
    assert "Public claims" in relaxed_human.output
    assert "conditional" in relaxed_human.output
    assert "Conditional public claims" in relaxed_human.output
    assert "Historical repair/backfill defensibility" in relaxed_human.output
    assert "partial=1" in relaxed_human.output
    assert "History repair risks" in relaxed_human.output
    assert "codex_macapp: partial; scope=partial-metadata-or-activity-only" in relaxed_human.output
    assert "cannot repair:" in relaxed_human.output
    assert "Do not claim full historical repair" in relaxed_human.output
    assert "Safe to attempt the listed repair path" in relaxed_human.output
    assert "Blocked public claims" in relaxed_human.output

    strict = CliRunner().invoke(
        cli_mod.cli,
        ["public-check", "--json", "--fail-under", "0", "--strict-history"],
    )
    assert strict.exit_code == 1
    strict_report = json.loads(strict.output)
    assert strict_report["passed"] is False
    assert strict_report["history_recoverability"]["strict"] is True
    assert strict_report["history_recoverability"]["passed"] is False
    assert strict_report["public_proof_sequence"]["status"] == "fail"
    strict_steps = {
        step["key"]: step for step in strict_report["public_proof_sequence"]["commands"]
    }
    assert strict_steps["history_recoverability"]["status"] == "fail"
    assert strict_steps["public_claim_boundaries"]["command"].endswith("--strict-history")
    strict_claims = {
        claim["key"]: claim
        for claim in strict_report["public_claim_readiness"]["claims"]
    }
    assert strict_claims["historical_repair_defensibility"]["status"] == "conditional"
    assert any("partially recoverable" in failure for failure in strict_report["failures"])


def test_public_check_claim_readiness_blocks_not_recoverable_history(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(storage, event_id="unknown-1", source="mystery_tool", raw_meta=None)
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["public-check", "--json", "--fail-under", "0"])

    assert result.exit_code == 1
    report = json.loads(result.output)
    claims = {claim["key"]: claim for claim in report["public_claim_readiness"]["claims"]}
    history_claim = claims["historical_repair_defensibility"]
    assert history_claim["status"] == "blocked"
    assert "not_recoverable=1" in history_claim["evidence"]
    risks = report["public_claim_readiness"]["history_repair_risks"]
    assert len(risks) == 1
    assert risks[0]["source"] == "mystery_tool"
    assert risks[0]["label"] == "mystery_tool"
    assert risks[0]["recoverability"] == "not-recoverable-from-ship"
    assert risks[0]["repair_scope"] == "visible-evidence-only"
    assert risks[0]["malformed_meta_events"] == 0
    assert risks[0]["recommendation"] == (
        "Treat as visible evidence only; add a profile before making public claims."
    )
    assert "Do not claim repair/backfill from SHIP alone" in risks[0]["claim_boundary"]
    assert "registered source profile" in risks[0]["unrecoverable_truth_fields"]


def test_public_check_human_output_marks_no_go(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(storage, event_id="unknown-1", source="mystery_tool", raw_meta=None)
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["public-check"])

    assert result.exit_code == 1
    assert "PUBLIC CHECK" in result.output
    assert "NO GO" in result.output
    assert "mystery_tool risk is unknown-source" in result.output
    assert "Provider gaps visible" in result.output
    assert "History recoverability" in result.output
    assert "History repair risks" in result.output
    assert "mystery_tool: not-recoverable-from-ship" in result.output
    assert "Treat as visible evidence only" in result.output
    assert "not recoverable" in result.output
    assert "Allowed public claims" in result.output
    assert "Blocked public claims" in result.output
    assert "Public proof sequence" in result.output
    assert "ship1000x public-check --since 30d --fail-under 70" in result.output
    assert "unknown_billing_basis_usd=" in result.output
    assert "caveat:" in result.output
