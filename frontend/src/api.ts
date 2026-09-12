// Typed API client. No fake/mock interface: every call hits the real
// FastAPI service over HTTP.

export interface ActionState {
  action_id: string;
  label: string;
  status: "held" | "free";
  holder: string | null;
  acquired_at: string | null;
  expires_at: string | null;
  remaining_seconds: number;
  last_executed_by: string | null;
  event_count: number;
  server_time: string;
}

export interface ActionsSnapshot {
  server_time: string;
  ttl_seconds: number;
  poll_interval_ms: number;
  actions: ActionState[];
}

export interface LeaseGrant {
  token: string;
  holder: string;
  acquired_at: string;
  expires_at: string;
  ttl_seconds: number;
  state: ActionState;
}

export interface ApiError extends Error {
  code: string;
  status: number;
  state: ActionState | null;
}

function makeError(status: number, body: any): ApiError {
  const detail = body?.detail;
  const code =
    (detail && typeof detail === "object" && detail.code) || "http_error";
  const message =
    (detail && typeof detail === "object" && detail.message) ||
    (typeof detail === "string" ? detail : `请求失败（HTTP ${status}）`);
  const err = new Error(message) as ApiError;
  err.code = code;
  err.status = status;
  err.state = detail?.state ?? null;
  return err;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const text = await res.text();
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) {
    throw makeError(res.status, body);
  }
  return body as T;
}

export const api = {
  listActions: () => request<ActionsSnapshot>("/api/actions"),

  acquire: (actionId: string, holder: string) =>
    request<LeaseGrant>(`/api/actions/${actionId}/lease`, {
      method: "POST",
      body: JSON.stringify({ holder }),
    }),

  renew: (actionId: string, token: string) =>
    request<{ expires_at: string; ttl_seconds: number; state: ActionState }>(
      `/api/actions/${actionId}/renew`,
      { method: "POST", body: JSON.stringify({ token }) },
    ),

  release: (actionId: string, token: string) =>
    request<{ released: boolean; state: ActionState }>(
      `/api/actions/${actionId}/release`,
      { method: "POST", body: JSON.stringify({ token }) },
    ),

  execute: (actionId: string, token: string) =>
    request<{
      executed: boolean;
      event_id: number;
      executed_by: string;
      occurred_at: string;
      state: ActionState;
    }>(`/api/actions/${actionId}/execute`, {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
};
