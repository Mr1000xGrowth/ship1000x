"""Pricing module : tarifs API officiels des modeles LLM.

Utilise par les collectors pour estimer `cost_estimated` par session meme
quand l'utilisateur est sur abonnement flat (Claude Pro/Max, ChatGPT Plus).
L'idee : afficher le cout equivalent API pour comparaison / ROI. L'user
peut deduire sa vraie facture (abo flat) vs le cout theorique.

Tarifs en USD par million de tokens (input / output).
Sources :
  - Anthropic : https://www.anthropic.com/pricing
  - OpenAI : https://openai.com/api/pricing/

Mise a jour 2026-04-21.
"""

from __future__ import annotations


# Anthropic / Claude
# Tarifs par million de tokens (USD)
ANTHROPIC_PRICING = {
    # Claude 4.6 family (2026)
    "claude-opus-4-6": {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75},
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
    "gpt-5-codex": {"input": 1.25, "output": 10.0, "cached_input": 0.125},
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


def _match_model(model: str, table: dict) -> dict:
    """Retourne les tarifs pour un model_id donne.

    Cherche un match exact, sinon un prefixe (ex: "gpt-5-codex-preview" matche
    "gpt-5-codex"), sinon DEFAULT_PRICING.
    """
    if not model:
        return DEFAULT_PRICING
    model_l = model.lower().strip()
    # Match exact
    if model_l in table:
        return table[model_l]
    # Match substring (le plus long gagne pour eviter gpt-5 qui matche avant gpt-5-codex)
    best_match = None
    best_len = 0
    for key in table:
        if key in model_l and len(key) > best_len:
            best_match = key
            best_len = len(key)
    if best_match:
        return table[best_match]
    return DEFAULT_PRICING


def estimate_anthropic_cost(
    model: str,
    tokens_input: int = 0,
    tokens_output: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Cout estime USD pour une session Anthropic (Claude Code).

    Les tokens `cache_read` sont factures a ~10% du tarif input chez Anthropic.
    Les tokens `cache_write` sont factures a ~125% du tarif input.
    Par defaut 0 si non fournis → compatible avec l'ancien appel tokens_in/tokens_out.
    """
    rates = _match_model(model, ANTHROPIC_PRICING)
    cost = 0.0
    cost += (tokens_input / 1_000_000) * rates.get("input", DEFAULT_PRICING["input"])
    cost += (tokens_output / 1_000_000) * rates.get("output", DEFAULT_PRICING["output"])
    cost += (cache_read_tokens / 1_000_000) * rates.get("cache_read", 0.0)
    cost += (cache_write_tokens / 1_000_000) * rates.get("cache_write", 0.0)
    return cost


def estimate_openai_cost(
    model: str,
    tokens_input: int = 0,
    tokens_output: int = 0,
    cached_input_tokens: int = 0,
    reasoning_output_tokens: int = 0,
) -> float:
    """Cout estime USD pour une session OpenAI (Codex, GPT-5, etc.).

    `cached_input_tokens` : tokens lus depuis le cache OpenAI (reduction 90%).
      Ils sont INCLUS dans tokens_input cote OpenAI → on soustrait puis applique
      le tarif reduit.
    `reasoning_output_tokens` : tokens de raisonnement GPT-5 / o-series. Factures
      comme output standard (inclus dans tokens_output cote JSONL Codex).
    """
    rates = _match_model(model, OPENAI_PRICING)
    # Tokens input non-caches = total - caches
    non_cached_input = max(0, tokens_input - cached_input_tokens)
    cost = 0.0
    cost += (non_cached_input / 1_000_000) * rates.get("input", DEFAULT_PRICING["input"])
    cost += (cached_input_tokens / 1_000_000) * rates.get(
        "cached_input", rates.get("input", DEFAULT_PRICING["input"]) * 0.1
    )
    # reasoning_output deja inclus dans tokens_output cote Codex rollout : pas
    # besoin de l'ajouter separement. Parametre conserve pour traçabilite.
    cost += (tokens_output / 1_000_000) * rates.get("output", DEFAULT_PRICING["output"])
    return cost
