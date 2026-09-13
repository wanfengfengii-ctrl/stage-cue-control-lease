"""Linked execution: two dangerous actions committed as ONE atomic operation.

Covers the acceptance points:
  * success writes two events sharing one server-generated link id, each
    action's counter increments by exactly one;
  * historical single-action events keep a NULL link id;
  * swapping the item order in the request yields the same outcome;
  * a concurrent linked execute and single-action execute — only one commits;
  * one stale/expired/missing token aborts the whole operation with no side
    effects, and the error names the concrete failed action.
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app

LIFT = "lift_up"
LIFT_DOWN = "lift_down"
HOIST = "hoist_fly_in"


def _force_expiry(db_pool, action_id: str, at="now()"):
    with db_pool.get_pool().connection() as conn:
        conn.execute(
            f"UPDATE leases SET expires_at = {at} "
            f"WHERE id = (SELECT id FROM leases WHERE action_id = %s "
            f"            ORDER BY id DESC LIMIT 1)",
            (action_id,),
        )
        conn.commit()


def _events(db_pool, action_id: str):
    with db_pool.get_pool().connection() as conn:
        return conn.execute(
            "SELECT * FROM action_events WHERE action_id = %s ORDER BY id",
            (action_id,),
        ).fetchall()


def _acquire_pair(services, holder="联排控制席"):
    g_lift = services.acquire(LIFT, holder)
    g_hoist = services.acquire(HOIST, holder)
    return g_lift, g_hoist


# ---------------------------------------------------------------- success


def test_linked_execute_two_events_one_link_id(services, db_pool):
    g_lift, g_hoist = _acquire_pair(services)
    out = services.execute_linked(
        [
            {"action_id": LIFT, "token": g_lift["token"]},
            {"action_id": HOIST, "token": g_hoist["token"]},
        ]
    )
    assert out["linked"] is True
    link_id = out["link_id"]
    assert link_id and len(link_id) >= 16
    assert {e["action_id"] for e in out["events"]} == {LIFT, HOIST}

    lift_events = _events(db_pool, LIFT)
    hoist_events = _events(db_pool, HOIST)
    # Exactly one event per action, both stamped with the same link id.
    assert len(lift_events) == 1
    assert len(hoist_events) == 1
    assert lift_events[0]["link_id"] == link_id
    assert hoist_events[0]["link_id"] == link_id

    # Each action's counter incremented exactly once; both leases finished.
    lift_state = services.get_action_state(LIFT)
    hoist_state = services.get_action_state(HOIST)
    assert lift_state["event_count"] == 1
    assert hoist_state["event_count"] == 1
    assert lift_state["status"] == "free"
    assert hoist_state["status"] == "free"
    # The snapshot exposes the latest link id on BOTH cards.
    assert lift_state["last_link_id"] == link_id
    assert hoist_state["last_link_id"] == link_id

    with db_pool.get_pool().connection() as conn:
        unfinished = conn.execute(
            "SELECT count(*) AS c FROM leases "
            "WHERE action_id IN (%s, %s) "
            "AND released_at IS NULL AND executed_at IS NULL",
            (LIFT, HOIST),
        ).fetchone()["c"]
    assert unfinished == 0


def test_single_action_history_keeps_null_link_id(services, db_pool):
    g = services.acquire(LIFT, "seat-a")
    services.execute(LIFT, g["token"])
    (event,) = _events(db_pool, LIFT)
    assert event["link_id"] is None
    # A card whose history is only single-action executions shows no link id.
    assert services.get_action_state(LIFT)["last_link_id"] is None

    # A later linked run does not rewrite the historical NULL.
    g_lift, g_hoist = _acquire_pair(services)
    out = services.execute_linked(
        [
            {"action_id": LIFT, "token": g_lift["token"]},
            {"action_id": HOIST, "token": g_hoist["token"]},
        ]
    )
    events = _events(db_pool, LIFT)
    assert len(events) == 2
    assert events[0]["link_id"] is None
    assert events[1]["link_id"] == out["link_id"]
    assert services.get_action_state(LIFT)["last_link_id"] == out["link_id"]


def test_linked_order_in_request_is_irrelevant(services, db_pool):
    # First run: lift listed before hoist.
    g_lift, g_hoist = _acquire_pair(services)
    out1 = services.execute_linked(
        [
            {"action_id": LIFT, "token": g_lift["token"]},
            {"action_id": HOIST, "token": g_hoist["token"]},
        ]
    )
    # Second run: swapped order — same outcome, same fixed event order.
    g_lift2, g_hoist2 = _acquire_pair(services)
    out2 = services.execute_linked(
        [
            {"action_id": HOIST, "token": g_hoist2["token"]},
            {"action_id": LIFT, "token": g_lift2["token"]},
        ]
    )
    assert out1["linked"] is True and out2["linked"] is True
    assert out1["link_id"] != out2["link_id"]  # server-generated per run
    # Events are always emitted in the fixed (sorted) action order.
    assert [e["action_id"] for e in out1["events"]] == [HOIST, LIFT]
    assert [e["action_id"] for e in out2["events"]] == [HOIST, LIFT]
    assert services.get_action_state(LIFT)["event_count"] == 2
    assert services.get_action_state(HOIST)["event_count"] == 2
    for e in _events(db_pool, LIFT) + _events(db_pool, HOIST):
        assert e["link_id"] in (out1["link_id"], out2["link_id"])


# ------------------------------------------------------- concurrency


def test_concurrent_linked_vs_single_execute_only_one_commits(
    services, db_pool
):
    """A linked run and a single-action execute race on one shared action.

    Row locks serialise them: exactly one side commits; the loser is
    rejected with control_lost and writes nothing.
    """
    g_lift, g_hoist = _acquire_pair(services)
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def do_linked():
        try:
            barrier.wait(timeout=30)
            results["linked"] = services.execute_linked(
                [
                    {"action_id": LIFT, "token": g_lift["token"]},
                    {"action_id": HOIST, "token": g_hoist["token"]},
                ]
            )
        except services.LeaseError as exc:
            results["linked"] = exc

    def do_single():
        try:
            barrier.wait(timeout=30)
            results["single"] = services.execute(LIFT, g_lift["token"])
        except services.LeaseError as exc:
            results["single"] = exc

    threads = [threading.Thread(target=f) for f in (do_linked, do_single)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    linked_ok = not isinstance(results["linked"], services.LeaseError)
    single_ok = not isinstance(results["single"], services.LeaseError)
    assert linked_ok != single_ok, (
        f"exactly one side must commit, got linked={results['linked']!r} "
        f"single={results['single']!r}"
    )

    lift_events = _events(db_pool, LIFT)
    hoist_events = _events(db_pool, HOIST)
    if linked_ok:
        loser = results["single"]
        assert isinstance(loser, services.LeaseError)
        assert loser.code == "control_lost"
        assert len(lift_events) == 1 and len(hoist_events) == 1
        assert lift_events[0]["link_id"] == results["linked"]["link_id"]
        assert hoist_events[0]["link_id"] == results["linked"]["link_id"]
    else:
        loser = results["linked"]
        assert isinstance(loser, services.LeaseError)
        assert loser.code == "control_lost"
        # The rejection names the action that was lost under it.
        assert loser.action_id == LIFT
        # Single execute won: one event, no link id; the linked run left
        # the hoist lease completely untouched (still live and usable).
        assert len(lift_events) == 1
        assert lift_events[0]["link_id"] is None
        assert len(hoist_events) == 0
        assert services.get_action_state(HOIST)["status"] == "held"
        out = services.execute(HOIST, g_hoist["token"])
        assert out["executed"] is True


# ------------------------------------------------- atomic failure


def test_stale_token_aborts_whole_linked_execute(services, db_pool):
    """Old token after a takeover: nothing is written, nothing terminated."""
    old = services.acquire(LIFT, "失联席")
    _force_expiry(db_pool, LIFT)  # lease times out (server clock)
    services.acquire(LIFT, "接管席")  # a new seat takes over
    g_hoist = services.acquire(HOIST, "联排控制席")

    with pytest.raises(services.LeaseError) as ei:
        services.execute_linked(
            [
                {"action_id": LIFT, "token": old["token"]},
                {"action_id": HOIST, "token": g_hoist["token"]},
            ]
        )
    exc = ei.value
    assert exc.code == "control_lost"
    # The error names the concrete failed action.
    assert exc.action_id == LIFT

    # No side effects: zero events, and the VALID hoist lease is not
    # terminated — its token still works for a normal execute afterwards.
    assert _events(db_pool, LIFT) == []
    assert _events(db_pool, HOIST) == []
    state = services.get_action_state(HOIST)
    assert state["status"] == "held"
    assert state["holder"] == "联排控制席"
    out = services.execute(HOIST, g_hoist["token"])
    assert out["executed"] is True
    # The takeover lease on LIFT is equally untouched.
    assert services.get_action_state(LIFT)["holder"] == "接管席"


def test_expired_token_aborts_whole_linked_execute(services, db_pool):
    g_lift, g_hoist = _acquire_pair(services)
    _force_expiry(db_pool, HOIST)  # hoist token now expired server-side

    with pytest.raises(services.LeaseError) as ei:
        services.execute_linked(
            [
                {"action_id": LIFT, "token": g_lift["token"]},
                {"action_id": HOIST, "token": g_hoist["token"]},
            ]
        )
    assert ei.value.code == "control_lost"
    assert ei.value.action_id == HOIST
    # The still-valid lift lease must NOT be terminated by the failed run.
    assert _events(db_pool, LIFT) == []
    assert _events(db_pool, HOIST) == []
    assert services.get_action_state(LIFT)["status"] == "held"
    out = services.execute(LIFT, g_lift["token"])
    assert out["executed"] is True


def test_missing_token_names_the_action(services):
    g_lift, g_hoist = _acquire_pair(services)
    with pytest.raises(services.LeaseError) as ei:
        services.execute_linked(
            [
                {"action_id": LIFT, "token": g_lift["token"]},
                {"action_id": HOIST, "token": ""},
            ]
        )
    assert ei.value.code == "missing_token"
    assert ei.value.status == 401
    assert ei.value.action_id == HOIST
    assert services.get_action_state(LIFT)["status"] == "held"
    assert services.get_action_state(HOIST)["status"] == "held"


def test_invalid_linked_requests(services):
    with pytest.raises(services.LeaseError) as ei:
        services.execute_linked([{"action_id": LIFT, "token": "x"}])
    assert ei.value.code == "invalid_request"
    assert ei.value.status == 400

    with pytest.raises(services.LeaseError) as ei:
        services.execute_linked(
            [
                {"action_id": LIFT, "token": "x"},
                {"action_id": LIFT, "token": "y"},
            ]
        )
    assert ei.value.code == "invalid_request"

    with pytest.raises(services.LeaseError) as ei:
        services.execute_linked(
            [
                {"action_id": LIFT, "token": "x"},
                {"action_id": "nope", "token": "y"},
            ]
        )
    assert ei.value.code == "unknown_action"
    assert ei.value.status == 404
    assert ei.value.action_id == "nope"


def test_same_device_direction_pair_is_not_linkable(services, db_pool):
    """lift_up + lift_down must not form a linked cue.

    The two platform directions are mutually exclusive; linked execution is
    only for cross-device coordination.
    """
    g_up = services.acquire(LIFT, "联排控制席")
    g_down = services.acquire(LIFT_DOWN, "联排控制席")
    with pytest.raises(services.LeaseError) as ei:
        services.execute_linked(
            [
                {"action_id": LIFT, "token": g_up["token"]},
                {"action_id": LIFT_DOWN, "token": g_down["token"]},
            ]
        )
    assert ei.value.code == "invalid_request"
    assert ei.value.status == 400
    # Rejected before any write: no events, both leases still live.
    assert _events(db_pool, LIFT) == []
    assert _events(db_pool, LIFT_DOWN) == []
    assert services.get_action_state(LIFT)["status"] == "held"
    assert services.get_action_state(LIFT_DOWN)["status"] == "held"

    # Both leases still work individually; free the actions for the next leg.
    assert services.execute(LIFT, g_up["token"])["executed"] is True
    assert services.execute(LIFT_DOWN, g_down["token"])["executed"] is True

    # Two hoist directions are same-device too and equally non-linkable.
    g_in = services.acquire(HOIST, "联排控制席")
    g_out = services.acquire("hoist_fly_out", "联排控制席")
    with pytest.raises(services.LeaseError) as ei:
        services.execute_linked(
            [
                {"action_id": HOIST, "token": g_in["token"]},
                {"action_id": "hoist_fly_out", "token": g_out["token"]},
            ]
        )
    assert ei.value.code == "invalid_request"
    assert services.get_action_state(HOIST)["status"] == "held"
    assert services.get_action_state("hoist_fly_out")["status"] == "held"

    # A genuine cross-device pair still links: release the two hoist
    # directions, then take one of each device.
    services.release(HOIST, g_in["token"])
    services.release("hoist_fly_out", g_out["token"])
    g_lift2 = services.acquire(LIFT, "联排控制席")
    g_hoist2 = services.acquire(HOIST, "联排控制席")
    out = services.execute_linked(
        [
            {"action_id": HOIST, "token": g_hoist2["token"]},
            {"action_id": LIFT, "token": g_lift2["token"]},
        ]
    )
    assert out["linked"] is True
    assert {e["action_id"] for e in out["events"]} == {LIFT, HOIST}


def test_three_actions_is_not_linkable(services, db_pool):
    """Holding three actions and submitting all valid tokens executes none."""
    g_lift = services.acquire(LIFT, "联排控制席")
    g_down = services.acquire(LIFT_DOWN, "联排控制席")
    g_hoist = services.acquire(HOIST, "联排控制席")
    with pytest.raises(services.LeaseError) as ei:
        services.execute_linked(
            [
                {"action_id": LIFT, "token": g_lift["token"]},
                {"action_id": LIFT_DOWN, "token": g_down["token"]},
                {"action_id": HOIST, "token": g_hoist["token"]},
            ]
        )
    assert ei.value.code == "invalid_request"
    assert ei.value.status == 400
    # No side effects at all: zero events, all three leases still live.
    for aid in (LIFT, LIFT_DOWN, HOIST):
        assert _events(db_pool, aid) == []
        assert services.get_action_state(aid)["status"] == "held"


def test_last_executed_by_survives_reacquisition(services, db_pool):
    """After a linked run, re-acquiring a card keeps the last executor shown.

    The historical count stays and the "most recent executing seat" must
    remain the seat that ran the linked cue — the snapshot reads it from the
    latest action event, not from the newest lease row.
    """
    g_lift, g_hoist = _acquire_pair(services, holder="联排控制席")
    out = services.execute_linked(
        [
            {"action_id": LIFT, "token": g_lift["token"]},
            {"action_id": HOIST, "token": g_hoist["token"]},
        ]
    )

    # The same seat re-applies for control of one card.
    services.acquire(LIFT, "联排控制席")
    state = services.get_action_state(LIFT)
    assert state["event_count"] == 1
    assert state["last_executed_by"] == "联排控制席"
    assert state["last_link_id"] == out["link_id"]
    assert state["status"] == "held"
    assert state["holder"] == "联排控制席"

    # Even a takeover by a different seat keeps the historical executor.
    with db_pool.get_pool().connection() as conn:
        conn.execute(
            "UPDATE leases SET expires_at = now() WHERE action_id = %s",
            (LIFT,),
        )
        conn.commit()
    services.acquire(LIFT, "接管席")
    state = services.get_action_state(LIFT)
    assert state["holder"] == "接管席"
    assert state["last_executed_by"] == "联排控制席"
    assert state["event_count"] == 1
    assert state["last_link_id"] == out["link_id"]


def test_holder_with_surrounding_spaces_is_normalised(services):
    """A seat name with surrounding spaces behaves like the trimmed name."""
    grant = services.acquire(LIFT, "  联排控制席  ")
    assert grant["holder"] == "联排控制席"
    state = services.get_action_state(LIFT)
    assert state["holder"] == "联排控制席"
    # The raw token still authenticates the (normalised) lease.
    out = services.execute(LIFT, grant["token"])
    assert out["executed"] is True
    assert out["executed_by"] == "联排控制席"


# ------------------------------------------------------------- HTTP level


@pytest.fixture()
def client(db_pool):
    with TestClient(app) as c:
        yield c


def _http_acquire_pair(client):
    r = client.post(f"/api/actions/{LIFT}/lease", json={"holder": "联排控制席"})
    assert r.status_code == 200, r.text
    lift_token = r.json()["token"]
    r = client.post(f"/api/actions/{HOIST}/lease", json={"holder": "联排控制席"})
    assert r.status_code == 200, r.text
    hoist_token = r.json()["token"]
    return lift_token, hoist_token


def test_http_linked_execute_success(client):
    lift_token, hoist_token = _http_acquire_pair(client)
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
    body = r.json()
    assert body["linked"] is True
    assert body["link_id"]
    assert {e["action_id"] for e in body["events"]} == {LIFT, HOIST}
    for aid in (LIFT, HOIST):
        state = client.get(f"/api/actions/{aid}").json()
        assert state["event_count"] == 1
        assert state["last_link_id"] == body["link_id"]


def test_http_linked_stale_token_reports_failed_action(client, db_pool):
    old = client.post(
        f"/api/actions/{LIFT}/lease", json={"holder": "失联席"}
    ).json()["token"]
    _force_expiry(db_pool, LIFT)
    r = client.post(f"/api/actions/{LIFT}/lease", json={"holder": "接管席"})
    assert r.status_code == 200
    _, hoist_token = None, client.post(
        f"/api/actions/{HOIST}/lease", json={"holder": "联排控制席"}
    ).json()["token"]

    r = client.post(
        "/api/actions/execute-linked",
        json={
            "items": [
                {"action_id": LIFT, "token": old},
                {"action_id": HOIST, "token": hoist_token},
            ]
        },
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "control_lost"
    assert detail["action_id"] == LIFT
    # Fresh snapshots of both cards ride along for the UI.
    assert detail["states"][HOIST]["status"] == "held"
    assert detail["states"][LIFT]["holder"] == "接管席"
    # Nothing committed: no events, hoist lease still usable.
    assert client.get(f"/api/actions/{HOIST}").json()["event_count"] == 0
    assert client.get(f"/api/actions/{LIFT}").json()["event_count"] == 0
    r = client.post(
        f"/api/actions/{HOIST}/execute", json={"token": hoist_token}
    )
    assert r.status_code == 200


def test_http_linked_missing_token_is_401_and_side_effect_free(client):
    lift_token, _ = _http_acquire_pair(client)
    r = client.post(
        "/api/actions/execute-linked",
        json={
            "items": [
                {"action_id": LIFT, "token": lift_token},
                {"action_id": HOIST},
            ]
        },
    )
    assert r.status_code == 401
    assert r.json()["detail"]["action_id"] == HOIST
    assert client.get(f"/api/actions/{LIFT}").json()["status"] == "held"


def test_http_linked_requires_two_distinct_actions(client):
    lift_token, _ = _http_acquire_pair(client)
    r = client.post(
        "/api/actions/execute-linked",
        json={"items": [{"action_id": LIFT, "token": lift_token}]},
    )
    assert r.status_code == 400
    r = client.post(
        "/api/actions/execute-linked",
        json={
            "items": [
                {"action_id": LIFT, "token": lift_token},
                {"action_id": LIFT, "token": lift_token},
            ]
        },
    )
    assert r.status_code == 400
    # The single-action endpoints are untouched by all of this.
    assert client.get(f"/api/actions/{LIFT}").json()["status"] == "held"


def test_http_linked_rejects_same_device_pair_and_three_actions(client):
    # Hold BOTH lift directions plus a hoist action.
    up = client.post(
        f"/api/actions/{LIFT}/lease", json={"holder": "联排控制席"}
    ).json()["token"]
    down = client.post(
        f"/api/actions/{LIFT_DOWN}/lease", json={"holder": "联排控制席"}
    ).json()["token"]
    hoist = client.post(
        f"/api/actions/{HOIST}/lease", json={"holder": "联排控制席"}
    ).json()["token"]

    # Two directions of one device are not a linked combination.
    r = client.post(
        "/api/actions/execute-linked",
        json={
            "items": [
                {"action_id": LIFT, "token": up},
                {"action_id": LIFT_DOWN, "token": down},
            ]
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_request"

    # Three held actions submitted together with all valid tokens: rejected
    # as a whole — linked execution is exactly two cross-device actions.
    r = client.post(
        "/api/actions/execute-linked",
        json={
            "items": [
                {"action_id": LIFT, "token": up},
                {"action_id": LIFT_DOWN, "token": down},
                {"action_id": HOIST, "token": hoist},
            ]
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_request"

    # Nothing executed: all three leases are still held and eventless.
    for aid in (LIFT, LIFT_DOWN, HOIST):
        state = client.get(f"/api/actions/{aid}").json()
        assert state["status"] == "held"
        assert state["event_count"] == 0


def test_http_linked_last_executor_survives_reacquisition(client):
    lift_token, hoist_token = _http_acquire_pair(client)
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
    link_id = r.json()["link_id"]

    # Re-acquire one card after the linked cue: history and last executor and
    # the link id must all remain visible on the new live lease.
    r = client.post(
        f"/api/actions/{LIFT}/lease", json={"holder": "联排控制席"}
    )
    assert r.status_code == 200
    state = client.get(f"/api/actions/{LIFT}").json()
    assert state["status"] == "held"
    assert state["holder"] == "联排控制席"
    assert state["event_count"] == 1
    assert state["last_executed_by"] == "联排控制席"
    assert state["last_link_id"] == link_id
