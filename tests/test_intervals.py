"""Tests pour core/intervals.py : union d'intervalles cross-sources."""

from ship1000x.core.intervals import (
    merge_intervals,
    union_active_sec_from_events,
    union_duration_sec,
)


def test_merge_empty():
    assert merge_intervals([]) == []


def test_merge_zero_duration_discarded():
    assert merge_intervals([(100.0, 100.0)]) == []


def test_merge_disjoint_intervals_kept():
    intervals = [(100.0, 200.0), (300.0, 400.0)]
    assert merge_intervals(intervals) == [(100.0, 200.0), (300.0, 400.0)]


def test_merge_overlapping_intervals_fused():
    # Claude Code CLI actif 09:00 -> 13:30 (seconds encoded)
    # Codex.app actif en parallele 10:00 -> 14:00
    # Union : 09:00 -> 14:00
    a = (9 * 3600, 13.5 * 3600)  # 9h - 13h30
    b = (10 * 3600, 14 * 3600)   # 10h - 14h
    assert merge_intervals([a, b]) == [(9 * 3600, 14 * 3600)]


def test_merge_chained_overlaps():
    # Trois intervalles qui se chainent : A overlap B, B overlap C
    # Doit produire un seul gros intervalle
    ins = [(0, 100), (50, 150), (120, 200)]
    assert merge_intervals(ins) == [(0, 200)]


def test_merge_out_of_order_input():
    # Input non trie doit etre gere par le tri interne
    ins = [(200, 300), (0, 100), (250, 400)]
    assert merge_intervals(ins) == [(0, 100), (200, 400)]


def test_union_duration_no_overlap():
    # 2 fenetres disjointes de 100s chacune -> 200s
    assert union_duration_sec([(0, 100), (200, 300)]) == 200


def test_union_duration_full_overlap_avoided():
    # Cas concret : Claude Code 30min + Codex.app 30min sur les MEMES 30 minutes
    # Sommation naive = 60min. Union = 30min.
    thirty_min = 30 * 60
    start = 1_700_000_000
    ins = [(start, start + thirty_min), (start, start + thirty_min)]
    assert union_duration_sec(ins) == thirty_min


def test_union_partial_overlap():
    # A = 100 sec, B chevauche les 30 dernieres sec + 70 sec en plus
    # Union = 170 sec (pas 200 si on sommait naivement)
    assert union_duration_sec([(0, 100), (70, 170)]) == 170


def test_union_from_events_filters_bad_rows():
    events = [
        {"started_at": "2026-04-23T10:00:00+00:00", "duration_sec": 600},
        {"started_at": None, "duration_sec": 300},              # pas de ts
        {"started_at": "2026-04-23T10:00:00+00:00", "duration_sec": 0},  # dur 0
        {"started_at": "not-an-iso", "duration_sec": 300},     # parse fail
        {"started_at": "2026-04-23T10:05:00+00:00", "duration_sec": 600},  # chevauche le 1er
    ]
    # Event 1 : 10:00-10:10, Event 5 : 10:05-10:15, union = 10:00-10:15 = 900s
    assert union_active_sec_from_events(events) == 900


def test_union_from_events_cross_sources_scenario():
    """Scenario reel : 4 sources actives en parallele sur le meme creneau."""
    ts = "2026-04-23T14:00:00+00:00"
    # 4 collectors IA declarent chacun 30min sur le meme creneau 14:00-14:30
    events = [
        {"started_at": ts, "duration_sec": 1800, "source": "claude_code"},
        {"started_at": ts, "duration_sec": 1800, "source": "codex_macapp"},
        {"started_at": ts, "duration_sec": 1800, "source": "codex"},
        {"started_at": ts, "duration_sec": 1800, "source": "codex_desktop"},
    ]
    # Somme naive = 4 × 30min = 2h. Union = 30min.
    assert union_active_sec_from_events(events) == 1800
