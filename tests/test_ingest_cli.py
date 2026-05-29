"""CLI routing tests for `ship1000x ingest --source ...`.

These tests use monkeypatched collectors only: no real local stores, drop
folders, provider APIs, prompts, or user files are read.
"""

from __future__ import annotations

import importlib

import pytest
from click.testing import CliRunner

import ship1000x.cli as cli_mod
from ship1000x.core.source_inventory import (
    ACTIVE_COLLECTOR_MODULES,
    COLLECTOR_MODULE_TO_INGEST_SOURCE,
    FIXTURE_ONLY_PARSER_MODULES,
    NON_EMITTING_COLLECTOR_MODULES,
    PRODUCTION_EVENT_SOURCES,
)


def _patch_cli_runtime(monkeypatch):
    storage = object()
    classifier = object()
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: storage)
    monkeypatch.setattr(cli_mod, "_get_classifier", lambda: classifier)
    monkeypatch.setattr(cli_mod, "_load_yaml", lambda _path: {})
    return storage, classifier


def _assert_explicit_ingest_calls_collector(
    monkeypatch,
    *,
    source: str,
    module_name: str,
    stats: dict[str, int],
    expected_output: str,
) -> None:
    storage, classifier = _patch_cli_runtime(monkeypatch)
    module = importlib.import_module(f"collectors.{module_name}")
    calls = []

    def fake_collect(received_storage, received_classifier, privacy_config):
        calls.append((received_storage, received_classifier, privacy_config))
        return stats

    monkeypatch.setattr(module, "collect", fake_collect)

    result = CliRunner().invoke(cli_mod.cli, ["ingest", "--source", source])

    assert result.exit_code == 0
    assert calls == [(storage, classifier, {})]
    assert expected_output in result.output


def test_ingest_source_routes_roo_kilo_code(monkeypatch):
    _assert_explicit_ingest_calls_collector(
        monkeypatch,
        source="roo_kilo_code",
        module_name="roo_kilo_code",
        stats={"sessions_ingested": 2, "events_ingested": 3, "files_seen": 4, "skipped": 0},
        expected_output="Roo/Kilo",
    )


def test_ingest_source_routes_claude_statusline(monkeypatch):
    _assert_explicit_ingest_calls_collector(
        monkeypatch,
        source="claude_statusline",
        module_name="claude_statusline",
        stats={"sessions_ingested": 0, "events_ingested": 2, "files_seen": 1, "skipped": 0},
        expected_output="statusline ticks",
    )


def test_ingest_source_routes_agent_runtime(monkeypatch):
    _assert_explicit_ingest_calls_collector(
        monkeypatch,
        source="agent_runtime",
        module_name="agent_runtime",
        stats={
            "sessions_ingested": 0,
            "events_ingested": 2,
            "files_seen": 1,
            "skipped": 0,
            "ticks_aggregated": 5,
        },
        expected_output="runtime events",
    )


def test_ingest_source_routes_trace(monkeypatch):
    _assert_explicit_ingest_calls_collector(
        monkeypatch,
        source="trace",
        module_name="trace",
        stats={"sessions_ingested": 0, "events_ingested": 2, "files_seen": 1, "skipped": 0},
        expected_output="usage-proxy api_call events",
    )


def test_ingest_unknown_source_fails_before_opening_storage(monkeypatch):
    monkeypatch.setattr(cli_mod, "_get_storage", lambda: pytest.fail("storage should not be opened"))

    result = CliRunner().invoke(cli_mod.cli, ["ingest", "--source", "typo_source"])

    assert result.exit_code == 2
    assert "Source inconnue" in result.output
    assert "typo_source" in result.output
    assert "trace" in result.output


def test_ingest_source_inventory_covers_production_collectors():
    ingest_sources = set(cli_mod.INGEST_SOURCE_NAMES)
    expected_sources = {
        COLLECTOR_MODULE_TO_INGEST_SOURCE.get(module_name, module_name)
        for module_name in ACTIVE_COLLECTOR_MODULES
    }
    expected_sources.add("all")

    assert ingest_sources == expected_sources


def test_ingest_source_inventory_excludes_fixture_only_parsers():
    ingest_sources = set(cli_mod.INGEST_SOURCE_NAMES)
    fixture_source_names = {
        "continue" if module_name == "continue_dev" else module_name
        for module_name in FIXTURE_ONLY_PARSER_MODULES
    }

    assert ingest_sources.isdisjoint(fixture_source_names)


def test_production_event_inventory_tracks_emitted_sources_not_enrichment_modules():
    assert "codex_sqlite" in ACTIVE_COLLECTOR_MODULES
    assert "codex_sqlite" in NON_EMITTING_COLLECTOR_MODULES
    assert "codex_sqlite" not in PRODUCTION_EVENT_SOURCES
    assert "web_exports" not in PRODUCTION_EVENT_SOURCES
    assert "web_export" in PRODUCTION_EVENT_SOURCES
    assert "git_secret_alert" in PRODUCTION_EVENT_SOURCES
