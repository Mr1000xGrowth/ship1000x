"""Collector web exports — drop folder pour exports manuels Claude.ai / ChatGPT.

Pattern d'usage :
    $ tracker drop ~/Downloads/claude-data-export-2026-04.zip
    $ tracker drop ~/Downloads/conversations.json
    $ tracker drop ~/Downloads/chatgpt-export/

Ou dossier surveille : `~/ai-time-tracker/drop/` scanne a chaque ingest.

Formats supportes (V1) :
- Claude.ai ZIP : contient `conversations.json` avec threads/messages
- ChatGPT JSON : format "conversations.json" single file
- Fichiers conversations.json standalone

PRIVACY : on lit uniquement les metadata (title, timestamp, nb messages,
wordcount). Aucun contenu n'est stocke.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_DROP_DIR = REPO_ROOT / "drop"


def ensure_drop_dir() -> Path:
    DEFAULT_DROP_DIR.mkdir(exist_ok=True)
    return DEFAULT_DROP_DIR


def _hash_id(prefix: str, *args: Any) -> str:
    raw = f"{prefix}|{'|'.join(str(a) for a in args)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _parse_ts(value: Any) -> str | None:
    """Parse un timestamp (str ISO, int unix, int ms) en ISO UTC."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            if value > 1e12:  # ms
                value = value / 1000
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        if isinstance(value, str):
            # ISO 8601
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except (ValueError, OSError, TypeError):
        return None
    return None


def parse_claude_ai_conversations(
    conversations: list[dict[str, Any]],
    source: str = "claude_ai",
) -> Iterator[dict[str, Any]]:
    """Parse le format conversations.json Claude.ai."""
    for c in conversations:
        uuid = c.get("uuid") or c.get("id") or ""
        name = c.get("name") or c.get("title") or ""
        created = _parse_ts(c.get("created_at"))
        updated = _parse_ts(c.get("updated_at"))
        messages = c.get("chat_messages") or c.get("messages") or []

        # Compte user vs assistant
        typed = 0
        approval = 0
        total_wc = 0
        for m in messages:
            sender = m.get("sender") or m.get("role") or ""
            if sender not in ("human", "user"):
                continue
            text = m.get("text") or ""
            if not text and isinstance(m.get("content"), list):
                # Nouveau format multi-part
                text = " ".join(
                    b.get("text", "") for b in m["content"] if isinstance(b, dict)
                )
            wc = len(text.split())
            total_wc += wc
            if wc <= 5:
                approval += 1
            else:
                typed += 1

        if not messages:
            continue

        yield {
            "uuid": uuid,
            "name": name,
            "created_at": created,
            "updated_at": updated,
            "typed": typed,
            "approval": approval,
            "message_count": len(messages),
            "total_wordcount": total_wc,
            "source": source,
        }


def parse_chatgpt_conversations(
    conversations: list[dict[str, Any]],
) -> Iterator[dict[str, Any]]:
    """Parse le format conversations.json ChatGPT export."""
    for c in conversations:
        title = c.get("title") or ""
        created = _parse_ts(c.get("create_time"))
        updated = _parse_ts(c.get("update_time"))
        mapping = c.get("mapping") or {}

        typed = 0
        approval = 0
        total_wc = 0
        msg_count = 0
        for node in mapping.values():
            msg = node.get("message") if isinstance(node, dict) else None
            if not msg:
                continue
            author = msg.get("author") or {}
            if author.get("role") != "user":
                continue
            content = msg.get("content") or {}
            parts = content.get("parts") or []
            text = " ".join(str(p) for p in parts if isinstance(p, (str,)))
            wc = len(text.split())
            total_wc += wc
            msg_count += 1
            if wc <= 5:
                approval += 1
            else:
                typed += 1

        if msg_count == 0:
            continue

        yield {
            "uuid": c.get("id") or c.get("conversation_id") or "",
            "name": title,
            "created_at": created,
            "updated_at": updated,
            "typed": typed,
            "approval": approval,
            "message_count": msg_count,
            "total_wordcount": total_wc,
            "source": "chatgpt",
        }


def process_file(path: Path) -> Iterator[dict[str, Any]]:
    """Detecte format + parse."""
    if not path.exists():
        return
    suffix = path.suffix.lower()

    if suffix == ".zip":
        try:
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    if name.endswith("conversations.json"):
                        with zf.open(name) as f:
                            data = json.load(f)
                        if isinstance(data, list):
                            # Auto-detect : claude.ai ou chatgpt
                            sample = data[0] if data else {}
                            if "chat_messages" in sample or "uuid" in sample:
                                yield from parse_claude_ai_conversations(data)
                            elif "mapping" in sample:
                                yield from parse_chatgpt_conversations(data)
                            else:
                                yield from parse_claude_ai_conversations(data)
        except (zipfile.BadZipFile, json.JSONDecodeError, OSError):
            return

    elif suffix == ".json":
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(data, list):
            sample = data[0] if data else {}
            if "chat_messages" in sample or "uuid" in sample:
                yield from parse_claude_ai_conversations(data)
            elif "mapping" in sample:
                yield from parse_chatgpt_conversations(data)
            else:
                yield from parse_claude_ai_conversations(data)


def ingest_path(
    storage,
    classifier,
    path: Path,
    privacy_config: dict[str, Any],
) -> dict[str, int]:
    """Ingestion d'un fichier ou dossier specifique."""
    from ship1000x.core.privacy import sanitize_event

    stats = {"files_seen": 0, "events_ingested": 0, "skipped": 0}
    exclude = privacy_config.get("exclude_keywords") or []

    targets: list[Path] = []
    if path.is_dir():
        targets = list(path.glob("**/*.zip")) + list(path.glob("**/*.json"))
    elif path.is_file():
        targets = [path]

    for f in targets:
        stats["files_seen"] += 1
        for conv in process_file(f):
            name = conv.get("name", "") or ""
            if any(kw.lower() in name.lower() for kw in exclude):
                stats["skipped"] += 1
                continue

            project_id, conf = classifier.classify_session(title=name)
            created = conv.get("created_at") or datetime.now(timezone.utc).isoformat()
            updated = conv.get("updated_at") or created

            event = {
                "id": _hash_id("web_export", conv["source"], conv.get("uuid", ""), created),
                "source": "web_export",
                "event_type": "web_conversation",
                "started_at": created,
                "ended_at": updated,
                "duration_sec": 0,  # pas de notion de temps actif pour exports web
                "cwd": None,
                "project_id": project_id,
                "project_conf": conf,
                "tool_or_action": conv["source"],  # "claude_ai" | "chatgpt"
                "token_input": 0,
                "token_output": 0,
                "cost_estimated": 0.0,
                "user_msg_type": None,
                "wordcount": conv.get("total_wordcount", 0),
                "confidence_flag": "medium" if conf >= 0.5 else "low",
                "raw_meta": json.dumps({
                    "source_app": conv["source"],
                    "message_count": conv.get("message_count", 0),
                    "user_typed": conv.get("typed", 0),
                    "user_approval": conv.get("approval", 0),
                }),
            }
            safe = sanitize_event(event)
            storage.upsert_event(safe)
            stats["events_ingested"] += 1

    return stats


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Ingest tous les fichiers du drop folder par defaut."""
    drop = ensure_drop_dir()
    return ingest_path(storage, classifier, drop, privacy_config)
