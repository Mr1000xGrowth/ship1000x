"""Observation coverage audit for Ship1000x.

This module turns an internal observability audit into a small,
machine-readable local checklist. It is deliberately conservative: it reports
capabilities and gaps, not raw event contents.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from ship1000x.core.source_quality import SOURCE_QUALITY_PROFILES

ObservationStatus = Literal["implemented", "partial", "planned", "deferred"]
ProviderGapStatus = Literal["supported", "partial", "needs-fixture", "planned", "deferred"]


@dataclass(frozen=True)
class ObservationCapability:
    key: str
    category: str
    label: str
    status: ObservationStatus
    evidence: str
    gap: str
    next_action: str


@dataclass(frozen=True)
class ProviderCapabilityGap:
    key: str
    label: str
    status: ProviderGapStatus
    current_coverage: str
    missing: str
    reference: str
    next_action: str
    public_claim: str


CAPABILITIES: tuple[ObservationCapability, ...] = (
    ObservationCapability(
        key="batch_local_usage",
        category="batch_local_usage",
        label="Batch/local AI and dev usage",
        status="implemented",
        evidence="Collectors, source quality audit, usage metadata, pricing provenance.",
        gap="Coverage is strongest for Claude Code, Codex CLI, git, and official usage exports.",
        next_action="Keep source-audit green and add missing provider fixtures before trusting new sources.",
    ),
    ObservationCapability(
        key="codex_reliability_split",
        category="batch_local_usage",
        label="Codex CLI/Desktop/macOS reliability split",
        status="implemented",
        evidence="Codex CLI can be token-factual; Desktop/macOS are token-unknown and cost-indicative.",
        gap="Desktop/macOS do not expose native token counters.",
        next_action="Never merge Codex variants into a single confidence claim.",
    ),
    ObservationCapability(
        key="provider_coverage_registry",
        category="provider_coverage",
        label="Provider/tool coverage registry",
        status="partial",
        evidence="Known source quality profiles exist for current collectors.",
        gap="Gemini, Continue, Aider, Antigravity, Copilot, and other emerging tools are not first-class.",
        next_action="Add fixture-backed profiles before any public claim about those providers.",
    ),
    ObservationCapability(
        key="model_pricing_aliases",
        category="provider_coverage",
        label="Model aliases and pricing provenance",
        status="partial",
        evidence="Static pricing source/version and canonical model helpers exist.",
        gap="Unknown model fallback is still possible and must remain visible.",
        next_action="Report unknown model and fallback pricing counts in audits and downstream importers.",
    ),
    ObservationCapability(
        key="correlation_identity",
        category="correlation",
        label="Human/agent/project/machine correlation",
        status="partial",
        evidence="Project IDs, machine_id, source, activity windows, and unified daily rollups exist.",
        gap="Cross-run/job identity is not yet a shared contract across SHIP and downstream observability tools.",
        next_action="Keep the source.module value stable and add explicit correlation IDs only via fixture-backed contracts.",
    ),
    ObservationCapability(
        key="live_runtime_observability",
        category="live_runtime",
        label="Live runtime/process/rate-limit observability",
        status="planned",
        evidence="Identified as a future live-capture capability, not implemented in SHIP batch collectors.",
        gap="No live context window, rate limit, child process, port, or MCP server observation yet.",
        next_action="Implement live runtime as a separate opt-in aggregated heartbeat, not as raw ticks.",
    ),
    ObservationCapability(
        key="os_human_context",
        category="os_context",
        label="OS human context",
        status="planned",
        evidence="Mac system collector is coarse and opt-in.",
        gap="No privacy-first active app/window/navigation contract beyond coarse local signals.",
        next_action="Require explicit consent and redacted/aggregated metadata before expanding OS context.",
    ),
    ObservationCapability(
        key="api_truth_trace",
        category="api_truth",
        label="API truth/proxy reconciliation",
        status="planned",
        evidence="Official usage exports exist for some providers; the local usage proxy is not implemented.",
        gap="No strict opt-in proxy/API truth mode for ambiguous local logs.",
        next_action="Keep the usage proxy disabled by default and use it only for token/cost reconciliation.",
    ),
    ObservationCapability(
        key="raw_content_capture",
        category="privacy_boundary",
        label="Prompt/response/raw content capture",
        status="deferred",
        evidence="Ship1000x stores metadata and hashes, not prompt/response bodies.",
        gap="Raw content is intentionally out of scope for public-trust metrics.",
        next_action="Do not add raw prompt/response capture to SHIP; use separate consented systems if ever needed.",
    ),
)


PROVIDER_CAPABILITY_GAPS: tuple[ProviderCapabilityGap, ...] = (
    ProviderCapabilityGap(
        key="claude_code",
        label="Claude Code",
        status="supported",
        current_coverage="Local batch usage, source quality profile, token/cost quality from normalized usage when present.",
        missing="Live runtime context, API-truth reconciliation, and complete cross-provider identity.",
        reference="the gap review: batch/local usage is the current SHIP strength.",
        next_action="Keep fixture-backed usage metadata and surface source quality before public claims.",
        public_claim="Safe for batch/local usage claims when source-audit stays green.",
    ),
    ProviderCapabilityGap(
        key="codex_cli",
        label="Codex CLI",
        status="supported",
        current_coverage="Fixture-backed rollout metadata including total_token_usage, cache tokens, reasoning tokens, and pricing provenance.",
        missing="Live context window, rate-limit state, child process, and API-truth reconciliation.",
        reference="the gap review: Codex coverage must split CLI from Desktop/macOS.",
        next_action="Keep CLI factual tokens separate from Desktop/macOS heuristic signals.",
        public_claim="Safe for token/cost claims only when native rollout usage is present.",
    ),
    ProviderCapabilityGap(
        key="codex_desktop_macos",
        label="Codex Desktop/macOS",
        status="partial",
        current_coverage="Metadata-only local signals can support activity coverage and active-time context.",
        missing="Native token counters and factual cost are absent.",
        reference="the gap review: Codex variants do not share the same confidence level.",
        next_action="Continue marking tokens/cost unknown or indicative until fixture-backed native usage exists.",
        public_claim="Do not claim factual tokens or cost for Desktop/macOS.",
    ),
    ProviderCapabilityGap(
        key="cursor",
        label="Cursor",
        status="partial",
        current_coverage="AI block metadata can support activity/source coverage.",
        missing="Reliable token/cost extraction and Cursor Agent separation are incomplete.",
        reference="the prioritized backlog: report Cursor tokens absent.",
        next_action="Add fixture-backed Cursor Agent and token/cost source before stronger claims.",
        public_claim="Activity coverage only; token/cost quality remains unknown.",
    ),
    ProviderCapabilityGap(
        key="cline",
        label="Cline",
        status="partial",
        current_coverage="Task metadata can support source/activity coverage.",
        missing="Token/cost truth and variant split across Roo/Kilo/Cline-style forks.",
        reference="the source catalog: Roo/Kilo Code and Cline variants need explicit coverage.",
        next_action="Add variant fixtures before merging all VS Code agent activity into one confidence claim.",
        public_claim="Activity coverage only; token/cost quality remains unknown.",
    ),
    ProviderCapabilityGap(
        key="web_exports",
        label="ChatGPT/Claude web exports",
        status="partial",
        current_coverage="Manual export/drop-folder workflows can provide aggregated conversation metadata.",
        missing="Provider-native cost truth, live runtime state, and automatic freshness.",
        reference="the gap review: web exports are present but not API truth.",
        next_action="Keep exports aggregated and avoid presenting them as live or billing-authoritative.",
        public_claim="Safe for aggregate history with caveats, not for live truth.",
    ),
    ProviderCapabilityGap(
        key="gemini_cli",
        label="Gemini CLI",
        status="needs-fixture",
        current_coverage="Source quality profile exists, but no read-only collector or real local format fixture yet.",
        missing="Usage metadata format, model aliases, pricing provenance, and collector privacy boundary tests.",
        reference="the gap review and the source catalog: Gemini CLI is a known missing provider.",
        next_action="Add synthetic Gemini CLI source fixtures before any public support claim.",
        public_claim="Known gap today; registry-only, not collected.",
    ),
    ProviderCapabilityGap(
        key="copilot_agents",
        label="GitHub Copilot / Copilot Chat",
        status="needs-fixture",
        current_coverage="Source quality profile exists, but no read-only collector or real local format fixture yet.",
        missing="Local source model, token/cost provenance, agent/chat split, and pricing aliases.",
        reference="the gap review and the source catalog: Copilot coverage is missing.",
        next_action="Research safe local metadata sources and add synthetic fixtures only.",
        public_claim="Known gap today; registry-only, not collected.",
    ),
    ProviderCapabilityGap(
        key="opencode",
        label="OpenCode",
        status="needs-fixture",
        current_coverage="Source quality profile exists, but no read-only collector or real local format fixture yet.",
        missing="Session schema, token/cost mapping, and pricing provenance.",
        reference="the source catalog: OpenCode is not covered.",
        next_action="Add a redacted fixture and quality profile before public reporting.",
        public_claim="Known gap today; registry-only, not collected.",
    ),
    ProviderCapabilityGap(
        key="roo_kilo_code",
        label="Roo/Kilo Code variants",
        status="partial",
        current_coverage="Dedicated `roo_kilo_code` collector is wired into ingest with variant identity separated from generic Cline.",
        missing="Native token/cost truth and variant pricing provenance remain unknown.",
        reference="the repo audit and the source catalog: Roo/Kilo Code appear in provider inventories.",
        next_action="Keep variant fixtures and ingest-path tests aligned before expanding cost claims.",
        public_claim="Supported for bounded activity metadata with caveats; not invoice-grade token/cost truth.",
    ),
    ProviderCapabilityGap(
        key="cursor_agent",
        label="Cursor Agent",
        status="needs-fixture",
        current_coverage="Source quality profile exists and is separated from broader Cursor metadata.",
        missing="Agent-mode identity, session boundaries, token/cost quality.",
        reference="the repo audit and the gap review: Cursor Agent needs distinct handling.",
        next_action="Add a fixture-backed Cursor Agent profile before claiming support.",
        public_claim="Known gap today; registry-only, not collected.",
    ),
    ProviderCapabilityGap(
        key="continue",
        label="Continue (continue.dev)",
        status="needs-fixture",
        current_coverage="Source quality profile and fixture parser exist; no read-only discovery layer yet.",
        missing="Local session storage discovery, multi-provider usage mapping, and pricing aliases for routed providers.",
        reference="the gap review: Continue routes to many providers and needs explicit handling.",
        next_action="Add a read-only Continue discovery layer; the fixture parser already locks the safe session contract.",
        public_claim="Known gap today; fixture-only, not collected.",
    ),
    ProviderCapabilityGap(
        key="aider",
        label="Aider",
        status="needs-fixture",
        current_coverage="Source quality profile and fixture parser exist; no read-only discovery layer yet.",
        missing="Safe parsing of chat history, input history, and Aider-reported token/cost lines, with provider aliasing.",
        reference="the gap review: Aider exposes its own token/cost output but requires a privacy-safe parser.",
        next_action="Add a read-only Aider discovery layer that ignores raw chat content; the fixture parser already locks the safe session contract.",
        public_claim="Known gap today; fixture-only, not collected.",
    ),
    ProviderCapabilityGap(
        key="antigravity_goose_crush",
        label="Antigravity / Goose / Crush",
        status="planned",
        current_coverage="No first-class SHIP source profiles yet.",
        missing="Provider identity, local metadata shape, pricing model, and privacy tests.",
        reference="the repo audit and the source catalog: these tools appear in open-source provider catalogs.",
        next_action="Track as future provider inventory; do not imply support.",
        public_claim="Not observed by SHIP today.",
    ),
    ProviderCapabilityGap(
        key="kimi_qwen",
        label="Kimi / Qwen",
        status="planned",
        current_coverage="No first-class SHIP source profiles yet.",
        missing="Model aliases, pricing provenance, and local event source.",
        reference="the repo audit and the source catalog: Kimi/Qwen are missing provider families.",
        next_action="Add only with explicit fixture and pricing source/version.",
        public_claim="Not observed by SHIP today.",
    ),
    ProviderCapabilityGap(
        key="api_truth_proxy",
        label="API truth / proxy coverage",
        status="deferred",
        current_coverage="SHIP does not run a proxy or read live provider APIs.",
        missing="Strict opt-in local-proxy contract for token/cost reconciliation.",
        reference="API truth belongs to a separate consented proxy, not SHIP batch collectors.",
        next_action="Keep disabled by default and implement only as a separate consented tool.",
        public_claim="Out of scope for SHIP batch/local usage.",
    ),
)


def _provider_rows() -> list[dict[str, str]]:
    return [
        {
            "key": gap.key,
            "label": gap.label,
            "status": gap.status,
            "current_coverage": gap.current_coverage,
            "missing": gap.missing,
            "reference": gap.reference,
            "next_action": gap.next_action,
            "public_claim": gap.public_claim,
        }
        for gap in PROVIDER_CAPABILITY_GAPS
    ]


def _safe_source_counts(storage, window_days: int) -> dict[str, int]:
    rows = storage.query(
        """SELECT source, COUNT(*) AS n
             FROM events
             WHERE date(started_at) >= date('now', ? || ' days')
             GROUP BY source""",
        (f"-{int(window_days)}",),
    )
    return {row["source"] or "unknown": int(row["n"] or 0) for row in rows}


def build_observation_audit_report(storage, window_days: int = 30) -> dict[str, Any]:
    """Build a capability-level audit without returning raw local data."""
    source_counts = _safe_source_counts(storage, window_days)
    observed_sources = sorted(source_counts)
    unknown_sources = sorted(source for source in observed_sources if source not in SOURCE_QUALITY_PROFILES)
    status_counts = Counter(cap.status for cap in CAPABILITIES)
    category_counts = Counter(cap.category for cap in CAPABILITIES)
    provider_status_counts = Counter(gap.status for gap in PROVIDER_CAPABILITY_GAPS)

    rows = [
        {
            "key": cap.key,
            "category": cap.category,
            "label": cap.label,
            "status": cap.status,
            "evidence": cap.evidence,
            "gap": cap.gap,
            "next_action": cap.next_action,
        }
        for cap in CAPABILITIES
    ]

    return {
        "schema_version": "ship1000x.observation_audit_report.v1",
        "window_days": int(window_days),
        "audit_source": "docs/OBSERVATION_AUDIT.md",
        "summary": {
            "capabilities": len(CAPABILITIES),
            "implemented": status_counts["implemented"],
            "partial": status_counts["partial"],
            "planned": status_counts["planned"],
            "deferred": status_counts["deferred"],
            "categories": dict(sorted(category_counts.items())),
            "observed_sources": len(observed_sources),
            "unknown_observed_sources": len(unknown_sources),
            "provider_capabilities": len(PROVIDER_CAPABILITY_GAPS),
            "provider_supported": provider_status_counts["supported"],
            "provider_partial": provider_status_counts["partial"],
            "provider_needs_fixture": provider_status_counts["needs-fixture"],
            "provider_planned": provider_status_counts["planned"],
            "provider_deferred": provider_status_counts["deferred"],
        },
        "observed_sources": [{"source": source, "events": source_counts[source]} for source in observed_sources],
        "unknown_observed_sources": unknown_sources,
        "rows": rows,
        "provider_capabilities": _provider_rows(),
    }
