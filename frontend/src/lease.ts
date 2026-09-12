import type { ActionState } from "./api";

/**
 * Remaining lease seconds computed against the SERVER clock.
 *
 * The server alone decides expiry; the client only renders a countdown.
 * We anchor server time at the fetch instant (`fetchStartMs`, local clock)
 * so local clock skew cancels out:
 *
 *   estimatedServerNow = Date.parse(serverTime) + (localNow - fetchStartMs)
 *   remainingSeconds   = ceil((expiresAt - estimatedServerNow) / 1000)
 *
 * Equality semantics: at remaining == 0 the lease must already be shown as
 * invalid, mirroring the backend rule now == expires_at => expired.
 */
export function remainingSeconds(
  state: Pick<ActionState, "expires_at" | "server_time">,
  localNowMs: number,
  fetchStartMs: number,
  ttlSeconds = 30,
): number {
  if (!state.expires_at) return 0;
  const serverNowMs = Date.parse(state.server_time);
  const expiresMs = Date.parse(state.expires_at);
  const estimatedServerNow = serverNowMs + (localNowMs - fetchStartMs);
  const diffMs = expiresMs - estimatedServerNow;
  if (diffMs <= 0) return 0;
  return Math.min(ttlSeconds, Math.ceil(diffMs / 1000));
}

export interface Notice {
  kind: "success" | "error" | "info";
  text: string;
  at: number;
}

/** Decide whether a polled snapshot implies this session has lost control. */
export function detectTakeover(
  state: ActionState,
  mySeat: string,
  iHaveToken: boolean,
): boolean {
  return (
    iHaveToken &&
    (state.status !== "held" || state.holder !== mySeat)
  );
}

export function formatClock(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}
