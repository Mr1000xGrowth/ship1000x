"""Collector Codex Desktop — exploit `~/.codex/logs_2.sqlite` table `logs`.

Codex Desktop ne stocke PAS les conversations (c'est cote serveur OpenAI),
MAIS il log les OTEL tracing spans + SSE events dans une table `logs`
avec timestamps + process_uuid (= session).

Schema change history:
  - pre-2026-05 : table dans `state_5.sqlite`, colonne `message`.
  - 2026-05+   : table dans `logs_2.sqlite`, colonne `feedback_log_body`.
    On lit la nouvelle source et on alias la colonne en `message` au
    niveau SQL pour garder tout le reste du collector intact.

Structure logs pertinente :
  - ts (unix seconds) + ts_nanos
  - target = "codex_api::sse::responses" ou OTEL spans
  - feedback_log_body = OTEL span body avec `model=gpt-5.5`, `turn.id=...`,
    plus parfois un SSE `{"type":"response.created", ...}` embedded.
  - process_uuid = identifiant de session

On extrait :
  - 1 session = 1 process_uuid
  - turns = nb de "response.created"
  - timestamps turns = les ts des "response.created" + "response.completed"
    -> active_sec via intervalles ponderes (idem claude_code)
  - paths = extraction regex des /Users/<username>/... dans les messages
    -> classification project_id + split multi-projets
  - wall_clock_sec = last_ts - first_ts par process_uuid
  - model = extraction regex `model=<id>` la plus frequente sur la session
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ship1000x.core.pricing import estimate_openai_cost
from ship1000x.core.usage import (
    TokenBreakdown,
    build_unknown_usage_metadata,
    build_usage_metadata,
)

CODEX_STATE_DB = Path.home() / ".codex" / "logs_2.sqlite"
# Backward-compat path used by older Codex Desktop builds; kept as a
# fallback so the collector still works on machines that have not yet
# migrated to the new SQLite file.
CODEX_STATE_DB_LEGACY = Path.home() / ".codex" / "state_5.sqlite"

import os as _os_max  # noqa
# Cap per session : protects against 'app left open' aberrations.
# Override via env var SHIP1000X_MAX_SESSION_HOURS for power users
# who genuinely run intensive multi-session days (>16h is rare but possible).
MAX_ACTIVE_SEC_PER_SESSION = int(_os_max.environ.get('SHIP1000X_MAX_SESSION_HOURS', '16')) * 3600
ACTIVE_PAUSE_THRESHOLD_SEC = 5 * 60
# Gap au-dela duquel on considere que la session s'est mise en pause (l'app
# Codex Desktop reste ouverte entre les usages, les process_uuid peuvent
# durer plusieurs jours sans activite).
SEGMENT_GAP_SEC = 30 * 60

# Regex path extraction : garde les chemins absolus sous /Users/...
_PATH_RE = re.compile(r"/Users/[a-zA-Z0-9_-]+/[\w\-./]+")

# Extract model identifier from Codex Desktop OTEL spans. The body looks
# like `turn{... model=gpt-5.5 ...}:run_sampling_request{turn_id=... model=gpt-5.5 ...}`.
# The model id is always lowercase, may contain dots and hyphens
# (gpt-5.5, gpt-5-codex, gpt-5.4-mini), and is never quoted in OTEL spans.
# We also tolerate the JSON-shaped `"model":"gpt-5.5"` for backward compat
# with older SSE-only payloads.
_MODEL_OTEL_RE = re.compile(r"\bmodel=([a-z][a-z0-9.\-]+)")
_MODEL_JSON_RE = re.compile(r'"model"\s*:\s*"([a-z0-9.\-]+)"', re.IGNORECASE)

# OpenAI auth-mode signals. Codex Desktop on a ChatGPT Plus/Pro plan calls
# `chatgpt.com/backend-api/codex/...` for analytics and uses the same OAuth
# session for the responses endpoint; a direct OPENAI_API_KEY user hits
# `api.openai.com/v1/...` instead. We sniff the URL families that
# `codex_client::request` / `codex_client::default_client` emit alongside
# the rest of the session log rows.
_AUTH_OAUTH_RE = re.compile(r"chatgpt\.com/backend-api/", re.IGNORECASE)
_AUTH_API_KEY_RE = re.compile(r"api\.openai\.com/v1/", re.IGNORECASE)

# Response id lookup is whitespace-tolerant: compact production payloads
# emit `"id":"resp_..."` while pretty-printed fixtures emit `"id": "resp_..."`.
_RESPONSE_ID_RE = re.compile(r'"id"\s*:\s*"(resp_[^"\\]+)"')

# Maximum size in chars of the JSON object we will attempt to parse for the
# `usage` block of a response.completed payload. Real payloads observed in
# `logs_2.sqlite` stay well under 1 KB for the usage block itself; the cap
# bounds the brace-balance loop so a corrupted row can't make us scan an
# unbounded slice of the surrounding payload.
_USAGE_PARSE_LIMIT_CHARS = 4096


def _extract_usage_from_payload(message: str) -> dict | None:
    """Extract the OpenAI `usage` block + response id from a log row.

    Codex Desktop logs the full SSE/websocket `response.completed` event
    into `logs_2.sqlite`. The payload carries the canonical OpenAI
    `usage` object:

        "usage": {
            "input_tokens": 12545,
            "input_tokens_details": {"cached_tokens": 7680},
            "output_tokens": 81,
            "output_tokens_details": {"reasoning_tokens": 61},
            "total_tokens": 12626
        }

    We only parse the rows that actually contain `response.completed`
    (the `created` / `in_progress` variants carry no usage), and we
    balance braces ourselves rather than running a regex on the JSON —
    the surrounding payload contains instruction strings with embedded
    braces that would defeat any non-balanced match.

    Returns a dict with `input_tokens`, `cached_tokens`, `output_tokens`,
    `reasoning_tokens`, `total_tokens` and `response_id`, or None when
    no usable usage block is present in the row.
    """
    if not message or "response.completed" not in message:
        return None
    idx = message.find('"usage":')
    if idx < 0:
        return None
    brace_idx = message.find("{", idx)
    if brace_idx < 0:
        return None
    end = -1
    depth = 0
    limit = min(len(message), brace_idx + _USAGE_PARSE_LIMIT_CHARS)
    for i in range(brace_idx, limit):
        c = message[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    try:
        usage = json.loads(message[brace_idx:end])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(usage, dict) or "input_tokens" not in usage:
        return None
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    cached = input_details.get("cached_tokens", 0) if isinstance(input_details, dict) else 0
    reasoning = output_details.get("reasoning_tokens", 0) if isinstance(output_details, dict) else 0
    # Response id lets us dedup across log rows when the same SSE event
    # is recorded by multiple targets (websocket transport + analytics
    # mirror). The id is opaque (`resp_<hex>`), safe to keep.
    response_id = None
    rid_match = _RESPONSE_ID_RE.search(message)
    if rid_match:
        response_id = rid_match.group(1)
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cached_tokens": int(cached or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "reasoning_tokens": int(reasoning or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "response_id": response_id,
    }


def _detect_auth_mode(messages: list[str]) -> str:
    """Detect Codex Desktop auth mode from URL signals in the session log.

    Codex Desktop on ChatGPT Plus / Pro / Team hits
    ``chatgpt.com/backend-api/codex/...``. A direct ``OPENAI_API_KEY`` user
    hits ``api.openai.com/v1/...`` instead. We return as soon as one
    family is observed; absence of both stays ``unknown`` so the cost
    honesty contract leaves billed_estimated at zero by default.
    """
    saw_oauth = False
    saw_api_key = False
    for msg in messages:
        if not msg:
            continue
        if not saw_oauth and _AUTH_OAUTH_RE.search(msg):
            saw_oauth = True
        if not saw_api_key and _AUTH_API_KEY_RE.search(msg):
            saw_api_key = True
        if saw_oauth and saw_api_key:
            break
    if saw_oauth and not saw_api_key:
        return "oauth"
    if saw_api_key and not saw_oauth:
        return "api_key"
    return "unknown"


def _extract_project_markers(message: str, markers: list[str]) -> list[str]:
    """Cherche les markers distinctifs (ids + segments projets) dans un message.

    Utile quand les paths sont fragmentes en deltas (Codex Desktop SSE events
    "response.function_call_arguments.delta"). Une mention d'un id projet
    (ex: "my-backend" ou "my-app") suffit a classifier l'event.
    """
    if not message or not markers:
        return []
    msg_lower = message.lower()
    found = []
    for m in markers:
        if len(m) >= 5 and m.lower() in msg_lower:
            # On conserve le marker matche pour pouvoir le passer au classifier
            found.append(m)
    return found


def _stable_event_id(process_uuid: str, day_key: str, project_id: str) -> str:
    raw = f"codex_desktop|{process_uuid}|{day_key}|{project_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _segmented_wall_clock(timestamps: list[int]) -> int:
    """Somme des intervalles entre events < SEGMENT_GAP_SEC (30 min).

    Quand Codex Desktop reste ouvert plusieurs jours sans activite, le wall
    brut (last-first) est trompeur. On ne compte que les intervalles "vivants".
    """
    if len(timestamps) < 2:
        return 0
    ts = sorted(timestamps)
    total = 0
    for i in range(1, len(ts)):
        delta = ts[i] - ts[i - 1]
        if 0 < delta <= SEGMENT_GAP_SEC:
            total += delta
    return total


def _estimate_active_sec(user_timestamps: list[int]) -> int:
    """Meme regle que claude_code : intervalles ponderes."""
    if len(user_timestamps) < 2:
        return 0
    total = 0.0
    prev = None
    for ts in sorted(user_timestamps):
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


def _extract_paths(message: str) -> list[str]:
    if not message or "/Users/" not in message:
        return []
    # Cap la longueur pour eviter les regex qui explosent sur de gros dumps JSON
    clipped = message[:10000]
    return [m.group(0) for m in _PATH_RE.finditer(clipped)]


def _daily_split(process_logs: list[dict], classifier, markers: list[str] | None = None) -> dict[str, dict]:
    """Agrege les logs d'une session par jour UTC.

    On collecte TOUS les timestamps des events SSE (pas juste response.created)
    pour que active_sec reflete bien l'activite continue. Un intervalle court
    entre events = utilisateur en train d'interagir avec l'app.
    """
    from collections import Counter, defaultdict
    daily: dict[str, dict] = defaultdict(lambda: {
        "turns": 0,
        "tool_calls": 0,
        "paths": [],
        "first_ts": None,
        "last_ts": None,
        "event_timestamps": [],
        # Per-day model occurrence counter. Codex Desktop OTEL spans tag
        # every run with `model=<id>`. We keep the dominant model per day
        # so the downstream usage block (build_unknown_usage_metadata)
        # surfaces a real model name instead of None.
        "model_counts": Counter(),
        # Aggregated real-token counters extracted from `response.completed`
        # SSE/websocket payloads. Stays at zero on days where no usage
        # block is exposed (e.g. very old session_uuid kept alive across
        # idle days), which trips the fallback to the hourly cost basis
        # in collect().
        "tokens": {
            "input": 0,
            "cached": 0,
            "output": 0,
            "reasoning": 0,
            "total": 0,
        },
        "response_count": 0,
        # Response ids deduped across log targets (websocket transport +
        # analytics mirror occasionally log the same `response.completed`
        # event twice; we count it once).
        "response_ids": set(),
    })

    for row in process_logs:
        ts = row["ts"]
        msg = row["message"] or ""
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        d = daily[day]
        if d["first_ts"] is None:
            d["first_ts"] = ts
        d["last_ts"] = ts

        # Tous les events significatifs participent au calcul d'active_sec
        # (response.created/completed/in_progress, function_call.*, etc.)
        d["event_timestamps"].append(ts)

        if "response.created" in msg:
            d["turns"] += 1
        if "function_call_arguments.done" in msg or "function_call.done" in msg:
            d["tool_calls"] += 1

        # Capture every model mention so the most-frequent one wins per
        # day. OTEL spans repeat `model=<id>` many times per turn; JSON
        # SSE payloads carry `"model":"<id>"` once per response.
        for m in _MODEL_OTEL_RE.findall(msg):
            d["model_counts"][m] += 1
        for m in _MODEL_JSON_RE.findall(msg):
            d["model_counts"][m] += 1

        usage_block = _extract_usage_from_payload(msg)
        if usage_block:
            response_id = usage_block.get("response_id")
            if not response_id or response_id not in d["response_ids"]:
                if response_id:
                    d["response_ids"].add(response_id)
                d["tokens"]["input"] += usage_block["input_tokens"]
                d["tokens"]["cached"] += usage_block["cached_tokens"]
                d["tokens"]["output"] += usage_block["output_tokens"]
                d["tokens"]["reasoning"] += usage_block["reasoning_tokens"]
                d["tokens"]["total"] += usage_block["total_tokens"]
                d["response_count"] += 1

        paths = _extract_paths(msg)
        if paths:
            d["paths"].extend(paths)
        # Fallback : si aucun path absolu dans le message, cherche les markers
        # projets (utile pour les SSE event deltas fragmentes).
        if not paths and markers:
            marker_hits = _extract_project_markers(msg, markers)
            d["paths"].extend(marker_hits)

    return daily


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Ingestion Codex Desktop via logs_2.sqlite (or legacy state_5.sqlite).

    Idempotent : ingestion_state offset = max(ts) deja ingere.
    """
    from ship1000x.core.privacy import is_excluded_path, sanitize_event

    stats = {"files_seen": 0, "sessions_ingested": 0, "events_ingested": 0, "skipped": 0}
    # Pick the first DB file that exists. logs_2.sqlite is the 2026-05+
    # Codex Desktop layout; state_5.sqlite is the pre-2026-05 path kept
    # here so older installations keep ingesting without intervention.
    db_path = None
    for candidate in (CODEX_STATE_DB, CODEX_STATE_DB_LEGACY):
        if candidate.exists():
            db_path = candidate
            break
    if db_path is None:
        return stats

    stats["files_seen"] = 1
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return stats

    # Verifie que la table existe
    has_logs = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='logs'"
    ).fetchone()
    if not has_logs:
        con.close()
        return stats

    # Detect which message column the schema uses so we can support both
    # the legacy (`message`) and current (`feedback_log_body`) layouts
    # without forking the rest of the code.
    cols = {row[1] for row in con.execute("PRAGMA table_info(logs)").fetchall()}
    if "feedback_log_body" in cols:
        message_column_sql = "feedback_log_body AS message"
    elif "message" in cols:
        message_column_sql = "message"
    else:
        con.close()
        return stats

    last_ts = storage.get_ingestion_offset("codex_desktop", "logs_ts_max")
    exclude_paths = privacy_config.get("exclude_paths", []) or []

    # DEDUP : le collector codex_macapp (~/Library/Logs/com.openai.codex/) est
    # plus precis (turn/start = vrais prompts user) que les logs SSE du
    # state_5.sqlite. Quand les 2 sources couvrent le meme jour ET le meme
    # projet, on skip l'event codex_desktop pour eviter le double compte.
    # Pas de linkage UUID possible entre les 2 sources (session_uuid frontend
    # vs process_uuid backend), donc on dedup au niveau (day, project).
    macapp_covered: set[tuple[str, str]] = set()
    try:
        rows_macapp = storage.query(
            """
            SELECT DISTINCT substr(started_at, 1, 10) AS day, project_id
            FROM events
            WHERE source = 'codex_macapp'
            """
        )
        for r in rows_macapp:
            macapp_covered.add((r["day"], r["project_id"]))
    except Exception:
        # Si le query echoue (schema different, etc.), on continue sans dedup
        pass

    # Construit la liste des markers de projet (ids + segments distinctifs) pour
    # detecter les mentions dans les messages Codex Desktop meme quand les paths
    # sont fragmentes (SSE function_call_arguments.delta).
    project_markers: list[str] = []
    for rule in classifier.rules:
        if rule.id not in project_markers:
            project_markers.append(rule.id)
        for pattern in rule.paths:
            for segment in pattern.split("/"):
                if segment and "*" not in segment and len(segment) >= 5:
                    if segment not in project_markers:
                        project_markers.append(segment)

    # On ne lit que les logs pertinents (SSE events avec contenu) pour limiter
    # le volume. On exclut les traces debug pure (opentelemetry, hyper_util).
    rows = con.execute(
        f"""
        SELECT ts, process_uuid, {message_column_sql}
        FROM logs
        WHERE ts > ?
          AND process_uuid IS NOT NULL
          AND (
              {message_column_sql.split(' AS ')[0]} LIKE '%response.created%'
              OR {message_column_sql.split(' AS ')[0]} LIKE '%response.completed%'
              OR {message_column_sql.split(' AS ')[0]} LIKE '%function_call%'
              OR {message_column_sql.split(' AS ')[0]} LIKE '/Users/%'
              OR {message_column_sql.split(' AS ')[0]} LIKE '%delta%'
              OR {message_column_sql.split(' AS ')[0]} LIKE '%model=%'
          )
        ORDER BY process_uuid, ts
        """,
        (last_ts,),
    ).fetchall()

    # Group par process_uuid = sessions
    from collections import defaultdict
    sessions: dict[str, list[dict]] = defaultdict(list)
    max_ts = last_ts
    for r in rows:
        sessions[r["process_uuid"]].append({"ts": r["ts"], "message": r["message"]})
        if r["ts"] > max_ts:
            max_ts = r["ts"]

    for process_uuid, logs in sessions.items():
        if not logs:
            continue
        daily = _daily_split(logs, classifier, markers=project_markers)
        # Auth mode is session-wide: Codex Desktop keeps a single client
        # session per process_uuid, so a `chatgpt.com/backend-api/codex/`
        # call anywhere in the session is a reliable signal for every
        # turn. We scan every row so a long-running session with the
        # auth URL only emitted on (say) the analytics flush still
        # resolves to `oauth` instead of staying `unknown` by accident.
        session_auth_mode = _detect_auth_mode([row["message"] or "" for row in logs])

        for day_key, d in daily.items():
            if not d["first_ts"]:
                continue

            # Filter exclude_paths
            paths_filtered = [p for p in d["paths"] if not is_excluded_path(p, exclude_paths)]

            # Classification multi-projets via paths
            distribution = classifier.paths_distribution(paths_filtered)
            if not distribution:
                primary, _conf = classifier.classify_session(paths=paths_filtered)
                distribution = {primary or "unclassified": 1.0}

            # Active sec via tous les timestamps events (densite elevee)
            active_sec = _estimate_active_sec(d["event_timestamps"])
            # Wall-clock segmente : exclut les longs gaps d'inactivite.
            # Codex Desktop reste ouvert H24, last-first peut faire 70h sans
            # activite reelle. On ne garde que les intervalles < 30 min.
            wall_sec = _segmented_wall_clock(d["event_timestamps"])

            # Floor par turns : chaque turn user = au minimum 60s de reflexion
            # reelle (preparer prompt + lire response). C'est un minimum
            # absolu, independant de la granularite des logs SSE.
            turns_floor = d["turns"] * 60

            # Floor wall-clock : les logs Codex Desktop sont echantillonnes
            # par le serveur OpenAI, beaucoup plus sparse que les JSONL
            # Claude Code. Sur une session avec >= 3 turns reels, on plancher
            # a 50% du wall_clock segmente (ratio reviewed apres feedback
            # user 2026-04-20 : 30% sous-estimait massivement).
            if d["turns"] >= 3 and wall_sec > 0:
                wall_floor = int(wall_sec * 0.50)
                active_sec = max(active_sec, wall_floor)

            # Applique le floor par turns (garantit qu'on ne rate pas
            # l'activite reelle meme si _estimate_active_sec sous-sample).
            active_sec = max(active_sec, turns_floor)

            # Cap : ne depasse jamais le wall_clock segmente (physiquement
            # impossible d'etre actif plus longtemps que la session elle-meme).
            # Ancien cap a 60% trop restrictif : un user intensif peut etre
            # actif 80-90% du wall_clock segmente.
            if wall_sec > 0:
                active_sec = min(active_sec, wall_sec)
            if active_sec > MAX_ACTIVE_SEC_PER_SESSION:
                active_sec = MAX_ACTIVE_SEC_PER_SESSION

            started_iso = datetime.fromtimestamp(d["first_ts"], tz=timezone.utc).isoformat()
            ended_iso = datetime.fromtimestamp(d["last_ts"], tz=timezone.utc).isoformat()

            for project_id, ratio in distribution.items():
                # Skip si codex_macapp couvre deja ce (day, project) — evite
                # le double compte quand Codex.app native tourne sur la meme
                # machine (cf. bloc DEDUP en debut de collect()).
                if (day_key, project_id) in macapp_covered:
                    stats["skipped"] += 1
                    continue

                # Pick the dominant model captured from OTEL spans in
                # this day's session_uuid. The Counter is empty when no
                # spans matched (e.g. an idle session that only logged
                # git activity), in which case we keep `model_raw=None`
                # and let the usage metadata builder mark the model as
                # unknown.
                model_counts = d.get("model_counts")
                model_raw = None
                if model_counts:
                    model_raw = model_counts.most_common(1)[0][0]

                tokens_day = d.get("tokens", {})
                total_tokens_day = tokens_day.get("total", 0) or (
                    tokens_day.get("input", 0) + tokens_day.get("output", 0)
                )
                token_input_share = int(tokens_day.get("input", 0) * ratio)
                token_output_share = int(tokens_day.get("output", 0) * ratio)
                cached_share = int(tokens_day.get("cached", 0) * ratio)
                reasoning_share = int(tokens_day.get("reasoning", 0) * ratio)

                if total_tokens_day > 0 and model_raw:
                    # Real tokens are exposed: switch the cost basis from
                    # the legacy hour×$10 heuristic to a token-equivalent
                    # API cost computed against the SHIP pricing table.
                    cost_estimated = estimate_openai_cost(
                        model_raw,
                        tokens_input=token_input_share,
                        tokens_output=token_output_share,
                        cached_input_tokens=cached_share,
                    )
                    usage = build_usage_metadata(
                        provider="openai",
                        client="codex-desktop",
                        model_raw=model_raw,
                        tokens=TokenBreakdown(
                            input_tokens=token_input_share,
                            output_tokens=token_output_share,
                            cached_input_tokens=cached_share,
                            reasoning_tokens=reasoning_share,
                        ),
                        cost_estimated=cost_estimated,
                        cost_quality="factual",
                        active_time_quality="defensible",
                        cost_basis="token_equivalent_api",
                        token_source="codex_desktop_response_completed",
                        auth_mode=session_auth_mode,
                    )
                    token_input_out = token_input_share
                    token_output_out = token_output_share
                else:
                    # Fallback : pas de `response.completed` payload utile
                    # pour ce day (session abortee tot, ou Codex Desktop sur
                    # un build qui ne logge pas encore le bloc usage). On
                    # retombe sur l'estimation horaire historique pour ne
                    # rien perdre.
                    cost_estimated = (active_sec * ratio / 3600) * 10.0
                    usage = build_unknown_usage_metadata(
                        provider="openai",
                        client="codex-desktop",
                        model_raw=model_raw,
                        cost_estimated=cost_estimated,
                        cost_quality="indicative",
                        cost_basis="hourly_active_time_estimate",
                        auth_mode=session_auth_mode,
                    )
                    token_input_out = 0
                    token_output_out = 0
                event = {
                    "id": _stable_event_id(process_uuid, day_key, project_id),
                    "source": "codex_desktop",
                    "event_type": "session_day",
                    "started_at": started_iso,
                    "ended_at": ended_iso,
                    "duration_sec": int(active_sec * ratio),
                    "wall_clock_sec": int(wall_sec * ratio),
                    "cwd": None,
                    "project_id": project_id,
                    "project_conf": 0.80 if paths_filtered else 0.50,
                    "tool_or_action": "codex_desktop_session",
                    "token_input": token_input_out,
                    "token_output": token_output_out,
                    "cost_estimated": cost_estimated,
                    "user_msg_type": None,
                    "wordcount": 0,
                    "confidence_flag": "high" if paths_filtered and ratio >= 0.5 else "medium",
                    "raw_meta": json.dumps({
                        "process_uuid": process_uuid,
                        "turn_count": d["turns"],
                        "tool_call_count": d["tool_calls"],
                        "paths_sampled": len(paths_filtered),
                        "split_ratio": round(ratio, 3),
                        "auth_mode": session_auth_mode,
                        "usage": usage,
                    }),
                }
                safe = sanitize_event(event)
                storage.upsert_event(safe, replace=True)
                stats["events_ingested"] += 1
            stats["sessions_ingested"] += 1

    con.close()

    if max_ts > last_ts:
        storage.set_ingestion_offset(
            "codex_desktop", "logs_ts_max",
            max_ts, datetime.utcnow().isoformat(),
        )

    return stats
