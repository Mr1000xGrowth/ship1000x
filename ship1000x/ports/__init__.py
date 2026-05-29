"""Ports package — abstract contracts that adapters implement.

Hexagonal / Ports & Adapters architecture for SHIP1000X. Five ports
verrouillés en Wave 2 et tenus jusqu'en 2027 minimum :

- :class:`PricingResolver`     — model + provider → rate card
- :class:`PIIScrubber`         — event → safely-storable event
- :class:`EventStorage`        — persistent store for normalised events
- :class:`SourceCollector`     — local source → stream of events
- :class:`ExportSink`          — events → outbound destination

Each port is a Python ``Protocol``. Adapters do not subclass; they just
need to satisfy the structural contract. Two consequences :

1. **Adapters are swappable.** OSS ships local adapters
   (``SQLiteStorage``, ``RegexScrubber``, ``LocalTable + LiteLLMSnapshot``
   pricing, all 22 collectors). Premium ships cloud / enterprise
   adapters (``PostgresStorage``, ``PresidioScrubber``,
   ``CloudSyncSink``, ``TeamAggregator``) in a separate package that
   imports the ports from here.

2. **The cœur OSS is testable in isolation.** Each port has a
   reference implementation in tests/ that can stand in for the real
   adapter when running unit tests on the domain logic.

The ports are pure abstractions — they import nothing from outside
the standard library + ``ship1000x.core.pricing`` (for the
``PricingResolution`` dataclass which is part of the domain).
"""

from ship1000x.ports.collector import SourceCollector
from ship1000x.ports.pricing import PricingResolver
from ship1000x.ports.scrubber import PIIScrubber
from ship1000x.ports.sink import ExportSink
from ship1000x.ports.storage import EventStorage

__all__ = [
    "PricingResolver",
    "PIIScrubber",
    "EventStorage",
    "SourceCollector",
    "ExportSink",
]
