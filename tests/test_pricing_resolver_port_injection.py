"""Wave 2 / Day 2 — verify resolve_model_pricing_hybrid honours an
injected ``PricingResolver``.

The Wave 1 hybrid resolver always reached for the LiteLLM default.
Wave 2 makes it port-injectable so Premium adapters (or tests) can
swap the primary source without touching the call site.
"""

from __future__ import annotations

from ship1000x.core.pricing import (
    OPENAI_PRICING,
    PricingResolution,
    resolve_model_pricing_hybrid,
)
from ship1000x.ports import PricingResolver


class _FakeResolver:
    """Test double satisfying the PricingResolver Protocol structurally."""

    def __init__(self, override_rate: float):
        self.override_rate = override_rate
        self.calls: list[tuple[str | None, str | None]] = []

    def resolve(
        self,
        provider: str | None,
        model_raw: str | None,
    ) -> PricingResolution | None:
        self.calls.append((provider, model_raw))
        if not model_raw:
            return None
        return PricingResolution(
            provider="custom",
            model_raw=model_raw,
            model_canonical=model_raw.lower(),
            rates={
                "input": self.override_rate,
                "output": self.override_rate * 8,
                "cache_read": self.override_rate / 10,
                "cache_write": self.override_rate * 1.25,
                "cached_input": self.override_rate / 10,
            },
            pricing_source="test.fake_resolver",
            pricing_version="test",
            match_quality="exact",
            unknown_model=False,
        )


def test_fake_resolver_satisfies_pricing_resolver_port():
    """Structural conformance check — no subclassing needed."""
    assert isinstance(_FakeResolver(42.0), PricingResolver)


def test_injected_resolver_wins_over_default_litellm():
    """When the caller injects a custom resolver, it takes precedence
    over the default LiteLLM path. The marker rate (99.0) proves the
    injection actually fired."""
    fake = _FakeResolver(override_rate=99.0)
    resolution = resolve_model_pricing_hybrid("openai", "gpt-5", resolver=fake)
    assert resolution.rates["input"] == 99.0
    assert resolution.pricing_source == "test.fake_resolver"
    # The fake was actually called.
    assert fake.calls == [("openai", "gpt-5")]


def test_injected_resolver_returning_none_falls_back_to_local_table():
    """A custom resolver may return None for models it doesn't know;
    the hybrid resolver then falls back to the local SHIP table (NOT
    to LiteLLM — the injection is meant to replace LiteLLM, not
    chain with it)."""

    class _AlwaysNone:
        def resolve(self, provider, model_raw):
            return None

    resolver = _AlwaysNone()
    resolution = resolve_model_pricing_hybrid("openai", "gpt-5", resolver=resolver)
    # Local fallback rate (1.25 from OPENAI_PRICING)
    assert resolution.rates["input"] == OPENAI_PRICING["gpt-5"]["input"]


def test_prefer_litellm_false_ignores_injected_resolver():
    """``prefer_litellm=False`` is the explicit bypass. The injected
    resolver must not be consulted in this case."""
    fake = _FakeResolver(override_rate=99.0)
    resolution = resolve_model_pricing_hybrid(
        "openai", "gpt-5", prefer_litellm=False, resolver=fake
    )
    assert resolution.rates["input"] != 99.0
    assert fake.calls == []  # never invoked


def test_default_path_still_uses_litellm_when_resolver_is_none():
    """Backward compatibility: no resolver argument means the Wave 1
    behavior is preserved — LiteLLM is the primary source."""
    resolution = resolve_model_pricing_hybrid("openai", "gpt-5")
    # gpt-5 is in the vendored LiteLLM snapshot; the source label
    # confirms we went through LiteLLM, not the local table.
    assert "litellm" in resolution.pricing_source.lower()
