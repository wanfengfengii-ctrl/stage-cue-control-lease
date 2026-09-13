"""Rehearsal sessions (场次): event attribution and error boundaries.

Covers the acceptance points:
  * single and linked executions tag their events with the active session
    inside the same transaction;
  * the polling snapshot carries the session summary — name, cumulative
    event count, distinct action count;
  * duplicate start / blank name / end-without-active are recognisable
    business errors that change no event;
  * after the session ends its summary stays queryable and frozen: later
    executions are not counted;
  * at most one active session, even under concurrent start requests.
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import sessions
from app.main import app

LIFT = "lift_up"
HOIST = "hoist_fly_in"
EXTRA = "emergency_stop"


def _events(db_pool, action_id: str):
    with db_pool.get_pool().connection() as conn:
        return conn.execute(
            "SELECT * FROM action_events WHERE action_id = %s ORDER BY id",
            (action_id,),
        ).fetchall()


def _all_events(db_pool):
    with db_pool.get_pool().connection() as conn:
        return conn.execute(
            "SELECT * FROM action_events ORDER BY id"
        ).fetchall()


def _session_rows(db_pool):
    with db_pool.get_pool().connection() as conn:
        return conn.execute(
            "SELECT * FROM rehearsal_sessions ORDER BY id"
        ).fetchall()


def _acquire_pair(services, holder="联排控制席"):
    g_lift = services.acquire(LIFT, holder)
    g_hoist = services.acquire(HOIST, holder)
    return g_lift, g_hoist


def _linked(services, g_lift, g_hoist):
    return services.execute_linked(
        [
            {"action_id": LIFT, "token": g_lift["token"]},
            {"action_id": HOIST, "token": g_hoist["token"]},
        ]
    )


# ------------------------------------------------------------- attribution


def test_no_session_initially(services):
    assert sessions.current_summary() is None


def test_start_session_summary_visible(services):
    out = sessions.transition("start", "第一轮联排")
    assert out["status"] == "active"
    assert out["name"] == "第一轮联排"
    assert out["event_count"] == 0
    assert out["action_count"] == 0
    assert out["started_at"] and out["ended_at"] is None
    assert sessions.current_summary() == out


def test_single_execute_attributes_event_to_active_session(services, db_pool):
    session = sessions.transition("start", "第一轮联排")
    g = services.acquire(LIFT, "联排控制席")
    services.execute(LIFT, g["token"])

    (event,) = _events(db_pool, LIFT)
    assert event["session_id"] == session["id"]

    summary = sessions.current_summary()
    assert summary["status"] == "active"
    assert summary["event_count"] == 1
    assert summary["action_count"] == 1


def test_linked_execute_attributes_both_events(services, db_pool):
    session = sessions.transition("start", "双设备协同段")
    g_lift, g_hoist = _acquire_pair(services)
    _linked(services, g_lift, g_hoist)

    for aid in (LIFT, HOIST):
        (event,) = _events(db_pool, aid)
        assert event["session_id"] == session["id"]

    summary = sessions.current_summary()
    assert summary["event_count"] == 2
    assert summary["action_count"] == 2


def test_mixed_single_and_linked_counts(services, db_pool):
    """One single + one linked run: 3 events across 2 distinct actions."""
    sessions.transition("start", "联排第一场")
    g = services.acquire(LIFT, "联排控制席")
    services.execute(LIFT, g["token"])
    g_lift, g_hoist = _acquire_pair(services)
    _linked(services, g_lift, g_hoist)

    summary = sessions.current_summary()
    assert summary["event_count"] == 3
    # lift_up ran twice (single + linked) but counts once as a distinct action.
    assert summary["action_count"] == 2
    assert all(e["session_id"] == summary["id"] for e in _all_events(db_pool))


def test_events_outside_session_keep_null_session_id(services, db_pool):
    """With no active session, executions behave exactly as before."""
    g = services.acquire(LIFT, "联排控制席")
    services.execute(LIFT, g["token"])
    (event,) = _events(db_pool, LIFT)
    assert event["session_id"] is None
    # No session was ever started: nothing to summarise.
    assert sessions.current_summary() is None


# ---------------------------------------------------------- error boundary


def test_duplicate_start_rejected_and_events_untouched(services, db_pool):
    first = sessions.transition("start", "第一轮联排")
    g = services.acquire(LIFT, "联排控制席")
    services.execute(LIFT, g["token"])

    with pytest.raises(sessions.LeaseError) as ei:
        sessions.transition("start", "插队场次")
    exc = ei.value
    assert exc.code == "session_active"
    assert exc.status == 409

    # Nothing changed: still the first session, its one event intact.
    assert len(_session_rows(db_pool)) == 1
    summary = sessions.current_summary()
    assert summary["id"] == first["id"]
    assert summary["name"] == "第一轮联排"
    assert summary["event_count"] == 1
    assert len(_all_events(db_pool)) == 1


def test_blank_name_rejected_and_no_session_created(services, db_pool):
    for bad in ("", "   ", None):
        with pytest.raises(sessions.LeaseError) as ei:
            sessions.transition("start", bad)
        assert ei.value.code == "invalid_name"
        assert ei.value.status == 400
    assert _session_rows(db_pool) == []
    assert sessions.current_summary() is None


def test_end_without_active_session_rejected(services, db_pool):
    with pytest.raises(sessions.LeaseError) as ei:
        sessions.transition("end")
    assert ei.value.code == "no_active_session"
    assert ei.value.status == 409

    # Ending an already-ended round is the same recognisable error.
    sessions.transition("start", "第一轮联排")
    sessions.transition("end")
    with pytest.raises(sessions.LeaseError) as ei:
        sessions.transition("end")
    assert ei.value.code == "no_active_session"
    # Only one session row ever existed and no event was touched.
    assert len(_session_rows(db_pool)) == 1
    assert _all_events(db_pool) == []


def test_unknown_operation_rejected(services):
    with pytest.raises(sessions.LeaseError) as ei:
        sessions.transition("pause", "x")
    assert ei.value.code == "invalid_request"
    assert ei.value.status == 400


def test_concurrent_starts_exactly_one_active_session(services, db_pool):
    """Racing start requests: exactly one wins, the loser gets 409."""
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def start(name):
        try:
            barrier.wait(timeout=30)
            results[name] = sessions.transition("start", name)
        except sessions.LeaseError as exc:
            results[name] = exc

    threads = [
        threading.Thread(target=start, args=(f"场次-{name}",))
        for name in ("A", "B")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    outcomes = list(results.values())
    winners = [o for o in outcomes if not isinstance(o, sessions.LeaseError)]
    losers = [o for o in outcomes if isinstance(o, sessions.LeaseError)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0].code == "session_active"

    rows = _session_rows(db_pool)
    assert len(rows) == 1
    assert rows[0]["ended_at"] is None
    assert sessions.current_summary()["id"] == winners[0]["id"]


# ----------------------------------------------------- end and frozen summary


def test_end_freezes_summary_and_later_events_not_counted(services, db_pool):
    session = sessions.transition("start", "第一轮联排")
    g = services.acquire(LIFT, "联排控制席")
    services.execute(LIFT, g["token"])
    g_lift, g_hoist = _acquire_pair(services)
    _linked(services, g_lift, g_hoist)

    ended = sessions.transition("end")
    assert ended["status"] == "ended"
    assert ended["ended_at"] is not None
    assert ended["event_count"] == 3
    assert ended["action_count"] == 2

    # Actions executed afterwards belong to no session: their events carry a
    # NULL session id and the ended summary does not move.
    g2 = services.acquire(EXTRA, "联排控制席")
    services.execute(EXTRA, g2["token"])
    (extra_event,) = _events(db_pool, EXTRA)
    assert extra_event["session_id"] is None

    summary = sessions.current_summary()
    assert summary["id"] == session["id"]
    assert summary["status"] == "ended"
    assert summary["event_count"] == 3
    assert summary["action_count"] == 2


def test_new_session_after_end_starts_fresh(services, db_pool):
    first = sessions.transition("start", "第一轮联排")
    g = services.acquire(LIFT, "联排控制席")
    services.execute(LIFT, g["token"])
    sessions.transition("end")

    second = sessions.transition("start", "第二轮联排")
    assert second["id"] != first["id"]
    assert second["status"] == "active"
    # The new round counts only its own events.
    assert second["event_count"] == 0
    assert second["action_count"] == 0

    g_lift, g_hoist = _acquire_pair(services)
    _linked(services, g_lift, g_hoist)
    summary = sessions.current_summary()
    assert summary["id"] == second["id"]
    assert summary["event_count"] == 2
    assert summary["action_count"] == 2
    # The first round's events still belong to the first round.
    assert {e["session_id"] for e in _all_events(db_pool)} == {
        first["id"],
        second["id"],
    }


# ------------------------------------------------------------- HTTP level


@pytest.fixture()
def client(db_pool):
    with TestClient(app) as c:
        yield c


def test_http_actions_snapshot_carries_session_summary(client):
    r = client.get("/api/actions")
    assert r.status_code == 200
    assert r.json()["session"] is None

    r = client.post(
        "/api/sessions/transition",
        json={"op": "start", "name": "第一轮联排"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["name"] == "第一轮联排"

    snap = client.get("/api/actions").json()
    session = snap["session"]
    assert session["name"] == "第一轮联排"
    assert session["status"] == "active"
    assert session["event_count"] == 0
    assert session["action_count"] == 0
    # Existing snapshot fields are untouched.
    assert snap["ttl_seconds"] == 30
    assert {a["action_id"] for a in snap["actions"]} >= {"lift_up"}

    r = client.get("/api/sessions/current")
    assert r.status_code == 200
    assert r.json()["session"]["id"] == session["id"]


def test_http_full_round_flow_and_frozen_summary(client):
    client.post(
        "/api/sessions/transition",
        json={"op": "start", "name": "联排第一场"},
    )

    # Single execution counts once.
    token = client.post(
        f"/api/actions/{LIFT}/lease", json={"holder": "联排控制席"}
    ).json()["token"]
    r = client.post(f"/api/actions/{LIFT}/execute", json={"token": token})
    assert r.status_code == 200
    session = client.get("/api/actions").json()["session"]
    assert session["event_count"] == 1
    assert session["action_count"] == 1

    # Linked execution adds two events and one more distinct action.
    lift_token = client.post(
        f"/api/actions/{LIFT}/lease", json={"holder": "联排控制席"}
    ).json()["token"]
    hoist_token = client.post(
        f"/api/actions/{HOIST}/lease", json={"holder": "联排控制席"}
    ).json()["token"]
    r = client.post(
        "/api/actions/execute-linked",
        json={
            "items": [
                {"action_id": LIFT, "token": lift_token},
                {"action_id": HOIST, "token": hoist_token},
            ]
        },
    )
    assert r.status_code == 200, r.text
    session = client.get("/api/actions").json()["session"]
    assert session["event_count"] == 3
    assert session["action_count"] == 2

    # End the round: the summary stays queryable and frozen.
    r = client.post("/api/sessions/transition", json={"op": "end"})
    assert r.status_code == 200
    assert r.json()["status"] == "ended"
    assert r.json()["event_count"] == 3

    token = client.post(
        f"/api/actions/{EXTRA}/lease", json={"holder": "联排控制席"}
    ).json()["token"]
    r = client.post(f"/api/actions/{EXTRA}/execute", json={"token": token})
    assert r.status_code == 200
    session = client.get("/api/sessions/current").json()["session"]
    assert session["status"] == "ended"
    assert session["event_count"] == 3
    assert session["action_count"] == 2


def test_http_transition_error_boundaries(client):
    # Blank name -> 400 invalid_name.
    r = client.post(
        "/api/sessions/transition", json={"op": "start", "name": "   "}
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_name"
    assert detail["session"] is None

    # End with no active session -> 409 no_active_session.
    r = client.post("/api/sessions/transition", json={"op": "end"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "no_active_session"

    # Start -> duplicate start -> 409 session_active carrying the summary.
    r = client.post(
        "/api/sessions/transition", json={"op": "start", "name": "第一轮"}
    )
    assert r.status_code == 200
    r = client.post(
        "/api/sessions/transition", json={"op": "start", "name": "第二轮"}
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "session_active"
    assert detail["session"]["name"] == "第一轮"
    assert detail["session"]["status"] == "active"

    # Unknown op -> 400 invalid_request.
    r = client.post("/api/sessions/transition", json={"op": "pause"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_request"

    # None of the rejections created a session or an event.
    session = client.get("/api/sessions/current").json()["session"]
    assert session["name"] == "第一轮"
    assert session["event_count"] == 0
    state = client.get(f"/api/actions/{LIFT}").json()
    assert state["event_count"] == 0
