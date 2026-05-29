"""PIIScrubber adapters for SHIP1000X.

The default adapter is :class:`RegexScrubber`, a thin OO wrapper
around the historical :func:`ship1000x.core.privacy.sanitize_event`
function. It satisfies the :class:`ship1000x.ports.PIIScrubber`
Protocol structurally.

Wave 2 / Day 3 of the SHIP1000X roadmap. Wave 6 will add a
``PresidioScrubber`` adapter that uses NER-based multi-language PII
detection for the Premium / enterprise tier; that adapter will live
alongside this one and be selected at runtime via dependency
injection in ``Storage.upsert_event``.
"""

from __future__ import annotations

from typing import Any

from ship1000x.core.privacy import sanitize_event


class RegexScrubber:
    """Whitelist-based regex scrubber — the default SHIP1000X adapter.

    Wraps the historical ``sanitize_event`` function (still exported
    from ``ship1000x.core.privacy`` for backward compatibility) so we
    have a class implementing the :class:`PIIScrubber` Protocol that
    can be injected as a constructor argument into
    ``Storage.upsert_event``.

    The scrubber is :

    - **Stateless** — no per-event mutable state, safe to share
      across threads.
    - **Idempotent** — ``scrub(scrub(event))`` returns the same dict
      as ``scrub(event)``. Verified by the regression suite in
      ``tests/test_storage_central_sanitize_guard.py``.
    - **Fail-safe** — never raises; an unparseable ``raw_meta`` JSON
      is replaced by ``None`` rather than crashing the ingestion
      loop.
    """

    def scrub(self, event: dict[str, Any]) -> dict[str, Any]:
        """Return a safely-storable copy of ``event``.

        See ``ship1000x.core.privacy.sanitize_event`` for the full
        contract — this method is a direct delegate.
        """
        return sanitize_event(event)


# Module-level singleton used by ``Storage`` when no scrubber is
# explicitly injected. Keeping a single instance is fine because the
# scrubber is stateless; we expose ``default_scrubber()`` so callers
# can rebind it for tests if needed.
_default_scrubber: RegexScrubber | None = None


def default_scrubber() -> RegexScrubber:
    """Return the module-level RegexScrubber instance, instantiated lazily."""
    global _default_scrubber
    if _default_scrubber is None:
        _default_scrubber = RegexScrubber()
    return _default_scrubber
