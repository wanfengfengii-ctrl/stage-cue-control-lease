"""Lease lifecycle: acquire / renew / release / execute / linked execute.

Every operation runs in ONE database transaction:

  1. LOCK the action row with SELECT ... FOR UPDATE.  Concurrent operations on
     the same action are therefore serialised by PostgreSQL itself — two seats
     racing for one action can never both observe "free".  Linked execution
     locks its action rows in sorted action-id order, so a linked run and a
     single-action execute (or two linked runs) never deadlock and exactly
     one side commits.
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


def _new_link_id() -> str:
    """Server-generated identifier shared by the events of one linked run."""
    return secrets.token_urlsafe(16)


def _lock_action(conn: psycopg.Connection, action_id: str):
    """Row-lock the action; returns its id or None if it does not exist."""
    return db.lock_action(conn, action_id)


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

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 409,
        action_id: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        # For linked execution: which action's token failed, so the caller
        # (and the UI) can point at the exact card that must re-acquire.
        self.action_id = action_id


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
        raise LeaseError("unknown_action", "未知动作", 404, action_id=action_id)
    latest = _latest_lease(conn, action_id)
    if latest is None or not _is_live(latest, now):
        # Expired / released / executed: a new seat may already hold a fresh
        # lease. The old token must not be able to touch it.
        raise LeaseError(
            "control_lost",
            "控制权已失效：租约已到期、释放或执行",
            409,
            action_id=action_id,
        )
    if not secrets.compare_digest(latest["token_hash"], hash_token(token)):
        # A live lease exists but belongs to another (newer) token.
        raise LeaseError(
            "control_lost",
            "控制权已失效：该令牌已被新租约取代",
            409,
            action_id=action_id,
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
                # Attribute the event to the active rehearsal session (if
                # any) inside the same transaction.
                session_id = db.active_session_id(conn)
                # Unique index on lease_id makes double-write impossible even
                # at the SQL level.
                event = conn.execute(
                    """
                    INSERT INTO action_events
                        (action_id, lease_id, token_hash, holder, result,
                         session_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, occurred_at
                    """,
                    (
                        action_id,
                        lease["id"],
                        lease["token_hash"],
                        lease["holder"],
                        "executed",
                        session_id,
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


def execute_linked(items: list[dict[str, str]]) -> dict[str, Any]:
    """Execute two held cross-device actions as ONE atomic linked operation.

    Exactly two actions participate — one lifting-platform action and one
    flying-hoist action; same-device direction pairs and any other count are
    rejected with 400 before any lock is taken.

    Either both actions commit (lease terminated + one event each, all
    sharing a server-generated link id) or — if any token is missing,
    expired, or superseded — the transaction rolls back: no event is
    written and no other lease is touched.

    Action rows are locked in sorted action-id order regardless of the
    request's item order, so concurrent linked executions (or a linked
    execution racing a single-action execute) serialise on the same row
    locks without deadlocking.
    """
    parsed = [
        {"action_id": (it.get("action_id") or "").strip(),
         "token": it.get("token") or ""}
        for it in items
    ]
    # A linked run is EXACTLY one lifting-platform action + one flying-hoist
    # action: the two devices move as a single coordinated cue.
    if len(parsed) != 2:
        raise LeaseError(
            "invalid_request", "联动执行必须恰好包含两个动作", 400
        )
    action_ids = [it["action_id"] for it in parsed]
    if len(set(action_ids)) != len(action_ids):
        raise LeaseError(
            "invalid_request", "联动动作不得重复", 400
        )
    tokens = {it["action_id"]: it["token"] for it in parsed}
    for aid in action_ids:
        if aid not in config.ACTION_IDS:
            raise LeaseError("unknown_action", "未知动作", 404, action_id=aid)
    devices = {aid: config.DEVICE_OF.get(aid) for aid in action_ids}
    if any(dev is None for dev in devices.values()) or frozenset(
        devices.values()
    ) not in config.LINKABLE_DEVICE_PAIRS:
        raise LeaseError(
            "invalid_request",
            "联动执行只允许升降台与飞行吊点的跨设备组合，"
            "同一设备的两个方向（如上升与下降）不得联动",
            400,
        )
    for it in parsed:
        if not it["token"]:
            raise LeaseError(
                "missing_token",
                "缺少令牌：联动执行要求每个动作都携带有效令牌",
                401,
                action_id=it["action_id"],
            )

    # Fixed lock order: sorted by action id, independent of request order.
    ordered = sorted(action_ids)
    link_id = _new_link_id()
    pool = db.get_pool()
    with pool.connection() as conn:
        try:
            with conn.transaction():
                now = db.server_now(conn)
                held = {}
                for aid in ordered:
                    # Locks the action row (FOR UPDATE) and validates the
                    # token against the live lease. The first failure raises
                    # and rolls back the whole transaction.
                    held[aid] = _authenticate_live(conn, aid, tokens[aid], now)
                events = []
                # Both events of the linked run belong to the rehearsal
                # session active right now (same transaction), if any.
                session_id = db.active_session_id(conn)
                for aid in ordered:
                    lease = held[aid]
                    conn.execute(
                        "UPDATE leases SET executed_at = %s WHERE id = %s",
                        (now, lease["id"]),
                    )
                    event = conn.execute(
                        """
                        INSERT INTO action_events
                            (action_id, lease_id, token_hash, holder, result,
                             link_id, session_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING id, occurred_at
                        """,
                        (
                            aid,
                            lease["id"],
                            lease["token_hash"],
                            lease["holder"],
                            "executed",
                            link_id,
                            session_id,
                        ),
                    ).fetchone()
                    events.append(
                        {
                            "action_id": aid,
                            "event_id": event["id"],
                            "executed_by": lease["holder"],
                            "occurred_at": event["occurred_at"].isoformat(),
                        }
                    )
                states = {aid: db.state_for(conn, aid, now) for aid in ordered}
        except psycopg.errors.UniqueViolation:
            # Defensive: an event already exists for one of these leases.
            raise LeaseError(
                "already_executed", "该租约已执行过，动作事件不得重复写入", 409
            )
    return {
        "linked": True,
        "link_id": link_id,
        "events": events,
        "states": states,
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
