"""Synthetic Codex fixture tests.

These tests do not read ~/.codex or any real local logs. They exercise the
metadata-only parser contract with temporary JSONL fixtures.
"""

import json
from pathlib import Path

from ship1000x.collectors.codex import parse_session_file


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")


def test_codex_rollout_parses_native_token_breakdown_and_usage_metadata(tmp_path):
    rollout = tmp_path / "rollout-2026-05-22-session-a.jsonl"
    _write_jsonl(
        rollout,
        [
            {
                "timestamp": "2026-05-22T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "cwd": str(tmp_path / "project"),
                    "base_instructions": {
                        "text": "You are Codex, a coding agent based on GPT-5-Codex.",
                    },
                },
            },
            {
                "timestamp": "2026-05-22T10:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "implement a safe parser test now"}
                    ],
                },
            },
            {
                "timestamp": "2026-05-22T10:02:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "arguments": json.dumps({"path": str(tmp_path / "project" / "app.py")}),
                },
            },
            {
                "timestamp": "2026-05-22T10:03:00Z",
                "type": "event_msg",
                "payload": {},
                "total_token_usage": {
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cached_input_tokens": 300,
                    "reasoning_output_tokens": 50,
                },
            },
        ],
    )

    parsed = parse_session_file(rollout)
    usage = parsed["usage"]

    assert parsed["model"] == "gpt-5-codex"
    assert parsed["tokens_input"] == 1000
    assert parsed["tokens_output"] == 200
    assert parsed["cached_input_tokens"] == 300
    assert parsed["reasoning_output_tokens"] == 50
    assert usage["client"] == "codex-cli"
    assert usage["model_canonical"] == "gpt-5-codex"
    assert usage["quality"]["tokens"] == "factual"
    assert usage["quality"]["cost"] == "factual"
    assert usage["provenance"]["token_source"] == "codex_total_token_usage"


def test_codex_rollout_marks_missing_tokens_unknown(tmp_path):
    rollout = tmp_path / "rollout-2026-05-22-session-b.jsonl"
    _write_jsonl(
        rollout,
        [
            {
                "timestamp": "2026-05-22T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "base_instructions": {
                        "text": "You are Codex, a coding agent based on GPT-5.",
                    },
                },
            }
        ],
    )

    parsed = parse_session_file(rollout)
    assert parsed["usage"]["quality"]["tokens"] == "unknown"
    assert parsed["usage"]["quality"]["cost"] == "unknown"
    assert parsed["usage"]["provenance"]["token_source"] == "not_exposed"


def test_codex_rollout_parses_2026_event_msg_token_count_format(tmp_path):
    """Codex 2026+ rollouts emit `type=event_msg` records wrapping a
    `payload.type=token_count` block with `payload.info.total_token_usage`.
    Before this format was supported, SHIP saw 0 tokens on every recent
    Codex CLI session.
    """
    rollout = tmp_path / "rollout-2026-05-22-session-c.jsonl"
    _write_jsonl(
        rollout,
        [
            {
                "timestamp": "2026-05-22T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "cwd": str(tmp_path / "project"),
                    "base_instructions": {
                        "text": "You are Codex, a coding agent based on GPT-5.",
                    },
                },
            },
            {
                "timestamp": "2026-05-22T10:00:01Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "abc",
                    "model": "gpt-5.5",
                    "effort": "medium",
                },
            },
            {
                "timestamp": "2026-05-22T10:00:30Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 12000,
                            "cached_input_tokens": 9000,
                            "output_tokens": 800,
                            "reasoning_output_tokens": 150,
                            "total_tokens": 12950,
                        },
                        "last_token_usage": {
                            "input_tokens": 2000,
                            "cached_input_tokens": 1500,
                            "output_tokens": 200,
                            "reasoning_output_tokens": 50,
                            "total_tokens": 2250,
                        },
                        "model_context_window": 258400,
                    },
                    "rate_limits": {},
                },
            },
            # Subsequent token_count snapshot: cumulative, must replace prior.
            {
                "timestamp": "2026-05-22T10:01:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 22000,
                            "cached_input_tokens": 18000,
                            "output_tokens": 1500,
                            "reasoning_output_tokens": 300,
                            "total_tokens": 23800,
                        },
                    },
                },
            },
        ],
    )

    parsed = parse_session_file(rollout)

    # turn_context wins over session_meta base_instructions regex.
    assert parsed["model"] == "gpt-5.5"
    # Cumulative max from the second event_msg snapshot.
    assert parsed["tokens_input"] == 22000
    assert parsed["tokens_output"] == 1500
    assert parsed["cached_input_tokens"] == 18000
    assert parsed["reasoning_output_tokens"] == 300
    assert parsed["cost_estimated"] > 0
    usage = parsed["usage"]
    assert usage["client"] == "codex-cli"
    assert usage["model_raw"] == "gpt-5.5"
    # gpt-5.5 is now explicitly in the OpenAI pricing table; resolution is exact.
    assert usage["model_canonical"] == "gpt-5.5"
    assert usage["quality"]["tokens"] == "factual"
    assert usage["quality"]["cost"] == "factual"


def test_codex_rollout_turn_context_overrides_session_meta_model(tmp_path):
    """`turn_context.payload.model` must take precedence over the
    base_instructions regex, even when the regex would have matched.
    """
    rollout = tmp_path / "rollout-2026-05-22-session-d.jsonl"
    _write_jsonl(
        rollout,
        [
            {
                "timestamp": "2026-05-22T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "base_instructions": {
                        "text": "You are Codex, a coding agent based on GPT-5.",
                    },
                },
            },
            {
                "timestamp": "2026-05-22T10:00:01Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5-codex"},
            },
        ],
    )

    parsed = parse_session_file(rollout)
    assert parsed["model"] == "gpt-5-codex"


def test_codex_rollout_falls_back_to_gpt5_when_neither_regex_nor_turn_context_present(tmp_path):
    """No model field anywhere -> the conservative default ('gpt-5') stays.
    This must not regress when the new 2026 format paths are added.
    """
    rollout = tmp_path / "rollout-2026-05-22-session-e.jsonl"
    _write_jsonl(
        rollout,
        [
            {
                "timestamp": "2026-05-22T10:00:00Z",
                "type": "session_meta",
                "payload": {"cwd": str(tmp_path / "project")},
            },
        ],
    )

    parsed = parse_session_file(rollout)
    assert parsed["model"] == "gpt-5"
