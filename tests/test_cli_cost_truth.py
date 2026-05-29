"""CLI cost aggregation tests.

These cover human-facing tables that label totals as API-equivalent cost. They
must consume the shared cost-truth semantics instead of raw ``cost_estimated``.
"""

from __future__ import annotations

import json
from datetime import datetime

from click.testing import CliRunner

import ship1000x.cli as cli_mod
from ship1000x.core.storage import Storage


def _insert_event(
    storage: Storage,
    *,
    event_id: str,
    project_id: str,
    source: str = "codex",
    stored_cost: float = 99.0,
    api_equivalent_cost: float = 4.0,
) -> None:
    now = datetime.now().replace(microsecond=0).isoformat()
    raw_meta = {
        "usage": {
            "auth_mode": "oauth",
            "cost": {
                "api_equivalent_usd": api_equivalent_cost,
                "billed_estimated_usd": 0.0,
            },
            "quality": {"tokens": "factual", "cost": "factual"},
        }
    }
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec, project_id,
                token_input, token_output, cost_estimated, confidence_flag,
                raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                source,
                "session",
                now,
                600,
                project_id,
                10,
                5,
                stored_cost,
                "high",
                json.dumps(raw_meta),
                "test-machine",
            ),
        )


def test_today_week_summary_and_project_use_cost_truth_api_equivalent(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(storage, event_id="codex-alpha", project_id="alpha")

    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    runner = CliRunner()
    for args in (
        ["today"],
        ["week"],
        ["summary", "--since", "30d"],
        ["project", "alpha", "--since", "30d"],
    ):
        result = runner.invoke(cli_mod.cli, args)

        assert result.exit_code == 0, result.output
        assert "alpha" in result.output
        assert "4.00" in result.output
        assert "99.00" not in result.output


def test_pulse_uses_cost_truth_api_equivalent(monkeypatch, tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(storage, event_id="codex-pulse", project_id="alpha")

    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)

    result = CliRunner().invoke(cli_mod.cli, ["pulse"])

    assert result.exit_code == 0, result.output
    assert "$4" in result.output
    assert "$99" not in result.output
