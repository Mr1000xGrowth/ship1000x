"""``ExportSink`` port — publish events to an outbound destination.

OSS adapters today: ``s3_push.py``, ``insights_push.py``,
``markdown_report.py``. Premium adapters in Wave 5: ``CloudSyncSink``
(Garage S3 push multi-machine), ``TeamAggregator`` (cohort
benchmarks), ``SlackAlertSink``, ``LinearIntegration``. Wave 6 adds
``OTelGenAISink`` for Phoenix / Langfuse / Helicone interop.

Sinks are write-side mirror of collectors: they take events from the
local store and push them somewhere outside. Like collectors, they
must be fail-safe (a network blip cannot crash an ingestion run) and
must respect the privacy contract (no event bypasses
:class:`PIIScrubber`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ExportSink(Protocol):
    """A sink consumes a batch of events and publishes them outward.

    The batch is an iterable so adapters can stream-process huge
    exports without loading everything in memory. Adapters that need
    a single shot can materialise it into a list internally.
    """

    def export(self, events: Iterable[dict[str, Any]]) -> None:
        """Publish the batch.

        Must not raise on transient failures — network blip,
        backpressure, partial success should be logged + retried on
        the adapter's own schedule. Raising propagates up to the
        scheduler and stops further sinks.
        """
        ...
