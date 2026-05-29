"""Tests for the auth-mode detection helpers.

These tests never read credentials, token files, or the real process
environment beyond explicit overrides. They drive the public
`detect_claude_auth_mode` and `detect_codex_auth_mode` API surface.
"""

from __future__ import annotations

import pytest

from ship1000x.core.auth_mode import (
    CLAUDE_CLI_ENTRYPOINTS,
    CLAUDE_DESKTOP_ENTRYPOINTS,
    CODEX_OAUTH_CAPABLE_ORIGINATORS,
    cost_split_for_mode,
    detect_claude_auth_mode,
    detect_codex_auth_mode,
)

# --- Claude ------------------------------------------------------------------


def test_claude_desktop_entrypoint_is_oauth_regardless_of_env():
    """Claude Desktop only authenticates via OAuth; an API key in env
    must not change the answer."""
    assert detect_claude_auth_mode("claude-desktop", has_api_key_env=False) == "oauth"
    assert detect_claude_auth_mode("claude-desktop", has_api_key_env=True) == "oauth"


def test_claude_cli_entrypoint_with_api_key_env_is_api_key():
    assert detect_claude_auth_mode("cli", has_api_key_env=True) == "api_key"
    assert detect_claude_auth_mode("sdk-cli", has_api_key_env=True) == "api_key"


def test_claude_cli_entrypoint_without_api_key_env_is_oauth():
    assert detect_claude_auth_mode("cli", has_api_key_env=False) == "oauth"
    assert detect_claude_auth_mode("sdk-cli", has_api_key_env=False) == "oauth"


def test_claude_unknown_entrypoint_is_unknown():
    assert detect_claude_auth_mode("future-launcher", has_api_key_env=False) == "unknown"
    assert detect_claude_auth_mode(None, has_api_key_env=False) == "unknown"
    assert detect_claude_auth_mode("", has_api_key_env=True) == "unknown"


def test_claude_entrypoint_constants_cover_observed_values():
    """Mid-2026 observed entrypoints in real ~/.claude/projects JSONL.
    Keep this assertion stable so future drift is detected by tests."""
    assert "claude-desktop" in CLAUDE_DESKTOP_ENTRYPOINTS
    assert {"cli", "sdk-cli"} <= CLAUDE_CLI_ENTRYPOINTS


# --- Codex ------------------------------------------------------------------


def test_codex_with_openai_api_key_env_is_api_key():
    assert detect_codex_auth_mode(
        originator="codex-tui", has_openai_api_key_env=True
    ) == "api_key"
    # API key in env wins even with an unknown originator.
    assert detect_codex_auth_mode(
        originator="future-codex", has_openai_api_key_env=True
    ) == "api_key"


def test_codex_without_api_key_with_known_originator_is_oauth():
    assert detect_codex_auth_mode(
        originator="codex-tui", has_openai_api_key_env=False
    ) == "oauth"
    assert detect_codex_auth_mode(
        originator="codex-desktop", has_openai_api_key_env=False
    ) == "oauth"


def test_codex_without_api_key_without_originator_is_unknown():
    assert detect_codex_auth_mode(
        originator=None, has_openai_api_key_env=False
    ) == "unknown"
    assert detect_codex_auth_mode(
        originator="future-codex", has_openai_api_key_env=False
    ) == "unknown"


def test_codex_originator_constants_cover_observed_values():
    assert "codex-tui" in CODEX_OAUTH_CAPABLE_ORIGINATORS


# --- cost_split_for_mode -----------------------------------------------------


def test_cost_split_api_key_returns_full_cost_on_both_sides():
    api_eq, billed = cost_split_for_mode(12.50, "api_key")
    assert api_eq == pytest.approx(12.50)
    assert billed == pytest.approx(12.50)


def test_cost_split_oauth_returns_api_equivalent_only():
    api_eq, billed = cost_split_for_mode(12.50, "oauth")
    assert api_eq == pytest.approx(12.50)
    assert billed == pytest.approx(0.0)


def test_cost_split_unknown_returns_api_equivalent_only_billed_zero():
    api_eq, billed = cost_split_for_mode(12.50, "unknown")
    assert api_eq == pytest.approx(12.50)
    assert billed == pytest.approx(0.0)


def test_cost_split_zero_or_negative_returns_zero_pair():
    assert cost_split_for_mode(0.0, "api_key") == (0.0, 0.0)
    assert cost_split_for_mode(-5.0, "oauth") == (0.0, 0.0)
