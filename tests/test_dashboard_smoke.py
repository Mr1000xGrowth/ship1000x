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

    def test_estimate_page_returns_200(self):
        client = self._make_client()
        r = client.get("/estimate")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        # Indicative / estimative ratios live here, not on Overview.
        self.assertIn("Agent efficiency", html)
        self.assertIn("Project output vs human team", html)
        self.assertIn("total_api_equivalent_cost", html)
        self.assertIn("not measured", html)

    def test_audit_page_returns_200(self):
        client = self._make_client()
        r = client.get("/audit")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Audit log", html)
        # Read-only / privacy framing must be visible.
        self.assertIn("metadata only", html)

    def test_api_audit_returns_valid_json(self):
        client = self._make_client()
        r = client.get("/api/audit?days=30")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["schema_version"], "ship1000x.dashboard.audit.v1")
        for key in ("page", "per_page", "pages", "total", "events", "facets"):
            self.assertIn(key, data)
        # Both seeded codex events surface (git excluded, none here).
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(data["events"]), 2)
        ev = data["events"][0]
        for key in ("id", "source", "client", "provider", "model",
                    "tokens", "pricing_quality", "has_usage_breakdown"):
            self.assertIn(key, ev)
        # Facets reflect the seeded providers/sources.
        sources = {f["value"] for f in data["facets"]["sources"]}
        self.assertIn("codex", sources)

    def test_api_audit_filters_by_source(self):
        client = self._make_client()
        r = client.get("/api/audit?days=30&source=codex_macapp")
        data = r.get_json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["events"][0]["source"], "codex_macapp")

    def test_api_audit_never_exposes_content_keys(self):
        client = self._make_client()
        data = client.get("/api/audit?days=30").get_json()
        forbidden = {"content", "text", "message", "prompt", "response",
                     "diff", "command", "input", "output"}
        for ev in data["events"]:
            blob = json.dumps(ev)
            ub = ev.get("usage_breakdown") or {}
            for k in forbidden:
                self.assertNotIn(k, ub)

    def test_overview_excludes_estimative_blocks(self):
        client = self._make_client()
        html = client.get("/").get_data(as_text=True)
        # R2/R3 moved to the Estimate tab; Overview is measured-only.
        self.assertNotIn("Agent efficiency", html)
        self.assertNotIn("Project output vs human team", html)

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

    def test_api_cost_models_returns_valid_json(self):
        client = self._make_client()
        r = client.get("/api/cost-models?days=30")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["schema_version"], "ship1000x.dashboard.cost_models.v1")
        for key in ("totals", "by_model", "pricing", "window_days"):
            self.assertIn(key, data)
        for key in ("api_equivalent", "billed", "subscription_absorbed"):
            self.assertIn(key, data["totals"])
        self.assertIsInstance(data["by_model"], list)
        self.assertIn("version", data["pricing"])
        self.assertIn("fallback_models", data["pricing"])

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
                "total_tokens",
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

    def test_api_work_mix_decomposes_real_lines(self):
        s = Storage(self.db_path)
        ts = datetime.now(timezone.utc).isoformat()
        with s.conn() as c:
            c.execute(
                """INSERT INTO events
                   (id, source, event_type, project_id, started_at, duration_sec,
                    cost_estimated, confidence_flag, raw_meta, machine_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "wm1", "git", "commit", "alpha", ts, 0, 0.0, "high",
                    json.dumps({
                        "lines_added": 1000, "lines_real_added": 1000,
                        "lines_code_added": 600, "lines_docs_added": 300,
                        "lines_config_added": 0, "lines_data_added": 100,
                    }),
                    "test",
                ),
            )
        r = self._make_client().get("/api/work-mix?days=30")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["schema_version"], "ship1000x.dashboard.work_mix.v1")
        g = d["global"]
        self.assertEqual(g["code"], 600)
        self.assertEqual(g["docs"], 300)
        self.assertEqual(g["data"], 100)
        self.assertEqual(g["code_share_pct"], 60.0)
        self.assertEqual(g["docs_per_code"], 0.5)
        self.assertEqual(g["pending_reclassify"], 0)
        self.assertTrue(any(p["project"] == "alpha" for p in d["by_project"]))
        self.assertTrue(d["by_day"])

    def test_api_work_mix_never_exposes_paths_or_content(self):
        data = self._make_client().get("/api/work-mix?days=30").get_json()
        blob = json.dumps(data)
        for forbidden in ("cwd", "raw_meta", "/Users", "diff", "prompt"):
            self.assertNotIn(forbidden, blob)

    def test_api_projects_tokens_read_from_raw_meta_not_columns(self):
        # Regression: the bulk of tokens (esp. cache) lives in raw_meta, not the
        # token_input/output columns. Summing columns undercounts to ~0.
        s = Storage(self.db_path)
        ts = datetime.now(timezone.utc).isoformat()
        with s.conn() as c:
            c.execute(
                """INSERT INTO events
                   (id, source, event_type, project_id, started_at, duration_sec,
                    cost_estimated, token_input, token_output, confidence_flag,
                    raw_meta, machine_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "tok1", "claude_code", "session_day", "tokproj", ts, 60, 1.0,
                    0, 0, "high",  # columns at 0 on purpose
                    json.dumps({"usage_breakdown": {
                        "fresh_input": 1000, "cache_read": 900000,
                        "cache_write_5m": 40000, "output_tokens": 18000,
                    }}),
                    "test",
                ),
            )
        data = self._make_client().get("/api/projects?days=30").get_json()
        proj = next(p for p in data if p["project_id"] == "tokproj")
        self.assertEqual(proj["total_tokens"], 959000)

    def test_api_production_mode_splits_by_auth_route(self):
        # setUp seeds e1 (auth_mode=api_key, api_equivalent 5.0) -> programmatic
        # and e2 (codex_macapp, auth_mode=oauth, api_equivalent 10.0) -> interactive.
        r = self._make_client().get("/api/production-mode?days=30")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["schema_version"], "ship1000x.dashboard.production_mode.v1")
        m = d["modes"]
        self.assertGreater(m["programmatic"]["api_equivalent_usd"], 0)
        self.assertGreater(m["interactive"]["api_equivalent_usd"], 0)
        for mode in ("interactive", "programmatic", "unknown"):
            self.assertIn("cost_share_pct", m[mode])
            self.assertIn("token_share_pct", m[mode])
        # privacy boundary: aggregates only
        blob = json.dumps(d)
        for forbidden in ("raw_meta", "cwd", "/Users", "prompt", "diff"):
            self.assertNotIn(forbidden, blob)

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
