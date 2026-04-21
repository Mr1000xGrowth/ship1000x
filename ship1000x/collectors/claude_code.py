"""Collector Claude Code JSONL.

Lit `~/.claude/projects/<slug>/*.jsonl` et extrait :
  - Sessions (file = 1 session)
  - User events typology (typed / approval / tool_result / system / paste)
  - Tool uses (nom + paths anonymises)
  - Active time reel (base sur intervalles entre USER events, seuil 5min)
  - Token counts / cost estimated

PAS de contenu stocke. Uniquement metadonnees quantitatives.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

CLAUDE_CODE_DIR = Path.home() / ".claude" / "projects"
ACTIVE_PAUSE_THRESHOLD_SEC = 5 * 60  # 5 min entre 2 user events = pause
SHORT_APPROVAL_WORDS = 5
VOICE_DICTATION_WORDCOUNT = 200
# Garde-fou : aucun humain ne peut maintenir > 12h d'activite continue
# sur une meme session. Au-dela, on cap pour eviter les aberrations des
# sessions laissees ouvertes plusieurs jours.
MAX_ACTIVE_SEC_PER_SESSION = 12 * 3600

# Cost estimates deleguees a core.pricing (source de verite partagee avec
# codex.py). Tarifs API officiels Anthropic, mis a jour 2026-04-21.


@dataclass
class UserEvent:
    """Un event de type `user` dans le JSONL."""
    timestamp: str
    msg_type: str  # typed | approval | tool_result | system | paste
    wordcount: int
    paths_touched: list[str] = field(default_factory=list)


@dataclass
class AssistantEvent:
    """Un event de type `assistant` (pour tokens/cost, pas pour temps actif)."""
    timestamp: str
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    tokens_input: int = 0
    tokens_output: int = 0
    model: str = "default"


def _classify_user_event(record: dict[str, Any]) -> tuple[str, int, list[str]]:
    """Determine le type d'event user + wordcount + paths touches.

    Returns:
        (msg_type, wordcount, paths_touched)
    """
    message = record.get("message", {})
    content = message.get("content") if isinstance(message, dict) else None

    # Content peut etre str ou list de blocks
    if isinstance(content, list):
        # Check si contient un tool_result
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return ("tool_result", 0, [])
            if isinstance(block, dict) and block.get("type") == "image":
                return ("paste", 0, [])

        # Sinon concatener les blocks text
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif isinstance(block, str):
                text_parts.append(block)
        text = " ".join(text_parts)
    elif isinstance(content, str):
        text = content
    else:
        text = ""

    # Detection system-reminder
    if "<system-reminder>" in text or text.startswith("[Request interrupted"):
        return ("system", 0, [])

    # Detection pastedContents
    if record.get("pastedContents"):
        return ("paste", 0, [])

    # Wordcount sur texte vrai
    stripped = text.strip()
    words = stripped.split()
    wc = len(words)

    if wc == 0:
        return ("system", 0, [])
    if wc <= SHORT_APPROVAL_WORDS:
        return ("approval", wc, [])

    return ("typed", wc, [])


def _extract_tool_paths(tool_use: dict[str, Any]) -> list[str]:
    """Extrait les paths touches par un tool_use (pour classification projet).

    NE retourne PAS les paths dans le raw_meta — uniquement pour classification.
    """
    name = tool_use.get("name", "")
    inp = tool_use.get("input", {}) or {}
    paths = []

    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        fp = inp.get("file_path") or inp.get("notebook_path")
        if fp:
            paths.append(fp)
    elif name == "Bash":
        cmd = inp.get("command", "")
        # Extraire des paths de la commande (best-effort)
        for m in re.finditer(r"[/~][\w\-./]+", cmd):
            paths.append(m.group(0))
    elif name in ("Glob", "Grep"):
        p = inp.get("path")
        if p:
            paths.append(p)

    return paths


def _parse_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _estimate_duration_sec(user_events: list[UserEvent]) -> int:
    """Calcule le temps actif reel base sur intervalles entre USER events.

    Regles :
      - intervalle <= 5 min : compte 100% (focus continu certain)
      - 5-15 min             : compte 50% (pause courte + reflexion probable)
      - 15-30 min            : compte 25% (pause longue mais Claude reste actif)
      - > 30 min             : compte 0 (vraie pause / reunion / autre tache)

    L'IA qui reflechit seule pendant 10 min n'ajoute rien car c'est l'ecart
    entre deux messages USER qui compte, pas les tool_results intercales.
    """
    if len(user_events) < 2:
        return 0

    total_sec = 0.0
    prev_ts = None
    for ev in user_events:
        if ev.msg_type == "system":
            continue
        ts = _parse_timestamp(ev.timestamp)
        if ts is None:
            continue
        if prev_ts is not None:
            delta = (ts - prev_ts).total_seconds()
            if delta <= 0:
                pass
            elif delta <= ACTIVE_PAUSE_THRESHOLD_SEC:           # <= 5 min
                total_sec += delta
            elif delta <= ACTIVE_PAUSE_THRESHOLD_SEC * 3:       # 5-15 min
                total_sec += delta * 0.5
            elif delta <= ACTIVE_PAUSE_THRESHOLD_SEC * 6:       # 15-30 min
                total_sec += delta * 0.25
            # > 30 min : ignore
        prev_ts = ts

    return int(total_sec)


def _wall_clock_sec(first_ts: str | None, last_ts: str | None) -> int:
    """Duree wall-clock entre le premier et le dernier event d'une session.

    Inclut les pauses, les tool_results, les reflexions IA. C'est la
    metrique "Temps session" exposee en parallele du "Temps actif".
    """
    a = _parse_timestamp(first_ts)
    b = _parse_timestamp(last_ts)
    if a is None or b is None:
        return 0
    delta = (b - a).total_seconds()
    return max(0, int(delta))


def _estimate_cost(model: str, tok_in: int, tok_out: int, cache_read: int = 0, cache_write: int = 0) -> float:
    """Cout approximatif USD based on tokens.

    Delegue a core.pricing.estimate_anthropic_cost pour coherence avec
    codex.py et mise a jour centralisee des tarifs.
    """
    from ship1000x.core.pricing import estimate_anthropic_cost
    return estimate_anthropic_cost(
        model=model,
        tokens_input=tok_in,
        tokens_output=tok_out,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


def _stable_event_id(source: str, file_key: str, line_num: int, ts: str) -> str:
    """ID deterministe pour dedup."""
    raw = f"{source}|{file_key}|{line_num}|{ts}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def parse_session_file(path: Path) -> dict[str, Any]:
    """Parse un fichier JSONL Claude Code complet.

    Returns dict avec:
      - session_id, cwd, started_at, ended_at
      - user_events: list[UserEvent]
      - assistant_events: list[AssistantEvent]
      - tool_paths: list[str] (paths touches agreges pour classification)
      - active_sec, lines_added/deleted, tokens_in/out, cost
    """
    user_events: list[UserEvent] = []
    assistant_events: list[AssistantEvent] = []
    tool_paths: list[str] = []
    cwd: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    session_id = path.stem  # le nom du fichier fait office d'ID

    total_tok_in = 0
    total_tok_out = 0
    total_cost = 0.0
    user_msg_counts = {"typed": 0, "approval": 0, "tool_result": 0, "system": 0, "paste": 0}

    # Split par jour calendaire (UTC) pour les sessions multi-jours (cas /compact)
    from collections import defaultdict
    daily: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "user_events": [],
        "tokens_input": 0,
        "tokens_output": 0,
        "cost": 0.0,
        "user_msg_counts": {"typed": 0, "approval": 0, "tool_result": 0, "system": 0, "paste": 0},
        "assistant_turns": 0,
        "first_ts": None,
        "last_ts": None,
        "tool_paths": [],  # paths touches ce jour, pour split multi-projets
    })

    try:
        with path.open("r", encoding="utf-8") as f:
            for line_num, line in enumerate(f):
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

                # CWD est dans les metas au debut du fichier
                if not cwd:
                    cwd = record.get("cwd") or record.get("workingDirectory")

                rec_type = record.get("type")

                # Clef jour = date du timestamp (UTC)
                day_key = (ts or "")[:10] if ts else "unknown"
                d = daily[day_key]
                if d["first_ts"] is None and ts:
                    d["first_ts"] = ts
                if ts:
                    d["last_ts"] = ts

                if rec_type == "user":
                    msg_type, wc, _ = _classify_user_event(record)
                    user_msg_counts[msg_type] = user_msg_counts.get(msg_type, 0) + 1
                    ue = UserEvent(
                        timestamp=ts or "",
                        msg_type=msg_type,
                        wordcount=wc,
                    )
                    user_events.append(ue)
                    # Split par jour
                    d["user_events"].append(ue)
                    d["user_msg_counts"][msg_type] = d["user_msg_counts"].get(msg_type, 0) + 1

                elif rec_type == "assistant":
                    msg = record.get("message", {}) or {}
                    model = msg.get("model") or record.get("model") or "default"
                    usage = msg.get("usage", {}) or {}
                    tin = usage.get("input_tokens", 0) or 0
                    tout = usage.get("output_tokens", 0) or 0
                    total_tok_in += tin
                    total_tok_out += tout
                    cost_here = _estimate_cost(model, tin, tout)
                    total_cost += cost_here
                    d["tokens_input"] += tin
                    d["tokens_output"] += tout
                    d["cost"] += cost_here
                    d["assistant_turns"] += 1

                    tool_uses = []
                    content = msg.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tu = {"name": block.get("name"), "input": block.get("input", {})}
                                tool_uses.append(tu)
                                extracted = _extract_tool_paths(tu)
                                tool_paths.extend(extracted)
                                d["tool_paths"].extend(extracted)

                    assistant_events.append(AssistantEvent(
                        timestamp=ts or "",
                        tool_uses=tool_uses,
                        tokens_input=tin,
                        tokens_output=tout,
                        model=model,
                    ))
    except OSError:
        pass

    active_sec = _estimate_duration_sec(user_events)
    # Cap anti-aberration : sessions > 12h sont suspectes (laissees ouvertes)
    was_capped = active_sec > MAX_ACTIVE_SEC_PER_SESSION
    if was_capped:
        active_sec = MAX_ACTIVE_SEC_PER_SESSION

    # Calcule active_sec + wall_clock_sec par jour
    # Cap 12h par jour sur active_sec (anti-aberration focus humain)
    # Pas de cap sur wall_clock_sec (peut legitimement atteindre 16-20h)
    for day_key, d in daily.items():
        d["active_sec"] = _estimate_duration_sec(d["user_events"])
        if d["active_sec"] > MAX_ACTIVE_SEC_PER_SESSION:
            d["active_sec"] = MAX_ACTIVE_SEC_PER_SESSION
        d["wall_clock_sec"] = _wall_clock_sec(d["first_ts"], d["last_ts"])

    return {
        "session_id": session_id,
        "cwd": cwd,
        "started_at": first_ts,
        "ended_at": last_ts,
        "user_events": user_events,
        "assistant_events": assistant_events,
        "tool_paths": tool_paths,
        "active_sec": active_sec,
        "tokens_input": total_tok_in,
        "tokens_output": total_tok_out,
        "cost_estimated": total_cost,
        "user_msg_counts": user_msg_counts,
        "was_capped": was_capped,
        "daily": dict(daily),  # split par jour calendaire
    }


def iter_session_files(projects_dir: Path = CLAUDE_CODE_DIR) -> Iterator[Path]:
    """Yield tous les fichiers JSONL sous ~/.claude/projects/."""
    if not projects_dir.exists():
        return
    yield from projects_dir.glob("*/*.jsonl")


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Ingestion complete des sessions Claude Code.

    Args:
        storage: Storage instance
        classifier: Classifier instance
        privacy_config: config/privacy.yaml parsed

    Returns:
        Stats d'ingestion
    """
    from ship1000x.core.privacy import sanitize_event

    stats = {"files_seen": 0, "sessions_ingested": 0, "events_ingested": 0, "skipped": 0}
    exclude_paths = privacy_config.get("exclude_paths", []) or []

    for jsonl_path in iter_session_files():
        stats["files_seen"] += 1
        file_key = str(jsonl_path.relative_to(Path.home()))

        # Skip si deja ingere (idempotence)
        file_size = jsonl_path.stat().st_size
        last_offset = storage.get_ingestion_offset("claude_code", file_key)
        if last_offset >= file_size:
            continue

        parsed = parse_session_file(jsonl_path)

        # Check exclusion
        cwd = parsed.get("cwd") or ""
        from ship1000x.core.privacy import is_excluded_path
        if is_excluded_path(cwd, exclude_paths):
            stats["skipped"] += 1
            continue

        # Classification
        project_id, conf = classifier.classify_session(
            cwd=cwd,
            paths=parsed["tool_paths"],
        )

        # Store session
        session_event = {
            "id": parsed["session_id"],
            "source": "claude_code",
            "started_at": parsed["started_at"],
            "ended_at": parsed["ended_at"],
            "event_count": len(parsed["user_events"]) + len(parsed["assistant_events"]),
            "project_id": project_id,
            "project_conf": conf,
            "primary_tool": "claude_code",
            "active_sec": parsed["active_sec"],
            "lines_added": 0,
            "lines_deleted": 0,
        }
        storage.upsert_session(session_event)

        # Store des events par JOUR pour la session (sessions multi-jours avec /compact).
        # Split multi-projets : si la session touche plusieurs repos via tool_paths,
        # on cree 1 event par projet touche avec duration/wall_clock/cost ponderes.
        # La somme des events par jour = total original. Sinon 1 seul event (comportement V1).
        # INSERT OR REPLACE : la session en cours peut grossir entre deux ingestions,
        # on re-calcule les events du jour courant a chaque ingest.
        for day_key, d in (parsed.get("daily") or {}).items():
            if day_key == "unknown" or not d["first_ts"]:
                continue

            day_paths = d.get("tool_paths") or []
            distribution = classifier.paths_distribution(day_paths)
            if not distribution:
                # Fallback : 100% sur le projet primaire (classification cwd/session)
                distribution = {project_id or "unclassified": 1.0}

            total_wc = sum(e.wordcount for e in d["user_events"])
            for pid, ratio in distribution.items():
                event_id = _stable_event_id(
                    "claude_code", file_key, 0, f"{day_key}|{pid}"
                )
                event = {
                    "id": event_id,
                    "source": "claude_code",
                    "event_type": "session_day",
                    "started_at": d["first_ts"],
                    "ended_at": d["last_ts"],
                    "duration_sec": int(d["active_sec"] * ratio),
                    "wall_clock_sec": int(d.get("wall_clock_sec", 0) * ratio),
                    "cwd": cwd,
                    "project_id": pid,
                    "project_conf": conf if pid == project_id else 0.80,
                    "tool_or_action": "session_day",
                    "token_input": int(d["tokens_input"] * ratio),
                    "token_output": int(d["tokens_output"] * ratio),
                    "cost_estimated": d["cost"] * ratio,
                    "user_msg_type": None,
                    "wordcount": int(total_wc * ratio),
                    "confidence_flag": "high" if conf >= 0.8 else ("medium" if conf >= 0.5 else "low"),
                    "raw_meta": json.dumps({
                        "user_msg_counts": d["user_msg_counts"],
                        "assistant_turns": d["assistant_turns"],
                        "session_id": parsed["session_id"],
                        "split_ratio": round(ratio, 3),
                        "primary_project": project_id,
                    }),
                }
                safe = sanitize_event(event)
                storage.upsert_event(safe, replace=True)
                stats["events_ingested"] += 1
        stats["sessions_ingested"] += 1

        storage.set_ingestion_offset(
            "claude_code",
            file_key,
            file_size,
            datetime.utcnow().isoformat(),
        )

    return stats
