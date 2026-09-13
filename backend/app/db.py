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
    -- NULL for historical single-action executions; linked executions share
    -- one server-generated link id across their two (or more) events.
    link_id     TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- A successful execution writes exactly one action event per lease.
CREATE UNIQUE INDEX IF NOT EXISTS action_events_lease_uniq
    ON action_events(lease_id);

-- Migration for databases created before linked execution existed.
ALTER TABLE action_events ADD COLUMN IF NOT EXISTS link_id TEXT;

-- Rehearsal sessions (场次): a named round grouping the action events
-- executed while it is active.  A session only ever moves
-- active -> ended; it is never deleted and never reopened.
CREATE TABLE IF NOT EXISTS rehearsal_sessions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ
);
-- At most ONE active (not yet ended) session at any instant, enforced at
-- the SQL level: every active row indexes the same key `true`, so a second
-- concurrent insert fails with a unique violation.
CREATE UNIQUE INDEX IF NOT EXISTS rehearsal_sessions_one_active
    ON rehearsal_sessions ((ended_at IS NULL))
    WHERE ended_at IS NULL;

-- Migration for databases created before rehearsal sessions existed.
-- NULL on events executed outside any session (and on all history).
ALTER TABLE action_events ADD COLUMN IF NOT EXISTS session_id BIGINT
    REFERENCES rehearsal_sessions(id);

-- On-site anomaly reports (现场异常) attached to the EXECUTED ACTION EVENT
-- they describe, so a verbal shift handover becomes a traceable record.
-- A report starts 'pending' (待确认) and the server alone may move it
-- exactly once to 'confirmed' (已确认), stamping the confirming seat and
-- time.  One event keeps at most one anomaly ever: a repeat report is a
-- business error, never an overwrite.  Rows are append-only: re-acquiring,
-- releasing, or executing (single or linked) never touches old rows.
CREATE TABLE IF NOT EXISTS action_anomalies (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action_id     TEXT NOT NULL REFERENCES actions(id),
    event_id      BIGINT NOT NULL REFERENCES action_events(id),
    category      TEXT NOT NULL,
    description   TEXT NOT NULL,
    reported_by   TEXT NOT NULL,
    -- Stable identity of the reporting browser console (independent of the
    -- freely editable seat NAME): only a DIFFERENT console id may confirm.
    reporter_id   TEXT NOT NULL,
    reported_at   TIMESTAMPTZ NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    confirmed_by  TEXT,
    confirmer_id  TEXT,
    confirmed_at  TIMESTAMPTZ,
    -- Exactly one anomaly report per executed event.
    CONSTRAINT action_anomalies_event_uniq UNIQUE (event_id),
    -- The only legal transition: pending (nothing stamped) or confirmed
    -- (both confirming seat and time present).
    CONSTRAINT action_anomalies_status_check CHECK (
        (status = 'pending' AND confirmed_by IS NULL
                             AND confirmer_id IS NULL
                             AND confirmed_at IS NULL)
        OR
        (status = 'confirmed' AND confirmed_by IS NOT NULL
                               AND confirmer_id IS NOT NULL
                               AND confirmed_at IS NOT NULL)
    )
);
-- Migrations for databases created before console identities existed.
ALTER TABLE action_anomalies ADD COLUMN IF NOT EXISTS reporter_id TEXT NOT NULL DEFAULT '';
ALTER TABLE action_anomalies ADD COLUMN IF NOT EXISTS confirmer_id TEXT;
"""


def active_session_id(conn: psycopg.Connection) -> int | None:
    """Id of the currently active (not-ended) rehearsal session, if any.

    Read inside the caller's transaction so an execution and its session
    attribution always commit — or roll back — together.
    """
    row = conn.execute(
        "SELECT id FROM rehearsal_sessions"
        " WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def lock_action(conn: psycopg.Connection, action_id: str):
    """Row-lock the action (SELECT ... FOR UPDATE); None if it is unknown.

    Every mutating operation on an action takes this lock first, so lease
    lifecycle, execution and anomaly reports/confirms all serialise on the
    same row instead of racing between a SELECT and its UPDATE.
    """
    return conn.execute(
        "SELECT id FROM actions WHERE id = %s FOR UPDATE",
        (action_id,),
    ).fetchone()


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
               AS event_count,
           (SELECT e.link_id FROM action_events e
             WHERE e.action_id = a.id AND e.link_id IS NOT NULL
             ORDER BY e.id DESC LIMIT 1)
               AS last_link_id,
           (SELECT e.holder FROM action_events e
             WHERE e.action_id = a.id
             ORDER BY e.id DESC LIMIT 1)
               AS last_executed_by,
           (SELECT e.id FROM action_events e
             WHERE e.action_id = a.id
             ORDER BY e.id DESC LIMIT 1)
               AS last_event_id,
           -- The anomaly OF THE LATEST EVENT ONLY (never an older event's:
           -- once a new execution arrives the card switches to that event
           -- and must not display the previous round's anomaly).
           an.id AS anomaly_id, an.event_id AS anomaly_event_id,
           an.category AS anomaly_category, an.description AS anomaly_description,
           an.reported_by AS anomaly_reported_by, an.reporter_id AS anomaly_reporter_id,
           an.reported_at AS anomaly_reported_at,
           an.status AS anomaly_status, an.confirmed_by AS anomaly_confirmed_by,
           an.confirmer_id AS anomaly_confirmer_id,
           an.confirmed_at AS anomaly_confirmed_at
      FROM actions a
      LEFT JOIN LATERAL (
          SELECT * FROM leases
          WHERE action_id = a.id
          ORDER BY id DESC
          LIMIT 1
      ) l ON TRUE
      LEFT JOIN LATERAL (
          SELECT n.* FROM action_anomalies n
          WHERE n.event_id = (
              SELECT e.id FROM action_events e
               WHERE e.action_id = a.id
               ORDER BY e.id DESC LIMIT 1
          )
      ) an ON TRUE
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
        # Derived from the most recent ACTION EVENT, not from the latest
        # lease: after a linked run, re-acquiring (or even being taken over)
        # must keep showing the seat that last executed the action.
        "last_executed_by": row.get("last_executed_by"),
        "event_count": row["event_count"],
        # .get(): rows assembled outside _STATE_SELECT (tests) may lack it.
        "last_link_id": row.get("last_link_id"),
        # The most recent execution event's id and its anomaly (if any).
        # The card's anomaly UI follows THIS event: a new execution switches
        # last_event_id and the old anomaly no longer rides along.
        "last_event_id": row.get("last_event_id"),
        "anomaly": _anomaly_from_state_row(row),
        "server_time": now.isoformat(),
    }


def _anomaly_from_state_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Serialise the latest event's anomaly out of a _STATE_SELECT row.

    Columns carry the anomaly_ prefix there (LEFT JOIN LATERAL); absent
    columns (e.g. hand-built rows in tests) simply yield None.
    """
    if row.get("anomaly_id") is None:
        return None
    return {
        "id": row["anomaly_id"],
        "event_id": row["anomaly_event_id"],
        "category": row["anomaly_category"],
        "description": row["anomaly_description"],
        "reported_by": row["anomaly_reported_by"],
        "reporter_id": row["anomaly_reporter_id"],
        "reported_at": row["anomaly_reported_at"].isoformat(),
        "status": row["anomaly_status"],
        "confirmed_by": row["anomaly_confirmed_by"],
        "confirmer_id": row["anomaly_confirmer_id"],
        "confirmed_at": row["anomaly_confirmed_at"].isoformat()
        if row["anomaly_confirmed_at"]
        else None,
    }
