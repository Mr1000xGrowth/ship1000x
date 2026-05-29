"""Markdown report cost-truth aggregation tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ship1000x.core.storage import Storage
from ship1000x.exporters.markdown_report import generate_report


def _insert_event(
    storage: Storage,
    *,
    event_id: str,
    source: str,
    stored_cost: float,
    api_equivalent_cost: float,
) -> None:
    raw_meta = {
        "usage": {
            "auth_mode": "oauth",
            "cost": {
                "api_equivalent_usd": api_equivalent_cost,
                "billed_estimated_usd": 0.0,
            },
            "quality": {"tokens": "factual", "cost": "factual"},
        },
        "prompt": "SECRET PROMPT SHOULD NOT LEAK",
    }
    with storage.conn() as conn:
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, started_at, duration_sec,
                wall_clock_sec, project_id, token_input, token_output,
                cost_estimated, confidence_flag, raw_meta, machine_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                source,
                "session",
                datetime.now(timezone.utc).isoformat(),
                600,
                600,
                "alpha",
                10,
                5,
                stored_cost,
                "high",
                json.dumps(raw_meta),
                "test-machine",
            ),
        )


def test_markdown_report_uses_cost_truth_for_api_equivalent_totals(tmp_path):
    storage = Storage(tmp_path / "tracker.sqlite")
    storage.init_schema()
    _insert_event(
        storage,
        event_id="codex-token-cost",
        source="codex",
        stored_cost=99.0,
        api_equivalent_cost=4.0,
    )
    _insert_event(
        storage,
        event_id="codex-mac-hourly-cost",
        source="codex_macapp",
        stored_cost=88.0,
        api_equivalent_cost=6.0,
    )

    md = generate_report(
        storage,
        datetime.now(timezone.utc) - timedelta(days=1),
        since_label="24h",
    )

    assert "Cout API-equivalent estime" in md
    assert "$10.00" in md
    assert "$4.00 mesure tokens reels" in md
    assert "$6.00 estime horaire apps fermees" in md
    assert "| alpha |" in md
    assert "| alpha | 20min | 2 | 30 | 10.00 |" in md
    assert "$99.00" not in md
    assert "$88.00" not in md
    assert "SECRET PROMPT" not in md
