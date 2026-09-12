"""Control-handover console API for the lifting platform / flying hoist."""
from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import config, db, leases

app = FastAPI(title="舞台联排控制权交接台", version="1.0.0")

# In docker-compose the React app and API are same-origin through the nginx
# proxy; CORS is opened so local development (vite dev server) also works.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AcquireBody(BaseModel):
    holder: str = Field(..., description="申请席位名称，如“升降台控制席”")


class TokenBody(BaseModel):
    token: str = Field(..., description="授予时返回的不可猜测令牌")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/actions")
def get_actions():
    """Snapshot of every action for short polling by all browser sessions."""
    return {
        "server_time": _iso_now(),
        "ttl_seconds": config.LEASE_TTL_SECONDS,
        "poll_interval_ms": config.POLL_INTERVAL_MS,
        "actions": leases.list_states(),
    }


@app.get("/api/actions/{action_id}")
def get_action(action_id: str):
    try:
        return leases.get_action_state(action_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="未知动作")


@app.post("/api/actions/{action_id}/lease")
def acquire_lease(action_id: str, body: AcquireBody):
    try:
        return leases.acquire(action_id, body.holder)
    except leases.LeaseError as exc:
        raise HTTPException(status_code=exc.status, detail={
            "code": exc.code,
            "message": exc.message,
            "state": leases.get_action_state(action_id)
            if action_id in config.ACTION_IDS
            else None,
        })


def _extract_token(body: TokenBody, authorization: str | None) -> str:
    if body.token:
        return body.token
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    raise HTTPException(status_code=401, detail={"code": "missing_token",
                                                 "message": "缺少令牌"})


@app.post("/api/actions/{action_id}/renew")
def renew_lease(action_id: str, body: TokenBody,
                authorization: str | None = Header(default=None)):
    token = _extract_token(body, authorization)
    try:
        return leases.renew(action_id, token)
    except leases.LeaseError as exc:
        raise HTTPException(status_code=exc.status, detail={
            "code": exc.code, "message": exc.message,
            "state": leases.get_action_state(action_id)
            if action_id in config.ACTION_IDS else None,
        })


@app.post("/api/actions/{action_id}/release")
def release_lease(action_id: str, body: TokenBody,
                  authorization: str | None = Header(default=None)):
    token = _extract_token(body, authorization)
    try:
        return leases.release(action_id, token)
    except leases.LeaseError as exc:
        raise HTTPException(status_code=exc.status, detail={
            "code": exc.code, "message": exc.message,
            "state": leases.get_action_state(action_id)
            if action_id in config.ACTION_IDS else None,
        })


@app.post("/api/actions/{action_id}/execute")
def execute_action(action_id: str, body: TokenBody,
                   authorization: str | None = Header(default=None)):
    token = _extract_token(body, authorization)
    try:
        return leases.execute(action_id, token)
    except leases.LeaseError as exc:
        raise HTTPException(status_code=exc.status, detail={
            "code": exc.code, "message": exc.message,
            "state": leases.get_action_state(action_id)
            if action_id in config.ACTION_IDS else None,
        })


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
