"""Wave 2 / Day 6-7 — exhaustive SourceCollector protocol conformance.

Walks every module under ``ship1000x/collectors/`` and asserts that
each production collector module exports a ``collect`` callable with the
expected signature ``(storage, classifier, privacy_config) -> dict[str, int]``.

The audit currently tracks 6 modules in ``ship1000x/collectors/`` as
**fixture-only parsers** without a runtime ``collect()`` function —
they were added during provider expansion previews as
fixture-tested parsers waiting to be promoted into real collectors
once the underlying source has been observed in the wild. The Wave 7+
roadmap covers their promotion.

This test enforces two invariants :

1. Every module listed in ``ACTIVE_COLLECTORS`` exports a
   ``collect`` callable with the expected signature.
2. Every module listed in ``FIXTURE_ONLY_PARSERS`` does NOT export
   a ``collect`` function (catches accidental promotion without
   following the Wave 7 promotion checklist).

When a fixture-only parser is promoted, move its name from
``FIXTURE_ONLY_PARSERS`` to ``ACTIVE_COLLECTORS`` in
``ship1000x.core.source_inventory`` and the new ``collect`` signature gets
validated automatically.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib

import pytest

from ship1000x.core.source_inventory import (
    ACTIVE_COLLECTOR_MODULES,
    FIXTURE_ONLY_PARSER_MODULES,
)

COLLECTORS_DIR = (
    pathlib.Path(__file__).resolve().parent.parent
    / "ship1000x"
    / "collectors"
)

ACTIVE_COLLECTORS = ACTIVE_COLLECTOR_MODULES
FIXTURE_ONLY_PARSERS = FIXTURE_ONLY_PARSER_MODULES


def _enumerate_collector_modules() -> list[str]:
    """All non-__init__ Python modules under ship1000x/collectors/."""
    return sorted(
        p.stem
        for p in COLLECTORS_DIR.glob("*.py")
        if p.stem != "__init__"
    )


@pytest.fixture(params=sorted(ACTIVE_COLLECTORS))
def active_collector_module(request):
    return importlib.import_module(f"ship1000x.collectors.{request.param}")


@pytest.fixture(params=sorted(FIXTURE_ONLY_PARSERS))
def fixture_only_module(request):
    return importlib.import_module(f"ship1000x.collectors.{request.param}")


class TestActiveCollectorsImplementSourceCollector:
    """One test per production collector. A new collector added to
    ACTIVE_COLLECTORS is automatically covered."""

    def test_module_exports_collect_callable(self, active_collector_module):
        assert hasattr(active_collector_module, "collect"), (
            f"{active_collector_module.__name__} must expose a `collect` function"
        )
        assert callable(active_collector_module.collect)

    def test_collect_has_expected_signature(self, active_collector_module):
        """Signature contract: ``collect(storage, classifier, privacy_config)``."""
        sig = inspect.signature(active_collector_module.collect)
        params = list(sig.parameters.values())
        positional = [
            p for p in params
            if p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        assert len(positional) >= 3, (
            f"{active_collector_module.__name__}.collect must accept at least 3 "
            f"positional args, got {len(positional)}"
        )


class TestFixtureOnlyParsersHaveNoCollect:
    """Wave 7+ promotion gate: parsers in FIXTURE_ONLY_PARSERS must
    NOT expose a `collect` function. The moment one of them gets a
    runtime `collect`, the maintainer must explicitly move it to
    ACTIVE_COLLECTORS — that promotion triggers all the production
    checks (coverage report visibility, ingestion offset handling,
    privacy / sanitize roundtrip, etc.) that fixture-only parsers
    don't need."""

    def test_no_runtime_collect_until_promoted(self, fixture_only_module):
        assert not hasattr(fixture_only_module, "collect"), (
            f"{fixture_only_module.__name__} is in FIXTURE_ONLY_PARSERS but "
            "exposes a `collect` function — promote it explicitly by moving "
            "its name to ACTIVE_COLLECTORS in ship1000x.core.source_inventory."
        )


class TestCollectorInventoryStability:
    """Meta-tests guarding the active/fixture split and the file system."""

    def test_active_plus_fixture_only_covers_all_modules(self):
        """Catches a new collector file being added without anyone
        deciding whether it's active or fixture-only."""
        on_disk = set(_enumerate_collector_modules())
        catalogued = ACTIVE_COLLECTORS | FIXTURE_ONLY_PARSERS
        missing = on_disk - catalogued
        assert not missing, (
            f"new collector(s) on disk but not catalogued in either "
            f"ACTIVE_COLLECTORS or FIXTURE_ONLY_PARSERS: {sorted(missing)}"
        )

    def test_no_overlap_between_active_and_fixture_only(self):
        overlap = ACTIVE_COLLECTORS & FIXTURE_ONLY_PARSERS
        assert not overlap, (
            f"collector(s) in both active and fixture-only sets: {sorted(overlap)}"
        )

    def test_active_count_matches_coverage_report_expectation(self):
        """The coverage report expects the shipped production collector set on a maintainer
        machine; the inventory must stay aligned."""
        assert len(ACTIVE_COLLECTORS) >= 19, (
            f"active collectors dropped below 19 — current: {len(ACTIVE_COLLECTORS)}"
        )
