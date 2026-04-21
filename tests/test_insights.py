"""Tests unitaires pour le module insights.

Utilisent une DB SQLite en memoire pour isolement complet.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Permet d'importer le module depuis la racine du projet
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from ship1000x.core.storage import Storage
from ship1000x.insights.engine import (
    Window,
    compute_overview,
    get_active_sec_by_day,
    get_consecutive_active_days,
    get_night_active_pct,
    get_sessions_long,
)
from ship1000x.insights.multiplier import compute_multiplier
from ship1000x.insights.signals import (
    compute_all_signals,
    detect_blocages,
    detect_burnout,
)


def _make_storage() -> Storage:
    """Cree un storage SQLite en tempfile + init schema."""
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    s = Storage(Path(tmp.name))
    s.init_schema()
    return s


def _insert_event(storage, **kwargs):
    """Helper pour inserer un event test avec defaults sains."""
    import uuid
    defaults = {
        "id": str(uuid.uuid4()),
        "source": "claude_code",
        "event_type": "session_day",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": None,
        "duration_sec": 3600,
        "cwd": "~/project",
        "project_id": "test",
        "project_conf": 0.95,
        "tool_or_action": "session_day",
        "token_input": 1000,
        "token_output": 500,
        "cost_estimated": 0.05,
        "confidence_flag": "high",
        "user_msg_type": None,
        "wordcount": 100,
        "payload_hash": None,
        "raw_meta": json.dumps({
            "user_msg_counts": {
                "typed": 10, "approval": 5, "tool_result": 80, "system": 0, "paste": 2,
            },
            "assistant_turns": 95,
            "session_id": "test-session",
        }),
    }
    defaults.update(kwargs)
    storage.upsert_event(defaults, replace=True)


class TestEngineOverview(unittest.TestCase):

    def test_empty_storage_zeros(self):
        s = _make_storage()
        w = Window(
            since=datetime.now(timezone.utc) - timedelta(days=30),
            until=datetime.now(timezone.utc),
        )
        ov = compute_overview(s, w)
        self.assertEqual(ov["totals"]["active_sec"], 0)
        self.assertEqual(ov["totals"]["commits"], 0)
        self.assertIsNone(ov["ratios"]["lines_per_hour"])

    def test_single_event_ratios(self):
        s = _make_storage()
        ts = datetime.now(timezone.utc) - timedelta(hours=2)
        _insert_event(s, started_at=ts.isoformat(), duration_sec=3600)
        # Ajout d'un event git avec raw_meta
        _insert_event(
            s,
            source="git",
            event_type="commit",
            started_at=ts.isoformat(),
            duration_sec=0,
            raw_meta=json.dumps({"lines_added": 500, "lines_deleted": 10, "files_changed": 3}),
            project_id="test",
        )
        w = Window(
            since=datetime.now(timezone.utc) - timedelta(days=1),
            until=datetime.now(timezone.utc),
        )
        ov = compute_overview(s, w)
        self.assertEqual(ov["totals"]["active_sec"], 3600)
        self.assertEqual(ov["totals"]["typed"], 10)
        self.assertEqual(ov["totals"]["commits"], 1)
        self.assertEqual(ov["totals"]["lines_added"], 500)
        # Ratios
        self.assertAlmostEqual(ov["ratios"]["lines_per_hour"], 500, places=0)
        self.assertAlmostEqual(ov["ratios"]["typed_per_hour"], 10, places=0)
        self.assertAlmostEqual(ov["ratios"]["lines_per_typed"], 50, places=0)
        self.assertAlmostEqual(ov["ratios"]["tool_per_typed"], 8.0, places=1)


class TestMultiplier(unittest.TestCase):

    def test_factor_vs_senior(self):
        s = _make_storage()
        ts = datetime.now(timezone.utc) - timedelta(hours=1)
        # 1h active avec 1000 lignes ajoutees = 1000 lignes/h
        _insert_event(s, started_at=ts.isoformat(), duration_sec=3600)
        _insert_event(
            s,
            source="git",
            event_type="commit",
            started_at=ts.isoformat(),
            duration_sec=0,
            raw_meta=json.dumps({"lines_added": 1000, "lines_deleted": 0, "files_changed": 5}),
            project_id="test",
        )
        w = Window(
            since=datetime.now(timezone.utc) - timedelta(days=1),
            until=datetime.now(timezone.utc),
        )
        m = compute_multiplier(s, w, tjm_eur_per_day=1000)
        self.assertAlmostEqual(m["output"]["lines_per_hour"], 1000, places=0)
        # Benchmark senior : 20-50 l/h
        # Facteur = 1000/50 = x20 (low) à 1000/20 = x50 (high)
        self.assertAlmostEqual(m["output"]["factor_vs_senior_low"], 20, places=0)
        self.assertAlmostEqual(m["output"]["factor_vs_senior_high"], 50, places=0)
        # TJM equivalent : 1h / 8h * 1000 EUR = 125 EUR
        self.assertAlmostEqual(m["value"]["tjm_equivalent_eur"], 125, places=0)


class TestSignalsBurnout(unittest.TestCase):

    def test_long_session_warning(self):
        """Une session > 10h doit soulever un signal burnout warning."""
        s = _make_storage()
        # 3 sessions de 11h chacune
        now = datetime.now(timezone.utc)
        for i in range(3):
            ts = now - timedelta(days=i + 1)
            _insert_event(
                s,
                started_at=ts.isoformat(),
                duration_sec=11 * 3600,
                event_type="session_day",
            )
        w = Window(since=now - timedelta(days=10), until=now)
        signals = detect_burnout(s, w)
        # Doit detecter long_sessions
        types = [sig["type"] for sig in signals]
        self.assertIn("long_sessions", types)

    def test_no_burnout_on_short_sessions(self):
        """Sessions < 10h ne doivent pas declencher de burnout."""
        s = _make_storage()
        now = datetime.now(timezone.utc)
        for i in range(5):
            ts = now - timedelta(days=i + 1)
            _insert_event(
                s,
                started_at=ts.isoformat(),
                duration_sec=4 * 3600,
                event_type="session_day",
            )
        w = Window(since=now - timedelta(days=10), until=now)
        signals = detect_burnout(s, w)
        types = [sig["type"] for sig in signals]
        self.assertNotIn("long_sessions", types)

    def test_consecutive_days(self):
        """10 jours consecutifs d'activite -> alerte."""
        s = _make_storage()
        now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        for i in range(10):
            ts = now - timedelta(days=i)
            _insert_event(
                s,
                started_at=ts.isoformat(),
                duration_sec=2 * 3600,
                event_type="session_day",
            )
        w = Window(since=now - timedelta(days=15), until=now + timedelta(hours=1))
        signals = detect_burnout(s, w)
        types = [sig["type"] for sig in signals]
        self.assertIn("no_rest", types)


class TestSignalsBlocages(unittest.TestCase):

    def test_prompts_without_commits(self):
        """Jour avec 25 typed et 0 commits doit lever info blocage."""
        s = _make_storage()
        now = datetime.now(timezone.utc)
        ts = now - timedelta(days=1, hours=12)
        _insert_event(
            s,
            started_at=ts.isoformat(),
            raw_meta=json.dumps({
                "user_msg_counts": {"typed": 25, "approval": 0, "tool_result": 100, "system": 0, "paste": 0},
                "assistant_turns": 50,
            }),
        )
        # Pas d'event git ce jour → blocage
        w = Window(since=now - timedelta(days=5), until=now)
        signals = detect_blocages(s, w)
        types = [sig["type"] for sig in signals]
        self.assertIn("prompts_without_commits", types)


class TestEngineHelpers(unittest.TestCase):

    def test_active_sec_by_day(self):
        s = _make_storage()
        now = datetime.now(timezone.utc)
        _insert_event(s, started_at=now.isoformat(), duration_sec=1800)
        _insert_event(s, started_at=(now - timedelta(days=2)).isoformat(), duration_sec=3600)
        w = Window(since=now - timedelta(days=10), until=now + timedelta(hours=1))
        by_day = get_active_sec_by_day(s, w)
        self.assertEqual(len(by_day), 2)
        self.assertIn(now.strftime("%Y-%m-%d"), by_day)

    def test_consecutive_active_days(self):
        s = _make_storage()
        now = datetime.now(timezone.utc).replace(hour=12)
        for i in range(5):
            ts = now - timedelta(days=i)
            _insert_event(s, started_at=ts.isoformat(), duration_sec=3600)
        w = Window(since=now - timedelta(days=10), until=now + timedelta(hours=1))
        streak = get_consecutive_active_days(s, w)
        self.assertEqual(streak, 5)


if __name__ == "__main__":
    unittest.main()
