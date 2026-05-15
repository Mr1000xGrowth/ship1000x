"""Collector Codex JSONL.

Format different de Claude Code (malgre l'idee initiale) :
  - type: "session_meta" (1er event, contient cwd)
  - type: "response_item" + payload.{type,role,content}

Events user reels = payload.role == "user" AVEC payload.content[].type == "input_text".
Events developer = instructions systeme injectees (AGENTS.md, etc.) → skip.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
ACTIVE_PAUSE_THRESHOLD_SEC = 5 * 60
SHORT_APPROVAL_WORDS = 5
import os as _os_max  # noqa
# Cap per session : protects against 'app left open' aberrations.
# Override via env var SHIP1000X_MAX_SESSION_HOURS for power users
# who genuinely run intensive multi-session days (>16h is rare but possible).
MAX_ACTIVE_SEC_PER_SESSION = int(_os_max.environ.get('SHIP1000X_MAX_SESSION_HOURS', '16')) * 3600


def _parse_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _classify_codex_user_content(content_blocks: list) -> tuple[str, int]:
    """Detecte si un message user Codex est vrai, approval, ou systeme."""
    if not content_blocks:
        return ("system", 0)

    text_parts = []
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "input_text":
            text_parts.append(block.get("text", ""))

    text = " ".join(text_parts).strip()
    if not text:
        return ("system", 0)

    # Detection injection systeme (AGENTS.md, permissions, etc.)
    if text.startswith("<INSTRUCTIONS>") or text.startswith("# AGENTS.md"):
        return ("system", 0)
    if "<permissions" in text[:200]:
        return ("system", 0)

    wc = len(text.split())
    if wc <= SHORT_APPROVAL_WORDS:
        return ("approval", wc)
    return ("typed", wc)


def _extract_tool_paths_codex(payload: dict) -> list[str]:
    """Extrait paths d'un function_call Codex."""
    args_raw = payload.get("arguments", "")
    paths = []
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        if isinstance(args, dict):
            for key in ("path", "file_path", "target"):
                v = args.get(key)
                if v and isinstance(v, str):
                    paths.append(v)
            # command parsing
            cmd = args.get("command", "")
            if isinstance(cmd, list):
                cmd = " ".join(str(x) for x in cmd)
            if isinstance(cmd, str):
                import re
                for m in re.finditer(r"[/~][\w\-./]+", cmd):
                    paths.append(m.group(0))
    except (json.JSONDecodeError, TypeError):
        pass
    return paths


def parse_session_file(path: Path) -> dict[str, Any]:
    """Parse un rollout Codex JSONL."""
    cwd: str | None = None
    session_id = path.stem.replace("rollout-", "").split("-", 1)[-1]
    first_ts: str | None = None
    last_ts: str | None = None
    tool_paths: list[str] = []
    user_events_ts: list[tuple[str, str]] = []  # (timestamp, msg_type)
    # Tokens : on garde le DERNIER snapshot total_token_usage vu (Codex
    # stocke un cumul deja, pas un delta par event). On garde aussi le
    # detail cache/reasoning pour l'estimation de cout precise.
    last_total_input = 0
    last_total_output = 0
    last_cached_input = 0
    last_reasoning_output = 0
    # Modele : extrait du session_meta (originator ou base_instructions).
    # Codex identifie son modele dans `base_instructions.text` type
    # "You are Codex, a coding agent based on GPT-5.". Parse de ce champ.
    model = ""
    user_msg_counts = {"typed": 0, "approval": 0, "tool_result": 0, "system": 0}
    tool_call_count = 0

    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = record.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

                rec_type = record.get("type")
                payload = record.get("payload", {}) or {}

                if rec_type == "session_meta":
                    cwd = payload.get("cwd") or cwd
                    # Parse model depuis base_instructions.text
                    bi = payload.get("base_instructions") or {}
                    bi_text = bi.get("text") if isinstance(bi, dict) else ""
                    if bi_text:
                        import re
                        m = re.search(
                            r"based on\s+(gpt-5-codex|gpt-5-mini|gpt-5-nano|gpt-5|gpt-4o-mini|gpt-4o|o3-mini|o3|o1)",
                            bi_text,
                            re.IGNORECASE,
                        )
                        if m:
                            model = m.group(1).lower()
                    # Fallback : default GPT-5 si Codex (le modele par defaut 2026)
                    if not model:
                        model = "gpt-5"
                    continue

                if rec_type == "response_item":
                    p_type = payload.get("type")
                    role = payload.get("role")

                    if p_type == "message" and role == "user":
                        msg_type, wc = _classify_codex_user_content(payload.get("content", []))
                        user_msg_counts[msg_type] = user_msg_counts.get(msg_type, 0) + 1
                        user_events_ts.append((ts or "", msg_type))

                    elif p_type == "function_call":
                        tool_call_count += 1
                        tool_paths.extend(_extract_tool_paths_codex(payload))

                    elif p_type == "function_call_output":
                        # Tool result, pas un vrai user message
                        user_msg_counts["tool_result"] = user_msg_counts.get("tool_result", 0) + 1

                elif rec_type == "token_count":
                    # V1 : format token_count avec payload.info.total_input_tokens
                    # V2 : format avec total_token_usage direct (2026 Codex)
                    info = payload.get("info", {}) if isinstance(payload, dict) else {}
                    # Format V1
                    t_in = info.get("total_input_tokens", 0) or 0
                    t_out = info.get("total_output_tokens", 0) or 0
                    if t_in or t_out:
                        last_total_input = max(last_total_input, t_in)
                        last_total_output = max(last_total_output, t_out)

                # Format V2 : total_token_usage peut etre dans n'importe quel
                # record (event_msg, etc.). On cherche a plat dans le record.
                tt = record.get("total_token_usage") or payload.get("total_token_usage")
                if isinstance(tt, dict):
                    last_total_input = max(last_total_input, tt.get("input_tokens", 0) or 0)
                    last_total_output = max(last_total_output, tt.get("output_tokens", 0) or 0)
                    last_cached_input = max(last_cached_input, tt.get("cached_input_tokens", 0) or 0)
                    last_reasoning_output = max(
                        last_reasoning_output, tt.get("reasoning_output_tokens", 0) or 0
                    )
    except OSError:
        pass

    tokens_in = last_total_input
    tokens_out = last_total_output
    cached_input = last_cached_input
    reasoning_output = last_reasoning_output

    # Temps actif base sur intervalles USER (meme regle que Claude Code)
    # Ponderation : <=5min 100%, 5-15min 50%, 15-30min 25%, >30min 0
    active_sec_f = 0.0
    prev = None
    for ts, msg_type in user_events_ts:
        if msg_type == "system":
            continue
        dt = _parse_timestamp(ts)
        if dt is None:
            continue
        if prev is not None:
            delta = (dt - prev).total_seconds()
            if delta <= 0:
                pass
            elif delta <= ACTIVE_PAUSE_THRESHOLD_SEC:
                active_sec_f += delta
            elif delta <= ACTIVE_PAUSE_THRESHOLD_SEC * 3:
                active_sec_f += delta * 0.5
            elif delta <= ACTIVE_PAUSE_THRESHOLD_SEC * 6:
                active_sec_f += delta * 0.25
        prev = dt
    active_sec = int(active_sec_f)

    if active_sec > MAX_ACTIVE_SEC_PER_SESSION:
        active_sec = MAX_ACTIVE_SEC_PER_SESSION

    # Temps wall-clock (first_ts -> last_ts, sans plafond)
    wall_clock_sec = 0
    a = _parse_timestamp(first_ts)
    b = _parse_timestamp(last_ts)
    if a and b:
        wall_clock_sec = max(0, int((b - a).total_seconds()))

    # Cout estime via tarifs OpenAI API (memes tokens, facturation equivalente)
    from ship1000x.core.pricing import estimate_openai_cost
    cost_estimated = estimate_openai_cost(
        model=model,
        tokens_input=tokens_in,
        tokens_output=tokens_out,
        cached_input_tokens=cached_input,
        reasoning_output_tokens=reasoning_output,
    )

    return {
        "session_id": session_id,
        "cwd": cwd,
        "started_at": first_ts,
        "ended_at": last_ts,
        "active_sec": active_sec,
        "wall_clock_sec": wall_clock_sec,
        "tool_paths": tool_paths,
        "tool_call_count": tool_call_count,
        "tokens_input": tokens_in,
        "tokens_output": tokens_out,
        "cached_input_tokens": cached_input,
        "reasoning_output_tokens": reasoning_output,
        "model": model,
        "cost_estimated": cost_estimated,
        "user_msg_counts": user_msg_counts,
    }


def iter_rollout_files(base_dir: Path = CODEX_SESSIONS_DIR) -> Iterator[Path]:
    if not base_dir.exists():
        return
    yield from base_dir.glob("**/rollout-*.jsonl")


def _stable_event_id(file_key: str, ts: str) -> str:
    raw = f"codex|{file_key}|{ts}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    from ship1000x.core.privacy import is_excluded_path, sanitize_event

    stats = {"files_seen": 0, "sessions_ingested": 0, "events_ingested": 0, "skipped": 0}
    exclude_paths = privacy_config.get("exclude_paths", []) or []

    for rollout in iter_rollout_files():
        stats["files_seen"] += 1
        file_key = str(rollout.relative_to(Path.home()))

        file_size = rollout.stat().st_size
        last_offset = storage.get_ingestion_offset("codex", file_key)
        if last_offset >= file_size:
            continue

        parsed = parse_session_file(rollout)
        cwd = parsed.get("cwd") or ""

        if is_excluded_path(cwd, exclude_paths):
            stats["skipped"] += 1
            continue

        project_id, conf = classifier.classify_session(
            cwd=cwd,
            paths=parsed["tool_paths"],
        )

        session_event = {
            "id": "codex-" + parsed["session_id"],
            "source": "codex",
            "started_at": parsed["started_at"],
            "ended_at": parsed["ended_at"],
            "event_count": sum(parsed["user_msg_counts"].values()) + parsed["tool_call_count"],
            "project_id": project_id,
            "project_conf": conf,
            "primary_tool": "codex",
            "active_sec": parsed["active_sec"],
            "lines_added": 0,
            "lines_deleted": 0,
        }
        storage.upsert_session(session_event)

        event = {
            "id": _stable_event_id(file_key, parsed["started_at"] or ""),
            "source": "codex",
            "event_type": "session",
            "started_at": parsed["started_at"],
            "ended_at": parsed["ended_at"],
            "duration_sec": parsed["active_sec"],
            "wall_clock_sec": parsed.get("wall_clock_sec", 0),
            "cwd": cwd,
            "project_id": project_id,
            "project_conf": conf,
            "tool_or_action": "session",
            "token_input": parsed["tokens_input"],
            "token_output": parsed["tokens_output"],
            # V2 : cout equivalent API OpenAI (GPT-5 par defaut pour Codex).
            # Meme logique que Claude Code : on calcule le cout theorique base
            # sur les tokens reels × tarif API, meme si l'user est en
            # abonnement flat (ChatGPT Plus). Permet comparaison cross-outils
            # et estimation ROI.
            "cost_estimated": parsed["cost_estimated"],
            "user_msg_type": None,
            "wordcount": 0,
            "confidence_flag": "high" if conf >= 0.8 else ("medium" if conf >= 0.5 else "low"),
            "raw_meta": json.dumps({
                "user_msg_counts": parsed["user_msg_counts"],
                "tool_calls": parsed["tool_call_count"],
                "model": parsed.get("model", ""),
                "cached_input_tokens": parsed.get("cached_input_tokens", 0),
                "reasoning_output_tokens": parsed.get("reasoning_output_tokens", 0),
            }),
        }
        safe = sanitize_event(event)
        storage.upsert_event(safe)
        stats["events_ingested"] += 1
        stats["sessions_ingested"] += 1

        storage.set_ingestion_offset(
            "codex",
            file_key,
            file_size,
            datetime.utcnow().isoformat(),
        )

    return stats
