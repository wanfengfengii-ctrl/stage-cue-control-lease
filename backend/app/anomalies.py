"""On-site anomaly records (现场异常) attached to executed action events.

During a rehearsal review the control seat reports the on-site anomaly of an
already-executed action ON THAT EVENT, so the next shift can confirm they
have actually seen it — replacing untraceable verbal handovers.

  report（报告）: the latest action_events row of the action gets ONE
    action_anomalies row in status 'pending' (待确认); the report carries a
    category, a description, the reporting seat and a server-side UTC time.

  confirm（确认）: a DIFFERENT browser console moves the record — and only
    such a record — from 'pending' to 'confirmed' (已确认); the reporting
    console can never confirm its own report, even after the operator edits
    the seat NAME, because the check compares the consoles' stable ids
    (409 anomaly_self_confirm), so the acknowledgement can only come from
    the next shift's console. The server stamps the confirming seat name,
    console id and time. The transition is conditional (UPDATE ... WHERE
    status='pending'), so even racing confirms admit exactly one winner.

Both operations row-lock the action first (the same FOR UPDATE lock that
execute/execute-linked take), serialising against new executions: a report
either lands on the event that was latest before the lock or on the new one,
never in between.  A repeat report or a repeat confirmation is a recognisable
business error that carries the CURRENT record back and changes no data.
Records are append-only — re-acquiring, releasing, single and linked
execution never overwrite old rows.
"""
from __future__ import annotations

from typing import Any

import psycopg

from . import db

MAX_DESCRIPTION_LEN = 500

# Fixed anomaly categories the console offers; the stored value is the code,
# the UI maps it to the Chinese label.
CATEGORIES: dict[str, str] = {
    "equipment": "设备异常",
    "operation": "操作异常",
    "environment": "环境异常",
    "other": "其他异常",
}

# Business error codes (surfaced as detail.code in the HTTP error envelope):
#   invalid_anomaly   400 — blank/unknown category or blank description/seat
#   no_execution_event 409 — no executed event exists to report on yet
#   anomaly_exists    409 — repeat report on the same event
#   anomaly_not_found 409 — confirm requested with no pending record on the event
#   anomaly_self_confirm 409 — the reporting CONSOLE tries to confirm its own
#                        report (matched by stable id, not the editable name);
#                        only another console (the next shift) may confirm
#   anomaly_confirmed 409 — repeat confirmation; the record is already confirmed


class AnomalyError(Exception):
    """Logical rejection; carries the current record when one exists."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 409,
        anomaly: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        # Repeat report/confirm: the current record rides along so the
        # console can immediately re-render the authoritative state.
        self.anomaly = anomaly


def _seat(value: str | None, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise AnomalyError("invalid_anomaly", f"{label}不能为空", 400)
    if len(value) > 64:
        raise AnomalyError(
            "invalid_anomaly", f"{label}过长（最多 64 字符）", 400
        )
    return value


def _console_id(value: str | None) -> str:
    """Validate the browser console's stable identity (not the seat name)."""
    value = (value or "").strip()
    if not value:
        raise AnomalyError(
            "invalid_anomaly", "缺少控制台身份标识，无法判定是否同一席", 400
        )
    if len(value) > 128:
        raise AnomalyError("invalid_anomaly", "控制台身份标识过长", 400)
    return value


def _latest_event_id(conn: psycopg.Connection, action_id: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM action_events WHERE action_id = %s"
        " ORDER BY id DESC LIMIT 1",
        (action_id,),
    ).fetchone()
    return row["id"] if row else None


def _fetch_for_event(conn: psycopg.Connection, event_id: int):
    return conn.execute(
        "SELECT * FROM action_anomalies WHERE event_id = %s",
        (event_id,),
    ).fetchone()


def report(
    action_id: str,
    category: str,
    description: str,
    reporter: str,
    reporter_id: str,
) -> dict[str, Any]:
    """Create the single pending anomaly record for the action's last event.

    A repeat report on the same event is rejected with anomaly_exists and the
    existing record, leaving the first report's data untouched.
    """
    category = (category or "").strip()
    if category not in CATEGORIES:
        raise AnomalyError(
            "invalid_anomaly",
            "异常类别无效（仅支持 equipment/operation/environment/other）",
            400,
        )
    description = (description or "").strip()
    if not description:
        raise AnomalyError("invalid_anomaly", "异常说明不能为空", 400)
    if len(description) > MAX_DESCRIPTION_LEN:
        raise AnomalyError(
            "invalid_anomaly",
            f"异常说明过长（最多 {MAX_DESCRIPTION_LEN} 字符）",
            400,
        )
    reporter = _seat(reporter, "报告席位")
    reporter_id = _console_id(reporter_id)

    pool = db.get_pool()
    with pool.connection() as conn:
        unique_violation = False
        try:
            with conn.transaction():
                if db.lock_action(conn, action_id) is None:
                    raise AnomalyError("unknown_action", "未知动作", 404)
                now = db.server_now(conn)
                event_id = _latest_event_id(conn, action_id)
                if event_id is None:
                    # The card only offers the entry after an execution; this
                    # guards the API directly.
                    raise AnomalyError(
                        "no_execution_event",
                        "该动作尚无已执行事件，无法报告异常",
                        409,
                    )
                existing = _fetch_for_event(conn, event_id)
                if existing is not None:
                    raise AnomalyError(
                        "anomaly_exists",
                        "该执行事件已存在异常记录，请勿重复报告",
                        409,
                        anomaly=_anomaly_from_row(existing),
                    )
                row = conn.execute(
                    """
                    INSERT INTO action_anomalies
                        (action_id, event_id, category, description,
                         reported_by, reporter_id, reported_at, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                    RETURNING *
                    """,
                    (
                        action_id,
                        event_id,
                        category,
                        description,
                        reporter,
                        reporter_id,
                        now,
                    ),
                ).fetchone()
                state = db.state_for(conn, action_id, now)
        except psycopg.errors.UniqueViolation:
            # Concurrent reports raced past the SELECT (defensive: the action
            # row lock normally serialises them): the unique index admitted
            # exactly one row. The failed transaction has rolled back by the
            # time we leave its context, so the winner can be read now.
            unique_violation = True
        if unique_violation:
            existing = _fetch_for_event(conn, event_id)
            raise AnomalyError(
                "anomaly_exists",
                "该执行事件已存在异常记录，请勿重复报告",
                409,
                anomaly=_anomaly_from_row(existing) if existing else None,
            )
    return {"anomaly": _anomaly_from_row(row), "state": state}


def confirm(action_id: str, confirmer: str, confirmer_id: str) -> dict[str, Any]:
    """Move the latest event's anomaly pending -> confirmed, exactly once.

    The self-confirmation check compares STABLE CONSOLE IDS, not the freely
    editable seat name: renaming the reporting console to "next shift" must
    not let it acknowledge its own report. Two different consoles happen to
    share a name are still two distinct seats (distinct ids) and may confirm.

    Anything but a pending record on the card's current event is a business
    error carrying the current record; the conditional UPDATE makes a
    duplicate confirmation a no-row match instead of re-stamping.
    """
    confirmer = _seat(confirmer, "确认席位")
    confirmer_id = _console_id(confirmer_id)

    pool = db.get_pool()
    with pool.connection() as conn:
        with conn.transaction():
            if db.lock_action(conn, action_id) is None:
                raise AnomalyError("unknown_action", "未知动作", 404)
            now = db.server_now(conn)
            event_id = _latest_event_id(conn, action_id)
            if event_id is None:
                raise AnomalyError(
                    "anomaly_not_found", "该动作尚无异常记录可供确认", 409
                )
            record = _fetch_for_event(conn, event_id)
            if record is None:
                # A newer event may have arrived (its anomaly lives on the new
                # event); an older event's record is never confirmed here.
                raise AnomalyError(
                    "anomaly_not_found",
                    "当前执行事件没有待确认的异常记录",
                    409,
                )
            # Only ANOTHER browser console — the next shift's — may
            # acknowledge the report. The check uses the stable console id,
            # NOT the editable seat name: renaming this page cannot turn the
            # reporter into the confirmer.
            if record["reporter_id"] == confirmer_id:
                raise AnomalyError(
                    "anomaly_self_confirm",
                    "报告席位不能自行确认，请由下一班（另一席）确认已看到",
                    409,
                    anomaly=_anomaly_from_row(record),
                )
            if record["status"] != "pending":
                raise AnomalyError(
                    "anomaly_confirmed",
                    "该异常记录已确认，请勿重复确认",
                    409,
                    anomaly=_anomaly_from_row(record),
                )
            row = conn.execute(
                """
                UPDATE action_anomalies
                   SET status = 'confirmed',
                       confirmed_by = %s,
                       confirmer_id = %s,
                       confirmed_at = %s
                 WHERE id = %s AND status = 'pending'
                RETURNING *
                """,
                (confirmer, confirmer_id, now, record["id"]),
            ).fetchone()
            if row is None:
                # Lost a race against another confirming seat: report the
                # winner's record instead of rewriting it.
                current = _fetch_for_event(conn, event_id)
                raise AnomalyError(
                    "anomaly_confirmed",
                    "该异常记录已确认，请勿重复确认",
                    409,
                    anomaly=_anomaly_from_row(current) if current else None,
                )
            state = db.state_for(conn, action_id, now)
    return {"anomaly": _anomaly_from_row(row), "state": state}


def _anomaly_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "category": row["category"],
        "description": row["description"],
        "reported_by": row["reported_by"],
        "reporter_id": row["reporter_id"],
        "reported_at": row["reported_at"].isoformat(),
        "status": row["status"],
        "confirmed_by": row["confirmed_by"],
        "confirmer_id": row["confirmer_id"],
        "confirmed_at": row["confirmed_at"].isoformat()
        if row["confirmed_at"]
        else None,
    }
