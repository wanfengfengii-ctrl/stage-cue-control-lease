import type {
  ActionState,
  AnomalyRecord,
  HandoverRecord,
  SessionSummary,
} from "../src/api";

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
  /** Unique per granted lease; handover records reference it. Optional so
   * tests can inject a takeover lease literally (never matches a handover's
   * lease_id, which is exactly the "superseded" case). */
  leaseId?: number;
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
  holder: string;
  link_id: string | null;
  occurred_at: number;
}

const ANOMALY_CATEGORIES = ["equipment", "operation", "environment", "other"];

/** Internal handover row: the mock keeps the raw code (the real service
 * stores only its hash) so accept can compare it; the code is never
 * exposed through the snapshot. */
interface MockHandover {
  id: number;
  lease_id: number;
  code: string;
  initiator: string;
  initiator_id: string;
  created_at: string;
  status: "pending" | "accepted" | "invalidated";
  accepted_by: string | null;
  accepted_id: string | null;
  accepted_at: string | null;
  new_lease_id: number | null;
}

const CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";

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
  /** action_id -> handover records in creation order. */
  handovers: Record<string, MockHandover[]> = {};
  sessions: SessionRec[] = [];
  /** Every executed event with the session it was attributed to (if any). */
  eventLog: EventRec[] = [];
  /** When > 0, the next history request(s) fail (decremented per request). */
  failNextHistory = 0;
  private nextSessionId = 1;
  private nextEventId = 1;
  private nextAnomalyId = 1;
  private nextLeaseId = 1;
  private nextHandoverId = 1;

  private activeSession(): SessionRec | undefined {
    return this.sessions.find((s) => s.endedAt === null);
  }

  private recordEvent(actionId: string, holder: string, linkId: string | null = null) {
    this.events[actionId] = (this.events[actionId] ?? 0) + 1;
    const event_id = this.nextEventId++;
    this.lastEventIds[actionId] = event_id;
    this.eventLog.push({
      action_id: actionId,
      session_id: this.activeSession()?.id ?? null,
      event_id,
      holder,
      link_id: linkId,
      occurred_at: this.serverNow,
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

  /** The latest handover with its effective (query-time) status. */
  private handoverFor(id: string): HandoverRecord | null {
    const list = this.handovers[id];
    if (!list || list.length === 0) return null;
    return this.publicHandover(id, list[list.length - 1]);
  }

  /** Serialise a record, deriving 'invalidated' exactly like the service:
   * a pending record whose initiating lease is no longer the live one. */
  private publicHandover(id: string, h: MockHandover): HandoverRecord {
    let status = h.status;
    if (status === "pending") {
      const live = this.live(id);
      if (!live || live.leaseId !== h.lease_id) status = "invalidated";
    }
    return {
      id: h.id,
      lease_id: h.lease_id,
      initiator: h.initiator,
      initiator_id: h.initiator_id,
      created_at: h.created_at,
      status,
      accepted_by: h.accepted_by,
      accepted_id: h.accepted_id,
      accepted_at: h.accepted_at,
      new_lease_id: h.new_lease_id,
    };
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
      handover: this.handoverFor(id),
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
      this.recordEvent(it.action_id, live.holder, linkId);
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

  /** Mirrors GET /api/history: keyset-paginated, newest first, read-only. */
  private handleHistory(url: string) {
    if (this.failNextHistory > 0) {
      this.failNextHistory -= 1;
      return {
        status: 500,
        body: { detail: { code: "http_error", message: "模拟的历史查询失败" } },
      };
    }
    const u = new URL(url, "http://mock.local");
    const cursorRaw = u.searchParams.get("cursor");
    let cursor: number | null = null;
    if (cursorRaw !== null) {
      const v = cursorRaw.trim();
      if (!/^\d+$/.test(v) || Number(v) <= 0) {
        return {
          status: 400,
          body: {
            detail: {
              code: "history_cursor_invalid",
              message: "历史游标无效：请使用上一页响应返回的下一页游标（事件编号）",
            },
          },
        };
      }
      cursor = Number(v);
    }
    const limitRaw = Number(u.searchParams.get("limit") ?? "20");
    const limit =
      Number.isFinite(limitRaw) && limitRaw > 0
        ? Math.min(Math.floor(limitRaw), 50)
        : 20;
    const sorted = [...this.eventLog].sort((a, b) => b.event_id - a.event_id);
    const older =
      cursor === null ? sorted : sorted.filter((e) => e.event_id < cursor!);
    const page = older.slice(0, limit);
    // Mirrors the real service: a linked run is never split by the page
    // boundary — when the next older event shares the page's last link id,
    // it is pulled onto this page so the pair stays complete and adjacent.
    if (
      older.length > page.length &&
      page.length > 0 &&
      page[page.length - 1].link_id !== null &&
      older[page.length].link_id === page[page.length - 1].link_id
    ) {
      page.push(older[page.length]);
    }
    const hasMore = older.length > page.length;
    const events = page.map((e) => ({
      event_id: e.event_id,
      action_id: e.action_id,
      label: LABELS[e.action_id],
      holder: e.holder,
      result: "executed",
      link_id: e.link_id,
      occurred_at: new Date(e.occurred_at).toISOString(),
      session: e.session_id
        ? {
            id: e.session_id,
            name:
              this.sessions.find((s) => s.id === e.session_id)?.name ?? "",
          }
        : null,
      anomaly: this.anomalies[e.event_id] ?? null,
    }));
    return {
      status: 200,
      body: {
        events,
        next_cursor: hasMore ? page[page.length - 1].event_id : null,
      },
    };
  }

  /** Mirrors POST /api/actions/{id}/handover: one-time code, lease untouched. */
  private handleHandoverInitiate(id: string, payload: any) {
    if (!ACTION_IDS.includes(id)) {
      return this.err("unknown_action", "未知动作", id);
    }
    const token: string = payload.token ?? "";
    if (!token) return this.err("missing_token", "缺少令牌", id);
    const live = this.live(id);
    if (!live || live.token !== token) {
      return this.err(
        "control_lost",
        live
          ? "控制权已失效：该令牌已被新租约取代"
          : "控制权已失效：租约已到期、释放或执行",
        id,
      );
    }
    const initiatorId = (payload.initiator_id ?? "").trim();
    if (!initiatorId) {
      return {
        status: 400,
        body: {
          detail: {
            code: "invalid_handover",
            message: "缺少发起席控制台身份标识",
            state: this.state(id),
          },
        },
      };
    }
    // A fresh code replaces every previous pending record.
    for (const h of this.handovers[id] ?? []) {
      if (h.status === "pending") h.status = "invalidated";
    }
    const code = Array.from(
      { length: 8 },
      () => CODE_ALPHABET[Math.floor(Math.random() * CODE_ALPHABET.length)],
    ).join("");
    const record: MockHandover = {
      id: this.nextHandoverId++,
      lease_id: live.leaseId ?? 0,
      code,
      initiator: live.holder,
      initiator_id: initiatorId,
      created_at: new Date(this.serverNow).toISOString(),
      status: "pending",
      accepted_by: null,
      accepted_id: null,
      accepted_at: null,
      new_lease_id: null,
    };
    (this.handovers[id] ??= []).push(record);
    return {
      status: 200,
      body: {
        code,
        handover: this.publicHandover(id, record),
        state: this.state(id),
      },
    };
  }

  /** Mirrors POST /api/actions/{id}/handover/accept: all-or-nothing transfer. */
  private handleHandoverAccept(id: string, payload: any) {
    if (!ACTION_IDS.includes(id)) {
      return this.err("unknown_action", "未知动作", id);
    }
    const err = (
      status: number,
      code: string,
      message: string,
      handover?: HandoverRecord | null,
    ) => ({
      status,
      body: {
        detail: {
          code,
          message,
          handover: handover ?? null,
          state: this.state(id),
        },
      },
    });
    const recipient = (payload.recipient ?? "").trim();
    const recipientId = (payload.recipient_id ?? "").trim();
    if (!recipient || !recipientId) {
      return err(400, "invalid_handover", "接班席位名称与控制台标识不能为空");
    }
    const pending = [...(this.handovers[id] ?? [])]
      .reverse()
      .find((h) => h.status === "pending");
    if (!pending) {
      return err(
        409,
        "handover_not_found",
        "当前没有待接收的交接：请确认发起席已生成交接码",
      );
    }
    const live = this.live(id);
    if (!live || live.leaseId !== pending.lease_id) {
      const invalidated = this.publicHandover(id, {
        ...pending,
        status: "invalidated",
      });
      const current = this.leases[id];
      const dead =
        current && current.leaseId === pending.lease_id ? current : null;
      if (!dead) {
        return err(
          409,
          "handover_invalid",
          "交接已失效：发起租约已被新租约取代，请重新发起交接",
          invalidated,
        );
      }
      if (dead.released) {
        return err(
          409,
          "handover_invalid",
          "交接已失效：发起席已释放控制权，请重新发起交接",
          invalidated,
        );
      }
      if (dead.executed) {
        return err(
          409,
          "handover_invalid",
          "交接已失效：发起席已执行该动作，请重新发起交接",
          invalidated,
        );
      }
      return err(
        409,
        "handover_expired",
        "交接已失效：接收时租约刚好到期，请重新申请控制权",
        invalidated,
      );
    }
    const code = (payload.code ?? "")
      .toUpperCase()
      .replace(/[-\s]/g, "")
      .trim();
    if (code !== pending.code) {
      return err(
        409,
        "handover_code_invalid",
        "交接码错误，请核对后重试",
        this.publicHandover(id, pending),
      );
    }
    if (pending.initiator_id === recipientId) {
      return err(
        409,
        "handover_self_accept",
        "发起席不能接收自己的交接码，请由接班席（另一控制台）接收",
        this.publicHandover(id, pending),
      );
    }
    // All checks passed: terminate the old lease, issue the replacement.
    live.released = true;
    const token = `tok-${Math.random().toString(36).slice(2)}-${id}`;
    const newLease: Lease = {
      leaseId: this.nextLeaseId++,
      token,
      holder: recipient,
      expiresAt: this.serverNow + this.ttl,
      released: false,
      executed: false,
    };
    this.leases[id] = newLease;
    pending.status = "accepted";
    pending.accepted_by = recipient;
    pending.accepted_id = recipientId;
    pending.accepted_at = new Date(this.serverNow).toISOString();
    pending.new_lease_id = newLease.leaseId ?? null;
    return {
      status: 200,
      body: {
        token,
        holder: recipient,
        expires_at: new Date(newLease.expiresAt).toISOString(),
        ttl_seconds: 30,
        handover: this.publicHandover(id, pending),
        state: this.state(id),
      },
    };
  }

  /** Entry point used by the mocked global fetch. */
  handle(url: string, init?: { method?: string; body?: string }) {
    const method = init?.method ?? "GET";
    const payload = init?.body ? JSON.parse(init.body) : {};

    if (url.endsWith("/api/actions") && method === "GET") {
      return { status: 200, body: this.all() };
    }

    // Execution history carries a query string, so match the path prefix.
    if (/\/api\/history(\?|$)/.test(url) && method === "GET") {
      return this.handleHistory(url);
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

    // Shift handover routes (also two-segment, before the generic regex).
    m = url.match(/\/api\/actions\/([^/]+)\/handover\/accept$/);
    if (m && method === "POST") {
      return this.handleHandoverAccept(m[1], payload);
    }
    m = url.match(/\/api\/actions\/([^/]+)\/handover$/);
    if (m && method === "POST") {
      return this.handleHandoverInitiate(m[1], payload);
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
        leaseId: this.nextLeaseId++,
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
      this.recordEvent(id, live.holder);
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
