"""Pytest fixtures: a real PostgreSQL instance, isolated database per test.

The instance URL comes from TEST_DATABASE_URL (default matches the local
extracted PostgreSQL used in this workspace).  In Docker verification, the
compose `verify` service points it at the compose `db` service.  No mocks.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import psycopg
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres@127.0.0.1:55432/postgres",
)


@pytest.fixture(scope="session")
def pg_admin_url() -> str:
    deadline = time.time() + 30
    while True:
        try:
            with psycopg.connect(ADMIN_URL, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            return ADMIN_URL
        except psycopg.OperationalError:
            if time.time() > deadline:
                raise
            time.sleep(0.5)


@pytest.fixture()
def db_pool(pg_admin_url, monkeypatch):
    """Point the app at an isolated database for each test, reset pools."""
    dbname = f"test_{os.getpid()}_{int(time.time() * 1000) % 1_000_000}"
    with psycopg.connect(pg_admin_url, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {dbname}")
        conn.execute(f"CREATE DATABASE {dbname}")
    uri = pg_admin_url.rsplit("/", 1)[0] + "/" + dbname

    from app import config, db as db_mod

    monkeypatch.setattr(config, "DATABASE_URL", uri)
    db_mod._pool = None
    db_mod.init_db()
    yield db_mod
    db_mod.get_pool().close()
    db_mod._pool = None


@pytest.fixture()
def services(db_pool):
    from app import leases

    return leases
