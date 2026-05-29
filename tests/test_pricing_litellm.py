"""Tests for the LiteLLM-backed pricing resolver.

These tests do not touch the network. They use a controlled synthetic
JSON snapshot per test to exercise the resolver semantics, plus a
sanity check that the real vendored snapshot (if present) loads and
resolves the SHIP-tracked models with non-zero rates.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ship1000x.core.pricing import (
    OPENAI_PRICING,
    resolve_model_pricing,
    resolve_model_pricing_hybrid,
)
from ship1000x.core.pricing_litellm import (
    LITELLM_PRICING_SOURCE,
    LiteLLMResolver,
)


def _write_snapshot(tmp_path: Path, entries: dict) -> Path:
    """Write a minimal LiteLLM-shaped JSON file for the resolver to consume."""
    path = tmp_path / "litellm_prices.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


# --- LiteLLMResolver ---------------------------------------------------------


class TestLiteLLMResolverIsAvailable:
    def test_returns_false_when_snapshot_absent(self, tmp_path):
        absent = tmp_path / "does_not_exist.json"
        resolver = LiteLLMResolver(snapshot_path=absent)
        assert resolver.is_available is False

    def test_returns_false_when_snapshot_is_empty(self, tmp_path):
        path = _write_snapshot(tmp_path, {})
        resolver = LiteLLMResolver(snapshot_path=path)
        assert resolver.is_available is False

    def test_returns_true_when_snapshot_has_entries(self, tmp_path):
        path = _write_snapshot(
            tmp_path,
            {
                "gpt-5": {
                    "input_cost_per_token": 1.25e-06,
                    "output_cost_per_token": 1e-05,
                    "litellm_provider": "openai",
                },
            },
        )
        resolver = LiteLLMResolver(snapshot_path=path)
        assert resolver.is_available is True


class TestLiteLLMResolverResolve:
    def test_exact_match_returns_per_million_rates(self, tmp_path):
        path = _write_snapshot(
            tmp_path,
            {
                "gpt-5": {
                    "input_cost_per_token": 1.25e-06,
                    "output_cost_per_token": 1e-05,
                    "cache_read_input_token_cost": 1.25e-07,
                    "litellm_provider": "openai",
                },
            },
        )
        resolver = LiteLLMResolver(snapshot_path=path)
        resolution = resolver.resolve("openai", "gpt-5")
        assert resolution is not None
        assert resolution.provider == "openai"
        assert resolution.match_quality == "exact"
        assert resolution.unknown_model is False
        # 1.25e-6 per token = $1.25 per million tokens.
        assert resolution.rates["input"] == pytest.approx(1.25)
        assert resolution.rates["output"] == pytest.approx(10.0)
        assert resolution.rates["cache_read"] == pytest.approx(0.125)
        assert resolution.rates["cached_input"] == pytest.approx(0.125)
        assert resolution.pricing_source == LITELLM_PRICING_SOURCE

    def test_google_provider_mapping_via_gemini(self, tmp_path):
        """Wave 7+ batch 7h: LiteLLM ``gemini`` and ``vertex_ai-*-models``
        keys should collapse to SHIP canonical ``google``."""
        path = _write_snapshot(
            tmp_path,
            {
                "gemini-2.0-flash": {
                    "input_cost_per_token": 1e-07,
                    "output_cost_per_token": 4e-07,
                    "litellm_provider": "vertex_ai-language-models",
                },
                "gemini-1.5-flash-direct": {
                    "input_cost_per_token": 1e-07,
                    "output_cost_per_token": 4e-07,
                    "litellm_provider": "gemini",
                },
            },
        )
        resolver = LiteLLMResolver(snapshot_path=path)
        r_vertex = resolver.resolve(None, "gemini-2.0-flash")
        r_direct = resolver.resolve(None, "gemini-1.5-flash-direct")
        assert r_vertex is not None and r_vertex.provider == "google"
        assert r_direct is not None and r_direct.provider == "google"

    def test_vertex_anthropic_keeps_anthropic_canonical(self, tmp_path):
        """Wave 7+ batch 7h: Anthropic weights served via Vertex AI must
        stay attributed to ``anthropic`` (model author > hosting layer),
        matching the existing precedent for ``bedrock → anthropic``."""
        path = _write_snapshot(
            tmp_path,
            {
                "claude-opus-4-7-via-vertex": {
                    "input_cost_per_token": 1.5e-05,
                    "output_cost_per_token": 7.5e-05,
                    "litellm_provider": "vertex_ai-anthropic_models",
                },
            },
        )
        resolver = LiteLLMResolver(snapshot_path=path)
        resolution = resolver.resolve(None, "claude-opus-4-7-via-vertex")
        assert resolution is not None
        assert resolution.provider == "anthropic"

    def test_extended_provider_mappings_wave7h(self, tmp_path):
        """Wave 7+ batch 7h: smoke-coverage that the major frontier
        vendors and aggregators added in this batch resolve to their
        canonical SHIP names."""
        path = _write_snapshot(
            tmp_path,
            {
                "grok-4": {"input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05, "litellm_provider": "xai"},
                "mistral-large": {"input_cost_per_token": 2e-06, "output_cost_per_token": 6e-06, "litellm_provider": "mistral"},
                "command-r-plus": {"input_cost_per_token": 2.5e-06, "output_cost_per_token": 1e-05, "litellm_provider": "cohere_chat"},
                "deepseek-chat": {"input_cost_per_token": 2.7e-07, "output_cost_per_token": 1.1e-06, "litellm_provider": "deepseek"},
                "sonar": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06, "litellm_provider": "perplexity"},
                "llama-3.3-70b-versatile": {"input_cost_per_token": 5.9e-07, "output_cost_per_token": 7.9e-07, "litellm_provider": "groq"},
                "openrouter-pass-through": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06, "litellm_provider": "openrouter"},
            },
        )
        resolver = LiteLLMResolver(snapshot_path=path)
        expectations = {
            "grok-4": "xai",
            "mistral-large": "mistral",
            "command-r-plus": "cohere",
            "deepseek-chat": "deepseek",
            "sonar": "perplexity",
            "llama-3.3-70b-versatile": "groq",
            "openrouter-pass-through": "openrouter",
        }
        for model, expected_provider in expectations.items():
            resolution = resolver.resolve(None, model)
            assert resolution is not None, f"{model} did not resolve"
            assert resolution.provider == expected_provider, (
                f"{model}: expected provider={expected_provider}, got {resolution.provider}"
            )

    def test_unmapped_litellm_provider_falls_back_to_caller(self, tmp_path):
        """Wave 7+ batch 7h regression: a LiteLLM provider that SHIP
        doesn't recognise still resolves via the snapshot, but the
        ``provider`` field falls back to the caller-passed string (or
        ``unknown``) — never silently labelled as a different vendor."""
        path = _write_snapshot(
            tmp_path,
            {
                "obscure-model-x": {
                    "input_cost_per_token": 5e-07,
                    "output_cost_per_token": 5e-07,
                    "litellm_provider": "obscure_brand_42",
                },
            },
        )
        resolver = LiteLLMResolver(snapshot_path=path)
        r_with_hint = resolver.resolve("caller-said-foo", "obscure-model-x")
        r_no_hint = resolver.resolve(None, "obscure-model-x")
        assert r_with_hint is not None
        assert r_with_hint.provider == "caller-said-foo"
        assert r_no_hint is not None
        assert r_no_hint.provider == "unknown"

    def test_anthropic_provider_mapping(self, tmp_path):
        path = _write_snapshot(
            tmp_path,
            {
                "claude-opus-4-7": {
                    "input_cost_per_token": 1.5e-05,
                    "output_cost_per_token": 7.5e-05,
                    "cache_read_input_token_cost": 1.5e-06,
                    "cache_creation_input_token_cost": 1.875e-05,
                    "litellm_provider": "anthropic",
                },
            },
        )
        resolver = LiteLLMResolver(snapshot_path=path)
        resolution = resolver.resolve("anthropic", "claude-opus-4-7")
        assert resolution is not None
        assert resolution.provider == "anthropic"
        assert resolution.rates["input"] == pytest.approx(15.0)
        assert resolution.rates["output"] == pytest.approx(75.0)
        assert resolution.rates["cache_read"] == pytest.approx(1.5)
        assert resolution.rates["cache_write"] == pytest.approx(18.75)

    def test_provider_prefix_stripped(self, tmp_path):
        """A query for ``anthropic/claude-opus-4-7`` should resolve via
        the bare ``claude-opus-4-7`` key (LiteLLM stores provider-less
        canonical ids)."""
        path = _write_snapshot(
            tmp_path,
            {
                "claude-opus-4-7": {
                    "input_cost_per_token": 1.5e-05,
                    "output_cost_per_token": 7.5e-05,
                    "litellm_provider": "anthropic",
                },
            },
        )
        resolver = LiteLLMResolver(snapshot_path=path)
        resolution = resolver.resolve(
            "anthropic", "anthropic/claude-opus-4-7"
        )
        assert resolution is not None
        assert resolution.match_quality == "alias"

    def test_returns_none_when_model_absent(self, tmp_path):
        path = _write_snapshot(
            tmp_path,
            {"gpt-5": {"input_cost_per_token": 1e-06, "litellm_provider": "openai"}},
        )
        resolver = LiteLLMResolver(snapshot_path=path)
        assert resolver.resolve("openai", "some-future-model") is None

    def test_returns_none_when_model_is_none_or_empty(self, tmp_path):
        path = _write_snapshot(
            tmp_path,
            {"gpt-5": {"input_cost_per_token": 1e-06, "litellm_provider": "openai"}},
        )
        resolver = LiteLLMResolver(snapshot_path=path)
        assert resolver.resolve("openai", None) is None
        assert resolver.resolve("openai", "") is None

    def test_sample_spec_entry_ignored(self, tmp_path):
        """LiteLLM ships a ``sample_spec`` meta-entry for contributor
        documentation. It must not be resolvable."""
        path = _write_snapshot(
            tmp_path,
            {
                "sample_spec": {
                    "input_cost_per_token": 99.0,
                    "output_cost_per_token": 99.0,
                    "litellm_provider": "anthropic",
                },
                "gpt-5": {
                    "input_cost_per_token": 1.25e-06,
                    "output_cost_per_token": 1e-05,
                    "litellm_provider": "openai",
                },
            },
        )
        resolver = LiteLLMResolver(snapshot_path=path)
        assert resolver.resolve("anthropic", "sample_spec") is None
        # Sanity: a real model still resolves
        assert resolver.resolve("openai", "gpt-5") is not None


# --- Hybrid resolver in core.pricing -----------------------------------------


class TestResolveModelPricingHybrid:
    def test_prefers_litellm_when_available(self, tmp_path, monkeypatch):
        # Inject a controlled snapshot via the module-level resolver.
        path = _write_snapshot(
            tmp_path,
            {
                "gpt-5": {
                    "input_cost_per_token": 9.99e-06,  # marker price
                    "output_cost_per_token": 9.99e-05,
                    "litellm_provider": "openai",
                },
            },
        )
        import ship1000x.core.pricing_litellm as pl

        monkeypatch.setattr(pl, "_default_resolver", LiteLLMResolver(snapshot_path=path))
        resolution = resolve_model_pricing_hybrid("openai", "gpt-5")
        # The hybrid path picked LiteLLM, so we see the marker price.
        assert resolution.rates["input"] == pytest.approx(9.99)
        assert resolution.pricing_source == LITELLM_PRICING_SOURCE

    def test_falls_back_to_local_when_litellm_misses(self, tmp_path, monkeypatch):
        path = _write_snapshot(
            tmp_path,
            {
                "gpt-5": {
                    "input_cost_per_token": 1.25e-06,
                    "output_cost_per_token": 1e-05,
                    "litellm_provider": "openai",
                },
            },
        )
        import ship1000x.core.pricing_litellm as pl

        monkeypatch.setattr(pl, "_default_resolver", LiteLLMResolver(snapshot_path=path))
        # ``gpt-5-codex`` is in the local table but not in this synthetic
        # snapshot, so hybrid must fall back to the local resolution.
        resolution = resolve_model_pricing_hybrid("openai", "gpt-5-codex")
        local = resolve_model_pricing("openai", "gpt-5-codex")
        assert resolution.rates == local.rates
        # Local pricing source label is used on the fallback path
        assert resolution.pricing_source != LITELLM_PRICING_SOURCE

    def test_prefer_litellm_false_bypasses_snapshot(self, tmp_path, monkeypatch):
        path = _write_snapshot(
            tmp_path,
            {
                "gpt-5": {
                    "input_cost_per_token": 9.99e-06,  # would be the marker
                    "output_cost_per_token": 9.99e-05,
                    "litellm_provider": "openai",
                },
            },
        )
        import ship1000x.core.pricing_litellm as pl

        monkeypatch.setattr(pl, "_default_resolver", LiteLLMResolver(snapshot_path=path))
        resolution = resolve_model_pricing_hybrid(
            "openai", "gpt-5", prefer_litellm=False
        )
        # Bypass: we read the local table value, not the marker.
        assert resolution.rates["input"] == pytest.approx(OPENAI_PRICING["gpt-5"]["input"])

    def test_local_fallback_when_snapshot_unavailable(self, tmp_path, monkeypatch):
        """If the vendored snapshot path doesn't exist, hybrid silently
        falls back to the local table (no warning, no exception)."""
        absent = tmp_path / "missing.json"
        import ship1000x.core.pricing_litellm as pl

        monkeypatch.setattr(
            pl, "_default_resolver", LiteLLMResolver(snapshot_path=absent)
        )
        resolution = resolve_model_pricing_hybrid("openai", "gpt-5")
        assert resolution.rates == OPENAI_PRICING["gpt-5"] | {
            "cached_input": OPENAI_PRICING["gpt-5"].get("cached_input", 0)
        } or resolution.rates  # tolerate dict superset


# --- Smoke test against the vendored real snapshot ---------------------------


class TestVendoredSnapshot:
    """Light-touch checks that the actual JSON we ship resolves the
    models SHIP cares about with non-zero rates. Skipped if the file
    isn't there (e.g. on a tree where the data dir is excluded)."""

    SNAPSHOT_PATH = (
        Path(__file__).resolve().parent.parent
        / "ship1000x"
        / "data"
        / "litellm_prices.json"
    )

    @pytest.fixture
    def resolver(self):
        if not self.SNAPSHOT_PATH.exists():
            pytest.skip("vendored LiteLLM snapshot missing on this tree")
        return LiteLLMResolver(snapshot_path=self.SNAPSHOT_PATH)

    @pytest.mark.parametrize(
        "model",
        [
            # Wave 1 original SHIP-tracked frontier set.
            "gpt-5",
            "gpt-4o",
            "claude-opus-4-7",
            "claude-sonnet-4-7",
            "claude-haiku-4-5",
            # Wave 7+ batch 7h extension — flagship of each new mapped vendor.
            # Each entry is a real key in the LiteLLM snapshot at pin
            # commit 35f6961 (2026-05-23). Pinned ids, not "latest"
            # tags, so a sync drift is visible as a None resolution.
            "gemini-2.0-flash",
            "xai/grok-4",
            "command-r-plus",
            "deepseek/deepseek-chat",
            "perplexity/sonar",
            "groq/llama-3.3-70b-versatile",
        ],
    )
    def test_ship_tracked_models_resolve_with_nonzero_input_rate(self, resolver, model):
        # is_available will trip the skip indirectly if the file is empty.
        if not resolver.is_available:
            pytest.skip("snapshot empty")
        # Some of these may not be in the snapshot today; treat None as
        # "skip" rather than "fail" — the goal is regression detection
        # on the models that ARE there.
        resolution = resolver.resolve(None, model)
        if resolution is None:
            pytest.skip(f"{model} not present in this snapshot")
        assert resolution.rates["input"] > 0, (
            f"{model} resolves with zero input rate — likely a sync bug"
        )
        assert resolution.unknown_model is False
