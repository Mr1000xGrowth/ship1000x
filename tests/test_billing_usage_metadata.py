"""Tests for official billing/API usage metadata."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from ship1000x.collectors import anthropic_usage, openai_usage
from ship1000x.core.storage import Storage


def _storage(tmp: str) -> Storage:
    storage = Storage(Path(tmp) / "tracker.sqlite")
    storage.init_schema()
    return storage


def test_anthropic_usage_writes_official_usage_metadata(monkeypatch):
    with TemporaryDirectory() as tmp:
        storage = _storage(tmp)
        monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-ant-admin-test")
        monkeypatch.setattr(
            anthropic_usage,
            "_fetch_usage_report",
            lambda *_args, **_kwargs: [
                {
                    "starting_at": "2026-05-22T00:00:00Z",
                    "results": [
                        {
                            "model": "claude-sonnet-4-6",
                            "workspace_id": "workspace-redacted",
                            "uncached_input_tokens": 100,
                            "cache_read_input_tokens": 20,
                            "cache_creation": {"ephemeral_1h_input_tokens": 5},
                            "output_tokens": 50,
                        }
                    ],
                }
            ],
        )
        monkeypatch.setattr(
            anthropic_usage,
            "_fetch_cost_report",
            lambda *_args, **_kwargs: [
                {
                    "starting_at": "2026-05-22T00:00:00Z",
                    "results": [{"amount": {"value": 1.25, "currency": "USD"}}],
                }
            ],
        )

        stats = anthropic_usage.collect(storage, None, {})

        assert stats["events_ingested"] == 1
        row = storage.query("SELECT raw_meta FROM events WHERE source = 'anthropic_usage'")[0]
        usage = json.loads(row["raw_meta"])["usage"]
        assert usage["provider"] == "anthropic"
        assert usage["client"] == "anthropic-usage-api"
        assert usage["model_canonical"] == "claude-sonnet-4-6"
        assert usage["tokens"]["input_tokens"] == 100
        assert usage["tokens"]["cached_input_tokens"] == 20
        assert usage["tokens"]["cache_write_tokens"] == 5
        assert usage["tokens"]["output_tokens"] == 50
        assert usage["quality"]["tokens"] == "factual"
        assert usage["quality"]["cost"] == "factual"
        assert usage["quality"]["active_time"] == "unknown"
        assert usage["provenance"]["token_source"] == "anthropic_admin_usage_report"


def test_openai_usage_writes_official_usage_metadata(monkeypatch):
    with TemporaryDirectory() as tmp:
        storage = _storage(tmp)
        monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-test")
        monkeypatch.setattr(
            openai_usage,
            "_fetch_completions_usage",
            lambda *_args, **_kwargs: [
                {
                    "start_time": 1779408000,
                    "results": [
                        {
                            "model": "gpt-5-codex",
                            "input_tokens": 100,
                            "input_cached_tokens": 25,
                            "output_tokens": 40,
                            "num_model_requests": 3,
                        }
                    ],
                }
            ],
        )
        monkeypatch.setattr(
            openai_usage,
            "_fetch_costs",
            lambda *_args, **_kwargs: [
                {
                    "start_time": 1779408000,
                    "results": [{"amount": {"value": 2.5}}],
                }
            ],
        )

        stats = openai_usage.collect(storage, None, {})

        assert stats["events_ingested"] == 1
        row = storage.query("SELECT raw_meta FROM events WHERE source = 'openai_usage'")[0]
        usage = json.loads(row["raw_meta"])["usage"]
        assert usage["provider"] == "openai"
        assert usage["client"] == "openai-usage-api"
        assert usage["model_canonical"] == "gpt-5-codex"
        assert usage["tokens"]["input_tokens"] == 75
        assert usage["tokens"]["cached_input_tokens"] == 25
        assert usage["tokens"]["output_tokens"] == 40
        assert usage["quality"]["tokens"] == "factual"
        assert usage["quality"]["cost"] == "factual"
        assert usage["quality"]["active_time"] == "unknown"
        assert usage["provenance"]["token_source"] == "openai_admin_usage_completions"


def test_openai_usage_treats_official_zero_cost_as_factual(monkeypatch):
    with TemporaryDirectory() as tmp:
        storage = _storage(tmp)
        monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-test")
        monkeypatch.setattr(
            openai_usage,
            "_fetch_completions_usage",
            lambda *_args, **_kwargs: [
                {
                    "start_time": 1779408000,
                    "results": [
                        {
                            "model": "gpt-5-mini",
                            "input_tokens": 10,
                            "input_cached_tokens": 0,
                            "output_tokens": 5,
                            "num_model_requests": 1,
                        }
                    ],
                }
            ],
        )
        monkeypatch.setattr(
            openai_usage,
            "_fetch_costs",
            lambda *_args, **_kwargs: [
                {
                    "start_time": 1779408000,
                    "results": [{"amount": {"value": 0.0}}],
                }
            ],
        )

        stats = openai_usage.collect(storage, None, {})

        assert stats["events_ingested"] == 1
        row = storage.query("SELECT raw_meta FROM events WHERE source = 'openai_usage'")[0]
        meta = json.loads(row["raw_meta"])
        usage = meta["usage"]
        # SHIP now computes cost from tokens * pricing.py instead of the
        # OpenAI Admin cost endpoint (whose unit/inclusion rules diverged
        # ~100x from per-token pricing on Anthropic and are not stable
        # cross-provider). With 10 input + 5 output tokens of gpt-5-mini
        # priced at $0.25/M input and $2.0/M output, expected cost is
        # 10*0.25/1e6 + 5*2.0/1e6 = 1.25e-05 USD. cost_quality stays
        # "factual" because tokens come from the API and pricing is
        # deterministic from the published rate card.
        assert usage["cost"]["estimated_usd"] == pytest.approx(1.25e-05, rel=1e-3)
        assert usage["quality"]["cost"] == "factual"
        # api_reported_cost preserves the API's value for audit purposes
        # without using it as the billed cost.
        assert "api_reported_cost" in meta
        assert meta["api_reported_cost"] == 0.0
