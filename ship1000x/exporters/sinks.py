"""ExportSink adapter wrappers for the historical SHIP1000X exporters.

Wave 2.5 (Wave 2 deferred clean-up) — wraps the legacy ``s3_push`` and
``insights_push`` functions in classes that satisfy the
:class:`ship1000x.ports.ExportSink` Protocol.

Why this exists separately from the legacy functions
----------------------------------------------------

The legacy ``push_to_s3(rollups, cloud_config, user_email, ...)`` and
``push_insights_to_s3(payload, cloud_config, user_email, ...)`` keep
working unchanged — every caller in the CLI and the Premium fork
continues to use them as before.

The classes here are **bound adapters**: the configuration (cloud
credentials, user email, machine id) is captured in the constructor,
and ``.export(items)`` then dispatches to the legacy function with
that bound config. This is the pattern Premium will follow when it
adds ``CloudSyncSink``, ``SlackAlertSink``, ``LinearIntegration``,
etc. — each adapter binds its config at construction, then exposes a
uniform single-method interface.

Why ``markdown_report`` is NOT wrapped
--------------------------------------

``generate_report(storage, cutoff, since_label)`` is a **report
generator**: it queries the storage backend and returns a Markdown
string. It is not a sink — it doesn't take events to publish, it
takes a storage backend to summarise. Forcing it behind ``ExportSink``
would be a semantic stretch ("ignore the events argument, just call
generate_report"). We keep it out of this module and treat it as a
distinct concern, to be re-examined in a future wave if and when it
makes sense.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class S3RollupSink:
    """Push a batch of rollup dicts to a Garage S3 bucket.

    Wraps :func:`ship1000x.exporters.s3_push.push_to_s3` with a
    constructor-bound cloud config and user email. The ``items``
    parameter of ``.export()`` is interpreted as an iterable of
    rollup dicts (the same shape ``push_to_s3`` expects).

    Construction::

        sink = S3RollupSink(
            cloud_config={"bucket": "...", "endpoint": "..."},
            user_email="you@example.com",
            machine_id="your-machine.local",
        )

    Usage::

        sink.export(rollup_rows)  # rollup_rows from storage.query
    """

    def __init__(
        self,
        cloud_config: dict[str, Any],
        user_email: str,
        machine_id: str = "unknown-machine",
    ):
        self._cloud_config = cloud_config
        self._user_email = user_email
        self._machine_id = machine_id

    def export(self, items: Iterable[dict[str, Any]]) -> None:
        """Push the given rollups to S3 via the legacy function."""
        from ship1000x.exporters.s3_push import push_to_s3

        # Materialise the iterable — push_to_s3 expects a list.
        rollups = list(items)
        if not rollups:
            return
        push_to_s3(
            rollups,
            self._cloud_config,
            self._user_email,
            machine_id=self._machine_id,
        )


class InsightsCloudSink:
    """Push a single insights payload to a Garage S3 bucket.

    Wraps :func:`ship1000x.exporters.insights_push.push_insights_to_s3`.
    The ``items`` parameter of ``.export()`` is an iterable; only
    the **first** non-None payload is pushed (insights are one
    payload per month, so the typical caller passes a single-element
    iterable). Additional items are ignored with no error — the
    Protocol contract says ``.export()`` returns None and must not
    crash; it doesn't say it must push every item.
    """

    def __init__(
        self,
        cloud_config: dict[str, Any],
        user_email: str,
        machine_id: str = "unknown-machine",
        month_key: str | None = None,
        share_config: dict[str, Any] | None = None,
    ):
        self._cloud_config = cloud_config
        self._user_email = user_email
        self._machine_id = machine_id
        self._month_key = month_key
        self._share_config = share_config

    def export(self, items: Iterable[dict[str, Any]]) -> None:
        """Push the first insights payload to S3 via the legacy function."""
        from ship1000x.exporters.insights_push import push_insights_to_s3

        for payload in items:
            if payload is None:
                continue
            push_insights_to_s3(
                payload,
                self._cloud_config,
                self._user_email,
                machine_id=self._machine_id,
                month_key=self._month_key,
                share_config=self._share_config,
            )
            # Insights push: one payload per month is the contract.
            # Stop after the first to avoid accidental double-push.
            return


class NoOpSink:
    """Drop-on-the-floor sink for tests and dry-run scenarios.

    Records exported items in ``self.received`` for assertions.
    Satisfies the ``ExportSink`` Protocol; useful as a default when a
    higher-level pipeline expects a sink but no real destination is
    configured.
    """

    def __init__(self):
        self.received: list[dict[str, Any]] = []

    def export(self, items: Iterable[dict[str, Any]]) -> None:
        self.received.extend(items)
