"""Wave 2.5 — ExportSink adapter wrappers conform to the port.

Verifies the wrapper classes (S3RollupSink, InsightsCloudSink, NoOpSink)
satisfy the :class:`ship1000x.ports.ExportSink` Protocol structurally,
and that they correctly dispatch to the legacy functions.

No real S3 push happens — boto3 is monkey-patched out via
``unittest.mock``.
"""

from __future__ import annotations

from unittest.mock import patch

from ship1000x.exporters.sinks import (
    InsightsCloudSink,
    NoOpSink,
    S3RollupSink,
)
from ship1000x.ports import ExportSink


class TestStructuralConformance:
    """All three classes satisfy the ExportSink Protocol."""

    def test_s3_rollup_sink_conforms(self):
        sink = S3RollupSink({"bucket": "x"}, "user@example.com")
        assert isinstance(sink, ExportSink)

    def test_insights_cloud_sink_conforms(self):
        sink = InsightsCloudSink({"bucket": "x"}, "user@example.com")
        assert isinstance(sink, ExportSink)

    def test_noop_sink_conforms(self):
        sink = NoOpSink()
        assert isinstance(sink, ExportSink)


class TestS3RollupSink:
    def test_export_dispatches_to_push_to_s3(self):
        with patch(
            "ship1000x.exporters.s3_push.push_to_s3"
        ) as mock_push:
            sink = S3RollupSink(
                {"bucket": "rollup-test"},
                "user@example.com",
                machine_id="mac-1",
            )
            rollups = [
                {"date": "2026-05-23", "project_id": "x", "duration_sec": 60},
                {"date": "2026-05-23", "project_id": "y", "duration_sec": 30},
            ]
            sink.export(iter(rollups))
            mock_push.assert_called_once()
            args, kwargs = mock_push.call_args
            assert args[0] == rollups
            assert args[1] == {"bucket": "rollup-test"}
            assert args[2] == "user@example.com"
            assert kwargs.get("machine_id") == "mac-1"

    def test_empty_iterable_does_not_push(self):
        with patch(
            "ship1000x.exporters.s3_push.push_to_s3"
        ) as mock_push:
            sink = S3RollupSink({"bucket": "x"}, "user@example.com")
            sink.export([])
            mock_push.assert_not_called()


class TestInsightsCloudSink:
    def test_export_pushes_first_payload(self):
        with patch(
            "ship1000x.exporters.insights_push.push_insights_to_s3"
        ) as mock_push:
            sink = InsightsCloudSink(
                {"bucket": "insights-test"},
                "alice@example.com",
                month_key="2026-05",
            )
            payload = {"global": {"hours": 100}, "_meta": {"month": "2026-05"}}
            sink.export([payload])
            mock_push.assert_called_once()
            args, kwargs = mock_push.call_args
            assert args[0] == payload
            assert args[1] == {"bucket": "insights-test"}
            assert args[2] == "alice@example.com"
            assert kwargs.get("month_key") == "2026-05"

    def test_export_pushes_only_first_payload(self):
        """Insights are one payload per month; multiple inputs = first
        wins, the rest are ignored to avoid accidental double-push."""
        with patch(
            "ship1000x.exporters.insights_push.push_insights_to_s3"
        ) as mock_push:
            sink = InsightsCloudSink(
                {"bucket": "x"}, "user@example.com"
            )
            sink.export([{"a": 1}, {"b": 2}, {"c": 3}])
            assert mock_push.call_count == 1
            args, _ = mock_push.call_args
            assert args[0] == {"a": 1}

    def test_skips_none_payloads(self):
        with patch(
            "ship1000x.exporters.insights_push.push_insights_to_s3"
        ) as mock_push:
            sink = InsightsCloudSink(
                {"bucket": "x"}, "user@example.com"
            )
            sink.export([None, {"a": 1}])
            args, _ = mock_push.call_args
            assert args[0] == {"a": 1}


class TestNoOpSink:
    def test_export_records_items_for_assertion(self):
        sink = NoOpSink()
        items = [{"a": 1}, {"b": 2}]
        sink.export(items)
        assert sink.received == items

    def test_export_does_not_raise_on_iter(self):
        sink = NoOpSink()
        sink.export(iter([{"a": 1}]))
        assert sink.received == [{"a": 1}]

    def test_two_calls_accumulate(self):
        sink = NoOpSink()
        sink.export([{"a": 1}])
        sink.export([{"b": 2}])
        assert sink.received == [{"a": 1}, {"b": 2}]
