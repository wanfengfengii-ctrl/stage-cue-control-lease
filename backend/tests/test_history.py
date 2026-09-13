"""Execution history (执行历史): read-only cursor-paginated review trail.

Covers the acceptance points:
  * events come back newest-first (descending immutable event id) with
    execution time, action, seat and the session they were attributed to;
  * the two events of one linked run share the link id and stay ADJACENT in
    the history order, so the UI can group them;
  * the anomaly reported on an event — with its confirmation result — rides
    along with that event's row;
  * keyset pagination by immutable event id: executions committed WHILE the
    reviewer pages through history never repeat or skip older records;
  * an unparsable cursor is a recognisable 400 history_cursor_invalid;
  * the endpoint is read-only and every existing interface keeps working.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import anomalies, history, sessions
from app.main import app

LIFT = "lift_up"
HOIST = "hoist_fly_in"
EXTRA = "emergency_stop"

CONSOLE_A = "console-aaaa-1111"
CONSOLE_B = "console-bbbb-2222"


def _execute(services, action_id: str, holder="联排控制席"):
    grant = services.acquire(action_id, holder)
    return services.execute(action_id, grant["token"])


def _linked(services, holder="联排控制席"):
    g_lift = services.acquire(LIFT, holder)
    g_hoist = services.acquire(HOIST, holder)
    return services.execute_linked(
        [
            {"action_id": LIFT, "token": g_lift["token"]},
            {"action_id": HOIST, "token": g_hoist["token"]},
        ]
    )


def _walk_pages(cursor=None, limit=2):
    """Yield successive pages by following next_cursor to the end."""
    pages = []
    while True:
        page = history.list_history(
            None if cursor is None else str(cursor), limit
        )
        pages.append(page)
        if page["next_cursor"] is None:
            return pages
        cursor = page["next_cursor"]


@pytest.fixture()
def client(db_pool):
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------- ordering


def test_history_empty_initially(services):
    page = history.list_history()
    assert page == {"events": [], "next_cursor": None}


def test_events_desc_with_time_action_seat_and_session(services):
    session = sessions.transition("start", "复盘轮次")
    first = _execute(services, LIFT, holder="升降台控制席")
    second = _execute(services, HOIST, holder="飞行吊点控制席")
    sessions.transition("end")
    # Executed after the round ended: belongs to no session.
    third = _execute(services, EXTRA, holder="联排负责人")

    page = history.list_history()
    ids = [e["event_id"] for e in page["events"]]
    assert ids == [third["event_id"], second["event_id"], first["event_id"]]
    assert ids == sorted(ids, reverse=True)

    top = page["events"][0]
    assert top["action_id"] == EXTRA
    assert top["label"]  # action label resolved in the same query
    assert top["holder"] == "联排负责人"
    assert top["result"] == "executed"
    assert top["occurred_at"]
    assert top["link_id"] is None
    assert top["session"] is None
    assert top["anomaly"] is None

    # Events inside the round carry its id and name.
    for event in page["events"][1:]:
        assert event["session"] == {"id": session["id"], "name": "复盘轮次"}
    assert page["events"][1]["holder"] == "飞行吊点控制席"
    assert page["next_cursor"] is None


# ----------------------------------------------------------- linked grouping


def test_linked_events_share_link_id_and_stay_adjacent(services):
    _execute(services, EXTRA)  # separates the two linked runs in the list
    first_run = _linked(services)
    _execute(services, EXTRA)
    second_run = _linked(services, holder="下一班控制席")
    _execute(services, EXTRA)

    page = history.list_history()
    events = page["events"]
    by_id = {e["event_id"]: e for e in events}

    for run, holder in ((first_run, "联排控制席"), (second_run, "下一班控制席")):
        run_ids = {ev["event_id"] for ev in run["events"]}
        rows = [by_id[i] for i in run_ids]
        # Same server-generated link id on exactly these two events.
        assert {r["link_id"] for r in rows} == {run["link_id"]}
        assert run["link_id"] is not None
        # ... and they are neighbours in the descending history order.
        positions = [i for i, e in enumerate(events) if e["event_id"] in run_ids]
        assert max(positions) - min(positions) == 1
        assert {r["holder"] for r in rows} == {holder}
        # Each action of the pair wrote exactly one event.
        assert {r["action_id"] for r in rows} == {LIFT, HOIST}

    # Single executions keep a NULL link id.
    singles = [e for e in events if e["action_id"] == EXTRA]
    assert singles and all(e["link_id"] is None for e in singles)


# ------------------------------------------------- anomaly rides its event


def test_anomaly_and_confirmation_shown_with_their_event(services):
    older = _execute(services, LIFT)
    anomalies.report(LIFT, "equipment", "上升异响", "升降台席", CONSOLE_A)
    anomalies.confirm(LIFT, "下一班-B", CONSOLE_B)
    newer = _execute(services, LIFT)  # the card moves on; history keeps both

    page = history.list_history()
    by_id = {e["event_id"]: e for e in page["events"]}

    old_row = by_id[older["event_id"]]
    record = old_row["anomaly"]
    assert record["event_id"] == older["event_id"]
    assert record["category"] == "equipment"
    assert record["description"] == "上升异响"
    assert record["reported_by"] == "升降台席"
    assert record["reported_at"]
    assert record["status"] == "confirmed"
    assert record["confirmed_by"] == "下一班-B"
    assert record["confirmed_at"]

    # The newer event has no anomaly; nothing bleeds across events.
    assert by_id[newer["event_id"]]["anomaly"] is None


# --------------------------------------- pagination stability (keyset cursor)


def test_pagination_ignores_events_created_while_paging(services):
    """New executions during a review never repeat or skip older records."""
    first = _execute(services, LIFT)
    second = _execute(services, HOIST)
    third = _execute(services, LIFT)
    fourth = _execute(services, HOIST)
    fifth = _execute(services, EXTRA)
    base_ids = [
        first["event_id"], second["event_id"], third["event_id"],
        fourth["event_id"], fifth["event_id"],
    ]

    # First page of a 2-per-page walk...
    page1 = history.list_history(None, 2)
    assert [e["event_id"] for e in page1["events"]] == base_ids[::-1][:2]
    assert page1["next_cursor"] == base_ids[::-1][1]

    # ...then NEW executions land before the reviewer turns the page.
    _execute(services, LIFT)
    _linked(services)

    seen = list(page1["events"])
    cursor = page1["next_cursor"]
    while cursor is not None:
        page = history.list_history(str(cursor), 2)
        seen.extend(page["events"])
        cursor = page["next_cursor"]

    seen_ids = [e["event_id"] for e in seen]
    # Exactly the five pre-walk events, each once, still strictly descending:
    # the three events created mid-walk belong to a newer page and can never
    # leak into (or shift) the pages of older records.
    assert seen_ids == base_ids[::-1]
    assert len(set(seen_ids)) == len(seen_ids)

    # A fresh walk from the top now sees everything, still each exactly once.
    all_ids = [
        e["event_id"]
        for page in _walk_pages()
        for e in page["events"]
    ]
    assert all_ids == sorted(all_ids, reverse=True)
    assert len(all_ids) == len(set(all_ids)) == 8


def test_page_size_clamped_and_next_cursor_exact(services):
    for _ in range(3):
        _execute(services, LIFT)

    page = history.list_history(None, 1)
    assert len(page["events"]) == 1
    assert page["next_cursor"] == page["events"][0]["event_id"]

    # An over-large limit is clamped to the server maximum, not honoured.
    assert history.clamp_limit(10_000) == history.MAX_PAGE_SIZE
    assert history.clamp_limit(0) == history.DEFAULT_PAGE_SIZE
    assert history.clamp_limit(None) == history.DEFAULT_PAGE_SIZE

    # Walking with page size 1 visits every event exactly once.
    ids = [e["event_id"] for page in _walk_pages(limit=1) for e in page["events"]]
    assert ids == sorted(ids, reverse=True)
    assert len(ids) == 3


# ------------------------------------------------------------- invalid cursor


def test_invalid_cursor_rejected(services):
    _execute(services, LIFT)
    for bad in ("abc", "1.5", "-3", "0", "", "  ", "12x",
                "99999999999999999999999999"):  # beyond the bigint range
        with pytest.raises(Exception) as ei:
            history.list_history(bad)
        exc = ei.value
        assert getattr(exc, "code", None) == "history_cursor_invalid", bad
        assert exc.status == 400

    # A well-formed cursor pointing past the oldest event is simply empty.
    page = history.list_history("1")
    assert page == {"events": [], "next_cursor": None}


# ------------------------------------------------------------------ HTTP level


def test_http_history_flow_and_error_envelope(client):
    assert client.get("/api/history").json() == {
        "events": [],
        "next_cursor": None,
    }

    client.post("/api/sessions/transition", json={"op": "start", "name": "复盘场"})
    token = client.post(
        f"/api/actions/{LIFT}/lease", json={"holder": "升降台控制席"}
    ).json()["token"]
    r = client.post(f"/api/actions/{LIFT}/execute", json={"token": token})
    assert r.status_code == 200
    client.post("/api/sessions/transition", json={"op": "end"})

    r = client.get("/api/history")
    assert r.status_code == 200
    (event,) = r.json()["events"]
    assert event["action_id"] == LIFT
    assert event["holder"] == "升降台控制席"
    assert event["session"]["name"] == "复盘场"
    assert r.json()["next_cursor"] is None

    # Invalid cursor: recognisable code in the shared error envelope.
    r = client.get("/api/history", params={"cursor": "不是事件编号"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "history_cursor_invalid"
    assert detail["message"]

    # The invalid call changed nothing: the same page is still served.
    assert client.get("/api/history").json()["events"] == [event]

    # Existing interfaces are untouched by the new endpoint.
    snap = client.get("/api/actions").json()
    assert snap["session"]["name"] == "复盘场"
    state = client.get(f"/api/actions/{LIFT}").json()
    assert state["event_count"] == 1
    assert state["last_event_id"] == event["event_id"]
