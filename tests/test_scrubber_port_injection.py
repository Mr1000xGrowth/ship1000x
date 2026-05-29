"""Wave 2 / Day 3 — Storage now accepts an injected PIIScrubber.

Verifies the default is RegexScrubber (= identical behavior to Wave 1)
and that an injected stricter / more-permissive scrubber can replace
the privacy layer without touching call sites.
"""

from __future__ import annotations

import json

from ship1000x.core.scrubber import RegexScrubber, default_scrubber
from ship1000x.core.storage import Storage
from ship1000x.ports import PIIScrubber


def test_regex_scrubber_satisfies_scrubber_port():
    assert isinstance(RegexScrubber(), PIIScrubber)


def test_default_scrubber_returns_a_regex_scrubber():
    s = default_scrubber()
    assert isinstance(s, RegexScrubber)


def test_default_scrubber_is_singleton():
    assert default_scrubber() is default_scrubber()


def test_storage_uses_default_scrubber_when_none_injected(tmp_path):
    """No scrubber argument → behavior identical to Wave 1."""
    storage = Storage(tmp_path / "db.sqlite")
    storage.init_schema()
    event = {
        "id": "default-1",
        "source": "test",
        "event_type": "t",
        "started_at": "2026-05-23T00:00:00Z",
        "ended_at": "2026-05-23T00:00:00Z",
        "duration_sec": 0,
        "wall_clock_sec": 0,
        "cwd": "/Users/alice/project",
        "project_id": "p",
        "project_conf": 1.0,
        "tool_or_action": "t",
        "token_input": 0,
        "token_output": 0,
        "cost_estimated": 0.0,
        "user_msg_type": None,
        "wordcount": 0,
        "confidence_flag": "high",
        "raw_meta": json.dumps({"session_id": "abc", "prompt": "leak me"}),
    }
    storage.upsert_event(event)
    rows = storage.query("SELECT cwd, raw_meta FROM events WHERE id = ?", ("default-1",))
    # Default RegexScrubber stripped the forbidden 'prompt' key
    assert "prompt" not in rows[0]["raw_meta"]
    # And anonymised the cwd
    assert "alice" not in rows[0]["cwd"]


class _UltraStrictScrubber:
    """Test double — drops the entire raw_meta to verify injection actually fires."""

    def __init__(self):
        self.scrub_calls = 0

    def scrub(self, event):
        self.scrub_calls += 1
        out = dict(event)
        out["raw_meta"] = None
        return out


def test_storage_uses_injected_scrubber(tmp_path):
    """Injected ultra-strict scrubber replaces the default and fires
    on every upsert."""
    injected = _UltraStrictScrubber()
    storage = Storage(tmp_path / "db.sqlite", scrubber=injected)
    storage.init_schema()
    event = {
        "id": "injected-1",
        "source": "test",
        "event_type": "t",
        "started_at": "2026-05-23T00:00:00Z",
        "ended_at": "2026-05-23T00:00:00Z",
        "duration_sec": 0,
        "wall_clock_sec": 0,
        "cwd": "/Users/alice/project",
        "project_id": "p",
        "project_conf": 1.0,
        "tool_or_action": "t",
        "token_input": 0,
        "token_output": 0,
        "cost_estimated": 0.0,
        "user_msg_type": None,
        "wordcount": 0,
        "confidence_flag": "high",
        "raw_meta": json.dumps({"session_id": "abc"}),
    }
    storage.upsert_event(event)
    assert injected.scrub_calls == 1
    rows = storage.query("SELECT raw_meta FROM events WHERE id = ?", ("injected-1",))
    # Ultra-strict dropped raw_meta entirely.
    assert rows[0]["raw_meta"] is None


def test_injection_is_per_storage_instance(tmp_path):
    """Each Storage instance carries its own scrubber — no global
    side-effect when tests / Premium / OSS instantiate side-by-side."""
    strict_a = _UltraStrictScrubber()
    storage_a = Storage(tmp_path / "a.sqlite", scrubber=strict_a)
    storage_a.init_schema()
    storage_b = Storage(tmp_path / "b.sqlite")  # default scrubber
    storage_b.init_schema()
    event = {
        "id": "iso",
        "source": "test",
        "event_type": "t",
        "started_at": "2026-05-23T00:00:00Z",
        "ended_at": "2026-05-23T00:00:00Z",
        "duration_sec": 0,
        "wall_clock_sec": 0,
        "cwd": "/Users/alice/x",
        "project_id": "p",
        "project_conf": 1.0,
        "tool_or_action": "t",
        "token_input": 0,
        "token_output": 0,
        "cost_estimated": 0.0,
        "user_msg_type": None,
        "wordcount": 0,
        "confidence_flag": "high",
        "raw_meta": json.dumps({"session_id": "abc"}),
    }
    storage_a.upsert_event(event)
    storage_b.upsert_event(event)
    rows_a = storage_a.query("SELECT raw_meta FROM events WHERE id = ?", ("iso",))
    rows_b = storage_b.query("SELECT raw_meta FROM events WHERE id = ?", ("iso",))
    # storage_a was ultra-strict → raw_meta wiped
    assert rows_a[0]["raw_meta"] is None
    # storage_b kept the default scrubber → raw_meta preserved (just sanitised)
    assert rows_b[0]["raw_meta"] is not None
    assert "session_id" in rows_b[0]["raw_meta"]
