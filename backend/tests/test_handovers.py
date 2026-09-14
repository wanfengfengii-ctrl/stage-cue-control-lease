"""Shift handovers (换班交接): one-time code control transfer of a live lease.

Covers the acceptance points:
  * the holder initiates while the lease is live and gets a one-time code;
    only its hash is stored; until the code is redeemed the original seat
    can still renew, release and execute;
  * the receiving console redeems the code in one transaction: the old
    lease is terminated, a fresh token is returned ONLY to the receiver,
    and the handover record moves pending -> accepted;
  * after a successful transfer the OLD token is dead (renew/release/
    execute all 409 control_lost) and no action event was written;
  * handover records ride the action snapshot with status pending /
    accepted / invalidated; a pending record whose lease was released,
    executed, expired or superseded is judged invalidated at query time
    and never blocks a fresh application;
  * wrong code, initiator losing control, and the lease expiring exactly
    at accept time are all recognisable business errors; every failure
    creates no lease;
  * acceptance racing the original seat's execution commits exactly one
    side; concurrent accepts admit exactly one receiver;
  * the HTTP error envelope matches every other business error
    (detail.code / detail.message / detail.state, plus detail.handover).
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import handovers
from app.main import app

LIFT = "lift_up"
HOIST = "hoist_fly_in"

CONSOLE_A = "console-aaaa-1111"
CONSOLE_B = "console-bbbb-2222"
CONSOLE_C = "console-cccc-3333"


# ------------------------------------------------------------- helpers


def _acquire_and_initiate(services, action_id=LIFT, holder="交班席",
                          console_id=CONSOLE_A):
    grant = services.acquire(action_id, holder)
    out = handovers.initiate(action_id, grant["token"], console_id)
    return grant, out


def _force_expiry(db_pool, action_id: str, at="now()"):
    with db_pool.get_pool().connection() as conn:
        conn.execute(
            f"UPDATE leases SET expires_at = {at} "
            f"WHERE id = (SELECT id FROM leases WHERE action_id = %s "
            f"            ORDER BY id DESC LIMIT 1)",
            (action_id,),
        )
        conn.commit()


def _handover_rows(db_pool, action_id=LIFT):
    with db_pool.get_pool().connection() as conn:
        return conn.execute(
            "SELECT * FROM lease_handovers WHERE action_id = %s ORDER BY id",
            (action_id,),
        ).fetchall()


def _lease_rows(db_pool, action_id=LIFT):
    with db_pool.get_pool().connection() as conn:
        return conn.execute(
            "SELECT * FROM leases WHERE action_id = %s ORDER BY id",
            (action_id,),
        ).fetchall()


@pytest.fixture()
def client(db_pool):
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------ initiate


def test_initiate_returns_one_time_code_stores_only_hash(services, db_pool):
    grant, out = _acquire_and_initiate(services)
    code = out["code"]
    record = out["handover"]
    assert len(code) == handovers.CODE_LENGTH
    assert record["status"] == "pending"
    assert record["initiator"] == "交班席"
    assert record["initiator_id"] == CONSOLE_A
    assert record["accepted_by"] is None
    assert record["accepted_at"] is None

    rows = _handover_rows(db_pool)
    assert len(rows) == 1
    assert rows[0]["code_hash"] == handovers._hash_code(code)
    assert code not in rows[0]["code_hash"]

    # The lease is untouched: still the same live lease on the snapshot.
    state = services.get_action_state(LIFT)
    assert state["status"] == "held"
    assert state["holder"] == "交班席"
    assert state["handover"]["status"] == "pending"
    assert state["handover"]["id"] == record["id"]
    assert grant["token"]  # still the valid credential


def test_initiate_keeps_original_seat_in_control(services):
    """Until acceptance the original seat renews/releases/executes freely."""
    grant, _ = _acquire_and_initiate(services)
    out = services.renew(LIFT, grant["token"])
    assert out["state"]["holder"] == "交班席"
    # And executing still works, which ends the handover's meaning.
    done = services.execute(LIFT, grant["token"])
    assert done["executed"] is True
    state = services.get_action_state(LIFT)
    assert state["status"] == "free"
    # The pending record is judged invalidated at query time.
    assert state["handover"]["status"] == "invalidated"


def test_initiate_requires_current_token(services):
    services.acquire(LIFT, "交班席")
    with pytest.raises(Exception) as ei:
        handovers.initiate(LIFT, "not-the-token", CONSOLE_A)
    assert ei.value.code == "control_lost"
    with pytest.raises(Exception) as ei:
        handovers.initiate(LIFT, "", CONSOLE_A)
    assert ei.value.code == "missing_token"
    with pytest.raises(Exception) as ei:
        handovers.initiate("nope", "whatever", CONSOLE_A)
    assert ei.value.code == "unknown_action"


def test_reinitiate_replaces_pending_code(services, db_pool):
    grant, first = _acquire_and_initiate(services)
    second = handovers.initiate(LIFT, grant["token"], CONSOLE_A)
    assert second["code"] != first["code"]

    rows = _handover_rows(db_pool)
    assert [r["status"] for r in rows] == ["invalidated", "pending"]

    # The old code is dead; the new one works.
    with pytest.raises(handovers.HandoverError) as ei:
        handovers.accept(LIFT, first["code"], "接班席", CONSOLE_B)
    assert ei.value.code == "handover_code_invalid"
    out = handovers.accept(LIFT, second["code"], "接班席", CONSOLE_B)
    assert out["handover"]["status"] == "accepted"


def test_code_normalisation(services):
    _, out = _acquire_and_initiate(services)
    code = out["code"]
    spaced = f" {code[:4]}-{code[4:].lower()} "
    accepted = handovers.accept(LIFT, spaced, "接班席", CONSOLE_B)
    assert accepted["holder"] == "接班席"


# -------------------------------------------------------------- accept


def test_accept_transfers_control_and_kills_old_token(services, db_pool):
    grant, out = _acquire_and_initiate(services)
    accepted = handovers.accept(LIFT, out["code"], "接班席-B", CONSOLE_B)

    # New lease issued only to the receiver; old lease terminated.
    assert accepted["token"] != grant["token"]
    assert accepted["holder"] == "接班席-B"
    assert accepted["handover"]["status"] == "accepted"
    assert accepted["handover"]["accepted_by"] == "接班席-B"
    assert accepted["handover"]["accepted_id"] == CONSOLE_B
    assert accepted["handover"]["accepted_at"] is not None

    leases = _lease_rows(db_pool)
    assert len(leases) == 2
    old, new = leases
    assert old["released_at"] is not None and old["executed_at"] is None
    assert new["released_at"] is None and new["executed_at"] is None
    assert new["holder"] == "接班席-B"
    rows = _handover_rows(db_pool)
    assert rows[0]["status"] == "accepted"
    assert rows[0]["new_lease_id"] == new["id"]

    # The OLD token is dead for every operation.
    for op in (services.renew, services.release, services.execute):
        with pytest.raises(services.LeaseError) as ei:
            op(LIFT, grant["token"])
        assert ei.value.code == "control_lost"

    # No action event was written by the transfer itself.
    state = services.get_action_state(LIFT)
    assert state["event_count"] == 0
    assert state["holder"] == "接班席-B"
    assert state["handover"]["status"] == "accepted"

    # The NEW token works: the receiver really holds control now.
    done = services.execute(LIFT, accepted["token"])
    assert done["executed_by"] == "接班席-B"
    assert services.get_action_state(LIFT)["event_count"] == 1


def test_accept_wrong_code_keeps_pending_and_creates_no_lease(
    services, db_pool
):
    _, out = _acquire_and_initiate(services)
    with pytest.raises(handovers.HandoverError) as ei:
        handovers.accept(LIFT, "WRONGCODE", "接班席", CONSOLE_B)
    assert ei.value.code == "handover_code_invalid"
    assert ei.value.status == 409

    # Nothing changed: still pending, still exactly one lease.
    assert len(_lease_rows(db_pool)) == 1
    rows = _handover_rows(db_pool)
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    # The right code still works afterwards.
    handovers.accept(LIFT, out["code"], "接班席", CONSOLE_B)


def test_accept_without_pending_is_not_found(services):
    services.acquire(LIFT, "持有者")
    with pytest.raises(handovers.HandoverError) as ei:
        handovers.accept(LIFT, "WHATEVER", "接班席", CONSOLE_B)
    assert ei.value.code == "handover_not_found"


def test_accept_rejects_blank_recipient(services, db_pool):
    _, out = _acquire_and_initiate(services)
    for recipient, rid in (("", CONSOLE_B), ("  ", CONSOLE_B),
                           ("接班席", ""), ("接班席", "  ")):
        with pytest.raises(handovers.HandoverError) as ei:
            handovers.accept(LIFT, out["code"], recipient, rid)
        assert ei.value.code == "invalid_handover"
        assert ei.value.status == 400
    # Every rejection left the pending record and the single lease intact.
    assert len(_lease_rows(db_pool)) == 1
    assert _handover_rows(db_pool)[0]["status"] == "pending"


def test_accept_by_initiating_console_rejected(services, db_pool):
    """The initiating console cannot redeem its own code (shift = 2 consoles)."""
    _, out = _acquire_and_initiate(services)
    with pytest.raises(handovers.HandoverError) as ei:
        handovers.accept(LIFT, out["code"], "交班席", CONSOLE_A)
    assert ei.value.code == "handover_self_accept"
    # Still pending, no new lease; another console may still accept.
    assert len(_lease_rows(db_pool)) == 1
    handovers.accept(LIFT, out["code"], "接班席", CONSOLE_B)


def test_accept_after_release_is_invalid_and_frees_nothing(services, db_pool):
    grant, out = _acquire_and_initiate(services)
    services.release(LIFT, grant["token"])

    with pytest.raises(handovers.HandoverError) as ei:
        handovers.accept(LIFT, out["code"], "接班席", CONSOLE_B)
    assert ei.value.code == "handover_invalid"
    assert "释放" in ei.value.message

    # The failure created no lease; the stored row is still 'pending' but
    # every query judges it invalidated (随查询判为失效), and a normal
    # application proceeds unaffected.
    assert len(_lease_rows(db_pool)) == 1
    assert _handover_rows(db_pool)[0]["status"] == "pending"
    state = services.get_action_state(LIFT)
    assert state["status"] == "free"
    assert state["handover"]["status"] == "invalidated"
    services.acquire(LIFT, "下一席")


def test_accept_after_execute_is_invalid(services, db_pool):
    grant, out = _acquire_and_initiate(services)
    services.execute(LIFT, grant["token"])
    with pytest.raises(handovers.HandoverError) as ei:
        handovers.accept(LIFT, out["code"], "接班席", CONSOLE_B)
    assert ei.value.code == "handover_invalid"
    assert "执行" in ei.value.message
    assert len(_lease_rows(db_pool)) == 1
    assert services.get_action_state(LIFT)["event_count"] == 1


def test_accept_at_exact_expiry_is_recognisable_and_action_reacquirable(
    services, db_pool
):
    """接收时租约刚好到期：可识别原因，且不产生新租约，动作可重新申请。"""
    _, out = _acquire_and_initiate(services)
    _force_expiry(db_pool, LIFT)  # expires_at = now(): equality == expired

    with pytest.raises(handovers.HandoverError) as ei:
        handovers.accept(LIFT, out["code"], "接班席", CONSOLE_B)
    assert ei.value.code == "handover_expired"

    # No new lease; the stored row is still 'pending' but reads as
    # invalidated, and the action is free and re-acquirable right away.
    assert len(_lease_rows(db_pool)) == 1
    assert _handover_rows(db_pool)[0]["status"] == "pending"
    state = services.get_action_state(LIFT)
    assert state["status"] == "free"
    assert state["handover"]["status"] == "invalidated"
    grant = services.acquire(LIFT, "重新申请席")
    assert services.get_action_state(LIFT)["holder"] == "重新申请席"
    assert grant["token"]


def test_accept_after_superseded_lease_is_invalid(services, db_pool):
    """The initiating lease was replaced by a newer one (expiry + acquire)."""
    _, out = _acquire_and_initiate(services)
    _force_expiry(db_pool, LIFT)
    services.acquire(LIFT, "第三方席")  # supersedes the initiating lease

    with pytest.raises(handovers.HandoverError) as ei:
        handovers.accept(LIFT, out["code"], "接班席", CONSOLE_B)
    assert ei.value.code == "handover_invalid"
    # The third party's lease is completely untouched.
    state = services.get_action_state(LIFT)
    assert state["holder"] == "第三方席"
    assert len(_lease_rows(db_pool)) == 2
    assert state["handover"]["status"] == "invalidated"


def test_pending_record_invalidated_at_query_time(services, db_pool):
    """随查询判为失效：release/execute/expiry/supersede all read as invalidated."""
    # expiry case (no mutation of the handover row at all)
    _, _ = _acquire_and_initiate(services)
    _force_expiry(db_pool, LIFT)
    state = services.get_action_state(LIFT)
    assert state["handover"]["status"] == "invalidated"
    # The stored row is still pending; the NEXT initiate cleans it up.
    assert _handover_rows(db_pool)[0]["status"] == "pending"
    grant = services.acquire(LIFT, "新持有席")
    out = handovers.initiate(LIFT, grant["token"], CONSOLE_C)
    assert out["handover"]["status"] == "pending"
    rows = _handover_rows(db_pool)
    assert [r["status"] for r in rows] == ["invalidated", "pending"]


def test_snapshot_exposes_no_code(services):
    _, _ = _acquire_and_initiate(services)
    state = services.get_action_state(LIFT)
    assert "code" not in state["handover"]
    assert "code_hash" not in state["handover"]


# ---------------------------------------------------------- concurrency


def test_accept_racing_execute_exactly_one_commits(services, db_pool):
    """接收与原席执行并发：行锁串行化，仅一方成功，另一方得到可识别拒绝。"""
    grant, out = _acquire_and_initiate(services)
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def do_accept():
        barrier.wait(timeout=30)
        try:
            results["accept"] = handovers.accept(
                LIFT, out["code"], "接班席", CONSOLE_B
            )
        except handovers.HandoverError as exc:
            results["accept_error"] = exc

    def do_execute():
        barrier.wait(timeout=30)
        try:
            results["execute"] = services.execute(LIFT, grant["token"])
        except services.LeaseError as exc:
            results["execute_error"] = exc

    threads = [
        threading.Thread(target=do_accept),
        threading.Thread(target=do_execute),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    # Exactly one side committed.
    accept_ok = "accept" in results
    execute_ok = "execute" in results
    assert accept_ok != execute_ok, results

    if accept_ok:
        # Execute lost: the old token died with the transferred lease.
        assert results["execute_error"].code == "control_lost"
        assert services.get_action_state(LIFT)["holder"] == "接班席"
        assert services.get_action_state(LIFT)["event_count"] == 0
    else:
        # Accept lost: the lease was executed before the code was redeemed.
        assert results["accept_error"].code == "handover_invalid"
        assert services.get_action_state(LIFT)["event_count"] == 1

    # Database invariants: handover accepted xor event written; the number
    # of leases matches exactly the committed outcome.  A losing accept
    # leaves the stored row 'pending' — judged invalidated at query time.
    leases = _lease_rows(db_pool)
    rows = _handover_rows(db_pool)
    if accept_ok:
        assert len(leases) == 2
        assert rows[0]["status"] == "accepted"
    else:
        assert len(leases) == 1
        assert rows[0]["status"] == "pending"
        assert (
            services.get_action_state(LIFT)["handover"]["status"]
            == "invalidated"
        )


def test_concurrent_accepts_exactly_one_receiver(services, db_pool):
    _, out = _acquire_and_initiate(services)
    n = 8
    barrier = threading.Barrier(n)
    wins: list[dict] = []
    losses: list[Exception] = []

    def worker(i: int):
        barrier.wait(timeout=30)
        try:
            wins.append(
                handovers.accept(LIFT, out["code"], f"接班席-{i}", f"console-{i}")
            )
        except handovers.HandoverError as exc:
            losses.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(wins) == 1
    assert len(losses) == n - 1
    assert all(exc.code == "handover_not_found" for exc in losses)
    # Exactly two leases (initiating + replacement) and one accepted record.
    assert len(_lease_rows(db_pool)) == 2
    assert _handover_rows(db_pool)[0]["status"] == "accepted"


# ---------------------------------------------------------------- HTTP


def test_http_full_handover_flow(client):
    r = client.post(f"/api/actions/{LIFT}/lease", json={"holder": "交班席"})
    assert r.status_code == 200
    token = r.json()["token"]

    r = client.post(
        f"/api/actions/{LIFT}/handover",
        json={"token": token, "initiator_id": CONSOLE_A},
    )
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    assert r.json()["handover"]["status"] == "pending"
    assert r.json()["state"]["handover"]["status"] == "pending"

    # Polling snapshot carries the pending record (without the code).
    r = client.get("/api/actions")
    card = next(a for a in r.json()["actions"] if a["action_id"] == LIFT)
    assert card["handover"]["status"] == "pending"
    assert "code" not in card["handover"]

    r = client.post(
        f"/api/actions/{LIFT}/handover/accept",
        json={"code": code, "recipient": "接班席",
              "recipient_id": CONSOLE_B},
    )
    assert r.status_code == 200, r.text
    new_token = r.json()["token"]
    assert new_token != token
    assert r.json()["handover"]["status"] == "accepted"

    # Old token is dead over HTTP as well.
    r = client.post(f"/api/actions/{LIFT}/execute", json={"token": token})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "control_lost"

    # New token executes.
    r = client.post(
        f"/api/actions/{LIFT}/execute", json={"token": new_token}
    )
    assert r.status_code == 200
    assert r.json()["executed_by"] == "接班席"


def test_http_errors_carry_code_message_state_and_record(client, db_pool):
    r = client.post(f"/api/actions/{HOIST}/lease", json={"holder": "交班席"})
    token = r.json()["token"]
    r = client.post(
        f"/api/actions/{HOIST}/handover",
        json={"token": token, "initiator_id": CONSOLE_A},
    )
    code = r.json()["code"]

    # Wrong code: recognisable, state + record ride along, nothing changes.
    r = client.post(
        f"/api/actions/{HOIST}/handover/accept",
        json={"code": "BADCODE1", "recipient": "接班席",
              "recipient_id": CONSOLE_B},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "handover_code_invalid"
    assert detail["message"]
    assert detail["state"]["holder"] == "交班席"
    assert detail["handover"]["status"] == "pending"

    # Expire the lease: the accept is rejected with the dedicated code.
    with db_pool.get_pool().connection() as conn:
        conn.execute(
            "UPDATE leases SET expires_at = now() WHERE action_id = %s",
            (HOIST,),
        )
        conn.commit()
    r = client.post(
        f"/api/actions/{HOIST}/handover/accept",
        json={"code": code, "recipient": "接班席",
              "recipient_id": CONSOLE_B},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "handover_expired"
    assert detail["handover"]["status"] == "invalidated"
    assert detail["state"]["status"] == "free"

    # The action can be applied for normally right away.
    r = client.post(f"/api/actions/{HOIST}/lease", json={"holder": "新席"})
    assert r.status_code == 200


def test_http_initiate_with_stale_token_is_control_lost(client):
    r = client.post(f"/api/actions/{LIFT}/lease", json={"holder": "一席"})
    token = r.json()["token"]
    client.post(f"/api/actions/{LIFT}/release", json={"token": token})
    r = client.post(
        f"/api/actions/{LIFT}/handover",
        json={"token": token, "initiator_id": CONSOLE_A},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "control_lost"
    assert r.json()["detail"]["state"]["status"] == "free"
