"""Lease lifecycle: acquire / renew / release / execute.

Every operation runs in ONE database transaction:

  1. LOCK the action row with SELECT ... FOR UPDATE.  Concurrent operations on
     the same action are therefore serialised by PostgreSQL itself — two seats
     racing for one action can never both observe "free".
  2. Read the most recent lease row.
  3. Decide validity against the transaction's server-side now() clock.
  4. Mutate and commit, or roll back.

Tokens are 256-bit random URL-safe strings; only their SHA-256 hash is stored,
so a database read cannot reconstruct a usable token.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Any

import psycopg

from . import config
from . import db

TOKEN_BYTES = 32  # 256 bits


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def _lock_action(conn: psycopg.Connection, action_id: str):
    """Row-lock the action; returns its id or None if it does not exist."""
    return conn.execute(
        "SELECT id FROM actions WHERE id = %s FOR UPDATE",
        (action_id,),
    ).fetchone()


def _latest_lease(conn: psycopg.Connection, action_id: str):
    return conn.execute(
        """
        SELECT * FROM leases
         WHERE action_id = %s
         ORDER BY id DESC
         LIMIT 1
        """,
        (action_id,),
    ).fetchone()


def _is_live(lease: dict[str, Any] | None, now) -> bool:
    return (
        lease is not None
        and lease["released_at"] is None
        and lease["executed_at"] is None
        and now < lease["expires_at"]  # now == expires_at => expired
    )


class LeaseError(Exception):
    """Logical rejection (not a database error)."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def acquire(action_id: str, holder: str) -> dict[str, Any]:
    """Grant a fresh 30 s lease iff no other live lease exists.

    A late/replayed request carrying an old token never gets this far:
    acquisition never looks at caller-supplied tokens.
    """
    holder = (holder or "").strip()
    if not holder:
        raise LeaseError("invalid_holder", "席位名称不能为空", 400)
    if len(holder) > 64:
        raise LeaseError("invalid_holder", "席位名称过长（最多 64 字符）", 400)

    token = _new_token()
    token_hash = hash_token(token)
    pool = db.get_pool()
    with pool.connection() as conn:
        try:
            with conn.transaction():
                if _lock_action(conn, action_id) is None:
                    raise LeaseError("unknown_action", "未知动作", 404)
                now = db.server_now(conn)
                latest = _latest_lease(conn, action_id)
                if _is_live(latest, now):
                    raise LeaseError(
                        "lease_held",
                        f"该动作已由 {latest['holder']} 持有",
                        409,
                    )
                expires = _add_seconds(now, config.LEASE_TTL_SECONDS)
                row = conn.execute(
                    """
                    INSERT INTO leases
                        (action_id, token_hash, holder, acquired_at, expires_at)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (action_id, token_hash, holder, now, expires),
                ).fetchone()
                state = db.state_for(conn, action_id, now)
        except LeaseError:
            raise
        payload = {
            "token": token,
            "holder": row["holder"],
            "acquired_at": row["acquired_at"].isoformat(),
            "expires_at": row["expires_at"].isoformat(),
            "ttl_seconds": config.LEASE_TTL_SECONDS,
        }
        payload["state"] = state
        return payload


def _authenticate_live(conn, action_id: str, token: str, now):
    """Lock action and return the latest lease iff it is live AND token matches."""
    if _lock_action(conn, action_id) is None:
        raise LeaseError("unknown_action", "未知动作", 404)
    latest = _latest_lease(conn, action_id)
    if latest is None or not _is_live(latest, now):
        # Expired / released / executed: a new seat may already hold a fresh
        # lease. The old token must not be able to touch it.
        raise LeaseError(
            "control_lost",
            "控制权已失效：租约已到期、释放或执行",
            409,
        )
    if not secrets.compare_digest(latest["token_hash"], hash_token(token)):
        # A live lease exists but belongs to another (newer) token.
        raise LeaseError(
            "control_lost",
            "控制权已失效：该令牌已被新租约取代",
            409,
        )
    return latest


def renew(action_id: str, token: str) -> dict[str, Any]:
    pool = db.get_pool()
    with pool.connection() as conn:
        with conn.transaction():
            now = db.server_now(conn)
            lease = _authenticate_live(conn, action_id, token, now)
            new_expiry = _add_seconds(now, config.LEASE_TTL_SECONDS)
            row = conn.execute(
                "UPDATE leases SET expires_at = %s WHERE id = %s RETURNING *",
                (new_expiry, lease["id"]),
            ).fetchone()
            state = db.state_for(conn, action_id, now)
    return {
        "expires_at": row["expires_at"].isoformat(),
        "ttl_seconds": config.LEASE_TTL_SECONDS,
        "state": state,
    }


def release(action_id: str, token: str) -> dict[str, Any]:
    pool = db.get_pool()
    with pool.connection() as conn:
        with conn.transaction():
            now = db.server_now(conn)
            lease = _authenticate_live(conn, action_id, token, now)
            conn.execute(
                "UPDATE leases SET released_at = %s WHERE id = %s",
                (now, lease["id"]),
            )
            state = db.state_for(conn, action_id, now)
    return {"released": True, "state": state}


def execute(action_id: str, token: str) -> dict[str, Any]:
    """Execute exactly once: mark lease executed and insert one action event."""
    pool = db.get_pool()
    with pool.connection() as conn:
        try:
            with conn.transaction():
                now = db.server_now(conn)
                lease = _authenticate_live(conn, action_id, token, now)
                conn.execute(
                    "UPDATE leases SET executed_at = %s WHERE id = %s",
                    (now, lease["id"]),
                )
                # Unique index on lease_id makes double-write impossible even
                # at the SQL level.
                event = conn.execute(
                    """
                    INSERT INTO action_events
                        (action_id, lease_id, token_hash, holder, result)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id, occurred_at
                    """,
                    (
                        action_id,
                        lease["id"],
                        lease["token_hash"],
                        lease["holder"],
                        "executed",
                    ),
                ).fetchone()
                state = db.state_for(conn, action_id, now)
        except psycopg.errors.UniqueViolation:
            # Defensive: an event already exists for this lease.
            raise LeaseError(
                "already_executed", "该租约已执行过，动作事件不得重复写入", 409
            )
    return {
        "executed": True,
        "event_id": event["id"],
        "executed_by": lease["holder"],
        "occurred_at": event["occurred_at"].isoformat(),
        "state": state,
    }


def get_action_state(action_id: str) -> dict[str, Any]:
    with db.get_pool().connection() as conn:
        return db.fetch_action_state(conn, action_id)


def list_states() -> list[dict[str, Any]]:
    with db.get_pool().connection() as conn:
        return db.fetch_all_states(conn)


def _add_seconds(value, seconds: int):
    from datetime import timedelta

    return value + timedelta(seconds=seconds)
