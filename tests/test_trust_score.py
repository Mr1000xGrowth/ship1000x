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
    """Composite global score with bonuses/penalties."""

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

    def test_no_data_returns_zero_label(self):
        result = compute_global_score(self.storage)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["label"], "No data")

    def test_critical_sources_penalty(self):
        """Missing claude_code AND git → -20 penalty."""
        self._insert("codex", "high", n=10)  # 100 score, but missing critical
        result = compute_global_score(self.storage)
        # Base 100, no bonuses (no cadence, no unified), penalty -20
        self.assertEqual(result["penalty"], 20)
        self.assertEqual(result["score"], 80)

    def test_critical_sources_present_no_penalty(self):
        self._insert("claude_code", "high", n=10)
        self._insert("git", "high", n=10)
        result = compute_global_score(self.storage)
        self.assertEqual(result["penalty"], 0)
        # Base 100, no bonuses → 100
        self.assertEqual(result["score"], 100)

    def test_score_capped_at_100(self):
        """Even with bonuses, score caps at 100."""
        self._insert("claude_code", "high", n=10)
        self._insert("git", "high", n=10)
        # Add daily_unified row to trigger +5 bonus
        with self.storage.conn() as c:
            c.execute(
                """INSERT INTO daily_unified
                   (date, machine_id, computed_at)
                   VALUES (date('now'), 'test', '2026-05-15T00:00:00Z')""",
            )
        result = compute_global_score(self.storage)
        self.assertEqual(result["score"], 100)  # Capped
        self.assertEqual(result["bonus"], 5)

    def test_breakdown_in_result(self):
        self._insert("claude_code", "high", n=5)
        self._insert("git", "medium", n=5)
        result = compute_global_score(self.storage)
        self.assertIn("breakdown", result)
        self.assertEqual(set(result["breakdown"].keys()), {"claude_code", "git"})


if __name__ == "__main__":
    unittest.main()
