"""Tests unitaires pour core/intervals.py — union cross-sources et compute unified."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ship1000x.core.storage import Storage
from ship1000x.core.unified_metrics import (
    DEDUP_TOLERANCE_SEC,
    THRESHOLD_LOOSE_SEC,
    THRESHOLD_STRICT_SEC,
    compute_active_sec_with_threshold,
    compute_unified_metrics,
    get_daily_unified,
    merge_human_events_cross_sources,
    upsert_daily_unified,
)


class TestMergeHumanEventsCrossSources(unittest.TestCase):
    """Tests sur la fusion d'event_timeline cross-sources."""

    def test_empty_input(self):
        """Aucune source -> liste vide, 0 sources."""
        ts, count = merge_human_events_cross_sources([])
        self.assertEqual(ts, [])
        self.assertEqual(count, 0)

    def test_single_source_human_only(self):
        """1 source avec events humains uniquement."""
        timelines = [("claude_code", [[1700000000, 0], [1700000060, 1], [1700000120, 2]])]
        ts, count = merge_human_events_cross_sources(timelines)
        self.assertEqual(ts, [1700000000, 1700000060, 1700000120])
        self.assertEqual(count, 1)

    def test_filters_non_human_codes(self):
        """Les codes 3 (assistant) et 4 (tool_result) et 5 (system) sont exclus."""
        timelines = [("claude_code", [
            [1700000000, 0],   # typed -> garde
            [1700000060, 3],   # assistant -> exclus
            [1700000120, 4],   # tool_result -> exclus
            [1700000180, 5],   # system -> exclus
            [1700000240, 1],   # approval -> garde
            [1700000300, 2],   # paste -> garde
        ])]
        ts, count = merge_human_events_cross_sources(timelines)
        self.assertEqual(ts, [1700000000, 1700000240, 1700000300])
        self.assertEqual(count, 1)

    def test_two_sources_merge_and_sort(self):
        """2 sources -> events fusionnes et tries chronologiquement."""
        timelines = [
            ("claude_code", [[1700000000, 0], [1700000200, 0]]),
            ("codex_macapp", [[1700000100, 1], [1700000300, 0]]),
        ]
        ts, count = merge_human_events_cross_sources(timelines)
        self.assertEqual(ts, [1700000000, 1700000100, 1700000200, 1700000300])
        self.assertEqual(count, 2)

    def test_dedup_tolerance(self):
        """2 events a +/- DEDUP_TOLERANCE_SEC sont dedupliques."""
        # 2 sources voient le meme prompt avec 1s de decalage
        timelines = [
            ("claude_code", [[1700000000, 0]]),
            ("openclaw", [[1700000001, 0]]),  # 1s plus tard, dedup
        ]
        ts, count = merge_human_events_cross_sources(timelines)
        self.assertEqual(len(ts), 1)
        self.assertEqual(count, 2)

    def test_dedup_does_not_collapse_real_intervals(self):
        """2 events a +DEDUP_TOLERANCE_SEC+1 sec sont gardes (pas de dedup)."""
        timelines = [
            ("claude_code", [[1700000000, 0]]),
            ("codex_macapp", [[1700000000 + DEDUP_TOLERANCE_SEC + 1, 0]]),
        ]
        ts, count = merge_human_events_cross_sources(timelines)
        self.assertEqual(len(ts), 2)

    def test_source_without_human_events_not_counted(self):
        """Une source qui n'emet QUE des events non-humains n'est pas comptee."""
        timelines = [
            ("claude_code", [[1700000000, 0]]),         # humain
            ("codex_macapp", [[1700000060, 3]]),        # assistant only
        ]
        ts, count = merge_human_events_cross_sources(timelines)
        self.assertEqual(len(ts), 1)
        self.assertEqual(count, 1)  # codex_macapp pas compte

    def test_invalid_entries_skipped(self):
        """Entries malformees (pas list, pas len>=2, ts/code non int) sont ignorees."""
        timelines = [("claude_code", [
            [1700000000, 0],          # ok
            "invalid_entry",          # pas list
            [1700000060],             # len < 2
            ["abc", "xyz"],           # types invalides
            [1700000120, 0],          # ok
            [-100, 0],                # ts negatif -> exclus (ts > 0)
        ])]
        ts, count = merge_human_events_cross_sources(timelines)
        self.assertEqual(ts, [1700000000, 1700000120])


class TestComputeActiveSecWithThreshold(unittest.TestCase):
    """Tests sur le calcul d'actif avec threshold."""

    def test_empty_or_single_event(self):
        """0 ou 1 event -> 0 sec actif (pas d'intervalle)."""
        self.assertEqual(compute_active_sec_with_threshold([], 300), 0)
        self.assertEqual(compute_active_sec_with_threshold([1700000000], 300), 0)

    def test_threshold_zero_or_negative(self):
        """Threshold <= 0 -> 0 sec (defensif)."""
        self.assertEqual(compute_active_sec_with_threshold([1, 2, 3], 0), 0)
        self.assertEqual(compute_active_sec_with_threshold([1, 2, 3], -10), 0)

    def test_all_intervals_under_threshold(self):
        """Tous les intervalles <= threshold -> somme totale."""
        ts = [1700000000, 1700000060, 1700000180, 1700000300]  # +60, +120, +120
        # Avec threshold 200 sec, tous comptent : 60 + 120 + 120 = 300
        self.assertEqual(compute_active_sec_with_threshold(ts, 200), 300)

    def test_some_intervals_over_threshold(self):
        """Intervalles > threshold sont exclus (vraies pauses)."""
        ts = [1700000000, 1700000060, 1700001000, 1700001120]  # +60, +940 (gap), +120
        # Avec threshold 300 : 60 (oui) + 940 (non, pause) + 120 (oui) = 180
        self.assertEqual(compute_active_sec_with_threshold(ts, 300), 180)

    def test_strict_vs_loose_consistency(self):
        """Mode loose (15min) capture toujours >= mode strict (5min)."""
        # Intervalles [3min, 8min, 12min, 18min] depuis t0
        ts = [1700000000, 1700000180, 1700000660, 1700001380, 1700002460]
        active_strict = compute_active_sec_with_threshold(ts, THRESHOLD_STRICT_SEC)
        active_loose = compute_active_sec_with_threshold(ts, THRESHOLD_LOOSE_SEC)
        self.assertGreaterEqual(active_loose, active_strict)
        # strict capte que le 1er gap (3min)
        self.assertEqual(active_strict, 180)
        # loose capte 3min + 8min + 12min (mais pas 18min)
        self.assertEqual(active_loose, 180 + 480 + 720)


class TestComputeUnifiedMetricsIntegration(unittest.TestCase):
    """Tests d'integration : DB temporaire + flow complet."""

    def setUp(self):
        self._tmpdir = TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test.sqlite"
        self.storage = Storage(self.db_path)
        self.storage.init_schema()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _insert_event(self, event_id: str, source: str, started_at: str, timeline: list):
        """Insert un event avec event_timeline dans raw_meta."""
        import json
        with self.storage.conn() as c:
            c.execute(
                """INSERT INTO events (id, source, event_type, started_at, raw_meta, machine_id)
                   VALUES (?, ?, 'session', ?, ?, ?)""",
                (event_id, source, started_at,
                 json.dumps({"event_timeline": timeline}), "test-machine"),
            )

    def test_no_events_returns_none(self):
        """Aucun event -> None."""
        result = compute_unified_metrics(self.storage, "2026-05-14")
        self.assertIsNone(result)

    def test_single_source_single_session(self):
        """1 source, 1 session avec 3 events humains -> active calcule correctement."""
        # 3 events espaces de 60 sec
        timeline = [[1715641200, 0], [1715641260, 1], [1715641320, 0]]
        self._insert_event("e1", "claude_code", "2026-05-14T00:00:00Z", timeline)

        result = compute_unified_metrics(self.storage, "2026-05-14", machine_id="test-machine")
        self.assertIsNotNone(result)
        # 2 intervalles de 60 sec chacun -> 120 sec strict
        self.assertEqual(result["active_sec_strict"], 120)
        self.assertEqual(result["sample_size"], 3)
        self.assertEqual(result["sources_count"], 1)
        self.assertEqual(result["wall_clock_sec"], 120)

    def test_multi_sources_parallel_dedup(self):
        """2 sources en parallele -> events dedupliques, sources_count=2."""
        # Claude code + Codex MacApp voient le meme prompt a +/- 1s
        self._insert_event("e1", "claude_code", "2026-05-14T00:00:00Z",
                           [[1715641200, 0], [1715641260, 0]])
        self._insert_event("e2", "codex_macapp", "2026-05-14T00:00:00Z",
                           [[1715641201, 0], [1715641261, 0]])  # +/- 1s

        result = compute_unified_metrics(self.storage, "2026-05-14", machine_id="test-machine")
        self.assertEqual(result["sources_count"], 2)
        # 4 events bruts mais 2 dedupliques -> 1 intervalle de 60 sec
        self.assertEqual(result["sample_size"], 2)
        self.assertEqual(result["active_sec_strict"], 60)

    def test_unified_alias_matches_p95(self):
        """active_sec_unified = active_sec_p95 (alias canonique V1)."""
        self._insert_event("e1", "claude_code", "2026-05-14T00:00:00Z",
                           [[1715641200, 0], [1715641260, 0]])
        result = compute_unified_metrics(self.storage, "2026-05-14", machine_id="test-machine")
        self.assertEqual(result["active_sec_unified"], result["active_sec_p95"])

    def test_loose_geq_strict(self):
        """Mode loose >= strict toujours."""
        # Intervalles 3min puis 8min
        self._insert_event("e1", "claude_code", "2026-05-14T00:00:00Z",
                           [[1715641200, 0], [1715641380, 0], [1715641860, 0]])
        result = compute_unified_metrics(self.storage, "2026-05-14", machine_id="test-machine")
        self.assertGreaterEqual(result["active_sec_loose"], result["active_sec_strict"])

    def test_persistence_roundtrip(self):
        """upsert + get -> roundtrip correct."""
        self._insert_event("e1", "claude_code", "2026-05-14T00:00:00Z",
                           [[1715641200, 0], [1715641260, 0]])
        metrics = compute_unified_metrics(self.storage, "2026-05-14", machine_id="test-machine")
        upsert_daily_unified(self.storage, metrics)

        loaded = get_daily_unified(self.storage, "2026-05-14", machine_id="test-machine")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["active_sec_strict"], metrics["active_sec_strict"])
        self.assertEqual(loaded["sample_size"], metrics["sample_size"])

    def test_upsert_idempotent(self):
        """Re-upsert sur meme (date, machine) -> update, pas de duplicate."""
        self._insert_event("e1", "claude_code", "2026-05-14T00:00:00Z",
                           [[1715641200, 0], [1715641260, 0]])
        m1 = compute_unified_metrics(self.storage, "2026-05-14", machine_id="test-machine")
        upsert_daily_unified(self.storage, m1)
        upsert_daily_unified(self.storage, m1)  # 2e fois

        with self.storage.conn() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM daily_unified").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_arithmetic_consistency(self):
        """Verification : active_sec_p95 + agent_sec_estimated = wall_clock_sec."""
        # Events espaces : 0s, 60s, 120s, 5400s (90min de pause), 5460s
        self._insert_event("e1", "claude_code", "2026-05-14T00:00:00Z",
                           [[1715641200, 0], [1715641260, 0], [1715641320, 0],
                            [1715646720, 0], [1715646780, 0]])
        result = compute_unified_metrics(self.storage, "2026-05-14", machine_id="test-machine")
        self.assertEqual(
            result["active_sec_p95"] + result["agent_sec_estimated"],
            result["wall_clock_sec"],
        )


if __name__ == "__main__":
    unittest.main()
