"""PostgreSQL access layer.

All lease mutations run inside single database transactions that row-lock the
action (`SELECT ... FOR UPDATE`) before deciding whether a lease is free, so
concurrent acquisition attempts across processes/connections are serialised by
the database and exactly one can win.
"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

_pool: ConnectionPool | None = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS actions (
    id          TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS leases (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action_id    TEXT NOT NULL REFERENCES actions(id),
    token_hash   TEXT NOT NULL,
    holder       TEXT NOT NULL,
    acquired_at  TIMESTAMPTZ NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    released_at  TIMESTAMPTZ,
    executed_at  TIMESTAMPTZ,
    -- A lease can only finish once: it is either released or executed.
    CONSTRAINT no_double_finish CHECK (
        released_at IS NULL OR executed_at IS NULL
    )
);
CREATE INDEX IF NOT EXISTS leases_action_id_idx
    ON leases(action_id, id DESC);

CREATE TABLE IF NOT EXISTS action_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action_id   TEXT NOT NULL REFERENCES actions(id),
    lease_id    BIGINT NOT NULL REFERENCES leases(id),
    token_hash  TEXT NOT NULL,
    holder      TEXT NOT NULL,
    result      TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- A successful execution writes exactly one action event per lease.
CREATE UNIQUE INDEX IF NOT EXISTS action_events_lease_uniq
    ON action_events(lease_id);
"""


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            config.DATABASE_URL,
            min_size=1,
            max_size=20,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        _pool.open(wait=True)
    return _pool


def init_db() -> None:
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(SCHEMA_SQL)
        for action_id in config.ACTION_IDS:
            conn.execute(
                "INSERT INTO actions (id, label) VALUES (%s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (action_id, config.ACTION_LABELS[action_id]),
            )
        conn.commit()


def server_now(conn: psycopg.Connection):
    """Server-side UTC clock (PostgreSQL now()).

    Every expiry decision uses THIS value, never a client-supplied clock.
    """
    return conn.execute("SELECT now() AS now").fetchone()["now"]


_STATE_SELECT = """
    SELECT a.id, a.label,
           l.id AS lease_id, l.token_hash, l.holder, l.acquired_at,
           l.expires_at, l.released_at, l.executed_at,
           (SELECT count(*) FROM action_events e WHERE e.action_id = a.id)
               AS event_count
      FROM actions a
      LEFT JOIN LATERAL (
          SELECT * FROM leases
          WHERE action_id = a.id
          ORDER BY id DESC
          LIMIT 1
      ) l ON TRUE
"""


def fetch_action_state(conn: psycopg.Connection, action_id: str) -> dict[str, Any]:
    row = conn.execute(
        _STATE_SELECT + " WHERE a.id = %s ORDER BY a.id",
        (action_id,),
    ).fetchone()
    return state_from_row(row, server_now(conn))


def state_for(
    conn: psycopg.Connection, action_id: str, now
) -> dict[str, Any]:
    """State snapshot inside an existing transaction, with a known clock."""
    row = conn.execute(
        _STATE_SELECT + " WHERE a.id = %s",
        (action_id,),
    ).fetchone()
    return state_from_row(row, now)


def fetch_all_states(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(_STATE_SELECT + " ORDER BY a.id").fetchall()
    now = server_now(conn)
    return [state_from_row(r, now) for r in rows]


def state_from_row(row: dict[str, Any] | None, now) -> dict[str, Any]:
    if row is None:
        raise KeyError("unknown action")
    # Held only while unfinished AND the server clock is strictly before
    # expiry: now == expires_at is already invalid.
    is_held = (
        row["released_at"] is None
        and row["executed_at"] is None
        and row["expires_at"] is not None
        and now < row["expires_at"]
    )
    if is_held:
        remaining = (row["expires_at"] - now).total_seconds()
        remaining = max(0, int(remaining + 0.999999))
    else:
        remaining = 0
    return {
        "action_id": row["id"],
        "label": row["label"],
        "status": "held" if is_held else "free",
        "holder": row["holder"] if is_held else None,
        "acquired_at": row["acquired_at"].isoformat() if is_held else None,
        "expires_at": row["expires_at"].isoformat() if is_held else None,
        "remaining_seconds": remaining,
        "last_executed_by": row["holder"]
        if row["executed_at"] is not None
        else None,
        "event_count": row["event_count"],
        "server_time": now.isoformat(),
    }
