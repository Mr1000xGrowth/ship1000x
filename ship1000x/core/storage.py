"""SQLite storage wrapper.

Gere la DB locale `db/tracker.sqlite` avec le schema commun.
Idempotent : `init_schema()` est no-op si tables existantes.
"""

from __future__ import annotations

import platform
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def _current_machine_id() -> str:
    """ID stable de la machine courante (= platform.node(), ex "Mac-Studio.local").

    Cache sur premier appel pour eviter d'appeler N fois. Toujours non-vide :
    fallback sur "unknown-machine" si platform.node() retourne "".
    """
    node = platform.node() or "unknown-machine"
    return node

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    duration_sec    INTEGER,                 -- temps "actif" : intervalles USER < 5 min
    wall_clock_sec  INTEGER DEFAULT 0,       -- temps wall-clock : first_user_ts -> last_user_ts (Phase B)
    cwd             TEXT,
    project_id      TEXT,
    project_conf    REAL DEFAULT 0.0,
    tool_or_action  TEXT,
    token_input     INTEGER,
    token_output    INTEGER,
    cost_estimated  REAL,
    confidence_flag TEXT DEFAULT 'medium',  -- high | medium | low
    user_msg_type   TEXT,                    -- typed | approval | tool_result | system | paste
    wordcount       INTEGER,
    payload_hash    TEXT,
    raw_meta        TEXT                     -- JSON
);

CREATE INDEX IF NOT EXISTS idx_events_started_at ON events(started_at);
CREATE INDEX IF NOT EXISTS idx_events_project    ON events(project_id, started_at);
CREATE INDEX IF NOT EXISTS idx_events_source     ON events(source, started_at);

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    event_count     INTEGER DEFAULT 0,
    project_id      TEXT,
    project_conf    REAL DEFAULT 0.0,
    primary_tool    TEXT,
    active_sec      INTEGER DEFAULT 0,       -- temps actif reel calcule
    lines_added     INTEGER DEFAULT 0,
    lines_deleted   INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, started_at);

CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    paths           TEXT,                    -- JSON array
    keywords        TEXT,
    git_remotes     TEXT,
    category        TEXT,
    status          TEXT DEFAULT 'active'    -- active | paused | archived
);

CREATE TABLE IF NOT EXISTS daily_rollup (
    date            TEXT NOT NULL,
    project_id      TEXT,
    source          TEXT,
    duration_sec    INTEGER DEFAULT 0,       -- SUM(duration_sec) temps actif (focus humain pur)
    active_sec      INTEGER DEFAULT 0,       -- alias historique = duration_sec (legacy V1)
    wall_clock_sec  INTEGER DEFAULT 0,       -- SUM(wall_clock_sec) temps session (Phase B)
    lines_added     INTEGER DEFAULT 0,       -- SUM lignes ajoutees (git events raw_meta)
    lines_deleted   INTEGER DEFAULT 0,       -- SUM lignes supprimees
    event_count     INTEGER DEFAULT 0,
    user_msg_count  INTEGER DEFAULT 0,
    cost_estimated  REAL DEFAULT 0.0,
    PRIMARY KEY (date, project_id, source)
);

CREATE TABLE IF NOT EXISTS ingestion_state (
    source          TEXT NOT NULL,
    file_key        TEXT NOT NULL,
    last_offset     INTEGER DEFAULT 0,
    last_ingested_at TEXT,
    PRIMARY KEY (source, file_key)
);

CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    machine_id      TEXT,
    display_name    TEXT,
    email           TEXT,
    share_opt_in    INTEGER DEFAULT 0,
    created_at      TEXT
);
"""


class Storage:
    """Wrapper SQLite simple + idempotent."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.conn() as c:
            c.executescript(SCHEMA)
            self._migrate_phase_b(c)

    @staticmethod
    def _migrate_phase_b(c: sqlite3.Connection) -> None:
        """Migrations idempotentes Phase B (wall_clock_sec) + V2 (line categories)."""
        def has_column(table: str, col: str) -> bool:
            rows = c.execute(f"PRAGMA table_info({table})").fetchall()
            return any(r["name"] == col for r in rows)

        if not has_column("events", "wall_clock_sec"):
            c.execute("ALTER TABLE events ADD COLUMN wall_clock_sec INTEGER DEFAULT 0")
        if not has_column("daily_rollup", "wall_clock_sec"):
            c.execute("ALTER TABLE daily_rollup ADD COLUMN wall_clock_sec INTEGER DEFAULT 0")
        if not has_column("daily_rollup", "lines_added"):
            c.execute("ALTER TABLE daily_rollup ADD COLUMN lines_added INTEGER DEFAULT 0")
        if not has_column("daily_rollup", "lines_deleted"):
            c.execute("ALTER TABLE daily_rollup ADD COLUMN lines_deleted INTEGER DEFAULT 0")

        # V2 line categories : real / seed / vendored / generated
        # lines_added reste la somme des 4 (retrocompat). Les nouvelles colonnes
        # permettent au dashboard de calculer les metriques defendables (ratios
        # sur real uniquement) sans casser les lectures legacy.
        for cat in ("real", "seed", "vendored", "generated"):
            for direction in ("added", "deleted"):
                col = f"lines_{cat}_{direction}"
                if not has_column("daily_rollup", col):
                    c.execute(f"ALTER TABLE daily_rollup ADD COLUMN {col} INTEGER DEFAULT 0")

        # V2 multi-machines (Phase 1+2) :
        #   events.machine_id       : machine source (Mac Studio.local, MBP.local, ...)
        #   daily_rollup.machine_id : idem, AJOUTE A LA PRIMARY KEY pour permettre
        #                             plusieurs rollups (date, project, source) quand
        #                             plusieurs machines ont de l'activite le meme jour
        #   daily_rollup.unique_commit_hashes_json : JSON array des hashes uniques
        #                                            pour source='git', permet le
        #                                            dedup cross-machines cote dashboard
        #   daily_rollup.machine_origin           : 'Mac-Studio.local' / 'MBP.local' /
        #                                            'shared' (hybride A+C selon plan)
        if not has_column("events", "machine_id"):
            c.execute("ALTER TABLE events ADD COLUMN machine_id TEXT")

        # Migration daily_rollup : ancien schema PK = (date, project_id, source).
        # Si on detecte ca et qu'on n'a pas encore migre, on recree la table
        # avec PK = (date, project_id, source, machine_id). Les rollups sont
        # toujours regenerables via `ship1000x rollup` donc on peut dropper sans
        # perte — c'est plus propre qu'un hack qui laisse la vieille PK en place.
        need_rollup_migration = not has_column("daily_rollup", "machine_id")
        if need_rollup_migration:
            # Sauvegarde des data existantes (juste au cas ou)
            c.execute("ALTER TABLE daily_rollup RENAME TO daily_rollup_v1_backup")
            # Nouvelle table avec PK etendue + toutes les colonnes V2
            c.execute("""
                CREATE TABLE daily_rollup (
                    date            TEXT NOT NULL,
                    project_id      TEXT,
                    source          TEXT,
                    machine_id      TEXT NOT NULL DEFAULT 'unknown-machine',
                    duration_sec    INTEGER DEFAULT 0,
                    active_sec      INTEGER DEFAULT 0,
                    wall_clock_sec  INTEGER DEFAULT 0,
                    lines_added     INTEGER DEFAULT 0,
                    lines_deleted   INTEGER DEFAULT 0,
                    lines_real_added      INTEGER DEFAULT 0,
                    lines_real_deleted    INTEGER DEFAULT 0,
                    lines_seed_added      INTEGER DEFAULT 0,
                    lines_seed_deleted    INTEGER DEFAULT 0,
                    lines_vendored_added  INTEGER DEFAULT 0,
                    lines_vendored_deleted INTEGER DEFAULT 0,
                    lines_generated_added  INTEGER DEFAULT 0,
                    lines_generated_deleted INTEGER DEFAULT 0,
                    unique_commit_hashes_json TEXT,
                    machine_origin  TEXT,
                    event_count     INTEGER DEFAULT 0,
                    user_msg_count  INTEGER DEFAULT 0,
                    cost_estimated  REAL DEFAULT 0.0,
                    PRIMARY KEY (date, project_id, source, machine_id)
                )
            """)
            # On NE copie PAS les anciennes data : il faut les regenerer via
            # `ship1000x rollup` pour qu'elles portent machine_id + unique_hashes.
            # L'ancienne table reste disponible en backup si besoin.

    def upsert_event(self, event: dict[str, Any], replace: bool = False) -> None:
        """Insert un event.

        - replace=False (defaut) : INSERT OR IGNORE. 1er insert gagne (commits git,
          events immutables).
        - replace=True : INSERT OR REPLACE. Necessaire pour sessions Claude Code
          multi-jours (un fichier JSONL peut durer plusieurs jours via /compact)
          : on doit pouvoir re-ecraser l'event agrege avec les donnees a jour.
        """
        cols = [
            "id", "source", "event_type", "started_at", "ended_at",
            "duration_sec", "wall_clock_sec", "cwd", "project_id", "project_conf",
            "tool_or_action", "token_input", "token_output", "cost_estimated",
            "confidence_flag", "user_msg_type", "wordcount",
            "payload_hash", "raw_meta", "machine_id",
        ]
        # Machine_id centralise : si le collector n'a pas mis la clef, fallback
        # sur platform.node(). Evite de toucher chacun des 11 collectors.
        values = [event.get(c) for c in cols]
        if values[-1] is None:
            values[-1] = _current_machine_id()
        placeholders = ", ".join(["?"] * len(cols))
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        sql = f"{verb} INTO events ({', '.join(cols)}) VALUES ({placeholders})"
        with self.conn() as c:
            c.execute(sql, values)

    def upsert_session(self, session: dict[str, Any]) -> None:
        cols = [
            "id", "source", "started_at", "ended_at", "event_count",
            "project_id", "project_conf", "primary_tool",
            "active_sec", "lines_added", "lines_deleted",
        ]
        values = [session.get(c) for c in cols]
        placeholders = ", ".join(["?"] * len(cols))
        sql = f"INSERT OR REPLACE INTO sessions ({', '.join(cols)}) VALUES ({placeholders})"
        with self.conn() as c:
            c.execute(sql, values)

    def get_ingestion_offset(self, source: str, file_key: str) -> int:
        with self.conn() as c:
            row = c.execute(
                "SELECT last_offset FROM ingestion_state WHERE source = ? AND file_key = ?",
                (source, file_key),
            ).fetchone()
            return row["last_offset"] if row else 0

    def set_ingestion_offset(self, source: str, file_key: str, offset: int, ts: str) -> None:
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO ingestion_state (source, file_key, last_offset, last_ingested_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, file_key) DO UPDATE SET
                    last_offset = excluded.last_offset,
                    last_ingested_at = excluded.last_ingested_at
                """,
                (source, file_key, offset, ts),
            )

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.conn() as c:
            return list(c.execute(sql, params).fetchall())
