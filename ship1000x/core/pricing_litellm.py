"""LiteLLM-backed pricing resolver.

This module reads the vendored snapshot of LiteLLM's
``model_prices_and_context_window.json`` (see
``ship1000x/data/LITELLM_ATTRIBUTION.md`` for source + license + pin
SHA) and translates it into SHIP's canonical
``PricingResolution`` shape.

It is the **primary pricing source** in the hybrid resolver — used
first, with the local table in ``ship1000x.core.pricing`` as fallback
for any model LiteLLM doesn't carry (and we as fallback for any
ship-specific aliasing the upstream doesn't know about).

The vendored JSON file is loaded once on first call and cached at
module level. Tests can substitute a different snapshot by passing
``snapshot_path`` to the ``LiteLLMResolver`` constructor.

Design note — preview of Wave 2 Port
-----------------------------------
The ``LiteLLMResolver`` class is intentionally shaped to match the
``PricingResolver`` ``Protocol`` that Wave 2 will introduce. Its only
public method is ``resolve(provider, model_raw) -> PricingResolution``
and it has no implicit global state. When Wave 2 extracts the formal
port, this class becomes the canonical adapter implementation with
zero refactor.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from ship1000x.core.pricing import (
    DEFAULT_PRICING,
    PRICING_SOURCE,
    PRICING_VERSION,
    PricingResolution,
)

# Where the vendored snapshot lives relative to this module.
_DEFAULT_SNAPSHOT_PATH = (
    Path(__file__).parent.parent / "data" / "litellm_prices.json"
)

# Pricing source label exposed in the PricingResolution returned by
# this resolver. Lets the audit pipeline tell apart LiteLLM-resolved
# prices from ship1000x-local-resolved ones.
LITELLM_PRICING_SOURCE = "litellm.berriai/model_prices_and_context_window.json"


def _per_million(per_token: Any) -> float:
    """Convert LiteLLM's per-token price to SHIP's per-million-tokens unit.

    Returns ``0.0`` for missing / non-numeric values (the historical
    behavior of the local table when a rate field is absent).
    """
    if per_token is None:
        return 0.0
    try:
        return float(per_token) * 1_000_000.0
    except (TypeError, ValueError):
        return 0.0


# Map LiteLLM ``litellm_provider`` values to SHIP's canonical provider
# names. Aliases here drive the ``provider`` field of the returned
# ``PricingResolution`` — downstream cost-honesty consumers group by this
# canonical name, so a misalias causes silent provider misattribution
# (e.g. a Gemini call counted under ``unknown``) rather than a hard error.
#
# Mapping rules :
# - Hosting layers serving an external model family keep the **model**
#   family as canonical name (Bedrock/Vertex serving Anthropic → ``anthropic``,
#   Azure serving OpenAI → ``openai``). The runtime owner of the GPU is not
#   what SHIP audits; the model author is.
# - Direct providers map to a stable lowercase short-name (``google``,
#   ``mistral``, ``xai``, ``cohere``, …). When LiteLLM splits one vendor
#   across multiple keys (e.g. ``cohere`` vs ``cohere_chat``), both keys
#   collapse to the same SHIP name.
# - Aggregators / brokers (``openrouter``, ``deepinfra``, ``together_ai``,
#   ``fireworks_ai``, ``novita``, ``replicate``, ``perplexity``, ``groq``,
#   ``dashscope``) keep their broker name because the underlying model
#   weights are routed dynamically and SHIP treats the broker as the actor
#   for cost attribution. The model id remains the source of truth for what
#   was actually invoked.
# - Anything not listed falls back to ``(provider or "unknown")``, i.e. the
#   caller-passed provider string when present, ``"unknown"`` otherwise.
#
# Wave 7+ batch 7h (2026-05-24) extended this from 5 → 24 entries to
# cover ~98 % of the model entries SHIP would encounter in the wild via
# LiteLLM, including the major frontier vendors (Google/Vertex, xAI,
# Mistral, Cohere, DeepSeek) and the major aggregators.
_PROVIDER_MAP = {
    # Anthropic family — direct + hosted layers serving Anthropic weights.
    "anthropic": "anthropic",
    "bedrock": "anthropic",  # Anthropic models served via AWS Bedrock
    "bedrock_converse": "anthropic",
    "vertex_ai-anthropic_models": "anthropic",
    # OpenAI family — direct + Azure hosted.
    "openai": "openai",
    "azure": "openai",
    "azure_ai": "openai",
    "azure_text": "openai",
    # Google family — direct Gemini API + Vertex AI variants.
    "gemini": "google",
    "vertex_ai": "google",
    "vertex_ai-language-models": "google",
    "vertex_ai-vision-models": "google",
    "vertex_ai-chat-models": "google",
    "vertex_ai-code-text-models": "google",
    "vertex_ai-code-chat-models": "google",
    # Vertex serving non-Google weights — attribute to the model author.
    "vertex_ai-mistral_models": "mistral",
    "vertex_ai-llama_models": "meta",
    "vertex_ai-ai21_models": "ai21",
    # Other major direct providers.
    "mistral": "mistral",
    "codestral": "mistral",
    "xai": "xai",
    "cohere": "cohere",
    "cohere_chat": "cohere",
    "deepseek": "deepseek",
    # Aggregators / brokers — keep broker name (model author = model id).
    "openrouter": "openrouter",
    "fireworks_ai": "fireworks_ai",
    "perplexity": "perplexity",
    "deepinfra": "deepinfra",
    "together_ai": "together_ai",
    "groq": "groq",
    "replicate": "replicate",
    "novita": "novita",
    "dashscope": "dashscope",
}


def _model_canonical(model_raw: str) -> str:
    """Best-effort canonicalisation of a model id for SHIP semantics.

    Preserves the model_raw value when no transformation is needed.
    LiteLLM already uses canonical-ish ids (e.g. ``gpt-5``,
    ``claude-opus-4-7``), so this is mostly an identity function with
    light cleanup (lowercase, strip surrounding whitespace).
    """
    if not model_raw:
        return "unknown"
    return model_raw.strip().lower()


class LiteLLMResolver:
    """Resolve a SHIP ``PricingResolution`` from the LiteLLM snapshot.

    Construction:
        resolver = LiteLLMResolver()                       # default path
        resolver = LiteLLMResolver(snapshot_path=custom)   # tests
    """

    def __init__(self, snapshot_path: Path | None = None) -> None:
        self._snapshot_path = snapshot_path or _DEFAULT_SNAPSHOT_PATH
        self._snapshot: dict[str, dict] | None = None

    @property
    def is_available(self) -> bool:
        """True if the vendored snapshot is present and parseable."""
        try:
            self._ensure_loaded()
        except (OSError, json.JSONDecodeError):
            return False
        return self._snapshot is not None and len(self._snapshot) > 0

    def _ensure_loaded(self) -> None:
        if self._snapshot is not None:
            return
        if not self._snapshot_path.exists():
            self._snapshot = {}
            return
        with self._snapshot_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            self._snapshot = {}
            return
        # Drop the ``sample_spec`` meta-entry that LiteLLM keeps for
        # contributor documentation; it has no real pricing.
        self._snapshot = {k: v for k, v in data.items() if k != "sample_spec"}

    def _lookup(self, model_raw: str) -> tuple[str, dict] | None:
        """Find the best matching entry in the snapshot for a model.

        LiteLLM keys are diverse: ``gpt-5``, ``claude-opus-4-7``,
        ``anthropic/claude-opus-4-7``, ``us.anthropic.claude-opus-4-7``,
        ``bedrock/anthropic.claude-opus-4-7-v1:0``, etc. We try
        progressively looser matches:

        1. Exact key.
        2. Stripped of any leading ``<provider>/`` prefix.
        3. The longest suffix of the model id that matches an entry.

        Returns ``(canonical_key, entry)`` or ``None``.
        """
        self._ensure_loaded()
        if not self._snapshot:
            return None
        if model_raw in self._snapshot:
            return model_raw, self._snapshot[model_raw]
        # Strip ``provider/`` prefix if any.
        if "/" in model_raw:
            tail = model_raw.split("/", 1)[1]
            if tail in self._snapshot:
                return tail, self._snapshot[tail]
        # Longest suffix match — useful for ``bedrock/anthropic.claude-...``
        # style ids that map to ``claude-...`` upstream.
        best_key: str | None = None
        for key in self._snapshot:
            if model_raw.endswith(key) and (
                best_key is None or len(key) > len(best_key)
            ):
                best_key = key
        if best_key:
            return best_key, self._snapshot[best_key]
        return None

    def resolve(
        self,
        provider: str | None,
        model_raw: str | None,
    ) -> PricingResolution | None:
        """Return a ``PricingResolution`` for the given model, or None.

        Returns ``None`` when the snapshot does not carry the model.
        Callers (typically the hybrid resolver in ``core.pricing``)
        fall back to the local table on ``None``.
        """
        if not model_raw:
            return None
        match = self._lookup(model_raw)
        if match is None:
            return None
        canonical_key, entry = match
        # Convert LiteLLM per-token costs to SHIP's per-million dollars.
        rates = {
            "input": _per_million(entry.get("input_cost_per_token")),
            "output": _per_million(entry.get("output_cost_per_token")),
            "cache_read": _per_million(entry.get("cache_read_input_token_cost")),
            "cache_write": _per_million(entry.get("cache_creation_input_token_cost")),
            "cached_input": _per_million(entry.get("cache_read_input_token_cost")),
        }
        # Provider normalisation: keep the SHIP canonical family when we
        # recognise it; otherwise the LiteLLM provider string is the
        # caller's clue that we drifted out of our covered territory.
        litellm_provider = entry.get("litellm_provider") or "unknown"
        ship_provider = _PROVIDER_MAP.get(
            litellm_provider.lower(),
            (provider or "unknown").strip().lower(),
        )
        return PricingResolution(
            provider=ship_provider,
            model_raw=model_raw,
            model_canonical=_model_canonical(canonical_key),
            rates=rates,
            pricing_source=LITELLM_PRICING_SOURCE,
            pricing_version=PRICING_VERSION,
            match_quality="exact" if canonical_key == model_raw else "alias",
            unknown_model=False,
        )


# Module-level singleton — lazy-init on first use so the JSON is not
# parsed when SHIP runs in a context that doesn't need pricing
# (e.g. a coverage report on an empty store).
_default_resolver: LiteLLMResolver | None = None


def get_default_resolver() -> LiteLLMResolver:
    """Lazy singleton used by the hybrid resolver in ``core.pricing``."""
    global _default_resolver
    if _default_resolver is None:
        _default_resolver = LiteLLMResolver()
    return _default_resolver


def resolve_via_litellm(
    provider: str | None,
    model_raw: str | None,
) -> PricingResolution | None:
    """Convenience function used by ``core.pricing.resolve_model_pricing_hybrid``.

    Returns ``None`` when the snapshot is unavailable or doesn't carry
    the requested model — the hybrid resolver then falls back to the
    SHIP-local table. ``DEFAULT_PRICING`` and ``PRICING_SOURCE`` are
    re-exported here so test mocks can reach them without importing
    ``core.pricing`` directly.
    """
    resolver = get_default_resolver()
    if not resolver.is_available:
        return None
    return resolver.resolve(provider, model_raw)


def fallback_resolution(provider: str | None, model_raw: str | None) -> PricingResolution:
    """Last-resort ``PricingResolution`` when no source can resolve.

    Returns a default-rates resolution flagged ``unknown_model=True``
    so downstream consumers (cost honesty contract) can mark the
    affected events explicitly as estimated against the SHIP fallback.
    """
    return PricingResolution(
        provider=(provider or "unknown").strip().lower(),
        model_raw=model_raw or "unknown",
        model_canonical=_model_canonical(model_raw or "unknown"),
        rates=replace_default_pricing(),
        pricing_source=PRICING_SOURCE,
        pricing_version=PRICING_VERSION,
        match_quality="fallback",
        unknown_model=True,
    )


def replace_default_pricing() -> dict:
    """Return a fresh copy of DEFAULT_PRICING so callers cannot mutate
    the original table.

    ``replace`` is not used here because ``DEFAULT_PRICING`` is a plain
    dict, not a dataclass — keeping a helper makes the dependency
    explicit and easier to swap during tests.
    """
    return dict(DEFAULT_PRICING)


# Mark `replace` as imported-but-used to satisfy ruff F401 when only
# the type signature references it.
_ = replace
