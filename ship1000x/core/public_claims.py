"""Public claim readiness for Ship1000x.

This module turns source quality and observation coverage into explicit public
claim boundaries. It intentionally uses only aggregate reports: no raw metadata,
prompts, paths, commands, or provider payloads are returned.
"""

from __future__ import annotations

from typing import Any


def _claim(
    key: str,
    label: str,
    status: str,
    evidence: str,
    caveat: str,
) -> dict[str, str]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "evidence": evidence,
        "caveat": caveat,
    }


def _proof_step(
    *,
    key: str,
    command: str,
    purpose: str,
    status: str,
    evidence: str,
    caveat: str,
) -> dict[str, str]:
    return {
        "key": key,
        "command": command,
        "purpose": purpose,
        "status": status,
        "evidence": evidence,
        "caveat": caveat,
    }


def build_public_proof_sequence(
    *,
    since: str,
    fail_under: float,
    strict_history: bool,
    source_report: dict[str, Any],
    observation_report: dict[str, Any],
    history_report: dict[str, Any],
    claim_readiness: dict[str, Any],
    source_failures: list[str],
    history_failures: list[str],
) -> dict[str, Any]:
    """Build a safe operator proof sequence for public demos/reports."""
    source_summary = source_report.get("summary", {})
    observation_summary = observation_report.get("summary", {})
    history_summary = history_report.get("summary", {})
    claim_summary = claim_readiness.get("summary", {})
    source_gate = source_report.get("gate", {})
    all_failures = source_failures + history_failures
    strict_suffix = " --strict-history" if strict_history else ""
    fail_under_label = (
        str(int(fail_under)) if float(fail_under).is_integer() else str(fail_under)
    )
    has_observation_caveats = bool(
        observation_summary.get("planned") or observation_summary.get("deferred")
    )
    has_history_caveats = bool(
        history_summary.get("partial_sources")
        or history_summary.get("external_source_required_sources")
        or history_summary.get("not_recoverable_from_ship_sources")
        or history_summary.get("malformed_meta_events")
    )
    has_claim_caveats = bool(
        int(claim_summary.get("conditional") or 0)
        or int(claim_summary.get("blocked") or 0)
    )

    steps = [
        _proof_step(
            key="source_quality_gate",
            command=f"ship1000x source-audit --strict-public --since {since}",
            purpose="Prove observed sources clear the public source-quality gate.",
            status="pass" if not source_failures else "fail",
            evidence=(
                f"gate_passed={bool(source_gate.get('passed'))}; "
                f"observed_sources={int(source_summary.get('observed_sources') or 0)}; "
                f"low_quality_sources={int(source_summary.get('low_quality_sources') or 0)}; "
                f"public_untrusted_stage_sources="
                f"{int(source_summary.get('public_untrusted_stage_sources') or 0)}."
            ),
            caveat="This covers only rows already stored for the selected window.",
        ),
        _proof_step(
            key="observation_coverage",
            command=f"ship1000x observation-audit --since {since}",
            purpose="Name supported, partial, planned, deferred, and fixture-only coverage.",
            status="caveat" if has_observation_caveats else "pass",
            evidence=(
                f"supported={int(observation_summary.get('supported') or 0)}; "
                f"partial={int(observation_summary.get('partial') or 0)}; "
                f"planned={int(observation_summary.get('planned') or 0)}; "
                f"deferred={int(observation_summary.get('deferred') or 0)}."
            ),
            caveat="Coverage gaps are visible caveats, not proof of absent activity.",
        ),
        _proof_step(
            key="history_recoverability",
            command=f"ship1000x history-audit --since {since}",
            purpose="Prove historical rows are recoverable, partial, external, or blocked.",
            status=(
                "fail"
                if history_failures
                else "caveat"
                if has_history_caveats
                else "pass"
            ),
            evidence=(
                f"recoverable={int(history_summary.get('recoverable_sources') or 0)}; "
                f"partial={int(history_summary.get('partial_sources') or 0)}; "
                f"external={int(history_summary.get('external_source_required_sources') or 0)}; "
                f"not_recoverable="
                f"{int(history_summary.get('not_recoverable_from_ship_sources') or 0)}; "
                f"malformed_meta={int(history_summary.get('malformed_meta_events') or 0)}."
            ),
            caveat="Repair/backfill claims require retained original local/source evidence.",
        ),
        _proof_step(
            key="public_claim_boundaries",
            command=(
                f"ship1000x public-check --since {since} --fail-under {fail_under_label}"
                f"{strict_suffix}"
            ),
            purpose="List what is allowed to quote, conditional, or blocked.",
            status="fail" if all_failures else "caveat" if has_claim_caveats else "pass",
            evidence=(
                f"allowed={int(claim_summary.get('allowed') or 0)}; "
                f"conditional={int(claim_summary.get('conditional') or 0)}; "
                f"blocked={int(claim_summary.get('blocked') or 0)}."
            ),
            caveat="Blocked claims must stay visible as caveats in public material.",
        ),
    ]

    return {
        "schema_version": "ship1000x.public_proof_sequence.v1",
        "status": (
            "fail"
            if all_failures
            else "caveat"
            if has_observation_caveats or has_history_caveats or has_claim_caveats
            else "pass"
        ),
        "strict_history": strict_history,
        "commands": steps,
        "quote_policy": (
            "Quote only allowed claims. Conditional claims require their named caveats. "
            "Blocked claims must remain explicit caveats, not marketing claims."
        ),
    }


def build_public_claim_readiness(
    *,
    source_report: dict[str, Any],
    observation_report: dict[str, Any],
    history_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build public claim boundaries from existing aggregate audit reports."""
    source_summary = source_report.get("summary", {})
    source_gate = source_report.get("gate", {})
    observation_summary = observation_report.get("summary", {})
    provider_rows = observation_report.get("provider_capabilities", [])

    source_gate_passed = bool(source_gate.get("passed"))
    observed_sources = int(source_summary.get("observed_sources") or 0)
    provider_supported = int(observation_summary.get("provider_supported") or 0)
    provider_partial = int(observation_summary.get("provider_partial") or 0)
    provider_needs_fixture = int(observation_summary.get("provider_needs_fixture") or 0)
    provider_planned = int(observation_summary.get("provider_planned") or 0)
    provider_deferred = int(observation_summary.get("provider_deferred") or 0)
    source_rows = source_report.get("rows", [])
    usage_events_total = int(source_summary.get("usage_events") or 0)
    if not usage_events_total:
        usage_events_total = sum(int(row.get("usage_events") or 0) for row in source_rows)
    provider_policy_snapshot_events = int(
        source_summary.get("provider_policy_snapshot_events") or 0
    )
    invalid_provider_policy_snapshot_events = int(
        source_summary.get("invalid_provider_policy_snapshot_events") or 0
    )
    has_summary_policy_counts = (
        "provider_policy_snapshot_events" in source_summary
        or "invalid_provider_policy_snapshot_events" in source_summary
        or "missing_provider_policy_snapshot_events" in source_summary
    )
    if "missing_provider_policy_snapshot_events" in source_summary:
        missing_provider_policy_snapshot_events = int(
            source_summary.get("missing_provider_policy_snapshot_events") or 0
        )
    elif has_summary_policy_counts and usage_events_total:
        missing_provider_policy_snapshot_events = max(
            0,
            usage_events_total
            - provider_policy_snapshot_events
            - invalid_provider_policy_snapshot_events,
        )
    else:
        missing_provider_policy_snapshot_events = 0
    if not provider_policy_snapshot_events and not invalid_provider_policy_snapshot_events:
        provider_policy_snapshot_events = sum(
            int(row.get("provider_policy_snapshot_events") or 0) for row in source_rows
        )
        invalid_provider_policy_snapshot_events = sum(
            int(row.get("invalid_provider_policy_snapshot_events") or 0)
            for row in source_rows
        )
    if (
        not missing_provider_policy_snapshot_events
        and usage_events_total
        and source_rows
        and not has_summary_policy_counts
    ):
        missing_provider_policy_snapshot_events = sum(
            int(
                row.get("missing_provider_policy_snapshot_events")
                if row.get("missing_provider_policy_snapshot_events") is not None
                else max(
                    0,
                    int(row.get("usage_events") or 0)
                    - int(row.get("provider_policy_snapshot_events") or 0)
                    - int(row.get("invalid_provider_policy_snapshot_events") or 0),
                )
            )
            for row in source_rows
        )
    provider_policy_snapshot_evidence = (
        f"{provider_policy_snapshot_events}/{usage_events_total}"
        if usage_events_total
        else "0/0"
    )
    source_cost_truth = source_summary.get("cost_truth")
    if isinstance(source_cost_truth, dict):
        unknown_billing_basis_usd = float(
            source_cost_truth.get("unknown_billing_basis_usd") or 0.0
        )
        unknown_billing_basis_events = int(
            source_cost_truth.get("events_with_unknown_billing_basis") or 0
        )
    else:
        unknown_billing_basis_usd = round(
            sum(
                float(row.get("cost_truth", {}).get("unknown_billing_basis_usd") or 0.0)
                for row in source_rows
            ),
            6,
        )
        unknown_billing_basis_events = sum(
            int(row.get("cost_truth", {}).get("events_with_unknown_billing_basis") or 0)
            for row in source_rows
        )
    auth_mode_counts: dict[str, int] = {}
    auth_mode_total_events = 0
    missing_or_unknown_auth_mode_events = 0
    nonstandard_auth_modes: dict[str, int] = {}
    for row in source_rows:
        counts = row.get("auth_mode_counts")
        usage_events = int(row.get("usage_events") or 0)
        if not isinstance(counts, dict):
            missing_or_unknown_auth_mode_events += usage_events
            continue
        row_auth_events = 0
        for mode, count in counts.items():
            mode_key = str(mode)
            mode_count = int(count or 0)
            row_auth_events += mode_count
            auth_mode_counts[mode_key] = auth_mode_counts.get(mode_key, 0) + mode_count
            if mode_key not in {"api_key", "oauth", "unknown"}:
                nonstandard_auth_modes[mode_key] = nonstandard_auth_modes.get(mode_key, 0) + mode_count
        auth_mode_total_events += row_auth_events
        missing_or_unknown_auth_mode_events += max(0, usage_events - row_auth_events)
        missing_or_unknown_auth_mode_events += int(counts.get("unknown") or 0)
    auth_modes_evidence = (
        ", ".join(f"{mode}:{count}" for mode, count in sorted(auth_mode_counts.items()))
        if auth_mode_counts
        else "none-observed"
    )
    nonstandard_auth_modes_evidence = (
        ", ".join(f"{mode}:{count}" for mode, count in sorted(nonstandard_auth_modes.items()))
        if nonstandard_auth_modes
        else "none"
    )

    claims: list[dict[str, str]] = []
    history_repair_risks: list[dict[str, Any]] = []
    claims.append(
        _claim(
            "local_first_privacy",
            "Local-first privacy posture",
            "allowed",
            "Public audits expose aggregate counts, quality labels, and source names only.",
            "Collectors and exports remain subject to user consent and source-specific privacy docs.",
        )
    )
    claims.append(
        _claim(
            "batch_local_usage",
            "Batch/local AI and dev usage visibility",
            "allowed" if source_gate_passed and observed_sources > 0 else "blocked",
            f"source gate passed={source_gate_passed}; observed sources={observed_sources}.",
            "Only the selected local database window is covered; absent sources are not inferred.",
        )
    )
    claims.append(
        _claim(
            "audit_grade_observed_sources",
            "Audit-grade observed source claims",
            "allowed" if source_gate_passed else "blocked",
            "The strict source-quality gate blocks fragile and unknown observed sources.",
            "This does not certify sources absent from the selected window.",
        )
    )
    claims.append(
        _claim(
            "provider_complete_coverage",
            "Complete provider/tool coverage",
            "blocked",
            (
                f"supported={provider_supported}; partial={provider_partial}; "
                f"needs_fixture={provider_needs_fixture}; planned={provider_planned}; "
                f"deferred={provider_deferred}."
            ),
            "Public claims must name supported providers and visible gaps separately.",
        )
    )
    claims.append(
        _claim(
            "invoice_grade_cost_truth",
            "Invoice-grade cost truth",
            "blocked",
            (
                "Billing/API reconciliation is separate from local activity truth; "
                f"unknown_billing_basis_usd={unknown_billing_basis_usd}; "
                f"unknown_billing_basis_events={unknown_billing_basis_events}."
            ),
            "Local costs are claim-safe only when source quality marks them factual and pricing provenance is present.",
        )
    )
    claims.append(
        _claim(
            "provider_policy_compliance",
            "Provider-policy/auth-route compliance",
            "blocked",
            (
                f"auth_modes={auth_modes_evidence}; "
                f"auth_mode_events={auth_mode_total_events}; "
                f"missing_or_unknown_auth_mode_events={missing_or_unknown_auth_mode_events}; "
                f"nonstandard_auth_modes={nonstandard_auth_modes_evidence}; "
                f"provider_policy_snapshots={provider_policy_snapshot_evidence}; "
                f"invalid_provider_policy_snapshots={invalid_provider_policy_snapshot_events}; "
                f"missing_provider_policy_snapshots={missing_provider_policy_snapshot_events}."
            ),
            (
                "SHIP can expose local auth-mode evidence, but it does not certify "
                "provider terms, subscription eligibility, proxy policy, or quota compliance."
            ),
        )
    )
    claims.append(
        _claim(
            "live_runtime_truth",
            "Live runtime/rate-limit/process truth",
            "blocked",
            "Live runtime observability is planned/deferred in the observation audit.",
            "Do not imply live HTTPS capture from SHIP batch/local collectors.",
        )
    )
    if history_report is not None:
        history_summary = history_report.get("summary", {})
        history_observed = int(history_summary.get("observed_sources") or 0)
        history_recoverable = int(history_summary.get("recoverable_sources") or 0)
        history_partial = int(history_summary.get("partial_sources") or 0)
        history_external = int(history_summary.get("external_source_required_sources") or 0)
        history_not_recoverable = int(history_summary.get("not_recoverable_from_ship_sources") or 0)
        malformed_meta = int(history_summary.get("malformed_meta_events") or 0)
        if malformed_meta or history_not_recoverable:
            history_status = "blocked"
            history_caveat = (
                "Repair/backfill claims are unsafe until malformed metadata and "
                "not-recoverable sources are resolved or excluded."
            )
        elif history_observed == 0 or history_partial or history_external:
            history_status = "conditional"
            history_caveat = (
                "Historical repair claims must name partial sources and any required external exports/API access."
            )
        else:
            history_status = "allowed"
            history_caveat = (
                "Limited to the selected window and only while original local/source data remains available."
            )
        claims.append(
            _claim(
                "historical_repair_defensibility",
                "Historical repair/backfill defensibility",
                history_status,
                (
                    f"observed={history_observed}; recoverable={history_recoverable}; "
                    f"partial={history_partial}; external={history_external}; "
                    f"not_recoverable={history_not_recoverable}; malformed_meta={malformed_meta}."
                ),
                history_caveat,
            )
        )
        history_repair_risks = [
            {
                "source": row.get("source", "unknown"),
                "label": row.get("label", row.get("source", "unknown")),
                "recoverability": row.get("recoverability", "unknown"),
                "repair_scope": row.get("repair_scope", "visible-evidence-only"),
                "claim_boundary": row.get("claim_boundary", ""),
                "unrecoverable_truth_fields": row.get("unrecoverable_truth_fields", []),
                "malformed_meta_events": int(row.get("malformed_meta_events") or 0),
                "recommendation": row.get("recommendation", ""),
            }
            for row in history_report.get("rows", [])
            if row.get("recoverability") != "recoverable" or int(row.get("malformed_meta_events") or 0) > 0
        ]

    blocked_provider_claims = [
        {
            "key": row.get("key", "unknown"),
            "label": row.get("label", row.get("key", "unknown")),
            "status": row.get("status", "unknown"),
            "public_claim": row.get("public_claim", ""),
        }
        for row in provider_rows
        if row.get("status") in {"needs-fixture", "planned", "deferred"}
    ]

    status_counts: dict[str, int] = {"allowed": 0, "blocked": 0, "conditional": 0}
    for claim in claims:
        status_counts[claim["status"]] = status_counts.get(claim["status"], 0) + 1

    return {
        "schema_version": "ship1000x.public_claim_readiness.v1",
        "summary": status_counts,
        "claims": claims,
        "blocked_provider_claims": blocked_provider_claims,
        "history_repair_risks": history_repair_risks,
        "operator_guidance": (
            "Quote allowed claims only. Conditional claims require their named "
            "preconditions and source/history caveats. Blocked claims must stay "
            "visible as caveats until source quality, history recoverability, "
            "fixtures, and reconciliation evidence catch up."
        ),
    }
