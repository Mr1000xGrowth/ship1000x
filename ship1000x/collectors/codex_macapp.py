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
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ship1000x.core.usage import (
    build_unknown_usage_metadata,
    confidence_flag_from_usage,
)

CODEX_LOGS_DIR = Path.home() / "Library" / "Logs" / "com.openai.codex"
# Codex Desktop logs SQLite store. Holds OTEL spans tagged with the
# real coding `thread.id` + `model=<id>` per turn, which is what we
# join the MacApp `conversationId` against to back-fill the coding
# model on MacApp events.
CODEX_LOGS_DB = Path.home() / ".codex" / "logs_2.sqlite"

# Meme regle que claude_code.py / codex_desktop.py
ACTIVE_PAUSE_THRESHOLD_SEC = 5 * 60
SEGMENT_GAP_SEC = 30 * 60
import os as _os_max  # noqa
# Cap per session : protects against 'app left open' aberrations.
# Override via env var SHIP1000X_MAX_SESSION_HOURS for power users
# who genuinely run intensive multi-session days (>16h is rare but possible).
MAX_ACTIVE_SEC_PER_SESSION = int(_os_max.environ.get('SHIP1000X_MAX_SESSION_HOURS', '16')) * 3600

# Parse timestamp ISO 8601 en debut de ligne : "2026-04-19T00:12:17.023Z"
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)")

# Extrait les cwd=<path> ou cwd="<path>" des lignes de log
_CWD_RE = re.compile(r'cwd=(?:"([^"]+)"|([^\s,}]+))')

# Detecte les event types qui marquent une activite user
_TURN_START_RE = re.compile(r"method=turn/start")
_TURN_COMPLETE_RE = re.compile(r"kind=turn-complete")

# Extract the `conversationId=<UUID>` from `method=turn/start` lines.
# Codex MacApp tags every turn with the conversationId that matches the
# `thread.id` recorded by `logs_2.sqlite` for the same coding turn, so
# we use it to join across sources and back-fill the real coding model.
_CONVERSATION_ID_RE = re.compile(
    r"conversationId=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)

# Match `thread.id=<UUID>` or `thread_id=<UUID>` in logs_2.sqlite OTEL
# span bodies. Used by the cross-source model resolver.
_THREAD_ID_RE = re.compile(
    r"thread[._]id=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)

# Regex path extraction generique (fallback si pas de cwd)
_PATH_RE = re.compile(r"/Users/[a-zA-Z0-9_-]+/[\w\-./]+")

# Extract model identifier from Codex macOS app logs. Observed shapes:
#   model=gpt-5.4-mini
#   model="gpt-5.5"
# The model id is lowercase, may contain dots and hyphens, and only
# OpenAI-family ids show up here (gpt-5*, gpt-5-codex, gpt-4o*, o3*).
# Restricted to those prefixes to avoid matching unrelated `model=`
# substrings (e.g. eval rubric labels).
_MODEL_RE = re.compile(r'\bmodel=(?:"([^"]+)"|((?:gpt-|o\d|claude-)[a-z0-9.\-]+))')

# Codex MacApp 2026+ logs only carry `model=<id>` on auxiliary sub-calls
# (e.g. `ephemeral_generation_token_usage feature=thread_title`), never
# on the coding turns themselves. Capturing the sub-model would lie about
# the cost basis — the thread-title model (typically gpt-5.4-mini) is
# ~5x cheaper than the actual coding model (gpt-5.5/gpt-5-codex). We
# skip those lines so `model_raw` stays `unknown` and the cost quality
# stays `indicative` instead of falsely upgrading to `alias` against a
# wrong rate card. The real coding model is exposed by the
# `codex_desktop` collector (`logs_2.sqlite` OTEL spans), which is the
# source of truth for per-turn model + tokens; cross-source join via
# conversationId is a separate follow-up.
_AUXILIARY_MODEL_MARKERS = (
    "ephemeral_generation_token_usage",
    "feature=thread_title",
)

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


# Codex.app logue parfois un cwd=<racine du workspace> au lancement de l'app
# (ex: "/Users/.../Developer") plutot qu'un cwd par conversation/projet. Ce
# cwd est TROP GENERIQUE pour classer : aucun pattern de projet ne le matche,
# donc l'inclure dans le vote pondere (cwds*3) ne fait que diluer le vrai
# signal (les paths de fichiers reels, eux precis) sans jamais l'emporter.
_WORKSPACE_ROOT_PLACEHOLDERS = {
    str(Path.home() / "Developer").rstrip("/"),
    "~/Developer",
}


def _is_workspace_root_placeholder(cwd: str) -> bool:
    """Vrai si le cwd n'est que la racine generique du workspace (aucun
    sous-dossier de projet) -- signal quasi inutile pour la classification."""
    return cwd.rstrip("/") in _WORKSPACE_ROOT_PLACEHOLDERS


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


def _resolve_thread_models(
    db_path: Path,
    thread_ids: set[str],
) -> dict[str, str]:
    """Return ``{thread_id -> dominant coding model}`` from logs_2.sqlite.

    Codex MacApp logs only carry the auxiliary `thread_title` sub-model.
    The real coding model for each turn is stored alongside the
    matching `thread.id` in `~/.codex/logs_2.sqlite` (OTEL spans). This
    helper opens that DB read-only, scans the rows that mention both a
    `thread.id` and a `model=<id>` (excluding the same auxiliary
    sub-call markers we filter in the local parser), and returns the
    dominant model per requested thread_id.

    The MacApp `conversationId` field is the same UUID as the
    `thread.id` recorded by Codex Desktop for the corresponding coding
    session — verified on the real store before this helper was
    written. Threads not found in logs_2.sqlite are simply absent from
    the result; callers should treat that as "unknown model".
    """
    if not thread_ids or not db_path.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = con.execute(
            """
            SELECT feedback_log_body FROM logs
            WHERE feedback_log_body LIKE '%thread.id=%'
              AND feedback_log_body LIKE '%model=%'
            """
        ).fetchall()
    except sqlite3.Error:
        con.close()
        return {}
    con.close()

    counts: dict[str, Counter] = defaultdict(Counter)
    for (body,) in rows:
        if not body:
            continue
        # Skip auxiliary sub-call rows for the same reason we skip them
        # in the local MacApp parser (see `_AUXILIARY_MODEL_MARKERS`).
        if any(marker in body for marker in _AUXILIARY_MODEL_MARKERS):
            continue
        tm = _THREAD_ID_RE.search(body)
        if not tm:
            continue
        thread_id = tm.group(1)
        if thread_id not in thread_ids:
            continue
        for mm in _MODEL_RE.finditer(body):
            model = mm.group(1) or mm.group(2)
            if model:
                counts[thread_id][model.strip().lower()] += 1

    return {
        thread_id: counter.most_common(1)[0][0]
        for thread_id, counter in counts.items()
        if counter
    }


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
    # Model timestamps (ts) → model name. Lets the daily split pick the
    # dominant model per UTC day, since one app session can span days.
    model_events: list[tuple[float, str]] = []
    # conversationId timestamps → thread_id. Used to back-fill the
    # coding model from logs_2.sqlite when the local parser produced
    # no model_events for this day (the normal case post-2026 since
    # MacApp logs only carry the auxiliary thread_title sub-model).
    conversation_events: list[tuple[float, str]] = []

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
                # Capture every `model=<id>` mention with its timestamp,
                # except on auxiliary sub-call lines (see
                # `_AUXILIARY_MODEL_MARKERS` above) — those carry the
                # thread-title sub-model, not the coding model.
                if not any(marker in line for marker in _AUXILIARY_MODEL_MARKERS):
                    for m in _MODEL_RE.finditer(line):
                        name = m.group(1) or m.group(2)
                        if name:
                            model_events.append((ts, name.strip().lower()))
                # Capture conversationId from `method=turn/start` lines
                # for the cross-source join with logs_2.sqlite.
                cm = _CONVERSATION_ID_RE.search(line)
                if cm:
                    conversation_events.append((ts, cm.group(1)))
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
        "model_events": model_events,
        "conversation_events": conversation_events,
    }


def _daily_split(
    parsed: dict[str, Any],
    classifier,
    *,
    thread_models: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Agrege la session par jour UTC.

    Comme codex_desktop.py, on peut avoir une session qui s'etale sur
    plusieurs jours (app lancee le matin, active jusqu'au soir). Dans
    ce cas on split les metriques par date.

    ``thread_models`` is a ``{conversationId -> dominant model}`` map
    resolved upstream from logs_2.sqlite. When a day has no local
    model_events (the normal case post-2026, see
    ``_AUXILIARY_MODEL_MARKERS``), we back-fill the dominant model
    from the conversationIds that appeared on that day.
    """
    daily: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "all_ts": [],
            "turn_ts": [],
            "first_ts": None,
            "last_ts": None,
            "model_counts": Counter(),
            "model_source": "unknown",
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

    # Bucket model mentions by day so the dominant id wins per day.
    for ts, name in parsed.get("model_events") or []:
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        d = daily[day]
        d["model_counts"][name] += 1
        d["model_source"] = "macapp_log"

    # Back-fill the coding model from the cross-source join when the
    # day has no local model_events (post-2026 the MacApp log only
    # carries the auxiliary thread_title sub-model, which we
    # deliberately ignore upstream).
    # Distinguish three failure modes so the report can tell them apart:
    #   - `logs_2_sqlite_join` — conversationId found, model resolved.
    #   - `logs_2_sqlite_expired` — conversationId present in the
    #     MacApp log but absent from logs_2.sqlite (OTEL spans rotate,
    #     typically ~15 days; the session itself was real, but we
    #     can no longer recover its model).
    #   - `unknown` — no conversationId in the MacApp log at all (rare;
    #     happens when the session never emitted a turn/start line).
    for _ts, conv_id in parsed.get("conversation_events") or []:
        day = datetime.fromtimestamp(_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        d = daily[day]
        if d["model_counts"]:
            continue
        joined_model = (thread_models or {}).get(conv_id)
        if joined_model:
            d["model_counts"][joined_model] += 1
            d["model_source"] = "logs_2_sqlite_join"
        elif d["model_source"] == "unknown":
            d["model_source"] = "logs_2_sqlite_expired"

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

    # Phase 1 — parse every new log file and buffer the parsed result.
    # We do not emit events yet because the coding model needs to be
    # joined from logs_2.sqlite in a single batch (phase 2) before the
    # per-event usage block can be built. Buffering 25 small dicts is
    # cheap (kilobytes); the alternative would be to re-open the
    # SQLite DB once per file.
    parsed_jobs: list[tuple[Path, int, dict[str, Any], list[str], list[str]]] = []
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

        # Classification project_id : priorite au cwd (plus fiable que fallback),
        # SAUF quand ce cwd n'est que la racine generique du workspace -- dans ce
        # cas il n'apporte aucune info de projet et on se rabat sur les paths reels.
        cwds = [
            c for c in parsed["cwds"]
            if not is_excluded_path(c, exclude_paths) and not _is_workspace_root_placeholder(c)
        ]
        # Meme filtre applique aux paths bruts : `_extract_paths` scanne la
        # ligne entiere avec une regex generique et capture donc AUSSI le
        # `cwd=<racine workspace>` des lignes de log internes (ex: polling
        # git status/config en arriere-plan de l'app), qui se repete des
        # dizaines/centaines de fois par fichier. Sans ce filtre, ce
        # placeholder domine numeriquement `paths` et l'auto-resolve git
        # (paths_distribution pass 3) le classe vers le repo git de
        # ~/Developer lui-meme plutot que vers le vrai projet travaille,
        # noyant le signal reel (bug observe 2026-07-01 : Sabaca a moins
        # de 5 hits reels contre 170+ hits du placeholder par fichier).
        paths = [
            p for p in parsed["paths"]
            if not is_excluded_path(p, exclude_paths) and not _is_workspace_root_placeholder(p)
        ]
        parsed_jobs.append((log_file, mtime_ns, parsed, cwds, paths))

    # Phase 2 — resolve the coding model for every conversationId we
    # saw, in one read of logs_2.sqlite. Threads not found in the DB
    # are simply absent from the map; emission below treats that as
    # "unknown model" (the conservative default of the cost honesty
    # contract).
    all_thread_ids: set[str] = set()
    for _path, _mt, parsed, _c, _p in parsed_jobs:
        for _ts, conv_id in parsed.get("conversation_events") or []:
            all_thread_ids.add(conv_id)
    thread_models = _resolve_thread_models(CODEX_LOGS_DB, all_thread_ids)

    # Phase 3 — emit one event per (session_day, project_id), with the
    # coding model back-filled from `thread_models` when available.
    for _log_file, _mtime_ns, parsed, cwds, paths in parsed_jobs:
        # Combine cwds + paths pour le classifier (cwds comptent plus)
        classification_input = cwds * 3 + paths  # weight cwds 3x
        distribution = classifier.paths_distribution(classification_input)
        if not distribution:
            primary, _conf = classifier.classify_session(paths=classification_input)
            distribution = {primary or "unclassified": 1.0}

        daily = _daily_split(parsed, classifier, thread_models=thread_models)

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
                cost_estimated = (active_sec * ratio / 3600) * 10.0
                # Pick the dominant model for this day, which can come
                # either from the local MacApp log (rare, pre-2026
                # layout) or from the cross-source join with
                # logs_2.sqlite (post-2026, the common case). The
                # `model_source` field below makes the provenance
                # explicit so the dashboard can tell them apart.
                model_counts = d.get("model_counts")
                model_raw = None
                if model_counts:
                    model_raw = model_counts.most_common(1)[0][0]
                model_source = d.get("model_source", "unknown")
                usage = build_unknown_usage_metadata(
                    provider="openai",
                    client="codex-macapp",
                    model_raw=model_raw,
                    cost_estimated=cost_estimated,
                    cost_quality="indicative",
                    cost_basis="hourly_active_time_estimate",
                )
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
                    "cost_estimated": cost_estimated,
                    "user_msg_type": None,
                    "wordcount": 0,
                    # A1-cont: flag reflects MEASUREMENT quality (no native
                    # tokens, hourly-estimate cost = indicative) → not the
                    # project attribution. Attribution stays in project_conf.
                    "confidence_flag": confidence_flag_from_usage(usage),
                    "raw_meta": json.dumps(
                        {
                            "session_uuid": parsed["session_uuid"],
                            "pid": parsed["pid"],
                            "log_file": _log_file.name,
                            "turn_count": turns,
                            "cwds_count": len(cwds),
                            "paths_sampled": len(paths),
                            "split_ratio": round(ratio, 3),
                            "model_source": model_source,
                            "usage": usage,
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
