"""Fixture-only provider expansion parser tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ship1000x.collectors import (
    aider,
    continue_dev,
    copilot_agents,
    cursor_agent,
    gemini_cli,
    opencode,
    roo_kilo_code,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "provider_expansion"


def test_gemini_cli_fixture_maps_safe_usage_metadata():
    event = gemini_cli.parse_session_file(FIXTURE_DIR / "gemini_cli_session.json")

    assert event["source"] == "gemini_cli"
    assert event["event_type"] == "session"
    assert event["token_input"] == 1200
    assert event["token_output"] == 420
    assert event["cost_estimated"] == 0.0
    usage = event["raw_meta"]["usage"]
    assert usage["provider"] == "google"
    assert usage["client"] == "gemini-cli"
    assert usage["model_canonical"] == "gemini-2.5-pro"
    assert usage["tokens"]["reasoning_tokens"] == 80
    assert usage["quality"]["tokens"] == "factual"
    assert usage["quality"]["cost"] == "unknown"
    assert usage["cost"]["unknown_model"] is True
    assert usage["cost"]["pricing_match_quality"] == "fallback"

    rendered = json.dumps(event, sort_keys=True)
    assert "prompt" not in rendered.lower()
    assert "response" not in rendered.lower()
    assert "/Users/" not in rendered


def test_opencode_fixture_stays_unknown_when_tokens_absent():
    event = opencode.parse_session_file(FIXTURE_DIR / "opencode_session.json")

    assert event["source"] == "opencode"
    assert event["event_type"] == "session"
    assert event["token_input"] == 0
    assert event["token_output"] == 0
    usage = event["raw_meta"]["usage"]
    assert usage["client"] == "opencode"
    assert usage["provider"] == "openai-compatible"
    assert usage["quality"]["tokens"] == "unknown"
    assert usage["quality"]["cost"] == "unknown"
    assert usage["provenance"]["token_source"] == "not_exposed"

    rendered = json.dumps(event, sort_keys=True)
    assert "prompt" not in rendered.lower()
    assert "response" not in rendered.lower()
    assert "/Users/" not in rendered


def test_cursor_agent_fixture_is_separate_from_generic_cursor():
    event = cursor_agent.parse_session_file(FIXTURE_DIR / "cursor_agent_session.json")

    assert event["source"] == "cursor_agent"
    assert event["event_type"] == "session"
    assert event["tool_or_action"] == "agent_session"
    assert event["token_input"] == 0
    assert event["token_output"] == 0
    usage = event["raw_meta"]["usage"]
    assert usage["provider"] == "cursor"
    assert usage["client"] == "cursor-agent"
    assert usage["quality"]["tokens"] == "unknown"
    assert usage["quality"]["cost"] == "unknown"
    assert event["raw_meta"]["files_touched_count"] == 4

    rendered = json.dumps(event, sort_keys=True)
    assert "prompt" not in rendered.lower()
    assert "response" not in rendered.lower()
    assert "/Users/" not in rendered


def test_roo_kilo_fixture_does_not_inherit_cline_identity():
    event = roo_kilo_code.parse_task_file(FIXTURE_DIR / "roo_kilo_task.json")

    assert event["source"] == "roo_kilo_code"
    assert event["event_type"] == "task"
    assert event["tool_or_action"] == "variant_task"
    assert event["token_input"] == 900
    assert event["token_output"] == 240
    usage = event["raw_meta"]["usage"]
    assert usage["provider"] == "anthropic"
    assert usage["client"] == "roo-code"
    assert usage["model_canonical"] == "claude-sonnet-4-7"
    assert usage["tokens"]["cached_input_tokens"] == 120
    assert usage["quality"]["tokens"] == "factual"
    assert usage["quality"]["cost"] == "unknown"

    rendered = json.dumps(event, sort_keys=True)
    assert "prompt" not in rendered.lower()
    assert "response" not in rendered.lower()
    assert "/Users/" not in rendered


def test_copilot_agents_fixture_keeps_tokens_unknown_when_usage_absent():
    event = copilot_agents.parse_session_file(FIXTURE_DIR / "copilot_agents_session.json")

    assert event["source"] == "copilot_agents"
    assert event["event_type"] == "session"
    assert event["tool_or_action"] == "agent_session"
    assert event["token_input"] == 0
    assert event["token_output"] == 0
    assert event["raw_meta"]["agent_mode"] == "chat"
    assert event["raw_meta"]["edits_applied"] == 2
    usage = event["raw_meta"]["usage"]
    assert usage["provider"] == "github"
    assert usage["client"] == "copilot-agents"
    assert usage["quality"]["tokens"] == "unknown"
    assert usage["quality"]["cost"] == "unknown"
    assert usage["provenance"]["token_source"] == "not_exposed"

    rendered = json.dumps(event, sort_keys=True)
    assert "prompt" not in rendered.lower()
    assert "response" not in rendered.lower()
    assert "/Users/" not in rendered


def test_copilot_agents_fixture_normalizes_unknown_mode(tmp_path):
    payload = json.loads(
        (FIXTURE_DIR / "copilot_agents_session.json").read_text(encoding="utf-8")
    )
    payload["agent_mode"] = "future_unmapped_mode"
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps(payload), encoding="utf-8")

    event = copilot_agents.parse_session_file(custom)

    assert event["raw_meta"]["agent_mode"] == "unknown"


def test_continue_fixture_maps_safe_usage_metadata_when_tokens_present():
    event = continue_dev.parse_session_file(FIXTURE_DIR / "continue_session.json")

    assert event["source"] == "continue"
    assert event["event_type"] == "session"
    assert event["tool_or_action"] == "session"
    assert event["token_input"] == 1800
    assert event["token_output"] == 540
    assert event["raw_meta"]["edits_applied"] == 1
    usage = event["raw_meta"]["usage"]
    assert usage["provider"] == "anthropic"
    assert usage["client"] == "continue"
    assert usage["model_canonical"] == "claude-sonnet-4-7"
    assert usage["quality"]["tokens"] == "factual"
    assert usage["quality"]["cost"] == "unknown"

    rendered = json.dumps(event, sort_keys=True)
    assert "prompt" not in rendered.lower()
    assert "response" not in rendered.lower()
    assert "/Users/" not in rendered


def test_continue_fixture_stays_unknown_when_tokens_absent(tmp_path):
    payload = json.loads(
        (FIXTURE_DIR / "continue_session.json").read_text(encoding="utf-8")
    )
    payload["usage"] = {}
    custom = tmp_path / "continue_no_tokens.json"
    custom.write_text(json.dumps(payload), encoding="utf-8")

    event = continue_dev.parse_session_file(custom)

    assert event["token_input"] == 0
    assert event["token_output"] == 0
    usage = event["raw_meta"]["usage"]
    assert usage["quality"]["tokens"] == "unknown"
    assert usage["provenance"]["token_source"] == "not_exposed"


def test_aider_fixture_maps_safe_usage_metadata_when_tokens_present():
    event = aider.parse_session_file(FIXTURE_DIR / "aider_session.json")

    assert event["source"] == "aider"
    assert event["event_type"] == "session"
    assert event["tool_or_action"] == "session"
    assert event["token_input"] == 5200
    assert event["token_output"] == 1800
    assert event["raw_meta"]["commits_created"] == 3
    assert event["raw_meta"]["files_added_to_chat"] == 4
    usage = event["raw_meta"]["usage"]
    assert usage["provider"] == "openai"
    assert usage["client"] == "aider"
    assert usage["model_canonical"] == "gpt-4o"
    assert usage["quality"]["tokens"] == "factual"
    assert usage["quality"]["cost"] == "unknown"

    rendered = json.dumps(event, sort_keys=True)
    assert "prompt" not in rendered.lower()
    assert "response" not in rendered.lower()
    # "chat" is allowed as a field-name token (files_added_to_chat) but no raw
    # chat history / message content should appear.
    assert "chat history" not in rendered.lower()
    assert "/Users/" not in rendered


def test_aider_fixture_stays_unknown_when_tokens_absent(tmp_path):
    payload = json.loads(
        (FIXTURE_DIR / "aider_session.json").read_text(encoding="utf-8")
    )
    payload["usage"] = {}
    custom = tmp_path / "aider_no_tokens.json"
    custom.write_text(json.dumps(payload), encoding="utf-8")

    event = aider.parse_session_file(custom)

    assert event["token_input"] == 0
    assert event["token_output"] == 0
    usage = event["raw_meta"]["usage"]
    assert usage["quality"]["tokens"] == "unknown"
    assert usage["provenance"]["token_source"] == "not_exposed"


def test_provider_fixture_parsers_reject_unknown_schema(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": "unknown"}), encoding="utf-8")

    with pytest.raises(ValueError):
        gemini_cli.parse_session_file(bad)
    with pytest.raises(ValueError):
        opencode.parse_session_file(bad)
    with pytest.raises(ValueError):
        cursor_agent.parse_session_file(bad)
    with pytest.raises(ValueError):
        roo_kilo_code.parse_task_file(bad)
    with pytest.raises(ValueError):
        copilot_agents.parse_session_file(bad)
    with pytest.raises(ValueError):
        continue_dev.parse_session_file(bad)
    with pytest.raises(ValueError):
        aider.parse_session_file(bad)
