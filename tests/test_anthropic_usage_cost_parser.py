"""Tests for the anthropic_usage cost_report amount parser.

The Anthropic Admin API silently changed the cost_report schema in 2026:
results now carry `amount` as a numeric string at the result root
(`"amount": "20.394575"`, `"currency": "USD"`) instead of the legacy
`{"amount": {"value": ..., "currency": ...}}` object. The parser must
support both shapes, plus legacy fallbacks, so a future drift does not
silently zero out billing totals again.
"""

from __future__ import annotations

import pytest

from ship1000x.collectors.anthropic_usage import _extract_amount_usd


def test_parses_numeric_string_amount_current_2026_shape():
    """Current API shape: `amount` is a numeric string at result root."""
    assert _extract_amount_usd(
        {"amount": "20.394575", "currency": "USD"}
    ) == pytest.approx(20.394575)


def test_parses_plain_int_amount():
    assert _extract_amount_usd({"amount": 15, "currency": "USD"}) == 15.0


def test_parses_plain_float_amount():
    assert _extract_amount_usd({"amount": 15.5}) == 15.5


def test_parses_legacy_amount_object_with_numeric_value():
    """Legacy shape: `amount: {value: ..., currency: ...}`."""
    assert _extract_amount_usd(
        {"amount": {"value": 7.0, "currency": "USD"}}
    ) == pytest.approx(7.0)


def test_parses_legacy_amount_object_with_string_value():
    assert _extract_amount_usd(
        {"amount": {"value": "7.5", "currency": "USD"}}
    ) == pytest.approx(7.5)


def test_falls_back_to_cost_usd_when_amount_is_null():
    assert _extract_amount_usd({"amount": None, "cost_usd": 3.14}) == pytest.approx(3.14)


def test_falls_back_to_amount_usd_when_amount_is_null():
    assert _extract_amount_usd({"amount": None, "amount_usd": 2.71}) == pytest.approx(2.71)


def test_returns_zero_when_currency_is_not_usd():
    """SHIP cost contract is USD-only. Non-USD silently zeroed is safer
    than misreporting EUR/GBP as dollars.
    """
    assert _extract_amount_usd({"amount": "20.0", "currency": "EUR"}) == 0.0


def test_returns_zero_for_garbage_string_amount():
    assert _extract_amount_usd({"amount": "not-a-number"}) == 0.0


def test_returns_zero_for_empty_result():
    assert _extract_amount_usd({}) == 0.0


def test_currency_in_nested_amount_overrides_top_level():
    """When `amount` is an object carrying its own `currency`, that one
    wins over the top-level `currency` field.
    """
    # Currency from the nested object says EUR even though top-level says USD.
    assert _extract_amount_usd(
        {"amount": {"value": 10.0, "currency": "EUR"}, "currency": "USD"}
    ) == 0.0
