"""Detect Claude / Codex client auth mode (OAuth vs API key) for cost honesty.

Why this matters
----------------
SHIP collectors observe token usage from local logs (`~/.claude/projects`,
`~/.codex/sessions`) and apply pay-per-token pricing to estimate cost.
That estimate is correct for users on an API key, where every token is
billed by the provider. It is misleading for users on a consumer/seat
OAuth login (Claude Pro / Max / Team, ChatGPT Plus / Pro / Team) where
token cost is included in a flat monthly subscription up to a quota.

A user who runs Claude Code through Claude Desktop on a Claude Max plan
can see "$1,361 used today" in SHIP and panic, while Anthropic only
charges them $113 (the part that exceeds their quota). The local
estimate is right as an *API-equivalent* number; it is wrong as a
*billed* number. SHIP must surface both dimensions, not one.

This module provides heuristic detectors that map each collected event
to one of:

- ``oauth``: user authenticates via OAuth (Pro / Max / Team / ChatGPT
  Plus / Pro). Pay-per-token estimate is the "API equivalent" cost.
- ``api_key``: user authenticates via an Anthropic / OpenAI API key.
  Pay-per-token estimate is the "billed" cost.
- ``unknown``: signals are ambiguous; reports must say so.

Detection never reads credentials, tokens, or files. It only inspects
environment variables that the user already exposes to the shell, and
fields the provider itself wrote into its own log records.
"""

from __future__ import annotations

import os
from typing import Literal

AuthMode = Literal["oauth", "api_key", "unknown"]


# Entrypoint values written by Claude Code into its JSONL `entrypoint`
# field. Observed in the wild as of mid-2026:
#   - ``claude-desktop``: Claude Desktop wrapper. Desktop authenticates
#     via OAuth only; an API key cannot be used here.
#   - ``cli``: bare Claude Code CLI. Can be OAuth or API key.
#   - ``sdk-cli``: SDK-driven launch (typically another agent calling
#     Claude Code as a subroutine). Can be either.
CLAUDE_DESKTOP_ENTRYPOINTS = frozenset({"claude-desktop", "claude-code-desktop"})
CLAUDE_CLI_ENTRYPOINTS = frozenset({"cli", "claude-cli", "claude-code", "sdk-cli"})


def detect_claude_auth_mode(
    entrypoint: str | None = None,
    *,
    has_api_key_env: bool | None = None,
) -> AuthMode:
    """Best-effort Claude Code / Desktop auth mode.

    Heuristics, in order:

    1. ``entrypoint`` indicates Claude Desktop → ``oauth`` (Desktop has
       no API-key entry path today).
    2. ``ANTHROPIC_API_KEY`` is set in the current process environment
       and ``entrypoint`` is a CLI-style value → ``api_key``.
    3. No API key in env and ``entrypoint`` is a CLI-style value →
       ``oauth`` (CLI almost always falls back to OAuth login when no
       key is provided).
    4. Otherwise → ``unknown``.

    The detector never opens credentials, token files, or the keychain;
    if a future Claude release exposes a more explicit field in its own
    JSONL records, prefer that field over this heuristic.

    ``has_api_key_env`` is exposed for tests so the detector can be
    exercised without mutating the real process environment. In
    production it falls back to ``os.environ`` inspection.
    """
    if entrypoint:
        ent = entrypoint.strip().lower()
        if ent in CLAUDE_DESKTOP_ENTRYPOINTS:
            return "oauth"
    if has_api_key_env is None:
        has_api_key_env = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    ent = (entrypoint or "").strip().lower()
    if ent in CLAUDE_CLI_ENTRYPOINTS:
        return "api_key" if has_api_key_env else "oauth"
    return "unknown"


# Originator values written by Codex into its JSONL `session_meta.originator`
# field. Observed in the wild as of mid-2026:
#   - ``codex-tui``: terminal UI Codex CLI launched interactively. Falls
#     back to ChatGPT OAuth when no API key is set.
#   - ``codex-cli``: scripted CLI launch.
#   - ``codex-desktop``: Codex Desktop wrapper.
CODEX_OAUTH_CAPABLE_ORIGINATORS = frozenset({"codex-tui", "codex-cli", "codex-desktop"})


def detect_codex_auth_mode(
    *,
    originator: str | None = None,
    has_openai_api_key_env: bool | None = None,
) -> AuthMode:
    """Best-effort Codex CLI / Desktop auth mode.

    Heuristics, in order:

    1. ``OPENAI_API_KEY`` is set in the current process environment →
       ``api_key`` (Codex prefers the env key when present).
    2. No API key and ``originator`` is a known Codex client →
       ``oauth`` (Codex authenticates via ChatGPT login as fallback).
    3. Otherwise → ``unknown``.
    """
    if has_openai_api_key_env is None:
        has_openai_api_key_env = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    if has_openai_api_key_env:
        return "api_key"
    if originator and originator.strip().lower() in CODEX_OAUTH_CAPABLE_ORIGINATORS:
        return "oauth"
    return "unknown"


def cost_split_for_mode(
    cost_estimated: float,
    auth_mode: AuthMode,
) -> tuple[float, float]:
    """Split a SHIP-estimated cost into (api_equivalent, billed_estimated).

    The pay-per-token estimate that SHIP computes from local tokens is
    always treated as the **API-equivalent** cost — what the same usage
    would have cost on a pure pay-per-token API key.

    The **billed-estimated** part depends on the auth mode:

    - ``api_key``: the entire estimate is what the provider will bill.
    - ``oauth``: under a subscription, per-token billing is mostly
      absorbed by the plan's flat fee and quota; SHIP returns ``0`` for
      the billed share by default and lets the reconcile layer compare
      against the real billing snapshot for actual overage.
    - ``unknown``: SHIP cannot decide; returns ``0`` for billed so the
      dashboard does not double-count usage that may already be free.

    Callers that want a more nuanced subscription model (quota-aware
    overage estimate) can re-compute the billed share themselves from
    the provider's usage report; this helper is the conservative
    default for the UI.
    """
    cost = float(cost_estimated or 0.0)
    if cost <= 0:
        return 0.0, 0.0
    if auth_mode == "api_key":
        return cost, cost
    # oauth / unknown → estimate is API-equivalent only; billed share
    # is left to the reconcile layer (real billing snapshot).
    return cost, 0.0
