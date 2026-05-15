"""Collector OpenClaw gateway.

OpenClaw est un gateway local qui spawne Claude Code / Codex / autres CLI
en sous-processus et capture leur output dans des JSONL centralises :

    ~/.openclaw/agents/{agent_name}/sessions/{session-uuid}.jsonl

Format de ligne (version 3) :
    {"type":"session", "version":3, "id":"...", "timestamp":"...", "cwd":"..."}
    {"type":"model_change", "provider":"openai-codex", "modelId":"gpt-5.4", ...}
    {"type":"thinking_level_change", ...}
    {"type":"custom", "customType":"model-snapshot", ...}
    {"type":"message", "message":{"role":"user|assistant|toolResult", ...}}

Les messages assistant contiennent directement :
    - provider : "openai-codex" | "anthropic" | ...
    - model : "gpt-5.4" | "claude-opus-4-7" | ...
    - usage : { input, output, cacheRead, cacheWrite, totalTokens, cost: {input, output, cacheRead, cacheWrite, total}}

OpenClaw logue le cout **deja calcule** par le provider (pas besoin de
re-estimer via pricing.py) — c'est la source la plus precise.

Chaque session_day event produit porte comme source="openclaw" pour
distinguer des collectors directs (claude_code, codex) — evite le
double-compte si Claude Code est lance ET via OpenClaw sur la meme journee.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OPENCLAW_BASE = Path.home() / ".openclaw"


def _resolve_openclaw_base() -> Path | None:
    """Retourne le chemin base OpenClaw s'il existe, sinon None."""
    if OPENCLAW_BASE.exists():
        return OPENCLAW_BASE
    return None


def _iso_to_epoch_sec(iso_str: str) -> int:
    """Convertit ISO → epoch seconds. Retourne 0 si invalide."""
    try:
        s = iso_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0


def _extract_user_text(message: dict) -> str:
    """Concatene le texte d'un message user (content list ou str)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(parts)
    return ""


def _classify_user_msg(text: str) -> str:
    """Retourne le type de message user : 'cron', 'typed', 'paste', 'approval'."""
    if "[cron:" in text:
        return "cron"
    # Heuristique paste : > 500 chars collés (pas tapés)
    if len(text) > 1500:
        return "paste"
    # Heuristique approval : texte tres court type "allow", "continue", "ok"
    low = text.lower().strip()
    if low in {"allow", "allow-once", "allow-always", "continue", "ok", "yes", "go", "oui"}:
        return "approval"
    return "typed"


# Type codes sync avec core.rollup._fetch_event_markers (V4)
_MSG_TYPE_CODES = {
    "typed": 0,
    "approval": 1,
    "paste": 2,
    "tool_result": 4,
    "system": 5,
    "cron": 0,  # cron compte comme event humain (intention, meme si automatise)
}


def parse_session_file(path: Path) -> dict[str, Any] | None:
    """Parse un JSONL session OpenClaw.

    Retourne un dict avec les daily stats regroupees par date UTC :
    {
        "session_id": "...",
        "agent_name": "main",
        "cwd": "...",
        "daily": {
            "2026-04-22": {
                "first_ts_epoch": int,
                "last_ts_epoch": int,
                "user_events": [(ts_epoch, type_code), ...],
                "assistant_events": [(ts_epoch, type_code=3), ...],
                "tool_results": [(ts_epoch, type_code=4), ...],
                "user_msg_counts": {"typed", "paste", "approval", "cron"},
                "assistant_turns": int,
                "tokens_input": int,
                "tokens_output": int,
                "cache_read": int,
                "cache_write": int,
                "cost": float,  # USD
                "models": {model_name: {tokens_in, tokens_out, cost, turns}},
            }
        }
    }
    """
    daily: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "first_ts_epoch": None,
            "last_ts_epoch": None,
            "user_events": [],
            "assistant_events": [],
            "tool_results": [],
            "user_msg_counts": {"typed": 0, "paste": 0, "approval": 0, "cron": 0},
            "assistant_turns": 0,
            "tokens_input": 0,
            "tokens_output": 0,
            "cache_read": 0,
            "cache_write": 0,
            "cost": 0.0,
            "models": {},
        }
    )
    session_id = path.stem
    cwd = ""

    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if rec.get("type") == "session":
                    cwd = rec.get("cwd", "") or cwd
                    continue

                if rec.get("type") != "message":
                    continue

                ts_iso = rec.get("timestamp")
                ts_epoch = _iso_to_epoch_sec(ts_iso or "")
                if ts_epoch <= 0:
                    continue
                day = ts_iso[:10] if ts_iso else "unknown"
                d = daily[day]
                if d["first_ts_epoch"] is None:
                    d["first_ts_epoch"] = ts_epoch
                d["last_ts_epoch"] = ts_epoch

                msg = rec.get("message", {}) or {}
                role = msg.get("role")

                if role == "user":
                    text = _extract_user_text(msg)
                    msg_type = _classify_user_msg(text)
                    d["user_msg_counts"][msg_type] = (
                        d["user_msg_counts"].get(msg_type, 0) + 1
                    )
                    d["user_events"].append(
                        [ts_epoch, _MSG_TYPE_CODES.get(msg_type, 0)]
                    )
                elif role == "toolResult":
                    d["tool_results"].append([ts_epoch, 4])
                elif role == "assistant":
                    d["assistant_turns"] += 1
                    d["assistant_events"].append([ts_epoch, 3])
                    usage = msg.get("usage", {}) or {}
                    tin = usage.get("input", 0) or 0
                    tout = usage.get("output", 0) or 0
                    cache_r = usage.get("cacheRead", 0) or 0
                    cache_w = usage.get("cacheWrite", 0) or 0
                    cost_block = usage.get("cost", {}) or {}
                    cost_total = cost_block.get("total", 0.0) or 0.0
                    model = msg.get("model") or "unknown"
                    d["tokens_input"] += tin
                    d["tokens_output"] += tout
                    d["cache_read"] += cache_r
                    d["cache_write"] += cache_w
                    d["cost"] += cost_total
                    ms = d["models"].setdefault(
                        model,
                        {
                            "tokens_in": 0,
                            "tokens_out": 0,
                            "cost": 0.0,
                            "turns": 0,
                        },
                    )
                    ms["tokens_in"] += tin
                    ms["tokens_out"] += tout
                    ms["cost"] += cost_total
                    ms["turns"] += 1
    except OSError:
        return None

    if not daily:
        return None

    # Extrait le nom de l'agent depuis le chemin : ~/.openclaw/agents/{agent}/sessions/...
    try:
        parts = path.parts
        agents_idx = parts.index("agents")
        agent_name = parts[agents_idx + 1] if len(parts) > agents_idx + 1 else "unknown"
    except ValueError:
        agent_name = "unknown"

    return {
        "session_id": session_id,
        "agent_name": agent_name,
        "cwd": cwd,
        "daily": dict(daily),
    }


def _stable_event_id(session_id: str, day: str, project_id: str) -> str:
    raw = f"openclaw|{session_id}|{day}|{project_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _build_event_timeline(
    user_events: list[list[int]],
    assistant_events: list[list[int]],
    tool_results: list[list[int]],
) -> list[list[int]]:
    """Fusionne les 3 listes en une timeline triee."""
    merged = user_events + assistant_events + tool_results
    merged.sort(key=lambda x: x[0])
    return merged


def collect(
    storage, classifier, privacy_config: dict[str, Any]
) -> dict[str, int]:
    """Ingest OpenClaw sessions. Skip silencieusement si ~/.openclaw absent.

    Emet 1 event 'session_day' par (session_id, date, agent) avec :
      - source="openclaw"
      - raw_meta incluant event_timeline (V4), model_stats (V5), agent_name,
        auth_mode="via_openclaw_gateway"
      - project_id classifie via cwd + agent_name
    """
    from ship1000x.core.privacy import sanitize_event, is_excluded_path

    stats = {
        "files_seen": 0,
        "files_parsed": 0,
        "sessions_ingested": 0,
        "events_ingested": 0,
        "skipped": 0,
    }

    base = _resolve_openclaw_base()
    if base is None:
        return stats  # OpenClaw pas installe, skip

    exclude_paths = privacy_config.get("exclude_paths", []) or []

    # Scan tous les JSONL dans agents/{agent}/sessions/
    session_files = list(base.glob("agents/*/sessions/*.jsonl"))
    stats["files_seen"] = len(session_files)

    for session_file in session_files:
        # Offset ingestion par file_key (nom de fichier)
        file_key = str(session_file.relative_to(base))
        file_size = session_file.stat().st_size
        last_offset = storage.get_ingestion_offset("openclaw", file_key)
        if last_offset >= file_size:
            stats["skipped"] += 1
            continue

        parsed = parse_session_file(session_file)
        if not parsed:
            continue
        stats["files_parsed"] += 1

        cwd = parsed["cwd"]
        if is_excluded_path(cwd, exclude_paths):
            stats["skipped"] += 1
            continue

        # Classification project_id : on utilise cwd quand dispo, sinon
        # agent_name comme indice (e.g. "department---role---project" → "project")
        agent_hint = parsed["agent_name"].split("---")[-1] if parsed["agent_name"] else ""
        classify_paths = [cwd] * 3 + ([agent_hint] if agent_hint else [])
        distribution = classifier.paths_distribution(classify_paths)
        if not distribution:
            primary, _ = classifier.classify_session(cwd=cwd, paths=[agent_hint])
            distribution = {primary or "unclassified": 1.0}

        for day_key, d in parsed["daily"].items():
            if d["first_ts_epoch"] is None:
                continue

            wall_clock_sec = max(0, d["last_ts_epoch"] - d["first_ts_epoch"])
            # active_sec : approximation basee sur les user events avec cap 5 min
            # On ne refait pas le calcul complet ici (trop complexe) — on somme
            # les ecarts < 300s entre events humains, + cap par turn 60s min.
            user_ts = sorted([e[0] for e in d["user_events"]])
            active_sec = 0
            for i, ts in enumerate(user_ts):
                if i == 0:
                    active_sec += 60  # 1er event = 60s
                    continue
                gap = ts - user_ts[i - 1]
                if gap <= 300:
                    active_sec += gap
                else:
                    active_sec += 60
            if wall_clock_sec > 0:
                active_sec = min(active_sec, wall_clock_sec)

            started_iso = datetime.fromtimestamp(
                d["first_ts_epoch"], tz=timezone.utc
            ).isoformat()
            ended_iso = datetime.fromtimestamp(
                d["last_ts_epoch"], tz=timezone.utc
            ).isoformat()

            timeline = _build_event_timeline(
                d["user_events"], d["assistant_events"], d["tool_results"]
            )
            total_user_msgs = sum(d["user_msg_counts"].values())

            for project_id, ratio in distribution.items():
                event = {
                    "id": _stable_event_id(parsed["session_id"], day_key, project_id),
                    "source": "openclaw",
                    "event_type": "session_day",
                    "started_at": started_iso,
                    "ended_at": ended_iso,
                    "duration_sec": int(active_sec * ratio),
                    "wall_clock_sec": int(wall_clock_sec * ratio),
                    "cwd": cwd,
                    "project_id": project_id,
                    "project_conf": 0.85 if cwd else 0.60,
                    "tool_or_action": f"openclaw_{parsed['agent_name']}",
                    "token_input": int(d["tokens_input"] * ratio),
                    "token_output": int(d["tokens_output"] * ratio),
                    "cost_estimated": d["cost"] * ratio,
                    "user_msg_type": None,
                    "wordcount": 0,
                    "confidence_flag": "high" if cwd else "medium",
                    "raw_meta": json.dumps(
                        {
                            "session_id": parsed["session_id"],
                            "agent_name": parsed["agent_name"],
                            "user_msg_counts": d["user_msg_counts"],
                            "assistant_turns": d["assistant_turns"],
                            "cache_read_tokens": d["cache_read"],
                            "cache_write_tokens": d["cache_write"],
                            "split_ratio": round(ratio, 3),
                            # V4 event_timeline
                            "event_timeline": timeline,
                            # V5 model_stats proratise
                            "model_stats": {
                                m: {
                                    "tokens_in": int(s["tokens_in"] * ratio),
                                    "tokens_out": int(s["tokens_out"] * ratio),
                                    "cost": s["cost"] * ratio,
                                    "turns": int(s["turns"] * ratio),
                                }
                                for m, s in d["models"].items()
                            },
                            # V6 auth flag : toutes les requetes via OpenClaw
                            "auth_mode": "openclaw_gateway",
                        }
                    ),
                }
                safe = sanitize_event(event)
                storage.upsert_event(safe, replace=True)
                stats["events_ingested"] += 1
            stats["sessions_ingested"] += 1

        storage.set_ingestion_offset(
            "openclaw",
            file_key,
            file_size,
            datetime.utcnow().isoformat(),
        )

    return stats
