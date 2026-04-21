"""Collector shell — parse ~/.zsh_history avec EXTENDED_HISTORY.

Format extended :
    : <unix_timestamp>:<duration_sec>;<command>

Sans EXTENDED_HISTORY, le fichier n'a que les commandes sans timestamp.
Dans ce cas, le collector avise le user de configurer son shell.

PRIVACY : on ne stocke JAMAIS la commande complete. Uniquement le verbe
(1er token), un hash SHA256 tronque du reste, et un tag projet derivee du
cwd au moment de l'execution (impossible a reconstruire, best-effort via
match des paths dans la commande).
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


ZSH_HISTORY = Path.home() / ".zsh_history"
BASH_HISTORY = Path.home() / ".bash_history"

# Verbes interessants (autres ignores car souvent unix trivial)
INTERESTING_VERBS = {
    "git", "npm", "yarn", "pnpm", "python", "python3", "uv", "pip", "pip3",
    "cargo", "go", "node", "deno", "bun",
    "claude", "codex", "cursor", "tracker",
    "docker", "kubectl", "ansible", "terraform",
    "pytest", "vitest", "jest", "mocha",
    "make", "cmake",
    "prisma", "rails", "mix",
}


def _hash_command(cmd: str) -> str:
    """Hash court pour dedup sans stocker contenu."""
    return hashlib.sha256(cmd.encode("utf-8", errors="replace")).hexdigest()[:16]


def _extract_verb(cmd: str) -> str:
    """Extrait le 1er mot exe (git, npm, python, etc.)."""
    tokens = cmd.strip().split()
    if not tokens:
        return "unknown"
    return tokens[0].split("/")[-1]  # strip path prefix /usr/bin/python -> python


def _extract_paths(cmd: str) -> list[str]:
    """Best-effort extraction de paths dans la commande (pour classification)."""
    paths = []
    for m in re.finditer(r"[~/][\w\-./]+", cmd):
        paths.append(m.group(0))
    return paths


def check_extended_history() -> dict[str, Any]:
    """Check si EXTENDED_HISTORY est actif dans le zshrc."""
    zshrc = Path.home() / ".zshrc"
    has_extended = False
    if zshrc.exists():
        try:
            text = zshrc.read_text()
            has_extended = (
                "EXTENDED_HISTORY" in text
                or "setopt extended_history" in text.lower()
            )
        except OSError:
            pass

    # Check direct si le fichier a des timestamps ":"
    file_has_timestamps = False
    if ZSH_HISTORY.exists():
        try:
            with ZSH_HISTORY.open("rb") as f:
                head = f.read(4096).decode("utf-8", errors="ignore")
                file_has_timestamps = any(
                    line.startswith(": ") and ":" in line[2:]
                    for line in head.splitlines()
                )
        except OSError:
            pass

    return {
        "zshrc_configured": has_extended,
        "history_has_timestamps": file_has_timestamps,
        "history_path": str(ZSH_HISTORY),
        "history_exists": ZSH_HISTORY.exists(),
    }


def parse_zsh_history(path: Path = ZSH_HISTORY) -> Iterator[dict[str, Any]]:
    """Parse zsh history format extended.

    Ligne = `: <ts>:<duration>;<command>` (ou `: <ts>:<duration>;<cmd1>\\\\<cmd2>`)
    Sans le prefix `: ` = pas de timestamp, skip.
    """
    if not path.exists():
        return

    try:
        with path.open("rb") as f:
            raw = f.read()
    except OSError:
        return

    # Zsh history est en latin1 avec echappement \xHH — on decode best-effort
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("latin-1", errors="replace")

    buffer = ""
    for line in content.splitlines():
        if line.startswith(": "):
            # Flush la ligne precedente si elle existait
            if buffer:
                parsed = _parse_line(buffer)
                if parsed:
                    yield parsed
            buffer = line
        else:
            # Suite de la commande precedente (multi-line avec \)
            buffer += "\n" + line

    if buffer:
        parsed = _parse_line(buffer)
        if parsed:
            yield parsed


def _parse_line(line: str) -> dict[str, Any] | None:
    """Parse une ligne `: ts:duration;command`."""
    if not line.startswith(": "):
        return None
    body = line[2:]
    # body = ts:duration;command
    parts = body.split(";", 1)
    if len(parts) != 2:
        return None
    header, cmd = parts
    ts_dur = header.split(":")
    if len(ts_dur) != 2:
        return None
    try:
        ts = int(ts_dur[0])
        duration = int(ts_dur[1])
    except ValueError:
        return None
    return {
        "timestamp": ts,
        "duration_sec": duration,
        "command": cmd.strip(),
    }


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Ingestion zsh history extended."""
    from ship1000x.core.privacy import sanitize_event

    stats = {"files_seen": 0, "events_ingested": 0, "events_skipped": 0}

    check = check_extended_history()
    if not check["history_exists"] or not check["history_has_timestamps"]:
        return stats

    stats["files_seen"] = 1
    file_key = str(ZSH_HISTORY.relative_to(Path.home()))

    # Idempotence : on stocke le last_ts ingere
    last_offset = storage.get_ingestion_offset("shell", file_key)

    max_ts = last_offset
    for entry in parse_zsh_history():
        ts = entry["timestamp"]
        if ts <= last_offset:
            continue
        verb = _extract_verb(entry["command"])
        if verb not in INTERESTING_VERBS:
            stats["events_skipped"] += 1
            continue

        paths = _extract_paths(entry["command"])
        project_id, conf = classifier.classify_session(paths=paths)

        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        event = {
            "id": f"shell-{_hash_command(entry['command'])}-{ts}",
            "source": "shell",
            "event_type": "command",
            "started_at": dt.isoformat(),
            "ended_at": dt.isoformat(),
            "duration_sec": entry["duration_sec"],
            "cwd": None,
            "project_id": project_id,
            "project_conf": conf,
            "tool_or_action": verb,
            "token_input": 0,
            "token_output": 0,
            "cost_estimated": 0.0,
            "user_msg_type": None,
            "wordcount": 0,
            "confidence_flag": "medium" if conf >= 0.5 else "low",
            "raw_meta": None,  # pas de contenu stocke
        }
        safe = sanitize_event(event)
        storage.upsert_event(safe)
        stats["events_ingested"] += 1
        if ts > max_ts:
            max_ts = ts

    if max_ts > last_offset:
        storage.set_ingestion_offset(
            "shell", file_key, max_ts, datetime.utcnow().isoformat()
        )

    return stats
