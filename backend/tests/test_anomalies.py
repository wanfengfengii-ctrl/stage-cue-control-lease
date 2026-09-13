"""On-site anomaly records (现场异常) on executed action events.

Covers the acceptance points:
  * reporting creates exactly ONE pending record on the action's most
    recent execution event; the polling snapshot carries reporter, report
    time, category and description;
  * blank description / unknown category / blank reporter / missing console
    id are 400 invalid_anomaly and write nothing; reporting with no executed
    event is 409 no_execution_event;
  * a repeat report is 409 anomaly_exists carrying the CURRENT record, and
    the failure never rewrites the first report's data;
  * confirmation moves pending -> confirmed exactly once, stamping the
    confirming seat name, its stable CONSOLE ID and the time; confirming with
    no record is 409 anomaly_not_found, a repeat confirmation is 409
    anomaly_confirmed and the first confirmation's data is untouched;
  * the reporting CONSOLE can never acknowledge its own report — the check
    uses the stable console id, so editing the seat name in the same browser
    page (an obvious handover bypass) is still rejected; two different
    consoles happen to share a seat name remain two distinct seats;
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

# Stable browser-console identities, deliberately independent of the
# editable seat NAME. Tests use these exactly as the React console does.
CONSOLE_A = "console-aaaa-1111"
CONSOLE_B = "console-bbbb-2222"
CONSOLE_C = "console-cccc-3333"


# ------------------------------------------------------------- helpers


def _execute_once(services, action_id: str, holder="执行席-A"):
    grant = services.acquire(action_id, holder)
    out = services.execute(action_id, grant["token"])
    return out


def _report(services, description="异响", reporter="一席", console_id=CONSOLE_A,
            category="equipment", action_id=LIFT):
    return anomalies.report(
        action_id, category, description, reporter, console_id
    )


def _confirm(services, confirmer="下一班-B", console_id=CONSOLE_B,
             action_id=LIFT):
    return anomalies.confirm(action_id, confirmer, console_id)


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
    res = _report(services, "上升至 3 米处有异响", "升降台席", CONSOLE_A)
    record = res["anomaly"]
    assert record["status"] == "pending"
    assert record["event_id"] == out["event_id"]
    assert record["category"] == "equipment"
    assert record["description"] == "上升至 3 米处有异响"
    assert record["reported_by"] == "升降台席"
    assert record["reporter_id"] == CONSOLE_A
    assert record["reported_at"]
    assert record["confirmed_by"] is None
    assert record["confirmer_id"] is None
    assert record["confirmed_at"] is None

    # The polling snapshot of the card now carries exactly this record.
    state = services.get_action_state(LIFT)
    assert state["last_event_id"] == out["event_id"]
    assert state["anomaly"]["id"] == record["id"]
    assert state["anomaly"]["reported_by"] == "升降台席"
    assert state["anomaly"]["reporter_id"] == CONSOLE_A


def test_report_trims_inputs(services):
    _execute_once(services, LIFT)
    res = anomalies.report(
        LIFT, "  operation ", "  限位触发后未复位  ", " 升降台席 ", " cid "
    )
    assert res["anomaly"]["category"] == "operation"
    assert res["anomaly"]["description"] == "限位触发后未复位"
    assert res["anomaly"]["reported_by"] == "升降台席"
    assert res["anomaly"]["reporter_id"] == "cid"


@pytest.mark.parametrize(
    "category,description,reporter,console_id",
    [
        ("bogus", "说明", "席", CONSOLE_A),    # unknown category
        ("equipment", "", "席", CONSOLE_A),    # blank description
        ("equipment", "   ", "席", CONSOLE_A),  # whitespace-only description
        ("equipment", "说明", "", CONSOLE_A),  # blank reporter
        ("equipment", "说明", "   ", CONSOLE_A),  # whitespace-only reporter
        ("equipment", "说明", "席", ""),       # missing console id
        ("equipment", "说明", "席", "   "),    # whitespace console id
    ],
)
def test_report_invalid_inputs_400_and_nothing_written(
    services, db_pool, category, description, reporter, console_id
):
    _execute_once(services, LIFT)
    with pytest.raises(anomalies.AnomalyError) as ei:
        anomalies.report(LIFT, category, description, reporter, console_id)
    assert ei.value.status == 400
    assert ei.value.code == "invalid_anomaly"
    assert _anomaly_rows(db_pool, LIFT) == []


def test_report_without_executed_event(services, db_pool):
    # No event at all.
    with pytest.raises(anomalies.AnomalyError) as ei:
        anomalies.report(LIFT, "equipment", "说明", "席", CONSOLE_A)
    assert ei.value.status == 409
    assert ei.value.code == "no_execution_event"

    # Releasing a lease without executing writes no event either.
    grant = services.acquire(LIFT, "席")
    services.release(LIFT, grant["token"])
    with pytest.raises(anomalies.AnomalyError) as ei:
        anomalies.report(LIFT, "equipment", "说明", "席", CONSOLE_A)
    assert ei.value.code == "no_execution_event"
    assert _anomaly_rows(db_pool, LIFT) == []


def test_repeat_report_returns_current_record_without_overwrite(services, db_pool):
    _execute_once(services, LIFT)
    first = _report(services, "首次报告：异响", "一席", CONSOLE_A)["anomaly"]

    # The second seat tries to report again, with DIFFERENT content.
    with pytest.raises(anomalies.AnomalyError) as ei:
        anomalies.report(LIFT, "other", "二次报告：试图覆盖", "二席", CONSOLE_B)
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
    assert rows[0]["reporter_id"] == CONSOLE_A
    assert rows[0]["confirmed_by"] is None


# --------------------------------------------------------------- confirm


def test_confirm_pending_record_stamps_seat_console_and_time(services):
    _execute_once(services, LIFT)
    reported = _report(services, "侧台有强光干扰", "一席", CONSOLE_A,
                       category="environment")["anomaly"]

    res = _confirm(services, "下一班-B", CONSOLE_B)
    record = res["anomaly"]
    assert record["id"] == reported["id"]
    assert record["status"] == "confirmed"
    assert record["confirmed_by"] == "下一班-B"
    assert record["confirmer_id"] == CONSOLE_B
    assert record["confirmed_at"]
    # Report-side data is preserved by the transition.
    assert record["reported_by"] == "一席"
    assert record["reporter_id"] == CONSOLE_A
    assert record["category"] == "environment"
    assert record["description"] == "侧台有强光干扰"

    state = services.get_action_state(LIFT)
    assert state["anomaly"]["status"] == "confirmed"
    assert state["anomaly"]["confirmed_by"] == "下一班-B"
    assert state["anomaly"]["confirmer_id"] == CONSOLE_B


def test_reporting_console_cannot_confirm_even_after_renaming(services, db_pool):
    """The reported bypass: rename this page to the next shift, then confirm.

    The seat NAME changes but the browser console's stable id does not, so
    the server keeps rejecting the acknowledgement as anomaly_self_confirm.
    """
    _execute_once(services, LIFT)
    _report(services, "异响", "升降台席", CONSOLE_A)

    # Same console id under the original name — rejected.
    with pytest.raises(anomalies.AnomalyError) as ei:
        anomalies.confirm(LIFT, "升降台席", CONSOLE_A)
    exc = ei.value
    assert exc.status == 409
    assert exc.code == "anomaly_self_confirm"
    assert exc.anomaly["status"] == "pending"
    assert exc.anomaly["reported_by"] == "升降台席"
    assert exc.anomaly["confirmed_by"] is None

    # The bypass attempt: same browser console, now CALLING itself the next
    # shift (with or without surrounding spaces). Must still be rejected.
    for fake_name in ("下一班-B", "  下一班-B  "):
        with pytest.raises(anomalies.AnomalyError) as ei:
            anomalies.confirm(LIFT, fake_name, CONSOLE_A)
        assert ei.value.code == "anomaly_self_confirm"
        assert ei.value.anomaly["status"] == "pending"

    rows = _anomaly_rows(db_pool, LIFT)
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["confirmed_by"] is None
    assert rows[0]["confirmer_id"] is None
    assert rows[0]["confirmed_at"] is None

    # A genuinely DIFFERENT console can still confirm it afterwards, even
    # though it happens to type the same seat name the reporter used.
    out = anomalies.confirm(LIFT, "升降台席", CONSOLE_B)
    assert out["anomaly"]["status"] == "confirmed"
    assert out["anomaly"]["confirmed_by"] == "升降台席"
    assert out["anomaly"]["confirmer_id"] == CONSOLE_B


def test_confirm_requires_console_id(services):
    _execute_once(services, LIFT)
    _report(services, "异响", "一席", CONSOLE_A)
    for missing in ("", "   "):
        with pytest.raises(anomalies.AnomalyError) as ei:
            anomalies.confirm(LIFT, "下一班-B", missing)
        assert ei.value.code == "invalid_anomaly"
        assert ei.value.status == 400


def test_confirm_without_record_is_409(services, db_pool):
    _execute_once(services, LIFT)
    with pytest.raises(anomalies.AnomalyError) as ei:
        anomalies.confirm(LIFT, "下一班-B", CONSOLE_B)
    assert ei.value.status == 409
    assert ei.value.code == "anomaly_not_found"

    # Never executed: same boundary.
    with pytest.raises(anomalies.AnomalyError) as ei:
        anomalies.confirm(HOIST, "下一班-B", CONSOLE_B)
    assert ei.value.code == "anomaly_not_found"
    assert _anomaly_rows(db_pool, LIFT) == []


def test_repeat_confirm_returns_record_without_restamping(services, db_pool):
    _execute_once(services, LIFT)
    _report(services, "异响", "一席", CONSOLE_A)
    first = _confirm(services, "下一班-B", CONSOLE_B)["anomaly"]

    # A third console confirms again, trying to claim the acknowledgement.
    with pytest.raises(anomalies.AnomalyError) as ei:
        anomalies.confirm(LIFT, "下一班-C", CONSOLE_C)
    exc = ei.value
    assert exc.status == 409
    assert exc.code == "anomaly_confirmed"
    assert exc.anomaly["id"] == first["id"]
    assert exc.anomaly["confirmed_by"] == "下一班-B"
    assert exc.anomaly["confirmer_id"] == CONSOLE_B
    assert exc.anomaly["status"] == "confirmed"

    # The first confirmer and time are untouched.
    rows = _anomaly_rows(db_pool, LIFT)
    assert len(rows) == 1
    assert rows[0]["status"] == "confirmed"
    assert rows[0]["confirmed_by"] == "下一班-B"
    assert rows[0]["confirmer_id"] == CONSOLE_B
    assert rows[0]["confirmed_at"].isoformat() == first["confirmed_at"]
    assert rows[0]["reported_by"] == "一席"


def test_concurrent_confirms_exactly_one_winner(services, db_pool):
    _execute_once(services, LIFT)
    _report(services, "异响", "一席", CONSOLE_A)
    n = 16
    barrier = threading.Barrier(n)
    results: dict[int, object] = {}

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=30)
            results[i] = anomalies.confirm(
                LIFT, f"接班席-{i}", f"console-confirm-{i:02d}"
            )
        except anomalies.AnomalyError as exc:
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    winners = [r for r in results.values() if not isinstance(r, Exception)]
    assert len(winners) == 1
    winner_id = winners[0]["anomaly"]["confirmer_id"]
    rows = _anomaly_rows(db_pool, LIFT)
    assert len(rows) == 1
    assert rows[0]["confirmer_id"] == winner_id
    # Every loser was told about that same record.
    losers = [r for r in results.values() if isinstance(r, Exception)]
    assert len(losers) == n - 1
    assert all(r.code == "anomaly_confirmed" for r in losers)
    assert all(r.anomaly["confirmer_id"] == winner_id for r in losers)


# ------------------------------------------------- append-only / new event


def test_reacquire_and_release_keep_record(services):
    out = _execute_once(services, LIFT)
    _report(services, "异响", "一席", CONSOLE_A)

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
    _report(services, "第一次执行后的异响", "一席", CONSOLE_A)
    _confirm(services, "下一班-B", CONSOLE_B)

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
    anomalies.report(
        LIFT, "other", "第二次执行后的新情况", "三席", CONSOLE_C
    )
    state = services.get_action_state(LIFT)
    assert state["anomaly"]["event_id"] == second["event_id"]
    assert state["anomaly"]["description"] == "第二次执行后的新情况"
    rows = _anomaly_rows(db_pool, LIFT)
    assert len(rows) == 2


def test_linked_execution_does_not_overwrite_old_records(services, db_pool):
    # An old anomaly exists on a previous single execution of the lift.
    old = _execute_once(services, LIFT)
    _report(services, "旧异常", "一席", CONSOLE_A)

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

    res = anomalies.report(
        LIFT, "operation", "联动后姿态偏差", "联排席", CONSOLE_A
    )
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
            "reporter_id": CONSOLE_A,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["anomaly"]["status"] == "pending"
    assert body["anomaly"]["reported_by"] == "升降台席"
    assert body["anomaly"]["reporter_id"] == CONSOLE_A
    assert body["state"]["anomaly"]["id"] == body["anomaly"]["id"]

    # Polling snapshot carries the pending record.
    state = client.get(f"/api/actions/{LIFT}").json()
    assert state["anomaly"]["category"] == "equipment"
    assert state["anomaly"]["confirmed_at"] is None

    # Another console confirms.
    r = client.post(
        f"/api/actions/{LIFT}/anomaly/confirm",
        json={"confirmer": "下一班-B", "confirmer_id": CONSOLE_B},
    )
    assert r.status_code == 200, r.text
    assert r.json()["anomaly"]["status"] == "confirmed"
    assert r.json()["anomaly"]["confirmed_by"] == "下一班-B"
    assert r.json()["anomaly"]["confirmer_id"] == CONSOLE_B


def test_http_blank_description_inline_error(client):
    token = client.post(
        f"/api/actions/{LIFT}/lease", json={"holder": "一席"}
    ).json()["token"]
    client.post(f"/api/actions/{LIFT}/execute", json={"token": token})

    r = client.post(
        f"/api/actions/{LIFT}/anomaly",
        json={
            "category": "equipment",
            "description": "  ",
            "reporter": "一席",
            "reporter_id": CONSOLE_A,
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_anomaly"
    assert "说明" in detail["message"]


def test_http_renaming_bypass_rejected_and_other_console_succeeds(client):
    """Same browser console renamed to the next shift still cannot confirm."""
    token = client.post(
        f"/api/actions/{LIFT}/lease", json={"holder": "报告席-A"}
    ).json()["token"]
    client.post(f"/api/actions/{LIFT}/execute", json={"token": token})
    client.post(
        f"/api/actions/{LIFT}/anomaly",
        json={
            "category": "equipment",
            "description": "异响",
            "reporter": "报告席-A",
            "reporter_id": CONSOLE_A,
        },
    )

    # The reporting console renames itself to "下一班-B" — the stable id
    # stays CONSOLE_A, so the server still rejects the acknowledgement.
    r = client.post(
        f"/api/actions/{LIFT}/anomaly/confirm",
        json={"confirmer": "下一班-B", "confirmer_id": CONSOLE_A},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "anomaly_self_confirm"
    assert detail["anomaly"]["status"] == "pending"
    assert detail["anomaly"]["confirmed_by"] is None
    assert detail["state"]["action_id"] == LIFT

    # The record is untouched and a genuinely different console confirms it.
    state = client.get(f"/api/actions/{LIFT}").json()
    assert state["anomaly"]["status"] == "pending"
    r = client.post(
        f"/api/actions/{LIFT}/anomaly/confirm",
        json={"confirmer": "下一班-B", "confirmer_id": CONSOLE_B},
    )
    assert r.status_code == 200, r.text
    assert r.json()["anomaly"]["confirmed_by"] == "下一班-B"


def test_http_repeat_report_and_confirm_carry_current_record(client):
    token = client.post(
        f"/api/actions/{LIFT}/lease", json={"holder": "一席"}
    ).json()["token"]
    client.post(f"/api/actions/{LIFT}/execute", json={"token": token})
    first = client.post(
        f"/api/actions/{LIFT}/anomaly",
        json={
            "category": "equipment",
            "description": "异响",
            "reporter": "一席",
            "reporter_id": CONSOLE_A,
        },
    ).json()["anomaly"]

    r = client.post(
        f"/api/actions/{LIFT}/anomaly",
        json={
            "category": "other",
            "description": "覆盖尝试",
            "reporter": "二席",
            "reporter_id": CONSOLE_B,
        },
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
        json={"confirmer": "下一班-B", "confirmer_id": CONSOLE_B},
    )
    r = client.post(
        f"/api/actions/{LIFT}/anomaly/confirm",
        json={"confirmer": "下一班-C", "confirmer_id": CONSOLE_C},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "anomaly_confirmed"
    assert detail["anomaly"]["confirmed_by"] == "下一班-B"
    assert detail["anomaly"]["confirmer_id"] == CONSOLE_B


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
        json={
            "category": "other",
            "description": "x",
            "reporter": "席",
            "reporter_id": CONSOLE_A,
        },
    )
    assert r.status_code == 404
    r = client.post(
        "/api/actions/nope/anomaly/confirm",
        json={"confirmer": "席", "confirmer_id": CONSOLE_B},
    )
    assert r.status_code == 404
