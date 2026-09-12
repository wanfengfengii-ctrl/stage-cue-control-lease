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


class LinkedItem(BaseModel):
    action_id: str = Field(..., description="参与联动的动作编号")
    # Defaults to "" so a missing token is judged uniformly by the service
    # (401 naming the action) instead of a schema-level 422.
    token: str = Field(default="", description="该动作的当前有效令牌")


class LinkedExecuteBody(BaseModel):
    items: list[LinkedItem] = Field(
        ..., description="联动的动作与令牌；全部成功或全部不变"
    )


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


@app.post("/api/actions/execute-linked")
def execute_linked_actions(body: LinkedExecuteBody):
    """Atomic linked execution: every listed action commits, or none does.

    On failure the response names the concrete action whose token is
    missing/expired/superseded and carries fresh snapshots of all involved
    actions so the console can re-render real control state.
    """
    try:
        return leases.execute_linked(
            [{"action_id": it.action_id, "token": it.token} for it in body.items]
        )
    except leases.LeaseError as exc:
        involved = list(dict.fromkeys(it.action_id for it in body.items))
        raise HTTPException(status_code=exc.status, detail={
            "code": exc.code,
            "message": exc.message,
            "action_id": exc.action_id,
            "states": {
                aid: leases.get_action_state(aid)
                for aid in involved
                if aid in config.ACTION_IDS
            },
        })


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
