"""SHIP1000x cost reconciliation.

Compares costs reported by SHIP local collectors (`claude_code`, `codex`,
`codex_macapp`, `codex_desktop`, ...) against billing-side snapshots
(`anthropic_usage`, `openai_usage`) for the same calendar day and provider.

This is a quality audit tool, not a billing system. It answers two questions
that SHIP could not answer before:

1. Are local cost estimates close to provider-billed costs?
2. Where does SHIP fall back to defaults (model unknown, cost_quality not
   factual, no usage metadata) that could explain order-of-magnitude errors?

The module is pure: it reads the SHIP storage handle that the caller passes
in and never opens a private store on its own. The caller (a CLI command,
a test) is responsible for the privacy boundary.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Literal, Protocol

DailyStatus = Literal["ok", "warn", "missing_billing", "missing_local"]


@dataclass(frozen=True)
class ReconcilePair:
    """One local-source <-> billing-source comparison."""

    local: str
    billing: str
    provider_label: str


# Default pairs SHIP supports today. Extend when new billing/usage adapters
# land. The provider_label is informational only.
DEFAULT_RECONCILE_PAIRS: tuple[ReconcilePair, ...] = (
    ReconcilePair(local="claude_code", billing="anthropic_usage", provider_label="anthropic"),
    ReconcilePair(local="codex", billing="openai_usage", provider_label="openai"),
)


@dataclass
class DailyReconcile:
    day: str
    cost_local: float
    cost_billing: float
    delta_abs: float
    delta_pct: float | None  # None when both sides are zero
    status: DailyStatus


@dataclass
class ReconcileSummary:
    total_local: float
    total_billing: float
    total_delta_abs: float
    total_delta_pct: float | None
    days_ok: int
    days_warn: int
    days_missing_billing: int
    days_missing_local: int


@dataclass
class ModelFallback:
    """A model bucket considered a fallback / unspecified detection."""

    model: str
    event_count: int
    cost_total: float


@dataclass
class QualityIssues:
    non_factual_count: int
    non_factual_cost: float
    total_local_events: int
    total_local_cost: float
    model_fallbacks: list[ModelFallback] = field(default_factory=list)
    # Auth-mode distribution over the local source, computed from
    # `raw_meta.usage.auth_mode`. Keys are "oauth", "api_key", "unknown".
    auth_mode_counts: dict[str, int] = field(default_factory=dict)
    auth_mode_cost: dict[str, float] = field(default_factory=dict)
    # Honest cost split derived from auth_mode. The pay-per-token estimate
    # is always treated as the API-equivalent cost. The billed-estimated
    # share equals the estimate under api_key, zero under oauth/unknown
    # (real billing comes from the billing-side adapter).
    api_equivalent_cost: float = 0.0
    billed_estimated_cost: float = 0.0
    # Safe aggregate token counters from normalized usage metadata or
    # model_stats. These are diagnostic only: collectors differ on whether
    # `input_tokens` is uncached input or provider-reported total input.
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    events_with_token_breakdown: int = 0

    @property
    def non_factual_pct(self) -> float | None:
        return _safe_pct(self.non_factual_cost, self.total_local_cost)


@dataclass
class ReconcileReport:
    pair: ReconcilePair
    window_days: int
    threshold_pct: float
    days: list[DailyReconcile]
    summary: ReconcileSummary
    quality: QualityIssues


# The set of model strings SHIP collectors emit when no precise model could be
# extracted. They are not necessarily wrong, but they degrade cost confidence
# because pricing/aliases cannot disambiguate them.
FALLBACK_MODEL_TOKENS = frozenset(
    {"", "default", "all-models", "unknown", "mixed", "fallback"}
)


class StorageProtocol(Protocol):
    """Minimal subset of `ship1000x.core.storage.Storage` used here."""

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        ...


def _safe_pct(delta: float, base: float) -> float | None:
    """Return percent of `delta` against `base`, or None when undefined."""
    if base <= 0:
        return None
    return (delta / base) * 100.0


def _safe_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    return 0


def _day_status(
    cost_local: float,
    cost_billing: float,
    threshold_pct: float,
) -> tuple[DailyStatus, float | None]:
    """Classify one day's delta given a relative threshold."""
    if cost_local <= 0 and cost_billing <= 0:
        # Nothing on either side. Treat as ok so it does not pollute warnings;
        # operator can still see the day in the table because it appears when
        # at least one source had events.
        return "ok", 0.0
    if cost_billing <= 0:
        return "missing_billing", None
    if cost_local <= 0:
        return "missing_local", None
    delta_pct = _safe_pct(abs(cost_local - cost_billing), cost_billing)
    if delta_pct is None or delta_pct <= threshold_pct:
        return "ok", delta_pct
    return "warn", delta_pct


def _aggregate_costs_by_day(
    storage: StorageProtocol,
    source: str,
    window_days: int,
) -> dict[str, float]:
    """Return `{day: total_cost_estimated_USD}` for events of `source` within
    the last `window_days` days. Uses substr on `started_at` because SHIP
    stores ISO timestamps as TEXT.
    """
    if window_days <= 0:
        return {}
    rows = storage.query(
        """
        SELECT substr(started_at, 1, 10) AS day,
               COALESCE(SUM(cost_estimated), 0.0) AS cost
        FROM events
        WHERE source = ?
          AND started_at >= datetime('now', ?)
        GROUP BY day
        """,
        (source, f"-{int(window_days)} days"),
    )
    out: dict[str, float] = {}
    for row in rows:
        day = (row["day"] or "")[:10]
        if not day:
            continue
        out[day] = float(row["cost"] or 0.0)
    return out


def _quality_for_local(
    storage: StorageProtocol,
    source: str,
    window_days: int,
) -> QualityIssues:
    """Compute quality issues for a local source over the window.

    Reads each event's `raw_meta` JSON to extract `usage.quality.cost` (when
    present) and `usage.model_raw` (the source-reported model). Events where
    `usage.quality.cost` is missing or not "factual" are flagged as
    non-factual. Models in `FALLBACK_MODEL_TOKENS` are counted as fallbacks.
    """
    rows = storage.query(
        """
        SELECT cost_estimated, raw_meta
        FROM events
        WHERE source = ?
          AND started_at >= datetime('now', ?)
        """,
        (source, f"-{int(window_days)} days"),
    )

    non_factual_count = 0
    non_factual_cost = 0.0
    total_cost = 0.0
    total_events = 0
    fallback_buckets: dict[str, ModelFallback] = {}
    auth_mode_counts: dict[str, int] = {"oauth": 0, "api_key": 0, "unknown": 0}
    auth_mode_cost: dict[str, float] = {"oauth": 0.0, "api_key": 0.0, "unknown": 0.0}
    api_equivalent_cost = 0.0
    billed_estimated_cost = 0.0
    input_tokens = 0
    output_tokens = 0
    cached_input_tokens = 0
    cache_write_tokens = 0
    reasoning_tokens = 0
    events_with_token_breakdown = 0

    for row in rows:
        total_events += 1
        cost = float(row["cost_estimated"] or 0.0)
        total_cost += cost

        meta_raw = row["raw_meta"] or "{}"
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}

        usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
        quality = usage.get("quality") if isinstance(usage.get("quality"), dict) else {}
        cost_quality = quality.get("cost") if isinstance(quality, dict) else None
        usage_tokens = usage.get("tokens") if isinstance(usage.get("tokens"), dict) else {}
        event_input_tokens = 0
        event_output_tokens = 0
        event_cached_input_tokens = 0
        event_cache_write_tokens = 0
        event_reasoning_tokens = 0
        has_usage_token_breakdown = bool(usage_tokens)
        if has_usage_token_breakdown:
            event_input_tokens += _safe_int(usage_tokens.get("input_tokens"))
            event_output_tokens += _safe_int(usage_tokens.get("output_tokens"))
            event_cached_input_tokens += _safe_int(
                usage_tokens.get("cached_input_tokens")
            )
            event_cache_write_tokens += _safe_int(usage_tokens.get("cache_write_tokens"))
            event_reasoning_tokens += _safe_int(usage_tokens.get("reasoning_tokens"))

        # `model_stats` is the canonical SHIP-side per-model breakdown
        # (Claude Code, Codex, anthropic_usage, openai_usage). When present
        # it carries one or more model identifiers as dict keys and a
        # nested `{cost, tokens_in, tokens_out, turns, ...}` payload.
        # Earlier versions of this module only inspected `usage.model_raw`,
        # which Claude Code never wrote — leading to a false-positive
        # "100% non-factual" line for every Claude Code reconcile run.
        model_stats = meta.get("model_stats") if isinstance(meta.get("model_stats"), dict) else {}
        models_seen: list[str] = []
        if model_stats:
            for model_key, model_payload in model_stats.items():
                if not isinstance(model_key, str):
                    continue
                models_seen.append(model_key)
                if isinstance(model_payload, dict) and not has_usage_token_breakdown:
                    event_input_tokens += _safe_int(model_payload.get("tokens_in"))
                    event_output_tokens += _safe_int(model_payload.get("tokens_out"))
                    event_cached_input_tokens += _safe_int(
                        model_payload.get("cache_read_tokens")
                    )
                    event_cache_write_tokens += _safe_int(
                        model_payload.get("cache_write_tokens")
                    )
                    event_reasoning_tokens += _safe_int(
                        model_payload.get("reasoning_tokens")
                    )
                # If we can read a per-model cost > 0 from model_stats and
                # the top-level cost_quality is missing, treat the event as
                # factual at the cost level. SHIP collectors that write
                # model_stats compute cost from per-call usage, which is
                # the same evidence as `cost_quality: factual`.
                if cost_quality is None and isinstance(model_payload, dict):
                    pc = model_payload.get("cost")
                    if isinstance(pc, (int, float)) and pc > 0:
                        cost_quality = "factual"
        if any(
            (
                event_input_tokens,
                event_output_tokens,
                event_cached_input_tokens,
                event_cache_write_tokens,
                event_reasoning_tokens,
            )
        ):
            events_with_token_breakdown += 1
            input_tokens += event_input_tokens
            output_tokens += event_output_tokens
            cached_input_tokens += event_cached_input_tokens
            cache_write_tokens += event_cache_write_tokens
            reasoning_tokens += event_reasoning_tokens

        if not models_seen:
            # Fall back to the legacy single-model field for collectors that
            # still write `raw_meta.usage.model_raw` (some fixture-only
            # parsers and the older anthropic_usage shape).
            model_raw = (
                usage.get("model_raw")
                or usage.get("model_canonical")
                or meta.get("model")
                or ""
            )
            if isinstance(model_raw, str):
                models_seen = [model_raw]

        if cost_quality != "factual":
            non_factual_count += 1
            non_factual_cost += cost

        for model_raw in models_seen:
            normalized = model_raw.strip().lower() if isinstance(model_raw, str) else ""
            if normalized in FALLBACK_MODEL_TOKENS:
                key = normalized or "(empty)"
                bucket = fallback_buckets.setdefault(
                    key, ModelFallback(model=key, event_count=0, cost_total=0.0)
                )
                bucket.event_count += 1
                bucket.cost_total += cost
                # Only count each event once even when several model entries
                # would all hit the fallback list (rare but possible).
                break

        # Auth-mode bucketing for honest cost reporting.
        raw_auth = usage.get("auth_mode") if isinstance(usage, dict) else None
        am = (raw_auth or "").strip().lower() if isinstance(raw_auth, str) else ""
        if am not in {"oauth", "api_key", "unknown"}:
            am = "unknown"
        auth_mode_counts[am] = auth_mode_counts.get(am, 0) + 1
        auth_mode_cost[am] = auth_mode_cost.get(am, 0.0) + cost

        # api_equivalent is what the usage would cost on pay-per-token API.
        # billed_estimated is what SHIP thinks the user actually pays.
        if isinstance(usage, dict):
            cost_block = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
            api_eq = cost_block.get("api_equivalent_usd")
            billed = cost_block.get("billed_estimated_usd")
        else:
            cost_block = {}
            api_eq = None
            billed = None
        if isinstance(api_eq, (int, float)):
            api_equivalent_cost += float(api_eq)
        else:
            api_equivalent_cost += cost
        if isinstance(billed, (int, float)):
            billed_estimated_cost += float(billed)
        else:
            billed_estimated_cost += cost if am == "api_key" else 0.0

    return QualityIssues(
        non_factual_count=non_factual_count,
        non_factual_cost=round(non_factual_cost, 6),
        total_local_events=total_events,
        total_local_cost=round(total_cost, 6),
        model_fallbacks=sorted(
            fallback_buckets.values(),
            key=lambda b: (-b.cost_total, -b.event_count, b.model),
        ),
        auth_mode_counts={k: v for k, v in auth_mode_counts.items() if v > 0},
        auth_mode_cost={k: round(v, 6) for k, v in auth_mode_cost.items() if v > 0},
        api_equivalent_cost=round(api_equivalent_cost, 6),
        billed_estimated_cost=round(billed_estimated_cost, 6),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        events_with_token_breakdown=events_with_token_breakdown,
    )


def reconcile_pair(
    storage: StorageProtocol,
    pair: ReconcilePair,
    window_days: int = 30,
    threshold_pct: float = 10.0,
) -> ReconcileReport:
    """Compute a full reconciliation report for one `local` vs `billing` pair.

    `threshold_pct` is the relative percent above which a day is flagged as
    `warn`. The default 10% is conservative; pick higher for noisy stores.
    """
    if window_days <= 0:
        raise ValueError("window_days must be a positive integer")
    if threshold_pct < 0:
        raise ValueError("threshold_pct must be >= 0")

    local_by_day = _aggregate_costs_by_day(storage, pair.local, window_days)
    billing_by_day = _aggregate_costs_by_day(storage, pair.billing, window_days)

    days: list[DailyReconcile] = []
    all_days = sorted(set(local_by_day.keys()) | set(billing_by_day.keys()))
    for day in all_days:
        cost_local = round(local_by_day.get(day, 0.0), 6)
        cost_billing = round(billing_by_day.get(day, 0.0), 6)
        status, delta_pct = _day_status(cost_local, cost_billing, threshold_pct)
        delta_abs = round(cost_local - cost_billing, 6)
        days.append(
            DailyReconcile(
                day=day,
                cost_local=cost_local,
                cost_billing=cost_billing,
                delta_abs=delta_abs,
                delta_pct=None if delta_pct is None else round(delta_pct, 2),
                status=status,
            )
        )

    total_local = round(sum(d.cost_local for d in days), 6)
    total_billing = round(sum(d.cost_billing for d in days), 6)
    total_delta_abs = round(total_local - total_billing, 6)
    total_delta_pct = _safe_pct(abs(total_delta_abs), total_billing)
    if total_delta_pct is not None:
        total_delta_pct = round(total_delta_pct, 2)

    summary = ReconcileSummary(
        total_local=total_local,
        total_billing=total_billing,
        total_delta_abs=total_delta_abs,
        total_delta_pct=total_delta_pct,
        days_ok=sum(1 for d in days if d.status == "ok"),
        days_warn=sum(1 for d in days if d.status == "warn"),
        days_missing_billing=sum(1 for d in days if d.status == "missing_billing"),
        days_missing_local=sum(1 for d in days if d.status == "missing_local"),
    )

    quality = _quality_for_local(storage, pair.local, window_days)

    return ReconcileReport(
        pair=pair,
        window_days=window_days,
        threshold_pct=threshold_pct,
        days=days,
        summary=summary,
        quality=quality,
    )


def reconcile_pairs(
    storage: StorageProtocol,
    pairs: tuple[ReconcilePair, ...] = DEFAULT_RECONCILE_PAIRS,
    window_days: int = 30,
    threshold_pct: float = 10.0,
) -> list[ReconcileReport]:
    """Run `reconcile_pair` for every pair, in order."""
    return [
        reconcile_pair(
            storage,
            pair,
            window_days=window_days,
            threshold_pct=threshold_pct,
        )
        for pair in pairs
    ]


def _format_currency(value: float) -> str:
    return f"${value:,.2f}"


def _format_pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:+.1f}%" if value != 0 else "0.0%"


def _format_int(value: int) -> str:
    return f"{value:,}"


def render_markdown_report(reports: list[ReconcileReport]) -> str:
    """Render a Markdown reconciliation report safe for stdout / a file."""
    if not reports:
        return "# SHIP cost reconciliation\n\n_No pairs to report._\n"

    lines: list[str] = []
    lines.append("# SHIP cost reconciliation")
    lines.append("")
    lines.append(
        "Compares local-collector API-equivalent cost estimates against "
        "billing-side snapshots for the same calendar day and provider. "
        "Local cost is what SHIP computes from JSONL/SQLite collectors; "
        "billing-side cost is what the provider Admin/Usage API reports for "
        "returned buckets. Large persistent gaps suggest "
        "pricing drift, missing model aliases, cache-token misinterpretation, "
        "or out-of-band consumption (OAuth seats, team workspaces)."
    )
    lines.append("")

    for report in reports:
        s = report.summary
        q = report.quality

        lines.append(f"## {report.pair.local} vs {report.pair.billing}")
        lines.append("")
        lines.append(
            f"Window: last {report.window_days} days. "
            f"Daily-delta warning threshold: {report.threshold_pct:.1f}%."
        )
        lines.append("")
        lines.append("### Totals")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---:|")
        lines.append(f"| Local API-equivalent total | {_format_currency(s.total_local)} |")
        lines.append(f"| Billing-side total | {_format_currency(s.total_billing)} |")
        lines.append(f"| Delta abs | {_format_currency(s.total_delta_abs)} |")
        lines.append(f"| Delta pct | {_format_pct(s.total_delta_pct)} |")
        lines.append(f"| Days ok | {s.days_ok} |")
        lines.append(f"| Days warn | {s.days_warn} |")
        lines.append(f"| Days missing billing | {s.days_missing_billing} |")
        lines.append(f"| Days missing local | {s.days_missing_local} |")
        lines.append("")

        if report.days:
            lines.append("### Daily breakdown")
            lines.append("")
            lines.append("| Day | Local | Billing | Delta | Delta % | Status |")
            lines.append("|---|---:|---:|---:|---:|---|")
            for day in report.days:
                lines.append(
                    f"| {day.day} | {_format_currency(day.cost_local)} | {_format_currency(day.cost_billing)} | {_format_currency(day.delta_abs)} | {_format_pct(day.delta_pct)} | {day.status} |"
                )
            lines.append("")

        lines.append("### Local-source quality issues")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---:|")
        lines.append(f"| Local events | {q.total_local_events} |")
        lines.append(f"| Local cost | {_format_currency(q.total_local_cost)} |")
        lines.append(f"| Non-factual cost events | {q.non_factual_count} |")
        lines.append(
            f"| Non-factual cost share | {_format_pct(q.non_factual_pct)} |"
        )
        lines.append("")

        # Honest cost split: separate the pay-per-token API-equivalent
        # value from what SHIP estimates is actually billed. Under an
        # OAuth subscription, billed_estimated is 0 by design — the real
        # billed cost belongs to the billing-side adapter (Totals above).
        lines.append("#### Cost honest split (local source)")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---:|")
        lines.append(
            f"| API-equivalent cost (pay-per-token value) | "
            f"{_format_currency(q.api_equivalent_cost)} |"
        )
        lines.append(
            f"| Billed-estimated cost (local route estimate, not invoice truth) | "
            f"{_format_currency(q.billed_estimated_cost)} |"
        )
        subscription_saving = q.api_equivalent_cost - q.billed_estimated_cost
        lines.append(
            f"| Subscription absorption (API-equivalent − billed-estimated) | "
            f"{_format_currency(subscription_saving)} |"
        )
        lines.append("")
        lines.append(
            "_For OAuth events, SHIP reports `billed_estimated = 0` so the "
            "billing-side comparator is taken from the billing-side adapter "
            "only (no double counting). Compare the API-equivalent figure with "
            "the billing-side total above to estimate what the subscription "
            "absorbs._"
        )
        lines.append("")

        if q.auth_mode_counts:
            lines.append("#### Auth mode distribution")
            lines.append("")
            lines.append("| Mode | Events | Cost (API-equivalent) |")
            lines.append("|---|---:|---:|")
            for mode in ("api_key", "oauth", "unknown"):
                if q.auth_mode_counts.get(mode):
                    lines.append(
                        f"| `{mode}` | {q.auth_mode_counts[mode]} | "
                        f"{_format_currency(q.auth_mode_cost.get(mode, 0.0))} |"
                    )
            lines.append("")

        if q.events_with_token_breakdown:
            lines.append("#### Token breakdown (local source)")
            lines.append("")
            lines.append("| Metric | Value |")
            lines.append("|---|---:|")
            lines.append(
                f"| Events with token breakdown | "
                f"{_format_int(q.events_with_token_breakdown)} |"
            )
            lines.append(
                f"| Input tokens (collector-reported) | "
                f"{_format_int(q.input_tokens)} |"
            )
            lines.append(f"| Output tokens | {_format_int(q.output_tokens)} |")
            lines.append(
                f"| Cached input tokens | {_format_int(q.cached_input_tokens)} |"
            )
            lines.append(
                f"| Cache write tokens | {_format_int(q.cache_write_tokens)} |"
            )
            lines.append(f"| Reasoning tokens | {_format_int(q.reasoning_tokens)} |")
            lines.append("")
            lines.append(
                "_Token counters are diagnostic aggregates. Collector shapes differ: "
                "`input_tokens` can mean uncached input for some sources and "
                "provider-reported total input for others. Use this section to "
                "spot cache/reasoning-driven reconcile gaps, not as invoice truth._"
            )
            lines.append("")

        if q.model_fallbacks:
            lines.append("#### Model fallbacks detected")
            lines.append("")
            lines.append("| Model | Events | Cost (API-equivalent) |")
            lines.append("|---|---:|---:|")
            for bucket in q.model_fallbacks:
                lines.append(
                    f"| `{bucket.model}` | {bucket.event_count} | "
                    f"{_format_currency(bucket.cost_total)} |"
                )
            lines.append("")
        else:
            lines.append("_No model-fallback events detected in this window._")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_Generated by `ship1000x reconcile`. Report contains only aggregates "
        "and model labels; no event payloads, attributes, conversation bodies, "
        "patch bodies, local paths, or credential markers are included._"
    )
    return "\n".join(lines) + "\n"
