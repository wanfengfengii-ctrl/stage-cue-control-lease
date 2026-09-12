"""End-to-end HTTP tests against the real FastAPI app + real PostgreSQL."""
from __future__ import annotations

import pytest

# db_pool fixture (conftest) configures DATABASE_URL before app startup.
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture()
def client(db_pool):
    with TestClient(app) as c:
        yield c


def test_actions_listing_shape(client):
    r = client.get("/api/actions")
    assert r.status_code == 200
    body = r.json()
    assert body["ttl_seconds"] == 30
    assert {a["action_id"] for a in body["actions"]} >= {"lift_up"}
    assert all(a["status"] == "free" for a in body["actions"])


def test_acquire_renew_execute_flow(client):
    r = client.post(
        "/api/actions/lift_up/lease", json={"holder": "升降台席"}
    )
    assert r.status_code == 200, r.text
    token = r.json()["token"]

    r = client.get("/api/actions/lift_up")
    assert r.json()["holder"] == "升降台席"

    r = client.post("/api/actions/lift_up/renew", json={"token": token})
    assert r.status_code == 200
    assert r.json()["state"]["holder"] == "升降台席"

    # Bearer auth is accepted as well.
    r = client.post(
        "/api/actions/lift_up/execute",
        headers={"Authorization": f"Bearer {token}"},
        json={"token": ""},
    )
    assert r.status_code == 200, r.text
    assert r.json()["executed"] is True

    r = client.get("/api/actions/lift_up")
    assert r.json()["status"] == "free"
    assert r.json()["event_count"] == 1


def test_competing_acquires_one_winner_http(client):
    r1 = client.post("/api/actions/lift_up/lease", json={"holder": "A"})
    assert r1.status_code == 200
    r2 = client.post("/api/actions/lift_up/lease", json={"holder": "B"})
    assert r2.status_code == 409
    detail = r2.json()["detail"]
    assert detail["code"] == "lease_held"
    # Rejection carries fresh state so the loser can render the holder.
    assert detail["state"]["holder"] == "A"


def test_old_token_rejected_after_takeover_http(client, db_pool):
    r = client.post(
        "/api/actions/hoist_fly_in/lease", json={"holder": "失联席"}
    )
    old = r.json()["token"]

    # Simulate 30 s passing.
    with db_pool.get_pool().connection() as conn:
        conn.execute(
            "UPDATE leases SET expires_at = now() "
            "WHERE action_id='hoist_fly_in'"
        )
        conn.commit()

    r = client.post(
        "/api/actions/hoist_fly_in/lease", json={"holder": "接管席"}
    )
    assert r.status_code == 200

    for path in ("renew", "release", "execute"):
        r = client.post(
            f"/api/actions/hoist_fly_in/{path}", json={"token": old}
        )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "control_lost"

    # New lease still healthy; old page sees it no longer holds anything.
    r = client.get("/api/actions/hoist_fly_in")
    assert r.json()["holder"] == "接管席"


def test_missing_token_rejected(client):
    r = client.post(
        "/api/actions/lift_up/renew",
        json={"token": ""},
    )
    assert r.status_code == 401
