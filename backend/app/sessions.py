"""Rehearsal sessions (场次): group a stretch of live operation into a named
round so its execution scope and outcome can be judged afterwards.

A session only ever transitions

    active（进行中）→ ended（已结束）

and at most one active session exists at any instant — enforced in SQL by a
partial unique index, so even concurrent start requests serialise to exactly
one winner.  While a session is active, `leases.execute` and
`leases.execute_linked` tag their action events with its id **inside the same
transaction**; the summary (name, cumulative event count, distinct action
count) rides along with the action polling snapshot and stays queryable after
the session ends.  Events executed afterwards carry no session id and no
longer count towards the ended round.
"""
from __future__ import annotations

from typing import Any

import psycopg

from . import db
from .leases import LeaseError

MAX_NAME_LEN = 64

# Business error codes (surfaced as detail.code in the HTTP error envelope):
#   invalid_name      400 — blank or over-long session name
#   session_active    409 — start requested while a session is already active
#   no_active_session 409 — end requested with no active session
#   invalid_request   400 — unknown transition operation


def _active_row(conn: psycopg.Connection):
    return conn.execute(
        "SELECT id FROM rehearsal_sessions"
        " WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


_SUMMARY_SELECT = """
    SELECT s.id, s.name, s.started_at, s.ended_at,
           (SELECT count(*) FROM action_events e
             WHERE e.session_id = s.id) AS event_count,
           (SELECT count(DISTINCT e.action_id) FROM action_events e
             WHERE e.session_id = s.id) AS action_count
      FROM rehearsal_sessions s
"""


def _summary_from_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "status": "active" if row["ended_at"] is None else "ended",
        "started_at": row["started_at"].isoformat(),
        "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
        "event_count": row["event_count"],
        "action_count": row["action_count"],
    }


def _summary_for(conn: psycopg.Connection, session_id: int) -> dict[str, Any]:
    row = conn.execute(
        _SUMMARY_SELECT + " WHERE s.id = %s",
        (session_id,),
    ).fetchone()
    return _summary_from_row(row)


def current_summary() -> dict[str, Any] | None:
    """Summary of the active session, else the most recently ended one.

    Counts are derived from the events tagged with the session id, so an
    ended session's summary is frozen: later executions are tagged with the
    NEXT active session (or none) and never change these numbers.
    """
    with db.get_pool().connection() as conn:
        row = conn.execute(
            _SUMMARY_SELECT
            + " ORDER BY (s.ended_at IS NULL) DESC, s.id DESC LIMIT 1"
        ).fetchone()
        return _summary_from_row(row)


def transition(op: str, name: str | None = None) -> dict[str, Any]:
    """The one session state-transition entry point: start or end the round.

    Runs in a single transaction; every rejection is a recognisable business
    error that writes nothing and touches no event.
    """
    op = (op or "").strip().lower()
    if op not in ("start", "end"):
        raise LeaseError(
            "invalid_request", "未知的场次操作（仅支持 start / end）", 400
        )
    pool = db.get_pool()
    with pool.connection() as conn:
        try:
            with conn.transaction():
                if op == "start":
                    return _start(conn, name)
                return _end(conn)
        except psycopg.errors.UniqueViolation:
            # Two concurrent starts raced past the active check; the partial
            # unique index admitted exactly one of them.
            raise LeaseError(
                "session_active", "已有进行中的场次，请先结束当前场次", 409
            )


def _start(conn: psycopg.Connection, name: str | None) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise LeaseError("invalid_name", "场次名称不能为空", 400)
    if len(name) > MAX_NAME_LEN:
        raise LeaseError(
            "invalid_name", f"场次名称过长（最多 {MAX_NAME_LEN} 字符）", 400
        )
    if _active_row(conn) is not None:
        raise LeaseError(
            "session_active", "已有进行中的场次，请先结束当前场次", 409
        )
    now = db.server_now(conn)
    row = conn.execute(
        "INSERT INTO rehearsal_sessions (name, started_at)"
        " VALUES (%s, %s) RETURNING id",
        (name, now),
    ).fetchone()
    return _summary_for(conn, row["id"])


def _end(conn: psycopg.Connection) -> dict[str, Any]:
    active = _active_row(conn)
    if active is None:
        raise LeaseError("no_active_session", "当前没有进行中的场次", 409)
    now = db.server_now(conn)
    # The AND ended_at IS NULL guard makes a concurrent second end a no-row
    # update (it blocks on the row lock, then matches nothing) instead of
    # silently re-stamping the finish time.
    row = conn.execute(
        "UPDATE rehearsal_sessions SET ended_at = %s"
        " WHERE id = %s AND ended_at IS NULL RETURNING id",
        (now, active["id"]),
    ).fetchone()
    if row is None:
        raise LeaseError("no_active_session", "当前没有进行中的场次", 409)
    return _summary_for(conn, active["id"])
