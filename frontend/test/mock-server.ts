import type { ActionState, AnomalyRecord, SessionSummary } from "../src/api";

/**
 * In-memory test double that mirrors the FastAPI lease rules:
 * one live lease per action, 30 s TTL judged by the *server* clock,
 * token hashes compared, expired/replaced tokens rejected with control_lost.
 * Sessions mirror the rehearsal-round rules: at most one active session,
 * events tagged with the session active at execution time, ended summaries
 * frozen but still queryable.
 *
 * It only exists for jsdom component tests; the shipped app and Playwright
 * suites talk to the real FastAPI + PostgreSQL service.
 */

const ACTION_IDS = [
  "lift_up",
  "lift_down",
  "hoist_fly_in",
  "hoist_fly_out",
  "emergency_stop",
];
const LABELS: Record<string, string> = {
  lift_up: "升降台 上升",
  lift_down: "升降台 下降",
  hoist_fly_in: "飞行吊点 进场",
  hoist_fly_out: "飞行吊点 退场",
  emergency_stop: "紧急停止（联排）",
};

const DEVICE_OF: Record<string, string> = {
  lift_up: "lift",
  lift_down: "lift",
  hoist_fly_in: "hoist",
  hoist_fly_out: "hoist",
};

interface Lease {
  token: string;
  holder: string;
  expiresAt: number;
  released: boolean;
  executed: boolean;
}

interface SessionRec {
  id: number;
  name: string;
  startedAt: number;
  endedAt: number | null;
}

interface EventRec {
  action_id: string;
  session_id: number | null;
  event_id: number;
}

const ANOMALY_CATEGORIES = ["equipment", "operation", "environment", "other"];

export class MockServer {
  leases: Record<string, Lease> = {};
  serverNow = 1_700_000_000_000;
  ttl = 30_000;
  events: Record<string, number> = {};
  /** action_id -> holder of the most recent execution event. */
  lastExecutedBy: Record<string, string> = {};
  /** action_id -> link id of the most recent linked execution. */
  links: Record<string, string> = {};
  /** action_id -> global id of the most recent execution event. */
  lastEventIds: Record<string, number> = {};
  /** event_id -> its anomaly record (at most one per event). */
  anomalies: Record<number, AnomalyRecord> = {};
  sessions: SessionRec[] = [];
  /** Every executed event with the session it was attributed to (if any). */
  eventLog: EventRec[] = [];
  private nextSessionId = 1;
  private nextEventId = 1;
  private nextAnomalyId = 1;

  private activeSession(): SessionRec | undefined {
    return this.sessions.find((s) => s.endedAt === null);
  }

  private recordEvent(actionId: string) {
    this.events[actionId] = (this.events[actionId] ?? 0) + 1;
    const event_id = this.nextEventId++;
    this.lastEventIds[actionId] = event_id;
    this.eventLog.push({
      action_id: actionId,
      session_id: this.activeSession()?.id ?? null,
      event_id,
    });
  }

  /** Active session, else the most recently ended one (frozen summary). */
  sessionSummary(): SessionSummary | null {
    const s = this.activeSession() ?? this.sessions[this.sessions.length - 1];
    if (!s) return null;
    const evts = this.eventLog.filter((e) => e.session_id === s.id);
    return {
      id: s.id,
      name: s.name,
      status: s.endedAt === null ? "active" : "ended",
      started_at: new Date(s.startedAt).toISOString(),
      ended_at: s.endedAt ? new Date(s.endedAt).toISOString() : null,
      event_count: evts.length,
      action_count: new Set(evts.map((e) => e.action_id)).size,
    };
  }

  /** Move the server clock past every outstanding lease. */
  elapse30s() {
    this.serverNow += this.ttl + 1_000;
  }

  private latest(id: string): Lease | undefined {
    return this.leases[id];
  }

  private live(id: string): Lease | undefined {
    const l = this.latest(id);
    if (!l || l.released || l.executed) return undefined;
    // equality == expiry means invalid
    return this.serverNow < l.expiresAt ? l : undefined;
  }

  /** The anomaly attached to the action's most recent event, if any. */
  private anomalyFor(id: string): AnomalyRecord | null {
    const eventId = this.lastEventIds[id];
    return eventId ? this.anomalies[eventId] ?? null : null;
  }

  private state(id: string): ActionState {
    const l = this.live(id);
    return {
      action_id: id,
      label: LABELS[id],
      status: l ? "held" : "free",
      holder: l ? l.holder : null,
      acquired_at: l ? new Date(this.serverNow).toISOString() : null,
      expires_at: l ? new Date(l.expiresAt).toISOString() : null,
      remaining_seconds: l
        ? Math.max(0, Math.ceil((l.expiresAt - this.serverNow) / 1000))
        : 0,
      last_executed_by: this.lastExecutedBy[id] ?? null,
      event_count: this.events[id] ?? 0,
      last_link_id: this.links[id] ?? null,
      last_event_id: this.lastEventIds[id] ?? null,
      anomaly: this.anomalyFor(id),
      server_time: new Date(this.serverNow).toISOString(),
    };
  }

  private all(): { server_time: string; ttl_seconds: number; poll_interval_ms: number; actions: ActionState[]; session: SessionSummary | null } {
    return {
      server_time: new Date(this.serverNow).toISOString(),
      ttl_seconds: 30,
      poll_interval_ms: 1000,
      actions: ACTION_IDS.map((id) => this.state(id)),
      session: this.sessionSummary(),
    };
  }

  private err(code: string, message: string, actionId: string) {
    return {
      status: code === "unknown_action" ? 404 : code === "missing_token" ? 401 : 409,
      body: { detail: { code, message, state: this.state(actionId) } },
    };
  }

  private statesFor(ids: string[]) {
    return Object.fromEntries(
      ids.filter((id) => ACTION_IDS.includes(id)).map((id) => [id, this.state(id)]),
    );
  }

  /** Mirrors POST /api/actions/execute-linked: all commit or none does. */
  private handleLinked(payload: any) {
    const items: { action_id: string; token?: string }[] = payload.items ?? [];
    if (items.length !== 2) {
      return {
        status: 400,
        body: { detail: { code: "invalid_request", message: "联动执行必须恰好包含两个动作" } },
      };
    }
    const ids = items.map((it) => it.action_id);
    if (new Set(ids).size !== ids.length) {
      return {
        status: 400,
        body: { detail: { code: "invalid_request", message: "联动动作不得重复" } },
      };
    }
    for (const id of ids) {
      if (!ACTION_IDS.includes(id)) {
        return {
          status: 404,
          body: { detail: { code: "unknown_action", message: "未知动作", action_id: id } },
        };
      }
    }
    // Exactly one platform action + one hoist action: cross-device only.
    const devices = ids.map((id) => DEVICE_OF[id]);
    if (
      devices.some((d) => d === undefined) ||
      new Set(devices).size !== 2
    ) {
      return {
        status: 400,
        body: {
          detail: {
            code: "invalid_request",
            message:
              "联动执行只允许升降台与飞行吊点的跨设备组合，同一设备的两个方向（如上升与下降）不得联动",
          },
        },
      };
    }
    // Validate EVERY token first; any failure aborts the whole run.
    for (const it of items) {
      if (!it.token) {
        return {
          status: 401,
          body: {
            detail: {
              code: "missing_token",
              message: "缺少令牌：联动执行要求每个动作都携带有效令牌",
              action_id: it.action_id,
              states: this.statesFor(ids),
            },
          },
        };
      }
      const live = this.live(it.action_id);
      if (!live || live.token !== it.token) {
        return {
          status: 409,
          body: {
            detail: {
              code: "control_lost",
              message: live
                ? "控制权已失效：该令牌已被新租约取代"
                : "控制权已失效：租约已到期、释放或执行",
              action_id: it.action_id,
              states: this.statesFor(ids),
            },
          },
        };
      }
    }
    const linkId = `link-${Math.random().toString(36).slice(2)}`;
    const events = items.map((it) => {
      const live = this.live(it.action_id)!;
      live.executed = true;
      this.recordEvent(it.action_id);
      this.lastExecutedBy[it.action_id] = live.holder;
      this.links[it.action_id] = linkId;
      return {
        action_id: it.action_id,
        event_id: this.events[it.action_id],
        executed_by: live.holder,
        occurred_at: new Date(this.serverNow).toISOString(),
      };
    });
    return {
      status: 200,
      body: { linked: true, link_id: linkId, events, states: this.statesFor(ids) },
    };
  }

  /** Mirrors POST /api/sessions/transition: start or end the round. */
  private handleSessionTransition(payload: any) {
    const op = (payload.op ?? "").trim();
    const err = (status: number, code: string, message: string) => ({
      status,
      body: { detail: { code, message, session: this.sessionSummary() } },
    });
    if (op === "start") {
      const name = (payload.name ?? "").trim();
      if (!name) return err(400, "invalid_name", "场次名称不能为空");
      if (this.activeSession()) {
        return err(409, "session_active", "已有进行中的场次，请先结束当前场次");
      }
      this.sessions.push({
        id: this.nextSessionId++,
        name,
        startedAt: this.serverNow,
        endedAt: null,
      });
      return { status: 200, body: this.sessionSummary() };
    }
    if (op === "end") {
      const active = this.activeSession();
      if (!active) return err(409, "no_active_session", "当前没有进行中的场次");
      active.endedAt = this.serverNow;
      return { status: 200, body: this.sessionSummary() };
    }
    return err(400, "invalid_request", "未知的场次操作（仅支持 start / end）");
  }

  /** Mirrors POST /api/actions/{id}/anomaly: one pending record per event. */
  private handleAnomalyReport(id: string, payload: any) {
    const category = (payload.category ?? "").trim();
    const description = (payload.description ?? "").trim();
    const reporter = (payload.reporter ?? "").trim();
    const reporterId = (payload.reporter_id ?? "").trim();
    const err = (status: number, code: string, message: string, anomaly?: AnomalyRecord) => ({
      status,
      body: {
        detail: {
          code,
          message,
          anomaly: anomaly ?? null,
          state: this.state(id),
        },
      },
    });
    if (
      !ANOMALY_CATEGORIES.includes(category) ||
      !description ||
      !reporter ||
      !reporterId
    ) {
      return err(400, "invalid_anomaly", "异常类别、说明、报告席位与控制台标识均不能为空");
    }
    const eventId = this.lastEventIds[id];
    if (!eventId) {
      return err(409, "no_execution_event", "该动作尚无已执行事件，无法报告异常");
    }
    const existing = this.anomalies[eventId];
    if (existing) {
      return err(409, "anomaly_exists", "该执行事件已存在异常记录，请勿重复报告", existing);
    }
    const record: AnomalyRecord = {
      id: this.nextAnomalyId++,
      event_id: eventId,
      category,
      description,
      reported_by: reporter,
      reporter_id: reporterId,
      reported_at: new Date(this.serverNow).toISOString(),
      status: "pending",
      confirmed_by: null,
      confirmer_id: null,
      confirmed_at: null,
    };
    this.anomalies[eventId] = record;
    return { status: 200, body: { anomaly: record, state: this.state(id) } };
  }

  /** Mirrors POST /api/actions/{id}/anomaly/confirm: pending -> confirmed once. */
  private handleAnomalyConfirm(id: string, payload: any) {
    const confirmer = (payload.confirmer ?? "").trim();
    const confirmerId = (payload.confirmer_id ?? "").trim();
    const err = (status: number, code: string, message: string, anomaly?: AnomalyRecord | null) => ({
      status,
      body: {
        detail: {
          code,
          message,
          anomaly: anomaly ?? null,
          state: this.state(id),
        },
      },
    });
    if (!confirmer || !confirmerId) {
      return err(400, "invalid_anomaly", "确认席位与控制台标识不能为空");
    }
    const eventId = this.lastEventIds[id];
    const record = eventId ? this.anomalies[eventId] : undefined;
    if (!record) {
      return err(409, "anomaly_not_found", "当前执行事件没有待确认的异常记录");
    }
    // Only ANOTHER browser console (stable id, not the editable seat name)
    // may acknowledge the report.
    if (record.reporter_id === confirmerId) {
      return err(
        409,
        "anomaly_self_confirm",
        "报告席位不能自行确认，请由下一班（另一席）确认已看到",
        record,
      );
    }
    if (record.status !== "pending") {
      return err(409, "anomaly_confirmed", "该异常记录已确认，请勿重复确认", record);
    }
    record.status = "confirmed";
    record.confirmed_by = confirmer;
    record.confirmer_id = confirmerId;
    record.confirmed_at = new Date(this.serverNow).toISOString();
    return { status: 200, body: { anomaly: { ...record }, state: this.state(id) } };
  }

  /** Entry point used by the mocked global fetch. */
  handle(url: string, init?: { method?: string; body?: string }) {
    const method = init?.method ?? "GET";
    const payload = init?.body ? JSON.parse(init.body) : {};

    if (url.endsWith("/api/actions") && method === "GET") {
      return { status: 200, body: this.all() };
    }

    if (url.endsWith("/api/sessions/transition") && method === "POST") {
      return this.handleSessionTransition(payload);
    }
    if (url.endsWith("/api/sessions/current") && method === "GET") {
      return { status: 200, body: { session: this.sessionSummary() } };
    }

    // Linked execution must be matched before the single-action regex,
    // which would otherwise read "execute-linked" as an action id.
    if (url.endsWith("/api/actions/execute-linked") && method === "POST") {
      return this.handleLinked(payload);
    }

    // Anomaly confirm likewise before the single-action regex (two segments).
    let m = url.match(/\/api\/actions\/([^/]+)\/anomaly\/confirm$/);
    if (m && method === "POST") {
      return this.handleAnomalyConfirm(m[1], payload);
    }
    m = url.match(/\/api\/actions\/([^/]+)\/anomaly$/);
    if (m && method === "POST") {
      return this.handleAnomalyReport(m[1], payload);
    }

    m = url.match(/\/api\/actions\/([^/]+)(?:\/(lease|renew|release|execute))?$/);
    if (!m) return { status: 404, body: { detail: "not found" } };
    const id = m[1];
    const op = m[2];
    if (!ACTION_IDS.includes(id)) {
      return this.err("unknown_action", "未知动作", id);
    }

    if (op === "lease" && method === "POST") {
      if (this.live(id)) {
        return this.err("lease_held", `该动作已由 ${this.live(id)!.holder} 持有`, id);
      }
      const token = `tok-${Math.random().toString(36).slice(2)}-${id}`;
      const holder = (payload.holder ?? "").trim();
      const lease: Lease = {
        token,
        holder,
        expiresAt: this.serverNow + this.ttl,
        released: false,
        executed: false,
      };
      this.leases[id] = lease;
      return {
        status: 200,
        body: {
          token,
          holder,
          acquired_at: new Date(this.serverNow).toISOString(),
          expires_at: new Date(lease.expiresAt).toISOString(),
          ttl_seconds: 30,
          state: this.state(id),
        },
      };
    }

    if (["renew", "release", "execute"].includes(op ?? "")) {
      const token: string = payload.token ?? "";
      const live = this.live(id);
      if (!token) return this.err("missing_token", "缺少令牌", id);
      if (!live || live.token !== token) {
        return this.err(
          "control_lost",
          live
            ? "控制权已失效：该令牌已被新租约取代"
            : "控制权已失效：租约已到期、释放或执行",
          id,
        );
      }
      if (op === "renew") {
        live.expiresAt = this.serverNow + this.ttl;
        return {
          status: 200,
          body: {
            expires_at: new Date(live.expiresAt).toISOString(),
            ttl_seconds: 30,
            state: this.state(id),
          },
        };
      }
      if (op === "release") {
        live.released = true;
        return { status: 200, body: { released: true, state: this.state(id) } };
      }
      // execute: exactly one event
      live.executed = true;
      this.recordEvent(id);
      this.lastExecutedBy[id] = live.holder;
      return {
        status: 200,
        body: {
          executed: true,
          event_id: this.events[id],
          executed_by: live.holder,
          occurred_at: new Date(this.serverNow).toISOString(),
          state: this.state(id),
        },
      };
    }

    return { status: 404, body: { detail: "not found" } };
  }

  installFetch() {
    const server = this;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: { method?: string; body?: string }) => {
        const r = server.handle(url, init);
        return {
          ok: r.status < 400,
          status: r.status,
          text: async () => JSON.stringify(r.body),
        };
      }),
    );
  }
}
