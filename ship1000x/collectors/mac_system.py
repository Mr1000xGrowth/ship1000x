"""Collector macOS system — pmset wake/sleep + log show active window.

2 signaux complementaires :
1. `pmset -g log` : historique des wake/sleep du Mac. Utile pour calculer
   le temps total "Mac allume" et le cross-checker avec le temps Claude
   Code actif.
2. `log show --predicate 'process == "Claude"'` : (macOS 10.15+) montre
   les events du process Claude. On peut en deduire si la fenetre etait
   au premier plan vs arriere-plan.

Les 2 sources sont couteuses en temps d'execution (peuvent prendre
plusieurs secondes chacune). Le collector les run uniquement quand
explicitement active (pas par defaut) et avec un rate-limit.

Requiert Full Disk Access pour `log show` sur certaines versions macOS.
Le collector verifie avant de crier gracieusement.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def check_permissions() -> dict[str, Any]:
    """Verifie si pmset et log show sont disponibles + donnent de la data."""
    res = {"pmset_available": False, "log_show_available": False, "errors": []}

    # Test pmset
    try:
        out = subprocess.run(
            ["pmset", "-g", "log"],
            capture_output=True, text=True, timeout=10,
        )
        res["pmset_available"] = out.returncode == 0 and len(out.stdout) > 100
    except (FileNotFoundError, subprocess.TimeoutExpired):
        res["errors"].append("pmset introuvable ou timeout")

    # Test log show (peut demander Full Disk Access)
    try:
        out = subprocess.run(
            ["log", "show", "--last", "1m", "--style", "compact"],
            capture_output=True, text=True, timeout=15,
        )
        # Si Full Disk Access manque, log show renvoie rien ou 0 lignes exploitables
        res["log_show_available"] = out.returncode == 0 and len(out.stdout) > 50
        if out.returncode != 0:
            res["errors"].append(f"log show exit {out.returncode}: {out.stderr[:200]}")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        res["errors"].append(f"log show : {e}")

    return res


_PMSET_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+([-+]\d{4})\s+(\w+)\s+(\w+)"
)


def parse_pmset_log(output: str, since: datetime) -> list[dict[str, Any]]:
    """Parse `pmset -g log` pour extraire wake/sleep events.

    Format typique :
        2026-04-19 10:30:00 +0200 Wake       Sleep prevented by ...
        2026-04-19 12:45:00 +0200 Sleep      Entering Sleep state ...
    """
    events = []
    for line in output.splitlines():
        m = _PMSET_RE.match(line)
        if not m:
            continue
        ts_str, tz_str, event_type, _rest = m.groups()
        if event_type not in ("Wake", "Sleep", "DarkWake"):
            continue
        try:
            # Parse "+0200" → UTC offset
            sign = 1 if tz_str[0] == "+" else -1
            hours = int(tz_str[1:3])
            minutes = int(tz_str[3:5])
            tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        except ValueError:
            continue
        if dt < since:
            continue
        events.append({
            "timestamp": dt.astimezone(timezone.utc).isoformat(),
            "event_type": event_type,
        })
    return events


def collect_pmset(storage, since_days: int = 30) -> dict[str, int]:
    """Collecte les wake/sleep events des N derniers jours."""
    from ship1000x.core.privacy import sanitize_event
    import hashlib

    stats = {"wake_events": 0, "sleep_events": 0}
    try:
        out = subprocess.run(
            ["pmset", "-g", "log"],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return stats

    if out.returncode != 0:
        return stats

    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    events = parse_pmset_log(out.stdout, since)

    for e in events:
        key_type = "mac_wake" if e["event_type"] in ("Wake", "DarkWake") else "mac_sleep"
        event_id = hashlib.sha256(
            f"pmset|{e['timestamp']}|{key_type}".encode()
        ).hexdigest()[:24]
        event = {
            "id": event_id,
            "source": "mac_system",
            "event_type": key_type,
            "started_at": e["timestamp"],
            "ended_at": e["timestamp"],
            "duration_sec": 0,
            "cwd": None,
            "project_id": None,
            "project_conf": 0.0,
            "tool_or_action": e["event_type"].lower(),
            "token_input": 0,
            "token_output": 0,
            "cost_estimated": 0.0,
            "user_msg_type": None,
            "wordcount": 0,
            "confidence_flag": "high",
            "raw_meta": None,
        }
        safe = sanitize_event(event)
        storage.upsert_event(safe)
        if key_type == "mac_wake":
            stats["wake_events"] += 1
        else:
            stats["sleep_events"] += 1

    return stats


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Point d'entree : collecte pmset. `log show` non actif par defaut (cher)."""
    stats = {"files_seen": 0, "events_ingested": 0}
    perms = check_permissions()
    if not perms["pmset_available"]:
        return stats

    stats["files_seen"] = 1
    pmset_stats = collect_pmset(storage, since_days=30)
    stats["events_ingested"] = pmset_stats["wake_events"] + pmset_stats["sleep_events"]
    return stats
