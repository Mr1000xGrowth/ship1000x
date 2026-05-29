"""Tests for usage normalization and safe metadata preservation."""

import json

from ship1000x.core.pricing import PRICING_SOURCE, PRICING_VERSION, resolve_model_pricing
from ship1000x.core.privacy import sanitize_event
from ship1000x.core.usage import (
    TokenBreakdown,
    build_unknown_usage_metadata,
    build_usage_metadata,
    canonicalize_model,
)


def test_canonicalize_model_uses_longest_known_alias():
    assert canonicalize_model("gpt-5-codex-preview-2026-05") == "gpt-5-codex"
    assert canonicalize_model("GPT-5 mini") == "gpt-5-mini"
    assert canonicalize_model("") == "unknown"


def test_build_usage_metadata_marks_native_tokens_factual():
    usage = build_usage_metadata(
        provider="openai",
        client="codex-cli",
        model_raw="gpt-5-codex-preview",
        tokens=TokenBreakdown(
            input_tokens=1000,
            output_tokens=200,
            cached_input_tokens=300,
            reasoning_tokens=50,
        ),
        cost_estimated=0.003,
        cost_quality="factual",
        token_source="codex_total_token_usage",
    )

    assert usage["model_canonical"] == "gpt-5-codex"
    assert usage["tokens"]["input_tokens"] == 1000
    assert usage["tokens"]["cached_input_tokens"] == 300
    assert usage["tokens"]["reasoning_tokens"] == 50
    assert usage["cost"]["pricing_source"] == PRICING_SOURCE
    assert usage["cost"]["pricing_version"] == PRICING_VERSION
    assert usage["cost"]["pricing_match_quality"] == "alias"
    assert usage["cost"]["unknown_model"] is False
    assert usage["quality"]["tokens"] == "factual"
    assert usage["quality"]["cost"] == "factual"
    assert usage["quality"]["active_time"] == "defensible"
    assert usage["provenance"]["pricing_model_canonical"] == "gpt-5-codex"


def test_build_unknown_usage_metadata_marks_missing_tokens_unknown():
    usage = build_unknown_usage_metadata(
        provider="openai",
        client="codex-macapp",
        cost_estimated=1.25,
    )

    assert usage["tokens"]["input_tokens"] == 0
    assert usage["quality"]["tokens"] == "unknown"
    assert usage["quality"]["cost"] == "indicative"
    assert usage["cost"]["basis"] == "hourly_estimate"
    assert usage["provenance"]["token_source"] == "not_exposed"


def test_resolve_model_pricing_marks_unknown_fallback():
    resolved = resolve_model_pricing("openai", "future-model-x")

    assert resolved.provider == "openai"
    assert resolved.model_canonical == "future-model-x"
    assert resolved.match_quality == "fallback"
    assert resolved.unknown_model is True
    assert resolved.pricing_source == PRICING_SOURCE
    assert resolved.pricing_version == PRICING_VERSION


def test_build_usage_metadata_downgrades_factual_cost_when_pricing_falls_back():
    usage = build_usage_metadata(
        provider="openai",
        client="codex-cli",
        model_raw="future-model-x",
        tokens=TokenBreakdown(input_tokens=1000, output_tokens=200),
        cost_estimated=0.006,
        cost_quality="factual",
        token_source="codex_total_token_usage",
    )

    assert usage["quality"]["tokens"] == "factual"
    assert usage["quality"]["cost"] == "indicative"
    assert usage["cost"]["quality"] == "indicative"
    assert usage["cost"]["pricing_match_quality"] == "fallback"
    assert usage["cost"]["unknown_model"] is True


def test_build_usage_metadata_preserves_safe_provider_policy_snapshot_only():
    usage = build_usage_metadata(
        provider="openai",
        client="codex-cli",
        model_raw="gpt-5-codex",
        tokens=TokenBreakdown(input_tokens=1000, output_tokens=200),
        cost_estimated=0.006,
        cost_quality="factual",
        provider_policy_snapshot={
            "provider": "openai",
            "checked_at": "2026-05-26",
            "status": "local-evidence-only",
            "risk_label": "unknown",
            "policy_url": "https://example.invalid/policy",
            "prompt": "must never survive",
            "raw_policy_text": "must never survive either",
        },
    )

    assert usage["provider_policy_snapshot"] == {
        "provider": "openai",
        "checked_at": "2026-05-26",
        "status": "local-evidence-only",
        "risk_label": "unknown",
        "policy_url": "https://example.invalid/policy",
    }


def test_build_usage_metadata_omits_invalid_provider_policy_snapshot():
    usage = build_usage_metadata(
        provider="openai",
        client="codex-cli",
        model_raw="gpt-5-codex",
        tokens=TokenBreakdown(input_tokens=1000, output_tokens=200),
        cost_estimated=0.006,
        cost_quality="factual",
        provider_policy_snapshot="not-a-dict",  # type: ignore[arg-type]
    )

    assert "provider_policy_snapshot" not in usage


def test_sanitize_event_preserves_usage_and_drops_nested_content_like_keys():
    event = {
        "id": "e1",
        "source": "codex",
        "event_type": "session",
        "started_at": "2026-05-22T10:00:00Z",
        "raw_meta": json.dumps(
            {
                "usage": {
                    "client": "codex-cli",
                    "tokens": {"input_tokens": 1, "output_tokens": 2},
                    "provider_policy_snapshot": {
                        "provider": "openai",
                        "checked_at": "2026-05-26",
                        "status": "local-evidence-only",
                        "prompt": "must never survive",
                    },
                    "prompt": "must never survive",
                    "path_hint": "/Users/example/project",
                }
            }
        ),
    }

    safe = sanitize_event(event)
    meta = json.loads(safe["raw_meta"])
    assert meta["usage"]["client"] == "codex-cli"
    assert meta["usage"]["tokens"] == {"input_tokens": 1, "output_tokens": 2}
    assert meta["usage"]["provider_policy_snapshot"] == {
        "provider": "openai",
        "checked_at": "2026-05-26",
        "status": "local-evidence-only",
    }
    assert "prompt" not in meta["usage"]
    assert "prompt" not in meta["usage"]["provider_policy_snapshot"]
    assert meta["usage"]["path_hint"] == "~/project"
