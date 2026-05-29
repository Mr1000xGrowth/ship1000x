"""``PricingResolver`` port — resolve a model id to its rate card.

The canonical adapter today is
:class:`ship1000x.core.pricing_litellm.LiteLLMResolver` (introduced in
Wave 1, see CHANGELOG v0.6.0). Premium adapters in the future may
fetch live billing rates from a provider Admin API instead of the
vendored LiteLLM snapshot; the port keeps both interchangeable.

The protocol is intentionally minimal — one method, returning either
the resolution or ``None``. Hybrid resolution (try one source, fall
back to another) is the responsibility of a *composing* adapter, not
of the port.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ship1000x.core.pricing import PricingResolution


@runtime_checkable
class PricingResolver(Protocol):
    """A resolver maps ``(provider, model_raw)`` to a :class:`PricingResolution`.

    Returning ``None`` means "I don't know this model". Callers that
    need a final answer can compose multiple resolvers behind a
    fallback chain.

    The port deliberately does NOT mandate a constructor signature.
    Adapters may take a snapshot path, an API key, a database
    connection, a request handle — whatever they need. The port only
    cares about the call shape.
    """

    def resolve(
        self,
        provider: str | None,
        model_raw: str | None,
    ) -> PricingResolution | None:
        """Look up the rate card for a model.

        Parameters
        ----------
        provider : str | None
            Canonical provider name SHIP uses (``"anthropic"``,
            ``"openai"``, ...). Adapters may use this as a hint or
            ignore it if the underlying source carries its own
            provider information.
        model_raw : str | None
            The model id as observed in the source. May include
            provider prefix (``"anthropic/claude-opus-4-7"``) or a
            full Bedrock-style key. Adapters do their own normalisation.

        Returns
        -------
        PricingResolution | None
            A ``PricingResolution`` instance with rates per million
            tokens, provenance and match quality, or ``None`` when
            the adapter does not carry the model.
        """
        ...
