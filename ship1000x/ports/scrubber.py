"""``PIIScrubber`` port — anonymise an event before storage.

The OSS adapter is the regex-based scrubber in
:func:`ship1000x.core.privacy.sanitize_event` (current default).
Premium adapters can swap in Microsoft Presidio for NER-based PII
detection (multi-language, configurable rules) in Wave 6.

The contract is intentionally simple: a scrubber takes a dict-shaped
event and returns a dict-shaped event with all dangerous fields
removed or hashed. Idempotent — calling it twice produces the same
result as calling it once.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PIIScrubber(Protocol):
    """A scrubber takes an event dict and returns a safely-storable event dict.

    Implementations must be **idempotent**: ``scrub(scrub(event))``
    must equal ``scrub(event)``. The central storage guard relies on
    this property (Wave 1 / J4) so it can re-scrub upstream-scrubbed
    events without changing behaviour.

    Implementations must be **fail-safe**: an event that cannot be
    fully scrubbed (e.g. contains an unexpected nested structure) is
    either rejected (``raw_meta`` set to ``None``) or scrubbed
    defensively. Never let an event pass through unchanged when in
    doubt.

    The scrubber MUST NOT raise on malformed input — collectors run
    in a long ingestion loop and a single bad event must not stall
    the whole run.
    """

    def scrub(self, event: dict[str, Any]) -> dict[str, Any]:
        """Return a safely-storable copy of ``event``.

        - Anonymises absolute home paths (``cwd``).
        - Strips forbidden ``raw_meta`` keys (``prompt``, ``response``,
          ``command``, ``stdout``, ``stderr``, ...).
        - Whitelist-filters ``raw_meta`` against ``ALLOWED_META_KEYS``.

        The returned dict is the same shape as the input (same top-
        level keys), only with dangerous values redacted or hashed.
        """
        ...
