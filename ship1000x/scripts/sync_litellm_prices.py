"""Refresh the vendored LiteLLM model_prices snapshot.

Usage
-----

    python -m ship1000x.scripts.sync_litellm_prices               # update in place
    python -m ship1000x.scripts.sync_litellm_prices --check       # diff only, no write
    python -m ship1000x.scripts.sync_litellm_prices --commit <sha> # pin a specific commit

The script :

1. Fetches the latest commit SHA of ``model_prices_and_context_window.json``
   from the LiteLLM GitHub repo (``--commit`` overrides this).
2. If different from the pinned SHA recorded in
   ``ship1000x/data/LITELLM_ATTRIBUTION.md``, downloads the new JSON
   at that commit (read-pin, immutable).
3. Compares it against the local copy. Reports added / changed /
   removed model entries plus a small summary of price drift for the
   models SHIP actively tracks (gpt-5*, claude-opus*, claude-sonnet*,
   claude-haiku*).
4. Updates the snapshot file and the attribution metadata (SHA, date,
   entry count) unless ``--check`` was passed.

No third-party Python dependency. Uses the stdlib only — ``urllib``
+ ``json``. Network calls are mockable in tests via
``urllib.request.urlopen``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LITELLM_RAW_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/{sha}/"
    "model_prices_and_context_window.json"
)
LITELLM_LATEST_COMMIT_API = (
    "https://api.github.com/repos/BerriAI/litellm/commits/main?"
    "path=model_prices_and_context_window.json"
)

SNAPSHOT_PATH = (
    Path(__file__).parent.parent / "data" / "litellm_prices.json"
)
ATTRIBUTION_PATH = (
    Path(__file__).parent.parent / "data" / "LITELLM_ATTRIBUTION.md"
)

# Subset of models SHIP actively tracks. Used by the drift summary so
# a 100x bug like the May 2026 Anthropic cost_report drift would be
# caught at sync time.
SHIP_TRACKED_PREFIXES = (
    "gpt-5",
    "gpt-4o",
    "o1",
    "o3",
    "claude-opus",
    "claude-sonnet",
    "claude-haiku",
)


def _http_get(url: str) -> bytes:
    """Wrapper around urlopen used so tests can monkey-patch."""
    req = urllib.request.Request(url, headers={"User-Agent": "ship1000x-sync"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return resp.read()


def fetch_latest_commit_sha() -> str:
    """Return the SHA of the latest commit touching the pricing file."""
    raw = _http_get(LITELLM_LATEST_COMMIT_API)
    payload = json.loads(raw.decode("utf-8"))
    sha = payload.get("sha")
    if not isinstance(sha, str) or not sha:
        raise RuntimeError("GitHub API did not return a commit SHA")
    return sha


def fetch_snapshot_at(sha: str) -> dict:
    """Return the parsed JSON snapshot at the given commit SHA."""
    raw = _http_get(LITELLM_RAW_URL.format(sha=sha))
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("LiteLLM snapshot is not a JSON object")
    return payload


def read_pinned_sha() -> str | None:
    """Parse the pinned SHA from the attribution markdown file."""
    if not ATTRIBUTION_PATH.exists():
        return None
    text = ATTRIBUTION_PATH.read_text(encoding="utf-8")
    match = re.search(r"Commit\s*:\s*`([0-9a-f]{7,40})`", text)
    if not match:
        return None
    return match.group(1)


def load_local_snapshot() -> dict:
    if not SNAPSHOT_PATH.exists():
        return {}
    with SNAPSHOT_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _tracked(key: str) -> bool:
    return any(key.lower().startswith(p) for p in SHIP_TRACKED_PREFIXES)


def summarise_diff(
    old: dict,
    new: dict,
) -> tuple[list[str], list[str], list[str], list[tuple[str, float, float]]]:
    """Compare two snapshots, return added / removed / changed / drift."""
    old_keys = set(old.keys())
    new_keys = set(new.keys())
    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    changed: list[str] = []
    tracked_drift: list[tuple[str, float, float]] = []
    for key in sorted(old_keys & new_keys):
        if old[key] != new[key]:
            changed.append(key)
            if _tracked(key):
                old_input = old[key].get("input_cost_per_token") or 0
                new_input = new[key].get("input_cost_per_token") or 0
                if old_input != new_input:
                    tracked_drift.append((key, float(old_input), float(new_input)))
    return added, removed, changed, tracked_drift


def update_attribution(new_sha: str, entry_count: int) -> None:
    """Rewrite the SHA and date lines in LITELLM_ATTRIBUTION.md."""
    if not ATTRIBUTION_PATH.exists():
        return
    text = ATTRIBUTION_PATH.read_text(encoding="utf-8")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = re.sub(r"- Commit\s*:\s*`[^`]+`", f"- Commit : `{new_sha}`", text)
    text = re.sub(r"- Date\s*:\s*[0-9]{4}-[0-9]{2}-[0-9]{2}", f"- Date   : {today}", text)
    text = re.sub(r"- Entries\s*:\s*\d+", f"- Entries: {entry_count}", text)
    ATTRIBUTION_PATH.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only print diff vs current pin; do not write files.",
    )
    parser.add_argument(
        "--commit",
        default=None,
        help="Pin to this commit SHA instead of fetching latest.",
    )
    args = parser.parse_args(argv)

    pinned_sha = read_pinned_sha()
    target_sha = args.commit or fetch_latest_commit_sha()
    if pinned_sha == target_sha and not args.commit:
        print(f"already pinned at {pinned_sha}, nothing to do.")
        return 0

    new_snapshot = fetch_snapshot_at(target_sha)
    old_snapshot = load_local_snapshot()
    added, removed, changed, drift = summarise_diff(old_snapshot, new_snapshot)
    print(f"pinned -> {pinned_sha or '<none>'}")
    print(f"target -> {target_sha}")
    print(f"entries: {len(old_snapshot)} -> {len(new_snapshot)}")
    print(f"  added  : {len(added)}")
    print(f"  removed: {len(removed)}")
    print(f"  changed: {len(changed)}")
    if drift:
        print("\nSHIP-tracked input cost drift:")
        for key, old_input, new_input in drift:
            if old_input == 0 and new_input == 0:
                continue
            ratio = (new_input / old_input) if old_input else float("inf")
            print(f"  {key}: {old_input} -> {new_input}  (x{ratio:.2f})")

    if args.check:
        return 0

    SNAPSHOT_PATH.write_text(
        json.dumps(new_snapshot, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    update_attribution(target_sha, len(new_snapshot))
    print("\nsnapshot updated. review the diff and commit:")
    print(f"  git diff -- {SNAPSHOT_PATH.relative_to(Path.cwd())}")
    print(f"  git diff -- {ATTRIBUTION_PATH.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
