"""Tests for source quality audit reporting."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from ship1000x.core.source_inventory import PRODUCTION_EVENT_SOURCES
from ship1000x.core.source_quality import (
    SOURCE_QUALITY_PROFILES,
    build_source_quality_report,
)
from ship1000x.core.storage import Storage


class TestSourceQualityReport(unittest.TestCase):
    def setUp(self):
        self._tmpdir = TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "tracker.sqlite"
        self.storage = Storage(self.db_path)
        self.storage.init_schema()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _insert_event(
        self,
        *,
        event_id: str,
        source: str,
        raw_meta: dict | None = None,
        confidence: str = "high",
        token_input: int = 0,
        token_output: int = 0,
        cost: float = 0.0,
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self.storage.conn() as conn:
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
                    ts,
                    60,
                    token_input,
                    token_output,
                    cost,
                    confidence,
                    json.dumps(raw_meta) if raw_meta is not None else None,
                    "test-machine",
                ),
            )

    def test_reports_usage_metadata_and_missing_sources(self):
        self._insert_event(
            event_id="codex-1",
            source="codex",
            token_input=100,
            token_output=50,
            cost=0.002,
            raw_meta={
                "usage": {
                    "provider": "openai",
                    "client": "codex-cli",
                    "model_canonical": "gpt-5-codex",
                    "tokens": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cached_input_tokens": 20,
                        "cache_write_tokens": 0,
                        "reasoning_tokens": 10,
                    },
                    "cost": {
                        "estimated_usd": 0.002,
                        "billed_estimated_usd": 0.0,
                        "quality": "factual",
                        "pricing_version": "2026-04-21",
                    },
                    "auth_mode": "oauth",
                    "quality": {
                        "tokens": "factual",
                        "active_time": "defensible",
                        "cost": "factual",
                    },
                },
                "prompt": "SECRET PROMPT SHOULD NOT LEAK",
            },
        )
        self._insert_event(event_id="claude-1", source="claude_code")

        report = build_source_quality_report(self.storage, window_days=30)

        codex = next(row for row in report["rows"] if row["source"] == "codex")
        claude = next(row for row in report["rows"] if row["source"] == "claude_code")

        self.assertEqual(codex["risk"], "ok")
        self.assertEqual(codex["collector_stage"], "default_ingest")
        self.assertFalse(codex["collector_stage_untrusted_for_public_claims"])
        self.assertEqual(codex["usage_events"], 1)
        self.assertEqual(codex["observed_quality"]["tokens"]["factual"], 1)
        self.assertEqual(codex["pricing_versions"], ["2026-04-21"])
        self.assertEqual(codex["auth_mode_counts"], {"oauth": 1})
        self.assertEqual(codex["cost_truth"]["api_equivalent_usd"], 0.002)
        self.assertEqual(codex["cost_truth"]["billed_estimated_usd"], 0.0)
        self.assertEqual(codex["cost_truth"]["subscription_absorbed_usd"], 0.002)
        self.assertEqual(codex["quality_scores"]["tokens"], 100)
        self.assertEqual(codex["quality_scores"]["cost"], 100)
        self.assertEqual(codex["quality_scores"]["active_time"], 80)
        self.assertEqual(codex["quality_scores"]["overall"], 93.33)
        self.assertEqual(codex["quality_band"], "high")
        self.assertEqual(claude["risk"], "fragile")
        self.assertEqual(claude["quality_scores"]["overall"], 0)
        self.assertEqual(claude["quality_band"], "low")
        self.assertIn("claude_code", report["missing_usage_metadata_sources"])
        self.assertEqual(report["summary"]["low_quality_sources"], 1)

        serialized = json.dumps(report)
        self.assertNotIn("SECRET PROMPT", serialized)
        self.assertNotIn("prompt", serialized.lower())

    def test_unknown_source_is_visible_not_trusted(self):
        self._insert_event(event_id="x-1", source="mystery_tool", confidence="medium")

        report = build_source_quality_report(self.storage, window_days=30)
        row = next(row for row in report["rows"] if row["source"] == "mystery_tool")

        self.assertEqual(row["risk"], "unknown-source")
        self.assertEqual(row["collector_stage"], "unknown")
        self.assertEqual(report["summary"]["observed_unknown_sources"], 1)
        self.assertEqual(row["next_action"], "Add a source quality profile before trusting this source.")

    def test_production_event_sources_have_source_quality_profiles(self):
        missing = sorted(PRODUCTION_EVENT_SOURCES - set(SOURCE_QUALITY_PROFILES))

        self.assertEqual(missing, [])

    def test_ship_emitted_non_usage_sources_are_profiled_not_unknown(self):
        self._insert_event(event_id="web-export", source="web_export")
        self._insert_event(event_id="secret-alert", source="git_secret_alert")

        report = build_source_quality_report(self.storage, window_days=30)
        web_export = next(row for row in report["rows"] if row["source"] == "web_export")
        secret_alert = next(row for row in report["rows"] if row["source"] == "git_secret_alert")

        self.assertEqual(report["summary"]["observed_unknown_sources"], 0)
        self.assertEqual(web_export["risk"], "partial")
        self.assertEqual(web_export["expected_quality"]["tokens"], "unknown")
        self.assertEqual(web_export["expected_quality"]["cost"], "unknown")
        self.assertEqual(secret_alert["risk"], "ok")
        self.assertEqual(secret_alert["expected_quality"]["tokens"], "n/a")

    def test_provider_expansion_sources_are_known_but_conservative(self):
        for source in (
            "gemini_cli",
            "copilot_agents",
            "opencode",
            "cursor_agent",
            "roo_kilo_code",
        ):
            self._insert_event(
                event_id=f"{source}-fixture",
                source=source,
                raw_meta={
                    "usage": {
                        "provider": source.split("_", 1)[0],
                        "client": source,
                        "model_canonical": "unknown",
                        "quality": {
                            "tokens": "unknown",
                            "cost": "unknown",
                            "active_time": "defensible",
                        },
                        "cost": {
                            "estimated_usd": 0,
                            "quality": "unknown",
                            "pricing_version": "unknown",
                        },
                    },
                    "prompt": "SECRET PROMPT SHOULD NOT LEAK",
                    "path": "/Users/example/private/project",
                },
            )

        report = build_source_quality_report(self.storage, window_days=30)

        assert report["summary"]["observed_unknown_sources"] == 0
        assert report["summary"]["collector_stage_counts"] == {
            "default_ingest": 1,
            "fixture_only": 4,
        }
        assert report["summary"]["public_untrusted_stage_sources"] == 4
        expected_stages = {
            "gemini_cli": "fixture_only",
            "copilot_agents": "fixture_only",
            "opencode": "fixture_only",
            "cursor_agent": "fixture_only",
            "roo_kilo_code": "default_ingest",
        }
        for source, stage in expected_stages.items():
            row = next(row for row in report["rows"] if row["source"] == source)
            self.assertEqual(row["risk"], "partial")
            self.assertEqual(row["collector_stage"], stage)
            self.assertEqual(
                row["collector_stage_untrusted_for_public_claims"],
                stage == "fixture_only",
            )
            self.assertEqual(row["usage_events"], 1)
            self.assertEqual(row["observed_quality"]["tokens"]["unknown"], 1)
            self.assertEqual(row["observed_quality"]["cost"]["unknown"], 1)
            self.assertEqual(row["observed_quality"]["active_time"]["defensible"], 1)
            self.assertEqual(row["quality_band"], "low")

        serialized = json.dumps(report)
        self.assertNotIn("SECRET PROMPT", serialized)
        self.assertNotIn("/Users/example", serialized)


if __name__ == "__main__":
    unittest.main()
