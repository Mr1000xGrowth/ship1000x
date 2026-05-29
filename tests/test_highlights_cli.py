"""Tests for highlights cost confidence labels."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from click.testing import CliRunner

import ship1000x.cli as cli_mod
from ship1000x.core.storage import Storage


def test_highlights_uses_usage_quality_for_factual_cost(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    today = datetime.now(timezone.utc).date().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO daily_unified
               (date, machine_id, active_sec_unified, wall_clock_sec, threshold_used_sec,
                sample_size, sources_count, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (today, "test-machine", 3600, 7200, 300, 2, 2, now),
        )
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec, cost_estimated,
                confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "codex-factual",
                "codex",
                "session",
                now,
                600,
                99.0,
                "high",
                json.dumps({
                    "usage": {
                        "quality": {"cost": "factual"},
                        "cost": {
                            "api_equivalent_usd": 5.0,
                            "billed_estimated_usd": 0.0,
                        },
                    }
                }),
                "test-machine",
            ),
        )
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec, cost_estimated,
                confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "codex-mac-indicative",
                "codex_macapp",
                "session_day",
                now,
                600,
                88.0,
                "medium",
                json.dumps({
                    "usage": {
                        "quality": {"cost": "indicative"},
                        "cost": {
                            "api_equivalent_usd": 10.0,
                            "billed_estimated_usd": 0.0,
                        },
                    }
                }),
                "test-machine",
            ),
        )
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec, cost_estimated,
                confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "git-lines",
                "git",
                "commit",
                now,
                0,
                0.0,
                "high",
                json.dumps({"lines_real_added": 100, "lines_added": 100}),
                "test-machine",
            ),
        )
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)
    monkeypatch.setattr(cli_mod, "_get_user_email", lambda: None)

    result = CliRunner().invoke(cli_mod.cli, ["highlights", "--since", "30d"])

    assert result.exit_code == 0
    assert "33% native token/pricing coverage" in result.output
    assert "remainder indicative/unknown" in result.output
    assert "rest heuristic" not in result.output
    assert "Coût API-equivalent" in result.output
    assert "$0.1500" in result.output
    assert "$0.0500" not in result.output
    assert "$187" not in result.output
    assert "total API-eq / net line" in result.output
    assert "ultra-efficient" not in result.output
    assert "Coût agentique" not in result.output
