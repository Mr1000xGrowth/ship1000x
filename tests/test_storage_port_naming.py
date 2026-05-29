"""Wave 2 / Day 4-5 — SQLiteStorage adapter name + Storage alias.

The Wave 2 refactor renames the historical ``Storage`` class to
``SQLiteStorage`` to make the adapter identity explicit. The ``Storage``
symbol stays exported as a backward-compat alias so all existing
callers (collectors, CLI, Premium fork) continue to work without any
modification.
"""

from __future__ import annotations

from ship1000x.core.storage import SQLiteStorage, Storage
from ship1000x.ports import EventStorage


def test_sqlite_storage_class_exists():
    assert isinstance(SQLiteStorage, type)


def test_storage_is_alias_of_sqlite_storage():
    """They must be the same object — assignment, not subclass."""
    assert Storage is SQLiteStorage


def test_sqlite_storage_satisfies_event_storage_port(tmp_path):
    storage = SQLiteStorage(tmp_path / "ports_storage.sqlite")
    storage.init_schema()
    assert isinstance(storage, EventStorage)


def test_storage_alias_also_satisfies_port(tmp_path):
    """Belt-and-braces: instances created via the legacy alias also
    satisfy the port (because Storage is SQLiteStorage)."""
    storage = Storage(tmp_path / "alias_storage.sqlite")
    storage.init_schema()
    assert isinstance(storage, EventStorage)


def test_isinstance_check_works_for_both_names(tmp_path):
    """The Premium fork uses isinstance(s, Storage) in places. After
    the rename, that check must still return True for SQLiteStorage
    instances (because it's the same class)."""
    instance = SQLiteStorage(tmp_path / "x.sqlite")
    instance.init_schema()
    assert isinstance(instance, Storage)
    assert isinstance(instance, SQLiteStorage)


def test_collectors_can_keep_importing_storage(tmp_path):
    """Smoke test : the exact import + instantiate pattern used by
    every collector still works."""
    from ship1000x.core.storage import Storage as ImportedStorage

    s = ImportedStorage(tmp_path / "smoke.sqlite")
    s.init_schema()
    # The smoke test passes only if init_schema actually wrote tables
    rows = s.query("SELECT name FROM sqlite_master WHERE type='table' AND name='events'")
    assert len(rows) == 1
