"""Tests for insights/trust_score.py — confidence scoring per source and global."""

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from ship1000x.core.storage import Storage
from ship1000x.insights.trust_score import (
    CONFIDENCE_WEIGHTS,
    compute_global_score,
    compute_source_score,
    get_all_source_scores,
    get_score_label,
)


class TestGetScoreLabel(unittest.TestCase):
    """Test the score → label mapping."""

    def test_factual_range(self):
        self.assertEqual(get_score_label(100)[0], "Factual")
        self.assertEqual(get_score_label(95)[0], "Factual")
        self.assertEqual(get_score_label(90)[0], "Factual")

    def test_defensible_range(self):
        self.assertEqual(get_score_label(89)[0], "Defensible")
        self.assertEqual(get_score_label(80)[0], "Defensible")
        self.assertEqual(get_score_label(70)[0], "Defensible")

    def test_indicative_range(self):
        self.assertEqual(get_score_label(69)[0], "Indicative")
        self.assertEqual(get_score_label(50)[0], "Indicative")
        self.assertEqual(get_score_label(40)[0], "Indicative")

    def test_low_range(self):
        self.assertEqual(get_score_label(39)[0], "Low")
        self.assertEqual(get_score_label(0)[0], "Low")


class TestComputeSourceScore(unittest.TestCase):
    """Per-source score computation, integration with DB."""

    def setUp(self):
        self._tmpdir = TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test.sqlite"
        self.storage = Storage(self.db_path)
        self.storage.init_schema()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _insert(self, source: str, confidence: str, n: int = 1):
        """Insert N events of given source/confidence in the recent window."""
        ts = datetime.now(timezone.utc).isoformat()
        with self.storage.conn() as c:
            for i in range(n):
                c.execute(
                    """INSERT INTO events
                       (id, source, event_type, started_at, confidence_flag, machine_id)
                       VALUES (?, ?, 'session', ?, ?, 'test')""",
                    (f"{source}-{confidence}-{i}", source, ts, confidence),
                )

    def test_no_events_returns_zero(self):
        self.assertEqual(compute_source_score(self.storage, "claude_code"), 0)

    def test_all_high_returns_100(self):
        self._insert("claude_code", "high", n=10)
        self.assertEqual(compute_source_score(self.storage, "claude_code"), 100)

    def test_all_medium_returns_70(self):
        self._insert("codex", "medium", n=5)
        self.assertEqual(compute_source_score(self.storage, "codex"), 70)

    def test_all_low_returns_40(self):
        self._insert("shell", "low", n=3)
        self.assertEqual(compute_source_score(self.storage, "shell"), 40)

    def test_weighted_mix(self):
        """50% high (100) + 50% medium (70) → average 85."""
        self._insert("git", "high", n=5)
        self._insert("git", "medium", n=5)
        self.assertEqual(compute_source_score(self.storage, "git"), 85)

    def test_weights_constant_correct(self):
        """Sanity check on the weights table."""
        self.assertEqual(CONFIDENCE_WEIGHTS["high"], 100)
        self.assertEqual(CONFIDENCE_WEIGHTS["medium"], 70)
        self.assertEqual(CONFIDENCE_WEIGHTS["low"], 40)


class TestGetAllSourceScores(unittest.TestCase):
    """Aggregated per-source scores."""

    def setUp(self):
        self._tmpdir = TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test.sqlite"
        self.storage = Storage(self.db_path)
        self.storage.init_schema()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _insert(self, source: str, confidence: str, n: int = 1):
        ts = datetime.now(timezone.utc).isoformat()
        with self.storage.conn() as c:
            for i in range(n):
                c.execute(
                    """INSERT INTO events
                       (id, source, event_type, started_at, confidence_flag, machine_id)
                       VALUES (?, ?, 'session', ?, ?, 'test')""",
                    (f"{source}-{confidence}-{i}", source, ts, confidence),
                )

    def test_empty_returns_empty(self):
        self.assertEqual(get_all_source_scores(self.storage), {})

    def test_multiple_sources(self):
        self._insert("claude_code", "high", n=10)
        self._insert("codex", "medium", n=5)
        self._insert("git", "high", n=3)
        result = get_all_source_scores(self.storage)
        self.assertEqual(set(result.keys()), {"claude_code", "codex", "git"})
        self.assertEqual(result["claude_code"]["score"], 100)
        self.assertEqual(result["claude_code"]["event_count"], 10)
        self.assertEqual(result["codex"]["score"], 70)
        self.assertEqual(result["git"]["score"], 100)


class TestComputeGlobalScore(unittest.TestCase):
    """Composite global score = raw weighted average + robustness checks."""

    def setUp(self):
        self._tmpdir = TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test.sqlite"
        self.storage = Storage(self.db_path)
        self.storage.init_schema()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _insert(self, source: str, confidence: str, n: int = 1):
        ts = datetime.now(timezone.utc).isoformat()
        with self.storage.conn() as c:
            for i in range(n):
                c.execute(
                    """INSERT INTO events
                       (id, source, event_type, started_at, confidence_flag, machine_id)
                       VALUES (?, ?, 'session', ?, ?, 'test')""",
                    (f"{source}-{confidence}-{i}", source, ts, confidence),
                )

    def _check(self, result: dict, name: str) -> dict:
        for c in result["robustness_checks"]:
            if c["name"] == name:
                return c
        self.fail(f"robustness check {name!r} not found")

    def test_no_data_returns_zero_label(self):
        result = compute_global_score(self.storage)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["label"], "No data")
        self.assertEqual(result["robustness_checks"], [])

    def test_score_is_raw_weighted_average(self):
        """Score = weighted avg per source — never capped, never inflated."""
        self._insert("claude_code", "high", n=10)
        self._insert("git", "high", n=10)
        result = compute_global_score(self.storage)
        self.assertEqual(result["score"], 100)
        # No additive fields: no base / bonus / penalty in the new shape
        self.assertNotIn("bonus", result)
        self.assertNotIn("penalty", result)
        self.assertNotIn("base", result)

    def test_weighted_average_mixed_confidence(self):
        """50% high (100) + 50% medium (70) → average 85."""
        self._insert("claude_code", "high", n=5)
        self._insert("claude_code", "medium", n=5)
        self._insert("git", "high", n=10)
        result = compute_global_score(self.storage)
        # 10 events @85 (claude) + 10 events @100 (git) → avg 92
        self.assertEqual(result["score"], 92)

    def test_critical_sources_check_passes_when_all_present(self):
        self._insert("claude_code", "high", n=10)
        self._insert("git", "high", n=10)
        result = compute_global_score(self.storage)
        chk = self._check(result, "Critical sources present")
        self.assertTrue(chk["passed"])

    def test_critical_sources_check_fails_when_missing(self):
        """Missing claude_code AND git → check fails but score is NOT penalized."""
        self._insert("codex", "high", n=10)
        result = compute_global_score(self.storage)
        # Score stays at the raw quality (100) — robustness is reported separately
        self.assertEqual(result["score"], 100)
        chk = self._check(result, "Critical sources present")
        self.assertFalse(chk["passed"])
        self.assertIn("claude_code", chk["detail"])
        self.assertIn("git", chk["detail"])

    def test_unified_check_passes_when_populated(self):
        self._insert("claude_code", "high", n=10)
        self._insert("git", "high", n=10)
        with self.storage.conn() as c:
            c.execute(
                """INSERT INTO daily_unified
                   (date, machine_id, computed_at)
                   VALUES (date('now'), 'test', '2026-05-15T00:00:00Z')""",
            )
        result = compute_global_score(self.storage)
        chk = self._check(result, "Cross-source unified")
        self.assertTrue(chk["passed"])

    def test_unified_check_fails_when_empty(self):
        self._insert("claude_code", "high", n=10)
        self._insert("git", "high", n=10)
        result = compute_global_score(self.storage)
        chk = self._check(result, "Cross-source unified")
        self.assertFalse(chk["passed"])
        self.assertIn("rollup", chk["detail"])

    def test_cadence_check_fails_without_email(self):
        self._insert("claude_code", "high", n=10)
        result = compute_global_score(self.storage)
        chk = self._check(result, "Cadence calibrated")
        self.assertFalse(chk["passed"])
        self.assertIn("calibrate", chk["detail"])

    def test_breakdown_in_result(self):
        self._insert("claude_code", "high", n=5)
        self._insert("git", "medium", n=5)
        result = compute_global_score(self.storage)
        self.assertIn("breakdown", result)
        self.assertEqual(set(result["breakdown"].keys()), {"claude_code", "git"})


if __name__ == "__main__":
    unittest.main()
