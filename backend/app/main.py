"""Control-handover console API for the lifting platform / flying hoist."""
from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import anomalies, config, db, leases, sessions

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


class SessionTransitionBody(BaseModel):
    op: str = Field(..., description="场次状态转换：start 开始 / end 结束")
    # Defaults to "" so a missing name is judged uniformly by the service
    # (400 invalid_name) instead of a schema-level 422.
    name: str = Field(default="", description="场次名称（start 时必填）")


class AnomalyReportBody(BaseModel):
    # Defaults to "" so missing fields are judged uniformly by the service
    # (400 invalid_anomaly) instead of a schema-level 422.
    category: str = Field(default="", description="异常类别：equipment/operation/environment/other")
    description: str = Field(default="", description="现场异常说明，供下一班确认")
    reporter: str = Field(default="", description="报告席位名称")
    # Stable identity of the reporting browser console (independent of the
    # editable seat name); only a different console id may confirm.
    reporter_id: str = Field(default="", description="报告控制台稳定身份标识")


class AnomalyConfirmBody(BaseModel):
    confirmer: str = Field(default="", description="确认席位名称")
    confirmer_id: str = Field(default="", description="确认控制台稳定身份标识")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/actions")
def get_actions():
    """Snapshot of every action for short polling by all browser sessions.

    Also carries the current rehearsal-session summary (name, cumulative
    event count, distinct action count) so every console continuously shows
    the round in progress — or the frozen result of the last ended one.
    """
    return {
        "server_time": _iso_now(),
        "ttl_seconds": config.LEASE_TTL_SECONDS,
        "poll_interval_ms": config.POLL_INTERVAL_MS,
        "actions": leases.list_states(),
        "session": sessions.current_summary(),
    }


@app.post("/api/sessions/transition")
def session_transition(body: SessionTransitionBody):
    """The single session state-transition entry: start or end the round.

    Failures are recognisable business errors (duplicate start, blank name,
    no active session) that write nothing and touch no event; the current
    summary rides along so the console can re-render.
    """
    try:
        return sessions.transition(body.op, body.name)
    except leases.LeaseError as exc:
        raise HTTPException(status_code=exc.status, detail={
            "code": exc.code,
            "message": exc.message,
            "session": sessions.current_summary(),
        })


@app.get("/api/sessions/current")
def get_current_session():
    """The active session's summary, or the frozen summary of the last ended
    one — still queryable after the round is over."""
    return {"session": sessions.current_summary()}


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


def _anomaly_error(exc: anomalies.AnomalyError, action_id: str):
    raise HTTPException(status_code=exc.status, detail={
        "code": exc.code,
        "message": exc.message,
        # Repeat report/confirm: the CURRENT record rides along so the other
        # seat renders the authoritative pending/confirmed state.
        "anomaly": exc.anomaly,
        "state": leases.get_action_state(action_id)
        if action_id in config.ACTION_IDS
        else None,
    })


@app.post("/api/actions/{action_id}/anomaly")
def report_anomaly(action_id: str, body: AnomalyReportBody):
    """Report the on-site anomaly of the action's most recent executed event.

    Creates exactly one pending record on that event; a repeat report is a
    business error carrying the existing record and never overwrites it.
    """
    try:
        return anomalies.report(
            action_id,
            body.category,
            body.description,
            body.reporter,
            body.reporter_id,
        )
    except anomalies.AnomalyError as exc:
        _anomaly_error(exc, action_id)


@app.post("/api/actions/{action_id}/anomaly/confirm")
def confirm_anomaly(action_id: str, body: AnomalyConfirmBody):
    """Another seat acknowledges the pending record (pending -> confirmed).

    Only that one transition is legal; a repeat confirmation returns the
    current record and never re-stamps the first confirmer/time.
    """
    try:
        return anomalies.confirm(action_id, body.confirmer, body.confirmer_id)
    except anomalies.AnomalyError as exc:
        _anomaly_error(exc, action_id)


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
