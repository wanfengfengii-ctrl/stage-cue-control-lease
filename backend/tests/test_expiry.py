"""Expiry boundary: server-side UTC clock, equality means expired."""
from __future__ import annotations

from datetime import timedelta

import pytest


def _force_expiry(db_pool, action_id: str, at="now()"):
    """Set the latest lease's expires_at to a SQL expression (default now())."""
    with db_pool.get_pool().connection() as conn:
        conn.execute(
            f"UPDATE leases SET expires_at = {at} "
            f"WHERE id = (SELECT id FROM leases WHERE action_id = %s "
            f"            ORDER BY id DESC LIMIT 1)",
            (action_id,),
        )
        conn.commit()


def test_boundary_now_equals_expires_is_invalid(db_pool):
    """The exact boundary: expires_at == server now() => already expired."""
    from app import db as db_mod
    from datetime import datetime, timezone

    t = datetime(2026, 9, 12, 15, 0, 0, tzinfo=timezone.utc)
    base = {
        "id": "lift_up",
        "label": "x",
        "lease_id": 1,
        "token_hash": "h",
        "holder": "seat",
        "acquired_at": t - timedelta(seconds=30),
        "expires_at": None,
        "released_at": None,
        "executed_at": None,
        "event_count": 0,
    }

    # one microsecond before expiry -> still held
    almost = {**base, "expires_at": t}
    assert db_mod.state_from_row(almost, t - timedelta(microseconds=1))[
        "status"
    ] == "held"
    # exactly equal -> invalid (the required inclusive boundary)
    assert db_mod.state_from_row(almost, t)["status"] == "free"
    # one microsecond after -> invalid
    assert db_mod.state_from_row(almost, t + timedelta(microseconds=1))[
        "status"
    ] == "free"


def test_lease_at_database_boundary_reports_free(services, db_pool):
    """Force expires_at = now() inside one transaction; a later read is free."""
    services.acquire("lift_up", "seat-a")
    with db_pool.get_pool().connection() as conn:
        conn.execute(
            "UPDATE leases SET expires_at = now() "
            "WHERE action_id = 'lift_up'"
        )
        conn.commit()
    state = services.get_action_state("lift_up")
    assert state["status"] == "free"
    assert state["holder"] is None


def test_expired_lease_cannot_renew_release_execute(services, db_pool):
    granted = services.acquire("lift_up", "seat-a")
    _force_expiry(db_pool, "lift_up")  # expires_at = now()

    for op in (services.renew, services.release, services.execute):
        with pytest.raises(services.LeaseError) as ei:
            op("lift_up", granted["token"])
        assert ei.value.code == "control_lost"


def test_new_seat_takes_over_immediately_after_expiry(services, db_pool):
    old = services.acquire("lift_up", "失联席")
    _force_expiry(db_pool, "lift_up")

    new = services.acquire("lift_up", "接管席")
    assert new["token"] != old["token"]
    state = services.get_action_state("lift_up")
    assert state["status"] == "held"
    assert state["holder"] == "接管席"


def test_old_token_late_ops_rejected_and_new_lease_untouched(services, db_pool):
    """A reconnected old seat must never affect the new lease."""
    old = services.acquire("hoist_fly_in", "失联席")
    _force_expiry(db_pool, "hoist_fly_in")
    new = services.acquire("hoist_fly_in", "接管席")

    with db_pool.get_pool().connection() as conn:
        before = conn.execute(
            "SELECT * FROM leases WHERE action_id='hoist_fly_in' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

    for op in (services.renew, services.release):
        with pytest.raises(services.LeaseError) as ei:
            op("hoist_fly_in", old["token"])
        assert ei.value.code == "control_lost"
    with pytest.raises(services.LeaseError):
        services.execute("hoist_fly_in", old["token"])

    with db_pool.get_pool().connection() as conn:
        after = conn.execute(
            "SELECT * FROM leases WHERE action_id='hoist_fly_in' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        events = conn.execute(
            "SELECT count(*) AS c FROM action_events "
            "WHERE action_id='hoist_fly_in'"
        ).fetchone()["c"]

    # New lease completely untouched by the old token's late requests.
    assert after["id"] == before["id"]
    assert after["token_hash"] == before["token_hash"]
    assert after["expires_at"] == before["expires_at"]
    assert after["released_at"] is None
    assert after["executed_at"] is None
    assert events == 0
    state = services.get_action_state("hoist_fly_in")
    assert state["holder"] == "接管席"

    # The current token still works normally.
    services.execute("hoist_fly_in", new["token"])
    assert services.get_action_state("hoist_fly_in")["event_count"] == 1


def test_wrong_token_on_live_lease_rejected(services):
    services.acquire("lift_down", "seat-a")
    other = services  # noqa: F841
    fake = "not-a-real-token"
    for op in (services.renew, services.release, services.execute):
        with pytest.raises(services.LeaseError) as ei:
            op("lift_down", fake)
        assert ei.value.code == "control_lost"


def test_shortened_lease_expires_then_takeover(services, db_pool):
    """Force expiry one second in the past; takeover is immediate."""
    services.acquire("emergency_stop", "seat-a")
    _force_expiry(db_pool, "emergency_stop", "now() - interval '1 second'")
    assert services.get_action_state("emergency_stop")["status"] == "free"
    granted = services.acquire("emergency_stop", "seat-b")
    assert granted["state"]["holder"] == "seat-b"


def test_renew_extends_by_thirty_seconds(services, db_pool):
    from app import config

    granted = services.acquire("lift_up", "seat-a")
    # Backdate by 10 s to prove renew recomputes from the server clock.
    _force_expiry(db_pool, "lift_up", "now() + interval '20 seconds'")
    out = services.renew("lift_up", granted["token"])
    with db_pool.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT * FROM leases WHERE action_id='lift_up' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        now = conn.execute("SELECT now() AS n").fetchone()["n"]
    delta = (row["expires_at"] - now).total_seconds()
    assert 29 <= delta <= 30
    assert out["ttl_seconds"] == config.LEASE_TTL_SECONDS
