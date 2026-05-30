"""``EventStorage`` port — persist + query normalised events.

The OSS adapter is the SQLite-backed :class:`ship1000x.core.storage.Storage`.
Premium adapters can implement ``PostgresStorage`` /
``ClickHouseStorage`` for larger-scale workloads in Wave 5.

The port covers three concerns:

1. **Upsert** — write an event with INSERT OR IGNORE semantics.
2. **Query** — read events with arbitrary SQL (kept here because we
   target SQLite-compatible adapters; non-SQL stores will need a
   richer query DSL that we have not designed yet).
3. **Ingestion offsets** — opaque key/value persistence used by
   collectors to remember where they stopped.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EventStorage(Protocol):
    """Persistent store for normalised events + ingestion offsets.

    Implementations must apply a :class:`PIIScrubber` inside
    ``upsert_event`` so no caller can bypass privacy by going through
    the storage adapter directly. See ``ship1000x.core.storage.Storage.upsert_event``
    for the canonical implementation of that guarantee.
    """

    def upsert_event(self, event: dict[str, Any], replace: bool = False) -> None:
        """Insert an event with INSERT OR IGNORE or INSERT OR REPLACE semantics.

        ``replace=False`` (default) — first insert wins. Right for
        immutable events (commits, finalised sessions).

        ``replace=True`` — overwrites the existing row. Right for
        aggregated events that get refined as more data arrives
        (Claude Code session_day events updated by post-compact data,
        for example).
        """
        ...

    def query(self, sql: str, params: tuple = ()) -> list:
        """Execute a parameterised SQL query, return rows.

        Rows behave like a sequence of ``sqlite3.Row`` instances —
        indexable by both position and column name. Adapters backed
        by non-SQL stores should expose a row-like compatibility
        wrapper.
        """
        ...

    def get_ingestion_offset(self, source: str, key: str) -> int:
        """Return the integer offset value previously set, or 0."""
        ...

    def set_ingestion_offset(
        self,
        source: str,
        key: str,
        value: int,
        timestamp: str,
    ) -> None:
        """Persist a new offset value with a timestamp marker."""
        ...
