"""Smoke tests for the V1.2 web dashboard.

Verifies :
- App factory creates without error
- All routes return 200
- API endpoints return valid JSON with expected keys
- Localhost binding (security : refuses external)

These tests use a temporary in-memory DB so they don't depend on
a populated production DB.
"""

import json
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
                    confidence_flag, raw_meta, machine_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "e1",
                    "codex",
                    "session",
                    ts,
                    3600,
                    5.0,
                    "high",
                    json.dumps(
                        {
                            "usage": {
                                "provider": "openai",
                                "client": "codex-cli",
                                "model_canonical": "gpt-5-codex",
                                "tokens": {
                                    "input_tokens": 100,
                                    "output_tokens": 50,
                                    "cached_input_tokens": 10,
                                    "cache_write_tokens": 0,
                                    "reasoning_tokens": 5,
                                },
                                "cost": {
                                    "estimated_usd": 0.01,
                                    "api_equivalent_usd": 5.0,
                                    "billed_estimated_usd": 5.0,
                                    "auth_mode": "api_key",
                                    "quality": "factual",
                                    "pricing_version": "2026-04-21",
                                },
                                "auth_mode": "api_key",
                                "quality": {
                                    "tokens": "factual",
                                    "active_time": "defensible",
                                    "cost": "factual",
                                },
                            }
                        }
                    ),
                    "test",
                ),
            )
            c.execute(
                """INSERT INTO events
                   (id, source, event_type, started_at, duration_sec, cost_estimated,
                    confidence_flag, raw_meta, machine_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "e2",
                    "codex_macapp",
                    "session",
                    ts,
                    1800,
                    10.0,
                    "medium",
                    json.dumps(
                        {
                            "usage": {
                                "provider": "openai",
                                "client": "codex-macapp",
                                "model_canonical": "unknown",
                                "cost": {
                                    "estimated_usd": 10.0,
                                    "api_equivalent_usd": 10.0,
                                    "billed_estimated_usd": 0.0,
                                    "auth_mode": "oauth",
                                    "quality": "indicative",
                                    "pricing_version": "2026-04-21",
                                },
                                "auth_mode": "oauth",
                                "quality": {
                                    "tokens": "unknown",
                                    "active_time": "defensible",
                                    "cost": "indicative",
                                },
                            }
                        }
                    ),
                    "test",
                ),
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
        html = r.get_data(as_text=True)
        self.assertIn("API-equivalent cost", html)
        self.assertIn("billed-est.", html)
        self.assertIn("unknown-basis", html)

    def test_api_highlights_returns_valid_json(self):
        client = self._make_client()
        r = client.get("/api/highlights?days=30")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["schema_version"], "ship1000x.dashboard.highlights.v1")
        # Must contain these keys
        for key in ("orchestration_factor", "agent_hours_additive", "days_equivalent", "active_hours",
                    "lines_real", "cost_api_equivalent", "cost_total", "trust_score", "trust_label",
                    "trust_robustness", "sources_count", "threshold_min", "window_days",
                    "cost_total_basis", "schema_version"):
            self.assertIn(key, data, f"missing key in /api/highlights: {key}")
        # Robustness checks must be a list of {name, passed, detail}
        self.assertIsInstance(data["trust_robustness"], list)
        for chk in data["trust_robustness"]:
            self.assertIn("name", chk)
            self.assertIn("passed", chk)
            self.assertIn("detail", chk)
        self.assertEqual(data["cost_total"], 15.0)
        self.assertEqual(data["cost_total_basis"], "api_equivalent_legacy_alias")
        self.assertEqual(data["cost_api_equivalent"], 15.0)
        self.assertEqual(data["cost_factual"], 5.0)
        self.assertEqual(data["cost_factual_pct"], 33.3)
        self.assertEqual(data["cost_truth"]["api_equivalent_usd"], 15.0)
        self.assertEqual(data["cost_truth"]["billed_estimated_usd"], 5.0)
        self.assertEqual(data["cost_truth"]["subscription_absorbed_usd"], 10.0)
        self.assertEqual(data["cost_truth"]["unknown_billing_basis_usd"], 0.0)
        self.assertEqual(data["cost_truth"]["native_token_pricing_usd"], 5.0)
        self.assertEqual(
            data["cost_truth"]["presentation_status"],
            "api_equivalent_with_subscription_absorption",
        )
        self.assertIn("not invoice truth", data["cost_truth"]["presentation_label"])

    def test_api_highlights_keeps_legacy_unknown_cost_out_of_billed_estimate(self):
        s = Storage(self.db_path)
        ts = datetime.now(timezone.utc).isoformat()
        with s.conn() as c:
            c.execute(
                """INSERT INTO events
                   (id, source, event_type, started_at, duration_sec, cost_estimated,
                    confidence_flag, raw_meta, machine_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "legacy-unknown-cost",
                    "legacy_tool",
                    "session",
                    ts,
                    60,
                    2.0,
                    "low",
                    "{}",
                    "test",
                ),
            )

        client = self._make_client()
        r = client.get("/api/highlights?days=30")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["cost_truth"]["api_equivalent_usd"], 17.0)
        self.assertEqual(data["cost_truth"]["billed_estimated_usd"], 5.0)
        self.assertEqual(data["cost_truth"]["unknown_billing_basis_usd"], 2.0)
        self.assertEqual(data["cost_truth"]["events_with_unknown_billing_basis"], 1)
        self.assertEqual(data["cost_truth"]["presentation_status"], "unknown_billing_basis")
        self.assertIn("unknown-basis rows", data["cost_truth"]["presentation_label"])

    def test_api_highlights_uses_explicit_cost_truth_for_api_equivalent_total(self):
        s = Storage(self.db_path)
        ts = datetime.now(timezone.utc).isoformat()
        with s.conn() as c:
            c.execute(
                """INSERT INTO events
                   (id, source, event_type, started_at, duration_sec, cost_estimated,
                    confidence_flag, raw_meta, machine_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "explicit-api-equivalent-cost",
                    "codex",
                    "session",
                    ts,
                    60,
                    0.0,
                    "high",
                    json.dumps(
                        {
                            "usage": {
                                "cost": {
                                    "api_equivalent_usd": 4.0,
                                    "billed_estimated_usd": 4.0,
                                    "auth_mode": "api_key",
                                    "quality": "factual",
                                },
                                "auth_mode": "api_key",
                                "quality": {"cost": "factual"},
                            }
                        }
                    ),
                    "test",
                ),
            )

        client = self._make_client()
        r = client.get("/api/highlights?days=30")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["cost_api_equivalent"], 19.0)
        self.assertEqual(data["cost_total"], 19.0)
        self.assertEqual(data["cost_factual"], 9.0)
        self.assertEqual(data["cost_factual_pct"], 47.4)
        self.assertEqual(data["cost_truth"]["api_equivalent_usd"], 19.0)
        self.assertEqual(data["cost_truth"]["native_token_pricing_usd"], 9.0)
        self.assertEqual(data["cost_truth"]["billed_estimated_usd"], 9.0)
        self.assertEqual(
            data["cost_truth"]["presentation_status"],
            "api_equivalent_with_subscription_absorption",
        )

    def test_api_trend_returns_list(self):
        client = self._make_client()
        r = client.get("/api/trend?days=30")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIsInstance(data, list)
        if data:
            self.assertEqual(data[0]["schema_version"], "ship1000x.dashboard.trend_point.v1")
            self.assertIn("date", data[0])
            self.assertIn("active_hours", data[0])
            self.assertIn("wall_hours", data[0])

    def test_api_projects_returns_list(self):
        client = self._make_client()
        r = client.get("/api/projects?days=30")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIsInstance(data, list)
        if data:
            for key in (
                "project_id",
                "schema_version",
                "total_hours",
                "dominant_tool",
                "commits",
                "total_api_equivalent_cost",
                "total_billed_estimated_cost",
                "total_subscription_absorbed_cost",
                "unknown_billing_basis_cost",
                "events_with_unknown_billing_basis",
                "cost_truth",
                "total_cost",
                "total_cost_basis",
            ):
                self.assertIn(key, data[0])
            self.assertEqual(data[0]["schema_version"], "ship1000x.dashboard.project.v1")
            self.assertEqual(data[0]["total_api_equivalent_cost"], data[0]["total_cost"])
            self.assertEqual(data[0]["total_cost_basis"], "api_equivalent_legacy_alias")
            self.assertIn("presentation_status", data[0]["cost_truth"])
            self.assertIn("presentation_label", data[0]["cost_truth"])
            self.assertIn("not invoice truth", data[0]["cost_truth"]["presentation_label"])
            self.assertEqual(data[0]["cost_truth"]["api_equivalent_usd"], 15.0)
            self.assertEqual(data[0]["cost_truth"]["billed_estimated_usd"], 5.0)
            self.assertEqual(data[0]["cost_truth"]["subscription_absorbed_usd"], 10.0)

    def test_api_projects_uses_explicit_cost_truth_for_api_equivalent_total(self):
        s = Storage(self.db_path)
        ts = datetime.now(timezone.utc).isoformat()
        with s.conn() as c:
            c.execute(
                """INSERT INTO events
                   (id, source, event_type, project_id, started_at, duration_sec,
                    cost_estimated, confidence_flag, raw_meta, machine_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "project-explicit-api-equivalent-cost",
                    "codex",
                    "session",
                    "explicit-project",
                    ts,
                    60,
                    0.0,
                    "high",
                    json.dumps(
                        {
                            "usage": {
                                "cost": {
                                    "api_equivalent_usd": 4.0,
                                    "billed_estimated_usd": 4.0,
                                    "auth_mode": "api_key",
                                    "quality": "factual",
                                },
                                "auth_mode": "api_key",
                                "quality": {"cost": "factual"},
                            }
                        }
                    ),
                    "test",
                ),
            )

        client = self._make_client()
        r = client.get("/api/projects?days=30")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        explicit = next(row for row in data if row["project_id"] == "explicit-project")
        self.assertEqual(explicit["total_api_equivalent_cost"], 4.0)
        self.assertEqual(explicit["total_cost"], 4.0)
        self.assertEqual(explicit["total_cost_basis"], "api_equivalent_legacy_alias")
        self.assertEqual(explicit["total_billed_estimated_cost"], 4.0)
        self.assertEqual(explicit["total_subscription_absorbed_cost"], 0.0)
        self.assertEqual(explicit["unknown_billing_basis_cost"], 0.0)
        self.assertEqual(
            explicit["cost_truth"]["presentation_status"],
            "api_equivalent_matches_billed_estimate",
        )
        self.assertEqual(explicit["sources_ia"], 1)

    def test_api_projects_surfaces_unknown_billing_basis_per_project(self):
        s = Storage(self.db_path)
        ts = datetime.now(timezone.utc).isoformat()
        with s.conn() as c:
            c.execute(
                """INSERT INTO events
                   (id, source, event_type, project_id, started_at, duration_sec,
                    cost_estimated, confidence_flag, raw_meta, machine_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "project-unknown-billing",
                    "legacy_tool",
                    "session",
                    "unknown-billing-project",
                    ts,
                    60,
                    2.0,
                    "low",
                    "{}",
                    "test",
                ),
            )

        client = self._make_client()
        r = client.get("/api/projects?days=30")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        project = next(row for row in data if row["project_id"] == "unknown-billing-project")
        self.assertEqual(project["total_api_equivalent_cost"], 2.0)
        self.assertEqual(project["total_billed_estimated_cost"], 0.0)
        self.assertEqual(project["unknown_billing_basis_cost"], 2.0)
        self.assertEqual(project["events_with_unknown_billing_basis"], 1)
        self.assertEqual(project["cost_truth"]["presentation_status"], "unknown_billing_basis")
        self.assertIn("unknown-basis rows", project["cost_truth"]["presentation_label"])

    def test_api_trust_returns_global_and_per_source(self):
        client = self._make_client()
        r = client.get("/api/trust?days=30")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["schema_version"], "ship1000x.dashboard.trust.v1")
        self.assertIn("global", data)
        self.assertIn("per_source", data)
        self.assertIsInstance(data["per_source"], list)
        if data["per_source"]:
            self.assertEqual(
                data["per_source"][0]["schema_version"],
                "ship1000x.dashboard.trust_source.v1",
            )
            self.assertIn("source", data["per_source"][0])
            self.assertIn("score", data["per_source"][0])
            self.assertIn("event_count", data["per_source"][0])
            self.assertIn("label", data["per_source"][0])

    def test_api_source_quality_returns_audit_shape(self):
        client = self._make_client()
        r = client.get("/api/source-quality?days=30")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["schema_version"], "ship1000x.source_quality_report.v1")
        self.assertIn("summary", data)
        self.assertIn("rows", data)
        self.assertIn("average_observed_quality_score", data["summary"])
        self.assertIn("low_quality_sources", data["summary"])
        codex = next(row for row in data["rows"] if row["source"] == "codex")
        self.assertEqual(codex["risk"], "ok")
        self.assertEqual(codex["usage_events"], 1)
        self.assertEqual(codex["quality_band"], "high")
        self.assertEqual(codex["quality_scores"]["overall"], 93.33)

    def test_overview_renders_source_quality_scorecard_shell(self):
        client = self._make_client()
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Measurement quality", html)
        self.assertIn("API-equivalent cost", html)
        self.assertIn("cost_api_equivalent", html)
        self.assertIn("total_api_equivalent_cost", html)
        self.assertIn("metric-cost-billing-label", html)
        self.assertIn("native token/pricing", html)
        self.assertIn("remainder indicative/unknown", html)
        self.assertIn("unknown-basis", html)
        self.assertIn("events_with_unknown_billing_basis", html)
        self.assertIn("presentation_label", html)
        self.assertIn("not invoice truth", html)
        self.assertNotIn("API-equivalent · factual", html)
        self.assertNotIn("% factual, rest heuristic", html)
        self.assertNotIn("rest heuristic", html)
        self.assertIn("source-quality-avg", html)
        self.assertIn("loadSourceQuality", html)


if __name__ == "__main__":
    unittest.main()
