"""Regression test for the central sanitize guard in Storage.upsert_event.

The privacy contract of SHIP rests on a single invariant: no event
can reach the events table without going through ``sanitize_event``.
``Storage.upsert_event`` enforces this by calling ``sanitize_event``
unconditionally before any SQL write, regardless of whether the
collector also called it upstream.

If a future contributor refactors ``upsert_event`` and accidentally
drops that call, these tests fail loudly. They also document that
``sanitize_event`` is idempotent (calling it twice on the same dict
yields the same result), so the existing double-call pattern in 14
collectors is safe — it will be cleaned up as part of the Wave 2
``EventStorage`` port refactor.
"""

from __future__ import annotations

import json

import pytest

from ship1000x.core.privacy import FORBIDDEN_META_KEYS, sanitize_event
from ship1000x.core.storage import Storage


@pytest.fixture
def storage(tmp_path):
    db = Storage(tmp_path / "guard.sqlite")
    db.init_schema()
    return db


def _dirty_event() -> dict:
    """An event that carries a forbidden raw_meta key plus an absolute
    HOME path in cwd — the kind a buggy collector might emit."""
    return {
        "id": "guard-test-1",
        "source": "test_source",
        "event_type": "test",
        "started_at": "2026-05-23T10:00:00Z",
        "ended_at": "2026-05-23T10:00:00Z",
        "duration_sec": 60,
        "wall_clock_sec": 60,
        # Absolute path — must be anonymised by the central guard.
        "cwd": "/Users/somebody/secret/project",
        "project_id": "test",
        "project_conf": 0.9,
        "tool_or_action": "test_action",
        "token_input": 0,
        "token_output": 0,
        "cost_estimated": 0.0,
        "user_msg_type": None,
        "wordcount": 0,
        "confidence_flag": "high",
        "raw_meta": json.dumps({
            "session_id": "abc",
            "turn_count": 1,
            # FORBIDDEN — must be stripped by the central guard.
            "prompt": "delete production user 42",
            # FORBIDDEN — content-like field.
            "command": "rm -rf /",
        }),
    }


class TestCentralSanitizeGuard:
    def test_forbidden_meta_keys_stripped_before_write(self, storage):
        """Even when the caller bypasses ``sanitize_event`` upstream,
        Storage.upsert_event must strip FORBIDDEN_META_KEYS."""
        # Sanity: the test event really does carry forbidden keys.
        raw = json.loads(_dirty_event()["raw_meta"])
        assert raw.keys() & FORBIDDEN_META_KEYS, (
            "test fixture must contain at least one forbidden key"
        )
        storage.upsert_event(_dirty_event())
        rows = storage.query("SELECT raw_meta FROM events WHERE id = ?", ("guard-test-1",))
        assert len(rows) == 1
        stored_meta = json.loads(rows[0]["raw_meta"])
        for forbidden in ("prompt", "command"):
            assert forbidden not in stored_meta, (
                f"central sanitize guard failed to strip '{forbidden}'"
            )

    def test_cwd_anonymised_even_when_collector_skips_sanitize(self, storage):
        """Absolute /Users/<...> paths must be reduced to ~/ by the
        guard, regardless of caller."""
        storage.upsert_event(_dirty_event())
        rows = storage.query("SELECT cwd FROM events WHERE id = ?", ("guard-test-1",))
        assert len(rows) == 1
        cwd = rows[0]["cwd"]
        # Either it's anonymised relative to the running user's home (~/)
        # or replaced with the generic ~/ prefix; never the raw /Users/<x>.
        assert not cwd.startswith("/Users/somebody/"), (
            f"central sanitize guard failed to anonymise cwd: {cwd}"
        )

    def test_double_sanitize_is_idempotent(self, storage):
        """sanitize_event called twice yields the same result as once.

        This is the property that makes the 14 collectors which still
        call sanitize_event upstream safe — the central guard simply
        re-runs the function and gets the same output.
        """
        event = _dirty_event()
        once = sanitize_event(event)
        twice = sanitize_event(once)
        # raw_meta is re-serialised, so compare structurally, not bytewise.
        once_meta = json.loads(once["raw_meta"])
        twice_meta = json.loads(twice["raw_meta"])
        assert once_meta == twice_meta
        assert once.get("cwd") == twice.get("cwd")

    def test_upsert_uses_sanitised_event_as_canonical(self, storage):
        """A pre-sanitised event passed in must roundtrip through the
        store identically (idempotency at the storage layer)."""
        event = sanitize_event(_dirty_event())
        storage.upsert_event(event)
        rows = storage.query("SELECT cwd, raw_meta FROM events WHERE id = ?", ("guard-test-1",))
        assert len(rows) == 1
        assert rows[0]["cwd"] == event["cwd"]
        assert json.loads(rows[0]["raw_meta"]) == json.loads(event["raw_meta"])


class TestNoCollectorBypassesGuard:
    """Lint-style check: every collector that exists today must call
    Storage.upsert_event (or its higher-level wrappers) — never write
    directly to the events table via the raw SQLite connection.

    This is the structural guarantee that lets us declare ``upsert_event``
    the single bottleneck for privacy enforcement.
    """

    def test_no_collector_uses_raw_executemany_on_events(self):
        import pathlib

        collectors_dir = (
            pathlib.Path(__file__).resolve().parent.parent
            / "ship1000x"
            / "collectors"
        )
        offenders = []
        for path in collectors_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            # Heuristic: any collector that runs INSERT ... INTO events
            # outside the upsert_event helper would bypass the guard.
            if "INSERT" in text and "INTO events" in text:
                offenders.append(path.name)
        assert offenders == [], (
            f"collectors writing directly to events bypass the central guard: {offenders}"
        )
