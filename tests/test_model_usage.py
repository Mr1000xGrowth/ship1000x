"""Tests rollup daily_model_usage (observabilité coût + tokens par modèle)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ship1000x.core.model_usage import rebuild_model_usage
from ship1000x.core.storage import Storage


def _breakdown(provider: str, **over) -> dict:
    base = {
        "provider": provider, "fresh_input": 0, "cache_read": 0,
        "cache_write_5m": 0, "cache_write_1h": 0, "output_tokens": 0,
        "thinking": 0, "reasoning": 0, "web_search_requests": 0,
    }
    base.update(over)
    return base


class TestModelUsageRollup(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "t.sqlite")
        self.storage.init_schema()

    def tearDown(self):
        self._tmp.cleanup()

    def _insert(self, eid, day, source, model, provider, breakdown, cost, auth="oauth"):
        raw = json.dumps({
            "usage": {"model_canonical": model, "provider": provider,
                      "auth_mode": auth, "cost": {"billed_estimated_usd": 0.0}},
            "usage_breakdown": breakdown,
        })
        with self.storage.conn() as c:
            c.execute(
                "INSERT INTO events (id, source, event_type, started_at, "
                "cost_estimated, raw_meta, machine_id) "
                "VALUES (?, ?, 'session_day', ?, ?, ?, 'M1')",
                (eid, source, f"{day}T10:00:00Z", cost, raw),
            )

    def test_aggregates_same_model_same_day(self):
        b = _breakdown("anthropic", cache_read=2000, output_tokens=100)
        self._insert("e1", "2026-05-20", "claude_code", "claude-opus-4-7", "anthropic", b, 3.0)
        self._insert("e2", "2026-05-20", "claude_code", "claude-opus-4-7", "anthropic", b, 3.0)
        rebuild_model_usage(self.storage, since=None)
        with self.storage.conn() as c:
            rows = c.execute(
                "SELECT * FROM daily_model_usage WHERE date='2026-05-20'"
            ).fetchall()
        self.assertEqual(len(rows), 1)               # 1 ligne (date,machine,source,model)
        self.assertEqual(rows[0]["cache_read"], 4000)  # sommé
        self.assertEqual(rows[0]["output_tokens"], 200)
        self.assertAlmostEqual(rows[0]["cost_api_equivalent"], 6.0)
        self.assertEqual(rows[0]["pricing_quality"], "exact")
        self.assertEqual(rows[0]["provider"], "anthropic")

    def test_unknown_model_flagged_fallback(self):
        b = _breakdown("openai", fresh_input=500)
        self._insert("e3", "2026-05-21", "codex", "codex-auto-review", "openai", b, 0.5)
        rebuild_model_usage(self.storage, since=None)
        with self.storage.conn() as c:
            r = c.execute(
                "SELECT pricing_quality FROM daily_model_usage WHERE model='codex-auto-review'"
            ).fetchone()
        self.assertEqual(r["pricing_quality"], "fallback")

    def test_distinct_models_separate_rows(self):
        self._insert("e4", "2026-05-22", "claude_code", "claude-opus-4-7", "anthropic",
                     _breakdown("anthropic", output_tokens=10), 1.0)
        self._insert("e5", "2026-05-22", "codex", "gpt-5", "openai",
                     _breakdown("openai", output_tokens=20), 0.2)
        stats = rebuild_model_usage(self.storage, since=None)
        with self.storage.conn() as c:
            n = c.execute(
                "SELECT COUNT(*) n FROM daily_model_usage WHERE date='2026-05-22'"
            ).fetchone()["n"]
        self.assertEqual(n, 2)
        self.assertEqual(stats["model_rows"], 2)  # 2 modèles distincts ce jour (DB fraîche)


if __name__ == "__main__":
    unittest.main()
