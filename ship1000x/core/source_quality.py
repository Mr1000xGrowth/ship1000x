"""Source quality audit for Ship1000x.

The dashboard and reports should not present all sources as equally reliable.
This module compares observed local events with the collector-level truth model:
what can be measured natively, what is heuristic, and what is currently missing
from the normalized usage metadata.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from ship1000x.core.cost_truth import event_cost_truth

MeasurementQuality = Literal["factual", "defensible", "indicative", "unknown", "n/a"]
SourceRisk = Literal["ok", "partial", "fragile", "absent", "unknown-source"]
CollectorStage = Literal[
    "default_ingest",
    "opt_in_ingest",
    "billing_api",
    "drop_import",
    "metadata_enrichment",
    "collector_module_only",
    "fixture_only",
    "registry_only",
    "unknown",
]

PUBLIC_UNTRUSTED_COLLECTOR_STAGES = frozenset({
    "collector_module_only",
    "fixture_only",
    "registry_only",
})

QUALITY_KEYS = ("factual", "defensible", "indicative", "unknown")
QUALITY_WEIGHTS: dict[str, float] = {
    "factual": 1.0,
    "defensible": 0.8,
    "indicative": 0.45,
    "unknown": 0.0,
}


@dataclass(frozen=True)
class SourceQualityProfile:
    source: str
    label: str
    collector_stage: CollectorStage
    token_quality: MeasurementQuality
    cost_quality: MeasurementQuality
    active_time_quality: MeasurementQuality
    line_quality: MeasurementQuality = "n/a"
    usage_metadata_expected: bool = False
    caveats: tuple[str, ...] = ()
    next_action: str = "Keep monitoring."


SOURCE_QUALITY_PROFILES: dict[str, SourceQualityProfile] = {
    "claude_code": SourceQualityProfile(
        source="claude_code",
        label="Claude Code",
        collector_stage="default_ingest",
        token_quality="factual",
        cost_quality="factual",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        caveats=(
            "Tokens are native when history JSONL exposes them.",
            # Sub-agent journals (Task tool spawns) live under
            # <project>/subagents/**/*.jsonl and are ingested with the same
            # native-usage quality as top-level sessions (agentId is a
            # strict/reliable identifier — high confidence). Broad-keyword
            # project matching (title/content search across all projects,
            # not just tool-touched paths) is a separate, noisier scope not
            # implemented here — treat any such cross-project estimate as
            # medium confidence, not factual.
            "Sub-agent journals (agentId/isSidechain) are included since 2026-07; "
            "attribution is strict-cwd (high), not broad-keyword (medium).",
        ),
        next_action="Keep Claude usage fixtures aligned with message usage, cache, and model changes.",
    ),
    "codex": SourceQualityProfile(
        source="codex",
        label="Codex CLI",
        collector_stage="default_ingest",
        token_quality="factual",
        cost_quality="factual",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        caveats=("Token quality falls back to unknown when total_token_usage is absent.",),
        next_action="Keep synthetic rollout fixtures current for cache and reasoning tokens.",
    ),
    "codex_sqlite": SourceQualityProfile(
        source="codex_sqlite",
        label="Codex state_5.sqlite (thread structure)",
        collector_stage="metadata_enrichment",
        token_quality="n/a",
        cost_quality="n/a",
        active_time_quality="n/a",
        usage_metadata_expected=False,
        caveats=(
            "Very high confidence: `threads` + `thread_spawn_edges` are a "
            "deterministic local DB read, not a heuristic. Used only to tag "
            "each codex event's agentic_unit (root thread vs sub-agent) and "
            "to reclassify low-confidence project attribution via "
            "git_origin_url — it never creates events or tokens/cost.",
        ),
        next_action="Cross-check root+subagent thread counts against the JSONL session count per project.",
    ),
    "codex_macapp": SourceQualityProfile(
        source="codex_macapp",
        label="Codex macOS app",
        collector_stage="default_ingest",
        token_quality="unknown",
        cost_quality="indicative",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        caveats=("Native tokens are not exposed by the macOS app logs.",),
        next_action="Keep this source clearly marked token-unknown and cost-indicative.",
    ),
    "codex_desktop": SourceQualityProfile(
        source="codex_desktop",
        label="Codex Desktop",
        collector_stage="default_ingest",
        token_quality="unknown",
        cost_quality="indicative",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        caveats=("Desktop traces expose activity but not reliable native token counters.",),
        next_action="Investigate logs_2.sqlite only with separate approval and fixtures first.",
    ),
    "cursor": SourceQualityProfile(
        source="cursor",
        label="Cursor",
        collector_stage="opt_in_ingest",
        token_quality="unknown",
        cost_quality="unknown",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        caveats=("Cursor cost/model extraction is incomplete and workspace mapping can be fragile.",),
        next_action="Keep Cursor token/cost claims explicit unknown until a scalable composer usage source exists.",
    ),
    "cline": SourceQualityProfile(
        source="cline",
        label="Cline",
        collector_stage="default_ingest",
        token_quality="unknown",
        cost_quality="unknown",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        caveats=("Current local metadata is not enough for invoice-grade usage.",),
        next_action="Keep Cline token/cost claims explicit unknown until state.vscdb-backed fixtures exist.",
    ),
    "gemini_cli": SourceQualityProfile(
        source="gemini_cli",
        label="Gemini CLI",
        collector_stage="fixture_only",
        token_quality="unknown",
        cost_quality="unknown",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        caveats=(
            "Fixture-only in this version: no real Gemini CLI logs are read.",
            "Token/cost truth requires a fixture-backed local source or API reconciliation.",
        ),
        next_action="Add a read-only Gemini CLI collector only after synthetic fixtures document the local format.",
    ),
    "copilot_agents": SourceQualityProfile(
        source="copilot_agents",
        label="GitHub Copilot / Copilot Chat",
        collector_stage="fixture_only",
        token_quality="unknown",
        cost_quality="unknown",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        caveats=(
            "Fixture-only in this version: Copilot agent/chat logs are not yet discovered.",
            "Copilot pricing and model aliases need provider-specific validation.",
        ),
        next_action="Wire a read-only Copilot agents discovery layer once a safe local metadata source is identified; the fixture parser already locks the session contract.",
    ),
    "opencode": SourceQualityProfile(
        source="opencode",
        label="OpenCode",
        collector_stage="fixture_only",
        token_quality="unknown",
        cost_quality="unknown",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        caveats=(
            "Fixture-only in this version: OpenCode session files are not collected.",
            "Session schema, token mapping, and pricing provenance are not yet verified.",
        ),
        next_action="Add redacted OpenCode fixtures before implementing a collector.",
    ),
    "cursor_agent": SourceQualityProfile(
        source="cursor_agent",
        label="Cursor Agent",
        collector_stage="fixture_only",
        token_quality="unknown",
        cost_quality="unknown",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        caveats=(
            "Fixture-only in this version: Cursor Agent is separated from generic Cursor claims.",
            "Agent-mode identity and token/cost truth are not yet verified.",
        ),
        next_action="Add Cursor Agent fixtures before merging it into Cursor reporting.",
    ),
    "roo_kilo_code": SourceQualityProfile(
        source="roo_kilo_code",
        label="Roo/Kilo Code variants",
        collector_stage="default_ingest",
        token_quality="unknown",
        cost_quality="unknown",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        caveats=(
            "Roo/Kilo variants are separated from Cline confidence and keep variant identity in usage metadata.",
            "Token/cost truth remains unknown until variant usage and pricing are validated.",
        ),
        next_action="Keep variant fixtures and ingest-path tests aligned before expanding cost claims.",
    ),
    "continue": SourceQualityProfile(
        source="continue",
        label="Continue (continue.dev)",
        collector_stage="fixture_only",
        token_quality="unknown",
        cost_quality="unknown",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        caveats=(
            "Fixture-only in this version: Continue session logs are not discovered.",
            "Continue routes to many providers (OpenAI, Anthropic, Gemini, local); token/cost truth depends on the underlying provider response.",
        ),
        next_action="Wire a read-only Continue session discovery layer once a safe local metadata source is identified; the fixture parser already locks the session contract.",
    ),
    "aider": SourceQualityProfile(
        source="aider",
        label="Aider",
        collector_stage="fixture_only",
        token_quality="unknown",
        cost_quality="unknown",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        caveats=(
            "Fixture-only in this version: Aider chat history and input history are not read.",
            "Aider reports tokens/cost in chat output but the local format and provider aliases need validation.",
        ),
        next_action="Wire a read-only Aider session discovery layer once a safe local metadata source is identified; the fixture parser already locks the session contract.",
    ),
    "openclaw": SourceQualityProfile(
        source="openclaw",
        label="OpenClaw",
        collector_stage="default_ingest",
        token_quality="factual",
        cost_quality="factual",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        next_action="Keep collector fixtures aligned with the local SQLite schema.",
    ),
    "anthropic_usage": SourceQualityProfile(
        source="anthropic_usage",
        label="Anthropic usage API",
        collector_stage="billing_api",
        token_quality="factual",
        cost_quality="factual",
        active_time_quality="n/a",
        usage_metadata_expected=True,
        caveats=("Official billing API validates cost, not local active time.",),
        next_action="Use as reconciliation truth, not as activity truth.",
    ),
    "openai_usage": SourceQualityProfile(
        source="openai_usage",
        label="OpenAI usage API",
        collector_stage="billing_api",
        token_quality="factual",
        cost_quality="factual",
        active_time_quality="n/a",
        usage_metadata_expected=True,
        caveats=("Official usage API validates cost, not local active time.",),
        next_action="Use as reconciliation truth, not as activity truth.",
    ),
    "web_export": SourceQualityProfile(
        source="web_export",
        label="Web export conversations",
        collector_stage="default_ingest",
        token_quality="unknown",
        cost_quality="unknown",
        active_time_quality="n/a",
        caveats=("Manual export events contain aggregate conversation metadata, not provider-native token/cost truth.",),
        next_action="Keep web export claims aggregate-only until provider usage metadata is fixture-backed.",
    ),
    "web_exports": SourceQualityProfile(
        source="web_exports",
        label="Web exports (legacy source id)",
        collector_stage="default_ingest",
        token_quality="unknown",
        cost_quality="unknown",
        active_time_quality="n/a",
        caveats=("Legacy/backward-compatible source id; current collector emits `web_export`.",),
        next_action="Keep manual import fixtures redacted and provider-specific.",
    ),
    "git": SourceQualityProfile(
        source="git",
        label="Git",
        collector_stage="default_ingest",
        token_quality="n/a",
        cost_quality="n/a",
        active_time_quality="n/a",
        line_quality="factual",
        caveats=("Line quality depends on generated/vendored/seed classification.",),
        next_action="Keep line-classification fixtures broad and auditable.",
    ),
    "git_secret_alert": SourceQualityProfile(
        source="git_secret_alert",
        label="Git secret-scan alerts",
        collector_stage="default_ingest",
        token_quality="n/a",
        cost_quality="n/a",
        active_time_quality="n/a",
        caveats=("Opt-in gitleaks findings are sanitized alerts, not activity or cost truth.",),
        next_action="Keep secret alert metadata sanitized and separate from productivity claims.",
    ),
    "shell": SourceQualityProfile(
        source="shell",
        label="Shell",
        collector_stage="opt_in_ingest",
        token_quality="n/a",
        cost_quality="n/a",
        active_time_quality="indicative",
        caveats=("Shell timestamps are intent signals, not full activity truth.",),
        next_action="Keep disabled/opt-in posture explicit.",
    ),
    "mac_system": SourceQualityProfile(
        source="mac_system",
        label="Mac system",
        collector_stage="opt_in_ingest",
        token_quality="n/a",
        cost_quality="n/a",
        active_time_quality="indicative",
        caveats=("System wake/focus signals are coarse and privacy-sensitive.",),
        next_action="Keep consent and aggregation boundaries visible.",
    ),
    "claude_desktop": SourceQualityProfile(
        source="claude_desktop",
        label="Claude Desktop session sidecars",
        collector_stage="default_ingest",
        token_quality="unknown",
        cost_quality="unknown",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        caveats=("Metadata-only sidecars; tokens and cost remain on Claude Code or provider exports.",),
        next_action="Keep this source metadata-only unless a safe token source is fixture-backed.",
    ),
    "claude_statusline": SourceQualityProfile(
        source="claude_statusline",
        label="Claude Code statusline",
        collector_stage="drop_import",
        token_quality="defensible",
        cost_quality="indicative",
        active_time_quality="defensible",
        usage_metadata_expected=True,
        caveats=("Opt-in drop importer; context-window ticks are not full conversation truth.",),
        next_action="Keep statusline consent, retention, and drop freshness visible.",
    ),
    "agent_runtime": SourceQualityProfile(
        source="agent_runtime",
        label="Agent runtime daemon",
        collector_stage="drop_import",
        token_quality="n/a",
        cost_quality="n/a",
        active_time_quality="indicative",
        usage_metadata_expected=True,
        caveats=("Opt-in drop importer for process/port/MCP presence, not usage or content truth.",),
        next_action="Keep runtime signals aggregated and separate from token/cost claims.",
    ),
    "trace": SourceQualityProfile(
        source="trace",
        label="local usage proxy bridge",
        collector_stage="drop_import",
        token_quality="factual",
        cost_quality="defensible",
        active_time_quality="n/a",
        usage_metadata_expected=True,
        caveats=("Opt-in usage-proxy drop import; trust depends on the separate proxy redaction and consent boundary.",),
        next_action="Keep the usage proxy disabled by default and reconcile proxy usage with billing exports.",
    ),
}


def _empty_quality_counter() -> dict[str, int]:
    return {key: 0 for key in QUALITY_KEYS}


def _counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: counter.get(key, 0) for key in QUALITY_KEYS}


def _quality_band(score: float | None) -> str:
    if score is None:
        return "not_measured"
    if score >= 90:
        return "high"
    if score >= 70:
        return "medium"
    if score >= 40:
        return "partial"
    return "low"


def _score_quality_counts(
    *,
    counts: Counter[str],
    total: int,
    expected: MeasurementQuality,
    events: int,
) -> float | None:
    if expected == "n/a" or events == 0:
        return None
    if total <= 0:
        return 0.0
    score = 0.0
    for quality, weight in QUALITY_WEIGHTS.items():
        score += counts.get(quality, 0) * weight
    return round(max(0.0, min(100.0, (score / total) * 100.0)), 2)


def _overall_score(scores: dict[str, float | None]) -> float | None:
    measured = [score for score in scores.values() if score is not None]
    if not measured:
        return None
    return round(sum(measured) / len(measured), 2)


def _parse_meta(raw_meta: str | None) -> dict[str, Any]:
    if not raw_meta:
        return {}
    try:
        parsed = json.loads(raw_meta)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _has_valid_provider_policy_snapshot(snapshot: Any) -> bool:
    """Return whether usage metadata carries a minimal policy snapshot marker.

    SHIP treats this as local evidence only. It does not validate provider
    policy content or certify compliance.
    """
    if not isinstance(snapshot, dict):
        return False
    provider = str(snapshot.get("provider") or "").strip()
    checked_at = str(snapshot.get("checked_at") or snapshot.get("checked_date") or "").strip()
    status = str(
        snapshot.get("status")
        or snapshot.get("risk_label")
        or snapshot.get("allowed_usage_claim")
        or ""
    ).strip()
    return bool(provider and checked_at and status)


def _risk_for_row(
    *,
    profile: SourceQualityProfile | None,
    events: int,
    usage_events: int,
    observed_unknown: int,
) -> SourceRisk:
    if profile is None:
        return "unknown-source"
    if events == 0:
        return "absent"
    if profile.usage_metadata_expected and usage_events == 0:
        return "fragile"
    if observed_unknown:
        return "partial"
    if profile.token_quality in {"unknown", "indicative"} or profile.cost_quality in {"unknown", "indicative"}:
        return "partial"
    return "ok"


def _profile_expectations(profile: SourceQualityProfile | None) -> dict[str, str]:
    if profile is None:
        return {
            "tokens": "unknown",
            "cost": "unknown",
            "active_time": "unknown",
            "lines": "unknown",
        }
    return {
        "tokens": profile.token_quality,
        "cost": profile.cost_quality,
        "active_time": profile.active_time_quality,
        "lines": profile.line_quality,
    }


def _collector_stage(profile: SourceQualityProfile | None) -> CollectorStage:
    return profile.collector_stage if profile else "unknown"


def build_source_quality_report(storage, window_days: int = 30) -> dict[str, Any]:
    """Build an audit report from already-stored events only.

    The report intentionally returns counts, quality labels, and static caveats.
    It never returns raw_meta, prompts, responses, paths, diffs, commands, or
    provider payloads.
    """
    rows = storage.query(
        """SELECT source, confidence_flag, token_input, token_output,
                  cost_estimated, raw_meta
             FROM events
             WHERE date(started_at) >= date('now', ? || ' days')""",
        (f"-{int(window_days)}",),
    )

    observed: dict[str, dict[str, Any]] = {
        source: {
            "source": source,
            "events": 0,
            "usage_events": 0,
            "token_input": 0,
            "token_output": 0,
            "cost_estimated": 0.0,
            "confidence": Counter(),
            "token_quality": Counter(),
            "cost_quality": Counter(),
            "active_time_quality": Counter(),
            "pricing_versions": set(),
            "clients": set(),
            "models": set(),
            "auth_modes": Counter(),
            "api_equivalent_cost": 0.0,
            "billed_estimated_cost": 0.0,
            "unknown_billing_basis_cost": 0.0,
            "events_with_cost_truth": 0,
            "events_with_unknown_billing_basis": 0,
            "provider_policy_snapshot_events": 0,
            "invalid_provider_policy_snapshot_events": 0,
        }
        for source in SOURCE_QUALITY_PROFILES
    }

    for row in rows:
        source = row["source"] or "unknown"
        if source not in observed:
            observed[source] = {
                "source": source,
                "events": 0,
                "usage_events": 0,
                "token_input": 0,
                "token_output": 0,
                "cost_estimated": 0.0,
                "confidence": Counter(),
                "token_quality": Counter(),
                "cost_quality": Counter(),
                "active_time_quality": Counter(),
                "pricing_versions": set(),
                "clients": set(),
                "models": set(),
                "auth_modes": Counter(),
                "api_equivalent_cost": 0.0,
                "billed_estimated_cost": 0.0,
                "unknown_billing_basis_cost": 0.0,
                "events_with_cost_truth": 0,
                "events_with_unknown_billing_basis": 0,
                "provider_policy_snapshot_events": 0,
                "invalid_provider_policy_snapshot_events": 0,
            }
        entry = observed[source]
        entry["events"] += 1
        entry["confidence"][row["confidence_flag"] or "unknown"] += 1
        entry["token_input"] += int(row["token_input"] or 0)
        entry["token_output"] += int(row["token_output"] or 0)
        stored_cost = float(row["cost_estimated"] or 0.0)
        entry["cost_estimated"] += stored_cost

        meta = _parse_meta(row["raw_meta"])
        usage = meta.get("usage")
        if not isinstance(usage, dict):
            if stored_cost > 0:
                entry["unknown_billing_basis_cost"] += stored_cost
                entry["events_with_unknown_billing_basis"] += 1
            continue
        entry["usage_events"] += 1
        quality = usage.get("quality") if isinstance(usage.get("quality"), dict) else {}
        cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
        token_quality = str(quality.get("tokens") or "unknown")
        cost_quality = str(quality.get("cost") or cost.get("quality") or "unknown")
        active_time_quality = str(quality.get("active_time") or "unknown")
        entry["token_quality"][token_quality] += 1
        entry["cost_quality"][cost_quality] += 1
        entry["active_time_quality"][active_time_quality] += 1
        if usage.get("client"):
            entry["clients"].add(str(usage["client"]))
        if usage.get("model_canonical") or usage.get("model_raw"):
            entry["models"].add(str(usage.get("model_canonical") or usage.get("model_raw")))
        if cost.get("pricing_version"):
            entry["pricing_versions"].add(str(cost["pricing_version"]))
        policy_snapshot = usage.get("provider_policy_snapshot")
        if _has_valid_provider_policy_snapshot(policy_snapshot):
            entry["provider_policy_snapshot_events"] += 1
        elif "provider_policy_snapshot" in usage and policy_snapshot is not None and policy_snapshot != "":
            entry["invalid_provider_policy_snapshot_events"] += 1
        truth = event_cost_truth(stored_cost=stored_cost, meta=meta)
        auth_mode = usage.get("auth_mode") or cost.get("auth_mode") or meta.get("auth_mode")
        if auth_mode:
            entry["auth_modes"][str(auth_mode)] += 1
        entry["api_equivalent_cost"] += truth.api_equivalent_usd
        entry["billed_estimated_cost"] += truth.billed_estimated_usd
        entry["unknown_billing_basis_cost"] += truth.unknown_billing_basis_usd
        entry["events_with_cost_truth"] += truth.events_with_cost_truth
        entry["events_with_unknown_billing_basis"] += truth.events_with_unknown_billing_basis

    report_rows = []
    for source, entry in observed.items():
        profile = SOURCE_QUALITY_PROFILES.get(source)
        unknown_observed = entry["token_quality"].get("unknown", 0) + entry["cost_quality"].get("unknown", 0)
        expected_quality = _profile_expectations(profile)
        quality_scores = {
            "tokens": _score_quality_counts(
                counts=entry["token_quality"],
                total=entry["usage_events"],
                expected=expected_quality["tokens"],  # type: ignore[arg-type]
                events=entry["events"],
            ),
            "cost": _score_quality_counts(
                counts=entry["cost_quality"],
                total=entry["usage_events"],
                expected=expected_quality["cost"],  # type: ignore[arg-type]
                events=entry["events"],
            ),
            "active_time": _score_quality_counts(
                counts=entry["active_time_quality"],
                total=entry["usage_events"],
                expected=expected_quality["active_time"],  # type: ignore[arg-type]
                events=entry["events"],
            ),
        }
        quality_scores["overall"] = _overall_score(quality_scores)
        risk = _risk_for_row(
            profile=profile,
            events=entry["events"],
            usage_events=entry["usage_events"],
            observed_unknown=unknown_observed,
        )
        report_rows.append(
            {
                "source": source,
                "label": profile.label if profile else source,
                "risk": risk,
                "events": entry["events"],
                "usage_events": entry["usage_events"],
                "usage_coverage_pct": round(
                    (entry["usage_events"] / entry["events"] * 100.0) if entry["events"] else 0.0,
                    1,
                ),
                "token_input": entry["token_input"],
                "token_output": entry["token_output"],
                "cost_estimated": round(entry["cost_estimated"], 4),
                "confidence": dict(entry["confidence"]),
                "observed_quality": {
                    "tokens": _counter_dict(entry["token_quality"]),
                    "cost": _counter_dict(entry["cost_quality"]),
                    "active_time": _counter_dict(entry["active_time_quality"]),
                },
                "expected_quality": expected_quality,
                "collector_stage": _collector_stage(profile),
                "collector_stage_untrusted_for_public_claims": _collector_stage(profile)
                in PUBLIC_UNTRUSTED_COLLECTOR_STAGES,
                "quality_scores": quality_scores,
                "quality_band": _quality_band(quality_scores["overall"]),
                "usage_metadata_expected": bool(profile and profile.usage_metadata_expected),
                "clients": sorted(entry["clients"]),
                "models": sorted(entry["models"]),
                "pricing_versions": sorted(entry["pricing_versions"]),
                "auth_mode_counts": dict(sorted(entry["auth_modes"].items())),
                "provider_policy_snapshot_events": entry["provider_policy_snapshot_events"],
                "invalid_provider_policy_snapshot_events": entry[
                    "invalid_provider_policy_snapshot_events"
                ],
                "missing_provider_policy_snapshot_events": max(
                    0,
                    entry["usage_events"]
                    - entry["provider_policy_snapshot_events"]
                    - entry["invalid_provider_policy_snapshot_events"],
                ),
                "provider_policy_snapshot_coverage_pct": round(
                    (
                        entry["provider_policy_snapshot_events"]
                        / entry["usage_events"]
                        * 100.0
                    )
                    if entry["usage_events"]
                    else 0.0,
                    1,
                ),
                "cost_truth": {
                    "api_equivalent_usd": round(entry["api_equivalent_cost"], 6),
                    "billed_estimated_usd": round(entry["billed_estimated_cost"], 6),
                    "subscription_absorbed_usd": round(
                        max(0.0, entry["api_equivalent_cost"] - entry["billed_estimated_cost"]),
                        6,
                    ),
                    "unknown_billing_basis_usd": round(entry["unknown_billing_basis_cost"], 6),
                    "events_with_cost_truth": entry["events_with_cost_truth"],
                    "events_with_unknown_billing_basis": entry["events_with_unknown_billing_basis"],
                },
                "caveats": list(profile.caveats) if profile else ["Source is not in the Ship1000x quality registry."],
                "next_action": profile.next_action if profile else "Add a source quality profile before trusting this source.",
            }
        )

    risk_order = {"fragile": 0, "partial": 1, "unknown-source": 2, "ok": 3, "absent": 4}
    report_rows.sort(key=lambda r: (risk_order.get(r["risk"], 9), -r["events"], r["source"]))
    observed_rows = [row for row in report_rows if row["events"] > 0]
    observed_scores = [
        row["quality_scores"]["overall"]
        for row in observed_rows
        if row["quality_scores"]["overall"] is not None
    ]
    cost_truth_summary = {
        "api_equivalent_usd": round(
            sum(row["cost_truth"]["api_equivalent_usd"] for row in observed_rows),
            6,
        ),
        "billed_estimated_usd": round(
            sum(row["cost_truth"]["billed_estimated_usd"] for row in observed_rows),
            6,
        ),
        "subscription_absorbed_usd": round(
            sum(row["cost_truth"]["subscription_absorbed_usd"] for row in observed_rows),
            6,
        ),
        "unknown_billing_basis_usd": round(
            sum(row["cost_truth"]["unknown_billing_basis_usd"] for row in observed_rows),
            6,
        ),
        "events_with_cost_truth": sum(
            row["cost_truth"]["events_with_cost_truth"] for row in observed_rows
        ),
        "events_with_unknown_billing_basis": sum(
            row["cost_truth"]["events_with_unknown_billing_basis"] for row in observed_rows
        ),
    }
    provider_policy_snapshot_events = sum(
        row["provider_policy_snapshot_events"] for row in observed_rows
    )
    invalid_provider_policy_snapshot_events = sum(
        row["invalid_provider_policy_snapshot_events"] for row in observed_rows
    )
    missing_provider_policy_snapshot_events = sum(
        row["missing_provider_policy_snapshot_events"] for row in observed_rows
    )
    usage_events_total = sum(row["usage_events"] for row in observed_rows)
    missing_usage = [
        row["source"]
        for row in observed_rows
        if row["usage_metadata_expected"] and row["usage_events"] == 0
    ]

    return {
        "schema_version": "ship1000x.source_quality_report.v1",
        "window_days": int(window_days),
        "summary": {
            "known_sources": len(SOURCE_QUALITY_PROFILES),
            "observed_sources": len(observed_rows),
            "observed_unknown_sources": len([row for row in observed_rows if row["risk"] == "unknown-source"]),
            "absent_known_sources": len(
                [row for row in report_rows if row["events"] == 0 and row["source"] in SOURCE_QUALITY_PROFILES]
            ),
            "sources_missing_usage_metadata": len(missing_usage),
            "fragile_sources": len([row for row in observed_rows if row["risk"] == "fragile"]),
            "partial_sources": len([row for row in observed_rows if row["risk"] == "partial"]),
            "collector_stage_counts": dict(
                sorted(Counter(row["collector_stage"] for row in observed_rows).items())
            ),
            "public_untrusted_stage_sources": len(
                [
                    row
                    for row in observed_rows
                    if row["collector_stage"] in PUBLIC_UNTRUSTED_COLLECTOR_STAGES
                ]
            ),
            "usage_events": usage_events_total,
            "average_observed_quality_score": (
                round(sum(observed_scores) / len(observed_scores), 2) if observed_scores else None
            ),
            "low_quality_sources": len(
                [
                    row
                    for row in observed_rows
                    if row["quality_scores"]["overall"] is not None
                    and row["quality_scores"]["overall"] < 70
                ]
            ),
            "cost_truth": cost_truth_summary,
            "provider_policy_snapshot_events": provider_policy_snapshot_events,
            "invalid_provider_policy_snapshot_events": invalid_provider_policy_snapshot_events,
            "missing_provider_policy_snapshot_events": missing_provider_policy_snapshot_events,
            "provider_policy_snapshot_coverage_pct": round(
                (provider_policy_snapshot_events / usage_events_total * 100.0)
                if usage_events_total
                else 0.0,
                1,
            ),
        },
        "missing_usage_metadata_sources": missing_usage,
        "rows": report_rows,
    }
