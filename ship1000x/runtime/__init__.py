"""Runtime helpers — live event capture from external processes.

Sub-modules in this package are typically invoked by external tools
(Claude Code statusline, future ABTop-style daemon) on their own
schedule and write events into a ``drop/`` directory that the regular
SHIP1000X ingestion loop then picks up.

This indirection is intentional :

- The external invocation point (e.g. statusline at every tick of
  Claude Code's UI) must be ultra-fast — no SQLite write, no
  pricing resolution, no privacy scrub. Just append a JSON record
  and exit.
- The SHIP1000X ingestion loop reads ``drop/`` files at its own
  cadence and applies the full pipeline (sanitize, classify, store).

Wave 3 of the SHIP1000X roadmap.
"""
