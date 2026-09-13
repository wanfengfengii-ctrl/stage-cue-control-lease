"""On-site anomaly records (现场异常) on executed action events.

Covers the acceptance points:
  * reporting creates exactly ONE pending record on the action's most
    recent execution event; the polling snapshot carries reporter, report
    time, category and description;
  * blank description / unknown category / blank reporter are 400
    invalid_anomaly and write nothing; reporting with no executed event is
    409 no_execution_event;
  * a repeat report is 409 anomaly_exists carrying the CURRENT record, and
    the failure never rewrites the first report's data;
  * confirmation moves pending -> confirmed exactly once, stamping the
    confirming seat and time; confirming with no record is 409
    anomaly_not_found, a repeat confirmation is 409 anomaly_confirmed and
    the first confirmation's data is untouched;
  * records are append-only and attached to their event: re-acquiring,
    releasing, single and linked execution never overwrite old rows, while
    the card snapshot follows the NEW event and stops showing the old
    anomaly;
  * the HTTP error envelope matches every other business error
    (detail.code / detail.message / detail.state) and additionally carries
    the current record in detail.anomaly.
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import anomalies
from app.main import app

LIFT = "lift_up"
HOIST = "hoist_fly_in"


# ------------------------------------------------------------- helpers


def _execute_once(services, action_id: str, holder="执行席-A"):
    grant = services.acquire(action_id, holder)
    out = services.execute(action_id, grant["token"])
    return out


def _anomaly_rows(db_pool, action_id: str):
    with db_pool.get_pool().connection() as conn:
        return conn.execute(
            """
            SELECT n.* FROM action_anomalies n
            JOIN action_events e ON e.id = n.event_id
            WHERE e.action_id = %s
            ORDER BY n.id
            """,
            (action_id,),
        ).fetchall()


@pytest.fixture()
def client(db_pool):
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- report


def test_report_creates_pending_record_on_latest_event(services):
    out = _execute_once(services, LIFT)
    res = anomalies.report(LIFT, "equipment", "上升至 3 米处有异响", "升降台席")
    record = res["anomaly"]
    assert record["status"] == "pending"
    assert record["event_id"] == out["event_id"]
    assert record["category"] == "equipment"
    assert record["description"] == "上升至 3 米处有异响"
    assert record["reported_by"] == "升降台席"
    assert record["reported_at"]
    assert record["confirmed_by"] is None
    assert record["confirmed_at"] is None

    # The polling snapshot of the card now carries exactly this record.
    state = services.get_action_state(LIFT)
    assert state["last_event_id"] == out["event_id"]
    assert state["anomaly"]["id"] == record["id"]
    assert state["anomaly"]["reported_by"] == "升降台席"


def test_report_trims_inputs(services):
    _execute_once(services, LIFT)
    res = anomalies.report(LIFT, "  operation ", "  限位触发后未复位  ", " 升降台席 ")
    assert res["anomaly"]["category"] == "operation"
    assert res["anomaly"]["description"] == "限位触发后未复位"
    assert res["anomaly"]["reported_by"] == "升降台席"


@pytest.mark.parametrize(
    "category,description,reporter",
    [
        ("bogus", "说明", "席"),          # unknown category
        ("equipment", "", "席"),          # blank description
        ("equipment", "   ", "席"),       # whitespace-only description
        ("equipment", "说明", ""),        # blank reporter
        ("equipment", "说明", "   "),     # whitespace-only reporter
    ],
)
def test_report_invalid_inputs_400_and_nothing_written(
    services, db_pool, category, description, reporter
):
    _execute_once(services, LIFT)
    with pytest.raises(anomalies.AnomalyError) as ei:
        anomalies.report(LIFT, category, description, reporter)
    assert ei.value.status == 400
    assert ei.value.code == "invalid_anomaly"
    assert _anomaly_rows(db_pool, LIFT) == []


def test_report_without_executed_event(services, db_pool):
    # No event at all.
    with pytest.raises(anomalies.AnomalyError) as ei:
        anomalies.report(LIFT, "equipment", "说明", "席")
    assert ei.value.status == 409
    assert ei.value.code == "no_execution_event"

    # Releasing a lease without executing writes no event either.
    grant = services.acquire(LIFT, "席")
    services.release(LIFT, grant["token"])
    with pytest.raises(anomalies.AnomalyError) as ei:
        anomalies.report(LIFT, "equipment", "说明", "席")
    assert ei.value.code == "no_execution_event"
    assert _anomaly_rows(db_pool, LIFT) == []


def test_repeat_report_returns_current_record_without_overwrite(services, db_pool):
    _execute_once(services, LIFT)
    first = anomalies.report(LIFT, "equipment", "首次报告：异响", "一席")["anomaly"]

    # The second seat tries to report again, with DIFFERENT content.
    with pytest.raises(anomalies.AnomalyError) as ei:
        anomalies.report(LIFT, "other", "二次报告：试图覆盖", "二席")
    exc = ei.value
    assert exc.status == 409
    assert exc.code == "anomaly_exists"
    # The business error carries the current (first) record.
    assert exc.anomaly["id"] == first["id"]
    assert exc.anomaly["description"] == "首次报告：异响"
    assert exc.anomaly["reported_by"] == "一席"
    assert exc.anomaly["status"] == "pending"

    # Exactly one row, and the first report's data is byte-for-byte intact.
    rows = _anomaly_rows(db_pool, LIFT)
    assert len(rows) == 1
    assert rows[0]["category"] == "equipment"
    assert rows[0]["description"] == "首次报告：异响"
    assert rows[0]["reported_by"] == "一席"
    assert rows[0]["confirmed_by"] is None


# --------------------------------------------------------------- confirm


def test_confirm_pending_record_stamps_seat_and_time(services):
    _execute_once(services, LIFT)
    reported = anomalies.report(LIFT, "environment", "侧台有强光干扰", "一席")["anomaly"]

    res = anomalies.confirm(LIFT, "下一班-B")
    record = res["anomaly"]
    assert record["id"] == reported["id"]
    assert record["status"] == "confirmed"
    assert record["confirmed_by"] == "下一班-B"
    assert record["confirmed_at"]
    # Report-side data is preserved by the transition.
    assert record["reported_by"] == "一席"
    assert record["category"] == "environment"
    assert record["description"] == "侧台有强光干扰"

    state = services.get_action_state(LIFT)
    assert state["anomaly"]["status"] == "confirmed"
    assert state["anomaly"]["confirmed_by"] == "下一班-B"


def test_confirm_without_record_is_409(services, db_pool):
    _execute_once(services, LIFT)
    with pytest.raises(anomalies.AnomalyError) as ei:
        anomalies.confirm(LIFT, "下一班-B")
    assert ei.value.status == 409
    assert ei.value.code == "anomaly_not_found"

    # Never executed: same boundary.
    with pytest.raises(anomalies.AnomalyError) as ei:
        anomalies.confirm(HOIST, "下一班-B")
    assert ei.value.code == "anomaly_not_found"
    assert _anomaly_rows(db_pool, LIFT) == []


def test_repeat_confirm_returns_record_without_restamping(services, db_pool):
    _execute_once(services, LIFT)
    anomalies.report(LIFT, "equipment", "异响", "一席")
    first = anomalies.confirm(LIFT, "下一班-B")["anomaly"]

    # Another seat confirms again, trying to claim the acknowledgement.
    with pytest.raises(anomalies.AnomalyError) as ei:
        anomalies.confirm(LIFT, "下一班-C")
    exc = ei.value
    assert exc.status == 409
    assert exc.code == "anomaly_confirmed"
    assert exc.anomaly["id"] == first["id"]
    assert exc.anomaly["confirmed_by"] == "下一班-B"
    assert exc.anomaly["status"] == "confirmed"

    # The first confirmer and time are untouched.
    rows = _anomaly_rows(db_pool, LIFT)
    assert len(rows) == 1
    assert rows[0]["status"] == "confirmed"
    assert rows[0]["confirmed_by"] == "下一班-B"
    assert rows[0]["confirmed_at"].isoformat() == first["confirmed_at"]
    assert rows[0]["reported_by"] == "一席"


def test_concurrent_confirms_exactly_one_winner(services, db_pool):
    _execute_once(services, LIFT)
    anomalies.report(LIFT, "equipment", "异响", "一席")
    n = 16
    barrier = threading.Barrier(n)
    results: dict[int, object] = {}

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=30)
            results[i] = anomalies.confirm(LIFT, f"接班席-{i}")
        except anomalies.AnomalyError as exc:
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    winners = [r for r in results.values() if not isinstance(r, Exception)]
    assert len(winners) == 1
    winner_seat = winners[0]["anomaly"]["confirmed_by"]
    rows = _anomaly_rows(db_pool, LIFT)
    assert len(rows) == 1
    assert rows[0]["confirmed_by"] == winner_seat
    # Every loser was told about that same record.
    losers = [r for r in results.values() if isinstance(r, Exception)]
    assert len(losers) == n - 1
    assert all(r.code == "anomaly_confirmed" for r in losers)
    assert all(r.anomaly["confirmed_by"] == winner_seat for r in losers)


# ------------------------------------------------- append-only / new event


def test_reacquire_and_release_keep_record(services):
    out = _execute_once(services, LIFT)
    anomalies.report(LIFT, "equipment", "异响", "一席")

    # A new lease cycle (acquire -> release) writes no event; the card still
    # follows the same last event and its pending anomaly stays put.
    grant = services.acquire(LIFT, "二席")
    assert services.get_action_state(LIFT)["anomaly"]["reported_by"] == "一席"
    services.release(LIFT, grant["token"])
    state = services.get_action_state(LIFT)
    assert state["last_event_id"] == out["event_id"]
    assert state["anomaly"]["status"] == "pending"
    assert state["anomaly"]["reported_by"] == "一席"


def test_new_event_switches_card_and_old_anomaly_hidden_but_kept(services, db_pool):
    first = _execute_once(services, LIFT)
    anomalies.report(LIFT, "equipment", "第一次执行后的异响", "一席")
    anomalies.confirm(LIFT, "下一班-B")

    # A second execution produces a new event with no anomaly.
    second = _execute_once(services, LIFT, "二席")
    state = services.get_action_state(LIFT)
    assert state["last_event_id"] == second["event_id"]
    assert state["anomaly"] is None
    assert state["event_count"] == 2

    # The old record is still in PostgreSQL, still attached to the FIRST
    # event, and was not overwritten or deleted.
    rows = _anomaly_rows(db_pool, LIFT)
    assert len(rows) == 1
    assert rows[0]["event_id"] == first["event_id"]
    assert rows[0]["description"] == "第一次执行后的异响"
    assert rows[0]["status"] == "confirmed"
    assert rows[0]["confirmed_by"] == "下一班-B"

    # A report now lands on the NEW event, never touching the old row.
    anomalies.report(LIFT, "other", "第二次执行后的新情况", "三席")
    state = services.get_action_state(LIFT)
    assert state["anomaly"]["event_id"] == second["event_id"]
    assert state["anomaly"]["description"] == "第二次执行后的新情况"
    rows = _anomaly_rows(db_pool, LIFT)
    assert len(rows) == 2


def test_linked_execution_does_not_overwrite_old_records(services, db_pool):
    # An old anomaly exists on a previous single execution of the lift.
    old = _execute_once(services, LIFT)
    anomalies.report(LIFT, "equipment", "旧异常", "一席")

    # A linked run executes the lift + hoist as one operation.
    g_lift = services.acquire(LIFT, "联排席")
    g_hoist = services.acquire(HOIST, "联排席")
    linked = services.execute_linked(
        [
            {"action_id": LIFT, "token": g_lift["token"]},
            {"action_id": HOIST, "token": g_hoist["token"]},
        ]
    )
    assert len(linked["events"]) == 2

    # Both cards follow their fresh linked events and show no anomaly...
    for aid in (LIFT, HOIST):
        state = services.get_action_state(aid)
        assert state["anomaly"] is None
        assert state["last_link_id"] == linked["link_id"]

    # ...while the old row survives untouched on its original event.
    rows = _anomaly_rows(db_pool, LIFT)
    assert len(rows) == 1
    assert rows[0]["event_id"] == old["event_id"]
    assert rows[0]["description"] == "旧异常"
    assert _anomaly_rows(db_pool, HOIST) == []


def test_report_after_linked_execution_attaches_to_linked_event(services, db_pool):
    g_lift = services.acquire(LIFT, "联排席")
    g_hoist = services.acquire(HOIST, "联排席")
    linked = services.execute_linked(
        [
            {"action_id": LIFT, "token": g_lift["token"]},
            {"action_id": HOIST, "token": g_hoist["token"]},
        ]
    )
    lift_event = next(e for e in linked["events"] if e["action_id"] == LIFT)

    res = anomalies.report(LIFT, "operation", "联动后姿态偏差", "联排席")
    assert res["anomaly"]["event_id"] == lift_event["event_id"]
    rows = _anomaly_rows(db_pool, LIFT)
    assert len(rows) == 1
    assert rows[0]["event_id"] == lift_event["event_id"]


# ------------------------------------------------------------------ HTTP


def test_http_report_and_confirm_flow(client):
    token = client.post(
        f"/api/actions/{LIFT}/lease", json={"holder": "一席"}
    ).json()["token"]
    client.post(f"/api/actions/{LIFT}/execute", json={"token": token})

    r = client.post(
        f"/api/actions/{LIFT}/anomaly",
        json={
            "category": "equipment",
            "description": "异响",
            "reporter": "升降台席",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["anomaly"]["status"] == "pending"
    assert body["anomaly"]["reported_by"] == "升降台席"
    assert body["state"]["anomaly"]["id"] == body["anomaly"]["id"]

    # Polling snapshot carries the pending record.
    state = client.get(f"/api/actions/{LIFT}").json()
    assert state["anomaly"]["category"] == "equipment"
    assert state["anomaly"]["confirmed_at"] is None

    # Another seat confirms.
    r = client.post(
        f"/api/actions/{LIFT}/anomaly/confirm",
        json={"confirmer": "下一班-B"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["anomaly"]["status"] == "confirmed"
    assert r.json()["anomaly"]["confirmed_by"] == "下一班-B"


def test_http_blank_description_inline_error(client):
    token = client.post(
        f"/api/actions/{LIFT}/lease", json={"holder": "一席"}
    ).json()["token"]
    client.post(f"/api/actions/{LIFT}/execute", json={"token": token})

    r = client.post(
        f"/api/actions/{LIFT}/anomaly",
        json={"category": "equipment", "description": "  ", "reporter": "一席"},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_anomaly"
    assert "说明" in detail["message"]


def test_http_repeat_report_and_confirm_carry_current_record(client):
    token = client.post(
        f"/api/actions/{LIFT}/lease", json={"holder": "一席"}
    ).json()["token"]
    client.post(f"/api/actions/{LIFT}/execute", json={"token": token})
    first = client.post(
        f"/api/actions/{LIFT}/anomaly",
        json={"category": "equipment", "description": "异响", "reporter": "一席"},
    ).json()["anomaly"]

    r = client.post(
        f"/api/actions/{LIFT}/anomaly",
        json={"category": "other", "description": "覆盖尝试", "reporter": "二席"},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "anomaly_exists"
    assert detail["anomaly"]["id"] == first["id"]
    assert detail["anomaly"]["description"] == "异响"
    # Like every other business error, fresh action state rides along.
    assert detail["state"]["action_id"] == LIFT

    client.post(
        f"/api/actions/{LIFT}/anomaly/confirm",
        json={"confirmer": "下一班-B"},
    )
    r = client.post(
        f"/api/actions/{LIFT}/anomaly/confirm",
        json={"confirmer": "下一班-C"},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "anomaly_confirmed"
    assert detail["anomaly"]["confirmed_by"] == "下一班-B"


def test_http_actions_snapshot_shape_keeps_contract(client):
    """Anomaly fields are additive; lease/link/session contract is intact."""
    snap = client.get("/api/actions").json()
    for action in snap["actions"]:
        assert action["anomaly"] is None
        assert "last_event_id" in action
        assert {"action_id", "status", "holder", "event_count",
                "remaining_seconds", "last_link_id"} <= set(action)


def test_http_unknown_action_anomaly_routes_404(client):
    r = client.post(
        "/api/actions/nope/anomaly",
        json={"category": "other", "description": "x", "reporter": "席"},
    )
    assert r.status_code == 404
    r = client.post(
        "/api/actions/nope/anomaly/confirm", json={"confirmer": "席"}
    )
    assert r.status_code == 404
