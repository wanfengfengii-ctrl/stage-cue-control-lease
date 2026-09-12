import type { ActionState } from "../src/api";

/**
 * In-memory test double that mirrors the FastAPI lease rules:
 * one live lease per action, 30 s TTL judged by the *server* clock,
 * token hashes compared, expired/replaced tokens rejected with control_lost.
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

interface Lease {
  token: string;
  holder: string;
  expiresAt: number;
  released: boolean;
  executed: boolean;
}

export class MockServer {
  leases: Record<string, Lease> = {};
  serverNow = 1_700_000_000_000;
  ttl = 30_000;
  events: Record<string, number> = {};

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
      last_executed_by:
        this.leases[id]?.executed ? this.leases[id].holder : null,
      event_count: this.events[id] ?? 0,
      server_time: new Date(this.serverNow).toISOString(),
    };
  }

  private all(): { server_time: string; ttl_seconds: number; poll_interval_ms: number; actions: ActionState[] } {
    return {
      server_time: new Date(this.serverNow).toISOString(),
      ttl_seconds: 30,
      poll_interval_ms: 1000,
      actions: ACTION_IDS.map((id) => this.state(id)),
    };
  }

  private err(code: string, message: string, actionId: string) {
    return {
      status: code === "unknown_action" ? 404 : code === "missing_token" ? 401 : 409,
      body: { detail: { code, message, state: this.state(actionId) } },
    };
  }

  /** Entry point used by the mocked global fetch. */
  handle(url: string, init?: { method?: string; body?: string }) {
    const method = init?.method ?? "GET";
    const payload = init?.body ? JSON.parse(init.body) : {};

    if (url.endsWith("/api/actions") && method === "GET") {
      return { status: 200, body: this.all() };
    }

    const m = url.match(/\/api\/actions\/([^/]+)(?:\/(lease|renew|release|execute))?$/);
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
      const lease: Lease = {
        token,
        holder: payload.holder,
        expiresAt: this.serverNow + this.ttl,
        released: false,
        executed: false,
      };
      this.leases[id] = lease;
      return {
        status: 200,
        body: {
          token,
          holder: payload.holder,
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
      this.events[id] = (this.events[id] ?? 0) + 1;
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
