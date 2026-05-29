"""Roo Code + Kilo Code live ingestion + fixture parser.

Roo Code (legacy, shutdown 2026-04-21) and Kilo Code (successor since
2026-04-02) are both forks of Cline and share the same VS Code / Cursor
task storage schema:

    <editor>/User/globalStorage/<extension>/tasks/<task_id>/
        - task_metadata.json
        - api_conversation_history.json
        - ui_messages.json

The two extensions live side by side under different globalStorage
directories:

    rooveterinaryinc.roo-cline/   (Roo Code, legacy)
    kilocode.kilo-code/           (Kilo Code, successor)

Each is supported across the three macOS editor variants: Cursor, VS
Code, VS Code Insiders. SHIP keeps both under a single
``source="roo_kilo_code"`` event identity (conservative variant
labelling per fixture contract) but distinguishes the two via a
``variant`` field inside ``raw_meta`` so coverage reports and
downstream insights can drill down without inventing a separate source
id per fork.

The fixture parser ``parse_task_file`` remains the conservative,
schema-locked entry point for synthetic test data and is unchanged.
The new ``collect`` function shares parsing logic with the Cline
collector (forks of the same base, same on-disk schema).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ship1000x.core.usage import TokenBreakdown, build_unknown_usage_metadata, build_usage_metadata

SOURCE = "roo_kilo_code"
SCHEMA_VERSION = "ship1000x.roo_kilo_code.task.v1"

# Six globalStorage paths to probe: two extensions x three editor
# variants (Cursor, VS Code, VS Code Insiders).
_APP_SUPPORT = Path.home() / "Library" / "Application Support"
_EDITORS = ("Cursor", "Code", "Code - Insiders")
_EXTENSION_DIRS = {
    "rooveterinaryinc.roo-cline": "roo-code",
    "kilocode.kilo-code": "kilo-code",
}


def _stable_event_id(task_id: str, started_at: str) -> str:
    raw = f"{SOURCE}|{task_id}|{started_at}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _tokens_from_payload(payload: dict[str, Any]) -> TokenBreakdown:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return TokenBreakdown(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_write_tokens") or 0),
        reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
    )


def parse_task_file(path: Path) -> dict[str, Any]:
    """Parse a redacted Roo/Kilo fixture without preserving task contents."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported Roo/Kilo fixture schema: {payload.get('schema_version')}")

    task_id = str(payload.get("task_id") or path.stem)
    started_at = str(payload.get("started_at") or "")
    ended_at = str(payload.get("ended_at") or started_at)
    variant = str(payload.get("variant") or "unknown")
    provider = str(payload.get("provider") or "unknown")
    model = str(payload.get("model") or "unknown")
    tokens = _tokens_from_payload(payload)
    usage = build_usage_metadata(
        provider=provider,
        client=variant,
        model_raw=model,
        tokens=tokens,
        cost_estimated=0.0,
        cost_quality="unknown",
        cost_basis="pricing_not_configured",
        token_source="roo_kilo_fixture_usage",
    )

    return {
        "id": _stable_event_id(task_id, started_at),
        "source": SOURCE,
        "event_type": "task",
        "task_id": task_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_sec": int(payload.get("active_sec") or 0),
        "wall_clock_sec": int(payload.get("wall_clock_sec") or 0),
        "model": model,
        "token_input": tokens.input_tokens,
        "token_output": tokens.output_tokens,
        "cost_estimated": 0.0,
        "confidence_flag": "medium",
        "tool_or_action": "variant_task",
        "raw_meta": {
            "schema_version": SCHEMA_VERSION,
            "provider": provider,
            "client": variant,
            "model": model,
            "mode": str(payload.get("mode") or "unknown"),
            "files_in_context_count": int(payload.get("files_in_context_count") or 0),
            "api_turn_count": int(payload.get("api_turn_count") or 0),
            "usage": usage,
        },
    }


def _candidate_task_dirs() -> list[tuple[Path, str]]:
    """Return existing ``(tasks_dir, variant)`` pairs.

    ``variant`` is ``"roo-code"`` or ``"kilo-code"`` depending on the
    extension. Non-existent paths are filtered out so the collector is
    safe to run on a machine where neither extension is installed.
    """
    candidates: list[tuple[Path, str]] = []
    for editor in _EDITORS:
        for ext_dir, variant in _EXTENSION_DIRS.items():
            tasks_path = _APP_SUPPORT / editor / "User" / "globalStorage" / ext_dir / "tasks"
            if tasks_path.exists():
                candidates.append((tasks_path, variant))
    return candidates


def _provider_for_model(model: str | None) -> str:
    """Best-effort provider label from the model id, mirroring Cline."""
    model_lower = (model or "").lower()
    if "claude" in model_lower or "anthropic" in model_lower:
        return "anthropic"
    if "gpt" in model_lower or "openai" in model_lower or model_lower.startswith("o"):
        return "openai"
    if "gemini" in model_lower or "google" in model_lower:
        return "google"
    return "unknown"


def collect(storage, classifier, privacy_config: dict[str, Any]) -> dict[str, int]:
    """Ingestion Roo Code + Kilo Code. Idempotent via ingestion_state.

    Shares the on-disk parsing logic with the Cline collector since
    Roo and Kilo are forks of Cline. The ingestion offset key embeds
    the variant so a Roo task_id never overwrites a Kilo task_id with
    the same numeric timestamp.
    """
    from ship1000x.collectors.cline import _parse_task
    from ship1000x.core.privacy import is_excluded_path, sanitize_event

    stats = {"files_seen": 0, "sessions_ingested": 0, "events_ingested": 0, "skipped": 0}
    candidates = _candidate_task_dirs()
    if not candidates:
        return stats

    exclude_paths = privacy_config.get("exclude_paths", []) or []

    for tasks_path, variant in candidates:
        for task_dir in tasks_path.iterdir():
            if not task_dir.is_dir():
                continue
            stats["files_seen"] += 1

            offset_key = f"{variant}|{task_dir.name}"

            try:
                mtimes = [
                    (task_dir / fn).stat().st_mtime
                    for fn in (
                        "task_metadata.json",
                        "api_conversation_history.json",
                        "ui_messages.json",
                    )
                    if (task_dir / fn).exists()
                ]
                if not mtimes:
                    continue
                current_mtime = int(max(mtimes))
            except OSError:
                continue

            last_mtime = storage.get_ingestion_offset(SOURCE, offset_key)
            if current_mtime <= last_mtime:
                continue

            parsed = _parse_task(task_dir)
            if parsed is None:
                continue

            paths = [p for p in parsed["tool_paths"] if not is_excluded_path(p, exclude_paths)]
            if not paths:
                stats["skipped"] += 1
                continue

            distribution = classifier.paths_distribution(paths)
            if not distribution:
                primary, conf = classifier.classify_session(paths=paths)
                distribution = {primary or "unclassified": 1.0}
                conf_used = conf
            else:
                conf_used = 0.80

            for project_id, ratio in distribution.items():
                composite_task_id = f"{variant}|{parsed['task_id']}"
                event = {
                    "id": _stable_event_id(composite_task_id, project_id),
                    "source": SOURCE,
                    "event_type": "task",
                    "started_at": parsed["started_at"],
                    "ended_at": parsed["ended_at"],
                    "duration_sec": int(parsed["active_sec"] * ratio),
                    "wall_clock_sec": int(parsed["wall_clock_sec"] * ratio),
                    "cwd": None,
                    "project_id": project_id,
                    "project_conf": conf_used,
                    "tool_or_action": "variant_task",
                    "token_input": 0,
                    "token_output": 0,
                    "cost_estimated": 0.0,
                    "user_msg_type": None,
                    "wordcount": 0,
                    "confidence_flag": "medium",
                    "raw_meta": json.dumps({
                        "task_id": parsed["task_id"],
                        "variant": variant,
                        "model": parsed["model"],
                        "mode": parsed["mode"],
                        "files_touched": parsed["files_touched"],
                        "api_turn_count": parsed["api_turn_count"],
                        "user_msg_count": parsed["user_msg_count"],
                        "split_ratio": round(ratio, 3),
                        "usage": build_unknown_usage_metadata(
                            provider=_provider_for_model(parsed["model"]),
                            client=variant,
                            model_raw=parsed["model"],
                            cost_estimated=0.0,
                            cost_quality="unknown",
                            cost_basis="not_exposed_by_task_metadata",
                        ),
                    }),
                }
                safe = sanitize_event(event)
                storage.upsert_event(safe, replace=True)
                stats["events_ingested"] += 1
            stats["sessions_ingested"] += 1

            storage.set_ingestion_offset(
                SOURCE,
                offset_key,
                current_mtime,
                datetime.now(timezone.utc).isoformat(),
            )

    return stats
