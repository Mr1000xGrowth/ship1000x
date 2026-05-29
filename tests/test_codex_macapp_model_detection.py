"""Tests for Codex MacApp model detection filtering.

Codex MacApp 2026+ logs only carry `model=<id>` on auxiliary sub-calls
(`ephemeral_generation_token_usage feature=thread_title`), never on the
coding turns themselves. The thread-title sub-model is typically
gpt-5.4-mini, which is ~5x cheaper than the actual coding model
(gpt-5.5 / gpt-5-codex). Capturing it would misreport the cost basis.

These tests synthesize log lines and verify `_parse_log_file` no longer
returns the auxiliary model.
"""

from __future__ import annotations

from pathlib import Path

from ship1000x.collectors.codex_macapp import _parse_log_file


def _write_synthetic_log(tmp_path: Path, lines: list[str]) -> Path:
    """Write lines into a file with the codex-desktop-* naming convention."""
    log = tmp_path / "codex-desktop-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee-12345-t0-i1-000000-0.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


def test_parse_log_file_skips_thread_title_model(tmp_path):
    """`ephemeral_generation_token_usage feature=thread_title` must not
    contribute its `model=<id>` to the detection — that's the sub-model
    used for title generation, not the coding model."""
    log = _write_synthetic_log(
        tmp_path,
        [
            "2026-05-11T17:15:55.857Z info [ephemeral-generation] ephemeral_generation_token_usage cachedInputTokens=7680 event=ephemeral_generation_token_usage feature=thread_title inputTokens=11669 model=gpt-5.4-mini outputTokens=112 reasoningEffort=low reasoningOutputTokens=92 serviceTier=null status=success totalTokens=11781",
        ],
    )

    parsed = _parse_log_file(log)
    assert parsed["model_events"] == []


def test_parse_log_file_keeps_real_coding_model(tmp_path):
    """A log line that mentions `model=` outside of the ephemeral sub-call
    must still be captured (pre-2026 layout + any future surface where the
    coding model shows up)."""
    log = _write_synthetic_log(
        tmp_path,
        [
            '2026-05-22T10:00:00.000Z info [model-client] streaming_started conversationId=abc model=gpt-5.5 turn=1',
        ],
    )

    parsed = _parse_log_file(log)
    assert len(parsed["model_events"]) == 1
    _ts, name = parsed["model_events"][0]
    assert name == "gpt-5.5"


def test_parse_log_file_mixed_lines_only_keeps_real_model(tmp_path):
    """Realistic mix: thread-title sub-call AND a (hypothetical) coding
    model line — only the coding model survives."""
    log = _write_synthetic_log(
        tmp_path,
        [
            "2026-05-22T09:59:00.000Z info [ephemeral-generation] ephemeral_generation_token_usage feature=thread_title model=gpt-5.4-mini totalTokens=100",
            '2026-05-22T10:00:00.000Z info [model-client] streaming_started conversationId=abc model="gpt-5-codex" turn=1',
        ],
    )

    parsed = _parse_log_file(log)
    names = [name for _ts, name in parsed["model_events"]]
    assert "gpt-5.4-mini" not in names
    assert "gpt-5-codex" in names


def test_parse_log_file_returns_empty_when_only_thread_title_models(tmp_path):
    """Regression: when every `model=<id>` mention is from a thread-title
    sub-call (= current 2026+ MacApp layout), the parser must keep
    `model_events` empty so downstream collectors emit `model_raw=unknown`
    instead of misreporting the sub-model as the coding model."""
    log = _write_synthetic_log(
        tmp_path,
        [
            "2026-05-22T09:00:00.000Z info [ephemeral-generation] ephemeral_generation_token_usage feature=thread_title model=gpt-5.4-mini totalTokens=100",
            "2026-05-22T10:00:00.000Z info [ephemeral-generation] ephemeral_generation_token_usage feature=thread_title model=gpt-5.4-mini totalTokens=200",
            "2026-05-22T11:00:00.000Z info [AppServerConnection] response_routed method=turn/start conversationId=abc",
        ],
    )

    parsed = _parse_log_file(log)
    assert parsed["model_events"] == []
    # turn/start was still counted as activity
    assert len(parsed["turn_timestamps"]) == 1
