"""Historical recoverability audit for Ship1000x.

The audit answers a narrow public-trust question: if old SHIP rows look wrong,
can SHIP repair them from local/source evidence, or are they only explainable
with caveats? It reads only aggregate event metadata already stored in SHIP.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

Recoverability = Literal[
    "recoverable",
    "partial",
    "external-source-required",
    "not-recoverable-from-ship",
]


@dataclass(frozen=True)
class HistoryRecoverabilityProfile:
    source: str
    label: str
    recoverability: Recoverability
    repairable_fields: tuple[str, ...]
    unrecoverable_if_missing: tuple[str, ...]
    command: str
    caveat: str


PROFILES: dict[str, HistoryRecoverabilityProfile] = {
    "claude_code": HistoryRecoverabilityProfile(
        source="claude_code",
        label="Claude Code",
        recoverability="recoverable",
        repairable_fields=("project_id", "native tokens", "cache tokens", "cost", "usage metadata"),
        unrecoverable_if_missing=("original Claude Code JSONL with message.usage",),
        command="ship1000x reclassify --since 365d",
        caveat="Recoverable only while local Claude Code history files still exist and expose usage blocks.",
    ),
    "codex": HistoryRecoverabilityProfile(
        source="codex",
        label="Codex CLI",
        recoverability="recoverable",
        repairable_fields=("project_id", "native tokens", "cache tokens", "reasoning tokens", "cost"),
        unrecoverable_if_missing=("original Codex rollout/session files with total_token_usage",),
        command="ship1000x reclassify --since 365d",
        caveat="Recoverable only from retained local Codex metadata; Desktop/macOS variants are weaker.",
    ),
    "git": HistoryRecoverabilityProfile(
        source="git",
        label="Git",
        recoverability="recoverable",
        repairable_fields=("project_id", "line classification", "generated/vendored/seed split"),
        unrecoverable_if_missing=("local git repository history",),
        command="ship1000x reclassify --since 365d",
        caveat="Git can repair line semantics only while the referenced repositories are still available locally.",
    ),
    "openclaw": HistoryRecoverabilityProfile(
        source="openclaw",
        label="OpenClaw",
        recoverability="recoverable",
        repairable_fields=("project_id", "native tokens", "cost", "usage metadata"),
        unrecoverable_if_missing=("original OpenClaw local SQLite store",),
        command="rerun the OpenClaw collector, then run ship1000x reclassify --since 365d",
        caveat="Recoverable only while the local OpenClaw store still exists and exposes the same schema.",
    ),
    "claude_desktop": HistoryRecoverabilityProfile(
        source="claude_desktop",
        label="Claude Desktop session sidecars",
        recoverability="partial",
        repairable_fields=("project_id", "session metadata", "activity windows"),
        unrecoverable_if_missing=("native token counters", "invoice-grade cost", "deleted sidecar files"),
        command="rerun the Claude Desktop sidecar collector, then run ship1000x reclassify --since 365d",
        caveat="Metadata sidecars can repair aggregate activity, not Claude Code token/cost truth.",
    ),
    "claude_statusline": HistoryRecoverabilityProfile(
        source="claude_statusline",
        label="Claude Code statusline",
        recoverability="partial",
        repairable_fields=("context-window usage metadata", "model hints", "compact boundaries"),
        unrecoverable_if_missing=("deleted statusline drop files", "full prompt/message contents"),
        command="rerun ship1000x ingest --source claude_statusline if retained drop files still exist",
        caveat="Statusline drops can repair context-window signals, not full conversation or billing truth.",
    ),
    "codex_desktop": HistoryRecoverabilityProfile(
        source="codex_desktop",
        label="Codex Desktop",
        recoverability="partial",
        repairable_fields=("project_id", "activity windows", "available model hints"),
        unrecoverable_if_missing=("native token counters", "invoice-grade cost"),
        command="ship1000x reclassify --since 365d",
        caveat="Useful for activity history; token/cost truth remains partial unless native usage appears.",
    ),
    "codex_macapp": HistoryRecoverabilityProfile(
        source="codex_macapp",
        label="Codex macOS app",
        recoverability="partial",
        repairable_fields=("project_id", "activity windows", "available model hints"),
        unrecoverable_if_missing=("native token counters", "invoice-grade cost"),
        command="ship1000x reclassify --since 365d",
        caveat="Useful for activity history; token/cost truth remains partial unless native usage appears.",
    ),
    "cursor": HistoryRecoverabilityProfile(
        source="cursor",
        label="Cursor",
        recoverability="partial",
        repairable_fields=("project_id", "activity/source coverage"),
        unrecoverable_if_missing=("reliable token/cost truth",),
        command="ship1000x reclassify --since 365d",
        caveat="Cursor token/cost claims remain unknown until a stronger fixture-backed source exists.",
    ),
    "cline": HistoryRecoverabilityProfile(
        source="cline",
        label="Cline",
        recoverability="partial",
        repairable_fields=("project_id", "activity/source coverage"),
        unrecoverable_if_missing=("reliable token/cost truth", "variant-specific identity"),
        command="ship1000x reclassify --since 365d",
        caveat="Do not infer Roo/Kilo/Cline variants from generic Cline rows.",
    ),
    "roo_kilo_code": HistoryRecoverabilityProfile(
        source="roo_kilo_code",
        label="Roo/Kilo Code variants",
        recoverability="partial",
        repairable_fields=("project_id", "activity/source coverage", "variant identity"),
        unrecoverable_if_missing=("reliable token/cost truth", "deleted extension task metadata"),
        command="rerun ship1000x ingest --source roo_kilo_code, then run ship1000x reclassify --since 365d",
        caveat="Variant activity is repairable from retained task metadata; token/cost truth remains unknown.",
    ),
    "web_exports": HistoryRecoverabilityProfile(
        source="web_exports",
        label="Web exports (legacy source id)",
        recoverability="partial",
        repairable_fields=("aggregate conversation history", "project_id when export metadata allows it"),
        unrecoverable_if_missing=("deleted export files", "provider-native billing truth"),
        command="re-import the redacted export, then run ship1000x reclassify --since 365d",
        caveat="Manual exports can repair aggregate history but are not live or billing-authoritative.",
    ),
    "web_export": HistoryRecoverabilityProfile(
        source="web_export",
        label="Web export conversations",
        recoverability="partial",
        repairable_fields=("aggregate conversation history", "project_id when export metadata allows it"),
        unrecoverable_if_missing=("deleted export files", "provider-native billing truth"),
        command="re-import the redacted export, then run ship1000x reclassify --since 365d",
        caveat="Manual exports can repair aggregate history but are not live or billing-authoritative.",
    ),
    "git_secret_alert": HistoryRecoverabilityProfile(
        source="git_secret_alert",
        label="Git secret-scan alerts",
        recoverability="recoverable",
        repairable_fields=("secret alert metadata", "project_id"),
        unrecoverable_if_missing=("local git repository history", "same gitleaks rules/config"),
        command="rerun the opt-in gitleaks-enabled git collector, then run ship1000x reclassify --since 365d",
        caveat="Recoverable only while the repository history and scan rules are still available.",
    ),
    "anthropic_usage": HistoryRecoverabilityProfile(
        source="anthropic_usage",
        label="Anthropic usage API",
        recoverability="external-source-required",
        repairable_fields=("daily billing-side tokens", "daily billing-side cost"),
        unrecoverable_if_missing=("authorized Admin API/export access for that historical window",),
        command="re-run the opt-in usage collector with valid Admin credentials",
        caveat="Official usage validates billing, not local activity or project attribution.",
    ),
    "openai_usage": HistoryRecoverabilityProfile(
        source="openai_usage",
        label="OpenAI usage API",
        recoverability="external-source-required",
        repairable_fields=("daily billing-side tokens", "daily billing-side cost"),
        unrecoverable_if_missing=("authorized Admin API/export access for that historical window",),
        command="re-run the opt-in usage collector with valid Admin credentials",
        caveat="Official usage validates billing, not local activity or project attribution.",
    ),
    "shell": HistoryRecoverabilityProfile(
        source="shell",
        label="Shell",
        recoverability="partial",
        repairable_fields=("project_id", "git-intent commands", "coarse activity timing"),
        unrecoverable_if_missing=("shell history with timestamps", "commands excluded by privacy settings"),
        command="rerun the opt-in shell collector, then run ship1000x reclassify --since 365d",
        caveat="Shell history can repair coarse intent signals, not complete activity or AI usage.",
    ),
    "mac_system": HistoryRecoverabilityProfile(
        source="mac_system",
        label="Mac system",
        recoverability="partial",
        repairable_fields=("wake/sleep/unlock timing", "coarse activity context"),
        unrecoverable_if_missing=("expired macOS unified logs", "missing permissions"),
        command="rerun the opt-in mac_system collector, then run ship1000x reclassify --since 365d",
        caveat="macOS signals are coarse and privacy-sensitive; they never repair token/cost truth.",
    ),
    "agent_runtime": HistoryRecoverabilityProfile(
        source="agent_runtime",
        label="Agent runtime daemon",
        recoverability="partial",
        repairable_fields=("process presence", "port/MCP aggregate metadata"),
        unrecoverable_if_missing=("deleted watch drop files", "raw process ticks beyond retained aggregates"),
        command="rerun ship1000x ingest --source agent_runtime if retained drop files still exist",
        caveat="Runtime drops repair process/port presence only, not usage or content truth.",
    ),
    "trace": HistoryRecoverabilityProfile(
        source="trace",
        label="local usage proxy bridge",
        recoverability="partial",
        repairable_fields=("provider/model/token metadata captured by the proxy", "api_call rows"),
        unrecoverable_if_missing=("deleted usage-proxy drop files", "requests never routed through the proxy"),
        command="rerun ship1000x ingest --source trace if retained proxy drops still exist",
        caveat="The proxy bridge repairs only explicitly captured proxy/drop metadata; it must stay opt-in.",
    ),
}


def _parse_meta(raw_meta: str | None) -> dict[str, Any]:
    if not raw_meta:
        return {}
    try:
        parsed = json.loads(raw_meta)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _profile_for_source(source: str) -> HistoryRecoverabilityProfile:
    return PROFILES.get(
        source,
        HistoryRecoverabilityProfile(
            source=source,
            label=source,
            recoverability="not-recoverable-from-ship",
            repairable_fields=(),
            unrecoverable_if_missing=("registered source profile", "fixture-backed collector contract"),
            command="add a source quality profile and fixture-backed collector before trusting history",
            caveat="Unknown sources are visible but not repairable from SHIP semantics alone.",
        ),
    )


def _recommendation(
    profile: HistoryRecoverabilityProfile,
    *,
    events: int,
    usage_events: int,
    malformed_meta_events: int,
) -> str:
    if events == 0:
        return "No observed rows in this window."
    if malformed_meta_events:
        return "Inspect collector/version first: some rows have malformed metadata."
    if usage_events == 0 and profile.recoverability in {"recoverable", "partial"}:
        return "Run reclassify only if original source files still exist; the DB alone cannot recreate usage metadata."
    if profile.recoverability == "external-source-required":
        return "Use only with explicit provider credentials/export approval; do not infer local activity from billing rows."
    if profile.recoverability == "not-recoverable-from-ship":
        return "Treat as visible evidence only; add a profile before making public claims."
    return "Safe to attempt the listed repair path, then rerun source-audit and public-check."


def _repair_scope(profile: HistoryRecoverabilityProfile) -> str:
    if profile.recoverability == "recoverable":
        return "listed-fields-from-retained-source"
    if profile.recoverability == "partial":
        return "partial-metadata-or-activity-only"
    if profile.recoverability == "external-source-required":
        return "external-provider-export-only"
    return "visible-evidence-only"


def _claim_boundary(profile: HistoryRecoverabilityProfile) -> str:
    if profile.recoverability == "recoverable":
        return (
            "Claim repair only for listed fields and only while the original local/source "
            "evidence still exists; missing evidence remains unrecoverable."
        )
    if profile.recoverability == "partial":
        return (
            "Do not claim full historical repair: SHIP can repair only the listed "
            "metadata/activity fields, not token, cost, billing, or deleted-source truth."
        )
    if profile.recoverability == "external-source-required":
        return (
            "Do not claim local repair: billing/token truth requires explicit provider "
            "export/API access and still does not prove local activity attribution."
        )
    return (
        "Do not claim repair/backfill from SHIP alone; this source is visible evidence "
        "until a governed profile and fixture-backed collector contract exist."
    )


def _strict_history_blocking_reasons(
    profile: HistoryRecoverabilityProfile,
    *,
    malformed_meta_events: int,
) -> list[str]:
    reasons: list[str] = []
    if profile.recoverability == "partial":
        reasons.append("partially recoverable")
    elif profile.recoverability == "external-source-required":
        reasons.append("external source required")
    elif profile.recoverability == "not-recoverable-from-ship":
        reasons.append("not recoverable from SHIP semantics alone")
    if malformed_meta_events:
        reasons.append("malformed metadata")
    return reasons


def strict_history_failures_from_report(report: dict[str, Any]) -> list[str]:
    """Return the public-check strict-history failure list for a history report."""
    failures: list[str] = []
    for row in report.get("rows", []):
        source = str(row.get("source") or "unknown")
        for reason in row.get("strict_history_blocking_reasons") or []:
            failures.append(f"{source}: {reason}")
    return failures


def build_history_recoverability_report(storage, window_days: int = 365) -> dict[str, Any]:
    """Build a safe historical recoverability report from stored metadata."""
    rows = storage.query(
        """SELECT source, raw_meta
             FROM events
             WHERE date(started_at) >= date('now', ? || ' days')""",
        (f"-{int(window_days)}",),
    )

    observed: dict[str, dict[str, Any]] = {}
    for row in rows:
        source = row["source"] or "unknown"
        entry = observed.setdefault(
            source,
            {
                "events": 0,
                "usage_events": 0,
                "token_events": 0,
                "cost_events": 0,
                "auth_mode_events": 0,
                "malformed_meta_events": 0,
                "quality": Counter(),
            },
        )
        entry["events"] += 1
        meta = _parse_meta(row["raw_meta"])
        if row["raw_meta"] and not meta:
            entry["malformed_meta_events"] += 1
        usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
        if not usage:
            continue
        entry["usage_events"] += 1
        if isinstance(usage.get("tokens"), dict):
            entry["token_events"] += 1
        if isinstance(usage.get("cost"), dict):
            entry["cost_events"] += 1
        if usage.get("auth_mode") or (isinstance(usage.get("cost"), dict) and usage["cost"].get("auth_mode")):
            entry["auth_mode_events"] += 1
        quality = usage.get("quality") if isinstance(usage.get("quality"), dict) else {}
        if quality.get("tokens"):
            entry["quality"][str(quality["tokens"])] += 1
        if quality.get("cost"):
            entry["quality"][str(quality["cost"])] += 1

    report_rows: list[dict[str, Any]] = []
    for source, entry in sorted(observed.items()):
        profile = _profile_for_source(source)
        strict_reasons = _strict_history_blocking_reasons(
            profile,
            malformed_meta_events=entry["malformed_meta_events"],
        )
        report_rows.append(
            {
                "source": source,
                "label": profile.label,
                "recoverability": profile.recoverability,
                "events": entry["events"],
                "usage_events": entry["usage_events"],
                "token_events": entry["token_events"],
                "cost_events": entry["cost_events"],
                "auth_mode_events": entry["auth_mode_events"],
                "malformed_meta_events": entry["malformed_meta_events"],
                "strict_history_blocking": bool(strict_reasons),
                "strict_history_blocking_reasons": strict_reasons,
                "observed_quality_counts": dict(sorted(entry["quality"].items())),
                "repair_scope": _repair_scope(profile),
                "claim_boundary": _claim_boundary(profile),
                "repairable_fields": list(profile.repairable_fields),
                "unrecoverable_truth_fields": list(profile.unrecoverable_if_missing),
                "unrecoverable_if_missing": list(profile.unrecoverable_if_missing),
                "command": profile.command,
                "caveat": profile.caveat,
                "recommendation": _recommendation(
                    profile,
                    events=entry["events"],
                    usage_events=entry["usage_events"],
                    malformed_meta_events=entry["malformed_meta_events"],
                ),
            }
        )

    recoverability_counts = Counter(row["recoverability"] for row in report_rows)
    strict_history_failures = [
        failure
        for row in report_rows
        for failure in (
            f"{row['source']}: {reason}"
            for reason in row["strict_history_blocking_reasons"]
        )
    ]
    return {
        "schema_version": "ship1000x.history_recoverability_report.v1",
        "window_days": int(window_days),
        "summary": {
            "observed_sources": len(report_rows),
            "events": sum(row["events"] for row in report_rows),
            "usage_events": sum(row["usage_events"] for row in report_rows),
            "recoverable_sources": recoverability_counts["recoverable"],
            "partial_sources": recoverability_counts["partial"],
            "external_source_required_sources": recoverability_counts["external-source-required"],
            "not_recoverable_from_ship_sources": recoverability_counts["not-recoverable-from-ship"],
            "malformed_meta_events": sum(row["malformed_meta_events"] for row in report_rows),
            "strict_history_blocking_sources": len(
                [row for row in report_rows if row["strict_history_blocking"]]
            ),
        },
        "rows": report_rows,
        "strict_history_failures": strict_history_failures,
        "operator_guidance": (
            "`history-audit` is read-only. Run repair commands only after confirming "
            "the original local source files or explicit provider exports still exist."
        ),
    }
