"""Usage normalization helpers for AI/dev collectors.

Collectors often expose different levels of truth: native token counters,
hourly heuristics, inferred model names, or no cost data at all. This module
keeps that vocabulary explicit and collector-independent while preserving the
existing SQLite schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ship1000x.core.pricing import (
    PRICING_SOURCE,
    PRICING_VERSION,
    pricing_freshness,
    resolve_model_pricing,
)

MeasurementQuality = Literal["factual", "defensible", "indicative", "unknown"]

_POLICY_SNAPSHOT_KEYS = frozenset({
    "provider",
    "checked_at",
    "checked_date",
    "status",
    "risk_label",
    "allowed_usage_claim",
    "policy_url",
})


# ──────────────────────────────────────────────────────────────────────────
# Contrat usage_breakdown — superset normalise cross-provider (capture exhaustive)
#
# Chaque collecteur emet raw_meta["usage_breakdown"] selon CE schema, en
# capturant TOUT ce que le provider expose, meme les champs a 0 (pour ne
# jamais perdre une donnee observable).
#   Invariant : total_input = fresh_input + cache_read + cache_write
#               output_tokens inclut reasoning/thinking selon le provider
#
# FUTURS PROVIDERS (Gemini, Mistral, local LM Studio, ...) : suivre ce schema.
# Mapper les champs natifs ici ; mettre 0 pour ce qui n'est pas expose.
# ──────────────────────────────────────────────────────────────────────────
USAGE_BREAKDOWN_FIELDS = (
    "fresh_input", "cache_read", "cache_write", "cache_write_5m",
    "cache_write_1h", "output_tokens", "thinking", "reasoning",
    "web_search_requests", "web_fetch_requests",
)


def empty_usage_breakdown(provider: str) -> dict:
    """Gabarit usage_breakdown a zero (coherence cross-collector)."""
    d: dict = {f: 0 for f in USAGE_BREAKDOWN_FIELDS}
    d["provider"] = provider
    d["service_tier"] = None
    return d


def format_token_count(n: int | float) -> str:
    """Unites adaptatives lisibles : 1234 -> '1.2 k', 2.3e6 -> '2.3 M', 1.5e9 -> '1.50 Md'."""
    val = float(n or 0)
    a = abs(val)
    if a >= 1e9:
        return f"{val / 1e9:.2f} Md"
    if a >= 1e6:
        return f"{val / 1e6:.1f} M"
    if a >= 1e3:
        return f"{val / 1e3:.0f} k"
    return str(int(val))


@dataclass(frozen=True)
class TokenBreakdown:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def has_native_tokens(self) -> bool:
        return any(
            value > 0
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cached_input_tokens,
                self.cache_write_tokens,
                self.reasoning_tokens,
            )
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


MODEL_ALIASES = {
    "gpt-5": "gpt-5",
    "gpt-5.5": "gpt-5.5",
    # Versions précises observées dans turn_context.model des rollouts Codex
    # 2026. Listées explicitement (mapping identité) pour que le match EXACT
    # gagne avant le fallback substring — sinon `gpt-5.4`/`gpt-5.3-codex`
    # contiennent le substring `gpt-5` et étaient rabattus sur le bucket
    # générique `gpt-5`, masquant la version réellement détectée.
    "gpt-5.4": "gpt-5.4",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.3-codex-spark": "gpt-5.3-codex-spark",
    "gpt-5-codex": "gpt-5-codex",
    "gpt-5-mini": "gpt-5-mini",
    "gpt-5-nano": "gpt-5-nano",
    "gpt-4o": "gpt-4o",
    "gpt-4o-mini": "gpt-4o-mini",
    "o3-mini": "o3-mini",
    "o3": "o3",
    "o1": "o1",
    "claude-opus-4-7": "claude-opus-4-7",
    "claude-sonnet-4-7": "claude-sonnet-4-7",
    "claude-opus-4-8": "claude-opus-4-8",
    "claude-opus-4-6": "claude-opus-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-haiku-4-5": "claude-haiku-4-5",
    # NB: doit rester APRÈS les versions plus précises (4-8/4-7/4-6) : le
    # fallback substring prend l'alias le plus long, mais le match exact
    # (claude-opus-4-8) gagne de toute façon. Sans l'entrée 4-8 ci-dessus,
    # `claude-opus-4-8` était rabattu sur `claude-opus-4` (modèle plus ancien).
    "claude-opus-4": "claude-opus-4",
    "claude-sonnet-4": "claude-sonnet-4",
    "claude-opus-3-5": "claude-opus-3-5",
    "claude-sonnet-3-5": "claude-sonnet-3-5",
    "claude-haiku-3-5": "claude-haiku-3-5",
}


def canonicalize_model(model_raw: str | None) -> str:
    """Return a stable model id when a known alias appears in the raw value."""
    model = (model_raw or "").strip().lower().replace("_", "-").replace(" ", "-")
    if not model:
        return "unknown"
    if model in MODEL_ALIASES:
        return MODEL_ALIASES[model]
    best = ""
    best_canonical = model
    for alias, canonical in MODEL_ALIASES.items():
        if alias in model and len(alias) > len(best):
            best = alias
            best_canonical = canonical
    if best:
        return best_canonical
    return model


def quality_for_tokens(tokens: TokenBreakdown) -> MeasurementQuality:
    return "factual" if tokens.has_native_tokens else "unknown"


def _safe_policy_snapshot(snapshot: dict | None) -> dict | None:
    """Keep only short categorical policy-snapshot metadata.

    The snapshot is local evidence for audits. It is not policy text, legal
    advice, or a compliance certification.
    """
    if not isinstance(snapshot, dict):
        return None
    safe: dict[str, str] = {}
    for key in _POLICY_SNAPSHOT_KEYS:
        value = snapshot.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        safe[key] = text[:200]
    return safe or None


def build_usage_metadata(
    *,
    provider: str,
    client: str,
    model_raw: str | None,
    tokens: TokenBreakdown,
    cost_estimated: float | None,
    cost_quality: MeasurementQuality,
    active_time_quality: MeasurementQuality = "defensible",
    pricing_source: str = PRICING_SOURCE,
    pricing_version: str = PRICING_VERSION,
    cost_basis: str = "token_equivalent_api",
    token_source: str = "native",
    auth_mode: str | None = None,
    provider_policy_snapshot: dict | None = None,
) -> dict:
    """Build safe raw_meta usage metadata with explicit measurement quality.

    ``auth_mode`` is one of ``"oauth"``, ``"api_key"``, or ``"unknown"``
    (default). Collectors that know how their user authenticated to the
    provider should pass this so the dashboard can split SHIP's
    pay-per-token estimate into the **API-equivalent** cost (always) and
    the **billed-estimated** cost (zero under OAuth subscriptions, full
    estimate under API keys). See ``ship1000x.core.auth_mode``.
    """
    model_canonical = canonicalize_model(model_raw)
    pricing = resolve_model_pricing(provider, model_raw)
    token_quality = quality_for_tokens(tokens)
    normalized_cost_quality = cost_quality
    if (
        normalized_cost_quality == "factual"
        and pricing.match_quality in {"fallback", "unknown"}
    ):
        normalized_cost_quality = "indicative"
    elif normalized_cost_quality == "factual" and pricing_freshness().get("stale"):
        # Native tokens priced against a local rate card older than the
        # staleness threshold: the cost stays a DEFENSIBLE modeled estimate
        # (explicit, verifiable assumption = "rates as of PRICING_VERSION"),
        # not an audit-grade factual number, because published rates may have
        # moved. The freshness signal already exists in `pricing_freshness`;
        # this wires it into the per-event confidence the user reads next to
        # the dollar figure instead of leaving it in a sidecar command.
        normalized_cost_quality = "defensible"
    cost_value = round(float(cost_estimated or 0.0), 8)
    normalized_auth_mode = (auth_mode or "unknown").strip().lower()
    if normalized_auth_mode not in {"oauth", "api_key", "unknown"}:
        normalized_auth_mode = "unknown"
    if normalized_auth_mode == "api_key":
        billed_estimated = cost_value
    else:
        # oauth / unknown: pay-per-token estimate is API-equivalent only.
        # Real billing comes from the provider usage report (anthropic_usage,
        # openai_usage); SHIP does not double-count it here.
        billed_estimated = 0.0
    usage = {
        "provider": provider,
        "client": client,
        "model_raw": model_raw or "unknown",
        "model_canonical": model_canonical,
        "tokens": tokens.as_dict(),
        "cost": {
            "estimated_usd": cost_value,
            "api_equivalent_usd": cost_value,
            "billed_estimated_usd": round(billed_estimated, 8),
            "pricing_source": pricing_source,
            "pricing_version": pricing_version,
            "basis": cost_basis,
            "quality": normalized_cost_quality,
            "pricing_match_quality": pricing.match_quality,
            "unknown_model": pricing.unknown_model,
        },
        "auth_mode": normalized_auth_mode,
        "quality": {
            "tokens": token_quality,
            "active_time": active_time_quality,
            "cost": normalized_cost_quality,
        },
        "provenance": {
            "token_source": token_source if token_quality != "unknown" else "not_exposed",
            "model_source": "collector_metadata" if model_raw else "unknown",
            "pricing_source": pricing_source,
            "pricing_model_canonical": pricing.model_canonical,
        },
    }
    safe_policy_snapshot = _safe_policy_snapshot(provider_policy_snapshot)
    if safe_policy_snapshot is not None:
        usage["provider_policy_snapshot"] = safe_policy_snapshot
    return usage


def build_unknown_usage_metadata(
    *,
    provider: str,
    client: str,
    model_raw: str | None = None,
    cost_estimated: float | None = None,
    cost_quality: MeasurementQuality = "indicative",
    cost_basis: str = "hourly_estimate",
    auth_mode: str | None = None,
) -> dict:
    """Usage metadata for clients that do not expose native token counters."""
    return build_usage_metadata(
        provider=provider,
        client=client,
        model_raw=model_raw,
        tokens=TokenBreakdown(),
        cost_estimated=cost_estimated,
        cost_quality=cost_quality,
        cost_basis=cost_basis,
        token_source="not_exposed",
        auth_mode=auth_mode,
    )
