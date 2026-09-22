"""
Persistence for scheduled trips.

The map itself is stateless — it asks for live data and throws it away. The
scheduler cannot be: a background job has to read your schedule while the app
is closed, which is precisely the trigger CLAUDE.md named for adding a database.

Two backends, one interface. `DATABASE_URL` selects Postgres; without it this
falls back to a SQLite file, so the whole feature runs locally with no signup
and no external service. The SQL is deliberately plain so both dialects accept
it; `_q()` handles the one real difference, the parameter placeholder.

Rows carry a `user_id` from the start, defaulted to 'local'. There is one user
today, but the column costs nothing now and turns multi-user into a migration
rather than a rewrite.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime

DB_URL = os.environ.get("DATABASE_URL", "")
SQLITE_PATH = os.environ.get("SCHEDULE_DB_PATH", "schedule.db")
LOCAL_USER = "local"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_trips (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    label        TEXT NOT NULL,
    destination  TEXT NOT NULL,
    dest_lat     REAL NOT NULL,
    dest_lon     REAL NOT NULL,
    arrive_at    TEXT,
    days         TEXT,
    time_of_day  TEXT,
    source       TEXT NOT NULL,
    external_id  TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
)
"""

_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    user_id TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT,
    PRIMARY KEY (user_id, key)
)
"""


def using_postgres() -> bool:
    return bool(DB_URL)


def _q(sql: str) -> str:
    """SQLite takes ? placeholders, Postgres takes %s. Everything else is shared."""
    return sql.replace("?", "%s") if using_postgres() else sql


def _connect():
    if using_postgres():
        import psycopg2  # imported lazily so local dev needs no driver
        return psycopg2.connect(DB_URL)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(_SCHEMA)
        cur.execute(_SETTINGS_SCHEMA)
        conn.commit()


_COLUMNS = ["id", "user_id", "label", "destination", "dest_lat", "dest_lon",
            "arrive_at", "days", "time_of_day", "source", "external_id",
            "enabled", "created_at"]


def _row_to_trip(row) -> dict:
    trip = dict(zip(_COLUMNS, row)) if not isinstance(row, sqlite3.Row) else {
        k: row[k] for k in _COLUMNS}
    trip["enabled"] = bool(trip["enabled"])
    return trip


def list_trips(user_id: str = LOCAL_USER, include_disabled: bool = True) -> list[dict]:
    sql = f"SELECT {', '.join(_COLUMNS)} FROM scheduled_trips WHERE user_id = ?"
    if not include_disabled:
        sql += " AND enabled = 1"
    sql += " ORDER BY time_of_day, arrive_at"
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(_q(sql), (user_id,))
        return [_row_to_trip(r) for r in cur.fetchall()]


def save_trip(trip: dict, user_id: str = LOCAL_USER) -> dict:
    """Insert or replace one trip. An `external_id` (an ICS UID) makes re-import
    idempotent: the same calendar event updates its row instead of duplicating."""
    existing = None
    if trip.get("external_id"):
        existing = _find_by_external_id(trip["external_id"], user_id)

    record = {
        "id": trip.get("id") or (existing or {}).get("id") or uuid.uuid4().hex,
        "user_id": user_id,
        "label": trip.get("label") or trip.get("destination") or "Trip",
        "destination": trip.get("destination") or "",
        "dest_lat": float(trip["dest_lat"]),
        "dest_lon": float(trip["dest_lon"]),
        "arrive_at": trip.get("arrive_at"),
        "days": trip.get("days"),
        "time_of_day": trip.get("time_of_day"),
        "source": trip.get("source") or "manual",
        "external_id": trip.get("external_id"),
        "enabled": 1 if trip.get("enabled", True) else 0,
        "created_at": (existing or {}).get("created_at") or datetime.utcnow().isoformat(timespec="seconds"),
    }

    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(_q("DELETE FROM scheduled_trips WHERE id = ? AND user_id = ?"),
                    (record["id"], user_id))
        cur.execute(
            _q(f"INSERT INTO scheduled_trips ({', '.join(_COLUMNS)}) "
               f"VALUES ({', '.join('?' for _ in _COLUMNS)})"),
            tuple(record[c] for c in _COLUMNS),
        )
        conn.commit()
    record["enabled"] = bool(record["enabled"])
    return record


def _find_by_external_id(external_id: str, user_id: str) -> dict | None:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(_q(f"SELECT {', '.join(_COLUMNS)} FROM scheduled_trips "
                       f"WHERE external_id = ? AND user_id = ?"), (external_id, user_id))
        row = cur.fetchone()
        return _row_to_trip(row) if row else None


def delete_trip(trip_id: str, user_id: str = LOCAL_USER) -> bool:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(_q("DELETE FROM scheduled_trips WHERE id = ? AND user_id = ?"),
                    (trip_id, user_id))
        conn.commit()
        return cur.rowcount > 0


def delete_by_source(source: str, user_id: str = LOCAL_USER) -> int:
    """Clear one source's trips — used to reconcile a calendar re-import."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(_q("DELETE FROM scheduled_trips WHERE source = ? AND user_id = ?"),
                    (source, user_id))
        conn.commit()
        return cur.rowcount


def get_setting(key: str, user_id: str = LOCAL_USER) -> str | None:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(_q("SELECT value FROM settings WHERE user_id = ? AND key = ?"),
                    (user_id, key))
        row = cur.fetchone()
        return row[0] if row else None


def set_setting(key: str, value: str | None, user_id: str = LOCAL_USER) -> None:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(_q("DELETE FROM settings WHERE user_id = ? AND key = ?"), (user_id, key))
        if value is not None:
            cur.execute(_q("INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)"),
                        (user_id, key, value))
        conn.commit()
