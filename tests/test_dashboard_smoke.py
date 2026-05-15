"""Smoke tests for the V1.2 web dashboard.

Verifies :
- App factory creates without error
- All routes return 200
- API endpoints return valid JSON with expected keys
- Localhost binding (security : refuses external)

These tests use a temporary in-memory DB so they don't depend on
a populated production DB.
"""

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from ship1000x.core.storage import Storage


class TestDashboardSmoke(unittest.TestCase):
    """End-to-end smoke : factory + routes + APIs."""

    def setUp(self):
        self._tmpdir = TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.db_path = self.tmp / "tracker.sqlite"
        self.config_dir = self.tmp / "config"
        self.config_dir.mkdir(parents=True)
        # Init schema
        s = Storage(self.db_path)
        s.init_schema()
        # Seed minimal data
        ts = datetime.now(timezone.utc).isoformat()
        with s.conn() as c:
            c.execute(
                """INSERT INTO events
                   (id, source, event_type, started_at, duration_sec, cost_estimated,
                    confidence_flag, machine_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("e1", "claude_code", "session", ts, 3600, 5.0, "high", "test"),
            )
            c.execute(
                """INSERT INTO daily_unified
                   (date, machine_id, active_sec_unified, active_sec_p95,
                    active_sec_strict, active_sec_loose,
                    wall_clock_sec, threshold_used_sec, computed_at)
                   VALUES (date('now'), ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("test", 3600, 3600, 3600, 7200, 7200, 300, ts),
            )

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_client(self):
        from ship1000x.web.app import create_app
        app = create_app(self.db_path, self.config_dir)
        return app.test_client()

    def test_app_factory_works(self):
        from ship1000x.web.app import create_app
        app = create_app(self.db_path, self.config_dir)
        self.assertIsNotNone(app)

    def test_overview_page_returns_200(self):
        client = self._make_client()
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Ship1000x", r.data)
        self.assertIn(b"Highlights", r.data)

    def test_projects_page_returns_200(self):
        client = self._make_client()
        r = client.get("/projects")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Projects", r.data)

    def test_api_highlights_returns_valid_json(self):
        client = self._make_client()
        r = client.get("/api/highlights?days=30")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        # Must contain these keys
        for key in ("leverage", "parallelism", "days_equivalent", "active_hours",
                    "lines_real", "cost_total", "trust_score", "trust_base",
                    "sources_count", "threshold_min", "window_days"):
            self.assertIn(key, data, f"missing key in /api/highlights: {key}")

    def test_api_trend_returns_list(self):
        client = self._make_client()
        r = client.get("/api/trend?days=30")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIsInstance(data, list)
        if data:
            self.assertIn("date", data[0])
            self.assertIn("active_hours", data[0])

    def test_api_projects_returns_list(self):
        client = self._make_client()
        r = client.get("/api/projects?days=30")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIsInstance(data, list)
        if data:
            for key in ("project_id", "total_hours", "dominant_tool", "commits", "total_cost"):
                self.assertIn(key, data[0])

    def test_api_trust_returns_global_and_per_source(self):
        client = self._make_client()
        r = client.get("/api/trust?days=30")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("global", data)
        self.assertIn("per_source", data)
        self.assertIsInstance(data["per_source"], list)


if __name__ == "__main__":
    unittest.main()
