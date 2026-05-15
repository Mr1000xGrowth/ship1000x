"""Ship1000x web dashboard (V1.2 MVP).

Local-first Flask app exposing your tracking data as a web UI.
Bound to localhost only by default — never accepts external connections.

Launched via `ship1000x dashboard` CLI command.
"""

from ship1000x.web.app import create_app

__all__ = ["create_app"]
