"""Lease lifecycle: renew/release/execute and write-once event semantics."""
from __future__ import annotations

import pytest


def test_grant_returns_unpredictable_token(services):
    a = services.acquire("lift_up", "seat-a")
    b = services.acquire("lift_down", "seat-a")
    assert a["token"] != b["token"]
    assert len(a["token"]) >= 40  # 256-bit urlsafe base64 -> ~43 chars
    assert a["ttl_seconds"] == 30
    # Only a hash is stored in the database; the raw token must not appear.
    from app import db as db_mod

    with db_mod.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT token_hash FROM leases WHERE action_id='lift_up' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert a["token"] not in row["token_hash"]
    assert row["token_hash"] == services.hash_token(a["token"])


def test_release_frees_action_and_token_stops_working(services):
    g = services.acquire("lift_up", "seat-a")
    out = services.release("lift_up", g["token"])
    assert out["released"] is True
    assert services.get_action_state("lift_up")["status"] == "free"
    for op in (services.renew, services.release, services.execute):
        with pytest.raises(services.LeaseError):
            op("lift_up", g["token"])


def test_renew_only_for_current_token(services):
    services.acquire("lift_up", "seat-a")
    with pytest.raises(services.LeaseError):
        services.renew("lift_up", "stale-or-foreign-token")


def test_execute_writes_exactly_one_event(services, db_pool):
    g = services.acquire("hoist_fly_out", "seat-a")
    out = services.execute("hoist_fly_out", g["token"])
    assert out["executed"] is True
    assert out["executed_by"] == "seat-a"

    # Re-execute with the same (consumed) token is refused, no second event.
    with pytest.raises(services.LeaseError):
        services.execute("hoist_fly_out", g["token"])

    with db_pool.get_pool().connection() as conn:
        events = conn.execute(
            "SELECT * FROM action_events WHERE action_id='hoist_fly_out'"
        ).fetchall()
        lease = conn.execute(
            "SELECT * FROM leases WHERE action_id='hoist_fly_out' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert len(events) == 1
    assert events[0]["holder"] == "seat-a"
    assert lease["executed_at"] is not None

    # After execution the action becomes free again for the next operator.
    assert services.get_action_state("hoist_fly_out")["status"] == "free"
    g2 = services.acquire("hoist_fly_out", "seat-b")
    out2 = services.execute("hoist_fly_out", g2["token"])
    assert out2["event_id"] != out["event_id"]
    assert services.get_action_state("hoist_fly_out")["event_count"] == 2


def test_unknown_action(services):
    with pytest.raises(services.LeaseError) as ei:
        services.acquire("nope", "seat")
    assert ei.value.status == 404
