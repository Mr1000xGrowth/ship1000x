"""Coverage health report — render what SHIP actually observes vs misses.

An internal coverage-health review motivated a report that surfaces blind
spots explicitly rather than letting a quiet dashboard imply full coverage.
This module is its SHIP-side implementation.

The report is built from two ingredients:

- ``SOURCE_REGISTRY`` — a hand-curated catalogue of every source SHIP
  could see, with a status tag (``active`` | ``supported`` |
  ``not_supported``), a category, and (for uncovered ones) an audit
  reference and the reason it stays uncovered.
- A query over the local SHIP store that fills, for every ``active``
  source, the freshness/volume/quality numbers (event count, first/
  last event, unknown-model count, auth_mode distribution, cost basis
  distribution, api_equivalent vs billed totals).

The renderer emits Markdown only. The data flow is read-only against
``Storage`` — no network, no provider call, no payload inspection.
Output is event-derived metadata only; no raw content ever appears.
"""

from __future__ import annotations

import contextlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# Status taxonomy. ``active`` means a collector exists AND has events
# in the store for the window; ``supported`` means a collector exists
# but the store has no events (the user does not use that source on
# this machine, or has not ingested it yet); ``not_supported`` means
# no collector exists, the source is a known gap from the audit.
SOURCE_STATUS_ACTIVE = "active"
SOURCE_STATUS_SUPPORTED = "supported"
SOURCE_STATUS_NOT_SUPPORTED = "not_supported"

CATEGORY_ANTHROPIC = "anthropic"
CATEGORY_OPENAI = "openai"
CATEGORY_CODING_IDE = "coding_ide"
CATEGORY_OTHER_LLM = "other_llm"
CATEGORY_DEV_CODE = "dev_code"
CATEGORY_SYSTEM = "system"
CATEGORY_RUNTIME = "runtime"
CATEGORY_OS_CONTEXT = "os_context"

CATEGORY_DISPLAY = {
    CATEGORY_ANTHROPIC: "Anthropic / Claude",
    CATEGORY_OPENAI: "OpenAI / Codex / ChatGPT",
    CATEGORY_CODING_IDE: "Coding IDEs & agents",
    CATEGORY_OTHER_LLM: "Other LLM providers",
    CATEGORY_DEV_CODE: "Dev / Code / Web",
    CATEGORY_SYSTEM: "System / Shell",
    CATEGORY_RUNTIME: "Live runtime (process / ports / MCP)",
    CATEGORY_OS_CONTEXT: "OS context (foreground app / window / browser)",
}


@dataclass(frozen=True)
class SourceEntry:
    """One row of the coverage registry."""

    source_id: str
    display_name: str
    category: str
    status: str  # SOURCE_STATUS_*
    audit_ref: str = ""
    reason: str = ""
    # Optional path (relative to ``~/.ship1000x/``) where a foreground
    # writer-daemon drops JSONL ticks before the collector picks them
    # up. When set, the coverage report surfaces drop file freshness so
    # the user can tell apart "no daemon running" vs "daemon ran but
    # ingest hasn't run yet". Only the Wave 3 runtime sources use this.
    drop_subpath: str = ""


# Curated registry, sorted by category. The list is hand-maintained
# against the audit (the source catalog + the gap review) and
# against the actual collectors under ``ship1000x/collectors/``.
# When a new collector is added, append it here with ``active`` /
# ``supported``; when a new gap is acknowledged from the audit, add
# it with ``not_supported`` plus a one-line reason.
SOURCE_REGISTRY: tuple[SourceEntry, ...] = (
    # Anthropic / Claude family
    SourceEntry(
        source_id="claude_code",
        display_name="Claude Code (CLI / Desktop / SDK)",
        category=CATEGORY_ANTHROPIC,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="claude_desktop",
        display_name="Claude Desktop session sidecars",
        category=CATEGORY_ANTHROPIC,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="claude_statusline",
        display_name="Claude Code statusline live ticks (Wave 3)",
        category=CATEGORY_ANTHROPIC,
        status=SOURCE_STATUS_SUPPORTED,
        drop_subpath="drop/statusline",
    ),
    SourceEntry(
        source_id="anthropic_usage",
        display_name="Anthropic billing (Admin API)",
        category=CATEGORY_ANTHROPIC,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="claude_web",
        display_name="Claude.ai web conversations (pure, hors Claude Code)",
        category=CATEGORY_ANTHROPIC,
        status=SOURCE_STATUS_NOT_SUPPORTED,
        audit_ref="the source catalog (C1/C2 partial)",
        reason="IndexedDB LevelDB binary; requires opt-in HTTPS proxy or browser extension.",
    ),
    # NOTE: the historical `claude_code_statusline` audit gap entry was
    # superseded by `claude_statusline` (Wave 3 / Day 1-2). The
    # statusline adapter described as "missing" in
    # `prioritized backlog item P1.2` now ships as
    # `ship1000x.collectors.claude_statusline` + the
    # `ship1000x.runtime.statusline_writer` Claude Code statusline hook.
    # OpenAI / Codex / ChatGPT family
    SourceEntry(
        source_id="codex",
        display_name="Codex CLI rollouts (~/.codex/sessions)",
        category=CATEGORY_OPENAI,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="codex_desktop",
        display_name="Codex Desktop (logs_2.sqlite SSE/websocket)",
        category=CATEGORY_OPENAI,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="codex_macapp",
        display_name="Codex.app macOS logs + cross-source model join",
        category=CATEGORY_OPENAI,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="codex_sqlite",
        display_name="Codex SQLite history (legacy)",
        category=CATEGORY_OPENAI,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="openai_usage",
        display_name="OpenAI billing (Admin API)",
        category=CATEGORY_OPENAI,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="codex_cloud",
        display_name="Codex Cloud / chatgpt.com/backend-api/codex analytics events",
        category=CATEGORY_OPENAI,
        status=SOURCE_STATUS_NOT_SUPPORTED,
        audit_ref="the source catalog (C1)",
        reason="Backend analytics events visible in Codex Desktop logs but not yet ingested as a dedicated source.",
    ),
    SourceEntry(
        source_id="chatgpt_desktop",
        display_name="ChatGPT Desktop standalone (macOS)",
        category=CATEGORY_OPENAI,
        status=SOURCE_STATUS_NOT_SUPPORTED,
        audit_ref="the source catalog (C1)",
        reason="Local SQLite schema not investigated yet.",
    ),
    SourceEntry(
        source_id="chatgpt_web",
        display_name="ChatGPT Web conversations (chat.openai.com / chatgpt.com)",
        category=CATEGORY_OPENAI,
        status=SOURCE_STATUS_NOT_SUPPORTED,
        audit_ref="the source catalog (C1/C2)",
        reason="No continuous local source; needs opt-in HTTPS proxy or browser extension.",
    ),
    SourceEntry(
        source_id="openai_api_trace",
        display_name="OpenAI Responses API request-level (proxy opt-in)",
        category=CATEGORY_OPENAI,
        status=SOURCE_STATUS_NOT_SUPPORTED,
        audit_ref="prioritized backlog item P2.1",
        reason="Request-level truth needs an opt-in HTTPS proxy (Claude Tap style); billing snapshot is the only source today.",
    ),
    # Coding IDEs & agents
    SourceEntry(
        source_id="cursor",
        display_name="Cursor IDE",
        category=CATEGORY_CODING_IDE,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="cursor_agent",
        display_name="Cursor Agent / Cursor CLI",
        category=CATEGORY_CODING_IDE,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="cline",
        display_name="Cline",
        category=CATEGORY_CODING_IDE,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="roo_kilo_code",
        display_name="Roo / Kilo Code (Cline variants)",
        category=CATEGORY_CODING_IDE,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="continue_dev",
        display_name="Continue.dev",
        category=CATEGORY_CODING_IDE,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="aider",
        display_name="Aider",
        category=CATEGORY_CODING_IDE,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="copilot_agents",
        display_name="GitHub Copilot agents",
        category=CATEGORY_CODING_IDE,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="opencode",
        display_name="OpenCode",
        category=CATEGORY_CODING_IDE,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="openclaw",
        display_name="OpenClaw",
        category=CATEGORY_CODING_IDE,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="goose",
        display_name="Goose",
        category=CATEGORY_CODING_IDE,
        status=SOURCE_STATUS_NOT_SUPPORTED,
        audit_ref="the source catalog (C1)",
        reason="Emerging coding agent; logs / pricing / model aliases not yet catalogued.",
    ),
    SourceEntry(
        source_id="antigravity",
        display_name="Antigravity",
        category=CATEGORY_CODING_IDE,
        status=SOURCE_STATUS_NOT_SUPPORTED,
        audit_ref="the source catalog (C1)",
        reason="Recently released agent; source format not catalogued.",
    ),
    # Other LLM providers
    SourceEntry(
        source_id="gemini_cli",
        display_name="Gemini CLI",
        category=CATEGORY_OTHER_LLM,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="kimi",
        display_name="Kimi (Moonshot)",
        category=CATEGORY_OTHER_LLM,
        status=SOURCE_STATUS_NOT_SUPPORTED,
        audit_ref="the source catalog (C1)",
        reason="Provider format / tooling not catalogued.",
    ),
    SourceEntry(
        source_id="qwen",
        display_name="Qwen",
        category=CATEGORY_OTHER_LLM,
        status=SOURCE_STATUS_NOT_SUPPORTED,
        audit_ref="the source catalog (C1)",
        reason="Provider format / tooling not catalogued.",
    ),
    # Dev / Code / Web
    SourceEntry(
        source_id="git",
        display_name="Git commits (multi-repo)",
        category=CATEGORY_DEV_CODE,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="git_secret_alert",
        display_name="Gitleaks secret-scan alerts (opt-in via privacy.yaml)",
        category=CATEGORY_DEV_CODE,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="web_exports",
        display_name="Web export imports (Claude / ChatGPT exports)",
        category=CATEGORY_DEV_CODE,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    # System / Shell
    SourceEntry(
        source_id="shell",
        display_name="Shell history (opt-in)",
        category=CATEGORY_SYSTEM,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    SourceEntry(
        source_id="mac_system",
        display_name="macOS wake / sleep / unlock (opt-in)",
        category=CATEGORY_SYSTEM,
        status=SOURCE_STATUS_SUPPORTED,
    ),
    # Live runtime — Wave 3 / Day 3-5 ABTop-inspired daemon.
    SourceEntry(
        source_id="agent_runtime",
        display_name="Agent live runtime (processes, listening ports, MCP servers) — Wave 3",
        category=CATEGORY_RUNTIME,
        status=SOURCE_STATUS_SUPPORTED,
        drop_subpath="drop/watch",
    ),
    # Local usage-proxy bridge.
    # An optional local HTTPS proxy captures provider API usage metadata
    # (Anthropic + OpenAI today) and drops 9-key JSONL records here. This
    # collector reads those drops.
    SourceEntry(
        source_id="trace",
        display_name="AI provider API traces via a local usage proxy (opt-in)",
        category=CATEGORY_RUNTIME,
        status=SOURCE_STATUS_SUPPORTED,
        drop_subpath="drop/trace",
    ),
    # OS context (not yet implemented)
    SourceEntry(
        source_id="foreground_app",
        display_name="Foreground app + window title (redacted)",
        category=CATEGORY_OS_CONTEXT,
        status=SOURCE_STATUS_NOT_SUPPORTED,
        audit_ref="prioritized backlog item P1.4",
        reason="OS Activity observer not implemented; needs opt-in consent flow.",
    ),
    SourceEntry(
        source_id="browser_active_domain",
        display_name="Browser active domain / tab (allowlisted)",
        category=CATEGORY_OS_CONTEXT,
        status=SOURCE_STATUS_NOT_SUPPORTED,
        audit_ref="prioritized backlog item P1.4",
        reason="Needs a browser extension or native connector with explicit allowlist.",
    ),
)


@dataclass
class SourceHealth:
    """Computed health metrics for one ``active`` / ``supported`` source."""

    entry: SourceEntry
    status: str  # may upgrade SOURCE_STATUS_SUPPORTED -> SOURCE_STATUS_ACTIVE
    event_count: int = 0
    first_event: str | None = None
    last_event: str | None = None
    days_covered: int = 0
    unknown_model_count: int = 0
    auth_mode_counts: Counter = field(default_factory=Counter)
    cost_basis_counts: Counter = field(default_factory=Counter)
    cost_quality_counts: Counter = field(default_factory=Counter)
    api_equivalent_usd: float = 0.0
    billed_estimated_usd: float = 0.0


def _parse_raw_meta(raw_meta: Any) -> dict:
    """Best-effort parse of an event's ``raw_meta`` column."""
    if not raw_meta:
        return {}
    if isinstance(raw_meta, dict):
        return raw_meta
    try:
        parsed = json.loads(raw_meta)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}


def _format_iso_date(value: str | None) -> str | None:
    """Return the YYYY-MM-DD slice of an ISO timestamp, or None."""
    if not value:
        return None
    return value[:10]


def compute_source_health(
    storage,
    entry: SourceEntry,
    *,
    since_days: int,
) -> SourceHealth:
    """Aggregate the health metrics for one source over ``since_days``."""
    health = SourceHealth(entry=entry, status=entry.status)
    if entry.status == SOURCE_STATUS_NOT_SUPPORTED:
        return health

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=since_days)
    ).isoformat()
    rows = storage.query(
        """
        SELECT started_at, raw_meta
        FROM events
        WHERE source = ? AND started_at >= ?
        ORDER BY started_at ASC
        """,
        (entry.source_id, cutoff),
    )
    if not rows:
        return health

    health.status = SOURCE_STATUS_ACTIVE
    health.event_count = len(rows)
    health.first_event = rows[0]["started_at"]
    health.last_event = rows[-1]["started_at"]

    days = set()
    for row in rows:
        day = _format_iso_date(row["started_at"])
        if day:
            days.add(day)
        meta = _parse_raw_meta(row["raw_meta"])
        usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
        cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
        quality = (
            usage.get("quality") if isinstance(usage.get("quality"), dict) else {}
        )
        model_raw = usage.get("model_raw")
        if not model_raw or model_raw == "unknown":
            health.unknown_model_count += 1
        auth_mode = usage.get("auth_mode") or meta.get("auth_mode") or "unknown"
        health.auth_mode_counts[auth_mode] += 1
        basis = cost.get("basis")
        if basis:
            health.cost_basis_counts[basis] += 1
        cost_quality = quality.get("cost") or cost.get("quality")
        if cost_quality:
            health.cost_quality_counts[cost_quality] += 1
        with contextlib.suppress(TypeError, ValueError):
            health.api_equivalent_usd += float(cost.get("api_equivalent_usd") or 0.0)
        with contextlib.suppress(TypeError, ValueError):
            health.billed_estimated_usd += float(
                cost.get("billed_estimated_usd") or 0.0
            )
    health.days_covered = len(days)
    return health


def compute_coverage(
    storage,
    *,
    since_days: int,
) -> list[SourceHealth]:
    """Compute health for every registry entry, preserving insertion order."""
    return [
        compute_source_health(storage, entry, since_days=since_days)
        for entry in SOURCE_REGISTRY
    ]


def _format_counter(counter: Counter) -> str:
    if not counter:
        return "—"
    return ", ".join(
        f"{name}: {count}"
        for name, count in counter.most_common()
    )


def _format_money(value: float) -> str:
    return f"${value:,.2f}"


def render_markdown_report(
    healths: list[SourceHealth],
    *,
    since_days: int,
    generated_at: datetime | None = None,
) -> str:
    """Render the coverage report as Markdown, grouped by category."""
    generated_at = generated_at or datetime.now(timezone.utc)
    lines: list[str] = []
    lines.append("# SHIP1000x Coverage Report")
    lines.append("")
    lines.append(f"Generated: {generated_at.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append(f"Window: last {since_days} days")
    lines.append("")

    # Top-line summary
    active = sum(1 for h in healths if h.status == SOURCE_STATUS_ACTIVE)
    supported = sum(1 for h in healths if h.status == SOURCE_STATUS_SUPPORTED)
    not_supported = sum(
        1 for h in healths if h.status == SOURCE_STATUS_NOT_SUPPORTED
    )
    total = len(healths)
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Sources known to SHIP : **{total}**")
    lines.append(f"- Active (collector + events in window) : **{active}**")
    lines.append(
        f"- Supported but no events in window : **{supported}**"
    )
    lines.append(f"- Not supported (audit-flagged gap) : **{not_supported}**")
    lines.append("")

    # Group by category, preserving the order in CATEGORY_DISPLAY.
    by_category: dict[str, list[SourceHealth]] = {}
    for health in healths:
        by_category.setdefault(health.entry.category, []).append(health)

    for category in CATEGORY_DISPLAY:
        rows = by_category.get(category) or []
        if not rows:
            continue
        lines.append(f"## {CATEGORY_DISPLAY[category]}")
        lines.append("")
        for health in rows:
            lines.extend(_render_source_block(health))
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_Generated by `ship1000x coverage`. Report contains only event-derived "
        "metadata (event counts, ISO dates, distribution counters, dollar "
        "totals from the published rate card); no event payloads, "
        "attributes, conversation bodies, file paths, or credential markers "
        "are included._"
    )
    return "\n".join(lines) + "\n"


def _probe_drop_freshness(drop_subpath: str) -> dict | None:
    """Return ``{newest_iso, file_count}`` for a drop dir, or None.

    Looks under ``~/.ship1000x/<drop_subpath>/*.jsonl`` and reports the
    newest mtime + how many drop files exist. Best-effort — any OSError
    returns None so the coverage report stays robust.
    """
    if not drop_subpath:
        return None
    from pathlib import Path
    drop_dir = Path.home() / ".ship1000x" / drop_subpath
    try:
        files = sorted(drop_dir.glob("*.jsonl"))
    except OSError:
        return None
    if not files:
        return None
    newest_mtime = 0.0
    for f in files:
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        if m > newest_mtime:
            newest_mtime = m
    if newest_mtime <= 0:
        return None
    iso = datetime.fromtimestamp(newest_mtime, tz=timezone.utc).isoformat()
    return {"newest_iso": iso, "file_count": len(files)}


def _render_source_block(health: SourceHealth) -> list[str]:
    entry = health.entry
    lines: list[str] = []
    if health.status == SOURCE_STATUS_ACTIVE:
        marker = "✅ active"
    elif health.status == SOURCE_STATUS_SUPPORTED:
        marker = "⚠️ supported (no events in window)"
    else:
        marker = "❌ not supported"
    lines.append(f"### `{entry.source_id}` — {entry.display_name}")
    lines.append("")
    lines.append(f"- Status: {marker}")
    if entry.status == SOURCE_STATUS_NOT_SUPPORTED:
        if entry.audit_ref:
            lines.append(f"- Audit reference: {entry.audit_ref}")
        if entry.reason:
            lines.append(f"- Reason: {entry.reason}")
        lines.append("")
        return lines
    if health.status == SOURCE_STATUS_SUPPORTED:
        # For drop-file-based runtime sources, surface the daemon
        # health hint: does the drop dir already contain ticks ?
        freshness = _probe_drop_freshness(entry.drop_subpath)
        if freshness:
            lines.append(
                f"- Daemon health: **{freshness['file_count']}** drop file(s) "
                f"present, newest tick @ `{freshness['newest_iso']}`. "
                f"Run `ship1000x ingest --source {entry.source_id}` to "
                "materialise events into the store."
            )
        else:
            lines.append(
                "- Note: collector available, but no events in the requested window. "
                "Either the user does not use this source on this machine, or "
                "ingestion has not run yet."
            )
        lines.append("")
        return lines

    lines.append(f"- Events: **{health.event_count}**")
    first_day = _format_iso_date(health.first_event)
    last_day = _format_iso_date(health.last_event)
    if first_day and last_day:
        lines.append(f"- First / last event: {first_day} → {last_day}")
    lines.append(f"- Days covered in window: {health.days_covered}")
    if health.unknown_model_count:
        lines.append(
            f"- Unknown-model events: **{health.unknown_model_count}** / {health.event_count}"
        )
    if health.auth_mode_counts:
        lines.append(f"- auth_mode distribution: {_format_counter(health.auth_mode_counts)}")
    if health.cost_basis_counts:
        lines.append(f"- cost_basis distribution: {_format_counter(health.cost_basis_counts)}")
    if health.cost_quality_counts:
        lines.append(
            f"- cost_quality distribution: {_format_counter(health.cost_quality_counts)}"
        )
    if health.api_equivalent_usd or health.billed_estimated_usd:
        lines.append(
            "- Cost totals: "
            f"api_equivalent = {_format_money(health.api_equivalent_usd)}, "
            f"billed_estimated = {_format_money(health.billed_estimated_usd)}"
        )
    lines.append("")
    return lines


def render_json_report(
    healths: list[SourceHealth],
    *,
    since_days: int,
    generated_at: datetime | None = None,
) -> str:
    """Return the coverage report as a JSON document."""
    generated_at = generated_at or datetime.now(timezone.utc)
    payload = {
        "generated_at": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "since_days": since_days,
        "summary": {
            "total": len(healths),
            "active": sum(1 for h in healths if h.status == SOURCE_STATUS_ACTIVE),
            "supported": sum(
                1 for h in healths if h.status == SOURCE_STATUS_SUPPORTED
            ),
            "not_supported": sum(
                1 for h in healths if h.status == SOURCE_STATUS_NOT_SUPPORTED
            ),
        },
        "sources": [
            {
                "source_id": h.entry.source_id,
                "display_name": h.entry.display_name,
                "category": h.entry.category,
                "status": h.status,
                "audit_ref": h.entry.audit_ref or None,
                "reason": h.entry.reason or None,
                "event_count": h.event_count,
                "first_event": h.first_event,
                "last_event": h.last_event,
                "days_covered": h.days_covered,
                "unknown_model_count": h.unknown_model_count,
                "auth_mode_counts": dict(h.auth_mode_counts),
                "cost_basis_counts": dict(h.cost_basis_counts),
                "cost_quality_counts": dict(h.cost_quality_counts),
                "api_equivalent_usd": round(h.api_equivalent_usd, 6),
                "billed_estimated_usd": round(h.billed_estimated_usd, 6),
                # Daemon health surface: drop file freshness for Wave 3
                # foreground writer sources. None when the source has no
                # drop_subpath or the dir is empty.
                "daemon_health": (
                    _probe_drop_freshness(h.entry.drop_subpath)
                    if h.status == SOURCE_STATUS_SUPPORTED
                    and h.entry.drop_subpath
                    else None
                ),
            }
            for h in healths
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
