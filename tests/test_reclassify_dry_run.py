"""Tests for reclassify dry-run safety."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from click.testing import CliRunner

import ship1000x.cli as cli_mod
from ship1000x.core.source_inventory import ACTIVE_COLLECTOR_MODULES, RECLASSIFY_COLLECTORS
from ship1000x.core.storage import Storage


def _insert_event(
    storage: Storage,
    *,
    event_id: str,
    source: str,
    cost: float = 0.0,
    api_equivalent_cost: float | None = None,
) -> None:
    usage = {"quality": {"tokens": "factual", "cost": "factual"}}
    if api_equivalent_cost is not None:
        usage["auth_mode"] = "oauth"
        usage["cost"] = {
            "api_equivalent_usd": api_equivalent_cost,
            "billed_estimated_usd": 0.0,
        }
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
                cost,
                "high",
                json.dumps({"usage": usage}),
                "test-machine",
            ),
        )


def _counts(storage: Storage) -> tuple[int, int]:
    with storage.conn() as conn:
        events = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        offsets = conn.execute("SELECT COUNT(*) AS n FROM ingestion_state").fetchone()["n"]
    return int(events), int(offsets)


def test_reclassify_dry_run_json_is_safe_and_does_not_mutate(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(storage, event_id="codex-1", source="codex", cost=0.25)
    _insert_event(storage, event_id="claude-1", source="claude_code", cost=1.50)
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO ingestion_state (source, file_key, last_offset, last_ingested_at)
               VALUES (?, ?, ?, ?)""",
            ("codex", "fixture", 123, datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            """INSERT INTO daily_rollup (date, project_id, source, event_count)
               VALUES (?, ?, ?, ?)""",
            (datetime.now(timezone.utc).date().isoformat(), "project", "codex", 1),
        )
    before = _counts(storage)
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)
    monkeypatch.setattr(cli_mod, "_load_yaml", lambda _path: {"sources": {"codex": "disabled"}})

    result = CliRunner().invoke(cli_mod.cli, ["reclassify", "--since", "365d", "--dry-run", "--json"])

    assert result.exit_code == 0
    assert _counts(storage) == before
    report = json.loads(result.output)
    assert report["schema_version"] == "ship1000x.reclassify_plan.v1"
    assert report["dry_run"] is True
    assert report["impact"]["events_to_delete"] == 2
    assert report["impact"]["ingestion_offsets_to_reset"] == 1
    assert report["impact"]["daily_rollup_rows_to_rebuild"] == 1
    assert any(row["source"] == "codex" and row["api_equivalent_cost_usd"] == 0.25 for row in report["sources"])
    assert any(row["source"] == "codex" and row["cost_estimated_usd"] == 0.25 for row in report["sources"])
    codex = next(row for row in report["sources"] if row["source"] == "codex")
    assert codex["recoverability"] == "recoverable"
    assert codex["repair_scope"] == "listed-fields-from-retained-source"
    assert "original local/source evidence still exists" in codex["claim_boundary"]
    assert "original Codex rollout/session files with total_token_usage" in codex["unrecoverable_truth_fields"]
    assert codex["repair_recommendation"].startswith("Safe to attempt")
    assert any(row["collector"] == "codex" and row["enabled"] is False for row in report["collectors"])
    collectors = {row["collector"]: row for row in report["collectors"]}
    assert collectors["roo_kilo_code"]["source_key"] == "roo_kilo_code"
    assert collectors["claude_statusline"]["source_key"] == "claude_statusline"
    assert collectors["agent_runtime"]["source_key"] == "agent_runtime"
    assert collectors["trace"]["source_key"] == "trace"
    assert collectors["claude_desktop_sessions"]["source_key"] == "claude_desktop"
    assert "prompt" not in result.output.lower()


def test_reclassify_dry_run_json_surfaces_history_recoverability_warnings(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(storage, event_id="unknown-1", source="mystery_tool", cost=0.25)
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)
    monkeypatch.setattr(cli_mod, "_load_yaml", lambda _path: {"sources": {}})

    result = CliRunner().invoke(cli_mod.cli, ["reclassify", "--since", "365d", "--dry-run", "--json"])

    assert result.exit_code == 0
    report = json.loads(result.output)
    assert report["history_recoverability_summary"]["not_recoverable_from_ship_sources"] == 1
    assert report["history_recoverability_warnings"] == [
        "1 source(s) are not recoverable from SHIP semantics alone."
    ]
    mystery = next(row for row in report["sources"] if row["source"] == "mystery_tool")
    assert mystery["recoverability"] == "not-recoverable-from-ship"
    assert mystery["repair_scope"] == "visible-evidence-only"
    assert "Do not claim repair/backfill from SHIP alone" in mystery["claim_boundary"]
    assert mystery["repair_recommendation"].startswith("Treat as visible evidence only")


def test_reclassify_dry_run_human_output_is_explicit(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(storage, event_id="codex-1", source="codex", cost=0.25)
    _insert_event(storage, event_id="unknown-1", source="mystery_tool", cost=0.25)
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)
    monkeypatch.setattr(cli_mod, "_load_yaml", lambda _path: {"sources": {}})

    result = CliRunner().invoke(cli_mod.cli, ["reclassify", "--since", "365d", "--dry-run"])

    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert "aucune modification" in result.output
    assert "Events purges" in result.output
    assert "History recoverability" in result.output
    assert "not-recoverable-from-ship" in result.output
    assert "Repair scope" in result.output
    assert "visible-evidence-only" in result.output
    assert "cannot repair:" in result.output
    assert "boundary:" in result.output
    assert "recommendation:" in result.output
    assert "Treat as visible evidence only" in result.output
    assert "not recoverable from SHIP semantics alone" in result.output


def test_reclassify_dry_run_cost_uses_api_equivalent_truth(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(
        storage,
        event_id="codex-1",
        source="codex",
        cost=99.0,
        api_equivalent_cost=4.0,
    )
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)
    monkeypatch.setattr(cli_mod, "_load_yaml", lambda _path: {"sources": {}})

    json_result = CliRunner().invoke(
        cli_mod.cli,
        ["reclassify", "--since", "365d", "--dry-run", "--json"],
    )
    human_result = CliRunner().invoke(
        cli_mod.cli,
        ["reclassify", "--since", "365d", "--dry-run"],
    )

    assert json_result.exit_code == 0
    report = json.loads(json_result.output)
    codex = next(row for row in report["sources"] if row["source"] == "codex")
    assert codex["api_equivalent_cost_usd"] == 4.0
    assert codex["cost_estimated_usd"] == 4.0
    assert "99.0000" not in human_result.output
    assert "$4.0000" in human_result.output


def test_reclassify_real_run_prints_preflight_before_mutation(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(storage, event_id="unknown-1", source="mystery_tool", cost=0.25)
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO ingestion_state (source, file_key, last_offset, last_ingested_at)
               VALUES (?, ?, ?, ?)""",
            ("mystery_tool", "fixture", 123, datetime.now(timezone.utc).isoformat()),
        )
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)
    monkeypatch.setattr(cli_mod, "_load_yaml", lambda _path: {"sources": {}})
    monkeypatch.setattr(cli_mod, "RECLASSIFY_COLLECTORS", ())

    result = CliRunner().invoke(cli_mod.cli, ["reclassify", "--since", "365d"])

    assert result.exit_code == 0
    assert "Preflight impact" in result.output
    assert "Events purges" in result.output
    assert "History recoverability" in result.output
    assert "mystery_tool: not-recoverable-from-ship" in result.output
    assert "Treat as visible evidence only" in result.output
    assert "not recoverable from SHIP semantics alone" in result.output
    assert _counts(storage) == (0, 0)


def test_reclassify_json_without_dry_run_is_rejected(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)
    monkeypatch.setattr(cli_mod, "_load_yaml", lambda _path: {"sources": {}})

    result = CliRunner().invoke(cli_mod.cli, ["reclassify", "--json"])

    assert result.exit_code == 2
    assert "--json est disponible uniquement avec --dry-run" in result.output


def test_reclassify_help_describes_all_historical_events_not_git_only():
    result = CliRunner().invoke(cli_mod.cli, ["reclassify", "--help"])

    assert result.exit_code == 0
    assert "Reclasse les evenements historiques" in result.output
    assert "commits git historiques" not in result.output


def test_reclassify_inventory_covers_active_production_collectors():
    reclassify_modules = {collector for collector, _source_key in RECLASSIFY_COLLECTORS}

    assert reclassify_modules == set(ACTIVE_COLLECTOR_MODULES)
