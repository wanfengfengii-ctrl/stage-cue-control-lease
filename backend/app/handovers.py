"""Shift handovers (换班交接): transfer a LIVE lease to the next console.

The current holder initiates the handover while its lease is still valid;
the system generates a ONE-TIME handover code (only its SHA-256 hash is
stored, the raw code is returned to the initiating console exactly once).
Until the receiving console redeems it, the original seat keeps every
right: it can still renew, release or execute — initiation never touches
the lease.

  initiate（发起）: authenticated by the current lease token, exactly like
    renew/release/execute.  Any previous pending record of the action is
    invalidated in the same transaction (a lost code can always be
    replaced by a fresh one), then one 'pending' row is inserted.

  accept（接收）: the receiving console submits the code on the same
    action card.  ONE transaction locks the action row (the same
    FOR UPDATE lock execute/renew take), then validates the initiating
    lease, the handover status and the lease's validity window:

      * no pending record            -> 409 handover_not_found
      * initiating lease released,
        executed or superseded       -> 409 handover_invalid
      * lease expired at accept time -> 409 handover_expired
      * code mismatch                -> 409 handover_code_invalid
      * the initiating console tries
        to receive its own code      -> 409 handover_self_accept

    Only then does the transaction terminate the old lease, insert the
    replacement lease whose token is returned ONLY to the receiving
    console, and stamp the record accepted — all or nothing.  Every
    failure rolls back: no new lease is ever created on a failed accept.

A record's status is pending（待接收）/ accepted（已接收）/ invalidated
（已失效）.  When the initiating lease is released, executed, expires or
is superseded, a still-pending record is judged invalidated AT QUERY TIME
(see db.handover_from_prefixed_row), and initiation cleans such stale
rows up inside its own transaction, so a dead record never blocks a fresh
handover or a normal lease application.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Any

from . import config
from . import db
from .leases import (
    LeaseError,
    _add_seconds,
    _is_live,
    _latest_lease,
    _new_token,
    hash_token,
)

# Unambiguous alphabet (no 0/O, 1/I/L): 8 chars ~= 40 bits, unguessable
# within the lease's short lifetime yet easy to read aloud across consoles.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8

# Business error codes (surfaced as detail.code in the HTTP error envelope):
#   invalid_handover      400 — blank recipient seat / console identity
#   handover_not_found    409 — no pending handover on this action
#   handover_code_invalid 409 — the submitted code does not match
#   handover_invalid      409 — initiating lease released/executed/superseded
#   handover_expired      409 — the lease expired exactly at accept time
#   handover_self_accept  409 — the initiating console submits its own code
# Initiation reuses the lease errors: 401 missing_token, 409 control_lost,
# 404 unknown_action.


class HandoverError(Exception):
    """Logical rejection; carries the current record when one exists."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 409,
        handover: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        # The current record rides along so the console can re-render the
        # authoritative state (and prompt a fresh initiation) at once.
        self.handover = handover


def _new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def normalize_code(raw: str | None) -> str:
    """Canonical form of a typed-in code: case/space/hyphen insensitive."""
    return (
        (raw or "")
        .upper()
        .replace("-", "")
        .replace(" ", "")
        .strip()
    )


def _console_id(value: str | None, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise HandoverError(
            "invalid_handover", f"缺少{label}控制台身份标识", 400
        )
    if len(value) > 128:
        raise HandoverError("invalid_handover", "控制台身份标识过长", 400)
    return value


def _seat(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        raise HandoverError("invalid_handover", "接班席位名称不能为空", 400)
    if len(value) > 64:
        raise HandoverError(
            "invalid_handover", "接班席位名称过长（最多 64 字符）", 400
        )
    return value


def _pending_for(conn, action_id: str):
    return conn.execute(
        """
        SELECT * FROM lease_handovers
         WHERE action_id = %s AND status = 'pending'
         ORDER BY id DESC
         LIMIT 1
        """,
        (action_id,),
    ).fetchone()


def _invalidate_pending(conn, action_id: str) -> None:
    """Drop every pending record of the action (same transaction).

    Used by initiation: a fresh code replaces any previous one, and stale
    rows whose lease already died are cleaned up at the same time, so the
    per-action partial unique index always admits the new record.
    """
    conn.execute(
        "UPDATE lease_handovers SET status = 'invalidated'"
        " WHERE action_id = %s AND status = 'pending'",
        (action_id,),
    )


def initiate(
    action_id: str, token: str, initiator_id: str
) -> dict[str, Any]:
    """Create the one-time handover code for the holder's live lease.

    The lease itself is NOT touched: until the code is redeemed the
    original seat can still renew, release or execute.  Re-initiating
    invalidates the previous pending record and issues a fresh code.
    """
    initiator_id = _console_id(initiator_id, "发起席")
    if not token:
        raise LeaseError("missing_token", "缺少令牌", 401)

    code = _new_code()
    pool = db.get_pool()
    with pool.connection() as conn:
        with conn.transaction():
            if db.lock_action(conn, action_id) is None:
                raise LeaseError("unknown_action", "未知动作", 404)
            now = db.server_now(conn)
            # Same authentication as renew/release/execute: only the
            # current live token may open a handover.
            latest = _latest_lease(conn, action_id)
            if latest is None or not _is_live(latest, now):
                raise LeaseError(
                    "control_lost",
                    "控制权已失效：租约已到期、释放或执行",
                    409,
                    action_id=action_id,
                )
            if not secrets.compare_digest(
                latest["token_hash"], hash_token(token)
            ):
                raise LeaseError(
                    "control_lost",
                    "控制权已失效：该令牌已被新租约取代",
                    409,
                    action_id=action_id,
                )
            # A fresh code replaces any previous pending record (and stale
            # rows whose lease already died are cleaned up here too), so
            # the per-action partial unique index always admits the insert.
            _invalidate_pending(conn, action_id)
            row = conn.execute(
                """
                INSERT INTO lease_handovers
                    (action_id, lease_id, code_hash, initiator,
                     initiator_id, created_at, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                RETURNING *
                """,
                (
                    action_id,
                    latest["id"],
                    _hash_code(code),
                    latest["holder"],
                    initiator_id,
                    now,
                ),
            ).fetchone()
            state = db.state_for(conn, action_id, now)
    return {
        # The raw code is returned to the initiating console exactly ONCE;
        # only its hash is stored.
        "code": code,
        "handover": _handover_from_row(row),
        "state": state,
    }


def accept(
    action_id: str,
    code: str,
    recipient: str,
    recipient_id: str,
) -> dict[str, Any]:
    """Redeem the one-time code: terminate the old lease, issue a new one.

    Everything happens in ONE transaction behind the action row lock, so a
    concurrent renew/release/execute of the original seat serialises with
    the acceptance and exactly one side commits.  Any rejection rolls the
    transaction back: a failed accept never creates a lease.
    """
    recipient = _seat(recipient)
    recipient_id = _console_id(recipient_id, "接收席")
    code = normalize_code(code)

    pool = db.get_pool()
    with pool.connection() as conn:
        with conn.transaction():
            if db.lock_action(conn, action_id) is None:
                raise LeaseError("unknown_action", "未知动作", 404)
            now = db.server_now(conn)
            pending = _pending_for(conn, action_id)
            if pending is None:
                raise HandoverError(
                    "handover_not_found",
                    "当前没有待接收的交接：请确认发起席已生成交接码",
                    409,
                )
            # Validate the initiating lease BEFORE the code: a dead lease
            # kills the handover no matter what was typed.  The record is
            # judged invalidated at query time from here on; the failed
            # transaction itself writes nothing.
            latest = _latest_lease(conn, action_id)
            lease_live = (
                latest is not None
                and latest["id"] == pending["lease_id"]
                and _is_live(latest, now)
            )
            if not lease_live:
                dead = (
                    latest
                    if latest is not None
                    and latest["id"] == pending["lease_id"]
                    else None
                )
                invalidated = _handover_from_row(pending, "invalidated")
                if dead is None:
                    # A newer lease already sits on the action.
                    raise HandoverError(
                        "handover_invalid",
                        "交接已失效：发起租约已被新租约取代，请重新发起交接",
                        409,
                        handover=invalidated,
                    )
                if dead["released_at"] is not None:
                    raise HandoverError(
                        "handover_invalid",
                        "交接已失效：发起席已释放控制权，请重新发起交接",
                        409,
                        handover=invalidated,
                    )
                if dead["executed_at"] is not None:
                    raise HandoverError(
                        "handover_invalid",
                        "交接已失效：发起席已执行该动作，请重新发起交接",
                        409,
                        handover=invalidated,
                    )
                # Unfinished but not live => the lease expired exactly
                # while the code was being typed (now == expires_at is
                # already invalid).
                raise HandoverError(
                    "handover_expired",
                    "交接已失效：接收时租约刚好到期，请重新申请控制权",
                    409,
                    handover=invalidated,
                )
            if not secrets.compare_digest(
                pending["code_hash"], _hash_code(code)
            ):
                # A typo must not kill the handover: the record stays
                # pending and no lease is created.
                raise HandoverError(
                    "handover_code_invalid",
                    "交接码错误，请核对后重试",
                    409,
                    handover=_handover_from_row(pending),
                )
            # The receiving console must be a DIFFERENT browser console
            # than the initiator's — the shift changes hands, not tabs.
            if pending["initiator_id"] == recipient_id:
                raise HandoverError(
                    "handover_self_accept",
                    "发起席不能接收自己的交接码，请由接班席（另一控制台）接收",
                    409,
                    handover=_handover_from_row(pending),
                )
            # All checks passed: terminate the initiating lease and issue
            # the replacement lease to the receiving console — atomically.
            conn.execute(
                "UPDATE leases SET released_at = %s WHERE id = %s",
                (now, pending["lease_id"]),
            )
            new_token = _new_token()
            new_lease = conn.execute(
                """
                INSERT INTO leases
                    (action_id, token_hash, holder, acquired_at, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    action_id,
                    hash_token(new_token),
                    recipient,
                    now,
                    _add_seconds(now, config.LEASE_TTL_SECONDS),
                ),
            ).fetchone()
            row = conn.execute(
                """
                UPDATE lease_handovers
                   SET status = 'accepted',
                       accepted_by = %s,
                       accepted_id = %s,
                       accepted_at = %s,
                       new_lease_id = %s
                 WHERE id = %s AND status = 'pending'
                RETURNING *
                """,
                (
                    recipient,
                    recipient_id,
                    now,
                    new_lease["id"],
                    pending["id"],
                ),
            ).fetchone()
            state = db.state_for(conn, action_id, now)
    return {
        # The new token is returned ONLY to the receiving console.
        "token": new_token,
        "holder": recipient,
        "expires_at": new_lease["expires_at"].isoformat(),
        "ttl_seconds": config.LEASE_TTL_SECONDS,
        "handover": _handover_from_row(row),
        "state": state,
    }


def _handover_from_row(
    row: dict[str, Any], status_override: str | None = None
) -> dict[str, Any]:
    return {
        "id": row["id"],
        "lease_id": row["lease_id"],
        "initiator": row["initiator"],
        "initiator_id": row["initiator_id"],
        "created_at": row["created_at"].isoformat(),
        "status": status_override or row["status"],
        "accepted_by": row["accepted_by"],
        "accepted_id": row["accepted_id"],
        "accepted_at": row["accepted_at"].isoformat()
        if row["accepted_at"]
        else None,
        "new_lease_id": row["new_lease_id"],
    }
