"""Union d'intervalles pour le calcul du vrai temps humain actif.

Probleme resolu : sommer naivement `duration_sec` des events compte plusieurs
fois les periodes ou plusieurs sources sont actives en parallele (ex : Claude
Code CLI + Codex.app macOS ouverts en meme temps). L'humain ne travaille
qu'une fois mais chaque source compte sa duree de son cote.

Solution : collecter les intervalles [start, end] de tous les events
qualifies, les trier, fusionner ceux qui se chevauchent, sommer les durees
fusionnees. Resultat = vrai temps humain sans double-compte cross-sources.

Analogue Python du helper TypeScript `unionDurationSec` du dashboard
(vantacrew-cartography/src/modules/ai-tracking/lib/interval-union.ts).
"""

from __future__ import annotations

from datetime import datetime


Interval = tuple[float, float]  # (start_epoch, end_epoch)


def _parse_iso_to_epoch(value: str) -> float | None:
    """Parse un timestamp ISO 8601 en epoch seconds. Retourne None si invalide."""
    if not value:
        return None
    try:
        # Supporte "2026-04-23T10:42:00+02:00", "...Z", "...+00:00" avec ou sans ms
        s = value.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Fusionne les intervalles qui se chevauchent ou se touchent.

    Complexite : O(n log n) (tri + une passe).

    Exemple :
      Input  : [(09:00, 13:30), (10:00, 14:00), (15:00, 19:00), (16:00, 20:00)]
      Output : [(09:00, 14:00), (15:00, 20:00)]  # 2 periodes disjointes
    """
    valid = [(s, e) for s, e in intervals if e > s]
    if not valid:
        return []
    valid.sort(key=lambda x: x[0])
    merged: list[Interval] = [valid[0]]
    for start, end in valid[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def union_duration_sec(intervals: list[Interval]) -> int:
    """Retourne la duree totale (en secondes entieres) de l'union des intervalles."""
    return int(sum(end - start for start, end in merge_intervals(intervals)))


def union_active_sec_from_events(
    events: list[dict],
    start_key: str = "started_at",
    duration_key: str = "duration_sec",
) -> int:
    """Calcule active_sec via union des intervalles [start, start+duration].

    Filtre les events sans timestamp valide ou duration <= 0. Ignore la source,
    donc la fusion deduplique aussi les overlaps cross-sources (Claude Code +
    Codex.app simultanes ne sont comptes qu'une fois).

    :param events: liste d'events (dict-like avec cles `start_key` et
        `duration_key`). Accepte aussi `sqlite3.Row`.
    """
    intervals: list[Interval] = []
    for e in events:
        # Compat dict et sqlite3.Row
        start_raw = e[start_key] if start_key in (e.keys() if hasattr(e, "keys") else e) else None
        dur = e[duration_key] if duration_key in (e.keys() if hasattr(e, "keys") else e) else None
        if not start_raw or not dur or dur <= 0:
            continue
        start = _parse_iso_to_epoch(start_raw)
        if start is None:
            continue
        intervals.append((start, start + float(dur)))
    return union_duration_sec(intervals)
