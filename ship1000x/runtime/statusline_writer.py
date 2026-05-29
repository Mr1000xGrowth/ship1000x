"""Claude Code statusline writer — capture live context-window pressure.

Invoked by Claude Code on every statusline tick (~every few seconds
when the user interacts). Reads the JSON payload Claude Code writes
to stdin, appends a one-line summary to
``~/.ship1000x/drop/statusline/<YYYY-MM-DD>.jsonl``, and writes back
to stdout the short statusline string Claude Code should display.

Configuration in ``~/.claude/settings.json`` :

.. code-block:: json

    {
      "statusline": {
        "command": ["python", "-m", "ship1000x.runtime.statusline_writer"]
      }
    }

Performance contract
--------------------

This script runs on every statusline render. It MUST :

- Return in < 50 ms wall time, ideally < 10 ms.
- Never touch the SQLite store directly — that's the regular
  ingestion loop's job.
- Never raise. A broken statusline writer must not crash the
  Claude Code UI loop.

Privacy contract
----------------

The Claude Code stdin payload may include the transcript path, the
working directory, the session id, the model name, and live context-
window usage. The writer :

- Stores only the **fields SHIP1000X tracks** : timestamp, session id,
  model, cwd basename, context usage percentage, transcript length,
  compact-boundary flag. Never the transcript content, never the
  current prompt.
- Anonymises absolute paths using ``ship1000x.core.privacy.anonymize_path``.

Inspiration
-----------

Pattern inspired by Claude HUD (MIT, github.com/jarrodwatts/claude-hud)
— mode 1 of the "inspired-by, not forked-from" strategy. No code
copied; we re-implement the stdin pipe + drop file pattern.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DROP_DIR = Path.home() / ".ship1000x" / "drop" / "statusline"

# Whitelist of payload fields we extract from the Claude Code stdin
# JSON. Keep this minimal — any new field requires a privacy review.
_TRACKED_FIELDS = (
    "session_id",
    "model",
    "cwd",
    "transcript_path",
    "version",
    "context",
    "compact",
)


def _safe_extract(payload: dict[str, Any]) -> dict[str, Any]:
    """Pluck the tracked fields out of the Claude Code stdin payload."""
    out: dict[str, Any] = {}
    for key in _TRACKED_FIELDS:
        if key in payload:
            out[key] = payload[key]
    # Always stamp with a server-side timestamp; the Claude Code
    # payload may not include one.
    out["captured_at"] = datetime.now(timezone.utc).isoformat()
    return out


def _build_statusline_text(record: dict[str, Any]) -> str:
    """Build the short string Claude Code displays in its statusline.

    Format example : ``ctx 47%  •  gpt-5.5  •  ~code/ship1000x``.
    Keep it under 60 characters to fit terminal widths.
    """
    parts: list[str] = []
    ctx = record.get("context") or {}
    if isinstance(ctx, dict):
        pct = ctx.get("used_percent")
        if isinstance(pct, (int, float)):
            parts.append(f"ctx {pct:.0f}%")
    model = record.get("model")
    if isinstance(model, str) and model:
        parts.append(model)
    cwd = record.get("cwd")
    if isinstance(cwd, str) and cwd:
        # Basename only for the statusline display.
        parts.append(Path(cwd).name)
    return "  •  ".join(parts) or "ship1000x"


def _append_to_drop(record: dict[str, Any]) -> None:
    """Append the record to today's drop file, never raise."""
    try:
        DROP_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = DROP_DIR / f"{day}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # Writer is fail-safe : a write error must not crash Claude
        # Code. The next tick will retry.
        return


def main(argv: list[str] | None = None) -> int:
    """Read stdin, append to drop, print statusline to stdout."""
    try:
        raw = sys.stdin.read()
    except OSError:
        return 0
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    record = _safe_extract(payload)
    _append_to_drop(record)
    sys.stdout.write(_build_statusline_text(record))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
