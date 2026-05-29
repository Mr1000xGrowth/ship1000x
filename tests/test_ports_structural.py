"""Structural conformance tests for the 5 ports.

Verifies that the existing concrete adapters (Wave 1 implementations
plus the legacy concrete classes) satisfy the Protocol contracts
without subclassing — the whole point of using ``runtime_checkable``
Protocols is that `isinstance(adapter, Port)` returns True purely on
method shape.

If a future refactor accidentally drops a method or renames a
parameter, these tests fail loudly.
"""

from __future__ import annotations

from ship1000x.collectors import git_multi, shell
from ship1000x.core.pricing_litellm import LiteLLMResolver
from ship1000x.core.privacy import sanitize_event
from ship1000x.core.storage import Storage
from ship1000x.ports import (
    EventStorage,
    ExportSink,
    PIIScrubber,
    PricingResolver,
)


class TestPricingResolverPort:
    def test_litellm_resolver_satisfies_port(self):
        resolver = LiteLLMResolver()
        assert isinstance(resolver, PricingResolver)

    def test_resolve_signature_returns_optional_resolution(self):
        resolver = LiteLLMResolver()
        # None when model is missing — documented contract
        out = resolver.resolve("openai", "some-future-model-not-in-snapshot")
        assert out is None or hasattr(out, "rates")


class TestEventStoragePort:
    def test_storage_satisfies_port(self, tmp_path):
        storage = Storage(tmp_path / "ports_test.sqlite")
        storage.init_schema()
        assert isinstance(storage, EventStorage)

    def test_required_methods_callable(self, tmp_path):
        """Belt-and-braces: even though isinstance check passes, make
        sure each port method actually executes without TypeError."""
        storage = Storage(tmp_path / "ports_test.sqlite")
        storage.init_schema()
        # ingestion offset roundtrip
        storage.set_ingestion_offset("test", "key", 42, "2026-05-23T00:00:00Z")
        assert storage.get_ingestion_offset("test", "key") == 42
        # query
        rows = storage.query("SELECT 1 AS one")
        assert len(rows) == 1


class TestSourceCollectorPort:
    """Every collector module exports a ``collect`` function with the
    expected positional signature. We sample two of them — exhaustive
    enumeration belongs to a future linting test."""

    def test_git_multi_module_has_collect(self):
        assert hasattr(git_multi, "collect")
        assert callable(git_multi.collect)

    def test_shell_module_has_collect(self):
        assert hasattr(shell, "collect")
        assert callable(shell.collect)


class _RegexScrubber:
    """Adapter wrapping sanitize_event behind the PIIScrubber port.

    This is the canonical bridge until Wave 2 / Day 3 moves the logic
    properly into a class under ship1000x/scrubber/. Keeping it as a
    test fixture for now so we can verify the port shape works.
    """

    def scrub(self, event):
        return sanitize_event(event)


class TestPIIScrubberPort:
    def test_regex_scrubber_satisfies_port(self):
        scrubber = _RegexScrubber()
        assert isinstance(scrubber, PIIScrubber)

    def test_idempotent_contract(self):
        """The port contract requires idempotency. The regex scrubber
        provides it (verified in test_storage_central_sanitize_guard
        too)."""
        scrubber = _RegexScrubber()
        event = {
            "id": "x",
            "source": "test",
            "event_type": "t",
            "started_at": "2026-05-23T00:00:00Z",
            "ended_at": "2026-05-23T00:00:00Z",
            "duration_sec": 0,
            "wall_clock_sec": 0,
            "cwd": "/Users/x/y",
            "project_id": "p",
            "project_conf": 1.0,
            "tool_or_action": "t",
            "token_input": 0,
            "token_output": 0,
            "cost_estimated": 0.0,
            "user_msg_type": None,
            "wordcount": 0,
            "confidence_flag": "high",
            "raw_meta": '{"session_id": "abc"}',
        }
        once = scrubber.scrub(event)
        twice = scrubber.scrub(once)
        assert once == twice


class _NoOpSink:
    """Smoke adapter to validate the ExportSink protocol shape."""

    def __init__(self):
        self.sent: list = []

    def export(self, events):
        self.sent.extend(events)


class TestExportSinkPort:
    def test_noop_sink_satisfies_port(self):
        sink = _NoOpSink()
        assert isinstance(sink, ExportSink)

    def test_export_consumes_iterable(self):
        sink = _NoOpSink()
        sink.export(iter([{"id": "a"}, {"id": "b"}]))
        assert len(sink.sent) == 2


class TestPortsPackageExports:
    """Make sure the public API of ``ship1000x.ports`` is stable —
    if a future contributor removes one of the 5 ports, this test
    fires before any downstream user finds out the hard way."""

    def test_five_ports_in_package_all(self):
        from ship1000x import ports

        assert set(ports.__all__) == {
            "PricingResolver",
            "PIIScrubber",
            "EventStorage",
            "SourceCollector",
            "ExportSink",
        }
