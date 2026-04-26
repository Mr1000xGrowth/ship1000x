"""Collector Codex App native macOS — parse les logs applicatifs.

Codex.app (app native OpenAI dans /Applications/Codex.app) ecrit des logs
texte dans `~/Library/Logs/com.openai.codex/YYYY/MM/DD/`. Chaque fichier
= 1 session applicative (identifiee par UUID dans le filename).

Ces logs sont **plus riches** que ceux de `state_5.sqlite` (codex_desktop.py)
car ils contiennent les events user plus granulaires :
  - `method=turn/start` = prompt user envoye
  - `method=turn-complete` = reponse serveur recue
  - `cwd=/Users/.../projet` = directory de travail courant

On extrait :
  - 1 session = 1 fichier log = 1 UUID applicatif
  - turns = nb de `turn/start`
  - active_sec = intervalles ponderes entre `turn/start` (meme regle que
    Claude Code : <5min=100%, 5-15min=50%, 15-30min=25%)
  - cwds = tous les `cwd=...` rencontres → classification project_id
  - wall_clock_sec = last_ts - first_ts du fichier (segmente < 30 min)

Rationale (2026-04-20) : les logs SSE dans state_5.sqlite sous-echantillonnent
l'activite reelle. Les logs macOS applicatifs capturent mieux les turns
user qui sont la vraie metrique d'intention humaine.

Pas de contenu stocke (prompts, responses) — uniquement metadata quantitative.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CODEX_LOGS_DIR = Path.home() / "Library" / "Logs" / "com.openai.codex"

# Meme regle que claude_code.py / codex_desktop.py
ACTIVE_PAUSE_THRESHOLD_SEC = 5 * 60
SEGMENT_GAP_SEC = 30 * 60
MAX_ACTIVE_SEC_PER_SESSION = 12 * 3600

# Parse timestamp ISO 8601 en debut de ligne : "2026-04-19T00:12:17.023Z"
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)")

# Extrait les cwd=<path> ou cwd="<path>" des lignes de log
_CWD_RE = re.compile(r'cwd=(?:"([^"]+)"|([^\s,}]+))')

# Detecte les event types qui marquent une activite user
_TURN_START_RE = re.compile(r"method=turn/start")
_TURN_COMPLETE_RE = re.compile(r"kind=turn-complete")

# Regex path extraction generique (fallback si pas de cwd)
_PATH_RE = re.compile(r"/Users/[a-zA-Z0-9_-]+/[\w\-./]+")

# Pattern filename : codex-desktop-<UUID>-<PID>-t<X>-i<Y>-<HHMMSS>-<N>.log
_FILENAME_RE = re.compile(
    r"^codex-desktop-([0-9a-f-]+)-(\d+)-t\d+-i\d+-\d{6}-\d+\.log$"
)


def _stable_event_id(session_uuid: str, day_key: str, project_id: str) -> str:
    raw = f"codex_macapp|{session_uuid}|{day_key}|{project_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _parse_timestamp(line: str) -> float | None:
    """Convertit 2026-04-19T00:12:17.023Z en unix timestamp float."""
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%fZ")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _extract_cwd(line: str) -> str | None:
    """Extrait le 1er cwd=... d'une ligne, si present."""
    m = _CWD_RE.search(line)
    if not m:
        return None
    return (m.group(1) or m.group(2)).strip()


def _extract_paths(line: str) -> list[str]:
    """Fallback : extrait les paths /Users/... d'une ligne."""
    if "/Users/" not in line:
        return []
    return _PATH_RE.findall(line)


def _segmented_wall_clock(timestamps: list[float]) -> int:
    """Somme des intervalles entre events < SEGMENT_GAP_SEC (30 min)."""
    if len(timestamps) < 2:
        return 0
    ts = sorted(timestamps)
    total = 0.0
    for i in range(1, len(ts)):
        delta = ts[i] - ts[i - 1]
        if 0 < delta <= SEGMENT_GAP_SEC:
            total += delta
    return int(total)


def _estimate_active_sec(turn_timestamps: list[float]) -> int:
    """Meme regle que claude_code : intervalles ponderes entre turn/start."""
    if len(turn_timestamps) < 2:
        return 0
    total = 0.0
    prev = None
    for ts in sorted(turn_timestamps):
        if prev is None:
            prev = ts
            continue
        delta = ts - prev
        if delta <= 0:
            pass
        elif delta <= ACTIVE_PAUSE_THRESHOLD_SEC:
            total += delta
        elif delta <= ACTIVE_PAUSE_THRESHOLD_SEC * 3:
            total += delta * 0.5
        elif delta <= ACTIVE_PAUSE_THRESHOLD_SEC * 6:
            total += delta * 0.25
        prev = ts
    return int(total)


def _parse_log_file(path: Path) -> dict[str, Any]:
    """Parse un fichier log Codex.app et renvoie ses metriques.

    Return : {
        "session_uuid": str,
        "pid": str,
        "all_timestamps": list[float],  # tous les ts (pour wall-clock)
        "turn_timestamps": list[float], # ts des turn/start uniquement
        "cwds": list[str],              # cwds extraits
        "paths": list[str],             # paths /Users/... extraits (fallback)
        "first_ts": float | None,
        "last_ts": float | None,
    }
    """
    filename_match = _FILENAME_RE.match(path.name)
    if not filename_match:
        return {}
    session_uuid = filename_match.group(1)
    pid = filename_match.group(2)

    all_ts: list[float] = []
    turn_ts: list[float] = []
    cwds: list[str] = []
    paths: list[str] = []

    try:
        # Les logs peuvent etre gros : lecture ligne par ligne
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                ts = _parse_timestamp(line)
                if ts is None:
                    continue
                all_ts.append(ts)
                if _TURN_START_RE.search(line):
                    turn_ts.append(ts)
                cwd = _extract_cwd(line)
                if cwd:
                    # Filtre les pseudo-cwd non projet (ex: .git sub-paths)
                    # On garde la racine parent pour classification
                    cwd_clean = cwd.rstrip("/")
                    if cwd_clean.endswith("/.git"):
                        cwd_clean = cwd_clean[:-5]
                    if cwd_clean and cwd_clean not in cwds:
                        cwds.append(cwd_clean)
                # Paths absolus en fallback (peu frequent dans ces logs)
                paths.extend(_extract_paths(line))
    except OSError:
        return {}

    return {
        "session_uuid": session_uuid,
        "pid": pid,
        "all_timestamps": all_ts,
        "turn_timestamps": turn_ts,
        "cwds": cwds,
        "paths": paths[:200],  # cap pour eviter memoire
        "first_ts": all_ts[0] if all_ts else None,
        "last_ts": all_ts[-1] if all_ts else None,
    }


def _daily_split(
    parsed: dict[str, Any],
    classifier,
) -> dict[str, dict[str, Any]]:
    """Agrege la session par jour UTC.

    Comme codex_desktop.py, on peut avoir une session qui s'etale sur
    plusieurs jours (app lancee le matin, active jusqu'au soir). Dans
    ce cas on split les metriques par date.
    """
    from collections import defaultdict

    daily: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "all_ts": [],
            "turn_ts": [],
            "first_ts": None,
            "last_ts": None,
        }
    )

    # Index les turn timestamps par jour
    turn_set = set(parsed["turn_timestamps"])
    for ts in parsed["all_timestamps"]:
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        d = daily[day]
        if d["first_ts"] is None:
            d["first_ts"] = ts
        d["last_ts"] = ts
        d["all_ts"].append(ts)
        if ts in turn_set:
            d["turn_ts"].append(ts)

    return daily


def collect(
    storage, classifier, privacy_config: dict[str, Any]
) -> dict[str, int]:
    """Ingestion Codex.app macOS logs.

    Idempotent via mtime : ne re-traite que les fichiers modifies depuis
    le dernier passage.
    """
    from ship1000x.core.privacy import is_excluded_path, sanitize_event

    stats = {
        "files_seen": 0,
        "files_parsed": 0,
        "sessions_ingested": 0,
        "events_ingested": 0,
        "skipped": 0,
    }

    if not CODEX_LOGS_DIR.exists():
        return stats

    exclude_paths = privacy_config.get("exclude_paths", []) or []

    # Offset : plus grand mtime deja traite (int nanoseconds)
    last_mtime_ns = storage.get_ingestion_offset("codex_macapp", "mtime_max_ns")
    max_mtime_ns = last_mtime_ns

    # Scan recursif YYYY/MM/DD/*.log
    for log_file in sorted(CODEX_LOGS_DIR.rglob("*.log")):
        stats["files_seen"] += 1
        try:
            mtime_ns = log_file.stat().st_mtime_ns
        except OSError:
            continue
        if mtime_ns <= last_mtime_ns:
            stats["skipped"] += 1
            continue
        if mtime_ns > max_mtime_ns:
            max_mtime_ns = mtime_ns

        parsed = _parse_log_file(log_file)
        if not parsed or not parsed.get("turn_timestamps"):
            # Pas d'activite user dans ce fichier (log d'update Sparkle etc.)
            continue
        stats["files_parsed"] += 1

        # Classification project_id : priorite au cwd (plus fiable que fallback)
        cwds = [c for c in parsed["cwds"] if not is_excluded_path(c, exclude_paths)]
        paths = [p for p in parsed["paths"] if not is_excluded_path(p, exclude_paths)]

        # Combine cwds + paths pour le classifier (cwds comptent plus)
        classification_input = cwds * 3 + paths  # weight cwds 3x
        distribution = classifier.paths_distribution(classification_input)
        if not distribution:
            primary, _conf = classifier.classify_session(paths=classification_input)
            distribution = {primary or "unclassified": 1.0}

        daily = _daily_split(parsed, classifier)

        for day_key, d in daily.items():
            if not d["first_ts"] or not d["turn_ts"]:
                continue

            turns = len(d["turn_ts"])
            active_sec = _estimate_active_sec(d["turn_ts"])
            wall_sec = _segmented_wall_clock(d["all_ts"])

            # Floor par turns : chaque turn = min 60s (prep + read response).
            # Les logs macOS Codex.app sont plus fiables que SSE car ils
            # capturent vraiment 1 event par prompt user.
            turns_floor = turns * 60

            # NOTE : floor wall-clock 60% retire pour aligner avec claude_code.
            # Avant, "Codex.app ouvert au foreground 9h avec 3 prompts"
            # reportait 5.4h actif (60% x 9h) meme sans usage reel. Asymetrie
            # avec Claude Code (pas de floor wall-clock) qui faisait un ratio
            # Codex/Claude artificiel ~4x.
            # Si besoin de re-activer pour debugging, decommenter le bloc.
            #
            # if turns >= 3 and wall_sec > 0:
            #     wall_floor = int(wall_sec * 0.60)
            #     active_sec = max(active_sec, wall_floor)

            active_sec = max(active_sec, turns_floor)
            if wall_sec > 0:
                active_sec = min(active_sec, wall_sec)
            active_sec = min(active_sec, MAX_ACTIVE_SEC_PER_SESSION)

            started_iso = datetime.fromtimestamp(
                d["first_ts"], tz=timezone.utc
            ).isoformat()
            ended_iso = datetime.fromtimestamp(
                d["last_ts"], tz=timezone.utc
            ).isoformat()

            for project_id, ratio in distribution.items():
                confidence = 0.90 if cwds else (0.60 if paths else 0.40)
                event = {
                    "id": _stable_event_id(
                        parsed["session_uuid"], day_key, project_id
                    ),
                    "source": "codex_macapp",
                    "event_type": "session_day",
                    "started_at": started_iso,
                    "ended_at": ended_iso,
                    "duration_sec": int(active_sec * ratio),
                    "wall_clock_sec": int(wall_sec * ratio),
                    "cwd": cwds[0] if cwds else None,
                    "project_id": project_id,
                    "project_conf": confidence,
                    "tool_or_action": "codex_macapp_session",
                    "token_input": 0,
                    "token_output": 0,
                    # Codex.app macOS : les logs applicatifs (format texte)
                    # ne contiennent PAS les tokens. On estime le cout par
                    # heure active base sur la moyenne Codex CLI (GPT-5 API
                    # : ~10$/h d'usage intensif). L'estimation est conservative
                    # et coherente avec le cost Claude Code calcule aussi
                    # en equivalent API.
                    "cost_estimated": (active_sec * ratio / 3600) * 10.0,
                    "user_msg_type": None,
                    "wordcount": 0,
                    "confidence_flag": (
                        "high" if cwds and ratio >= 0.5 else "medium"
                    ),
                    "raw_meta": json.dumps(
                        {
                            "session_uuid": parsed["session_uuid"],
                            "pid": parsed["pid"],
                            "log_file": log_file.name,
                            "turn_count": turns,
                            "cwds_count": len(cwds),
                            "paths_sampled": len(paths),
                            "split_ratio": round(ratio, 3),
                        }
                    ),
                }
                safe = sanitize_event(event)
                storage.upsert_event(safe, replace=True)
                stats["events_ingested"] += 1
            stats["sessions_ingested"] += 1

    if max_mtime_ns > last_mtime_ns:
        storage.set_ingestion_offset(
            "codex_macapp",
            "mtime_max_ns",
            max_mtime_ns,
            datetime.utcnow().isoformat(),
        )

    return stats
