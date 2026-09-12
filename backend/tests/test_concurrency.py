"""Concurrent acquisition: exactly one winner under real transactions."""
from __future__ import annotations

import hashlib
import secrets
import threading
from datetime import timedelta

import pytest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app import config


def test_concurrent_acquire_only_one_winner(services, db_pool):
    """N threads race on one action through the service; one lease granted.

    Pool connections are checked out per thread, so this is genuine
    cross-connection contention resolved by SELECT ... FOR UPDATE.
    """
    n = 24
    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=30)
            res = services.acquire("lift_up", f"seat-{i}")
            results.append(res)
        except services.LeaseError as exc:
            assert exc.code == "lease_held"
            errors.append(exc)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(results) == 1, f"expected one winner, got {len(results)}"
    assert len(errors) == n - 1

    # Database-level invariant: exactly one live, one total lease row.
    with db_pool.get_pool().connection() as conn:
        live = conn.execute(
            "SELECT count(*) AS c FROM leases "
            "WHERE action_id = 'lift_up' "
            "AND released_at IS NULL AND executed_at IS NULL "
            "AND now() < expires_at"
        ).fetchone()["c"]
        total = conn.execute(
            "SELECT count(*) AS c FROM leases WHERE action_id = 'lift_up'"
        ).fetchone()["c"]
    assert live == 1
    assert total == 1


def test_concurrent_acquire_from_independent_pools(services, db_pool):
    """Each racer owns a separate pool (as separate uvicorn workers would)."""
    n = 16
    wins: list[int] = []
    rejects: list[int] = []
    barrier = threading.Barrier(n)
    lock = threading.Lock()

    def worker(i: int) -> None:
        pool = ConnectionPool(
            config.DATABASE_URL,
            min_size=1,
            max_size=2,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        pool.open(wait=True)
        try:
            barrier.wait(timeout=30)
            with pool.connection() as conn:
                with conn.transaction():
                    locked = conn.execute(
                        "SELECT id FROM actions WHERE id = %s FOR UPDATE",
                        ("hoist_fly_in",),
                    ).fetchone()
                    assert locked is not None
                    now = conn.execute(
                        "SELECT now() AS n"
                    ).fetchone()["n"]
                    latest = conn.execute(
                        "SELECT * FROM leases WHERE action_id = %s "
                        "ORDER BY id DESC LIMIT 1",
                        ("hoist_fly_in",),
                    ).fetchone()
                    busy = (
                        latest is not None
                        and latest["released_at"] is None
                        and latest["executed_at"] is None
                        and now < latest["expires_at"]
                    )
                    if busy:
                        with lock:
                            rejects.append(i)
                        return
                    conn.execute(
                        "INSERT INTO leases(action_id, token_hash, holder, "
                        "acquired_at, expires_at) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (
                            "hoist_fly_in",
                            hashlib.sha256(secrets.token_bytes(8)).hexdigest(),
                            f"seat-{i}",
                            now,
                            now + timedelta(seconds=30),
                        ),
                    )
                    with lock:
                        wins.append(i)
        finally:
            pool.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(wins) == 1
    assert len(rejects) == n - 1


def test_acquire_while_held_is_rejected(services):
    services.acquire("lift_down", "升降台席")
    with pytest.raises(services.LeaseError) as ei:
        services.acquire("lift_down", "飞行吊点席")
    assert ei.value.code == "lease_held"
    state = services.get_action_state("lift_down")
    assert state["status"] == "held"
    assert state["holder"] == "升降台席"
