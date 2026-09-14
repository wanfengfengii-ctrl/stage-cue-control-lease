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

/**
 * Decide whether THIS console's lease was handed over (换班交接) to another
 * console.  Seat names are freely editable and may coincide across two
 * consoles, so the stable console ids on the accepted handover record are
 * the only reliable signal: the initiator is me, the receiver is not.
 */
export function detectHandoverTransferred(
  state: ActionState,
  consoleId: string,
): boolean {
  const h = state.handover;
  return (
    !!h &&
    h.status === "accepted" &&
    h.initiator_id === consoleId &&
    h.accepted_id !== consoleId
  );
}

export function formatClock(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

/** Date + time for the execution history, where the day matters too. */
export function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const date = d.toLocaleDateString("zh-CN");
  const time = d.toLocaleTimeString("zh-CN", { hour12: false });
  return `${date} ${time}`;
}
