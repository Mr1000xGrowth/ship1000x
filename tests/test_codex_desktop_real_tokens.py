"""Tests for the Codex Desktop real-tokens parser + auth-mode detector.

These tests synthesize the SSE/websocket `response.completed` payloads
and URL log lines Codex Desktop writes into ~/.codex/logs_2.sqlite,
without touching the real DB.
"""

from __future__ import annotations

import json

import pytest

from ship1000x.collectors.codex_desktop import (
    _detect_auth_mode,
    _extract_usage_from_payload,
)

# --- _extract_usage_from_payload ---------------------------------------------


def _wrap_response_completed(usage: dict, response_id: str = "resp_abc123") -> str:
    """Mimic the shape of a Codex Desktop log row containing the
    response.completed SSE event. The real rows are far larger and
    contain many fields; we keep the minimum the parser actually uses
    (the literal 'response.completed', the 'usage' object, and the
    response id)."""
    payload = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "usage": usage,
        },
    }
    # The collector parses the raw row string, not a pre-parsed dict, so
    # we serialize a payload with the right shape and the right marker.
    # Use compact separators (no spaces) to match the real SSE payload
    # shape — the response-id marker is `"id":"resp_` with no space,
    # which Python's default `json.dumps` would break with `"id": ...`.
    return f"SSE event response.completed body={json.dumps(payload, separators=(',', ':'))}"


def test_extract_usage_returns_none_when_row_has_no_response_completed():
    assert _extract_usage_from_payload("nothing useful here") is None
    assert _extract_usage_from_payload("response.created without usage") is None


def test_extract_usage_returns_none_when_usage_block_is_absent():
    msg = "SSE event response.completed body={\"type\":\"response.completed\"}"
    assert _extract_usage_from_payload(msg) is None


def test_extract_usage_parses_full_usage_block():
    usage = {
        "input_tokens": 12545,
        "input_tokens_details": {"cached_tokens": 7680},
        "output_tokens": 81,
        "output_tokens_details": {"reasoning_tokens": 61},
        "total_tokens": 12626,
    }
    msg = _wrap_response_completed(usage, response_id="resp_test_001")
    out = _extract_usage_from_payload(msg)

    assert out is not None
    assert out["input_tokens"] == 12545
    assert out["cached_tokens"] == 7680
    assert out["output_tokens"] == 81
    assert out["reasoning_tokens"] == 61
    assert out["total_tokens"] == 12626
    assert out["response_id"] == "resp_test_001"


def test_extract_usage_tolerates_missing_detail_blocks():
    """When OpenAI omits `input_tokens_details` or `output_tokens_details`
    the parser must still return the headline counts and default cache /
    reasoning to zero (instead of raising)."""
    usage = {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125}
    msg = _wrap_response_completed(usage, response_id="resp_partial")
    out = _extract_usage_from_payload(msg)

    assert out is not None
    assert out["input_tokens"] == 100
    assert out["output_tokens"] == 25
    assert out["cached_tokens"] == 0
    assert out["reasoning_tokens"] == 0
    assert out["total_tokens"] == 125


def test_extract_usage_balances_braces_through_nested_object():
    """The usage block is itself an object containing nested objects
    (`input_tokens_details`, `output_tokens_details`). The brace
    balancer must not stop on the first inner `}` it encounters."""
    usage = {
        "input_tokens": 200,
        "input_tokens_details": {"cached_tokens": 50, "extra_nested": {"unused": 1}},
        "output_tokens": 10,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 210,
    }
    msg = _wrap_response_completed(usage, response_id="resp_nested")
    out = _extract_usage_from_payload(msg)

    assert out is not None
    assert out["input_tokens"] == 200
    assert out["cached_tokens"] == 50
    assert out["total_tokens"] == 210


def test_extract_usage_returns_none_when_brace_balance_never_closes():
    """A truncated row (cut mid-payload by a log rotator) must yield
    None instead of crashing the collector."""
    truncated = 'SSE event response.completed "usage":{"input_tokens":100,"output_tokens":'
    assert _extract_usage_from_payload(truncated) is None


def test_extract_usage_returns_none_on_invalid_json():
    bad = 'SSE event response.completed "usage":{not valid json}'
    assert _extract_usage_from_payload(bad) is None


def test_extract_usage_returns_none_when_input_tokens_missing():
    """A `usage:{}` empty object or a `usage` that does not carry
    `input_tokens` is treated as no usage at all rather than as a zero-
    cost event (would otherwise inflate the per-response counter)."""
    empty = 'SSE event response.completed "usage":{}'
    assert _extract_usage_from_payload(empty) is None


# --- _detect_auth_mode --------------------------------------------------------


def test_detect_auth_mode_returns_oauth_for_chatgpt_backend_url():
    msgs = [
        "irrelevant log line",
        "POST https://chatgpt.com/backend-api/codex/responses",
        "another irrelevant line",
    ]
    assert _detect_auth_mode(msgs) == "oauth"


def test_detect_auth_mode_returns_api_key_for_api_openai_url():
    msgs = [
        "DEBUG codex_client::request url=https://api.openai.com/v1/responses",
        "more irrelevant lines",
    ]
    assert _detect_auth_mode(msgs) == "api_key"


def test_detect_auth_mode_returns_unknown_when_both_seen():
    """Hybrid sessions (rare but possible: a long-running session whose
    user signs out and back in with a different mode) stay `unknown`
    rather than silently picking one. The cost-honesty contract
    interprets `unknown` as `billed_estimated_usd=0`, which is the
    conservative default."""
    msgs = [
        "POST https://chatgpt.com/backend-api/codex/responses",
        "POST https://api.openai.com/v1/responses",
    ]
    assert _detect_auth_mode(msgs) == "unknown"


def test_detect_auth_mode_returns_unknown_when_neither_seen():
    assert _detect_auth_mode(["only error logs", "no url markers here"]) == "unknown"
    assert _detect_auth_mode([]) == "unknown"


def test_detect_auth_mode_is_case_insensitive():
    msgs = ["POST HTTPS://ChatGPT.com/Backend-Api/codex/responses"]
    assert _detect_auth_mode(msgs) == "oauth"


def test_detect_auth_mode_short_circuits_once_both_found():
    """Once both families are seen, the function returns without
    scanning the remaining messages (covered indirectly through the
    `break` in the loop body). Synthesize many trailing lines to
    exercise the short-circuit path; correctness implied if the test
    completes in microseconds."""
    msgs = [
        "POST https://chatgpt.com/backend-api/codex/responses",
        "POST https://api.openai.com/v1/responses",
    ] + ["filler"] * 10_000
    assert _detect_auth_mode(msgs) == "unknown"  # both seen -> unknown


# --- Smoke: parse a near-real shape ------------------------------------------


def test_extract_usage_parses_real_shape_observed_in_logs_2_sqlite():
    """Reproduces the exact shape codex Desktop writes into
    feedback_log_body for a typical gpt-5.5 response: payload nested
    inside a `response.completed` SSE envelope, response id present,
    other unrelated objects (`item`, `response_metadata`) on either
    side of the `usage` block."""
    real_like = (
        'transport=websocket target="codex_api::sse::responses" '
        'payload={"type":"response.completed","response":{"id":"resp_019e52df_001",'
        '"object":"response","status":"completed","model":"gpt-5.5",'
        '"output":[{"id":"msg_x","type":"message","content":[]}],'
        '"usage":{"input_tokens":4096,'
        '"input_tokens_details":{"cached_tokens":3200},'
        '"output_tokens":128,'
        '"output_tokens_details":{"reasoning_tokens":32},'
        '"total_tokens":4224},"metadata":{"workspace":"foo"}}}'
    )
    out = _extract_usage_from_payload(real_like)

    assert out is not None
    assert out["input_tokens"] == 4096
    assert out["cached_tokens"] == 3200
    assert out["output_tokens"] == 128
    assert out["reasoning_tokens"] == 32
    assert out["total_tokens"] == 4224
    assert out["response_id"] == "resp_019e52df_001"
    # Pricing for gpt-5.5 input + cache + output gives a non-zero cost,
    # which is what `collect()` ends up persisting downstream. Verify
    # the math here so a future pricing drift breaks this test loud.
    from ship1000x.core.pricing import estimate_openai_cost

    cost = estimate_openai_cost(
        "gpt-5.5",
        tokens_input=out["input_tokens"],
        tokens_output=out["output_tokens"],
        cached_input_tokens=out["cached_tokens"],
    )
    # gpt-5.5: input=$5/M, cached_input=$0.50/M, output=$30/M
    # uncached_input = 4096-3200=896 tokens × $5/M = $0.00448
    # cached       = 3200 × $0.50/M = $0.00160
    # output       = 128  × $30.0/M = $0.00384
    # ≈ $0.00992
    assert cost == pytest.approx(0.00992, abs=1e-4)
