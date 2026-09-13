"""Execution history (执行历史): a read-only, cursor-paginated review trail.

During the post-show review the rehearsal lead walks the executed actions IN
THE ORDER THEY HAPPENED, not just each card's latest result.  This module
serves exactly that: one page of `action_events` at a time, newest first,
keyset-paginated by the immutable event id.

  * Ordering / cursor: events come back `ORDER BY id DESC`; the response
    carries `next_cursor` — the last event id of the page — and the caller
    passes it back as `?cursor=` to fetch the page of strictly older events
    (`WHERE id < cursor`).  Because the cursor is the immutable event id
    itself (not an offset), events executed WHILE the reviewer is paging
    only ever appear above the first page already shown: they can never
    shift a later page, so no old record is repeated or skipped.
  * One query per page: the session (场次), the link id (联动标识) and the
    anomaly record (异常留痕) are joined in the SAME SELECT — no per-row
    follow-up lookups.
  * Read-only: nothing here ever writes; the lease/session/anomaly mutation
    paths are untouched.
  * An unparsable cursor is a recognisable business error
    (400 history_cursor_invalid), never a silent first page.
"""
from __future__ import annotations

from typing import Any

from . import db
from .leases import LeaseError

# Default and upper bound for the page size; the console asks for
# DEFAULT_PAGE_SIZE and the server never returns more than MAX_PAGE_SIZE.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

# Event ids are bigint identity values; a cursor outside their range can
# never have come from a next_cursor response.
_MAX_EVENT_ID = 2**63 - 1

# Business error code (surfaced as detail.code in the HTTP error envelope):
#   history_cursor_invalid 400 — the cursor is not a positive event id


def parse_cursor(raw: str | None) -> int | None:
    """Validate the opaque cursor: it must be a positive event id.

    The cursor is the immutable `action_events.id` of the last row of the
    previous page, handed back verbatim as `next_cursor`.  Anything else
    (blank, non-numeric, zero/negative) is rejected so the caller can keep
    showing what it already loaded instead of silently restarting.
    """
    if raw is None:
        return None
    value = raw.strip()
    # Digits only, in the positive bigint range: anything else (blank,
    # signed, decimal, overflowing) can never be an event id.
    if not value.isdigit() or not 0 < int(value) <= _MAX_EVENT_ID:
        raise LeaseError(
            "history_cursor_invalid",
            "历史游标无效：请使用上一页响应返回的下一页游标（事件编号）",
            400,
        )
    return int(value)


def clamp_limit(limit: int | None) -> int:
    if limit is None or limit <= 0:
        return DEFAULT_PAGE_SIZE
    return min(limit, MAX_PAGE_SIZE)


# One SELECT per page: events joined with their action label, the rehearsal
# session they were attributed to, and the anomaly record (at most one per
# event) — no N+1 lookups while rendering a page.
_HISTORY_SELECT = """
    SELECT e.id AS event_id, e.action_id, a.label AS action_label,
           e.holder, e.result, e.link_id, e.occurred_at,
           s.id AS session_id, s.name AS session_name,
           an.id AS anomaly_id, an.event_id AS anomaly_event_id,
           an.category AS anomaly_category,
           an.description AS anomaly_description,
           an.reported_by AS anomaly_reported_by,
           an.reporter_id AS anomaly_reporter_id,
           an.reported_at AS anomaly_reported_at,
           an.status AS anomaly_status,
           an.confirmed_by AS anomaly_confirmed_by,
           an.confirmer_id AS anomaly_confirmer_id,
           an.confirmed_at AS anomaly_confirmed_at
      FROM action_events e
      JOIN actions a ON a.id = e.action_id
      LEFT JOIN rehearsal_sessions s ON s.id = e.session_id
      LEFT JOIN action_anomalies an ON an.event_id = e.id
"""


def list_history(
    cursor_raw: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """One page of execution history, newest event first.

    `cursor_raw` is the previous page's `next_cursor` (None for the latest
    page).  The page size is clamped to [1, MAX_PAGE_SIZE].  The response's
    `next_cursor` is the id of the page's last event when strictly older
    events exist, else None — the caller stops paging then.
    """
    cursor = parse_cursor(cursor_raw)
    page_size = clamp_limit(limit)
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            _HISTORY_SELECT
            + " WHERE (%s::bigint IS NULL OR e.id < %s)"
              " ORDER BY e.id DESC LIMIT %s",
            (cursor, cursor, page_size + 1),  # one extra row: is there more?
        ).fetchall()
    has_more = len(rows) > page_size
    page = rows[:page_size]
    return {
        "events": [_event_from_row(r) for r in page],
        "next_cursor": page[-1]["event_id"] if has_more and page else None,
    }


def _event_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "action_id": row["action_id"],
        "label": row["action_label"],
        "holder": row["holder"],
        "result": row["result"],
        # Shared by the two events of one linked run; NULL for single
        # executions. The UI groups adjacent rows carrying the same id.
        "link_id": row["link_id"],
        "occurred_at": row["occurred_at"].isoformat(),
        # The rehearsal round this event counts towards (NULL when executed
        # outside any session).
        "session": {
            "id": row["session_id"],
            "name": row["session_name"],
        }
        if row["session_id"] is not None
        else None,
        # The anomaly reported ON THIS EVENT, with its confirmation result.
        "anomaly": db.anomaly_from_prefixed_row(row),
    }
