"""ABTop-inspired runtime daemon — observe AI-relevant processes + ports.

Foreground polling daemon invoked by ``ship1000x watch``. Every
``--interval`` seconds (default 30), enumerates running processes
matching a whitelist of AI-relevant patterns (claude, codex, cursor,
mcp, ollama, ...), captures their listening TCP ports, and appends a
snapshot to ``~/.ship1000x/drop/watch/<YYYY-MM-DD>.jsonl``.

The regular SHIP1000X ingest loop (``ship1000x ingest --source
agent_runtime``) reads those drop files via the
:mod:`ship1000x.collectors.agent_runtime` collector and aggregates
ticks per ``(process_name, minute)`` into ``agent_runtime_tick``
events.

Pattern inspired by ABTop (mode 1 — "inspired-by, not forked-from").
Zero code copied; we re-implement the polling + classification model.

V1 scope (locked)
-----------------

- **Foreground only**. No LaunchAgent, no systemd unit, no background
  install. The user must run ``ship1000x watch --foreground``
  explicitly.
- **Polling at 30s**. No eBPF, no syscall tracing, no DTrace. Resist
  the temptation to go deeper.
- **Process + ports only**. Rate-limit observability is deferred
  (needs the local usage proxy interception).

Privacy
-------

- The daemon stores **process names only** — never the full cmdline
  (which can contain session ids, file paths, customer hints, etc.).
- PIDs are not stored; only counts of distinct PIDs per process name.
- Listening ports are stored (not sensitive on their own, useful for
  MCP server detection).
- The optional psutil dependency must be installed
  (``pip install ship1000x[watch]``). Without it, the CLI command
  prints a helpful message and exits cleanly.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DROP_DIR = Path.home() / ".ship1000x" / "drop" / "watch"

DEFAULT_INTERVAL_SEC = 30
MIN_INTERVAL_SEC = 5
MAX_PORTS_PER_PROCESS = 10

# Whitelist of substrings matched against process names (lower-case).
# Conservative — false positives create privacy and noise issues. New
# entries require a brief justification in PR.
AI_PROCESS_PATTERNS: tuple[str, ...] = (
    "claude",
    "codex",
    "cursor",
    "anthropic",
    "openai",
    "chatgpt",
    "ollama",
    "llama",
    "mcp",
    "gemini",
    "copilot",
    "continue",
    "aider",
    "kilocode",
    "windsurf",
    "zed",
)

# Sub-patterns that promote a process to "MCP-likely" classification.
MCP_PATTERNS: tuple[str, ...] = (
    "mcp",
    "model-context-protocol",
    "modelcontextprotocol",
)


def _is_ai_process(name: str) -> bool:
    """True if the process name matches any AI whitelist substring."""
    if not name:
        return False
    n = name.lower()
    return any(pat in n for pat in AI_PROCESS_PATTERNS)


def _is_mcp_likely(name: str) -> bool:
    if not name:
        return False
    n = name.lower()
    return any(pat in n for pat in MCP_PATTERNS)


def _snapshot_processes(psutil_mod) -> list[dict[str, Any]]:
    """Enumerate AI-relevant processes via psutil. Never raises."""
    # Group by normalised process name so multiple PIDs of the same
    # process collapse into one entry with pid_count.
    by_name: dict[str, dict[str, Any]] = {}
    listening_by_pid: dict[int, set[int]] = {}

    # Listening sockets first — cheaper than iterating processes if
    # psutil has cached state.
    try:
        conns = psutil_mod.net_connections(kind="inet")
    except (psutil_mod.AccessDenied, psutil_mod.Error, OSError, PermissionError):
        conns = []
    for c in conns:
        try:
            if getattr(c, "status", None) != psutil_mod.CONN_LISTEN:
                continue
            pid = getattr(c, "pid", None)
            laddr = getattr(c, "laddr", None)
            if pid is None or laddr is None:
                continue
            port = getattr(laddr, "port", None)
            if not isinstance(port, int):
                continue
            listening_by_pid.setdefault(pid, set()).add(port)
        except (AttributeError, OSError):
            continue

    try:
        proc_iter = psutil_mod.process_iter(["pid", "name"])
    except (psutil_mod.Error, OSError):
        return []

    for proc in proc_iter:
        try:
            info = proc.info
            name = (info.get("name") or "").strip()
            pid = info.get("pid")
        except (psutil_mod.NoSuchProcess, psutil_mod.AccessDenied, KeyError):
            continue
        if not _is_ai_process(name):
            continue
        normalised = name.lower()
        entry = by_name.setdefault(
            normalised,
            {
                "name": normalised,
                "pid_count": 0,
                "ports": set(),
                "mcp_likely": _is_mcp_likely(normalised),
            },
        )
        entry["pid_count"] += 1
        if pid in listening_by_pid:
            entry["ports"].update(listening_by_pid[pid])

    # Materialise sets into bounded sorted lists for JSON.
    out: list[dict[str, Any]] = []
    for entry in by_name.values():
        ports = sorted(entry["ports"])[:MAX_PORTS_PER_PROCESS]
        out.append({
            "name": entry["name"],
            "pid_count": entry["pid_count"],
            "listening_ports": ports,
            "mcp_likely": bool(entry["mcp_likely"]),
        })
    out.sort(key=lambda e: e["name"])
    return out


def _append_to_drop(record: dict[str, Any]) -> bool:
    """Append the tick record to today's drop file. Never raise."""
    try:
        DROP_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = DROP_DIR / f"{day}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def _build_tick_record(processes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "processes": processes,
    }


def _install_signal_handlers() -> dict[str, bool]:
    state = {"stop": False}

    def _handler(signum, frame):  # noqa: ARG001
        state["stop"] = True

    try:
        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        # Some embedded contexts (e.g. tests in a thread) don't allow
        # signal handlers — fall back to a never-stopping flag. The
        # caller is expected to bound the loop via max_ticks in those
        # contexts.
        pass
    return state


def run_one_tick(psutil_mod) -> dict[str, Any]:
    """Run a single tick: snapshot, write to drop, return the record.

    Exposed for tests so the polling loop and the per-tick logic can
    be verified independently.
    """
    processes = _snapshot_processes(psutil_mod)
    record = _build_tick_record(processes)
    _append_to_drop(record)
    return record


def run_foreground(
    *,
    interval_sec: int = DEFAULT_INTERVAL_SEC,
    max_ticks: int | None = None,
    log_fn=None,
    psutil_mod=None,
) -> int:
    """Run the polling daemon in foreground until SIGINT/SIGTERM.

    ``max_ticks`` bounds the loop (tests use this; production passes
    ``None`` for unbounded). ``log_fn`` is a callable that receives one
    short info string per tick — defaults to writing to stderr.
    ``psutil_mod`` is injected for tests; production loads
    :mod:`psutil` lazily on first call.

    Returns 0 on clean shutdown, non-zero on unrecoverable error.
    """
    if interval_sec < MIN_INTERVAL_SEC:
        interval_sec = MIN_INTERVAL_SEC

    if psutil_mod is None:
        try:
            import psutil as _psutil
            psutil_mod = _psutil
        except ImportError:
            msg = (
                "ship1000x watch requires the optional `psutil` dependency. "
                "Install it with: pip install ship1000x[watch]"
            )
            print(msg, file=sys.stderr)
            return 2

    if log_fn is None:
        def log_fn(line: str) -> None:
            print(line, file=sys.stderr, flush=True)

    state = _install_signal_handlers()
    ticks = 0
    log_fn(
        f"ship1000x watch: polling every {interval_sec}s, "
        f"drop dir = {DROP_DIR}"
    )

    while not state["stop"]:
        record = run_one_tick(psutil_mod)
        n_proc = len(record["processes"])
        n_mcp = sum(1 for p in record["processes"] if p["mcp_likely"])
        log_fn(
            f"tick @ {record['captured_at']}  "
            f"processes={n_proc}  mcp_likely={n_mcp}"
        )
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        # Sleep in small chunks so SIGINT is responsive.
        slept = 0
        while slept < interval_sec and not state["stop"]:
            time.sleep(min(1, interval_sec - slept))
            slept += 1

    log_fn(f"ship1000x watch: stopped cleanly after {ticks} tick(s)")
    return 0


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    """Entry point used by `python -m ship1000x.runtime.watch_daemon`.

    The Click CLI in :mod:`ship1000x.cli` is the supported user-facing
    entry; this ``main`` exists for parity with
    :mod:`ship1000x.runtime.statusline_writer` and for ad-hoc testing.
    """
    return run_foreground()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
