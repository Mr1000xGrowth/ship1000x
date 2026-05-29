"""Per-event cost audit / drill-down.

`ship1000x reconcile` answers "do local totals match billing semantics?".
This module answers the next question: "where exactly do those local
API-equivalent totals come from, and can I reproduce each estimate from
tokens × pricing?"

It returns, for every event of a chosen source over a window:

- the SHIP-stored API-equivalent cost,
- a manual re-computation from the per-model token breakdown and the
  same `pricing.py` SHIP uses,
- the per-rate breakdown (uncached input / cache_read / cache_write /
  output) so the operator can audit each component,
- the absolute and percent divergence between stored and recomputed.

Everything stays read-only on the local SHIP store. No provider API
call. No prompt/response/path/diff exposure: the report contains only
aggregate token counts, model labels, and API-equivalent dollar amounts.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class PerModelBreakdown:
    """Cost breakdown for one model inside one stored event."""

    model: str
    rates: dict[str, float]
    uncached_input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int

    @property
    def cost_uncached(self) -> float:
        return self.uncached_input_tokens * self.rates.get("input", 0.0) / 1_000_000

    @property
    def cost_cache_read(self) -> float:
        # Some pricing entries use `cache_read`, OpenAI-style ones use
        # `cached_input`. Try both so the function works across providers.
        rate = self.rates.get("cache_read")
        if rate is None:
            rate = self.rates.get("cached_input", 0.0)
        return self.cache_read_tokens * rate / 1_000_000

    @property
    def cost_cache_write(self) -> float:
        return self.cache_write_tokens * self.rates.get("cache_write", 0.0) / 1_000_000

    @property
    def cost_output(self) -> float:
        return self.output_tokens * self.rates.get("output", 0.0) / 1_000_000

    @property
    def cost_total(self) -> float:
        return (
            self.cost_uncached
            + self.cost_cache_read
            + self.cost_cache_write
            + self.cost_output
        )


@dataclass
class EventCostAudit:
    """One stored event with stored vs recomputed cost."""

    event_id: str
    source: str
    started_at: str
    session_id: str | None
    auth_mode: str
    cost_stored: float
    cost_recomputed: float
    models: list[PerModelBreakdown] = field(default_factory=list)

    @property
    def delta_abs(self) -> float:
        return self.cost_stored - self.cost_recomputed

    @property
    def delta_pct(self) -> float | None:
        if self.cost_recomputed <= 0:
            return None
        return (self.delta_abs / self.cost_recomputed) * 100.0


@dataclass
class CostAuditSummary:
    source: str
    window_days: int
    total_events: int
    total_cost_stored: float
    total_cost_recomputed: float
    max_abs_delta: float
    max_pct_delta: float | None
    auth_mode_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class CostAuditReport:
    summary: CostAuditSummary
    events: list[EventCostAudit]


class StorageProtocol(Protocol):
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        ...


def _select_rates_for_model(model: str, provider: str) -> dict[str, float]:
    """Pick the right per-provider pricing table entry for `model`.

    Defers to `ship1000x.core.pricing` so this module stays free of
    pricing constants. Returns the same `{input, output, cache_read,
    cache_write}` shape every time, padded with zeros where the table
    does not define a rate (e.g. OpenAI does not expose cache_write).
    """
    from ship1000x.core.pricing import resolve_model_pricing

    resolution = resolve_model_pricing(provider, model)
    rates = resolution.rates or {}
    return {
        "input": float(rates.get("input", 0.0)),
        "output": float(rates.get("output", 0.0)),
        "cache_read": float(rates.get("cache_read", rates.get("cached_input", 0.0))),
        "cache_write": float(rates.get("cache_write", 0.0)),
    }


def _provider_for_source(source: str) -> str:
    """Map a SHIP `source` to the provider expected by pricing.py."""
    s = source.lower()
    if s in {"claude_code", "anthropic_usage"}:
        return "anthropic"
    if s in {
        "codex",
        "codex_desktop",
        "codex_macapp",
        "codex_sqlite",
        "openai_usage",
    }:
        return "openai"
    # For everything else (cursor, cline, ...) default to anthropic so the
    # caller still gets a recomputation rather than zeros; the auditor
    # surface stays useful but the operator should know the rate card may
    # not be the right one.
    return "anthropic"


def _breakdown_from_model_stats(
    model_stats: dict[str, dict],
    uncached_input_tokens_total: int,
    provider: str,
) -> list[PerModelBreakdown]:
    """Build PerModelBreakdown rows from the canonical SHIP shape.

    `uncached_input_tokens_total` is the per-event uncached_input from
    `raw_meta.usage.tokens.input_tokens` (Claude Code / Codex collectors).
    When several models share an event we proportionally split the
    uncached tokens by per-model `tokens_in`. Most events have only one
    model and that math collapses to identity.
    """
    out: list[PerModelBreakdown] = []
    total_model_tokens_in = sum(
        (m.get("tokens_in", 0) or 0) for m in model_stats.values()
    )
    for model, st in model_stats.items():
        if not isinstance(st, dict):
            continue
        cache_r = int(st.get("cache_read_tokens", 0) or 0)
        cache_w = int(st.get("cache_write_tokens", 0) or 0)
        output = int(st.get("tokens_out", 0) or 0)
        # Per-event uncached comes from the top-level usage block; for
        # multi-model events we share it pro rata to per-model tokens_in.
        if total_model_tokens_in > 0:
            share = (st.get("tokens_in", 0) or 0) / total_model_tokens_in
            uncached = int(uncached_input_tokens_total * share)
        else:
            uncached = 0
        # If model_stats already exposes uncached_input_tokens (the new
        # anthropic_usage/openai_usage shape), prefer that exact value
        # over the prorata.
        if "uncached_input_tokens" in st:
            uncached = int(st.get("uncached_input_tokens") or 0)
        rates = _select_rates_for_model(model, provider)
        out.append(
            PerModelBreakdown(
                model=model,
                rates=rates,
                uncached_input_tokens=uncached,
                cache_read_tokens=cache_r,
                cache_write_tokens=cache_w,
                output_tokens=output,
            )
        )
    return out


def audit_source(
    storage: StorageProtocol,
    source: str,
    *,
    window_days: int = 30,
    top: int = 20,
) -> CostAuditReport:
    """Audit one source's events: stored cost vs token×pricing recomputation.

    `top` caps the number of events returned in the report (sorted by
    stored cost descending) so the operator can focus on the events
    that move the totals first. Summary numbers always cover the full
    window, not just `top`.
    """
    if window_days <= 0:
        raise ValueError("window_days must be a positive integer")
    if top <= 0:
        raise ValueError("top must be a positive integer")

    provider = _provider_for_source(source)
    rows = storage.query(
        """
        SELECT id, started_at, cost_estimated, raw_meta
        FROM events
        WHERE source = ?
          AND started_at >= datetime('now', ?)
        ORDER BY cost_estimated DESC
        """,
        (source, f"-{int(window_days)} days"),
    )

    full_count = 0
    full_stored = 0.0
    full_recomputed = 0.0
    max_abs = 0.0
    max_pct: float | None = None
    auth_counts: dict[str, int] = {}
    events: list[EventCostAudit] = []

    for row in rows:
        full_count += 1
        cost_stored = float(row["cost_estimated"] or 0.0)
        full_stored += cost_stored

        meta_raw = row["raw_meta"] or "{}"
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}

        model_stats = (
            meta.get("model_stats") if isinstance(meta.get("model_stats"), dict) else {}
        ) or {}
        usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
        tokens = usage.get("tokens") if isinstance(usage.get("tokens"), dict) else {}
        uncached_input_total = int(tokens.get("input_tokens", 0) or 0)
        auth_mode = (
            (usage.get("auth_mode") if isinstance(usage, dict) else None)
            or meta.get("auth_mode")
            or "unknown"
        )
        auth_counts[auth_mode] = auth_counts.get(auth_mode, 0) + 1

        breakdowns = _breakdown_from_model_stats(model_stats, uncached_input_total, provider)
        cost_recomputed = sum(b.cost_total for b in breakdowns)
        full_recomputed += cost_recomputed

        delta_abs = cost_stored - cost_recomputed
        if abs(delta_abs) > abs(max_abs):
            max_abs = delta_abs
        if cost_recomputed > 0:
            pct = (delta_abs / cost_recomputed) * 100.0
            if max_pct is None or abs(pct) > abs(max_pct):
                max_pct = pct

        if len(events) < top:
            events.append(
                EventCostAudit(
                    event_id=str(row["id"]),
                    source=source,
                    started_at=str(row["started_at"]),
                    session_id=meta.get("session_id"),
                    auth_mode=str(auth_mode),
                    cost_stored=round(cost_stored, 6),
                    cost_recomputed=round(cost_recomputed, 6),
                    models=breakdowns,
                )
            )

    summary = CostAuditSummary(
        source=source,
        window_days=window_days,
        total_events=full_count,
        total_cost_stored=round(full_stored, 6),
        total_cost_recomputed=round(full_recomputed, 6),
        max_abs_delta=round(max_abs, 6),
        max_pct_delta=None if max_pct is None else round(max_pct, 4),
        auth_mode_counts=auth_counts,
    )
    return CostAuditReport(summary=summary, events=events)


def _format_currency(value: float) -> str:
    return f"${value:,.4f}"


def _format_tokens(value: int) -> str:
    return f"{value:,}"


def render_markdown_report(reports: list[CostAuditReport]) -> str:
    """Render a Markdown drill-down report safe for stdout / a file."""
    if not reports:
        return "# SHIP cost audit\n\n_No reports to render._\n"

    lines: list[str] = []
    lines.append("# SHIP cost audit (drill-down)")
    lines.append("")
    lines.append(
        "For every audited event, the report lists the SHIP-stored "
        "API-equivalent cost, "
        "a manual re-computation from per-model tokens × the published "
        "rate card (`pricing.py`), and the per-rate breakdown. Use it to "
        "justify every API-equivalent dollar in the reconcile report from "
        "the raw token counts written into your local logs. It does not "
        "claim invoice-grade billed cost."
    )
    lines.append("")

    for report in reports:
        s = report.summary
        lines.append(f"## {s.source}")
        lines.append("")
        lines.append(
            f"Window: last {s.window_days} days. "
            f"Top {len(report.events)} of {s.total_events} events shown."
        )
        lines.append("")
        lines.append("### Summary")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---:|")
        lines.append(f"| Total events | {s.total_events} |")
        lines.append(
            f"| Total API-equivalent cost stored | {_format_currency(s.total_cost_stored)} |"
        )
        lines.append(
            f"| Total API-equivalent cost recomputed (tokens × pricing.py) | {_format_currency(s.total_cost_recomputed)} |"
        )
        lines.append(f"| Max abs delta (stored − recomputed) | {_format_currency(s.max_abs_delta)} |")
        max_pct = "-" if s.max_pct_delta is None else f"{s.max_pct_delta:+.4f}%"
        lines.append(f"| Max pct delta | {max_pct} |")
        if s.auth_mode_counts:
            modes = ", ".join(
                f"`{k}`={v}" for k, v in sorted(s.auth_mode_counts.items())
            )
            lines.append(f"| Auth modes | {modes} |")
        lines.append("")

        if not report.events:
            lines.append("_No events in this window._")
            lines.append("")
            continue

        for ev in report.events:
            lines.append(
                f"### {ev.started_at[:19]} — `{ev.event_id[:16]}` — "
                f"`{ev.auth_mode}` — API-equivalent stored {_format_currency(ev.cost_stored)}"
            )
            lines.append("")
            if ev.session_id:
                lines.append(f"session_id: `{ev.session_id[:24]}`")
                lines.append("")
            lines.append("| Model | Rates ($/M) | Tokens | Cost |")
            lines.append("|---|---|---:|---:|")
            for b in ev.models:
                # Compact per-row description so the table stays readable.
                rates_str = (
                    f"in={b.rates['input']:.2f} | "
                    f"out={b.rates['output']:.2f} | "
                    f"cr={b.rates['cache_read']:.2f} | "
                    f"cw={b.rates['cache_write']:.2f}"
                )
                tokens_str = (
                    f"unc={_format_tokens(b.uncached_input_tokens)} | "
                    f"cr={_format_tokens(b.cache_read_tokens)} | "
                    f"cw={_format_tokens(b.cache_write_tokens)} | "
                    f"out={_format_tokens(b.output_tokens)}"
                )
                lines.append(
                    f"| `{b.model}` | {rates_str} | {tokens_str} | "
                    f"{_format_currency(b.cost_total)} |"
                )
            lines.append("")
            lines.append(
                f"_Recomputed = {_format_currency(ev.cost_recomputed)} · "
                f"stored − recomputed = {_format_currency(ev.delta_abs)}"
            )
            if ev.delta_pct is not None:
                lines.append(f" ({ev.delta_pct:+.4f}%)_")
            else:
                lines.append("_")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_Generated by `ship1000x audit-cost`. Report contains only "
        "aggregate token counts, model labels, and API-equivalent dollar "
        "amounts derived from the published rate card; no event payloads, "
        "attributes, conversation bodies, patch bodies, local paths, or "
        "credential markers are included._"
    )
    return "\n".join(lines) + "\n"
