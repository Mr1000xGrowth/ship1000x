"""Canonical SHIP source inventory.

This module keeps source identity lists in production code instead of burying
them in tests. It separates collector modules from emitted event source ids:
some collectors enrich existing events, and a few modules emit multiple source
ids through sanitized side effects.
"""

from __future__ import annotations

ACTIVE_COLLECTOR_MODULES = frozenset({
    "agent_runtime",
    "anthropic_usage",
    "claude_code",
    "claude_desktop_sessions",
    "claude_statusline",
    "cline",
    "codex",
    "codex_desktop",
    "codex_macapp",
    "codex_sqlite",
    "cursor",
    "git_multi",
    "mac_system",
    "openai_usage",
    "openclaw",
    "roo_kilo_code",
    "shell",
    "trace",
    "web_exports",
})

RECLASSIFY_COLLECTORS: tuple[tuple[str, str], ...] = (
    ("claude_code", "claude_code"),
    ("openclaw", "openclaw"),
    ("anthropic_usage", "anthropic_usage"),
    ("openai_usage", "openai_usage"),
    ("codex", "codex"),
    ("cursor", "cursor"),
    ("git_multi", "git"),
    ("codex_sqlite", "codex_sqlite"),
    ("cline", "cline"),
    ("codex_macapp", "codex_macapp"),
    ("codex_desktop", "codex_desktop"),
    ("web_exports", "web_exports"),
    ("claude_desktop_sessions", "claude_desktop"),
    ("roo_kilo_code", "roo_kilo_code"),
    ("claude_statusline", "claude_statusline"),
    ("agent_runtime", "agent_runtime"),
    ("trace", "trace"),
    ("shell", "shell"),
    ("mac_system", "mac_system"),
)

FIXTURE_ONLY_PARSER_MODULES = frozenset({
    "aider",
    "continue_dev",
    "copilot_agents",
    "cursor_agent",
    "gemini_cli",
    "opencode",
})

COLLECTOR_MODULE_TO_INGEST_SOURCE = {
    "claude_desktop_sessions": "claude_desktop",
    "git_multi": "git",
}

NON_EMITTING_COLLECTOR_MODULES = frozenset({
    "codex_sqlite",
})

COLLECTOR_MODULE_TO_EVENT_SOURCE = {
    "claude_desktop_sessions": "claude_desktop",
    "git_multi": "git",
    "web_exports": "web_export",
}

ADDITIONAL_PRODUCTION_EVENT_SOURCES = frozenset({
    "git_secret_alert",
})

INGEST_SOURCE_NAMES = tuple(
    sorted(
        {
            COLLECTOR_MODULE_TO_INGEST_SOURCE.get(module_name, module_name)
            for module_name in ACTIVE_COLLECTOR_MODULES
        }
        | {"all"}
    )
)

PRODUCTION_EVENT_SOURCES = frozenset(
    {
        COLLECTOR_MODULE_TO_EVENT_SOURCE.get(module_name, module_name)
        for module_name in ACTIVE_COLLECTOR_MODULES
        if module_name not in NON_EMITTING_COLLECTOR_MODULES
    }
    | ADDITIONAL_PRODUCTION_EVENT_SOURCES
)
