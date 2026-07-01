"""Pricing module : tarifs API officiels des modeles LLM.

Utilise par les collectors pour estimer `cost_estimated` par session meme
quand l'utilisateur est sur abonnement flat (Claude Pro/Max, ChatGPT Plus).
L'idee : afficher le cout equivalent API pour comparaison / ROI. L'user
peut deduire sa vraie facture (abo flat) vs le cout theorique.

Tarifs en USD par million de tokens (input / output).
Sources :
  - Anthropic : https://www.anthropic.com/pricing
  - OpenAI : https://openai.com/api/pricing/

Mise a jour 2026-07-01.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

PRICING_SOURCE = "ship1000x.core.pricing"
PRICING_VERSION = "2026-07-01"
# Au-dela de ce seuil, le snapshot tarifaire est considere perime (les tarifs
# LLM bougent ; un coût API-equivalent base sur de vieux tarifs derive).
PRICING_STALE_DAYS = 60


def pricing_freshness(today: date | None = None) -> dict:
    """Fraicheur du snapshot tarifaire : version, age en jours, perime ?"""
    today = today or date.today()
    try:
        snap = date.fromisoformat(PRICING_VERSION)
        age = (today - snap).days
    except ValueError:
        return {"version": PRICING_VERSION, "age_days": None, "stale": False}
    return {"version": PRICING_VERSION, "age_days": age, "stale": age > PRICING_STALE_DAYS}

PricingMatchQuality = Literal["exact", "alias", "fallback", "unknown"]


@dataclass(frozen=True)
class PricingResolution:
    """Explains how Ship1000x resolved pricing for a raw model string."""

    provider: str
    model_raw: str
    model_canonical: str
    rates: dict
    pricing_source: str = PRICING_SOURCE
    pricing_version: str = PRICING_VERSION
    match_quality: PricingMatchQuality = "unknown"
    unknown_model: bool = True

# Anthropic / Claude
# Tarifs par million de tokens (USD)
ANTHROPIC_PRICING = {
    # Claude Opus 4.6+ family (2026): $5 input / $25 output, cache writes
    # 1.25x input for 5m and cache reads 0.1x input.
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-4-7": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    # Claude 4.6 family (2025-2026)
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    # Legacy pre-2026
    "claude-opus-4": {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-opus-3-5": {"input": 15.0, "output": 75.0},
    "claude-sonnet-3-5": {"input": 3.0, "output": 15.0},
    "claude-haiku-3-5": {"input": 0.80, "output": 4.0},
}


# OpenAI / Codex / GPT
# Tarifs par million de tokens (USD)
# Codex utilise principalement GPT-5 en 2026 (cf "You are Codex, based on GPT-5"
# dans les session_meta des rollouts Codex).
OPENAI_PRICING = {
    # GPT-5 family (2026, Codex inclus)
    # Source : https://openai.com/api/pricing/
    "gpt-5": {"input": 1.25, "output": 10.0, "cached_input": 0.125},
    # Versions précises Codex 2026 (turn_context.model). Elles ont leurs
    # propres tarifs API publics ; on les liste explicitement pour éviter
    # l'alias substring vers gpt-5, qui sous-estime fortement l'équivalent API.
    "gpt-5.5": {"input": 5.0, "output": 30.0, "cached_input": 0.50},
    "gpt-5.4": {"input": 2.50, "output": 15.0, "cached_input": 0.25},
    "gpt-5.3-codex": {"input": 1.25, "output": 10.0, "cached_input": 0.125},
    "gpt-5.3-codex-spark": {"input": 1.25, "output": 10.0, "cached_input": 0.125},
    "gpt-5-codex": {"input": 1.25, "output": 10.0, "cached_input": 0.125},
    # codex-auto-review n'est PAS un modèle OpenAI publié : c'est le label
    # interne SHIP de la passe de revue automatique de Codex. Elle tourne sur
    # la famille gpt-5 (Codex) → on la facture aux tarifs gpt-5 plutôt que de
    # retomber sur DEFAULT_PRICING (3.0/15.0, Sonnet-like) qui sur-estimait un
    # travail OpenAI. On ne devine pas un tarif : on applique celui de la
    # famille de modèles réellement utilisée par l'opération.
    "codex-auto-review": {"input": 1.25, "output": 10.0, "cached_input": 0.125},
    "gpt-5-mini": {"input": 0.25, "output": 2.0, "cached_input": 0.025},
    "gpt-5-nano": {"input": 0.05, "output": 0.40, "cached_input": 0.005},
    # Legacy pre-2026
    "gpt-4o": {"input": 2.50, "output": 10.0, "cached_input": 1.25},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cached_input": 0.075},
    "o1": {"input": 15.0, "output": 60.0, "cached_input": 7.50},
    "o3-mini": {"input": 1.10, "output": 4.40, "cached_input": 0.55},
    "o3": {"input": 2.0, "output": 8.0, "cached_input": 0.50},
}


# Fallback commun : si le modele n'est pas trouve, on utilise Sonnet-like
DEFAULT_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cached_input": 0.30}


def _resolve_model(model: str, table: dict) -> tuple[str, dict, PricingMatchQuality, bool]:
    """Resolve model pricing and make fallback explicit.

    Cherche un match exact, sinon un substring canonique (ex:
    "gpt-5-codex-preview" matche "gpt-5-codex"), sinon DEFAULT_PRICING.
    """
    if not model:
        return "unknown", DEFAULT_PRICING, "unknown", True
    model_l = model.lower().strip()
    # Match exact
    if model_l in table:
        return model_l, table[model_l], "exact", False
    # Match substring (le plus long gagne pour eviter gpt-5 qui matche avant gpt-5-codex)
    best_match = None
    best_len = 0
    for key in table:
        if key in model_l and len(key) > best_len:
            best_match = key
            best_len = len(key)
    if best_match:
        return best_match, table[best_match], "alias", False
    return model_l, DEFAULT_PRICING, "fallback", True


def _match_model(model: str, table: dict) -> dict:
    """Retourne les tarifs pour un model_id donne.

    Backward-compatible wrapper around the explicit pricing resolution.
    """
    return _resolve_model(model, table)[1]


def resolve_model_pricing(provider: str, model: str | None) -> PricingResolution:
    """Return pricing rates plus provenance/quality for a raw model string.

    Uses the SHIP-local pricing tables only. For the hybrid LiteLLM +
    local resolution (preferred path post-Wave 1), see
    :func:`resolve_model_pricing_hybrid`.
    """
    provider_l = (provider or "unknown").strip().lower()
    model_raw = model or "unknown"
    if provider_l in {"openai", "codex", "gpt"}:
        canonical, rates, quality, unknown = _resolve_model(model_raw, OPENAI_PRICING)
        provider_l = "openai"
    elif provider_l in {"anthropic", "claude"}:
        canonical, rates, quality, unknown = _resolve_model(model_raw, ANTHROPIC_PRICING)
        provider_l = "anthropic"
    else:
        canonical, rates, quality, unknown = _resolve_model(model_raw, {**OPENAI_PRICING, **ANTHROPIC_PRICING})
    return PricingResolution(
        provider=provider_l,
        model_raw=model_raw,
        model_canonical=canonical,
        rates=rates,
        match_quality=quality,
        unknown_model=unknown,
    )


def resolve_model_pricing_hybrid(
    provider: str | None,
    model: str | None,
    *,
    prefer_litellm: bool = True,
    resolver: object | None = None,
) -> PricingResolution:
    """Resolve pricing trying a primary resolver first, then the SHIP-local table.

    Wave 2 makes the ``PricingResolver`` port the explicit injection
    point. The default primary resolver is the LiteLLM-backed one
    (``ship1000x.core.pricing_litellm.LiteLLMResolver``), but callers
    can pass any object satisfying the ``PricingResolver`` protocol
    (``ship1000x.ports.PricingResolver``) to swap the source — useful
    for Premium adapters that resolve against a live billing API or
    a cohort-specific snapshot.

    The returned ``PricingResolution`` exposes the actual source used
    via its ``pricing_source`` field, so downstream audit consumers
    (``ship1000x audit-cost``, ``ship1000x coverage``) can tell which
    resolution path a given event used. This is intentional: pricing
    provenance is part of the cost honesty contract.

    Parameters
    ----------
    provider : str | None
        SHIP provider label (``"anthropic"``, ``"openai"``, ...). Used
        only as a fallback hint — the primary resolver derives the
        real provider from the snapshot entry itself when possible.
    model : str | None
        Raw model id as observed in the source (e.g. ``"gpt-5.5"``,
        ``"claude-opus-4-7"``, ``"gpt-5-codex-preview"``).
    prefer_litellm : bool, default True
        Set False to bypass any primary resolver and return the local
        table resolution directly. Useful for regression testing or
        for operators who want to lock pricing to the SHIP local table
        snapshot.
    resolver : object | None, default None
        Optional override implementing the ``PricingResolver`` port
        (``.resolve(provider, model) -> PricingResolution | None``).
        When None, the LiteLLM default resolver is used. Ignored if
        ``prefer_litellm`` is False.
    """
    if prefer_litellm:
        if resolver is not None:
            # Injected port — Premium adapter, custom test double, etc.
            resolved = resolver.resolve(provider, model)
            if resolved is not None:
                return resolved
        else:
            # Default LiteLLM-backed resolver. Imported lazily to
            # avoid a circular import (pricing_litellm imports this
            # module for the PricingResolution dataclass).
            from ship1000x.core.pricing_litellm import resolve_via_litellm

            resolved = resolve_via_litellm(provider, model)
            if resolved is not None:
                return resolved
    return resolve_model_pricing(provider or "unknown", model)


def estimate_anthropic_cost(
    model: str,
    tokens_input: int = 0,
    tokens_output: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_discount: bool = True,
) -> float:
    """Cout estime USD pour une session Anthropic (Claude Code).

    Les tokens `cache_read` sont factures a ~10% du tarif input chez Anthropic.
    Les tokens `cache_write` sont factures a ~125% du tarif input.
    Par defaut 0 si non fournis → compatible avec l'ancien appel tokens_in/tokens_out.

    `cache_discount=False` calcule le cout "sans cache" (borne haute) :
    `cache_read`/`cache_write` sont factures au tarif input plein, comme si
    aucune remise de cache ne s'appliquait. Sert a chiffrer l'economie reelle
    du cache (ecart entre avec-cache et sans-cache).
    """
    rates = _match_model(model, ANTHROPIC_PRICING)
    cost = 0.0
    cost += (tokens_input / 1_000_000) * rates.get("input", DEFAULT_PRICING["input"])
    cost += (tokens_output / 1_000_000) * rates.get("output", DEFAULT_PRICING["output"])
    if cache_discount:
        cost += (cache_read_tokens / 1_000_000) * rates.get("cache_read", 0.0)
        cost += (cache_write_tokens / 1_000_000) * rates.get("cache_write", 0.0)
    else:
        full_input_rate = rates.get("input", DEFAULT_PRICING["input"])
        cost += ((cache_read_tokens + cache_write_tokens) / 1_000_000) * full_input_rate
    return cost


def estimate_openai_cost(
    model: str,
    tokens_input: int = 0,
    tokens_output: int = 0,
    cached_input_tokens: int = 0,
    reasoning_output_tokens: int = 0,
    cache_discount: bool = True,
) -> float:
    """Cout estime USD pour une session OpenAI (Codex, GPT-5, etc.).

    `cached_input_tokens` : tokens lus depuis le cache OpenAI (reduction 90%).
      Ils sont INCLUS dans tokens_input cote OpenAI → on soustrait puis applique
      le tarif reduit.
    `reasoning_output_tokens` : tokens de raisonnement GPT-5 / o-series. Factures
      comme output standard (inclus dans tokens_output cote JSONL Codex).

    `cache_discount=False` calcule le cout "sans cache" (borne haute) :
    `cached_input_tokens` est facture au tarif input plein au lieu du tarif
    cache reduit.
    """
    rates = _match_model(model, OPENAI_PRICING)
    # Tokens input non-caches = total - caches
    non_cached_input = max(0, tokens_input - cached_input_tokens)
    full_input_rate = rates.get("input", DEFAULT_PRICING["input"])
    cost = 0.0
    cost += (non_cached_input / 1_000_000) * full_input_rate
    if cache_discount:
        cost += (cached_input_tokens / 1_000_000) * rates.get(
            "cached_input", full_input_rate * 0.1
        )
    else:
        cost += (cached_input_tokens / 1_000_000) * full_input_rate
    # reasoning_output deja inclus dans tokens_output cote Codex rollout : pas
    # besoin de l'ajouter separement. Parametre conserve pour traçabilite.
    cost += (tokens_output / 1_000_000) * rates.get("output", DEFAULT_PRICING["output"])
    return cost


# ─── Sources estimees par heure vs tokens reels ──────────────────────
# Certaines apps ne captent pas les tokens reels (logs Electron frontend,
# DB Cursor...) : on estime leur cout a partir du temps actif x tarif
# horaire (generalement calibre sur les abos Pro/Max pour rester realiste).
#
# Utilise par les exporters pour distinguer "cost-if-metered" (tokens reels
# Anthropic/OpenAI) de "cost-if-hourly" (estime pour apps fermees).
HOURLY_ESTIMATED_SOURCES = frozenset({
    "codex_macapp",     # Codex.app macOS : logs Electron, pas de tokens
    "codex_desktop",    # Codex Desktop SSE : echantillonne, pas de facturation tokens
    "cursor",           # Cursor AI : DB fermee, heures uniquement
    "cline",            # Extension Cline (Cursor) : idem
})


def is_hourly_estimated(source: str) -> bool:
    """True si le cout de cette source est estime par heure (pas par tokens reels)."""
    return source in HOURLY_ESTIMATED_SOURCES
