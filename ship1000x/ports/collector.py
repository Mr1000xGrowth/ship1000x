"""``SourceCollector`` port — read one local source, emit events.

The 22 existing collectors under ``ship1000x/collectors/`` each
implement this protocol structurally. Wave 2 makes the contract
explicit so new collectors get a clear template and IDE-level
type-checking.

The protocol matches the historical ``collect(storage, classifier,
privacy_config)`` signature so the migration is annotation-only —
zero functional change.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SourceCollector(Protocol):
    """A collector reads one local source and writes events to storage.

    Collectors are **stateless** — all per-source state (last seen
    timestamp, file mtime, ingestion offset) lives in the storage
    via ``get_ingestion_offset`` / ``set_ingestion_offset``. This is
    what lets a collector run twice in a row without duplicating
    events.

    Collectors are **fail-safe** — a single broken row in a JSONL file
    or a missing source dir must not crash the whole ingestion. The
    expected behaviour is to log and skip.

    The historical signature uses positional arguments and a
    ``stats`` return dict. We keep that shape exactly so the
    structural Protocol matches all 22 existing implementations
    without code changes.
    """

    def collect(
        self,
        storage: Any,
        classifier: Any,
        privacy_config: dict[str, Any],
    ) -> dict[str, int]:
        """Read the source, write events, return ingestion stats.

        ``storage`` implements the :class:`EventStorage` port —
        collectors call ``storage.upsert_event(event)`` after
        building each event.

        ``classifier`` is the project-classification helper that maps
        paths / git remotes to project ids. Its precise shape is
        currently a concrete class
        (``ship1000x.core.classifier.Classifier``); a future port may
        formalise it but it is not in the Wave 2 lockset.

        ``privacy_config`` is the user's ``privacy.yaml`` parsed into
        a dict. Collectors check it for opt-in flags (``gitleaks_scan``,
        ``shell_history``, ...) and exclude paths.

        Returns a ``stats`` dict with at least ``files_seen``,
        ``events_ingested``, ``skipped``. Per-collector additional
        counters are allowed.
        """
        ...
